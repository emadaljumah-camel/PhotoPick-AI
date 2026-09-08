from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
from threading import Lock
from typing import Any
from uuid import uuid4

from server.config import ServerConfig, load_config
from server.models import CreateJobRequest, GroupDecision, JobResult, PhotoGroup, Progress
from server.result_schema import group_decision_errors, validate_result


CHECKPOINT_SCHEMA_VERSION = 1
LOGGER = logging.getLogger(__name__)
_JOB_CREATION_LOCK = Lock()


class JobConflictError(Exception):
    """Raised when a job ID is reused for a different immutable request."""


class PreviewConflictError(Exception):
    """Raised when changed preview content would invalidate derived job data."""


def utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


class JobStore:
    def __init__(self, config: ServerConfig | None = None) -> None:
        self.config = config or load_config()
        self.config.jobs_dir.mkdir(parents=True, exist_ok=True)

    def create_job(self, request: CreateJobRequest) -> tuple[str, bool]:
        with _JOB_CREATION_LOCK:
            return self._create_job(request)

    def _create_job(self, request: CreateJobRequest) -> tuple[str, bool]:
        job_id = request.job_id or str(uuid4())
        job_id = "".join(char for char in job_id if char.isalnum() or char in "-_")
        if not job_id:
            job_id = str(uuid4())
        job_dir = self.job_dir(job_id)
        fingerprint = self.request_fingerprint(request)
        if job_dir.exists():
            try:
                existing = self.read_json(job_id, "job.json")
                existing_request = CreateJobRequest.model_validate(existing)
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                raise JobConflictError(f"Job ID {job_id!r} already has a non-reusable job directory") from exc
            fingerprint_version = existing.get("_request_fingerprint_version")
            existing_fingerprint = (
                existing.get("_request_fingerprint")
                if fingerprint_version == 2
                else self.request_fingerprint(existing_request)
            )
            legacy_compatible = fingerprint_version != 2 and (
                self.request_fingerprint(existing_request, include_preview_hashes=False)
                == self.request_fingerprint(request, include_preview_hashes=False)
            )
            if existing_fingerprint != fingerprint and not legacy_compatible:
                raise JobConflictError(f"Job ID {job_id!r} already exists with different input")
            return job_id, True
        (job_dir / "previews").mkdir(parents=True)
        created_at = utc_now()
        job_payload = request.model_dump()
        job_payload["_request_fingerprint"] = fingerprint
        job_payload["_request_fingerprint_version"] = 2
        job_payload["_created_at"] = created_at
        self.write_json(job_id, "job.json", job_payload)
        self.write_json(
            job_id,
            "status.json",
            {
                "job_id": job_id,
                "status": "created",
                "progress": Progress().model_dump(),
                "errors": [],
                "resumable": True,
                "created_at": created_at,
                "updated_at": created_at,
                "started_at": None,
                "completed_at": None,
            },
        )
        return job_id, False

    @staticmethod
    def request_fingerprint(request: CreateJobRequest, include_preview_hashes: bool = True) -> str:
        payload = request.model_dump()
        payload.pop("job_id", None)
        payload.pop("skipped", None)
        if not include_preview_hashes:
            for photo in payload.get("photos", []):
                photo.pop("preview_sha256", None)
        payload["photos"] = sorted(
            payload.get("photos", []),
            key=lambda item: (str(item.get("photo_id", "")), json.dumps(item, sort_keys=True, separators=(",", ":"))),
        )
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def job_dir(self, job_id: str) -> Path:
        return self.config.jobs_dir / job_id

    def previews_dir(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "previews"

    def read_json(self, job_id: str, name: str) -> dict:
        return json.loads((self.job_dir(job_id) / name).read_text(encoding="utf-8"))

    def write_json(self, job_id: str, name: str, payload: dict) -> None:
        target = self.job_dir(job_id) / name
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, indent=2, ensure_ascii=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

    def set_status(
        self,
        job_id: str,
        status: str,
        progress: Progress | None = None,
        errors: list[str] | None = None,
        resumable: bool | None = None,
    ) -> None:
        path = self.job_dir(job_id) / "status.json"
        previous = self.read_json(job_id, "status.json") if path.exists() else {}
        now = utc_now()
        payload: dict[str, Any] = {
            "job_id": job_id,
            "status": status,
            "progress": (progress or Progress()).model_dump(),
            "errors": list(previous.get("errors", [])) if errors is None else errors,
            "resumable": (status != "completed") if resumable is None else resumable,
            "created_at": previous.get("created_at", now),
            "updated_at": now,
            "started_at": previous.get("started_at"),
            "completed_at": previous.get("completed_at"),
        }
        if status == "running" and not payload["started_at"]:
            payload["started_at"] = now
        if status == "completed":
            payload["completed_at"] = now
        self.write_json(job_id, "status.json", payload)

    def get_status(self, job_id: str) -> dict:
        return self.read_json(job_id, "status.json")

    def save_result(self, result: JobResult) -> None:
        validated = self.validate_completed_result(result)
        self.write_json(validated.job_id, "result.json", validated.model_dump())
        self.set_status(
            validated.job_id,
            validated.status,
            self._completed_progress(validated.job_id),
            errors=[],
            resumable=False,
        )

    def _completed_progress(self, job_id: str) -> Progress:
        saved_groups = self.load_groups(job_id)
        if saved_groups is not None:
            total_groups = len(saved_groups[0])
        else:
            previous = Progress.model_validate(
                self.get_status(job_id).get("progress", {})
            )
            total_groups = max(
                previous.processed_groups,
                previous.total_groups,
            )
        return Progress(
            stage="completed",
            processed_groups=total_groups,
            total_groups=total_groups,
            current_group_id=None,
        )

    def get_result(self, job_id: str) -> dict:
        return self.read_json(job_id, "result.json")

    def load_validated_result(self, job_id: str) -> JobResult:
        result = JobResult.model_validate(self.get_result(job_id))
        return self.validate_completed_result(result)

    def validate_completed_result(self, result: JobResult) -> JobResult:
        request = self.get_request(result.job_id)
        validated = validate_result(result, {photo.photo_id for photo in request.photos})
        if result.status != "completed" or validated.status != "completed" or validated.errors:
            raise ValueError("Stored result is not a valid completed result: " + "; ".join(validated.errors or [result.status]))

        saved_groups = self.load_groups(result.job_id)
        if saved_groups is not None:
            groups, _ = saved_groups
            checkpoint = self.load_checkpoint(result.job_id, groups)
            if len(checkpoint) != len(groups):
                raise ValueError("Stored result does not have a valid checkpoint for every review group")
            expected = [decision.model_dump() for group in groups for decision in checkpoint[group.group_id]]
            actual = [decision.model_dump() for decision in validated.groups]
            if actual != expected:
                raise ValueError("Stored result does not match the durable group checkpoints")
        return validated

    def get_request(self, job_id: str) -> CreateJobRequest:
        return CreateJobRequest.model_validate(self.read_json(job_id, "job.json"))

    def uploaded_photo_ids(self, job_id: str) -> list[str]:
        request = self.get_request(job_id)
        uploaded: list[str] = []
        for photo in request.photos:
            path = self.previews_dir(job_id) / photo.preview_filename
            if not path.is_file() or path.stat().st_size <= 0:
                continue
            if photo.preview_sha256 and hashlib.sha256(path.read_bytes()).hexdigest() != photo.preview_sha256:
                continue
            uploaded.append(photo.photo_id)
        return uploaded

    def uploaded_preview_hashes(self, job_id: str) -> dict[str, str]:
        request = self.get_request(job_id)
        hashes: dict[str, str] = {}
        for photo in request.photos:
            path = self.previews_dir(job_id) / photo.preview_filename
            if path.is_file() and path.stat().st_size > 0:
                hashes[photo.photo_id] = hashlib.sha256(path.read_bytes()).hexdigest()
        return hashes

    def save_preview(
        self,
        job_id: str,
        filename: str,
        content: bytes,
        expected_sha256: str | None = None,
    ) -> tuple[Path, bool]:
        preview_root = self.previews_dir(job_id).resolve()
        target = (preview_root / filename).resolve()
        if target.parent != preview_root:
            raise ValueError("Preview filename must not contain a directory")
        digest = hashlib.sha256(content).digest()
        if expected_sha256 and digest.hex() != expected_sha256:
            raise ValueError("Uploaded preview does not match its immutable SHA-256 hash")
        if target.exists() and hashlib.sha256(target.read_bytes()).digest() == digest:
            return target, True
        derived_files = ("groups.json", "checkpoint.json", "result.json")
        status_path = self.job_dir(job_id) / "status.json"
        status = self.read_json(job_id, "status.json").get("status") if status_path.exists() else "created"
        if target.exists() and (
            status != "created" or any((self.job_dir(job_id) / name).exists() for name in derived_files)
        ):
            raise PreviewConflictError("Preview content cannot change after processing has started; create a new job")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return target, False

    def save_groups(self, job_id: str, groups: list[PhotoGroup], skipped: list[dict]) -> None:
        now = utc_now()
        self.write_json(
            job_id,
            "groups.json",
            {
                "schema_version": CHECKPOINT_SCHEMA_VERSION,
                "job_id": job_id,
                "groups": [group.model_dump() for group in groups],
                "skipped": skipped,
                "created_at": now,
                "updated_at": now,
            },
        )

    def load_groups(self, job_id: str) -> tuple[list[PhotoGroup], list[dict]] | None:
        path = self.job_dir(job_id) / "groups.json"
        if not path.exists():
            return None
        payload = self.read_json(job_id, "groups.json")
        return [PhotoGroup.model_validate(item) for item in payload.get("groups", [])], list(payload.get("skipped", []))

    def load_semantic_diagnostics(self, job_id: str) -> dict[str, Any]:
        path = self.job_dir(job_id) / "semantic_diagnostics.json"
        if not path.exists():
            return {}
        payload = self.read_json(job_id, "semantic_diagnostics.json")
        groups = payload.get("broad_groups", {})
        return groups if isinstance(groups, dict) else {}

    def save_semantic_diagnostic(
        self,
        job_id: str,
        broad_group_id: str,
        diagnostic: dict[str, Any],
    ) -> None:
        path = self.job_dir(job_id) / "semantic_diagnostics.json"
        try:
            payload = self.read_json(job_id, "semantic_diagnostics.json") if path.exists() else {}
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            payload = {}
        now = utc_now()
        if not isinstance(payload.get("broad_groups"), dict):
            payload = {
                "schema_version": 1,
                "job_id": job_id,
                "broad_groups": {},
                "created_at": now,
            }
        payload["broad_groups"][broad_group_id] = diagnostic
        payload["updated_at"] = now
        self.write_json(job_id, "semantic_diagnostics.json", payload)

    def load_checkpoint(
        self,
        job_id: str,
        groups: list[PhotoGroup] | None = None,
    ) -> dict[str, list[GroupDecision]]:
        path = self.job_dir(job_id) / "checkpoint.json"
        if not path.exists():
            return {}
        try:
            payload = self.read_json(job_id, "checkpoint.json")
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            LOGGER.warning("Ignoring malformed checkpoint file job=%s: %s", job_id, exc)
            return {}
        raw_completed = payload.get("completed_groups")
        if payload.get("schema_version") != CHECKPOINT_SCHEMA_VERSION or not isinstance(raw_completed, dict):
            LOGGER.warning("Ignoring malformed checkpoint structure job=%s", job_id)
            return {}
        groups_by_id = {group.group_id: group for group in groups or []}
        completed: dict[str, list[GroupDecision]] = {}
        for group_id, entry in raw_completed.items():
            try:
                decisions = [GroupDecision.model_validate(item) for item in entry.get("decisions", [])]
                source_group = groups_by_id.get(group_id)
                if source_group is not None:
                    errors = group_decision_errors(source_group, decisions)
                    if errors:
                        raise ValueError("; ".join(errors))
                elif not decisions:
                    raise ValueError("checkpoint entry has no decisions")
            except (AttributeError, TypeError, ValueError) as exc:
                LOGGER.warning("Ignoring invalid checkpoint entry job=%s group=%s: %s", job_id, group_id, exc)
                continue
            completed[group_id] = decisions
        return completed

    def save_group_checkpoint(
        self,
        job_id: str,
        group: PhotoGroup,
        decisions: list[GroupDecision],
    ) -> None:
        errors = group_decision_errors(group, decisions)
        if errors:
            raise ValueError("Cannot checkpoint invalid group decisions: " + "; ".join(errors))
        path = self.job_dir(job_id) / "checkpoint.json"
        try:
            payload = self.read_json(job_id, "checkpoint.json") if path.exists() else {}
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            payload = {}
        if payload.get("schema_version") != CHECKPOINT_SCHEMA_VERSION or not isinstance(payload.get("completed_groups"), dict):
            payload = {
                "schema_version": CHECKPOINT_SCHEMA_VERSION,
                "job_id": job_id,
                "completed_groups": {},
                "created_at": utc_now(),
            }
        payload.setdefault("completed_groups", {})[group.group_id] = {
            "decisions": [decision.model_dump() for decision in decisions],
            "completed_at": utc_now(),
        }
        payload["updated_at"] = utc_now()
        self.write_json(job_id, "checkpoint.json", payload)

    def mark_stale_jobs_interrupted(self) -> list[str]:
        interrupted: list[str] = []
        for job_dir in self.config.jobs_dir.iterdir():
            status_path = job_dir / "status.json"
            if not job_dir.is_dir() or not status_path.exists():
                continue
            try:
                status = json.loads(status_path.read_text(encoding="utf-8"))
                stage = status.get("progress", {}).get("stage", "")
                state = status.get("status")
                result_path = job_dir / "result.json"
                if state == "completed":
                    try:
                        self.load_validated_result(job_dir.name)
                    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
                        self.set_status(
                            job_dir.name,
                            "failed",
                            Progress(stage="validation"),
                            errors=[f"Stored completed result failed validation: {exc}"],
                            resumable=True,
                        )
                    continue
                legacy_active_stage = not state and stage in {"grouping", "vision_review"}
                if state == "running" or state in {"grouping", "vision_review"} or legacy_active_stage:
                    if result_path.exists():
                        try:
                            self.load_validated_result(job_dir.name)
                        except (OSError, TypeError, ValueError, json.JSONDecodeError):
                            pass
                        else:
                            self.set_status(
                                job_dir.name,
                                "completed",
                                self._completed_progress(job_dir.name),
                                errors=[],
                                resumable=False,
                            )
                            continue
                    self.set_status(
                        job_dir.name,
                        "interrupted",
                        Progress.model_validate(status.get("progress", {})),
                        errors=["Server stopped while this job was running; it can be resumed."],
                        resumable=True,
                    )
                    interrupted.append(job_dir.name)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
        return interrupted
