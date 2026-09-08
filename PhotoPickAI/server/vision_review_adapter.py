from __future__ import annotations

import json
import logging
from pathlib import Path

from app.config import AppSettings, DEFAULT_VISION_LLM_MODEL
from app.core.models import DuplicateGroup, ImageRecord
from app.core.vision_llm_reviewer import VisionLLMReviewer
from server.config import load_config
from server.job_store import JobStore
from server.models import GroupDecision, PhotoGroup


LOGGER = logging.getLogger(__name__)


def review_group_with_vision(group: PhotoGroup, photos: list[ImageRecord]) -> list[GroupDecision]:
    config = load_config()
    photos_by_id = {photo.id: photo for photo in photos}
    group_images = [photos_by_id[photo_id] for photo_id in group.photos if photo_id in photos_by_id]
    duplicate_group = DuplicateGroup(
        group_id=group.group_id,
        images=group_images,
        capture_span_seconds=0,
        avg_similarity=1.0,
        group_type="Near-duplicate" if len(group_images) > 1 else "Single photo",
    )

    if len(group_images) == 1:
        photo_id = group_images[0].id
        return [GroupDecision(group_id=group.group_id, photos=[photo_id], representatives=[photo_id], rejected=[], reason="Single-photo group kept.", confidence=1.0)]

    settings = AppSettings(
        preview_long_edge=config.max_preview_long_edge,
        similarity_threshold=config.default_group_similarity_threshold,
        vision_llm_endpoint=config.vision_endpoint,
        vision_llm_model=config.model_name or DEFAULT_VISION_LLM_MODEL,
        vision_llm_timeout_seconds=config.model_timeout_seconds,
    )

    try:
        review = VisionLLMReviewer(
            settings,
            allow_subgroups=config.allow_subgroups,
            retry_model_json_once=config.retry_model_json_once,
        ).review_group(duplicate_group)
        if review.semantic_fallback:
            decisions = _all_singleton_decisions(
                group.group_id,
                group_images,
                review.reason,
            )
        else:
            decisions = _from_vision_subgroups(
                group.group_id,
                group_images,
                review.subgroups,
                review.reason,
                allow_subgroups=config.allow_subgroups,
            )
        _record_semantic_diagnostic(
            config,
            group.group_id,
            group_images,
            review,
            decisions,
        )
        return decisions
    except Exception as exc:
        decisions = _all_singleton_decisions(
            group.group_id,
            group_images,
            f"Semantic adapter failure: {exc}",
        )
        LOGGER.exception(
            "semantic_subgroup_fallback=all_singletons broad_group_id=%s "
            "photo_count=%s reason=%s attempts=adapter_failure",
            group.group_id,
            len(group_images),
            exc,
        )
        return decisions


def _all_singleton_decisions(
    group_id: str,
    images: list[ImageRecord],
    reason: str,
) -> list[GroupDecision]:
    return [
        GroupDecision(
            group_id=f"{group_id}_fallback_{index:02d}",
            photos=[image.id],
            representatives=[image.id],
            rejected=[],
            reason=reason,
            confidence=0.0,
        )
        for index, image in enumerate(images, start=1)
    ]


def _record_semantic_diagnostic(
    config,
    broad_group_id: str,
    images: list[ImageRecord],
    review,
    decisions: list[GroupDecision],
) -> None:
    jobs_dir = config.jobs_dir.resolve()
    job_id = None
    for image in images:
        try:
            relative = Path(image.preview_path or image.path).resolve().relative_to(jobs_dir)
        except ValueError:
            continue
        if len(relative.parts) >= 3 and relative.parts[1] == "previews":
            job_id = relative.parts[0]
            break

    id_by_filename = {image.filename: image.id for image in images}

    def normalize(subgroups):
        normalized = []
        for subgroup in subgroups:
            filenames = subgroup.get("images", [])
            representative = subgroup.get("representative")
            normalized.append(
                {
                    "photo_ids": [id_by_filename.get(item, item) for item in filenames],
                    "picked_photo_id": id_by_filename.get(representative, representative),
                    "reason": str(subgroup.get("reason", "")),
                }
            )
        return normalized

    diagnostic = {
        "broad_group_id": broad_group_id,
        "semantic_contract_version": review.semantic_contract_version,
        "allow_subgroups": config.allow_subgroups,
        "ordered_input_photo_ids": [image.id for image in images],
        "ordered_input_filenames": [image.filename for image in images],
        "raw_response_content": review.raw_responses,
        "photo_observations": [
            {
                "photo_id": id_by_filename.get(
                    observation.get("filename"),
                    observation.get("filename"),
                ),
                "filename": observation.get("filename"),
                "principal_subject": observation.get("principal_subject", ""),
                "visible_scene": observation.get("visible_scene", ""),
            }
            for observation in review.photo_observations
        ],
        "adjacent_transitions": [
            {
                "from_photo_id": id_by_filename.get(
                    transition.get("from"),
                    transition.get("from"),
                ),
                "to_photo_id": id_by_filename.get(
                    transition.get("to"),
                    transition.get("to"),
                ),
                "from_filename": transition.get("from"),
                "to_filename": transition.get("to"),
                "same_best_frame_decision": transition.get(
                    "same_best_frame_decision"
                ),
                "reason": transition.get("reason", ""),
                "photo_observations": [
                    {
                        "photo_id": id_by_filename.get(
                            observation.get("filename"),
                            observation.get("filename"),
                        ),
                        "filename": observation.get("filename"),
                        "principal_subject": observation.get(
                            "principal_subject",
                            "",
                        ),
                        "visible_scene": observation.get("visible_scene", ""),
                    }
                    for observation in transition.get(
                        "photo_observations",
                        [],
                    )
                ],
                "attempts": transition.get("attempts", 0),
                "fallback": transition.get("fallback", False),
                "validation_errors": transition.get("validation_errors", []),
            }
            for transition in review.adjacent_transitions
        ],
        "reconciliation_comparisons": [
            {
                "left_component_photo_ids": [
                    id_by_filename.get(filename, filename)
                    for filename in comparison.get("left_component", [])
                ],
                "right_component_photo_ids": [
                    id_by_filename.get(filename, filename)
                    for filename in comparison.get("right_component", [])
                ],
                "candidate_a_photo_id": id_by_filename.get(
                    comparison.get("candidate_a"),
                    comparison.get("candidate_a"),
                ),
                "candidate_b_photo_id": id_by_filename.get(
                    comparison.get("candidate_b"),
                    comparison.get("candidate_b"),
                ),
                "candidate_a_filename": comparison.get("candidate_a"),
                "candidate_b_filename": comparison.get("candidate_b"),
                "local_similarity": comparison.get("local_similarity", 0.0),
                "same_best_frame_decision": comparison.get(
                    "same_best_frame_decision"
                ),
                "reason": comparison.get("reason", ""),
                "photo_observations": [
                    {
                        "photo_id": id_by_filename.get(
                            observation.get("filename"),
                            observation.get("filename"),
                        ),
                        "filename": observation.get("filename"),
                        "principal_subject": observation.get(
                            "principal_subject",
                            "",
                        ),
                        "visible_scene": observation.get("visible_scene", ""),
                    }
                    for observation in comparison.get(
                        "photo_observations",
                        [],
                    )
                ],
                "attempts": comparison.get("attempts", 0),
                "fallback": comparison.get("fallback", False),
                "validation_errors": comparison.get(
                    "validation_errors",
                    [],
                ),
            }
            for comparison in review.reconciliation_comparisons
        ],
        "winner_comparisons": [
            {
                "candidate_a_photo_id": id_by_filename.get(
                    comparison.get("candidate_a"),
                    comparison.get("candidate_a"),
                ),
                "candidate_b_photo_id": id_by_filename.get(
                    comparison.get("candidate_b"),
                    comparison.get("candidate_b"),
                ),
                "winner_photo_id": id_by_filename.get(
                    comparison.get("winner"),
                    comparison.get("winner"),
                ),
                "candidate_a_filename": comparison.get("candidate_a"),
                "candidate_b_filename": comparison.get("candidate_b"),
                "winner_filename": comparison.get("winner"),
                "reason": comparison.get("reason", ""),
                "attempts": comparison.get("attempts", 0),
                "fallback": comparison.get("fallback", False),
                "validation_errors": comparison.get(
                    "validation_errors",
                    [],
                ),
            }
            for comparison in review.winner_comparisons
        ],
        "parsed_subgroups": normalize(review.parsed_subgroups),
        "validated_subgroups": normalize(review.subgroups),
        "final_group_ids": [decision.group_id for decision in decisions],
        "selected_photo_ids": [
            photo_id
            for decision in decisions
            for photo_id in decision.representatives
        ],
        "semantic_fallback": review.semantic_fallback,
        "attempts": review.attempts,
        "validation_errors": review.validation_errors,
    }
    LOGGER.info("semantic_group_diagnostic=%s", json.dumps(diagnostic, ensure_ascii=False))

    if job_id:
        try:
            JobStore(config).save_semantic_diagnostic(
                job_id,
                broad_group_id,
                diagnostic,
            )
        except Exception:
            LOGGER.exception(
                "Failed to persist semantic diagnostics job=%s broad_group_id=%s",
                job_id,
                broad_group_id,
            )


def _from_vision_subgroups(
    group_id: str,
    images: list[ImageRecord],
    subgroups: list[dict],
    overall_reason: str,
    allow_subgroups: bool = True,
) -> list[GroupDecision]:
    ids = {image.id for image in images}
    id_by_filename = {image.filename: image.id for image in images}
    if not subgroups:
        raise ValueError("Semantic response contains no subgroups")
    if not allow_subgroups and len(subgroups) != 1:
        raise ValueError("Subgrouping is disabled but semantic response contains multiple subgroups")

    decisions: list[GroupDecision] = []
    assigned: set[str] = set()
    for index, subgroup in enumerate(subgroups or [], start=1):
        items = subgroup.get("images", [])
        if not isinstance(items, list) or not items:
            raise ValueError(f"Semantic subgroup {index} contains no photos")

        photo_ids: list[str] = []
        for item in items:
            photo_id = item if item in ids else id_by_filename.get(item)
            if not photo_id:
                raise ValueError(f"Semantic subgroup {index} contains unknown photo {item}")
            if photo_id in assigned or photo_id in photo_ids:
                raise ValueError(f"Semantic response assigns photo {photo_id} more than once")
            photo_ids.append(photo_id)

        representative_item = subgroup.get("representative")
        representative = representative_item if representative_item in ids else id_by_filename.get(representative_item)
        if representative not in photo_ids:
            raise ValueError(f"Semantic subgroup {index} has no valid representative")

        reps = [representative]
        rejected = [photo_id for photo_id in photo_ids if photo_id != representative]
        assigned.update(photo_ids)
        decisions.append(
            GroupDecision(
                group_id=(
                    f"{group_id}_{index:02d}"
                    if allow_subgroups
                    else group_id
                ),
                photos=photo_ids,
                representatives=reps,
                rejected=rejected,
                reason=str(subgroup.get("reason", overall_reason)),
                confidence=0.9 if reps else 0.0,
            )
        )

    if assigned != ids:
        missing = ids - assigned
        unknown = assigned - ids
        raise ValueError(
            f"Semantic response coverage mismatch; missing={sorted(missing)} unknown={sorted(unknown)}"
        )
    return decisions
