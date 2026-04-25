from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class ImageRecord:
    """Metadata and analysis results for one image."""

    id: str
    filename: str
    path: str
    file_size: int
    width: int
    height: int
    orientation: str
    capture_time: str
    preview_path: str = ""
    phash: str = ""
    sharpness_score: float = 0.0
    similarity_vector: list[float] = field(default_factory=list)
    focus_vector: list[float] = field(default_factory=list)
    edge_vector: list[float] = field(default_factory=list)
    modified_time: str = ""
    group_id: str | None = None

    @property
    def capture_datetime(self) -> datetime:
        """Return parsed capture time, or minimum datetime for invalid data."""

        try:
            return datetime.fromisoformat(self.capture_time)
        except ValueError:
            return datetime.min

    @property
    def aspect_ratio(self) -> float:
        """Return width/height with a safe fallback."""

        return self.width / self.height if self.height else 0.0

    @property
    def path_obj(self) -> Path:
        """Return the image path as a Path instance."""

        return Path(self.path)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-safe dictionary."""

        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ImageRecord":
        """Construct an image record from persisted data."""

        return cls(**data)


@dataclass(slots=True)
class DuplicateGroup:
    """Connected group of near-duplicate images."""

    group_id: str
    images: list[ImageRecord]
    capture_span_seconds: int
    avg_similarity: float
    group_type: str = "Near-duplicate"
    group_key: str = ""
    suggested_best: str | None = None
    user_selected: list[str] = field(default_factory=list)
    local_ai_selected: list[str] = field(default_factory=list)
    local_ai_reason: str = ""
    vision_llm_selected: list[str] = field(default_factory=list)
    vision_llm_reason: str = ""
    vision_llm_subgroups: list[dict[str, Any]] = field(default_factory=list)
    status: str = "Pending"

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-safe dictionary."""

        return {
            "group_id": self.group_id,
            "images": [image.to_dict() for image in self.images],
            "capture_span_seconds": self.capture_span_seconds,
            "avg_similarity": self.avg_similarity,
            "group_type": self.group_type,
            "group_key": self.group_key,
            "suggested_best": self.suggested_best,
            "user_selected": self.user_selected,
            "local_ai_selected": self.local_ai_selected,
            "local_ai_reason": self.local_ai_reason,
            "vision_llm_selected": self.vision_llm_selected,
            "vision_llm_reason": self.vision_llm_reason,
            "vision_llm_subgroups": self.vision_llm_subgroups,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DuplicateGroup":
        """Construct a group from persisted data."""

        images = [ImageRecord.from_dict(item) for item in data.get("images", [])]
        return cls(
            group_id=data["group_id"],
            images=images,
            capture_span_seconds=data.get("capture_span_seconds", 0),
            avg_similarity=data.get("avg_similarity", 0.0),
            group_type=data.get("group_type", "Near-duplicate"),
            group_key=data.get("group_key", ""),
            suggested_best=data.get("suggested_best"),
            user_selected=list(data.get("user_selected", [])),
            local_ai_selected=list(data.get("local_ai_selected", [])),
            local_ai_reason=data.get("local_ai_reason", ""),
            vision_llm_selected=list(data.get("vision_llm_selected", [])),
            vision_llm_reason=data.get("vision_llm_reason", ""),
            vision_llm_subgroups=list(data.get("vision_llm_subgroups", [])),
            status=data.get("status", "Pending"),
        )


@dataclass(slots=True)
class ReviewDecision:
    """Review output for one duplicate group."""

    group_id: str
    winner: str | None
    alternates: list[str]
    rejected: list[str]
    reviewed: bool
    group_key: str = ""
    local_ai_selected: list[str] = field(default_factory=list)
    local_ai_reason: str = ""
    vision_llm_selected: list[str] = field(default_factory=list)
    vision_llm_reason: str = ""
    vision_llm_subgroups: list[dict[str, Any]] = field(default_factory=list)
    status: str = "Pending"

    def to_dict(self) -> dict[str, Any]:
        """Serialize to JSON-safe data."""

        return asdict(self)

    @classmethod
    def from_group(cls, group: DuplicateGroup) -> "ReviewDecision":
        """Create a default review decision from the current group state."""

        selected = list(group.user_selected)
        winner = selected[0] if selected else None
        alternates = selected[1:] if len(selected) > 1 else []
        rejected = [image.filename for image in group.images if image.filename not in selected]
        reviewed = group.status == "Reviewed"
        return cls(
            group_id=group.group_id,
            winner=winner,
            alternates=alternates,
            rejected=rejected,
            reviewed=reviewed,
            group_key=group.group_key,
            local_ai_selected=group.local_ai_selected,
            local_ai_reason=group.local_ai_reason,
            vision_llm_selected=group.vision_llm_selected,
            vision_llm_reason=group.vision_llm_reason,
            vision_llm_subgroups=group.vision_llm_subgroups,
            status=group.status,
        )
