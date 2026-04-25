from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Callable

import cv2
import numpy as np
from PIL import Image, ImageOps

from app.config import AppSettings
from app.core.models import ImageRecord
from app.core.sharpness import estimate_sharpness

LOGGER = logging.getLogger(__name__)


def average_hash(image: Image.Image, hash_size: int = 8) -> str:
    """Compute a simple perceptual average hash."""

    grayscale = image.convert("L").resize((hash_size, hash_size), Image.Resampling.LANCZOS)
    pixels = np.asarray(grayscale, dtype=np.float32)
    threshold = pixels.mean()
    bits = (pixels >= threshold).astype(np.uint8).flatten()
    return "".join(str(bit) for bit in bits.tolist())


def hamming_distance(hash_a: str, hash_b: str) -> int:
    """Return Hamming distance between two equal-length binary strings."""

    if len(hash_a) != len(hash_b):
        return max(len(hash_a), len(hash_b))
    return sum(left != right for left, right in zip(hash_a, hash_b))


def cosine_similarity(vec_a: np.ndarray, vec_b: np.ndarray) -> float:
    """Compute cosine similarity with safe zero checks."""

    denom = np.linalg.norm(vec_a) * np.linalg.norm(vec_b)
    if denom == 0:
        return 0.0
    return float(np.dot(vec_a, vec_b) / denom)


def extract_similarity_vector(image_path: Path) -> tuple[np.ndarray, np.ndarray, np.ndarray, str]:
    """Build compact local descriptors and a perceptual hash."""

    with Image.open(image_path) as image:
        image = ImageOps.exif_transpose(image).convert("RGB")
        phash = average_hash(image)
        rgb_image = np.asarray(image, dtype=np.uint8)
        global_gray = np.asarray(image.convert("L").resize((32, 32), Image.Resampling.LANCZOS), dtype=np.float32).flatten()
        global_gray = global_gray / 255.0
        global_hist = _hsv_histogram(rgb_image, bins=(12, 8))
        thirds_gray = _grid_descriptor(rgb_image, rows=3, cols=3, sample_size=12)
        center_gray = _center_crop_descriptor(rgb_image, crop_ratio=0.72, sample_size=18)
        focus_gray = _shift_tolerant_focus_descriptor(rgb_image, sample_size=24)
        edge_gray = _edge_descriptor(rgb_image, sample_size=28)
        vector = np.concatenate([global_hist, thirds_gray, center_gray, global_gray.astype(np.float32)])
        return vector, focus_gray, edge_gray, phash


def _hsv_histogram(image_rgb: np.ndarray, bins: tuple[int, int]) -> np.ndarray:
    """Compute a normalized HSV histogram descriptor."""

    hsv = cv2.cvtColor(image_rgb, cv2.COLOR_RGB2HSV)
    hist = cv2.calcHist([hsv], [0, 1], None, list(bins), [0, 180, 0, 256]).flatten()
    return cv2.normalize(hist, hist).flatten().astype(np.float32)


def _grid_descriptor(image_rgb: np.ndarray, rows: int, cols: int, sample_size: int) -> np.ndarray:
    """Build a region-aware grayscale descriptor for translation tolerance."""

    height, width = image_rgb.shape[:2]
    row_edges = np.linspace(0, height, rows + 1, dtype=int)
    col_edges = np.linspace(0, width, cols + 1, dtype=int)
    parts: list[np.ndarray] = []

    for row_index in range(rows):
        for col_index in range(cols):
            patch = image_rgb[row_edges[row_index] : row_edges[row_index + 1], col_edges[col_index] : col_edges[col_index + 1]]
            if patch.size == 0:
                parts.append(np.zeros(sample_size * sample_size, dtype=np.float32))
                continue
            gray = cv2.cvtColor(patch, cv2.COLOR_RGB2GRAY)
            reduced = cv2.resize(gray, (sample_size, sample_size), interpolation=cv2.INTER_AREA)
            parts.append(reduced.astype(np.float32).flatten() / 255.0)

    return np.concatenate(parts).astype(np.float32)


def _center_crop_descriptor(image_rgb: np.ndarray, crop_ratio: float, sample_size: int) -> np.ndarray:
    """Describe the central subject area so framing changes hurt less."""

    height, width = image_rgb.shape[:2]
    crop_height = max(8, int(height * crop_ratio))
    crop_width = max(8, int(width * crop_ratio))
    top = max(0, (height - crop_height) // 2)
    left = max(0, (width - crop_width) // 2)
    patch = image_rgb[top : top + crop_height, left : left + crop_width]
    gray = cv2.cvtColor(patch, cv2.COLOR_RGB2GRAY)
    reduced = cv2.resize(gray, (sample_size, sample_size), interpolation=cv2.INTER_AREA)
    return (reduced.astype(np.float32).flatten() / 255.0).astype(np.float32)


def _shift_tolerant_focus_descriptor(image_rgb: np.ndarray, sample_size: int) -> np.ndarray:
    """Describe overlapping inner crops so small framing shifts remain close."""

    height, width = image_rgb.shape[:2]
    descriptors: list[np.ndarray] = []
    crop_h = max(12, int(height * 0.68))
    crop_w = max(12, int(width * 0.68))
    offsets = (-0.08, 0.0, 0.08)

    for y_offset in offsets:
        for x_offset in offsets:
            center_y = (height / 2.0) + (height * y_offset)
            center_x = (width / 2.0) + (width * x_offset)
            top = int(max(0, min(height - crop_h, center_y - (crop_h / 2))))
            left = int(max(0, min(width - crop_w, center_x - (crop_w / 2))))
            patch = image_rgb[top : top + crop_h, left : left + crop_w]
            gray = cv2.cvtColor(patch, cv2.COLOR_RGB2GRAY)
            reduced = cv2.resize(gray, (sample_size, sample_size), interpolation=cv2.INTER_AREA)
            descriptors.append(reduced.astype(np.float32).flatten() / 255.0)

    return np.concatenate(descriptors).astype(np.float32)


def _edge_descriptor(image_rgb: np.ndarray, sample_size: int) -> np.ndarray:
    """Describe edge structure so small subjects stand out more than soft backgrounds."""

    gray = cv2.cvtColor(image_rgb, cv2.COLOR_RGB2GRAY)
    edges = cv2.Canny(gray, 40, 120)
    reduced = cv2.resize(edges, (sample_size, sample_size), interpolation=cv2.INTER_AREA)
    return (reduced.astype(np.float32).flatten() / 255.0).astype(np.float32)


class SimilarityEngine:
    """Compute image similarity signals and candidate edges."""

    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings

    def analyze_records(
        self,
        records: list[ImageRecord],
        progress_callback: Callable[[int, int, str], None] | None = None,
    ) -> list[tuple[str, str, float, str]]:
        """Populate record-level similarity data and return matched edges."""

        total = len(records) or 1
        for index, record in enumerate(records, start=1):
            # Similarity uses cached previews so repeated review runs stay fast.
            # A future local or AI-based ranking pass could inspect originals later.
            source_for_similarity = Path(record.preview_path or record.path)
            vector, focus_vector, edge_vector, phash = extract_similarity_vector(source_for_similarity)
            record.similarity_vector = vector.astype(float).tolist()
            record.focus_vector = focus_vector.astype(float).tolist()
            record.edge_vector = edge_vector.astype(float).tolist()
            record.phash = phash
            record.sharpness_score = estimate_sharpness(Path(record.path))
            if progress_callback:
                progress_callback(index, total, f"Extracted features for {record.filename}")

        sorted_records = sorted(records, key=lambda item: item.capture_datetime)
        matched_edges: list[tuple[str, str, float, str]] = []
        window_seconds = self.settings.comparison_time_window_seconds
        total_pairs = max(1, len(sorted_records))
        candidate_scores: dict[tuple[str, str], tuple[float, float, int, float, float, float]] = {}

        for left_index, left in enumerate(sorted_records):
            left_vector = np.asarray(left.similarity_vector, dtype=np.float32)
            left_focus = np.asarray(left.focus_vector, dtype=np.float32)
            left_edge = np.asarray(left.edge_vector, dtype=np.float32)
            for right in sorted_records[left_index + 1 :]:
                delta = abs((right.capture_datetime - left.capture_datetime).total_seconds())
                if delta > window_seconds:
                    break
                if self.settings.same_orientation_preference and left.orientation != right.orientation:
                    continue
                ratio_gap = 0.0
                if left.aspect_ratio and right.aspect_ratio:
                    ratio_gap = abs(left.aspect_ratio - right.aspect_ratio)
                right_vector = np.asarray(right.similarity_vector, dtype=np.float32)
                right_focus = np.asarray(right.focus_vector, dtype=np.float32)
                right_edge = np.asarray(right.edge_vector, dtype=np.float32)
                visual_similarity = cosine_similarity(left_vector, right_vector)
                focus_similarity = self._focus_similarity(left_focus, right_focus)
                edge_similarity = cosine_similarity(left_edge, right_edge)
                hash_distance = hamming_distance(left.phash, right.phash)
                size_similarity = self._size_similarity(left, right)
                time_factor = self._time_factor(delta, window_seconds)
                similarity_score = self._combine_similarity(
                    visual_similarity,
                    focus_similarity,
                    edge_similarity,
                    hash_distance,
                    size_similarity,
                    time_factor,
                )
                candidate_scores[(left.id, right.id)] = (
                    similarity_score,
                    max(visual_similarity, focus_similarity),
                    hash_distance,
                    delta,
                    ratio_gap,
                    size_similarity,
                )
                if self._is_match(
                    left,
                    right,
                    ratio_gap,
                    visual_similarity,
                    focus_similarity,
                    edge_similarity,
                    hash_distance,
                    similarity_score,
                    size_similarity,
                ):
                    matched_edges.append((left.id, right.id, similarity_score, "near_duplicate"))
            if progress_callback:
                progress_callback(left_index + 1, total_pairs, f"Compared candidates for {left.filename}")

        matched_edges.extend(self._bridge_sequence_neighbors(sorted_records, candidate_scores))
        matched_edges.extend(self._burst_candidate_edges(sorted_records, candidate_scores))
        matched_edges.extend(self._temporal_burst_chain_edges(sorted_records))
        matched_edges = self._dedupe_edges(matched_edges)

        LOGGER.info("Built %s matched edges from %s records", len(matched_edges), len(records))
        return matched_edges

    def _combine_similarity(
        self,
        visual_similarity: float,
        focus_similarity: float,
        edge_similarity: float,
        hash_distance: int,
        size_similarity: float,
        time_factor: float,
    ) -> float:
        hash_similarity = max(0.0, 1.0 - (hash_distance / 64.0))
        return (
            (visual_similarity * 0.4)
            + (focus_similarity * 0.24)
            + (edge_similarity * 0.13)
            + (hash_similarity * 0.15)
            + (size_similarity * 0.03)
            + (time_factor * 0.05)
        )

    def _is_match(
        self,
        left: ImageRecord,
        right: ImageRecord,
        ratio_gap: float,
        visual_similarity: float,
        focus_similarity: float,
        edge_similarity: float,
        hash_distance: int,
        similarity_score: float,
        size_similarity: float,
    ) -> bool:
        exact_like = (
            visual_similarity >= self.settings.exact_duplicate_threshold
            and hash_distance <= 4
            and size_similarity >= 0.9
        )
        if exact_like:
            return True

        orientation_match = left.orientation == right.orientation
        burst_like_match = (
            focus_similarity >= max(0.89, self.settings.similarity_threshold - 0.02)
            and edge_similarity >= 0.77
            and similarity_score >= 0.875
            and hash_distance <= 18
            and size_similarity >= 0.58
        )
        wide_framing_match = (
            max(focus_similarity, edge_similarity) >= max(0.88, self.settings.similarity_threshold - 0.025)
            and similarity_score >= 0.87
            and hash_distance <= 20
            and size_similarity >= 0.55
            and ratio_gap <= 0.42
        )
        return (
            (
                (
                    visual_similarity >= self.settings.similarity_threshold
                    and hash_distance <= 14
                    and similarity_score >= 0.895
                    and size_similarity >= 0.68
                )
                or burst_like_match
                or wide_framing_match
            )
            and (ratio_gap <= 0.22 or burst_like_match or wide_framing_match)
            and (orientation_match or not self.settings.same_orientation_preference)
        )

    def _size_similarity(self, left: ImageRecord, right: ImageRecord) -> float:
        if not left.file_size or not right.file_size:
            return 0.0
        smaller = min(left.file_size, right.file_size)
        larger = max(left.file_size, right.file_size)
        return float(smaller / larger) if larger else 0.0

    def _time_factor(self, delta_seconds: float, window_seconds: int) -> float:
        if window_seconds <= 0:
            return 0.0
        return max(0.0, 1.0 - (delta_seconds / window_seconds))

    def _focus_similarity(self, left_focus: np.ndarray, right_focus: np.ndarray) -> float:
        """Compare overlapping inner crops to tolerate small framing shifts."""

        if left_focus.size == 0 or right_focus.size == 0:
            return 0.0
        chunk_count = 9
        left_chunks = np.array_split(left_focus, chunk_count)
        right_chunks = np.array_split(right_focus, chunk_count)
        scores = [cosine_similarity(left_chunk, right_chunk) for left_chunk, right_chunk in zip(left_chunks, right_chunks)]
        return float(np.mean(scores)) if scores else 0.0

    def _bridge_sequence_neighbors(
        self,
        sorted_records: list[ImageRecord],
        candidate_scores: dict[tuple[str, str], tuple[float, float, int, float, float, float]],
    ) -> list[tuple[str, str, float, str]]:
        """Bridge near-in-time burst neighbors when both sides are moderately similar."""

        bridged_edges: list[tuple[str, str, float, str]] = []
        if len(sorted_records) < 3:
            return bridged_edges

        for index in range(1, len(sorted_records) - 1):
            left = sorted_records[index - 1]
            middle = sorted_records[index]
            right = sorted_records[index + 1]
            left_key = (left.id, middle.id)
            right_key = (middle.id, right.id)
            if left_key not in candidate_scores or right_key not in candidate_scores:
                continue

            left_score, left_visual, left_hash, left_delta, _, _ = candidate_scores[left_key]
            right_score, right_visual, right_hash, right_delta, _, _ = candidate_scores[right_key]
            if (
                left_delta <= self.settings.comparison_time_window_seconds
                and right_delta <= self.settings.comparison_time_window_seconds
                and left_visual >= max(0.82, self.settings.similarity_threshold - 0.07)
                and right_visual >= max(0.82, self.settings.similarity_threshold - 0.07)
                and left_hash <= 18
                and right_hash <= 18
            ):
                bridged_edges.append((left.id, middle.id, max(left_score, 0.875), "near_duplicate"))
                bridged_edges.append((middle.id, right.id, max(right_score, 0.875), "near_duplicate"))

        return bridged_edges

    def _burst_candidate_edges(
        self,
        sorted_records: list[ImageRecord],
        candidate_scores: dict[tuple[str, str], tuple[float, float, int, float, float, float]],
    ) -> list[tuple[str, str, float, str]]:
        """Add looser same-moment burst candidate edges."""

        burst_edges: list[tuple[str, str, float, str]] = []
        burst_window = min(self.settings.comparison_time_window_seconds, self.settings.burst_time_window_seconds)
        if burst_window <= 0:
            return burst_edges

        for left_index, left in enumerate(sorted_records):
            for right in sorted_records[left_index + 1 :]:
                key = (left.id, right.id)
                if key not in candidate_scores:
                    continue
                score, visual_or_focus, hash_distance, delta, ratio_gap, size_similarity = candidate_scores[key]
                if delta > burst_window:
                    break
                if self.settings.same_orientation_preference and left.orientation != right.orientation:
                    continue
                if (
                    visual_or_focus >= max(0.8, self.settings.similarity_threshold - 0.08)
                    and score >= 0.84
                    and hash_distance <= 24
                    and size_similarity >= 0.45
                    and ratio_gap <= 0.5
                ):
                    burst_edges.append((left.id, right.id, max(score, 0.84), "burst"))

        return burst_edges

    def _temporal_burst_chain_edges(self, sorted_records: list[ImageRecord]) -> list[tuple[str, str, float, str]]:
        """Connect consecutive same-time frames as a broad burst group.

        This intentionally does not require strong visual similarity. Wildlife and travel bursts
        often jump between leaves, branches, animals, and slightly different framing; the Vision
        LLM review stage can later split the broad burst group into visual subgroups.
        """

        burst_edges: list[tuple[str, str, float, str]] = []
        burst_window = min(self.settings.comparison_time_window_seconds, self.settings.burst_time_window_seconds)
        sequence_window = max(burst_window, min(240, self.settings.comparison_time_window_seconds * 4))
        if sequence_window <= 0 or len(sorted_records) < 2:
            return burst_edges

        for left, right in zip(sorted_records, sorted_records[1:]):
            delta = abs((right.capture_datetime - left.capture_datetime).total_seconds())
            if delta > sequence_window:
                continue
            if self.settings.same_orientation_preference and left.orientation != right.orientation:
                continue
            if not self._reasonable_burst_sequence(left, right):
                continue
            burst_score = max(0.78, self._time_factor(delta, sequence_window))
            burst_edges.append((left.id, right.id, burst_score, "burst"))

        return burst_edges

    def _reasonable_burst_sequence(self, left: ImageRecord, right: ImageRecord) -> bool:
        ratio_gap = 0.0
        if left.aspect_ratio and right.aspect_ratio:
            ratio_gap = abs(left.aspect_ratio - right.aspect_ratio)
        if ratio_gap > 0.65:
            return False

        left_number = self._trailing_number(left.filename)
        right_number = self._trailing_number(right.filename)
        if left_number is None or right_number is None:
            return True
        return abs(right_number - left_number) <= 20

    def _trailing_number(self, filename: str) -> int | None:
        match = re.search(r"(\d+)(?=\D*$)", filename)
        return int(match.group(1)) if match else None

    def _dedupe_edges(self, edges: list[tuple[str, str, float, str]]) -> list[tuple[str, str, float, str]]:
        """Keep the highest score per undirected edge."""

        unique: dict[tuple[str, str], tuple[float, str]] = {}
        for left_id, right_id, score, edge_type in edges:
            key = tuple(sorted((left_id, right_id)))
            previous = unique.get(key)
            if previous is None or score > previous[0]:
                unique[key] = (score, edge_type)
            elif previous[1] != "near_duplicate" and edge_type == "near_duplicate":
                unique[key] = (previous[0], edge_type)
        return [(left_id, right_id, score, edge_type) for (left_id, right_id), (score, edge_type) in unique.items()]
