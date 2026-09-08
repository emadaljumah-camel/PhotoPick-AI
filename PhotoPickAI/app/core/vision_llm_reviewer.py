from __future__ import annotations

import base64
import io
import json
import logging
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw, ImageOps

from app.config import AppSettings
from app.core.models import DuplicateGroup, ImageRecord
from app.core.similarity_engine import cosine_similarity, hamming_distance

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class VisionLLMReviewResult:
    """Vision LLM recommendation for a review group."""

    selected_filenames: list[str]
    reason: str
    subgroups: list[dict[str, Any]]
    raw_response: str = ""
    raw_responses: list[str] = field(default_factory=list)
    parsed_subgroups: list[dict[str, Any]] = field(default_factory=list)
    photo_observations: list[dict[str, Any]] = field(default_factory=list)
    adjacent_transitions: list[dict[str, Any]] = field(default_factory=list)
    reconciliation_comparisons: list[dict[str, Any]] = field(default_factory=list)
    winner_comparisons: list[dict[str, Any]] = field(default_factory=list)
    semantic_contract_version: str = "group-partition-v1"
    attempts: int = 1
    semantic_fallback: bool = False
    validation_errors: list[str] = field(default_factory=list)


class SemanticReviewAttemptError(RuntimeError):
    def __init__(self, message: str, raw_response: str = "") -> None:
        super().__init__(message)
        self.raw_response = raw_response


class VisionLLMReviewer:
    """Use a local Vision LLM to recommend keepers."""

    SUBGROUP_RETRY_CHUNK_SIZE = 6

    def __init__(
        self,
        settings: AppSettings,
        allow_subgroups: bool = True,
        retry_model_json_once: bool = True,
    ) -> None:
        self.settings = settings
        self.allow_subgroups = allow_subgroups
        self.retry_model_json_once = retry_model_json_once

    def review_group(self, group: DuplicateGroup) -> VisionLLMReviewResult:
        """Partition one broad group and select its keepers in one Vision call."""

        if not group.images:
            return VisionLLMReviewResult([], "No images in group", [])

        if len(group.images) == 1:
            image = group.images[0]
            subgroup = {
                "name": "Singleton",
                "main_subject": "Single photograph",
                "images": [image.filename],
                "representative": image.filename,
                "reason": "Single photograph kept.",
            }
            return VisionLLMReviewResult(
                selected_filenames=[image.filename],
                reason="Single photograph kept.",
                subgroups=[subgroup],
                parsed_subgroups=[subgroup],
                attempts=0,
            )

        raw_responses: list[str] = []
        validation_errors: list[str] = []
        maximum_attempts = 2 if self.retry_model_json_once else 1

        LOGGER.info(
            "semantic_contract_version=group-partition-v1 broad_group_id=%s "
            "allow_subgroups=%s ordered_input_photo_ids=%s",
            group.group_id,
            self.allow_subgroups,
            [image.id for image in group.images],
        )

        for attempt in range(1, maximum_attempts + 1):
            try:
                (
                    selected_filenames,
                    subgroups,
                    raw_response,
                    parsed_subgroups,
                    photo_observations,
                    adjacent_transitions,
                ) = self._semantic_review_attempt(
                    group.images,
                    context_name=group.group_id,
                )
                raw_responses.append(self._safe_response_content(raw_response))
                LOGGER.info(
                    "semantic_group_valid contract=group-partition-v1 "
                    "broad_group_id=%s attempt=%s subgroups=%s selected=%s",
                    group.group_id,
                    attempt,
                    len(subgroups),
                    selected_filenames,
                )
                return VisionLLMReviewResult(
                    selected_filenames=selected_filenames,
                    reason=(
                        f"Group-level semantic review created {len(subgroups)} "
                        "visual subgroup(s)."
                    ),
                    subgroups=subgroups,
                    raw_response=raw_responses[-1],
                    raw_responses=raw_responses,
                    parsed_subgroups=parsed_subgroups,
                    photo_observations=photo_observations,
                    adjacent_transitions=adjacent_transitions,
                    attempts=attempt,
                    validation_errors=validation_errors,
                )
            except Exception as exc:
                validation_errors.append(str(exc))
                failed_raw = getattr(exc, "raw_response", "")
                if failed_raw:
                    raw_responses.append(self._safe_response_content(failed_raw))
                LOGGER.warning(
                    "semantic_group_validation_failure "
                    "contract=group-partition-v1 broad_group_id=%s "
                    "attempt=%s/%s error=%s",
                    group.group_id,
                    attempt,
                    maximum_attempts,
                    exc,
                )

        last_error = RuntimeError(validation_errors[-1])
        context_failure = any(
            self._is_context_window_failure(RuntimeError(error))
            for error in validation_errors
        )
        if context_failure and len(group.images) > self.SUBGROUP_RETRY_CHUNK_SIZE:
            (
                selected_filenames,
                subgroups,
                chunk_raw,
                chunk_attempts,
            ) = self._review_large_group_in_chunks(group.images, last_error)
            all_raw = [*raw_responses, *chunk_raw]
            LOGGER.warning(
                "semantic_group_chunk_fallback contract=group-partition-v1 "
                "broad_group_id=%s chunks=%s",
                group.group_id,
                len(self._chunks(group.images, self.SUBGROUP_RETRY_CHUNK_SIZE)),
            )
            return VisionLLMReviewResult(
                selected_filenames=selected_filenames,
                reason="Full-group review exceeded context; contiguous chunks were reviewed.",
                subgroups=subgroups,
                raw_response=all_raw[-1] if all_raw else "",
                raw_responses=all_raw,
                parsed_subgroups=subgroups,
                attempts=maximum_attempts + chunk_attempts,
                validation_errors=validation_errors,
            )

        selected_filenames, subgroups, fallback_raw = (
            self._single_image_fallback_subgroups(
                group.images,
                reason="Group-level Vision review failed; every photo was kept.",
            )
        )
        LOGGER.error(
            "semantic_group_fallback=singletons contract=group-partition-v1 "
            "broad_group_id=%s attempts=%s errors=%s",
            group.group_id,
            maximum_attempts,
            validation_errors,
        )
        return VisionLLMReviewResult(
            selected_filenames=selected_filenames,
            reason="Vision review failed; every photo was retained.",
            subgroups=subgroups,
            raw_response=fallback_raw[-1],
            raw_responses=[*raw_responses, *fallback_raw],
            parsed_subgroups=subgroups,
            attempts=maximum_attempts,
            semantic_fallback=True,
            validation_errors=validation_errors,
        )

    def _review_adjacent_pair_with_retry(
        self,
        source: ImageRecord,
        target: ImageRecord,
        context_name: str,
    ) -> tuple[dict[str, Any], list[str], list[str], int]:
        maximum_attempts = 2 if self.retry_model_json_once else 1
        raw_responses: list[str] = []
        validation_errors: list[str] = []

        for attempt in range(1, maximum_attempts + 1):
            LOGGER.info(
                "semantic_pair_attempt contract=pairwise-graph-v2 context=%s "
                "from_photo_id=%s to_photo_id=%s attempt=%s/%s",
                context_name,
                source.id,
                target.id,
                attempt,
                maximum_attempts,
            )
            raw_response = ""
            try:
                raw_response = self._post_json(
                    self._build_payload(
                        self._build_adjacent_pair_prompt(
                            source,
                            target,
                            context_name,
                        ),
                        self._pair_image_inputs(source, target),
                    )
                )
                raw_responses.append(self._safe_response_content(raw_response))
                transition = self._parse_adjacent_pair_response(
                    raw_response,
                    source.filename,
                    target.filename,
                )
                transition.update(
                    {
                        "attempts": attempt,
                        "fallback": False,
                        "validation_errors": list(validation_errors),
                    }
                )
                return transition, raw_responses, validation_errors, attempt
            except Exception as exc:
                if raw_response and (
                    not raw_responses
                    or raw_responses[-1] != self._safe_response_content(raw_response)
                ):
                    raw_responses.append(self._safe_response_content(raw_response))
                validation_errors.append(str(exc))
                LOGGER.warning(
                    "semantic_pair_validation_failure contract=pairwise-graph-v2 "
                    "context=%s attempt=%s/%s error=%s",
                    context_name,
                    attempt,
                    maximum_attempts,
                    exc,
                )

        reason = (
            "Pair comparison failed after all configured attempts; "
            "a fail-open subgroup boundary was created."
        )
        LOGGER.error(
            "semantic_pair_fallback=separate contract=pairwise-graph-v2 context=%s "
            "from_photo_id=%s to_photo_id=%s attempts=%s errors=%s",
            context_name,
            source.id,
            target.id,
            maximum_attempts,
            validation_errors,
        )
        return (
            {
                "from": source.filename,
                "to": target.filename,
                "same_best_frame_decision": False,
                "reason": reason,
                "photo_observations": [],
                "attempts": maximum_attempts,
                "fallback": True,
                "validation_errors": validation_errors,
            },
            raw_responses,
            validation_errors,
            maximum_attempts,
        )

    def _select_representative_pairwise(
        self,
        images: list[ImageRecord],
        context_name: str,
    ) -> tuple[str, list[dict[str, Any]], list[str], list[str], int]:
        champion = images[0]
        comparisons: list[dict[str, Any]] = []
        raw_responses: list[str] = []
        validation_errors: list[str] = []
        total_attempts = 0

        for comparison_index, challenger in enumerate(images[1:], start=1):
            (
                comparison,
                comparison_raw,
                comparison_errors,
                attempts,
            ) = self._review_winner_pair_with_retry(
                champion,
                challenger,
                context_name=f"{context_name} winner comparison {comparison_index}",
            )
            comparisons.append(comparison)
            raw_responses.extend(comparison_raw)
            validation_errors.extend(comparison_errors)
            total_attempts += attempts
            if comparison["winner"] == challenger.filename:
                champion = challenger

        return (
            champion.filename,
            comparisons,
            raw_responses,
            validation_errors,
            total_attempts,
        )

    def _review_winner_pair_with_retry(
        self,
        first: ImageRecord,
        second: ImageRecord,
        context_name: str,
    ) -> tuple[dict[str, Any], list[str], list[str], int]:
        maximum_attempts = 2 if self.retry_model_json_once else 1
        raw_responses: list[str] = []
        validation_errors: list[str] = []

        for attempt in range(1, maximum_attempts + 1):
            raw_response = ""
            try:
                raw_response = self._post_json(
                    self._build_payload(
                        self._build_winner_pair_prompt(
                            first,
                            second,
                            context_name,
                        ),
                        self._pair_image_inputs(first, second),
                    )
                )
                raw_responses.append(self._safe_response_content(raw_response))
                comparison = self._parse_winner_pair_response(
                    raw_response,
                    first.filename,
                    second.filename,
                )
                comparison.update(
                    {
                        "attempts": attempt,
                        "fallback": False,
                        "validation_errors": list(validation_errors),
                    }
                )
                return comparison, raw_responses, validation_errors, attempt
            except Exception as exc:
                if raw_response and (
                    not raw_responses
                    or raw_responses[-1] != self._safe_response_content(raw_response)
                ):
                    raw_responses.append(self._safe_response_content(raw_response))
                validation_errors.append(str(exc))
                LOGGER.warning(
                    "semantic_winner_validation_failure contract=pairwise-graph-v2 "
                    "context=%s attempt=%s/%s error=%s",
                    context_name,
                    attempt,
                    maximum_attempts,
                    exc,
                )

        winner = max(
            (first, second),
            key=lambda image: image.sharpness_score,
        )
        comparison = {
            "candidate_a": first.filename,
            "candidate_b": second.filename,
            "winner": winner.filename,
            "reason": (
                "Winner comparison failed after all configured attempts; "
                "local sharpness selected the fallback winner."
            ),
            "photo_observations": [],
            "attempts": maximum_attempts,
            "fallback": True,
            "validation_errors": validation_errors,
        }
        LOGGER.error(
            "semantic_winner_fallback=local_sharpness contract=pairwise-graph-v2 "
            "context=%s winner=%s attempts=%s errors=%s",
            context_name,
            winner.filename,
            maximum_attempts,
            validation_errors,
        )
        return comparison, raw_responses, validation_errors, maximum_attempts

    def _reconcile_semantic_components(
        self,
        components: list[list[ImageRecord]],
        reviewed_pairs: set[frozenset[str]],
        context_name: str,
    ) -> tuple[
        list[list[ImageRecord]],
        list[dict[str, Any]],
        list[str],
        list[str],
        int,
    ]:
        """Merge separated components when new cross-component evidence agrees."""

        components = [list(component) for component in components]
        comparisons: list[dict[str, Any]] = []
        raw_responses: list[str] = []
        validation_errors: list[str] = []
        total_attempts = 0
        input_order = {
            image.filename: index
            for index, image in enumerate(
                image
                for component in components
                for image in component
            )
        }

        while len(components) > 1:
            merged = False
            for left_index in range(len(components) - 1):
                for right_index in range(left_index + 1, len(components)):
                    candidate = self._strongest_untested_cross_pair(
                        components[left_index],
                        components[right_index],
                        reviewed_pairs,
                    )
                    if candidate is None:
                        continue

                    source, target, local_score = candidate
                    pair_key = frozenset((source.filename, target.filename))
                    reviewed_pairs.add(pair_key)
                    transition, pair_raw, pair_errors, attempts = (
                        self._review_adjacent_pair_with_retry(
                            source,
                            target,
                            context_name=(
                                f"{context_name} reconciliation "
                                f"{left_index + 1}-{right_index + 1}"
                            ),
                        )
                    )
                    comparison = {
                        "left_component": [
                            image.filename for image in components[left_index]
                        ],
                        "right_component": [
                            image.filename for image in components[right_index]
                        ],
                        "candidate_a": source.filename,
                        "candidate_b": target.filename,
                        "local_similarity": local_score,
                        "same_best_frame_decision": transition[
                            "same_best_frame_decision"
                        ],
                        "reason": transition["reason"],
                        "photo_observations": transition.get(
                            "photo_observations",
                            [],
                        ),
                        "attempts": transition.get("attempts", attempts),
                        "fallback": transition.get("fallback", False),
                        "validation_errors": transition.get(
                            "validation_errors",
                            [],
                        ),
                    }
                    comparisons.append(comparison)
                    raw_responses.extend(pair_raw)
                    validation_errors.extend(pair_errors)
                    total_attempts += attempts

                    if transition["same_best_frame_decision"]:
                        combined = (
                            components[left_index] + components[right_index]
                        )
                        combined.sort(
                            key=lambda image: input_order[image.filename]
                        )
                        components[left_index] = combined
                        del components[right_index]
                        merged = True
                        LOGGER.info(
                            "semantic_component_merge contract=pairwise-graph-v2 "
                            "context=%s candidate_a=%s candidate_b=%s "
                            "local_similarity=%.4f",
                            context_name,
                            source.filename,
                            target.filename,
                            local_score,
                        )
                        break
                if merged:
                    break
            if not merged:
                break

        components.sort(
            key=lambda component: min(
                input_order[image.filename] for image in component
            )
        )
        return (
            components,
            comparisons,
            raw_responses,
            validation_errors,
            total_attempts,
        )

    def _strongest_untested_cross_pair(
        self,
        left_component: list[ImageRecord],
        right_component: list[ImageRecord],
        reviewed_pairs: set[frozenset[str]],
    ) -> tuple[ImageRecord, ImageRecord, float] | None:
        candidates: list[tuple[float, ImageRecord, ImageRecord]] = []
        for left in left_component:
            for right in right_component:
                pair_key = frozenset((left.filename, right.filename))
                if pair_key in reviewed_pairs:
                    continue
                candidates.append(
                    (self._local_pair_similarity(left, right), left, right)
                )
        if not candidates:
            return None
        score, left, right = max(candidates, key=lambda item: item[0])
        return left, right, score

    def _local_pair_similarity(
        self,
        left: ImageRecord,
        right: ImageRecord,
    ) -> float:
        scores: list[float] = []
        for left_values, right_values in (
            (left.similarity_vector, right.similarity_vector),
            (left.focus_vector, right.focus_vector),
            (left.edge_vector, right.edge_vector),
        ):
            left_vector = np.asarray(left_values, dtype=np.float32)
            right_vector = np.asarray(right_values, dtype=np.float32)
            if left_vector.size and left_vector.size == right_vector.size:
                scores.append(cosine_similarity(left_vector, right_vector))
        return max(scores, default=0.0)

    def _pair_image_inputs(
        self,
        first: ImageRecord,
        second: ImageRecord,
    ) -> list[tuple[Path, str]]:
        return [
            (
                Path(image.preview_path or image.path),
                f"{index}. {image.filename} | photo_id={image.id}",
            )
            for index, image in enumerate((first, second), start=1)
        ]

    def _parse_pair_observations(
        self,
        payload: dict[str, Any],
        expected_filenames: list[str],
    ) -> list[dict[str, str]]:
        raw_observations = payload.get("photo_observations")
        if not isinstance(raw_observations, list) or len(raw_observations) != 2:
            raise RuntimeError(
                "Vision LLM returned invalid pair JSON: "
                "photo_observations must contain exactly two entries"
            )

        observations: list[dict[str, str]] = []
        for index, (expected_filename, raw_observation) in enumerate(
            zip(expected_filenames, raw_observations),
            start=1,
        ):
            if not isinstance(raw_observation, dict):
                raise RuntimeError(
                    "Vision LLM returned invalid pair JSON: "
                    f"observation {index} must be an object"
                )
            if raw_observation.get("filename") != expected_filename:
                raise RuntimeError(
                    "Vision LLM returned invalid pair JSON: "
                    f"observation {index} must describe {expected_filename}"
                )

            principal_subject = raw_observation.get("principal_subject")
            visible_scene = raw_observation.get("visible_scene")
            if not isinstance(principal_subject, str) or not principal_subject.strip():
                raise RuntimeError(
                    "Vision LLM returned invalid pair JSON: "
                    f"{expected_filename} requires principal_subject"
                )
            if not isinstance(visible_scene, str) or not visible_scene.strip():
                raise RuntimeError(
                    "Vision LLM returned invalid pair JSON: "
                    f"{expected_filename} requires visible_scene"
                )
            observations.append(
                {
                    "filename": expected_filename,
                    "principal_subject": principal_subject.strip(),
                    "visible_scene": visible_scene.strip(),
                }
            )
        return observations

    def _parse_adjacent_pair_response(
        self,
        raw_response: str,
        source_filename: str,
        target_filename: str,
    ) -> dict[str, Any]:
        try:
            payload = json.loads(self._extract_json_text(raw_response))
        except json.JSONDecodeError as exc:
            raise RuntimeError("Vision LLM returned invalid pair JSON") from exc
        if not isinstance(payload, dict):
            raise RuntimeError(
                "Vision LLM returned invalid pair JSON: top-level value must be an object"
            )

        observations = self._parse_pair_observations(
            payload,
            [source_filename, target_filename],
        )
        same_decision = payload.get("same_best_frame_decision")
        if type(same_decision) is not bool:
            raise RuntimeError(
                "Vision LLM returned invalid pair JSON: "
                "same_best_frame_decision must be a boolean"
            )
        reason = payload.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise RuntimeError(
                "Vision LLM returned invalid pair JSON: reason is required"
            )
        return {
            "from": source_filename,
            "to": target_filename,
            "same_best_frame_decision": same_decision,
            "reason": reason.strip(),
            "photo_observations": observations,
        }

    def _parse_winner_pair_response(
        self,
        raw_response: str,
        first_filename: str,
        second_filename: str,
    ) -> dict[str, Any]:
        try:
            payload = json.loads(self._extract_json_text(raw_response))
        except json.JSONDecodeError as exc:
            raise RuntimeError("Vision LLM returned invalid winner JSON") from exc
        if not isinstance(payload, dict):
            raise RuntimeError(
                "Vision LLM returned invalid winner JSON: top-level value must be an object"
            )

        observations = self._parse_pair_observations(
            payload,
            [first_filename, second_filename],
        )
        winner = payload.get("winner")
        if winner not in {first_filename, second_filename}:
            raise RuntimeError(
                "Vision LLM returned invalid winner JSON: "
                "winner must be one of the compared photographs"
            )
        reason = payload.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise RuntimeError(
                "Vision LLM returned invalid winner JSON: reason is required"
            )
        return {
            "candidate_a": first_filename,
            "candidate_b": second_filename,
            "winner": winner,
            "reason": reason.strip(),
            "photo_observations": observations,
        }

    def _review_image_set(
        self,
        images: list[ImageRecord],
        context_name: str,
    ) -> tuple[list[str], list[dict[str, Any]], list[str]]:
        """Request and validate one semantic partition-and-pick response."""

        selected_filenames, subgroups, raw_response, _, _, _ = self._semantic_review_attempt(
            images,
            context_name,
        )
        return selected_filenames, subgroups, [raw_response]

    def _semantic_review_attempt(
        self,
        images: list[ImageRecord],
        context_name: str,
    ) -> tuple[
        list[str],
        list[dict[str, Any]],
        str,
        list[dict[str, Any]],
        list[dict[str, Any]],
        list[dict[str, Any]],
    ]:
        filenames = [image.filename for image in images]
        image_inputs = [
            (
                Path(image.preview_path or image.path),
                f"{index}. {image.filename} | photo_id={image.id}",
            )
            for index, image in enumerate(images, start=1)
        ]
        prompt = self._build_semantic_review_prompt(images, context_name)
        raw_response = self._post_json(self._build_payload(prompt, image_inputs))
        try:
            (
                subgroups,
                photo_observations,
                adjacent_transitions,
                parsed_subgroups,
            ) = self._parse_semantic_response(raw_response, filenames)
        except Exception as exc:
            raise SemanticReviewAttemptError(str(exc), raw_response) from exc
        selected_filenames = [subgroup["representative"] for subgroup in subgroups]
        return (
            selected_filenames,
            subgroups,
            raw_response,
            parsed_subgroups,
            photo_observations,
            adjacent_transitions,
        )

    def _parse_semantic_response(
        self,
        raw_response: str,
        valid_filenames: list[str],
    ) -> tuple[
        list[dict[str, Any]],
        list[dict[str, Any]],
        list[dict[str, Any]],
        list[dict[str, Any]],
    ]:
        """Validate visual evidence, adjacent decisions, and their partition."""

        subgroups = self._parse_subgroup_response(raw_response, valid_filenames)
        payload = json.loads(self._extract_json_text(raw_response))
        valid_set = set(valid_filenames)

        raw_observations = payload.get("photo_observations")
        if not isinstance(raw_observations, list):
            raise RuntimeError(
                "Vision LLM returned invalid semantic JSON: "
                "photo_observations must be an array"
            )
        if len(raw_observations) != len(valid_filenames):
            raise RuntimeError(
                "Vision LLM returned invalid semantic JSON: "
                "photo_observations must contain exactly one entry per photo"
            )

        observations: list[dict[str, Any]] = []
        observed_filenames: set[str] = set()
        for index, raw_observation in enumerate(raw_observations, start=1):
            if not isinstance(raw_observation, dict):
                raise RuntimeError(
                    "Vision LLM returned invalid semantic JSON: "
                    f"photo observation {index} must be an object"
                )
            filename = raw_observation.get("filename")
            if not isinstance(filename, str) or filename not in valid_set:
                raise RuntimeError(
                    "Vision LLM returned invalid semantic JSON: "
                    f"photo observation {index} has an unknown filename"
                )
            if filename in observed_filenames:
                raise RuntimeError(
                    "Vision LLM returned invalid semantic JSON: "
                    f"photo observation repeats {filename}"
                )

            principal_subject = raw_observation.get("principal_subject")
            visible_scene = raw_observation.get("visible_scene")
            if not isinstance(principal_subject, str) or not principal_subject.strip():
                raise RuntimeError(
                    "Vision LLM returned invalid semantic JSON: "
                    f"photo observation for {filename} requires principal_subject"
                )
            if not isinstance(visible_scene, str) or not visible_scene.strip():
                raise RuntimeError(
                    "Vision LLM returned invalid semantic JSON: "
                    f"photo observation for {filename} requires visible_scene"
                )

            observed_filenames.add(filename)
            observations.append(
                {
                    "filename": filename,
                    "principal_subject": principal_subject.strip(),
                    "visible_scene": visible_scene.strip(),
                }
            )

        missing_observations = valid_set - observed_filenames
        if missing_observations:
            raise RuntimeError(
                "Vision LLM returned invalid semantic JSON: "
                f"photo observations omitted {sorted(missing_observations)}"
            )

        expected_pairs = list(zip(valid_filenames, valid_filenames[1:]))
        raw_transitions = payload.get("adjacent_transitions")
        if not isinstance(raw_transitions, list):
            raise RuntimeError(
                "Vision LLM returned invalid semantic JSON: "
                "adjacent_transitions must be an array"
            )
        if len(raw_transitions) != len(expected_pairs):
            raise RuntimeError(
                "Vision LLM returned invalid semantic JSON: "
                "adjacent_transitions must contain exactly one entry "
                "for every adjacent input pair"
            )

        transitions: list[dict[str, Any]] = []
        for index, ((expected_from, expected_to), raw_transition) in enumerate(
            zip(expected_pairs, raw_transitions),
            start=1,
        ):
            if not isinstance(raw_transition, dict):
                raise RuntimeError(
                    "Vision LLM returned invalid semantic JSON: "
                    f"adjacent transition {index} must be an object"
                )

            source = raw_transition.get("from")
            target = raw_transition.get("to")
            if source != expected_from or target != expected_to:
                raise RuntimeError(
                    "Vision LLM returned invalid semantic JSON: "
                    f"adjacent transition {index} must compare "
                    f"{expected_from} to {expected_to}"
                )

            same_decision = raw_transition.get("same_best_frame_decision")
            if type(same_decision) is not bool:
                raise RuntimeError(
                    "Vision LLM returned invalid semantic JSON: "
                    f"adjacent transition {expected_from} -> {expected_to} "
                    "requires a boolean same_best_frame_decision"
                )

            reason = raw_transition.get("reason")
            if not isinstance(reason, str) or not reason.strip():
                raise RuntimeError(
                    "Vision LLM returned invalid semantic JSON: "
                    f"adjacent transition {expected_from} -> {expected_to} "
                    "requires a reason"
                )

            transitions.append(
                {
                    "from": source,
                    "to": target,
                    "same_best_frame_decision": same_decision,
                    "reason": reason.strip(),
                }
            )

        subgroup_by_filename: dict[str, int] = {}
        input_position = {
            filename: index for index, filename in enumerate(valid_filenames)
        }
        for subgroup in subgroups:
            subgroup["images"].sort(key=input_position.__getitem__)

        # Subgroup and member order have no semantic meaning. Normalize the
        # complete partition to capture order for deterministic downstream IDs.
        subgroups.sort(
            key=lambda subgroup: min(
                input_position[filename] for filename in subgroup["images"]
            )
        )
        for subgroup_index, subgroup in enumerate(subgroups):
            for filename in subgroup["images"]:
                subgroup_by_filename[filename] = subgroup_index

        for transition in transitions:
            same_subgroup = (
                subgroup_by_filename[transition["from"]]
                == subgroup_by_filename[transition["to"]]
            )
            if transition["same_best_frame_decision"] != same_subgroup:
                raise RuntimeError(
                    "Vision LLM returned invalid semantic JSON: transition "
                    f"{transition['from']} -> {transition['to']} contradicts "
                    "the returned subgroup partition"
                )

        return (
            subgroups,
            observations,
            transitions,
            list(payload.get("subgroups", [])),
        )

    def _safe_response_content(self, raw_response: str) -> str:
        without_hidden_reasoning = re.sub(
            r"<(?:think|analysis)>.*?</(?:think|analysis)>",
            "",
            raw_response,
            flags=re.IGNORECASE | re.DOTALL,
        ).strip()
        return without_hidden_reasoning[:20_000]

    def _review_large_group_in_chunks(
        self,
        images: list[ImageRecord],
        original_error: RuntimeError,
    ) -> tuple[list[str], list[dict[str, Any]], list[str], int]:
        """Retry failed large-group subgrouping in smaller Vision requests."""

        selected_filenames: list[str] = []
        final_subgroups: list[dict[str, Any]] = []
        raw_responses = [f"Full group subgrouping failed before chunk retry: {original_error}"]
        total_attempts = 0

        for chunk_index, chunk in enumerate(self._chunks(images, self.SUBGROUP_RETRY_CHUNK_SIZE), start=1):
            chunk_selected, chunk_subgroups, chunk_raw, chunk_attempts = self._review_chunk_or_split(
                chunk,
                label=f"Chunk {chunk_index}",
            )
            total_attempts += chunk_attempts
            for filename in chunk_selected:
                if filename not in selected_filenames:
                    selected_filenames.append(filename)
            final_subgroups.extend(chunk_subgroups)
            raw_responses.extend(chunk_raw)

        return selected_filenames, final_subgroups, raw_responses, total_attempts

    def _review_chunk_or_split(
        self,
        images: list[ImageRecord],
        label: str,
    ) -> tuple[list[str], list[dict[str, Any]], list[str], int]:
        """Review a chunk, splitting again if the local model context is still too small."""

        try:
            selected_filenames, subgroups, raw_responses = self._review_image_set(images, context_name=label)
            for subgroup in subgroups:
                subgroup["name"] = f"{label}: {subgroup.get('name', 'Subgroup')}"
            return selected_filenames, subgroups, raw_responses, 1
        except RuntimeError as exc:
            if self._is_context_window_failure(exc) and len(images) > 1:
                midpoint = max(1, len(images) // 2)
                selected_filenames: list[str] = []
                subgroups: list[dict[str, Any]] = []
                raw_responses = [f"{label} exceeded model context and was split into smaller Vision requests: {exc}"]
                total_attempts = 1
                for split_index, split in enumerate((images[:midpoint], images[midpoint:]), start=1):
                    split_selected, split_subgroups, split_raw, split_attempts = self._review_chunk_or_split(
                        split,
                        label=f"{label}.{split_index}",
                    )
                    total_attempts += split_attempts
                    for filename in split_selected:
                        if filename not in selected_filenames:
                            selected_filenames.append(filename)
                    subgroups.extend(split_subgroups)
                    raw_responses.extend(split_raw)
                return selected_filenames, subgroups, raw_responses, total_attempts

            if self._is_retryable_subgroup_failure(exc):
                selected_filenames, subgroups, raw_responses = self._single_image_fallback_subgroups(
                    images,
                    reason=f"{label} Vision subgrouping failed, so each photo was kept separately: {exc}",
                )
                for subgroup in subgroups:
                    subgroup["name"] = f"{label}: {subgroup.get('name', 'Fallback distinct photo')}"
                return selected_filenames, subgroups, raw_responses, 1

            raise

    def _single_image_fallback_subgroups(
        self,
        images: list[ImageRecord],
        reason: str,
    ) -> tuple[list[str], list[dict[str, Any]], list[str]]:
        """Safest fallback: keep every photo when subgrouping cannot be trusted."""

        selected_filenames = [image.filename for image in images]
        subgroups = [
            {
                "name": "Fallback distinct photo",
                "images": [image.filename],
                "representative": image.filename,
                "reason": reason,
            }
            for image in images
        ]
        return selected_filenames, subgroups, [reason]

    def _chunks(self, images: list[ImageRecord], size: int) -> list[list[ImageRecord]]:
        return [images[index : index + size] for index in range(0, len(images), size)]

    def _is_subgroup_json_failure(self, exc: RuntimeError) -> bool:
        message = str(exc).lower()
        return "invalid subgroup json" in message or "invalid semantic json" in message

    def _is_context_window_failure(self, exc: RuntimeError) -> bool:
        message = str(exc).lower()
        return "context size" in message or "context window" in message or "exceeds the available context" in message

    def _is_retryable_subgroup_failure(self, exc: RuntimeError) -> bool:
        return self._is_subgroup_json_failure(exc) or self._is_context_window_failure(exc)

    def _build_payload(self, prompt: str, image_inputs: list[tuple[Path, str]]) -> dict[str, Any]:
        if self._uses_openai_chat_api():
            content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
            for image_path, label in image_inputs:
                content.append(
                    {
                        "type": "text",
                        "text": f"The next image is {label}.",
                    }
                )
                content.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": self._encode_labeled_image_data_url(image_path, label)},
                    }
                )
            return {
                "model": self.settings.vision_llm_model,
                "messages": [{"role": "user", "content": content}],
                "temperature": 0,
            }

        images = [
            self._encode_labeled_image_base64(image_path, label)
            for image_path, label in image_inputs
        ]
        payload = {
            "model": self.settings.vision_llm_model,
            "messages": [
                {
                    "role": "user",
                    "content": prompt,
                    "images": images,
                }
            ],
            "stream": False,
            "format": "json",
            "options": {"temperature": 0},
        }
        return payload

    def _post_json(self, payload: dict[str, Any]) -> str:
        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.settings.vision_llm_endpoint,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.settings.vision_llm_timeout_seconds) as response:
                response_payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Vision LLM request failed: HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Vision LLM request failed: {exc}") from exc

        content = self._extract_response_content(response_payload)
        if not content:
            raise RuntimeError("Vision LLM returned an empty response")
        return str(content)

    def _extract_response_content(self, response_payload: dict[str, Any]) -> str:
        if "choices" in response_payload:
            choices = response_payload.get("choices", [])
            if choices:
                message = choices[0].get("message", {})
                return str(message.get("content", ""))
        message = response_payload.get("message", {})
        return str(message.get("content", ""))

    def _parse_response(self, raw_response: str, valid_filenames: list[str]) -> VisionLLMReviewResult:
        cleaned_response = self._extract_json_text(raw_response)
        try:
            payload = json.loads(cleaned_response)
        except json.JSONDecodeError as exc:
            preview = raw_response.strip().replace("\n", " ")
            if len(preview) > 300:
                preview = f"{preview[:300]}..."
            raise RuntimeError(f"Vision LLM returned invalid JSON. Response preview: {preview}") from exc

        reason = str(payload.get("reason", "Vision LLM recommendation applied."))
        subgroups = self._normalize_subgroups(payload.get("subgroups", []), valid_filenames, reason)
        selected = [subgroup["representative"] for subgroup in subgroups]
        return VisionLLMReviewResult(
            selected_filenames=selected,
            reason=reason,
            subgroups=subgroups,
            raw_response=raw_response,
        )

    def _parse_subgroup_response(self, raw_response: str, valid_filenames: list[str]) -> list[dict[str, Any]]:
        cleaned_response = self._extract_json_text(raw_response)
        try:
            payload = json.loads(cleaned_response)
        except json.JSONDecodeError as exc:
            preview = raw_response.strip().replace("\n", " ")
            if len(preview) > 300:
                preview = f"{preview[:300]}..."
            raise RuntimeError(f"Vision LLM returned invalid subgroup JSON. Response preview: {preview}") from exc

        if not isinstance(payload, dict):
            raise RuntimeError("Vision LLM returned invalid subgroup JSON: top-level value must be an object")

        raw_subgroups = payload.get("subgroups", [])
        if not isinstance(raw_subgroups, list) or not raw_subgroups:
            raise RuntimeError("Vision LLM returned invalid subgroup JSON: subgroups must be a non-empty array")

        subgroups: list[dict[str, Any]] = []
        assigned: set[str] = set()
        valid_set = set(valid_filenames)
        for index, raw_subgroup in enumerate(raw_subgroups, start=1):
            if not isinstance(raw_subgroup, dict):
                raise RuntimeError(
                    f"Vision LLM returned invalid subgroup JSON: subgroup {index} must be an object"
                )
            images = raw_subgroup.get("images", [])
            if not isinstance(images, list) or not images:
                raise RuntimeError(
                    f"Vision LLM returned invalid subgroup JSON: subgroup {index} must contain a non-empty images array"
                )
            if any(not isinstance(filename, str) for filename in images):
                raise RuntimeError(
                    f"Vision LLM returned invalid subgroup JSON: subgroup {index} contains a non-string photo identifier"
                )
            image_set = set(images)
            if len(image_set) != len(images):
                raise RuntimeError(
                    f"Vision LLM returned invalid subgroup JSON: subgroup {index} repeats a photo"
                )
            unknown = image_set - valid_set
            if unknown:
                raise RuntimeError(
                    f"Vision LLM returned invalid subgroup JSON: subgroup {index} contains unknown photos {sorted(unknown)}"
                )
            repeated = assigned & image_set
            if repeated:
                raise RuntimeError(
                    f"Vision LLM returned invalid subgroup JSON: photos appear in multiple subgroups {sorted(repeated)}"
                )

            representative = raw_subgroup.get("representative")
            if not isinstance(representative, str):
                raise RuntimeError(
                    f"Vision LLM returned invalid subgroup JSON: subgroup {index} must contain exactly one representative"
                )
            if representative not in image_set:
                raise RuntimeError(
                    f"Vision LLM returned invalid subgroup JSON: subgroup {index} representative is outside its images"
                )

            assigned.update(image_set)
            subgroups.append(
                {
                    "name": str(raw_subgroup.get("name", f"Subgroup {index}")),
                    "main_subject": str(raw_subgroup.get("main_subject", raw_subgroup.get("subject", ""))),
                    "images": list(images),
                    "representative": representative,
                    "reason": str(raw_subgroup.get("reason", "")),
                }
            )

        missing = valid_set - assigned
        if missing:
            raise RuntimeError(
                f"Vision LLM returned invalid subgroup JSON: response omitted photos {sorted(missing)}"
            )
        if not self.allow_subgroups and len(subgroups) != 1:
            raise RuntimeError(
                "Vision LLM returned invalid subgroup JSON: subgrouping is disabled but multiple subgroups were returned"
            )
        return subgroups

    def _parse_selection_response(self, raw_response: str, valid_filenames: list[str]) -> tuple[str, str]:
        cleaned_response = self._extract_json_text(raw_response)
        try:
            payload = json.loads(cleaned_response)
        except json.JSONDecodeError as exc:
            preview = raw_response.strip().replace("\n", " ")
            if len(preview) > 300:
                preview = f"{preview[:300]}..."
            raise RuntimeError(f"Vision LLM returned invalid selection JSON. Response preview: {preview}") from exc

        representative = payload.get("representative")
        if representative not in valid_filenames:
            representative = valid_filenames[0]
            reason = "Fallback selection because the Vision LLM returned an invalid representative filename."
        else:
            reason = str(payload.get("reason", "Selected by Vision LLM."))
        return str(representative), reason

    def _refine_pose_sensitive_subgroups(
        self,
        subgroups: list[dict[str, Any]],
        images: list[ImageRecord],
        valid_filenames: list[str],
        context_name: str,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Ask a focused follow-up when animal/wildlife photos are collapsed by broad subject."""

        if len(subgroups) != 1 or len(images) < 3:
            return subgroups, []

        subgroup = subgroups[0]
        subgroup_filenames = subgroup.get("images", [])
        if set(subgroup_filenames) != set(valid_filenames):
            return subgroups, []
        if not self._is_pose_sensitive_animal_group(subgroup):
            return subgroups, []

        prompt = self._build_pose_refinement_prompt(images, context_name, subgroup)
        image_inputs = [
            (Path(image.preview_path or image.path), f"{index}. {image.filename}")
            for index, image in enumerate(images, start=1)
        ]
        try:
            raw_response = self._post_json(self._build_payload(prompt, image_inputs))
            refined_subgroups = self._parse_subgroup_response(raw_response, valid_filenames)
        except Exception as exc:
            LOGGER.info("Vision pose refinement failed for %s: %s", context_name, exc)
            return subgroups, [f"Pose refinement failed: {exc}"]

        if len(refined_subgroups) <= 1:
            return subgroups, [raw_response]

        LOGGER.info("Vision pose refinement split %s into %s subgroup(s)", context_name, len(refined_subgroups))
        return refined_subgroups, [raw_response]

    def _is_pose_sensitive_animal_group(self, subgroup: dict[str, Any]) -> bool:
        text = " ".join(
            str(subgroup.get(key, ""))
            for key in ("name", "main_subject", "reason")
        ).lower()
        animal_terms = (
            "animal",
            "wildlife",
            "bird",
            "birds",
            "chameleon",
            "reptile",
            "lizard",
            "monkey",
            "lemur",
            "mammal",
            "butterfly",
            "insect",
            "frog",
        )
        return any(term in text for term in animal_terms)

    def _extract_json_text(self, raw_response: str) -> str:
        """Extract JSON from plain text or Markdown fenced responses."""

        text = raw_response.strip()
        if text.startswith("```"):
            lines = text.splitlines()
            if lines and lines[0].strip().startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip().startswith("```"):
                lines = lines[:-1]
            text = "\n".join(lines).strip()

        first_brace = text.find("{")
        if first_brace != -1:
            extracted = self._extract_balanced_json_object(text, first_brace)
            if extracted:
                return extracted
        return text

    def _extract_balanced_json_object(self, text: str, start_index: int) -> str:
        """Return the first complete JSON object from text."""

        depth = 0
        in_string = False
        escaped = False
        for index in range(start_index, len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue

            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return text[start_index : index + 1]
        return ""

    def _validate_subgroups_with_local_similarity(
        self,
        result: VisionLLMReviewResult,
        group: DuplicateGroup,
    ) -> VisionLLMReviewResult:
        """Split broad LLM buckets that are not locally near-duplicate enough.

        The Vision LLM is good at describing scenes but can over-group by theme
        ("market", "cityscape", "building") instead of true near-duplicate content.
        This guard keeps the LLM's representatives only when the photos inside a
        subgroup are connected by local visual similarity.
        """

        images_by_filename = {image.filename: image for image in group.images}
        validated_subgroups: list[dict[str, Any]] = []
        changed = False

        for subgroup in result.subgroups:
            subgroup_images = [
                images_by_filename[filename]
                for filename in subgroup.get("images", [])
                if filename in images_by_filename
            ]
            if len(subgroup_images) <= 1:
                validated_subgroups.append(subgroup)
                continue

            components = self._local_similarity_components(subgroup_images)
            if len(components) == 1:
                validated_subgroups.append(subgroup)
                continue

            changed = True
            for component_index, component in enumerate(components, start=1):
                representative = self._best_component_representative(component, subgroup.get("representative", ""))
                component_filenames = [image.filename for image in component]
                validated_subgroups.append(
                    {
                        "name": f"{subgroup.get('name', 'Subgroup')} - local set {component_index}",
                        "images": component_filenames,
                        "representative": representative,
                        "reason": "Split from a broad Vision LLM subgroup because local visual similarity did not support keeping these photos together.",
                    }
                )

        if not changed:
            return result

        selected = [subgroup["representative"] for subgroup in validated_subgroups]
        reason = (
            f"{result.reason} Local similarity validation split one or more broad Vision LLM subgroups "
            "to avoid rejecting visually different photos."
        )
        return VisionLLMReviewResult(
            selected_filenames=selected,
            reason=reason,
            subgroups=validated_subgroups,
            raw_response=result.raw_response,
        )

    def _local_similarity_components(self, images: list[ImageRecord]) -> list[list[ImageRecord]]:
        adjacency: dict[str, set[str]] = {image.filename: set() for image in images}

        for left_index, left in enumerate(images):
            for right in images[left_index + 1 :]:
                if self._locally_near_duplicate(left, right):
                    adjacency[left.filename].add(right.filename)
                    adjacency[right.filename].add(left.filename)

        components: list[list[ImageRecord]] = []
        visited: set[str] = set()
        images_by_filename = {image.filename: image for image in images}
        for image in images:
            if image.filename in visited:
                continue
            stack = [image.filename]
            component_names: list[str] = []
            while stack:
                current = stack.pop()
                if current in visited:
                    continue
                visited.add(current)
                component_names.append(current)
                stack.extend(adjacency[current] - visited)
            components.append([images_by_filename[filename] for filename in component_names])
        return components

    def _locally_near_duplicate(self, left: ImageRecord, right: ImageRecord) -> bool:
        if not left.similarity_vector or not right.similarity_vector:
            return False

        global_similarity = cosine_similarity(
            np.asarray(left.similarity_vector, dtype=np.float32),
            np.asarray(right.similarity_vector, dtype=np.float32),
        )
        focus_similarity = 0.0
        if left.focus_vector and right.focus_vector:
            focus_similarity = cosine_similarity(
                np.asarray(left.focus_vector, dtype=np.float32),
                np.asarray(right.focus_vector, dtype=np.float32),
            )
        edge_similarity = 0.0
        if left.edge_vector and right.edge_vector:
            edge_similarity = cosine_similarity(
                np.asarray(left.edge_vector, dtype=np.float32),
                np.asarray(right.edge_vector, dtype=np.float32),
            )
        hash_distance = hamming_distance(left.phash, right.phash) if left.phash and right.phash else 64

        combined = (global_similarity * 0.45) + (focus_similarity * 0.35) + (edge_similarity * 0.20)
        return (
            combined >= 0.88
            or (global_similarity >= 0.92 and hash_distance <= 16)
            or (focus_similarity >= 0.93 and hash_distance <= 20)
        )

    def _best_component_representative(self, images: list[ImageRecord], llm_representative: str) -> str:
        filenames = {image.filename for image in images}
        if llm_representative in filenames:
            return llm_representative
        ranked = sorted(images, key=lambda image: (-image.sharpness_score, -image.file_size, image.capture_datetime))
        return ranked[0].filename

    def _normalize_subgroups(
        self,
        raw_subgroups: Any,
        valid_filenames: list[str],
        overall_reason: str,
    ) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        seen_images: set[str] = set()
        if not isinstance(raw_subgroups, list):
            raw_subgroups = []

        for index, raw_subgroup in enumerate(raw_subgroups, start=1):
            if not isinstance(raw_subgroup, dict):
                continue
            images = raw_subgroup.get("images", [])
            if isinstance(images, str):
                images = [images]
            images = [filename for filename in images if filename in valid_filenames and filename not in seen_images]
            representative = raw_subgroup.get("representative")
            if representative not in images:
                representative = images[0] if images else None
            if representative is None:
                continue
            seen_images.update(images)
            name = str(raw_subgroup.get("name", f"Subgroup {index}"))
            subgroup_reason = str(raw_subgroup.get("reason", ""))
            if self._subgroup_is_broad_scene_bucket(name, subgroup_reason, images):
                for image_filename in images:
                    normalized.append(
                        {
                            "name": f"{name} - distinct view",
                            "images": [image_filename],
                            "representative": image_filename,
                            "reason": "Kept separately because this subgroup label appears to describe a broad scene/category rather than true near-duplicates.",
                        }
                    )
                continue
            normalized.append(
                {
                    "name": name,
                    "images": images,
                    "representative": representative,
                    "reason": subgroup_reason,
                }
            )

        if self._reason_implies_separate_groups(overall_reason, normalized, valid_filenames):
            return [
                {
                    "name": f"Distinct photo {index}",
                    "images": [filename],
                    "representative": filename,
                    "reason": "Kept separately because the Vision LLM described meaningful visual differences.",
                }
                for index, filename in enumerate(valid_filenames, start=1)
            ]

        if self._reason_implies_theme_grouping(overall_reason, normalized, valid_filenames):
            return [
                {
                    "name": f"Distinct scene {index}",
                    "images": [filename],
                    "representative": filename,
                    "reason": "Kept separately because the Vision LLM grouped by broad theme/location instead of true near-duplicate similarity.",
                }
                for index, filename in enumerate(valid_filenames, start=1)
            ]

        if self._reason_implies_contradictory_scene_content(overall_reason, normalized, valid_filenames):
            return [
                {
                    "name": f"Distinct content {index}",
                    "images": [filename],
                    "representative": filename,
                    "reason": "Kept separately because the Vision LLM described meaningfully different scene content inside one subgroup.",
                }
                for index, filename in enumerate(valid_filenames, start=1)
            ]

        if self._reason_implies_single_representative(overall_reason, normalized, valid_filenames):
            representative = normalized[0]["representative"] if normalized else valid_filenames[0]
            return [
                {
                    "name": "Near-duplicate set",
                    "images": valid_filenames,
                    "representative": representative,
                    "reason": "Kept one representative because the Vision LLM described the photos as redundant near-duplicates.",
                }
            ]

        if not normalized:
            normalized.append(
                {
                    "name": "Subgroup 1",
                    "images": valid_filenames,
                    "representative": valid_filenames[0],
                    "reason": "Fallback selection because no valid subgroups were returned.",
                }
            )
        return normalized

    def _reason_implies_separate_groups(
        self,
        reason: str,
        subgroups: list[dict[str, Any]],
        valid_filenames: list[str],
    ) -> bool:
        """Recover when the model's prose contradicts its subgroup structure."""

        if len(valid_filenames) < 2 or len(subgroups) != 1:
            return False
        subgroup_images = set(subgroups[0].get("images", []))
        if subgroup_images != set(valid_filenames):
            return False

        lowered = reason.lower()
        distinct_terms = ("distinct", "different", "separate", "unique", "whereas", "while the", "however")
        content_terms = ("person", "human", "people", "subject", "animal", "vehicle", "sign", "action")
        return any(term in lowered for term in distinct_terms) and any(term in lowered for term in content_terms)

    def _reason_implies_theme_grouping(
        self,
        reason: str,
        subgroups: list[dict[str, Any]],
        valid_filenames: list[str],
    ) -> bool:
        """Recover when the model groups different photos only by broad theme/location."""

        if len(valid_filenames) < 4 or len(subgroups) != 1:
            return False
        subgroup_images = set(subgroups[0].get("images", []))
        if subgroup_images != set(valid_filenames):
            return False

        text_parts = [reason, str(subgroups[0].get("reason", ""))]
        lowered = " ".join(text_parts).lower()
        broad_theme_terms = (
            "similar theme",
            "same theme",
            "city panoramas",
            "cityscape",
            "urban landscape",
            "landmark",
            "landmarks",
            "architecture",
            "buildings",
            "same location",
            "same area",
            "same place",
        )
        true_duplicate_terms = (
            "near-duplicate",
            "near duplicate",
            "redundant",
            "same composition",
            "same framing",
            "same subject",
            "no significant difference",
            "no significant differences",
        )
        return any(term in lowered for term in broad_theme_terms) and not any(term in lowered for term in true_duplicate_terms)

    def _reason_implies_contradictory_scene_content(
        self,
        reason: str,
        subgroups: list[dict[str, Any]],
        valid_filenames: list[str],
    ) -> bool:
        """Recover when the model says one subgroup contains different subjects."""

        if len(valid_filenames) < 2 or len(subgroups) != 1:
            return False
        subgroup_images = set(subgroups[0].get("images", []))
        if subgroup_images != set(valid_filenames):
            return False

        text_parts = [reason, str(subgroups[0].get("reason", "")), str(subgroups[0].get("name", ""))]
        lowered = " ".join(text_parts).lower()
        contrast_terms = ("while", "whereas", "but", "different", "focuses on", "broader view")
        subject_pairs = (
            ("river", "bridge"),
            ("river", "structure"),
            ("landscape", "bridge"),
            ("cityscape", "person"),
            ("panorama", "person"),
            ("field", "fruit"),
            ("field", "display"),
            ("building", "street"),
            ("facade", "street"),
            ("animal", "facing"),
            ("chameleon", "facing"),
            ("animal", "pose"),
            ("chameleon", "pose"),
            ("animal", "orientation"),
            ("chameleon", "orientation"),
            ("woman", "stream"),
            ("person", "stream"),
            ("woman", "forest stream"),
            ("person", "forest stream"),
            ("woman", "bridge"),
            ("person", "bridge"),
            ("woman", "path"),
            ("person", "path"),
            ("bird", "different bird"),
            ("birds", "different birds"),
            ("bird", "different species"),
            ("birds", "different species"),
            ("bird", "different color"),
            ("birds", "different colors"),
            ("bird", "markings"),
            ("birds", "markings"),
        )
        has_contrast = any(term in lowered for term in contrast_terms)
        has_subject_pair = any(left in lowered and right in lowered for left, right in subject_pairs)
        return has_contrast and has_subject_pair

    def _subgroup_is_broad_scene_bucket(self, name: str, reason: str, images: list[str]) -> bool:
        """Detect broad LLM buckets like 'Building Facade' that should not collapse photos."""

        if len(images) < 2:
            return False
        lowered = f"{name} {reason}".lower()
        broad_bucket_terms = (
            "building facade",
            "facade",
            "architecture",
            "architectural",
            "building",
            "buildings",
            "landmark",
            "landmarks",
            "structure",
            "street scene",
            "garden view",
            "city panorama",
            "city panoramas",
            "cityscape",
            "urban landscape",
            "person in cityscape",
            "people in cityscape",
            "person in landscape",
            "people in landscape",
            "birds in trees",
            "birds on branches",
            "bird in tree",
            "bird on branch",
            "view",
            "views",
        )
        duplicate_terms = (
            "near-duplicate",
            "near duplicate",
            "redundant",
            "same composition",
            "same framing",
            "same exact",
            "same subject",
            "same building",
            "same facade",
            "minor variation",
            "minor variations",
            "slight variation",
            "slight variations",
        )
        return any(term in lowered for term in broad_bucket_terms) and not any(term in lowered for term in duplicate_terms)

    def _reason_implies_single_representative(
        self,
        reason: str,
        subgroups: list[dict[str, Any]],
        valid_filenames: list[str],
    ) -> bool:
        """Recover when the model says redundant but accidentally splits into many subgroups."""

        if len(valid_filenames) < 2 or len(subgroups) <= 1:
            return False
        covered = {filename for subgroup in subgroups for filename in subgroup.get("images", [])}
        if covered != set(valid_filenames):
            return False
        if not self._looks_like_accidental_one_per_photo_split(subgroups):
            return False

        text_parts = [reason]
        text_parts.extend(str(subgroup.get("reason", "")) for subgroup in subgroups)
        lowered = " ".join(text_parts).lower()
        redundant_terms = (
            "near-duplicate",
            "near duplicate",
            "duplicates",
            "redundant",
            "no significant difference",
            "no significant differences",
            "only one representative",
            "one representative",
            "warrant keeping more than one",
        )
        distinct_terms = (
            "distinct pose",
            "different pose",
            "different subject",
            "different moment",
            "different composition",
            "meaningfully different",
            "unique",
        )
        return any(term in lowered for term in redundant_terms) and not any(term in lowered for term in distinct_terms)

    def _looks_like_accidental_one_per_photo_split(self, subgroups: list[dict[str, Any]]) -> bool:
        """Return True only for the bad pattern where duplicates were split one-by-one."""

        if any(len(subgroup.get("images", [])) != 1 for subgroup in subgroups):
            return False
        generic_names = ("distinct photo", "photo ", "image ", "subgroup ")
        return all(
            str(subgroup.get("name", "")).strip().lower().startswith(generic_names)
            for subgroup in subgroups
        )

    def _encode_image_base64(self, image_path: Path) -> str:
        return base64.b64encode(image_path.read_bytes()).decode("ascii")

    def _encode_image_data_url(self, image_path: Path) -> str:
        mime_type = "image/png" if image_path.suffix.lower() == ".png" else "image/jpeg"
        return f"data:{mime_type};base64,{self._encode_image_base64(image_path)}"

    def _encode_labeled_image_base64(self, image_path: Path, label: str) -> str:
        """Encode a preview with a visible label so the model maps images to filenames."""

        with Image.open(image_path) as image:
            image = ImageOps.exif_transpose(image).convert("RGB")
            header_height = 44
            labeled = Image.new("RGB", (image.width, image.height + header_height), "white")
            labeled.paste(image, (0, header_height))
            draw = ImageDraw.Draw(labeled)
            draw.text((12, 12), label, fill="black")
            buffer = io.BytesIO()
            labeled.save(buffer, format="JPEG", quality=88)
        return base64.b64encode(buffer.getvalue()).decode("ascii")

    def _encode_labeled_image_data_url(self, image_path: Path, label: str) -> str:
        return f"data:image/jpeg;base64,{self._encode_labeled_image_base64(image_path, label)}"

    def _uses_openai_chat_api(self) -> bool:
        return "/v1/chat/completions" in self.settings.vision_llm_endpoint

    def _build_subgroup_prompt(self, group: DuplicateGroup) -> str:
        return self._build_subgroup_prompt_for_images(group.images, group.group_id)

    def _build_adjacent_pair_prompt(
        self,
        source: ImageRecord,
        target: ImageRecord,
        context_name: str,
    ) -> str:
        return (
            "Compare exactly two candidate photographs. Determine whether they "
            "compete for the same best-frame decision.\n\n"
            f"Context: {context_name}\n"
            f"Photo 1: {source.filename} | photo_id={source.id}\n"
            f"Photo 2: {target.filename} | photo_id={target.id}\n\n"
            "Describe each supplied image independently before deciding. Set "
            "same_best_frame_decision to false when the principal subject, visible "
            "scene, or photographic purpose materially changes. Different streets, "
            "alleys, plazas, buildings, landscapes, or independently useful "
            "environmental views require false even when captured seconds apart. "
            "Expression, timing, focus, exposure, and small framing changes remain true "
            "when the principal subject and scene are otherwise the same.\n\n"
            "Return only JSON using this exact schema:\n"
            '{"photo_observations":[{"filename":"first filename",'
            '"principal_subject":"visible principal subject",'
            '"visible_scene":"what is visibly shown"},{"filename":"second filename",'
            '"principal_subject":"visible principal subject",'
            '"visible_scene":"what is visibly shown"}],'
            '"same_best_frame_decision":false,'
            '"reason":"visible evidence for the decision"}'
        )

    def _build_winner_pair_prompt(
        self,
        first: ImageRecord,
        second: ImageRecord,
        context_name: str,
    ) -> str:
        return (
            "Compare exactly two photographs that already belong to the same semantic "
            "subgroup. Select the technically and visually stronger photograph.\n\n"
            f"Context: {context_name}\n"
            f"Candidate A: {first.filename} | photo_id={first.id}\n"
            f"Candidate B: {second.filename} | photo_id={second.id}\n\n"
            "Describe both images independently, then choose using focus, motion "
            "quality, timing, expression, exposure, composition, obstruction, and "
            "overall visual effectiveness.\n\n"
            "Return only JSON using this exact schema:\n"
            '{"photo_observations":[{"filename":"first filename",'
            '"principal_subject":"visible principal subject",'
            '"visible_scene":"visible quality and composition"},{"filename":"second filename",'
            '"principal_subject":"visible principal subject",'
            '"visible_scene":"visible quality and composition"}],'
            '"winner":"one exact filename","reason":"why it is stronger"}'
        )

    def _build_semantic_review_prompt(
        self,
        images: list[ImageRecord],
        context_name: str,
    ) -> str:
        filenames = "\n".join(
            f"{index}. {image.filename} | photo_id={image.id}"
            for index, image in enumerate(images, start=1)
        )
        subgroup_mode = (
            "You may return one or more subgroups."
            if self.allow_subgroups
            else "Subgrouping is disabled. Return exactly one subgroup containing every listed photograph."
        )
        return (
            "You are reviewing one broad candidate photo group. Broad groups are intentionally permissive and may "
            "contain photographs that were captured close together but represent different photographic subjects.\n\n"
            f"Group: {context_name}\n"
            f"Allowed filenames:\n{filenames}\n\n"
            "Each supplied image has a visible label containing the same sequence "
            "number, filename, and photo ID. Inspect every labeled image individually.\n\n"
            "Your task is to partition the photographs according to this question:\n"
            "Which photographs compete for the same best-frame decision?\n\n"
            f"{subgroup_mode}\n\n"
            "CONSERVATIVE RETENTION POLICY\n"
            "Put photographs in the same subgroup only when they are genuinely interchangeable choices for one "
            "keeper. Sharing the same subject, building, room, location, or capture sequence is not sufficient. "
            "When uncertain whether two photographs provide independently useful visual coverage, keep them in "
            "separate subgroups. Prefer retaining an extra representative over rejecting a photograph with distinct "
            "photographic value.\n\n"
            "ARCHITECTURE AND VIEWPOINT RULES\n"
            "For architecture and places, create separate subgroups for materially different facades, corners, "
            "courtyards, interiors, exteriors, entrances, architectural details, wide establishing views, and "
            "environmental compositions. Also separate photographs made from materially different camera positions, "
            "viewing directions, fields of view, or compositions only when they provide materially different subject "
            "coverage, not merely a different crop or aspect ratio.\n\n"
            "ORIENTATION AND CROP VARIANT RULES\n"
            "Landscape and portrait photographs must remain in the same subgroup when they show the same principal "
            "subject from essentially the same camera position and moment, with strongly overlapping scene content. "
            "A change of aspect ratio, crop, or field of view does not create independent photographic value merely "
            "because one frame includes more wall, floor, sky, foreground, or the continuation of a doorway or "
            "building facade.\n\n"
            "Separate orientation variants only when one frame contains a materially important subject, action, "
            "architectural feature, or storytelling element that is absent from the other and gives the viewer a "
            "genuine reason to retain both. When the difference is primarily framing, the photographs compete for "
            "one keeper.\n\n"
            "EVIDENCE AND TRANSITION WORKFLOW\n"
            "First, write exactly one photo_observations entry for every photograph. "
            "Describe only the visible principal subject and visible scene in that "
            "specific labeled image. Do not copy an observation from another image.\n\n"
            "Second, compare every adjacent pair in the listed order. Return exactly "
            "one adjacent_transitions entry for each pair. Set "
            "same_best_frame_decision to false when the pair shows materially different "
            "principal subjects, scenes, or independently useful photographic "
            "interpretations. A false transition means that adjacent pair belongs to "
            "different subgroups, but it does not prevent a later photograph from "
            "joining an earlier visual subgroup. A true transition requires both "
            "adjacent photographs to remain in the same subgroup.\n\n"
            "SUBGROUP CRITERIA\n"
            "Keep photographs together when they show the same principal subject or subject set and differ mainly "
            "in expression, gesture, eye direction, pose, walking stride, subject position, timing, focus, exposure, "
            "motion quality, minor framing, small viewpoint changes, or orientation. These differences affect which "
            "photo should be picked; they do not independently justify another subgroup.\n\n"
            "Create separate subgroups when the photographic subject or selection purpose materially changes. Examples "
            "include a significant principal person, animal, vehicle, or object appearing or disappearing; a material "
            "change in principal subject identity or category; an independent environmental photograph changing into "
            "a subject-driven sequence; or the same location supporting independently useful photographic interpretations "
            "from which a viewer would reasonably retain one image each.\n\n"
            "When the photographs are genuinely interchangeable, do not split because a tiny or distant incidental "
            "person appears, a background object changes slightly, a subject moves within the frame, a person turns "
            "or gestures, focus or exposure changes, portrait and landscape versions show the same principal subject "
            "with materially equivalent coverage and composition, or one image is technically weaker. These examples "
            "must not override the conservative retention policy or the architecture and viewpoint rules.\n\n"
            "Do not identify named people, exact landmarks, or unverified locations.\n\n"
            "WINNER CRITERIA\n"
            "After deciding subgroup membership, select exactly one representative from each subgroup. Choose the "
            "technically and visually strongest photograph using focus, motion quality, expression, gesture, timing, "
            "exposure, composition, obstruction, and overall visual effectiveness. Winner criteria must not be used "
            "to create extra subgroups.\n\n"
            "Return only JSON using this exact schema:\n"
            '{"photo_observations":[{"filename":"filename.jpg",'
            '"principal_subject":"specific visible principal subject",'
            '"visible_scene":"concise description of what is visibly shown"}],'
            '"adjacent_transitions":[{"from":"earlier filename.jpg",'
            '"to":"next filename.jpg","same_best_frame_decision":true,'
            '"reason":"visible evidence supporting same or separate decisions"}],'
            '"subgroups":[{"name":"short label","main_subject":"principal subject or photographic purpose",'
            '"images":["filename.jpg"],"representative":"filename.jpg","reason":"why these photographs compete '
            'for one best-frame decision and why the representative is strongest"}],"reason":"short overall explanation"}\n\n'
            "Before responding, verify that every allowed filename has exactly one "
            "observation, every adjacent pair has exactly one transition in input order, "
            "and every allowed filename appears in exactly one subgroup. Non-adjacent "
            "photographs may share a subgroup when they are genuinely interchangeable. "
            "Verify that no unknown filename appears, every subgroup is non-empty, every "
            "representative belongs to its subgroup, and every transition agrees with "
            "the subgroup membership of its adjacent pair."
        )

    def _build_subgroup_prompt_for_images(self, images: list[ImageRecord], context_name: str) -> str:
        filenames = "\n".join(f"{index + 1}. {image.filename}" for index, image in enumerate(images))
        return (
            "You are helping split a burst or near-duplicate photo group into visual subgroups.\n"
            f"Current photo set: {context_name}\n"
            "Each provided image has a visible label at the top with its number and filename. The images are also provided in the same order as these filenames:\n"
            f"{filenames}\n\n"
            "Use ONLY the filenames listed above. Do not invent filenames, do not use filenames from previous requests, "
            "and do not include any filename more than once.\n\n"
            "Your only task is subgrouping. Do NOT choose winners or representatives in this step.\n\n"
            "First, identify the main subject of each image before deciding subgroups. The main subject is the specific visual subject "
            "the photo is about, not a broad category. Examples: the exact building or facade, a specific bird/animal, a person/portrait, "
            "a particular street market view, a fruit display, a bridge, a river scene, a skyline/panorama, or a forest path. Use this "
            "main-subject identity as the primary grouping rule.\n\n"
            "Split the images into visual subgroups. A subgroup should contain photos that are near-similar to each other: "
            "same subject, similar framing, similar moment, and redundant enough that only one representative should be kept. "
            "Create separate subgroups when photos show meaningfully different angles, compositions, subject moments, or scene content.\n\n"
            "For buildings and architecture, do not group photos merely because they are all buildings or facades. They must show the "
            "same exact building, same facade, or same architectural subject with similar framing. Different buildings, different facades, "
            "different structures, different courtyards, different streets, and different landmarks are separate subgroups, even if they "
            "were photographed seconds apart in the same place.\n\n"
            "For portraits, people, and street photos, the main subject matters. A close portrait/person-focused photo is separate from "
            "a general street, market, landscape, or building photo. Different people, different poses, different actions, or a person "
            "being present versus absent should usually be separate subgroups.\n\n"
            "Do not create one subgroup per filename by default. If two or more images are near-duplicates with no meaningful visual difference, "
            "put them in the same subgroup and choose only one representative from that subgroup.\n\n"
            "Same theme, same city, same trip, same broad location, or same type of subject is NOT enough to make photos near-duplicates. "
            "Different landmarks, different buildings, different street views, different gardens, different panoramas, and different viewpoints "
            "must be separate subgroups even if they share a similar travel/location theme.\n\n"
            "Important: the presence or absence of a person, animal, vehicle, sign, action, gesture, facial expression, or other distinct "
            "story element usually makes a photo visually different enough to keep as its own subgroup, even if the background scene is similar. "
            "Do not discard a photo just because a person or subject blocks part of the scene; treat that person or subject as meaningful content "
            "unless they are tiny, accidental, or clearly irrelevant.\n\n"
            "For animals and people, pose matters. Different facing direction, body orientation, head direction, expression, gesture, posture, "
            "or action should create separate subgroups. For example, an animal facing left and the same animal facing right are different "
            "subgroups; choose one best representative from each direction/pose subgroup.\n\n"
            "For wildlife, animal identity matters too. Different species, different colors/markings, different individual animals, or different "
            "bird types are separate subgroups even if they are both birds/animals in similar trees or branches. Same broad category such as "
            "'birds in trees' is not enough to keep only one.\n\n"
            "If a subgroup is named or described as containing a person, animal, vehicle, sign, fruit, food, produce, display, or other distinct subject, every image in that "
            "subgroup must actually show that subject, and the representative must be an image that clearly shows that subject. Do not choose "
            "a building, facade, panorama, empty scene, field, or unrelated scene as the representative for a person/animal/fruit/food/subject subgroup.\n\n"
            "A close or person-focused photo is a separate subgroup from a landscape/stream/path photo where the person is small, incidental, "
            "or absent. If one image is mainly about a person and another is mainly about the stream, forest, path, bridge, building, or view, "
            "create separate subgroups and keep one representative from each.\n\n"
            "Before returning JSON, verify each subgroup: the subgroup name must match all images inside it, and the representative filename "
            "will be chosen in a later step. If an image does not match any subgroup name, create a separate "
            "subgroup for it rather than forcing it into the nearest broad category.\n\n"
            "Every filename must appear in exactly one subgroup. If one photo has a person and another photo does not, create separate subgroups "
            "and keep one representative from each subgroup later. Do not put visually distinct photos into the same subgroup just because they share the same location.\n\n"
            "Return only JSON with this exact schema:\n"
            '{"subgroups":[{"name":"short label","main_subject":"specific subject identity shared by these images","images":["filename.jpg"],"reason":"why these images belong together"}],"reason":"short overall explanation"}'
        )

    def _build_selection_prompt(self, subgroup_name: str, images: list[ImageRecord]) -> str:
        filenames = "\n".join(f"{index + 1}. {image.filename}" for index, image in enumerate(images))
        return (
            "You are choosing the single best keeper from one visual subgroup.\n"
            "Each provided image has a visible label at the top with its number and filename. The images are also provided in the same order as these filenames:\n"
            f"{filenames}\n\n"
            f"Subgroup name: {subgroup_name}\n\n"
            "Use ONLY one of the filenames listed above. Do not invent filenames and do not choose a filename from another subgroup.\n\n"
            "Choose exactly one representative photo to keep. Select the image with the best overall photographic quality, "
            "not just the largest or widest view. Prefer the best balance of sharpness/in-focus detail, good exposure with preserved highlights "
            "and shadows, pleasing contrast and color, clear subject visibility, natural pose/expression when people or animals are present, "
            "strong composition/framing, and minimal obstruction, motion blur, noise, or distracting accidental elements. If two images in a "
            "subgroup are similarly sharp, choose the more visually pleasing one.\n\n"
            "Briefly mention the main quality factors that drove the choice, such as sharpness, exposure, contrast, "
            "composition, expression, obstruction, or visual appeal.\n\n"
            "Return only JSON with this exact schema:\n"
            '{"representative":"filename.jpg","reason":"why this photo was selected"}'
        )

    def _build_pose_refinement_prompt(
        self,
        images: list[ImageRecord],
        context_name: str,
        original_subgroup: dict[str, Any],
    ) -> str:
        filenames = "\n".join(f"{index + 1}. {image.filename}" for index, image in enumerate(images))
        original_name = str(original_subgroup.get("name", "animal/wildlife subgroup"))
        original_subject = str(original_subgroup.get("main_subject", original_name))
        return (
            "You are refining an animal or wildlife photo subgroup that may be too broad.\n"
            f"Current photo set: {context_name}\n"
            f"Original subgroup: {original_name}\n"
            f"Original main subject: {original_subject}\n\n"
            "Each provided image has a visible label at the top with its number and filename. The images are also provided in the same order as these filenames:\n"
            f"{filenames}\n\n"
            "Use ONLY the filenames listed above. Do not invent filenames, do not use filenames from previous requests, "
            "and do not include any filename more than once.\n\n"
            "Split these animal/wildlife photos into pose-aware visual subgroups. Photos belong together only when they show the same specific animal subject "
            "with the same meaningful pose/orientation and similar framing. Create separate subgroups for:\n"
            "- animal facing left versus facing right\n"
            "- animal looking toward the camera/front-facing versus side-facing\n"
            "- different head direction, body orientation, posture, action, expression, or moment\n"
            "- different individual animals/species/colors/markings\n"
            "- a close-up/detail view versus a wider environmental view\n\n"
            "For a chameleon/lizard/reptile on a branch, facing direction is important. A chameleon facing right, facing left, and looking at the camera/front "
            "should be separate subgroups, with one representative chosen later from each subgroup.\n\n"
            "Do not keep all images in one subgroup just because they all show the same animal type on a branch. Only keep them together if the pose, facing direction, "
            "head direction, and framing are visually redundant.\n\n"
            "Every filename must appear in exactly one subgroup. Return only JSON with this exact schema:\n"
            '{"subgroups":[{"name":"short pose-aware label","main_subject":"specific animal plus pose/facing direction","images":["filename.jpg"],"reason":"why these images are redundant enough to belong together"}],"reason":"short overall explanation"}'
        )
