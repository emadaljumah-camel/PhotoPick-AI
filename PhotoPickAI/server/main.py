from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from threading import Lock

from fastapi import BackgroundTasks, Depends, FastAPI, File, HTTPException, UploadFile

from server.grouping_adapter import create_similarity_groups
from server.job_store import JobConflictError, JobStore, PreviewConflictError
from server.logging_config import configure_logging
from server.models import CreateJobRequest, JobResult, PhotoGroup, Progress, RunJobRequest
from server.photo_ingest import build_image_records
from server.result_schema import validate_result
from server.security import require_api_key
from server.vision_review_adapter import review_group_with_vision
from app.core.models import ImageRecord

configure_logging()
LOGGER = logging.getLogger(__name__)
app = FastAPI(
    title="PhotoPickAI Server",
    version="1.0.0",
    root_path="/photopick",
)
executor = ThreadPoolExecutor(max_workers=1)
_active_jobs: set[str] = set()
_active_jobs_lock = Lock()


@app.on_event("startup")
def recover_interrupted_jobs() -> None:
    interrupted = JobStore().mark_stale_jobs_interrupted()
    for job_id in interrupted:
        LOGGER.warning("Marked stale job %s as interrupted and resumable", job_id)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "PhotoPickAI", "version": "1.0.0"}


@app.post("/jobs", dependencies=[Depends(require_api_key)])
def create_job(request: CreateJobRequest) -> dict:
    store = JobStore()
    try:
        job_id, reused = store.create_job(request)
    except JobConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    status = store.get_status(job_id).get("status", "created")
    LOGGER.info("%s Lightroom job %s with %s photos", "Reused" if reused else "Created", job_id, len(request.photos))
    return {"job_id": job_id, "status": status, "reused": reused}


@app.post("/jobs/{job_id}/photos/{photo_id}/preview", dependencies=[Depends(require_api_key)])
async def upload_preview(job_id: str, photo_id: str, file: UploadFile = File(...)) -> dict:
    store = JobStore()
    if not store.job_dir(job_id).exists():
        raise HTTPException(status_code=404, detail="Unknown job")
    job = store.get_request(job_id)
    photo = next((item for item in job.photos if item.photo_id == photo_id), None)
    if photo is None:
        raise HTTPException(status_code=404, detail="Unknown photo_id for this job")
    try:
        target, reused = store.save_preview(job_id, photo.preview_filename, await file.read(), photo.preview_sha256)
    except PreviewConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    LOGGER.info("%s preview for job=%s photo=%s path=%s", "Reused" if reused else "Stored", job_id, photo_id, target)
    return {"job_id": job_id, "photo_id": photo_id, "status": "uploaded", "reused": reused}


@app.get("/jobs/{job_id}/uploads", dependencies=[Depends(require_api_key)])
def get_uploaded_photos(job_id: str) -> dict:
    store = JobStore()
    if not store.job_dir(job_id).exists():
        raise HTTPException(status_code=404, detail="Unknown job")
    uploaded = store.uploaded_photo_ids(job_id)
    return {
        "job_id": job_id,
        "uploaded_photo_ids": uploaded,
        "uploaded_preview_sha256": store.uploaded_preview_hashes(job_id),
    }


@app.post("/jobs/{job_id}/run", dependencies=[Depends(require_api_key)])
def run_job(job_id: str, request: RunJobRequest, background_tasks: BackgroundTasks) -> dict:
    store = JobStore()
    if not store.job_dir(job_id).exists():
        raise HTTPException(status_code=404, detail="Unknown job")
    result_path = store.job_dir(job_id) / "result.json"
    if result_path.exists():
        try:
            result = store.load_validated_result(job_id)
        except (OSError, TypeError, ValueError):
            store.set_status(job_id, "failed", Progress(stage="validation"), errors=["Stored result failed validation"], resumable=True)
        else:
            store.set_status(job_id, "completed", Progress(stage="completed"), errors=[], resumable=False)
            LOGGER.info("Returning existing completed result for job %s", job_id)
            return result.model_dump()

    with _active_jobs_lock:
        if job_id in _active_jobs:
            LOGGER.info("Job %s is already running; duplicate run request ignored", job_id)
            return {"job_id": job_id, "status": "running", "already_running": True}
        current_status = store.get_status(job_id)
        if current_status.get("status") == "running":
            LOGGER.info("Job %s is marked running; duplicate run request ignored", job_id)
            return {"job_id": job_id, "status": "running", "already_running": True}
        _active_jobs.add(job_id)
        try:
            store.set_status(job_id, "running", Progress(stage="queued"), errors=[])
        except Exception:
            _active_jobs.discard(job_id)
            raise

    background_tasks.add_task(submit_job, job_id)
    LOGGER.info("Queued resumable job %s mode=%s", job_id, request.mode)
    return {"job_id": job_id, "status": "running", "resumed": current_status.get("status") in {"failed", "interrupted"}}


def submit_job(job_id: str) -> None:
    try:
        executor.submit(process_job, job_id)
    except Exception as exc:
        LOGGER.exception("Could not submit job %s to the worker", job_id)
        with _active_jobs_lock:
            _active_jobs.discard(job_id)
        JobStore().set_status(job_id, "failed", Progress(stage="queued"), errors=[str(exc)], resumable=True)


@app.get("/jobs/{job_id}/status", dependencies=[Depends(require_api_key)])
def get_status(job_id: str) -> dict:
    return JobStore().get_status(job_id)


@app.get("/jobs/{job_id}/result", dependencies=[Depends(require_api_key)])
def get_result(job_id: str) -> dict:
    store = JobStore()
    result_path = store.job_dir(job_id) / "result.json"
    if not result_path.exists():
        return {"job_id": job_id, "status": store.get_status(job_id).get("status", "running"), "groups": [], "ungrouped": [], "skipped": [], "errors": []}
    try:
        return store.load_validated_result(job_id).model_dump()
    except (OSError, TypeError, ValueError) as exc:
        store.set_status(job_id, "failed", Progress(stage="validation"), errors=[str(exc)], resumable=True)
        return {"job_id": job_id, "status": "failed", "groups": [], "ungrouped": [], "skipped": [], "errors": [str(exc)]}


def process_job(job_id: str) -> None:
    store = JobStore()
    try:
        request = store.get_request(job_id)
        job_dir = store.job_dir(job_id)
        saved_groups = store.load_groups(job_id)
        if saved_groups is None:
            store.set_status(job_id, "running", Progress(stage="grouping"), errors=[])
        records, skipped = build_image_records(job_dir, request)
        if saved_groups is None:
            groups = create_review_groups(records)
            store.save_groups(job_id, groups, skipped)
            LOGGER.info("Saved grouping checkpoint for job=%s groups=%s", job_id, len(groups))
        else:
            groups, skipped = saved_groups
            LOGGER.info("Resumed job %s using persisted grouping with %s groups", job_id, len(groups))

        completed = store.load_checkpoint(job_id, groups)
        completed_count = sum(1 for group in groups if group.group_id in completed)
        remaining_count = len(groups) - completed_count
        LOGGER.info(
            "Resumed job ID %s: loaded %s completed groups; %s groups remaining",
            job_id,
            completed_count,
            remaining_count,
        )
        for group in groups:
            if group.group_id in completed:
                continue
            LOGGER.info("Processing job=%s current group=%s", job_id, group.group_id)
            store.set_status(
                job_id,
                "running",
                Progress(
                    stage="vision_review",
                    processed_groups=completed_count,
                    total_groups=len(groups),
                    current_group_id=group.group_id,
                ),
                errors=[],
            )
            decisions = review_group_with_vision(group, records)
            store.save_group_checkpoint(job_id, group, decisions)
            completed[group.group_id] = decisions
            completed_count += 1
            LOGGER.info("Checkpoint save confirmed job=%s group=%s", job_id, group.group_id)
            store.set_status(
                job_id,
                "running",
                Progress(stage="vision_review", processed_groups=completed_count, total_groups=len(groups)),
                errors=[],
            )

        all_decisions = [decision for group in groups for decision in completed.get(group.group_id, [])]
        selected_ids = {photo.photo_id for photo in request.photos}
        result = validate_result(
            JobResult(job_id=job_id, status="completed", groups=all_decisions, skipped=skipped, errors=[]),
            selected_ids,
        )
        if result.status != "completed" or result.errors:
            store.set_status(
                job_id,
                "failed",
                Progress(stage="validation", processed_groups=completed_count, total_groups=len(groups)),
                errors=result.errors,
                resumable=True,
            )
            LOGGER.error("Validation failed for job %s: %s", job_id, result.errors)
            return
        store.save_result(result)
        LOGGER.info("Completed job %s status=%s groups=%s errors=%s", job_id, result.status, len(result.groups), len(result.errors))
    except Exception as exc:
        LOGGER.exception("Job %s failed", job_id)
        try:
            progress = Progress.model_validate(store.get_status(job_id).get("progress", {}))
        except (OSError, ValueError):
            progress = Progress(stage="failed")
        store.set_status(job_id, "failed", progress, errors=[str(exc)], resumable=True)
    finally:
        with _active_jobs_lock:
            _active_jobs.discard(job_id)


def create_review_groups(records: list[ImageRecord]) -> list[PhotoGroup]:
    return create_similarity_groups(records)


if __name__ == "__main__":
    import uvicorn

    from server.config import load_config

    config = load_config()
    uvicorn.run("server.main:app", host=config.host, port=config.port, reload=False)
