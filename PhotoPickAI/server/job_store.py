from __future__ import annotations

import json
import shutil
from pathlib import Path
from uuid import uuid4

from server.config import ServerConfig, load_config
from server.models import CreateJobRequest, JobResult, Progress


class JobStore:
    def __init__(self, config: ServerConfig | None = None) -> None:
        self.config = config or load_config()
        self.config.jobs_dir.mkdir(parents=True, exist_ok=True)

    def create_job(self, request: CreateJobRequest) -> str:
        job_id = request.job_id or str(uuid4())
        job_id = "".join(char for char in job_id if char.isalnum() or char in "-_")
        job_dir = self.job_dir(job_id)
        if job_dir.exists():
            shutil.rmtree(job_dir)
        (job_dir / "previews").mkdir(parents=True)
        self.write_json(job_id, "job.json", request.model_dump())
        self.write_json(job_id, "status.json", {"job_id": job_id, "status": "created", "progress": Progress().model_dump()})
        return job_id

    def job_dir(self, job_id: str) -> Path:
        return self.config.jobs_dir / job_id

    def previews_dir(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "previews"

    def read_json(self, job_id: str, name: str) -> dict:
        return json.loads((self.job_dir(job_id) / name).read_text(encoding="utf-8"))

    def write_json(self, job_id: str, name: str, payload: dict) -> None:
        target = self.job_dir(job_id) / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def set_status(self, job_id: str, status: str, progress: Progress | None = None) -> None:
        self.write_json(job_id, "status.json", {"job_id": job_id, "status": status, "progress": (progress or Progress()).model_dump()})

    def get_status(self, job_id: str) -> dict:
        return self.read_json(job_id, "status.json")

    def save_result(self, result: JobResult) -> None:
        self.write_json(result.job_id, "result.json", result.model_dump())
        self.set_status(result.job_id, result.status, Progress(stage=result.status))

    def get_result(self, job_id: str) -> dict:
        return self.read_json(job_id, "result.json")
