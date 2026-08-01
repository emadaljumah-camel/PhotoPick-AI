from __future__ import annotations

from app.config import AppSettings, DEFAULT_VISION_LLM_MODEL
from app.core.local_ai_reviewer import LocalAIReviewer
from app.core.models import DuplicateGroup, ImageRecord
from app.core.vision_llm_reviewer import VisionLLMReviewer
from server.config import load_config
from server.models import GroupDecision, PhotoGroup


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
        review = VisionLLMReviewer(settings).review_group(duplicate_group)
        return _from_vision_subgroups(group.group_id, group_images, review.subgroups, review.reason)
    except Exception as exc:
        local = LocalAIReviewer().review_group(duplicate_group)
        selected = set(local.selected_filenames)
        photo_ids = [image.id for image in group_images]
        return [
            GroupDecision(
                group_id=group.group_id,
                photos=photo_ids,
                representatives=[image.id for image in group_images if image.filename in selected],
                rejected=[image.id for image in group_images if image.filename not in selected],
                reason=f"Vision model failed; local fallback used. {exc}",
                confidence=0.55,
            )
        ]


def _from_vision_subgroups(group_id: str, images: list[ImageRecord], subgroups: list[dict], overall_reason: str) -> list[GroupDecision]:
    ids = {image.id for image in images}
    id_by_filename = {image.filename: image.id for image in images}
    decisions: list[GroupDecision] = []
    assigned: set[str] = set()
    for index, subgroup in enumerate(subgroups or [], start=1):
        photo_ids = []
        for item in subgroup.get("images", []):
            photo_id = item if item in ids else id_by_filename.get(item)
            if photo_id:
                photo_ids.append(photo_id)
        representative_item = subgroup.get("representative")
        representative = representative_item if representative_item in ids else id_by_filename.get(representative_item)
        reps = [representative] if representative in photo_ids else photo_ids[:1]
        rejected = [photo_id for photo_id in photo_ids if photo_id not in set(reps)]
        assigned.update(photo_ids)
        decisions.append(
            GroupDecision(
                group_id=f"{group_id}_{index:02d}",
                photos=photo_ids,
                representatives=reps,
                rejected=rejected,
                reason=str(subgroup.get("reason", overall_reason)),
                confidence=0.9 if reps else 0.0,
            )
        )
    for photo_id in sorted(ids - assigned):
        decisions.append(
            GroupDecision(
                group_id=f"{group_id}_unassigned_{len(decisions) + 1:02d}",
                photos=[photo_id],
                representatives=[photo_id],
                rejected=[],
                reason="Vision response omitted this photo, so it was kept separately.",
                confidence=0.5,
            )
        )
    return decisions
