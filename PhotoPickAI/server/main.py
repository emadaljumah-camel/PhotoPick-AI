from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import BackgroundTasks, Depends, FastAPI, File, HTTPException, UploadFile

from server.grouping_adapter import create_similarity_groups
from server.job_store import JobStore
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


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "PhotoPickAI", "version": "1.0.0"}


@app.post("/jobs", dependencies=[Depends(require_api_key)])
def create_job(request: CreateJobRequest) -> dict:
    store = JobStore()
    job_id = store.create_job(request)
    LOGGER.info("Created Lightroom job %s with %s photos", job_id, len(request.photos))
    return {"job_id": job_id, "status": "created"}


@app.post("/jobs/{job_id}/photos/{photo_id}/preview", dependencies=[Depends(require_api_key)])
async def upload_preview(job_id: str, photo_id: str, file: UploadFile = File(...)) -> dict:
    store = JobStore()
    job = CreateJobRequest.model_validate(store.read_json(job_id, "job.json"))
    photo = next((item for item in job.photos if item.photo_id == photo_id), None)
    if photo is None:
        raise HTTPException(status_code=404, detail="Unknown photo_id for this job")
    target = store.previews_dir(job_id) / photo.preview_filename
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(await file.read())
    LOGGER.info("Uploaded preview for job=%s photo=%s path=%s", job_id, photo_id, target)
    return {"job_id": job_id, "photo_id": photo_id, "status": "uploaded"}


@app.post("/jobs/{job_id}/run", dependencies=[Depends(require_api_key)])
def run_job(job_id: str, request: RunJobRequest, background_tasks: BackgroundTasks) -> dict:
    store = JobStore()
    if not store.job_dir(job_id).exists():
        raise HTTPException(status_code=404, detail="Unknown job")
    store.set_status(job_id, "running", Progress(stage="queued"))
    background_tasks.add_task(lambda: executor.submit(process_job, job_id))
    LOGGER.info("Queued job %s mode=%s", job_id, request.mode)
    return {"job_id": job_id, "status": "running"}


@app.get("/jobs/{job_id}/status", dependencies=[Depends(require_api_key)])
def get_status(job_id: str) -> dict:
    return JobStore().get_status(job_id)


@app.get("/jobs/{job_id}/result", dependencies=[Depends(require_api_key)])
def get_result(job_id: str) -> dict:
    store = JobStore()
    result_path = store.job_dir(job_id) / "result.json"
    if not result_path.exists():
        return {"job_id": job_id, "status": store.get_status(job_id).get("status", "running"), "groups": [], "ungrouped": [], "skipped": [], "errors": []}
    return store.get_result(job_id)


def process_job(job_id: str) -> None:
    store = JobStore()
    try:
        request = CreateJobRequest.model_validate(store.read_json(job_id, "job.json"))
        job_dir = store.job_dir(job_id)
        records, skipped = build_image_records(job_dir, request)
        store.set_status(job_id, "running", Progress(stage="grouping"))
        groups = create_review_groups(records)
        all_decisions = []
        for index, group in enumerate(groups, start=1):
            store.set_status(job_id, "running", Progress(stage="vision_review", processed_groups=index - 1, total_groups=len(groups)))
            all_decisions.extend(review_group_with_vision(group, records))
        selected_ids = {photo.photo_id for photo in request.photos}
        result = validate_result(
            JobResult(job_id=job_id, status="completed", groups=all_decisions, skipped=skipped, errors=[]),
            selected_ids,
        )
        store.save_result(result)
        LOGGER.info("Completed job %s status=%s groups=%s errors=%s", job_id, result.status, len(result.groups), len(result.errors))
    except Exception as exc:
        LOGGER.exception("Job %s failed", job_id)
        store.save_result(JobResult(job_id=job_id, status="failed", errors=[str(exc)]))


def create_review_groups(records: list[ImageRecord]) -> list[PhotoGroup]:
    return create_similarity_groups(records)


if __name__ == "__main__":
    import uvicorn

    from server.config import load_config

    config = load_config()
    uvicorn.run("server.main:app", host=config.host, port=config.port, reload=False)
