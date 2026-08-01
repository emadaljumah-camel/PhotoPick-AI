from __future__ import annotations

from pydantic import BaseModel, Field


class JobExif(BaseModel):
    iso: int | str | None = None
    shutter_speed: str | None = ""
    aperture: str | None = ""
    focal_length: str | None = ""
    camera: str | None = ""
    lens: str | None = ""


class JobPhoto(BaseModel):
    photo_id: str
    original_path: str = ""
    filename: str = ""
    capture_time: str = ""
    rating: int | None = 0
    color_label: str | None = ""
    current_pick_status: str | None = "unflagged"
    preview_filename: str
    exif: JobExif = Field(default_factory=JobExif)


class JobSettings(BaseModel):
    preview_long_edge: int = 1600
    auto_reject_non_selected: bool = True


class CreateJobRequest(BaseModel):
    client: str = "lightroom"
    plugin_version: str = "1.0.0"
    job_id: str | None = None
    settings: JobSettings = Field(default_factory=JobSettings)
    photos: list[JobPhoto]
    skipped: list[dict] = Field(default_factory=list)


class RunJobRequest(BaseModel):
    mode: str = "group_and_pick"
    model: dict | None = None


class Progress(BaseModel):
    stage: str = "created"
    processed_groups: int = 0
    total_groups: int = 0


class PhotoGroup(BaseModel):
    group_id: str
    photos: list[str]


class GroupDecision(BaseModel):
    group_id: str
    photos: list[str]
    representatives: list[str]
    rejected: list[str]
    reason: str = ""
    confidence: float = 0.0


class JobResult(BaseModel):
    job_id: str
    status: str
    groups: list[GroupDecision] = Field(default_factory=list)
    ungrouped: list[str] = Field(default_factory=list)
    skipped: list[dict] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
