from __future__ import annotations

from app.config import AppSettings
from app.core.duplicate_grouper import DuplicateGrouper
from app.core.models import ImageRecord
from app.core.similarity_engine import SimilarityEngine
from server.config import load_config
from server.models import PhotoGroup


def create_similarity_groups(job_photos: list[ImageRecord]) -> list[PhotoGroup]:
    config = load_config()
    settings = AppSettings(
        preview_long_edge=config.max_preview_long_edge,
        comparison_time_window_seconds=config.comparison_time_window_seconds,
        burst_time_window_seconds=config.burst_time_window_seconds,
        similarity_threshold=config.default_group_similarity_threshold,
        exact_duplicate_threshold=config.exact_duplicate_threshold,
        auto_suggest_winner=True,
        same_orientation_preference=config.same_orientation_preference,
        vision_llm_endpoint=config.vision_endpoint,
        vision_llm_model=config.model_name,
        vision_llm_timeout_seconds=config.model_timeout_seconds,
    )
    edges = SimilarityEngine(settings).analyze_records(job_photos)
    duplicate_groups = DuplicateGrouper(settings).build_groups(job_photos, edges)
    return [PhotoGroup(group_id=group.group_id, photos=[image.id for image in group.images]) for group in duplicate_groups if group.images]
