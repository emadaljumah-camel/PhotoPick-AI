from __future__ import annotations

import base64
import io
import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass
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


class VisionLLMReviewer:
    """Use a local Vision LLM to recommend keepers."""

    SUBGROUP_RETRY_CHUNK_SIZE = 6

    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings

    def review_group(self, group: DuplicateGroup) -> VisionLLMReviewResult:
        """Ask the local Vision LLM to split a group, then select one keeper per subgroup."""

        if not group.images:
            return VisionLLMReviewResult([], "No images in group", [])

        try:
            selected_filenames, final_subgroups, raw_responses = self._review_image_set(
                group.images,
                context_name=group.group_id,
            )
            reason = (
                f"Vision LLM created {len(final_subgroups)} subgroup(s), "
                "then selected one representative from each subgroup."
            )
        except RuntimeError as exc:
            if not self._is_retryable_subgroup_failure(exc):
                raise

            if len(group.images) > 1:
                selected_filenames, final_subgroups, raw_responses = self._review_large_group_in_chunks(
                    group.images,
                    original_error=exc,
                )
                if self._is_context_window_failure(exc):
                    reason = (
                        "Full-group Vision request exceeded the model context window, so the app retried the group "
                        f"in smaller chunks of up to {self.SUBGROUP_RETRY_CHUNK_SIZE} photos and combined the chunk selections."
                    )
                else:
                    reason = (
                        "Full-group Vision subgrouping returned invalid JSON, so the app retried the group in smaller "
                        f"chunks of up to {self.SUBGROUP_RETRY_CHUNK_SIZE} photos and combined the chunk selections."
                    )
            else:
                selected_filenames, final_subgroups, raw_responses = self._single_image_fallback_subgroups(
                    group.images,
                    reason=f"Vision subgrouping failed: {exc}",
                )
                reason = (
                    "Vision subgrouping failed for this single-photo group, so the photo was kept "
                    "to avoid rejecting distinct images."
                )

        result = VisionLLMReviewResult(
            selected_filenames=selected_filenames,
            reason=reason,
            subgroups=final_subgroups,
            raw_response="\n\n---\n\n".join(raw_responses),
        )
        LOGGER.info("Vision LLM selected %s for %s", result.selected_filenames, group.group_id)
        return result

    def _review_image_set(
        self,
        images: list[ImageRecord],
        context_name: str,
    ) -> tuple[list[str], list[dict[str, Any]], list[str]]:
        """Run the two-step Vision flow for one image set."""

        filenames = [image.filename for image in images]
        image_inputs = [(Path(image.preview_path or image.path), f"{index}. {image.filename}") for index, image in enumerate(images, start=1)]

        subgroup_prompt = self._build_subgroup_prompt_for_images(images, context_name)
        subgroup_raw_response = self._post_json(self._build_payload(subgroup_prompt, image_inputs))
        subgroups = self._parse_subgroup_response(subgroup_raw_response, filenames)
        subgroups, pose_refinement_raw_responses = self._refine_pose_sensitive_subgroups(
            subgroups,
            images,
            filenames,
            context_name,
        )

        selected_filenames: list[str] = []
        final_subgroups: list[dict[str, Any]] = []
        raw_responses = [subgroup_raw_response, *pose_refinement_raw_responses]
        images_by_filename = {image.filename: image for image in images}

        for subgroup_index, subgroup in enumerate(subgroups, start=1):
            subgroup_filenames = [filename for filename in subgroup["images"] if filename in images_by_filename]
            if not subgroup_filenames:
                continue

            if len(subgroup_filenames) == 1:
                representative = subgroup_filenames[0]
                selection_reason = "Single-photo subgroup selected automatically."
            else:
                subgroup_images = [images_by_filename[filename] for filename in subgroup_filenames]
                selection_prompt = self._build_selection_prompt(subgroup["name"], subgroup_images)
                selection_inputs = [
                    (Path(image.preview_path or image.path), f"{index}. {image.filename}")
                    for index, image in enumerate(subgroup_images, start=1)
                ]
                try:
                    selection_raw_response = self._post_json(self._build_payload(selection_prompt, selection_inputs))
                    raw_responses.append(selection_raw_response)
                    representative, selection_reason = self._parse_selection_response(selection_raw_response, subgroup_filenames)
                except Exception as exc:
                    representative = self._best_component_representative(subgroup_images, "")
                    selection_reason = f"Selection fallback used because the Vision LLM selection call failed: {exc}"

            selected_filenames.append(representative)
            final_subgroups.append(
                {
                    "name": subgroup.get("name", f"Subgroup {subgroup_index}"),
                    "main_subject": subgroup.get("main_subject", ""),
                    "images": subgroup_filenames,
                    "representative": representative,
                    "reason": selection_reason,
                }
            )

        return selected_filenames, final_subgroups, raw_responses

    def _review_large_group_in_chunks(
        self,
        images: list[ImageRecord],
        original_error: RuntimeError,
    ) -> tuple[list[str], list[dict[str, Any]], list[str]]:
        """Retry failed large-group subgrouping in smaller Vision requests."""

        selected_filenames: list[str] = []
        final_subgroups: list[dict[str, Any]] = []
        raw_responses = [f"Full group subgrouping failed before chunk retry: {original_error}"]

        for chunk_index, chunk in enumerate(self._chunks(images, self.SUBGROUP_RETRY_CHUNK_SIZE), start=1):
            chunk_selected, chunk_subgroups, chunk_raw = self._review_chunk_or_split(
                chunk,
                label=f"Chunk {chunk_index}",
            )
            for filename in chunk_selected:
                if filename not in selected_filenames:
                    selected_filenames.append(filename)
            final_subgroups.extend(chunk_subgroups)
            raw_responses.extend(chunk_raw)

        return selected_filenames, final_subgroups, raw_responses

    def _review_chunk_or_split(
        self,
        images: list[ImageRecord],
        label: str,
    ) -> tuple[list[str], list[dict[str, Any]], list[str]]:
        """Review a chunk, splitting again if the local model context is still too small."""

        try:
            selected_filenames, subgroups, raw_responses = self._review_image_set(images, context_name=label)
            for subgroup in subgroups:
                subgroup["name"] = f"{label}: {subgroup.get('name', 'Subgroup')}"
            return selected_filenames, subgroups, raw_responses
        except RuntimeError as exc:
            if self._is_context_window_failure(exc) and len(images) > 1:
                midpoint = max(1, len(images) // 2)
                selected_filenames: list[str] = []
                subgroups: list[dict[str, Any]] = []
                raw_responses = [f"{label} exceeded model context and was split into smaller Vision requests: {exc}"]
                for split_index, split in enumerate((images[:midpoint], images[midpoint:]), start=1):
                    split_selected, split_subgroups, split_raw = self._review_chunk_or_split(
                        split,
                        label=f"{label}.{split_index}",
                    )
                    for filename in split_selected:
                        if filename not in selected_filenames:
                            selected_filenames.append(filename)
                    subgroups.extend(split_subgroups)
                    raw_responses.extend(split_raw)
                return selected_filenames, subgroups, raw_responses

            if self._is_retryable_subgroup_failure(exc):
                selected_filenames, subgroups, raw_responses = self._single_image_fallback_subgroups(
                    images,
                    reason=f"{label} Vision subgrouping failed, so each photo was kept separately: {exc}",
                )
                for subgroup in subgroups:
                    subgroup["name"] = f"{label}: {subgroup.get('name', 'Fallback distinct photo')}"
                return selected_filenames, subgroups, raw_responses

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
        return "invalid subgroup JSON" in str(exc)

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

        raw_subgroups = payload.get("subgroups", [])
        if not isinstance(raw_subgroups, list):
            raw_subgroups = []

        subgroups: list[dict[str, Any]] = []
        assigned: set[str] = set()
        for index, raw_subgroup in enumerate(raw_subgroups, start=1):
            if not isinstance(raw_subgroup, dict):
                continue
            images = raw_subgroup.get("images", [])
            if isinstance(images, str):
                images = [images]
            normalized_images = [
                filename
                for filename in images
                if filename in valid_filenames and filename not in assigned
            ]
            if not normalized_images:
                continue
            assigned.update(normalized_images)
            subgroups.append(
                {
                    "name": str(raw_subgroup.get("name", f"Subgroup {index}")),
                    "main_subject": str(raw_subgroup.get("main_subject", raw_subgroup.get("subject", ""))),
                    "images": normalized_images,
                    "reason": str(raw_subgroup.get("reason", "")),
                }
            )

        for filename in valid_filenames:
            if filename not in assigned:
                subgroups.append(
                    {
                        "name": "Unassigned distinct photo",
                        "images": [filename],
                        "reason": "The subgrouping response omitted this filename, so it was kept separately.",
                    }
                )

        if not subgroups and valid_filenames:
            subgroups = [
                {
                    "name": "Fallback subgroup",
                    "images": valid_filenames,
                    "reason": "Fallback because no valid subgroups were returned.",
                }
            ]
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
