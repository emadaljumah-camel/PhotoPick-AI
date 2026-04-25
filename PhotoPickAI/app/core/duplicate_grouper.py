from __future__ import annotations

import logging
from collections import defaultdict
from hashlib import sha1
from typing import Callable

from app.config import AppSettings
from app.core.models import DuplicateGroup, ImageRecord

LOGGER = logging.getLogger(__name__)


class DuplicateGrouper:
    """Convert similarity edges into review-ready duplicate groups."""

    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings

    def build_groups(
        self,
        records: list[ImageRecord],
        edges: list[tuple[str, str, float, str]],
        progress_callback: Callable[[int, int, str], None] | None = None,
    ) -> list[DuplicateGroup]:
        """Build connected components from the similarity graph, including single-photo groups."""

        if not records:
            return []

        records_by_id = {record.id: record for record in records}
        adjacency: dict[str, set[str]] = defaultdict(set)
        weights: dict[tuple[str, str], float] = {}
        edge_types: dict[tuple[str, str], str] = {}

        for left_id, right_id, score, edge_type in edges:
            adjacency[left_id].add(right_id)
            adjacency[right_id].add(left_id)
            edge_key = tuple(sorted((left_id, right_id)))
            weights[edge_key] = score
            edge_types[edge_key] = edge_type

        visited: set[str] = set()
        groups: list[DuplicateGroup] = []

        total_records = len(records) or 1
        for index, record in enumerate(records, start=1):
            if record.id in visited:
                if progress_callback:
                    progress_callback(index, total_records, f"Reviewed graph node {index}/{len(records)}")
                continue

            stack = [record.id]
            component_ids: list[str] = []
            while stack:
                current = stack.pop()
                if current in visited:
                    continue
                visited.add(current)
                component_ids.append(current)
                stack.extend(neighbor for neighbor in adjacency[current] if neighbor not in visited)

            component_images = sorted((records_by_id[item_id] for item_id in component_ids), key=lambda item: item.capture_datetime)
            image_chunks = self._split_large_component(component_images)
            for images in image_chunks:
                chunk_ids = {image.id for image in images}
                similarities = [
                    score
                    for (left_id, right_id), score in weights.items()
                    if left_id in chunk_ids and right_id in chunk_ids
                ]
                component_edge_types = {
                    edge_types[(left_id, right_id)]
                    for (left_id, right_id), score in weights.items()
                    if left_id in chunk_ids and right_id in chunk_ids
                }
                self._append_group(groups, images, similarities, component_edge_types)
                if progress_callback:
                    progress_callback(index, total_records, f"Built {groups[-1].group_id} with {len(images)} images")

        LOGGER.info("Created %s photo groups", len(groups))
        return groups

    def _append_group(
        self,
        groups: list[DuplicateGroup],
        images: list[ImageRecord],
        similarities: list[float],
        component_edge_types: set[str],
    ) -> None:
        start_time = images[0].capture_datetime
        end_time = images[-1].capture_datetime
        span = int(abs((end_time - start_time).total_seconds()))
        group_id = f"G{len(groups) + 1:03d}"
        group_key = self._build_group_key(images)
        if len(images) == 1:
            group_type = "Single photo"
        elif component_edge_types == {"burst"}:
            group_type = "Burst candidate"
        else:
            group_type = "Near-duplicate"
        suggested_best = self._suggest_winner(images) if self.settings.auto_suggest_winner else None
        for image in images:
            image.group_id = group_id
        groups.append(
            DuplicateGroup(
                group_id=group_id,
                images=images,
                capture_span_seconds=span,
                avg_similarity=round(sum(similarities) / len(similarities), 4) if similarities else 1.0,
                group_type=group_type,
                group_key=group_key,
                suggested_best=suggested_best,
            )
        )

    def _split_large_component(self, images: list[ImageRecord]) -> list[list[ImageRecord]]:
        """Prevent transitive graph chains from becoming huge review groups."""

        if len(images) <= 1:
            return [images]

        max_images_per_group = 12
        max_span_seconds = max(90, self.settings.comparison_time_window_seconds * 3)
        max_gap_seconds = max(45, self.settings.burst_time_window_seconds * 3)

        chunks: list[list[ImageRecord]] = []
        current: list[ImageRecord] = []

        for image in images:
            if not current:
                current.append(image)
                continue

            previous = current[-1]
            gap = abs((image.capture_datetime - previous.capture_datetime).total_seconds())
            span_if_added = abs((image.capture_datetime - current[0].capture_datetime).total_seconds())
            should_split = (
                len(current) >= max_images_per_group
                or gap > max_gap_seconds
                or span_if_added > max_span_seconds
            )

            if should_split:
                chunks.append(current)
                current = [image]
            else:
                current.append(image)

        if current:
            chunks.append(current)
        return chunks

    def _suggest_winner(self, images: list[ImageRecord]) -> str:
        # Future AI-based winner recommendation can plug in here as a second-stage ranker.
        # This MVP deliberately stays local-only and deterministic.
        ranked = sorted(
            images,
            key=lambda image: (-image.sharpness_score, -image.file_size, image.capture_datetime),
        )
        return ranked[0].filename

    def _build_group_key(self, images: list[ImageRecord]) -> str:
        filenames = "|".join(sorted(image.filename for image in images))
        return sha1(filenames.encode("utf-8")).hexdigest()[:16]
