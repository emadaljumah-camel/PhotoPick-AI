from __future__ import annotations

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError
from unittest.mock import patch

from fastapi import HTTPException

from client_helper.lr_client_helper import add_preview_hashes, http_error_message, load_resume_state, resume_fingerprint, save_resume_state
from server.config import ServerConfig
from server.job_store import JobConflictError, JobStore, PreviewConflictError
from server.main import _active_jobs, _active_jobs_lock, create_job as api_create_job, get_result as api_get_result, process_job, run_job, submit_job
from server.models import CreateJobRequest, GroupDecision, JobPhoto, JobResult, PhotoGroup, Progress, RunJobRequest
from server.result_schema import validate_result


class CapturingBackgroundTasks:
    def __init__(self) -> None:
        self.tasks: list = []

    def add_task(self, function, *args, **kwargs) -> None:
        self.tasks.append((function, args, kwargs))


class ResumeJobTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.jobs_dir = Path(self.temporary.name) / "jobs"
        self.config = ServerConfig(jobs_dir=self.jobs_dir)
        self.store = JobStore(self.config)

    def tearDown(self) -> None:
        with _active_jobs_lock:
            _active_jobs.clear()
        self.temporary.cleanup()

    @staticmethod
    def request(job_id: str = "resume-job", count: int = 3) -> CreateJobRequest:
        return CreateJobRequest(
            job_id=job_id,
            photos=[
                JobPhoto(photo_id=f"photo-{index}", filename=f"photo-{index}.jpg", preview_filename=f"photo-{index}.jpg")
                for index in range(1, count + 1)
            ],
        )

    @staticmethod
    def groups(count: int = 3) -> list[PhotoGroup]:
        return [PhotoGroup(group_id=f"group-{index}", photos=[f"photo-{index}"]) for index in range(1, count + 1)]

    @staticmethod
    def decision(group: PhotoGroup) -> list[GroupDecision]:
        photo_id = group.photos[0]
        return [
            GroupDecision(
                group_id=group.group_id,
                photos=[photo_id],
                representatives=[photo_id],
                rejected=[],
                reason="test",
                confidence=1.0,
            )
        ]

    def create(self, request: CreateJobRequest | None = None) -> str:
        job_id, reused = self.store.create_job(request or self.request())
        self.assertFalse(reused)
        return job_id

    def run_process_with(self, job_id: str, groups: list[PhotoGroup], reviewer) -> None:
        with (
            patch("server.main.JobStore", lambda: JobStore(self.config)),
            patch("server.main.build_image_records", return_value=([object()], [])),
            patch("server.main.create_review_groups", return_value=groups),
            patch("server.main.review_group_with_vision", side_effect=reviewer),
        ):
            process_job(job_id)

    def test_preview_upload_interruption_resumes_without_recreating_job(self) -> None:
        request = self.request(count=2)
        job_id = self.create(request)
        marker = self.store.job_dir(job_id) / "keep-me.txt"
        marker.write_text("preserved", encoding="utf-8")
        first_path, first_reused = self.store.save_preview(job_id, "photo-1.jpg", b"first-preview")

        reused_id, reused = self.store.create_job(request)
        self.assertEqual(reused_id, job_id)
        self.assertTrue(reused)
        self.assertTrue(marker.exists())
        self.assertEqual(self.store.uploaded_photo_ids(job_id), ["photo-1"])

        repeated_path, repeated = self.store.save_preview(job_id, "photo-1.jpg", b"first-preview")
        self.assertEqual(repeated_path, first_path)
        self.assertTrue(repeated)
        self.store.save_preview(job_id, "photo-2.jpg", b"second-preview")
        self.assertEqual(self.store.uploaded_photo_ids(job_id), ["photo-1", "photo-2"])

    def test_idempotent_creation_preserves_all_existing_artifacts_byte_for_byte(self) -> None:
        request = self.request(count=1)
        job_id = self.create(request)
        group = self.groups(count=1)[0]
        self.store.save_preview(job_id, "photo-1.jpg", b"preview")
        self.store.save_groups(job_id, [group], [])
        self.store.save_group_checkpoint(job_id, group, self.decision(group))
        self.store.save_result(JobResult(job_id=job_id, status="completed", groups=self.decision(group)))
        artifact_names = ["job.json", "status.json", "groups.json", "checkpoint.json", "result.json"]
        before = {name: (self.store.job_dir(job_id) / name).read_bytes() for name in artifact_names}
        preview_before = (self.store.previews_dir(job_id) / "photo-1.jpg").read_bytes()

        reused_id, reused = self.store.create_job(request)

        self.assertEqual(reused_id, job_id)
        self.assertTrue(reused)
        self.assertEqual(before, {name: (self.store.job_dir(job_id) / name).read_bytes() for name in artifact_names})
        self.assertEqual(preview_before, (self.store.previews_dir(job_id) / "photo-1.jpg").read_bytes())

    def test_server_fingerprint_is_order_independent_and_processing_sensitive(self) -> None:
        first = self.request(count=3)
        reordered = first.model_copy(deep=True)
        reordered.photos.reverse()
        reordered.job_id = "another-transient-id"
        self.assertEqual(self.store.request_fingerprint(first), self.store.request_fingerprint(reordered))

        changed_setting = first.model_copy(deep=True)
        changed_setting.settings.preview_long_edge += 1
        self.assertNotEqual(self.store.request_fingerprint(first), self.store.request_fingerprint(changed_setting))
        changed_capture_time = first.model_copy(deep=True)
        changed_capture_time.photos[0].capture_time = "2026-08-01T10:00:00"
        self.assertNotEqual(self.store.request_fingerprint(first), self.store.request_fingerprint(changed_capture_time))
        transient_skip_detail = first.model_copy(deep=True)
        transient_skip_detail.skipped = [{"photo_id": "not-exported", "error": "temporary path C:/Temp/run-123 at 10:00"}]
        self.assertEqual(self.store.request_fingerprint(first), self.store.request_fingerprint(transient_skip_detail))

    def test_client_fingerprint_uses_preview_content_and_ignores_photo_order(self) -> None:
        preview_folder = Path(self.temporary.name) / "previews"
        preview_folder.mkdir()
        (preview_folder / "photo-1.jpg").write_bytes(b"preview-one")
        (preview_folder / "photo-2.jpg").write_bytes(b"preview-two")
        payload = self.request(count=2).model_dump()
        hashed = add_preview_hashes(payload, preview_folder)
        reordered = {**hashed, "job_id": "different-id", "photos": list(reversed(hashed["photos"]))}
        self.assertEqual(resume_fingerprint(hashed), resume_fingerprint(reordered))

        (preview_folder / "photo-1.jpg").write_bytes(b"changed-preview")
        changed = add_preview_hashes(payload, preview_folder)
        self.assertNotEqual(resume_fingerprint(hashed), resume_fingerprint(changed))

    def test_atomic_write_cleans_its_temporary_file_when_replace_fails(self) -> None:
        job_id = self.create(self.request(count=1))
        with patch("server.job_store.os.replace", side_effect=OSError("replace failed")):
            with self.assertRaises(OSError):
                self.store.write_json(job_id, "new.json", {"value": 1})
        self.assertFalse((self.store.job_dir(job_id) / "new.json").exists())
        self.assertEqual(list(self.store.job_dir(job_id).glob(".new.json.*.tmp")), [])

    def test_changed_preview_is_rejected_after_processing_starts(self) -> None:
        job_id = self.create(self.request(count=1))
        path, _ = self.store.save_preview(job_id, "photo-1.jpg", b"original")
        self.store.set_status(job_id, "running", Progress(stage="grouping"))
        with self.assertRaises(PreviewConflictError):
            self.store.save_preview(job_id, "photo-1.jpg", b"changed")
        self.assertEqual(path.read_bytes(), b"original")

    def test_same_job_id_with_different_request_conflicts(self) -> None:
        self.create(self.request(count=1))
        with self.assertRaises(JobConflictError):
            self.store.create_job(self.request(count=2))

    def test_changed_preview_hash_conflicts_for_a_versioned_job(self) -> None:
        request = self.request(count=1)
        request.photos[0].preview_sha256 = "a" * 64
        self.create(request)
        changed = request.model_copy(deep=True)
        changed.photos[0].preview_sha256 = "b" * 64
        with self.assertRaises(JobConflictError):
            self.store.create_job(changed)

    def test_legacy_job_without_preview_hash_can_be_reused_without_mutation(self) -> None:
        legacy = self.request(count=1)
        job_id = self.create(legacy)
        job_payload = self.store.read_json(job_id, "job.json")
        job_payload.pop("_request_fingerprint", None)
        job_payload.pop("_request_fingerprint_version", None)
        self.store.write_json(job_id, "job.json", job_payload)
        before = (self.store.job_dir(job_id) / "job.json").read_bytes()
        upgraded = legacy.model_copy(deep=True)
        upgraded.photos[0].preview_sha256 = "a" * 64

        reused_id, reused = self.store.create_job(upgraded)

        self.assertEqual(reused_id, job_id)
        self.assertTrue(reused)
        self.assertEqual((self.store.job_dir(job_id) / "job.json").read_bytes(), before)

    def test_api_returns_http_409_for_same_id_with_different_input(self) -> None:
        with patch("server.main.JobStore", lambda: JobStore(self.config)):
            api_create_job(self.request(count=1))
            with self.assertRaises(HTTPException) as raised:
                api_create_job(self.request(count=2))
        self.assertEqual(raised.exception.status_code, 409)

    def test_simultaneous_job_creation_is_idempotent(self) -> None:
        request = self.request(count=1)
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda _: JobStore(self.config).create_job(request), range(2)))
        self.assertEqual({job_id for job_id, _ in responses}, {request.job_id})
        self.assertEqual(sorted(reused for _, reused in responses), [False, True])

    def test_grouping_interruption_retries_grouping(self) -> None:
        job_id = self.create(self.request(count=1))
        groups = self.groups(count=1)
        with (
            patch("server.main.JobStore", lambda: JobStore(self.config)),
            patch("server.main.build_image_records", return_value=([object()], [])),
            patch("server.main.create_review_groups", side_effect=RuntimeError("grouping interrupted")),
        ):
            process_job(job_id)

        self.assertEqual(self.store.get_status(job_id)["status"], "failed")
        self.assertFalse((self.store.job_dir(job_id) / "groups.json").exists())

        self.run_process_with(job_id, groups, lambda group, records: self.decision(group))
        self.assertEqual(self.store.get_result(job_id)["status"], "completed")

    def test_vision_interruption_skips_completed_groups_on_resume(self) -> None:
        job_id = self.create()
        groups = self.groups()
        first_attempt: list[str] = []

        def interrupted_reviewer(group, records):
            first_attempt.append(group.group_id)
            if group.group_id == "group-2":
                raise RuntimeError("vision interrupted")
            return self.decision(group)

        self.run_process_with(job_id, groups, interrupted_reviewer)
        checkpoint = self.store.load_checkpoint(job_id)
        self.assertEqual(list(checkpoint), ["group-1"])
        self.assertEqual(self.store.get_status(job_id)["status"], "failed")

        resumed_attempt: list[str] = []

        def resumed_reviewer(group, records):
            resumed_attempt.append(group.group_id)
            return self.decision(group)

        self.run_process_with(job_id, groups, resumed_reviewer)
        self.assertEqual(resumed_attempt, ["group-2", "group-3"])
        self.assertEqual(self.store.get_status(job_id)["status"], "completed")

    def test_invalid_checkpoint_entry_is_not_treated_as_complete(self) -> None:
        job_id = self.create(self.request(count=1))
        group = self.groups(count=1)[0]
        self.store.save_groups(job_id, [group], [])
        self.store.write_json(
            job_id,
            "checkpoint.json",
            {
                "schema_version": 1,
                "job_id": job_id,
                "completed_groups": {group.group_id: {"decisions": [], "completed_at": "now"}},
            },
        )
        self.assertEqual(self.store.load_checkpoint(job_id, [group]), {})

    def test_malformed_checkpoint_file_is_rebuilt_only_after_valid_decision(self) -> None:
        job_id = self.create(self.request(count=1))
        group = self.groups(count=1)[0]
        checkpoint_path = self.store.job_dir(job_id) / "checkpoint.json"
        checkpoint_path.write_text("{malformed", encoding="utf-8")
        self.assertEqual(self.store.load_checkpoint(job_id, [group]), {})
        self.store.save_group_checkpoint(job_id, group, self.decision(group))
        self.assertIn(group.group_id, self.store.load_checkpoint(job_id, [group]))

    def test_invalid_decision_is_rejected_before_checkpoint_write(self) -> None:
        job_id = self.create(self.request(count=1))
        group = self.groups(count=1)[0]
        invalid = [GroupDecision(group_id=group.group_id, photos=group.photos, representatives=[], rejected=[], confidence=0)]
        with self.assertRaises(ValueError):
            self.store.save_group_checkpoint(job_id, group, invalid)
        self.assertFalse((self.store.job_dir(job_id) / "checkpoint.json").exists())

    def test_duplicate_photo_decisions_fail_final_validation(self) -> None:
        decision = self.decision(self.groups(count=1)[0])[0]
        duplicate = decision.model_copy(update={"group_id": "duplicate-group"})
        result = validate_result(
            JobResult(job_id="job", status="completed", groups=[decision, duplicate]),
            {"photo-1"},
        )
        self.assertEqual(result.status, "failed")
        self.assertTrue(any("multiple result decisions" in error for error in result.errors))

    def test_final_result_creation_failure_reuses_all_checkpoints(self) -> None:
        job_id = self.create(count_request := self.request(count=2))
        self.assertEqual(len(count_request.photos), 2)
        groups = self.groups(count=2)
        original_save_result = JobStore.save_result

        def fail_final_write(store, result):
            raise OSError("result disk interruption")

        with patch.object(JobStore, "save_result", fail_final_write):
            self.run_process_with(job_id, groups, lambda group, records: self.decision(group))

        self.assertEqual(set(self.store.load_checkpoint(job_id)), {"group-1", "group-2"})
        self.assertFalse((self.store.job_dir(job_id) / "result.json").exists())
        self.assertEqual(self.store.get_status(job_id)["status"], "failed")

        reviewer_calls: list[str] = []

        def should_not_review(group, records):
            reviewer_calls.append(group.group_id)
            return self.decision(group)

        self.run_process_with(job_id, groups, should_not_review)
        self.assertEqual(reviewer_calls, [])
        self.assertEqual(self.store.get_result(job_id)["status"], "completed")
        self.assertIs(JobStore.save_result, original_save_result)

    def test_completed_job_reports_all_review_groups_processed(self) -> None:
        job_id = self.create(self.request(count=2))
        groups = self.groups(count=2)

        self.run_process_with(
            job_id,
            groups,
            lambda group, records: self.decision(group),
        )

        status = self.store.get_status(job_id)
        self.assertEqual(status["status"], "completed")
        self.assertEqual(
            status["progress"],
            {
                "stage": "completed",
                "processed_groups": 2,
                "total_groups": 2,
                "current_group_id": None,
            },
        )

    def test_duplicate_run_requests_queue_only_once(self) -> None:
        job_id = self.create(self.request(count=1))
        background = CapturingBackgroundTasks()
        with patch("server.main.JobStore", lambda: JobStore(self.config)):
            with ThreadPoolExecutor(max_workers=2) as pool:
                responses = list(pool.map(lambda _: run_job(job_id, RunJobRequest(), background), range(2)))  # type: ignore[arg-type]

        self.assertEqual([response["status"] for response in responses], ["running", "running"])
        self.assertEqual(sum(bool(response.get("already_running")) for response in responses), 1)
        self.assertEqual(len(background.tasks), 1)

    def test_failed_and_interrupted_jobs_are_queued_for_resume(self) -> None:
        for state in ("failed", "interrupted"):
            with self.subTest(state=state):
                request = self.request(job_id=f"job-{state}", count=1)
                job_id = self.create(request)
                self.store.set_status(job_id, state, Progress(stage=state), errors=[state], resumable=True)
                background = CapturingBackgroundTasks()
                with patch("server.main.JobStore", lambda: JobStore(self.config)):
                    response = run_job(job_id, RunJobRequest(), background)  # type: ignore[arg-type]
                self.assertTrue(response["resumed"])
                self.assertEqual(len(background.tasks), 1)
                with _active_jobs_lock:
                    _active_jobs.discard(job_id)

    def test_job_marked_running_is_not_queued_again_without_local_registry_entry(self) -> None:
        job_id = self.create(self.request(count=1))
        self.store.set_status(job_id, "running", Progress(stage="vision_review"))
        background = CapturingBackgroundTasks()
        with patch("server.main.JobStore", lambda: JobStore(self.config)):
            response = run_job(job_id, RunJobRequest(), background)  # type: ignore[arg-type]
        self.assertTrue(response["already_running"])
        self.assertEqual(background.tasks, [])

    def test_worker_submission_failure_clears_active_marker_and_is_resumable(self) -> None:
        job_id = self.create(self.request(count=1))
        self.store.set_status(job_id, "running", Progress(stage="queued"))
        with _active_jobs_lock:
            _active_jobs.add(job_id)
        with (
            patch("server.main.JobStore", lambda: JobStore(self.config)),
            patch("server.main.executor.submit", side_effect=RuntimeError("executor unavailable")),
        ):
            submit_job(job_id)
        self.assertNotIn(job_id, _active_jobs)
        status = self.store.get_status(job_id)
        self.assertEqual(status["status"], "failed")
        self.assertTrue(status["resumable"])

    def test_server_restart_marks_stale_job_interrupted_and_preserves_checkpoint(self) -> None:
        job_id = self.create(self.request(count=1))
        group = self.groups(count=1)[0]
        self.store.save_groups(job_id, [group], [])
        self.store.save_group_checkpoint(job_id, group, self.decision(group))
        self.store.set_status(
            job_id,
            "running",
            Progress(stage="vision_review", processed_groups=1, total_groups=1),
        )

        interrupted = JobStore(self.config).mark_stale_jobs_interrupted()
        status = self.store.get_status(job_id)
        self.assertEqual(interrupted, [job_id])
        self.assertEqual(status["status"], "interrupted")
        self.assertTrue(status["resumable"])
        self.assertIn(group.group_id, self.store.load_checkpoint(job_id))

    def test_server_restart_treats_valid_result_as_authoritative(self) -> None:
        job_id = self.create(self.request(count=1))
        group = self.groups(count=1)[0]
        self.store.save_groups(job_id, [group], [])
        self.store.save_group_checkpoint(job_id, group, self.decision(group))
        self.store.save_result(JobResult(job_id=job_id, status="completed", groups=self.decision(group)))
        self.store.set_status(job_id, "running", Progress(stage="vision_review", processed_groups=1, total_groups=1))

        interrupted = JobStore(self.config).mark_stale_jobs_interrupted()
        status = self.store.get_status(job_id)
        self.assertEqual(interrupted, [])
        self.assertEqual(status["status"], "completed")
        self.assertFalse(status["resumable"])
        self.assertEqual(status["progress"]["processed_groups"], 1)
        self.assertEqual(status["progress"]["total_groups"], 1)

    def test_server_restart_marks_invalid_completed_result_failed(self) -> None:
        job_id = self.create(self.request(count=1))
        self.store.write_json(
            job_id,
            "result.json",
            {
                "job_id": job_id,
                "status": "completed",
                "groups": [{"group_id": "bad", "photos": ["photo-1"], "representatives": [], "rejected": []}],
            },
        )
        self.store.set_status(job_id, "completed", Progress(stage="completed"), resumable=False)
        JobStore(self.config).mark_stale_jobs_interrupted()
        status = self.store.get_status(job_id)
        self.assertEqual(status["status"], "failed")
        self.assertTrue(status["resumable"])

    def test_invalid_completed_result_is_never_returned_as_completed(self) -> None:
        job_id = self.create(self.request(count=1))
        invalid = JobResult(
            job_id=job_id,
            status="completed",
            groups=[GroupDecision(group_id="bad", photos=["photo-1"], representatives=[], rejected=[])],
        )
        self.store.write_json(job_id, "result.json", invalid.model_dump())
        self.store.set_status(job_id, "completed", Progress(stage="completed"), resumable=False)
        with patch("server.main.JobStore", lambda: JobStore(self.config)):
            response = api_get_result(job_id)
        self.assertEqual(response["status"], "failed")
        self.assertEqual(self.store.get_status(job_id)["status"], "failed")

    def test_completed_run_returns_existing_result_without_queueing(self) -> None:
        job_id = self.create(self.request(count=1))
        group = self.groups(count=1)[0]
        result = JobResult(job_id=job_id, status="completed", groups=self.decision(group))
        self.store.save_result(result)
        background = CapturingBackgroundTasks()
        with patch("server.main.JobStore", lambda: JobStore(self.config)):
            response = run_job(job_id, RunJobRequest(), background)  # type: ignore[arg-type]
        self.assertEqual(response["status"], "completed")
        self.assertEqual(background.tasks, [])

    def test_client_resume_state_is_found_from_a_new_lightroom_job_folder(self) -> None:
        first_payload = self.request(job_id="lightroom-run-one", count=1).model_dump()
        second_payload = self.request(job_id="lightroom-run-two", count=1).model_dump()
        fingerprint = resume_fingerprint(first_payload)
        self.assertEqual(fingerprint, resume_fingerprint(second_payload))

        shared_state = Path(self.temporary.name) / ".resume" / f"{fingerprint}.json"
        save_resume_state(
            [shared_state],
            {
                "schema_version": 1,
                "job_id": "server-job-id",
                "request_fingerprint": fingerprint,
                "server_url": "http://server/photopick",
                "status": "interrupted",
                "resumable": True,
            },
        )
        loaded = load_resume_state([Path(self.temporary.name) / "new-job" / "job_state.json", shared_state], fingerprint, "http://server/photopick")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["job_id"], "server-job-id")

    def test_corrupted_or_missing_resume_state_is_ignored(self) -> None:
        fingerprint = "fingerprint"
        corrupt = Path(self.temporary.name) / "corrupt.json"
        corrupt.write_text("{not-json", encoding="utf-8")
        missing = Path(self.temporary.name) / "missing.json"
        self.assertIsNone(load_resume_state([missing, corrupt], fingerprint, "http://server"))

    def test_http_409_message_includes_server_conflict_detail(self) -> None:
        error = HTTPError(
            "http://server/jobs",
            409,
            "Conflict",
            {},
            BytesIO(b'{"detail":"job input changed"}'),
        )
        try:
            self.assertEqual(http_error_message(error), "PhotoPickAI resume conflict (HTTP 409): job input changed")
        finally:
            error.close()


if __name__ == "__main__":
    unittest.main()
