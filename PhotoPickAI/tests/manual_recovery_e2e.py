from __future__ import annotations

import json
import multiprocessing
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from client_helper.lr_client_helper import write_json


def run_test_server(jobs_dir: str, vision_log: str, upload_log: str, port: int) -> None:
    import uvicorn

    import server.main as main
    from server.config import ServerConfig
    from server.job_store import JobStore
    from server.models import GroupDecision, PhotoGroup

    config = ServerConfig(jobs_dir=Path(jobs_dir))
    main.JobStore = lambda: JobStore(config)

    def build_records(job_dir, request):
        return [SimpleNamespace(id=photo.photo_id) for photo in request.photos], []

    def create_groups(records):
        ordered = sorted((record.id for record in records), key=lambda value: int(value.split("-")[1]))
        return [
            PhotoGroup(group_id=f"group-{index // 2 + 1}", photos=ordered[index : index + 2])
            for index in range(0, len(ordered), 2)
        ]

    def review(group, records):
        with Path(vision_log).open("a", encoding="utf-8") as handle:
            handle.write(group.group_id + "\n")
            handle.flush()
        time.sleep(2)
        return [
            GroupDecision(
                group_id=group.group_id,
                photos=group.photos,
                representatives=[group.photos[0]],
                rejected=group.photos[1:],
                reason="manual recovery test",
                confidence=1,
            )
        ]

    original_save_preview = JobStore.save_preview

    def logged_save_preview(self, job_id, filename, content, expected_sha256=None):
        with Path(upload_log).open("a", encoding="utf-8") as handle:
            handle.write(filename + "\n")
            handle.flush()
        return original_save_preview(self, job_id, filename, content, expected_sha256)

    JobStore.save_preview = logged_save_preview
    main.build_image_records = build_records
    main.create_review_groups = create_groups
    main.review_group_with_vision = review
    uvicorn.run(main.app, host="127.0.0.1", port=port, log_level="error")


def available_port() -> int:
    with socket.socket() as server_socket:
        server_socket.bind(("127.0.0.1", 0))
        return int(server_socket.getsockname()[1])


def wait_for_health(port: int, timeout: float = 15) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as response:
                if json.loads(response.read())["status"] == "ok":
                    return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError("Recovery test server did not become healthy")


def create_lightroom_run(root: Path, name: str, job_id: str, reverse: bool = False) -> Path:
    folder = root / name
    preview_folder = folder / "previews"
    preview_folder.mkdir(parents=True)
    photos = [
        {"photo_id": f"photo-{index}", "filename": f"photo-{index}.jpg", "preview_filename": f"photo-{index}.jpg"}
        for index in range(1, 7)
    ]
    for photo in photos:
        (preview_folder / photo["preview_filename"]).write_bytes(f"preview-content-{photo['photo_id']}".encode())
    write_json(
        folder / "job.json",
        {
            "client": "lightroom",
            "plugin_version": "1.0.0",
            "job_id": job_id,
            "settings": {"preview_long_edge": 1600, "auto_reject_non_selected": True},
            "photos": list(reversed(photos)) if reverse else photos,
            "skipped": [],
        },
    )
    return folder


def helper_command(folder: Path, port: int, action: str) -> list[str]:
    return [
        sys.executable,
        str(PROJECT_ROOT / "client_helper" / "lr_client_helper.py"),
        "--server-url",
        f"http://127.0.0.1:{port}",
        "--job-json",
        str(folder / "job.json"),
        "--preview-folder",
        str(folder / "previews"),
        "--result-json",
        str(folder / "result.json"),
        "--timeout",
        "2",
        "--action",
        action,
    ]


def line_count(path: Path) -> int:
    return len(path.read_text(encoding="utf-8").splitlines()) if path.exists() else 0


def main() -> int:
    multiprocessing.freeze_support()
    with tempfile.TemporaryDirectory(prefix="PhotoPickAI_Resume_E2E_") as temporary:
        root = Path(temporary)
        jobs_dir = root / "server_jobs"
        jobs_dir.mkdir()
        vision_log = root / "vision_calls.log"
        upload_log = root / "upload_calls.log"
        port = available_port()
        first_folder = create_lightroom_run(root, "run1", "manual-run-one")
        second_folder = create_lightroom_run(root, "run2", "manual-run-two", reverse=True)

        first_server = multiprocessing.Process(
            target=run_test_server,
            args=(str(jobs_dir), str(vision_log), str(upload_log), port),
        )
        first_server.start()
        first_helper: subprocess.Popen | None = None
        second_server: multiprocessing.Process | None = None
        try:
            wait_for_health(port)
            first_helper = subprocess.Popen(helper_command(first_folder, port, "submit"), cwd=PROJECT_ROOT)
            checkpoint = jobs_dir / "manual-run-one" / "checkpoint.json"
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline:
                checkpoint_ready = checkpoint.exists() and "group-1" in checkpoint.read_text(encoding="utf-8")
                group_two_started = vision_log.exists() and "group-2" in vision_log.read_text(encoding="utf-8")
                if checkpoint_ready and group_two_started:
                    break
                time.sleep(0.1)
            else:
                raise RuntimeError("Server did not reach the requested interruption point")

            uploads_before_restart = line_count(upload_log)
            first_server.terminate()
            first_server.join(10)
            first_helper.wait(10)

            second_server = multiprocessing.Process(
                target=run_test_server,
                args=(str(jobs_dir), str(vision_log), str(upload_log), port),
            )
            second_server.start()
            wait_for_health(port)
            submitted = subprocess.run(
                helper_command(second_folder, port, "submit"), cwd=PROJECT_ROOT, timeout=10, check=False
            )
            deadline = time.monotonic() + 40
            completed = submitted
            while submitted.returncode == 0 and time.monotonic() < deadline:
                time.sleep(0.2)
                completed = subprocess.run(
                    helper_command(second_folder, port, "check"), cwd=PROJECT_ROOT, timeout=10, check=False
                )
                if completed.returncode == 0:
                    current = json.loads((second_folder / "result.json").read_text(encoding="utf-8"))
                    if current.get("status") == "completed":
                        break

            result = json.loads((second_folder / "result.json").read_text(encoding="utf-8"))
            state = json.loads((second_folder / "job_state.json").read_text(encoding="utf-8"))
            vision_calls = vision_log.read_text(encoding="utf-8").splitlines()
            uploads_after_restart = line_count(upload_log)
            representatives = sorted(photo for group in result["groups"] for photo in group["representatives"])
            rejected = sorted(photo for group in result["groups"] for photo in group["rejected"])
            summary = {
                "helper_exit": completed.returncode,
                "resumed_job_id": state["job_id"],
                "uploads_before_restart": uploads_before_restart,
                "uploads_after_restart": uploads_after_restart,
                "uploads_skipped_on_resume": uploads_before_restart == uploads_after_restart,
                "vision_calls": vision_calls,
                "completed_group_not_repeated": vision_calls.count("group-1") == 1,
                "interrupted_group_repeat_count": vision_calls.count("group-2"),
                "remaining_group_count": vision_calls.count("group-3"),
                "result_status": result["status"],
                "representatives": representatives,
                "rejected": rejected,
            }
            expected_representatives = ["photo-1", "photo-3", "photo-5"]
            expected_rejected = ["photo-2", "photo-4", "photo-6"]
            assert completed.returncode == 0, summary
            assert state["job_id"] == "manual-run-one", summary
            assert uploads_before_restart == uploads_after_restart == 6, summary
            assert vision_calls.count("group-1") == 1, summary
            assert vision_calls.count("group-2") in {1, 2}, summary
            assert vision_calls.count("group-3") == 1, summary
            assert result["status"] == "completed", summary
            assert representatives == expected_representatives, summary
            assert rejected == expected_rejected, summary
            print(json.dumps(summary, indent=2))
            return 0
        finally:
            if first_helper is not None and first_helper.poll() is None:
                first_helper.terminate()
                first_helper.wait(10)
            if first_server.is_alive():
                first_server.terminate()
                first_server.join(10)
            if second_server is not None and second_server.is_alive():
                second_server.terminate()
                second_server.join(10)


if __name__ == "__main__":
    raise SystemExit(main())
