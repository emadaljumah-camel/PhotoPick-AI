from __future__ import annotations

import json
import sys
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError, URLError
from unittest.mock import patch

from client_helper.lr_client_helper import add_preview_hashes, main, read_json, resume_fingerprint, write_json


class AsyncClientHelperTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.job_dir = Path(self.temporary.name) / "job"
        self.preview_dir = self.job_dir / "previews"
        self.preview_dir.mkdir(parents=True)
        self.job_json = self.job_dir / "job.json"
        self.result_json = self.job_dir / "result.json"
        self.preview_path = self.preview_dir / "photo.jpg"
        self.preview_path.write_bytes(b"preview")
        write_json(
            self.job_json,
            {
                "client": "lightroom",
                "job_id": "client-job",
                "photos": [
                    {
                        "photo_id": "photo-1",
                        "filename": "photo.nef",
                        "preview_filename": self.preview_path.name,
                    }
                ],
            },
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_helper(self, action: str) -> int:
        argv = [
            "lr_client_helper.py",
            "--server-url",
            "http://server/photopick",
            "--job-json",
            str(self.job_json),
            "--preview-folder",
            str(self.preview_dir),
            "--result-json",
            str(self.result_json),
            "--action",
            action,
        ]
        with patch.object(sys, "argv", argv):
            return main()

    def save_job_state(self, status: str = "running") -> None:
        write_json(
            self.job_dir / "job_state.json",
            {
                "schema_version": 1,
                "job_id": "server-job",
                "request_fingerprint": "fingerprint",
                "server_url": "http://server/photopick",
                "status": status,
                "resumable": status != "completed",
            },
        )

    def result(self) -> dict:
        return json.loads(self.result_json.read_text(encoding="utf-8"))

    @patch("client_helper.lr_client_helper.upload_file", return_value={"status": "uploaded"})
    @patch("client_helper.lr_client_helper.request_json")
    def test_submit_returns_after_queueing_without_polling(self, request_json, upload_file) -> None:
        def response(method, url, payload=None, api_key="", timeout=60):
            if method == "POST" and url.endswith("/jobs"):
                return {"job_id": "server-job", "status": "created"}
            if method == "GET" and url.endswith("/uploads"):
                return {"uploaded_photo_ids": [], "uploaded_preview_sha256": {}}
            if method == "POST" and url.endswith("/run"):
                return {"job_id": "server-job", "status": "running"}
            self.fail(f"Unexpected request: {method} {url}")

        request_json.side_effect = response

        self.assertEqual(self.run_helper("submit"), 0)
        self.assertEqual(self.result()["status"], "running")
        self.assertEqual(upload_file.call_count, 1)
        self.assertFalse(any(call.args[1].endswith("/status") for call in request_json.call_args_list))

    @patch("client_helper.lr_client_helper.request_json")
    def test_check_returns_live_running_progress(self, request_json) -> None:
        self.save_job_state()
        request_json.return_value = {
            "job_id": "server-job",
            "status": "running",
            "progress": {"stage": "vision_review", "processed_groups": 3, "total_groups": 10},
            "resumable": True,
        }

        self.assertEqual(self.run_helper("check"), 0)
        self.assertEqual(self.result()["progress"]["processed_groups"], 3)
        self.assertEqual(request_json.call_count, 1)

    @patch("client_helper.lr_client_helper.request_json")
    def test_check_downloads_completed_result(self, request_json) -> None:
        self.save_job_state()
        request_json.side_effect = [
            {"job_id": "server-job", "status": "completed", "resumable": False},
            {"job_id": "server-job", "status": "completed", "groups": [], "errors": []},
        ]

        self.assertEqual(self.run_helper("check"), 0)
        self.assertEqual(self.result()["status"], "completed")
        self.assertEqual(request_json.call_count, 2)
        self.assertTrue(request_json.call_args_list[1].args[1].endswith("/result"))
        self.assertEqual(read_json(self.job_dir / "completed_result.json")["job_id"], "server-job")

    @patch("client_helper.lr_client_helper.request_json", side_effect=URLError("server unavailable"))
    def test_check_uses_preserved_completed_result_when_server_is_unavailable(self, request_json) -> None:
        self.save_job_state(status="completed")
        completed = {"job_id": "server-job", "status": "completed", "groups": [], "errors": []}
        write_json(self.job_dir / "completed_result.json", completed)

        self.assertEqual(self.run_helper("check"), 0)
        self.assertEqual(self.result(), completed)
        self.assertEqual(request_json.call_count, 1)

    @patch("client_helper.lr_client_helper.upload_file", return_value={"status": "uploaded"})
    @patch("client_helper.lr_client_helper.request_json")
    def test_submit_does_not_reuse_a_completed_shared_job(self, request_json, upload_file) -> None:
        payload = add_preview_hashes(read_json(self.job_json), self.preview_dir)
        fingerprint = resume_fingerprint(payload)
        write_json(
            Path(self.temporary.name) / ".resume" / f"{fingerprint}.json",
            {
                "schema_version": 1,
                "job_id": "old-completed-job",
                "request_fingerprint": fingerprint,
                "server_url": "http://server/photopick",
                "status": "completed",
                "resumable": False,
            },
        )

        def response(method, url, payload=None, api_key="", timeout=60):
            if method == "POST" and url.endswith("/jobs"):
                return {"job_id": "fresh-job", "status": "created"}
            if method == "GET" and url.endswith("/uploads"):
                return {"uploaded_photo_ids": [], "uploaded_preview_sha256": {}}
            if method == "POST" and url.endswith("/run"):
                return {"job_id": "fresh-job", "status": "running"}
            self.fail(f"Unexpected request: {method} {url}")

        request_json.side_effect = response
        self.assertEqual(self.run_helper("submit"), 0)
        self.assertEqual(read_json(self.job_dir / "job_state.json")["job_id"], "fresh-job")
        self.assertEqual(upload_file.call_count, 1)

    @patch("client_helper.lr_client_helper.upload_file")
    @patch("client_helper.lr_client_helper.request_json")
    def test_resume_queues_existing_job_without_uploading(self, request_json, upload_file) -> None:
        self.save_job_state(status="failed")
        request_json.side_effect = [
            {"job_id": "server-job", "status": "failed", "resumable": True},
            {"job_id": "server-job", "status": "running", "resumed": True},
        ]

        self.assertEqual(self.run_helper("resume"), 0)
        self.assertEqual(self.result()["status"], "running")
        self.assertEqual(request_json.call_count, 2)
        self.assertTrue(request_json.call_args_list[1].args[1].endswith("/run"))
        upload_file.assert_not_called()

    @patch("client_helper.lr_client_helper.request_json")
    def test_check_without_local_state_does_not_create_a_job(self, request_json) -> None:
        self.assertEqual(self.run_helper("check"), 1)
        self.assertEqual(self.result()["status"], "failed")
        self.assertFalse(self.result()["resumable"])
        request_json.assert_not_called()

    @patch("client_helper.lr_client_helper.request_json")
    def test_missing_server_job_requires_a_new_submission(self, request_json) -> None:
        self.save_job_state()
        missing_error = HTTPError(
            "http://server/photopick/jobs/server-job/status",
            404,
            "Not Found",
            {},
            BytesIO(b'{"detail":"Unknown job"}'),
        )
        request_json.side_effect = missing_error

        try:
            self.assertEqual(self.run_helper("check"), 1)
            self.assertEqual(self.result()["status"], "missing")
            self.assertFalse(self.result()["resumable"])
        finally:
            missing_error.close()


if __name__ == "__main__":
    unittest.main()
