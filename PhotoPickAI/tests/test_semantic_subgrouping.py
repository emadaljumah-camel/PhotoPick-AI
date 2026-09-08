from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.config import AppSettings
from app.core.models import DuplicateGroup, ImageRecord
from app.core.vision_llm_reviewer import VisionLLMReviewer
from server.config import ServerConfig
from server.job_store import JobStore
from server.vision_review_adapter import (
    _all_singleton_decisions,
    _from_vision_subgroups,
)


def image_record(photo_id: str, filename: str, orientation: str = "landscape") -> ImageRecord:
    return ImageRecord(
        id=photo_id,
        filename=filename,
        path=filename,
        file_size=1000,
        width=1200 if orientation == "landscape" else 800,
        height=800 if orientation == "landscape" else 1200,
        orientation=orientation,
        capture_time="2026-01-01T10:00:00",
    )


def semantic_response(
    filenames: list[str],
    subgroups: list[dict],
    *,
    observations: dict[str, tuple[str, str]] | None = None,
    transition_overrides: dict[tuple[str, str], bool] | None = None,
) -> str:
    subgroup_by_filename = {
        filename: subgroup_index
        for subgroup_index, subgroup in enumerate(subgroups)
        for filename in subgroup["images"]
    }
    observations = observations or {
        filename: (f"Subject in {filename}", f"Visible scene in {filename}")
        for filename in filenames
    }
    transition_overrides = transition_overrides or {}
    return json.dumps(
        {
            "photo_observations": [
                {
                    "filename": filename,
                    "principal_subject": observations[filename][0],
                    "visible_scene": observations[filename][1],
                }
                for filename in filenames
            ],
            "adjacent_transitions": [
                {
                    "from": source,
                    "to": target,
                    "same_best_frame_decision": transition_overrides.get(
                        (source, target),
                        subgroup_by_filename[source] == subgroup_by_filename[target],
                    ),
                    "reason": "Adjacent semantic comparison.",
                }
                for source, target in zip(filenames, filenames[1:])
            ],
            "subgroups": subgroups,
        }
    )


def pair_transition_response(
    first: str,
    second: str,
    same_best_frame_decision: bool,
    *,
    first_subject: str = "First visible subject",
    first_scene: str = "First visible scene",
    second_subject: str = "Second visible subject",
    second_scene: str = "Second visible scene",
) -> str:
    return json.dumps(
        {
            "photo_observations": [
                {
                    "filename": first,
                    "principal_subject": first_subject,
                    "visible_scene": first_scene,
                },
                {
                    "filename": second,
                    "principal_subject": second_subject,
                    "visible_scene": second_scene,
                },
            ],
            "same_best_frame_decision": same_best_frame_decision,
            "reason": "Adjacent pair decision.",
        }
    )


def pair_winner_response(first: str, second: str, winner: str) -> str:
    return json.dumps(
        {
            "photo_observations": [
                {
                    "filename": first,
                    "principal_subject": "Shared subject",
                    "visible_scene": "First candidate quality.",
                },
                {
                    "filename": second,
                    "principal_subject": "Shared subject",
                    "visible_scene": "Second candidate quality.",
                },
            ],
            "winner": winner,
            "reason": f"{winner} is visually stronger.",
        }
    )


class StubVisionReviewer(VisionLLMReviewer):
    def __init__(
        self,
        responses: str | list[str],
        allow_subgroups: bool = True,
        retry_model_json_once: bool = True,
    ) -> None:
        super().__init__(
            AppSettings(),
            allow_subgroups=allow_subgroups,
            retry_model_json_once=retry_model_json_once,
        )
        self.responses = responses if isinstance(responses, list) else [responses]
        self.calls = 0

    def _build_payload(self, prompt, image_inputs):
        return {"prompt": prompt, "image_count": len(image_inputs)}

    def _post_json(self, payload):
        response = self.responses[min(self.calls, len(self.responses) - 1)]
        self.calls += 1
        if isinstance(response, Exception):
            raise response
        return response


class SemanticSubgroupingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.images = [
            image_record("p1", "environment.jpg"),
            image_record("p2", "subject_1.jpg"),
            image_record("p3", "subject_2.jpg"),
            image_record("p4", "subject_3.jpg"),
            image_record("p5", "subject_4.jpg"),
        ]

    def test_environmental_frame_and_subject_sequence_produce_two_picks(self) -> None:
        response = semantic_response(
            [image.filename for image in self.images],
            [
                {
                    "name": "Independent environmental frame",
                    "main_subject": "Environment without a principal foreground subject",
                    "images": ["environment.jpg"],
                    "representative": "environment.jpg",
                    "reason": "Independent environmental interpretation.",
                },
                {
                    "name": "Principal-subject sequence",
                    "main_subject": "Same principal subject set moving through the scene",
                    "images": [
                        "subject_1.jpg",
                        "subject_2.jpg",
                        "subject_3.jpg",
                        "subject_4.jpg",
                    ],
                    "representative": "subject_4.jpg",
                    "reason": "Same best-frame decision with timing and position variations.",
                },
            ],
        )
        reviewer = StubVisionReviewer(response)

        selected, subgroups, _ = reviewer._review_image_set(self.images, "broad-group")
        decisions = _from_vision_subgroups(
            "G001",
            self.images,
            subgroups,
            "semantic review",
        )

        self.assertEqual(reviewer.calls, 1)
        self.assertEqual(selected, ["environment.jpg", "subject_4.jpg"])
        self.assertEqual(
            [decision.photos for decision in decisions],
            [["p1"], ["p2", "p3", "p4", "p5"]],
        )
        self.assertEqual(
            [decision.representatives for decision in decisions],
            [["p1"], ["p5"]],
        )

    def test_prompt_separates_partition_rules_from_winner_rules(self) -> None:
        reviewer = VisionLLMReviewer(AppSettings())

        prompt = reviewer._build_semantic_review_prompt(self.images, "G001")

        self.assertIn("Which photographs compete for the same best-frame decision?", prompt)
        self.assertIn("CONSERVATIVE RETENTION POLICY", prompt)
        self.assertIn("genuinely interchangeable choices for one keeper", prompt)
        self.assertIn("Prefer retaining an extra representative", prompt)
        self.assertIn("ARCHITECTURE AND VIEWPOINT RULES", prompt)
        self.assertIn("materially different facades, corners, courtyards, interiors, exteriors", prompt)
        self.assertIn("materially different camera positions", prompt)
        self.assertIn("SUBGROUP CRITERIA", prompt)
        self.assertIn("WINNER CRITERIA", prompt)
        self.assertIn("expression, gesture, eye direction, pose, walking stride", prompt)
        self.assertIn("tiny or distant incidental person", prompt)
        self.assertIn("portrait and landscape versions", prompt)
        self.assertIn("must not override the conservative retention policy", prompt)
        self.assertIn("significant principal person, animal, vehicle, or object appearing or disappearing", prompt)
        self.assertIn("Winner criteria must not be used to create extra subgroups", prompt)
        self.assertIn("exactly one photo_observations entry for every photograph", prompt)
        self.assertIn("compare every adjacent pair in the listed order", prompt)
        self.assertIn(
            "it does not prevent a later photograph from joining an earlier visual subgroup",
            prompt,
        )
        self.assertIn("Non-adjacent photographs may share a subgroup", prompt)

    def test_prompt_keeps_same_scene_orientation_variants_together(self) -> None:
        images = [
            image_record("p1", "corridor_landscape.jpg", "landscape"),
            image_record("p2", "corridor_portrait.jpg", "portrait"),
        ]
        reviewer = VisionLLMReviewer(AppSettings())

        prompt = reviewer._build_semantic_review_prompt(images, "G001")

        self.assertIn("ORIENTATION AND CROP VARIANT RULES", prompt)
        self.assertIn(
            "Landscape and portrait photographs must remain in the same subgroup",
            prompt,
        )
        self.assertIn("strongly overlapping scene content", prompt)
        self.assertIn(
            "more wall, floor, sky, foreground, or the continuation of a doorway",
            prompt,
        )
        self.assertIn(
            "When the difference is primarily framing, the photographs compete for one keeper",
            prompt,
        )
        self.assertIn(
            "materially important subject, action, architectural feature, or storytelling element",
            prompt,
        )

    def test_material_scene_change_creates_an_adjacent_boundary(self) -> None:
        images = [
            image_record("p1", "scene_a_1.jpg"),
            image_record("p2", "scene_a_2.jpg"),
            image_record("p3", "scene_a_3.jpg"),
            image_record("p4", "scene_b_1.jpg"),
        ]
        reviewer = StubVisionReviewer(
            semantic_response(
                [image.filename for image in images],
                [
                    {
                        "name": "Shared scene",
                        "images": ["scene_a_1.jpg", "scene_a_2.jpg", "scene_a_3.jpg"],
                        "representative": "scene_a_1.jpg",
                    },
                    {
                        "name": "Different scene",
                        "images": ["scene_b_1.jpg"],
                        "representative": "scene_b_1.jpg",
                    },
                ],
                observations={
                    "scene_a_1.jpg": ("Shared scene subject", "First photographic scene"),
                    "scene_a_2.jpg": ("Shared scene subject", "First photographic scene"),
                    "scene_a_3.jpg": ("Shared scene subject", "First photographic scene"),
                    "scene_b_1.jpg": ("Different scene subject", "Materially different photographic scene"),
                },
            )
        )

        result = reviewer.review_group(
            DuplicateGroup(
                group_id="G006",
                images=images,
                capture_span_seconds=12,
                avg_similarity=0.9,
                group_type="Near-duplicate",
            )
        )

        self.assertFalse(result.semantic_fallback)
        self.assertEqual(
            result.semantic_contract_version,
            "group-partition-v1",
        )
        self.assertEqual(reviewer.calls, 1)
        self.assertEqual(
            [subgroup["images"] for subgroup in result.subgroups],
            [
                ["scene_a_1.jpg", "scene_a_2.jpg", "scene_a_3.jpg"],
                ["scene_b_1.jpg"],
            ],
        )
        self.assertEqual(
            result.selected_filenames,
            ["scene_a_1.jpg", "scene_b_1.jpg"],
        )
        self.assertEqual(
            result.photo_observations[-1]["principal_subject"],
            "Different scene subject",
        )
        self.assertFalse(
            result.adjacent_transitions[-1]["same_best_frame_decision"]
        )
        self.assertEqual(result.reconciliation_comparisons, [])
        self.assertEqual(result.winner_comparisons, [])

    def test_complete_group_partition_is_returned_in_one_call(self) -> None:
        images = [
            image_record("p1", "sequence_1.jpg"),
            image_record("p2", "sequence_2.jpg"),
            image_record("p3", "sequence_3.jpg"),
            image_record("p4", "sequence_4.jpg"),
        ]
        reviewer = StubVisionReviewer(
            semantic_response(
                [image.filename for image in images],
                [
                    {
                        "name": "Complete sequence",
                        "images": [image.filename for image in images],
                        "representative": "sequence_1.jpg",
                    }
                ],
            )
        )
        group = DuplicateGroup(
            group_id="G001",
            images=images,
            capture_span_seconds=5,
            avg_similarity=0.9,
            group_type="Near-duplicate",
        )

        result = reviewer.review_group(group)

        self.assertEqual(reviewer.calls, 1)
        self.assertEqual(len(result.subgroups), 1)
        self.assertEqual(
            result.subgroups[0]["images"],
            [image.filename for image in images],
        )
        self.assertEqual(result.reconciliation_comparisons, [])
        self.assertEqual(
            result.selected_filenames,
            ["sequence_1.jpg"],
        )

    def test_openai_payload_interleaves_text_label_before_each_image(self) -> None:
        reviewer = VisionLLMReviewer(
            AppSettings(
                vision_llm_endpoint="http://127.0.0.1:1234/v1/chat/completions"
            )
        )
        image_inputs = [
            (Path("first.jpg"), "1. first.jpg | photo_id=p1"),
            (Path("second.jpg"), "2. second.jpg | photo_id=p2"),
        ]
        reviewer._encode_labeled_image_data_url = lambda path, label: (
            f"data:image/jpeg;base64,{path.stem}-{label}"
        )

        payload = reviewer._build_payload("prompt", image_inputs)
        content = payload["messages"][0]["content"]

        self.assertEqual(
            [item["type"] for item in content],
            ["text", "text", "image_url", "text", "image_url"],
        )
        self.assertIn("first.jpg | photo_id=p1", content[1]["text"])
        self.assertIn("second.jpg | photo_id=p2", content[3]["text"])

    def test_invalid_pair_response_retries_once(self) -> None:
        images = [
            image_record("p1", "a.jpg"),
            image_record("p2", "b.jpg"),
        ]
        reviewer = StubVisionReviewer(
            [
                "not json",
                pair_transition_response("a.jpg", "b.jpg", False),
            ]
        )

        transition, _, errors, attempts = (
            reviewer._review_adjacent_pair_with_retry(
                images[0],
                images[1],
                "G001 pair 1",
            )
        )

        self.assertEqual(reviewer.calls, 2)
        self.assertEqual(attempts, 2)
        self.assertEqual(len(errors), 1)
        self.assertFalse(transition["same_best_frame_decision"])
        self.assertFalse(transition["fallback"])

    def test_failed_group_review_keeps_every_photo(self) -> None:
        images = [
            image_record("p1", "a.jpg"),
            image_record("p2", "b.jpg"),
        ]
        reviewer = StubVisionReviewer(["not json", "still not json"])
        group = DuplicateGroup(
            group_id="G001",
            images=images,
            capture_span_seconds=1,
            avg_similarity=0.9,
            group_type="Near-duplicate",
        )

        result = reviewer.review_group(group)

        self.assertEqual(reviewer.calls, 2)
        self.assertEqual(
            [subgroup["images"] for subgroup in result.subgroups],
            [["a.jpg"], ["b.jpg"]],
        )
        self.assertTrue(result.semantic_fallback)
        self.assertEqual(result.adjacent_transitions, [])

    def test_semantic_contract_rejects_missing_photo_observation(self) -> None:
        filenames = ["a.jpg", "b.jpg"]
        response = json.loads(
            semantic_response(
                filenames,
                [
                    {
                        "images": filenames,
                        "representative": "a.jpg",
                    }
                ],
            )
        )
        response["photo_observations"].pop()
        reviewer = VisionLLMReviewer(AppSettings())

        with self.assertRaisesRegex(
            RuntimeError,
            "photo_observations must contain exactly one entry per photo",
        ):
            reviewer._parse_semantic_response(json.dumps(response), filenames)

    def test_semantic_contract_rejects_missing_adjacent_transition(self) -> None:
        filenames = ["a.jpg", "b.jpg", "c.jpg"]
        response = json.loads(
            semantic_response(
                filenames,
                [
                    {
                        "images": filenames,
                        "representative": "a.jpg",
                    }
                ],
            )
        )
        response["adjacent_transitions"].pop()
        reviewer = VisionLLMReviewer(AppSettings())

        with self.assertRaisesRegex(
            RuntimeError,
            "adjacent_transitions must contain exactly one entry",
        ):
            reviewer._parse_semantic_response(json.dumps(response), filenames)

    def test_semantic_contract_normalizes_subgroups_to_input_order(self) -> None:
        filenames = [
            "portrait_1.jpg",
            "portrait_2.jpg",
            "sunset.jpg",
            "boat_1.jpg",
            "boat_2.jpg",
        ]
        response = semantic_response(
            filenames,
            [
                {
                    "images": ["portrait_1.jpg", "portrait_2.jpg"],
                    "representative": "portrait_1.jpg",
                },
                {
                    "images": ["boat_1.jpg", "boat_2.jpg"],
                    "representative": "boat_1.jpg",
                },
                {
                    "images": ["sunset.jpg"],
                    "representative": "sunset.jpg",
                },
            ],
        )
        reviewer = VisionLLMReviewer(AppSettings())

        subgroups, _, _, parsed_subgroups = reviewer._parse_semantic_response(
            response,
            filenames,
        )

        self.assertEqual(
            [subgroup["images"] for subgroup in subgroups],
            [
                ["portrait_1.jpg", "portrait_2.jpg"],
                ["sunset.jpg"],
                ["boat_1.jpg", "boat_2.jpg"],
            ],
        )
        self.assertEqual(
            [subgroup["images"] for subgroup in parsed_subgroups],
            [
                ["portrait_1.jpg", "portrait_2.jpg"],
                ["boat_1.jpg", "boat_2.jpg"],
                ["sunset.jpg"],
            ],
        )

    def test_semantic_contract_accepts_non_contiguous_visual_subgroups(self) -> None:
        filenames = ["wide_1.jpg", "tight.jpg", "wide_2.jpg"]
        response = semantic_response(
            filenames,
            [
                {
                    "images": ["wide_2.jpg", "wide_1.jpg"],
                    "representative": "wide_2.jpg",
                },
                {
                    "images": ["tight.jpg"],
                    "representative": "tight.jpg",
                },
            ],
        )
        reviewer = VisionLLMReviewer(AppSettings())

        subgroups, _, transitions, _ = reviewer._parse_semantic_response(
            response,
            filenames,
        )

        self.assertEqual(
            [subgroup["images"] for subgroup in subgroups],
            [["wide_1.jpg", "wide_2.jpg"], ["tight.jpg"]],
        )
        self.assertEqual(
            [subgroup["representative"] for subgroup in subgroups],
            ["wide_2.jpg", "tight.jpg"],
        )
        self.assertEqual(
            [transition["same_best_frame_decision"] for transition in transitions],
            [False, False],
        )

    def test_transition_must_agree_with_subgroup_boundary(self) -> None:
        filenames = ["tree_1.jpg", "tree_2.jpg", "alley.jpg"]
        response = semantic_response(
            filenames,
            [
                {
                    "images": ["tree_1.jpg", "tree_2.jpg"],
                    "representative": "tree_1.jpg",
                },
                {
                    "images": ["alley.jpg"],
                    "representative": "alley.jpg",
                },
            ],
            transition_overrides={
                ("tree_2.jpg", "alley.jpg"): True,
            },
        )
        reviewer = VisionLLMReviewer(AppSettings())

        with self.assertRaisesRegex(RuntimeError, "contradicts"):
            reviewer._parse_semantic_response(response, filenames)

    def test_same_subject_variations_remain_one_best_frame_decision(self) -> None:
        filenames = [
            "expression.jpg",
            "gesture.jpg",
            "walking_stride.jpg",
            "focus_variant.jpg",
            "exposure_variant.jpg",
            "portrait_variant.jpg",
        ]
        response = json.dumps(
            {
                "subgroups": [
                    {
                        "name": "Same principal-subject sequence",
                        "images": filenames,
                        "representative": "gesture.jpg",
                        "reason": "Expression, pose, timing, focus, exposure, and orientation variations.",
                    }
                ]
            }
        )
        reviewer = VisionLLMReviewer(AppSettings())

        subgroups = reviewer._parse_subgroup_response(response, filenames)

        self.assertEqual(len(subgroups), 1)
        self.assertEqual(subgroups[0]["images"], filenames)
        self.assertEqual(subgroups[0]["representative"], "gesture.jpg")

    def test_significant_subject_change_can_produce_independent_subgroups(self) -> None:
        filenames = ["empty_scene.jpg", "person_sequence.jpg", "vehicle_sequence.jpg"]
        response = json.dumps(
            {
                "subgroups": [
                    {
                        "name": "Environment",
                        "images": ["empty_scene.jpg"],
                        "representative": "empty_scene.jpg",
                    },
                    {
                        "name": "Person interpretation",
                        "images": ["person_sequence.jpg"],
                        "representative": "person_sequence.jpg",
                    },
                    {
                        "name": "Vehicle interpretation",
                        "images": ["vehicle_sequence.jpg"],
                        "representative": "vehicle_sequence.jpg",
                    },
                ]
            }
        )
        reviewer = VisionLLMReviewer(AppSettings())

        subgroups = reviewer._parse_subgroup_response(response, filenames)

        self.assertEqual(len(subgroups), 3)

    def test_invalid_partitions_are_rejected_without_partial_repair(self) -> None:
        filenames = ["a.jpg", "b.jpg"]
        invalid_payloads = {
            "missing": {
                "subgroups": [
                    {
                        "images": ["a.jpg"],
                        "representative": "a.jpg",
                    }
                ]
            },
            "unknown": {
                "subgroups": [
                    {
                        "images": ["a.jpg", "b.jpg", "unknown.jpg"],
                        "representative": "a.jpg",
                    }
                ]
            },
            "duplicate_across_subgroups": {
                "subgroups": [
                    {
                        "images": ["a.jpg"],
                        "representative": "a.jpg",
                    },
                    {
                        "images": ["a.jpg", "b.jpg"],
                        "representative": "b.jpg",
                    },
                ]
            },
            "duplicate_inside_subgroup": {
                "subgroups": [
                    {
                        "images": ["a.jpg", "a.jpg", "b.jpg"],
                        "representative": "a.jpg",
                    }
                ]
            },
            "empty_subgroup": {
                "subgroups": [
                    {
                        "images": [],
                        "representative": "a.jpg",
                    }
                ]
            },
            "missing_representative": {
                "subgroups": [
                    {
                        "images": ["a.jpg", "b.jpg"],
                    }
                ]
            },
            "multiple_representatives": {
                "subgroups": [
                    {
                        "images": ["a.jpg", "b.jpg"],
                        "representative": ["a.jpg", "b.jpg"],
                    }
                ]
            },
            "representative_outside_subgroup": {
                "subgroups": [
                    {
                        "images": ["a.jpg"],
                        "representative": "b.jpg",
                    },
                    {
                        "images": ["b.jpg"],
                        "representative": "b.jpg",
                    },
                ]
            },
            "empty_subgroups": {"subgroups": []},
        }
        reviewer = VisionLLMReviewer(AppSettings())

        for case, payload in invalid_payloads.items():
            with self.subTest(case=case):
                with self.assertRaises(RuntimeError):
                    reviewer._parse_subgroup_response(
                        json.dumps(payload),
                        filenames,
                    )

    def test_singleton_subgroup_must_pick_its_only_member(self) -> None:
        reviewer = VisionLLMReviewer(AppSettings())
        valid = {
            "subgroups": [
                {
                    "images": ["only.jpg"],
                    "representative": "only.jpg",
                }
            ]
        }
        invalid = {
            "subgroups": [
                {
                    "images": ["only.jpg"],
                    "representative": "other.jpg",
                }
            ]
        }

        parsed = reviewer._parse_subgroup_response(
            json.dumps(valid),
            ["only.jpg"],
        )

        self.assertEqual(parsed[0]["representative"], "only.jpg")
        with self.assertRaises(RuntimeError):
            reviewer._parse_subgroup_response(
                json.dumps(invalid),
                ["only.jpg"],
            )

    def test_allow_subgroups_false_requires_one_group_and_one_pick(self) -> None:
        reviewer = VisionLLMReviewer(
            AppSettings(),
            allow_subgroups=False,
        )
        one_group = {
            "subgroups": [
                {
                    "images": ["a.jpg", "b.jpg"],
                    "representative": "b.jpg",
                }
            ]
        }
        multiple_groups = {
            "subgroups": [
                {
                    "images": ["a.jpg"],
                    "representative": "a.jpg",
                },
                {
                    "images": ["b.jpg"],
                    "representative": "b.jpg",
                },
            ]
        }

        parsed = reviewer._parse_subgroup_response(
            json.dumps(one_group),
            ["a.jpg", "b.jpg"],
        )

        self.assertEqual(len(parsed), 1)
        with self.assertRaises(RuntimeError):
            reviewer._parse_subgroup_response(
                json.dumps(multiple_groups),
                ["a.jpg", "b.jpg"],
            )

    def test_invalid_first_response_retries_complete_group_once(self) -> None:
        images = self.images[:2]
        reviewer = StubVisionReviewer(
            [
                "not json",
                semantic_response(
                    [image.filename for image in images],
                    [
                        {
                            "name": "Same sequence",
                            "images": [image.filename for image in images],
                            "representative": images[1].filename,
                        }
                    ],
                ),
            ]
        )
        group = DuplicateGroup(
            group_id="G001",
            images=images,
            capture_span_seconds=1,
            avg_similarity=0.9,
            group_type="Near-duplicate",
        )

        result = reviewer.review_group(group)

        self.assertEqual(reviewer.calls, 2)
        self.assertFalse(result.semantic_fallback)
        self.assertEqual(result.attempts, 2)
        self.assertEqual(result.selected_filenames, [images[1].filename])

    def test_failure_after_retry_creates_all_singleton_picks(self) -> None:
        images = self.images[:2]
        reviewer = StubVisionReviewer(["not json", "still not json"])
        group = DuplicateGroup(
            group_id="G001",
            images=images,
            capture_span_seconds=1,
            avg_similarity=0.9,
            group_type="Near-duplicate",
        )

        with self.assertLogs(
            "app.core.vision_llm_reviewer",
            level="ERROR",
        ) as captured:
            result = reviewer.review_group(group)
        decisions = _all_singleton_decisions(
            group.group_id,
            group.images,
            result.reason,
        )

        self.assertTrue(result.semantic_fallback)
        self.assertEqual(reviewer.calls, 2)
        self.assertEqual(result.selected_filenames, [image.filename for image in images])
        self.assertEqual(
            [decision.representatives for decision in decisions],
            [[image.id] for image in images],
        )
        self.assertTrue(all(not decision.rejected for decision in decisions))
        self.assertIn("semantic_group_fallback=singletons", "\n".join(captured.output))

    def test_context_failure_falls_back_to_contiguous_chunks(self) -> None:
        images = [
            image_record(f"p{index}", f"frame_{index}.jpg")
            for index in range(1, 8)
        ]
        first_chunk = images[:6]
        second_chunk = images[6:]
        reviewer = StubVisionReviewer(
            [
                RuntimeError("request exceeds the available context window"),
                RuntimeError("request exceeds the available context window"),
                semantic_response(
                    [image.filename for image in first_chunk],
                    [
                        {
                            "name": "First chunk",
                            "images": [image.filename for image in first_chunk],
                            "representative": first_chunk[0].filename,
                        }
                    ],
                ),
                semantic_response(
                    [image.filename for image in second_chunk],
                    [
                        {
                            "name": "Second chunk",
                            "images": [image.filename for image in second_chunk],
                            "representative": second_chunk[0].filename,
                        }
                    ],
                ),
            ]
        )
        group = DuplicateGroup(
            group_id="G007",
            images=images,
            capture_span_seconds=10,
            avg_similarity=0.9,
            group_type="Near-duplicate",
        )

        result = reviewer.review_group(group)

        self.assertEqual(reviewer.calls, 4)
        self.assertEqual(result.attempts, 4)
        self.assertFalse(result.semantic_fallback)
        self.assertEqual(
            result.selected_filenames,
            [first_chunk[0].filename, second_chunk[0].filename],
        )

    def test_subgroup_ids_retain_their_broad_group_parent(self) -> None:
        images = [
            image_record("p1", "a.jpg"),
            image_record("p2", "b.jpg"),
        ]
        subgroups = [
            {
                "images": ["a.jpg"],
                "representative": "a.jpg",
            },
            {
                "images": ["b.jpg"],
                "representative": "b.jpg",
            },
        ]

        decisions = _from_vision_subgroups(
            "G005",
            images,
            subgroups,
            "semantic review",
        )

        self.assertEqual(
            [decision.group_id for decision in decisions],
            ["G005_01", "G005_02"],
        )
        self.assertTrue(
            all(decision.group_id.startswith("G005_") for decision in decisions)
        )

    def test_semantic_diagnostics_persist_by_broad_group(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = JobStore(
                ServerConfig(jobs_dir=Path(temporary))
            )
            job_id = "diagnostic-job"
            store.job_dir(job_id).mkdir(parents=True)
            diagnostic = {
                "broad_group_id": "G005",
                "final_group_ids": ["G005_01", "G005_02"],
                "selected_photo_ids": ["p1", "p2"],
            }

            store.save_semantic_diagnostic(
                job_id,
                "G005",
                diagnostic,
            )

            self.assertEqual(
                store.load_semantic_diagnostics(job_id)["G005"],
                diagnostic,
            )


if __name__ == "__main__":
    unittest.main()
