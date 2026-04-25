from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

from app.core.models import DuplicateGroup, ImageRecord
from app.core.similarity_engine import cosine_similarity, extract_similarity_vector

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class LocalAIReviewResult:
    """Local-only recommendation for a review group."""

    selected_filenames: list[str]
    winner: str | None
    reason: str
    quality_scores: dict[str, float] = field(default_factory=dict)


class LocalAIReviewer:
    """Recommend keepers inside a group using local computer-vision signals."""

    def review_group(self, group: DuplicateGroup) -> LocalAIReviewResult:
        """Choose one winner, or multiple keepers when images are visually different."""

        if not group.images:
            return LocalAIReviewResult([], None, "No images in group")

        self._ensure_vectors(group.images)
        quality_scores = {image.filename: self._quality_score(image) for image in group.images}
        ranked_images = sorted(group.images, key=lambda image: quality_scores[image.filename], reverse=True)
        winner = ranked_images[0].filename

        keepers = [ranked_images[0]]
        diversity_threshold = 0.86 if group.group_type == "Burst candidate" else 0.82

        for candidate in ranked_images[1:]:
            closest_similarity = max(self._visual_similarity(candidate, kept) for kept in keepers)
            if closest_similarity < diversity_threshold:
                keepers.append(candidate)

        selected = [image.filename for image in keepers]
        if len(selected) == 1:
            reason = (
                "Selected the strongest local-quality frame using sharpness, exposure, contrast, "
                "and a simple visual-appeal score; the rest look visually redundant."
            )
        else:
            reason = (
                f"Kept {len(selected)} visually different frames and ranked each choice by sharpness, "
                "exposure, contrast, and simple visual appeal."
            )

        LOGGER.info("Local AI selected %s for %s", selected, group.group_id)
        return LocalAIReviewResult(
            selected_filenames=selected,
            winner=winner,
            reason=reason,
            quality_scores=quality_scores,
        )

    def _ensure_vectors(self, images: list[ImageRecord]) -> None:
        for image in images:
            if image.similarity_vector and image.focus_vector and image.edge_vector:
                continue
            source_path = Path(image.preview_path or image.path)
            vector, focus_vector, edge_vector, phash = extract_similarity_vector(source_path)
            image.similarity_vector = vector.astype(float).tolist()
            image.focus_vector = focus_vector.astype(float).tolist()
            image.edge_vector = edge_vector.astype(float).tolist()
            image.phash = phash

    def _quality_score(self, image: ImageRecord) -> float:
        exposure_score, contrast_score, appeal_score = self._image_quality_components(image)
        sharpness = np.log1p(max(image.sharpness_score, 0.0)) / 10.0
        size_score = np.log1p(max(image.file_size, 0)) / 18.0
        return float(
            (sharpness * 0.45)
            + (exposure_score * 0.22)
            + (contrast_score * 0.18)
            + (appeal_score * 0.10)
            + (size_score * 0.05)
        )

    def _image_quality_components(self, image: ImageRecord) -> tuple[float, float, float]:
        source_path = image.preview_path or image.path
        color = cv2.imread(str(source_path), cv2.IMREAD_COLOR)
        if color is None:
            return 0.5, 0.0, 0.0

        gray = cv2.cvtColor(color, cv2.COLOR_BGR2GRAY)
        mean = float(np.mean(gray)) / 255.0
        std = float(np.std(gray)) / 128.0
        exposure_score = max(0.0, 1.0 - abs(mean - 0.5) * 2.0)
        contrast_score = min(1.0, std)

        # Local-only visual appeal proxy. A later AI recommender can replace or augment this
        # with richer composition/subject-quality analysis while keeping originals untouched.
        blue, green, red = cv2.split(color.astype(np.float32))
        rg = np.abs(red - green)
        yb = np.abs(0.5 * (red + green) - blue)
        colorfulness = np.sqrt(np.std(rg) ** 2 + np.std(yb) ** 2) + 0.3 * np.sqrt(np.mean(rg) ** 2 + np.mean(yb) ** 2)
        color_score = min(1.0, float(colorfulness) / 80.0)
        appeal_score = float((exposure_score * 0.35) + (contrast_score * 0.35) + (color_score * 0.30))
        return exposure_score, contrast_score, appeal_score

    def _visual_similarity(self, left: ImageRecord, right: ImageRecord) -> float:
        left_global = np.asarray(left.similarity_vector, dtype=np.float32)
        right_global = np.asarray(right.similarity_vector, dtype=np.float32)
        left_focus = np.asarray(left.focus_vector, dtype=np.float32)
        right_focus = np.asarray(right.focus_vector, dtype=np.float32)
        left_edge = np.asarray(left.edge_vector, dtype=np.float32)
        right_edge = np.asarray(right.edge_vector, dtype=np.float32)

        global_similarity = cosine_similarity(left_global, right_global)
        focus_similarity = cosine_similarity(left_focus, right_focus)
        edge_similarity = cosine_similarity(left_edge, right_edge)
        return float((global_similarity * 0.45) + (focus_similarity * 0.35) + (edge_similarity * 0.2))
