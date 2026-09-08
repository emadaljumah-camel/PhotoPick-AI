from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from PIL import Image

from app.config import AppSettings
from app.core.similarity_engine import SimilarityEngine
from server.models import CreateJobRequest, JobPhoto
from server.photo_ingest import build_image_records


class PhotoIngestMetadataTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.job_dir = Path(self.temporary.name)
        self.previews_dir = self.job_dir / "previews"
        self.previews_dir.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_preview(
        self,
        filename: str,
        exif_capture_time: str | None,
        color: str = "white",
        exif_tag: int = 36867,
    ) -> Path:
        path = self.previews_dir / filename
        exif = Image.Exif()
        if exif_capture_time:
            exif[exif_tag] = exif_capture_time
        Image.new("RGB", (64, 48), color).save(path, format="JPEG", exif=exif)
        upload_time = datetime(2035, 1, 1, 12, 0, 0).timestamp()
        os.utime(path, (upload_time, upload_time))
        return path

    def _request(
        self,
        filename: str,
        capture_time: str = "",
        photo_id: str = "photo-1",
    ) -> CreateJobRequest:
        return CreateJobRequest(
            photos=[
                JobPhoto(
                    photo_id=photo_id,
                    filename=filename,
                    preview_filename=filename,
                    capture_time=capture_time,
                )
            ]
        )

    def test_embedded_exif_capture_time_is_used_instead_of_upload_time(self) -> None:
        self._write_preview("frame.jpg", "2024:06:15 10:11:12")

        records, skipped = build_image_records(
            self.job_dir,
            self._request("frame.jpg"),
        )

        self.assertEqual(skipped, [])
        self.assertEqual(records[0].capture_time, "2024-06-15T10:11:12")
        self.assertEqual(records[0].capture_time_source, "embedded_exif")

    def test_manifest_capture_time_takes_precedence_over_embedded_exif(self) -> None:
        self._write_preview("frame.jpg", "2024:06:15 10:11:12")

        records, skipped = build_image_records(
            self.job_dir,
            self._request("frame.jpg", "2025-07-20T13:14:15+03:00"),
        )

        self.assertEqual(skipped, [])
        self.assertEqual(records[0].capture_time, "2025-07-20T13:14:15+03:00")
        self.assertEqual(records[0].capture_time_source, "manifest")

    def test_missing_capture_time_remains_unknown_and_ignores_preview_mtime(self) -> None:
        self._write_preview("frame.jpg", None)

        records, skipped = build_image_records(
            self.job_dir,
            self._request("frame.jpg"),
        )

        self.assertEqual(skipped, [])
        self.assertEqual(records[0].capture_time, "")
        self.assertEqual(records[0].capture_time_source, "unknown")
        self.assertNotEqual(records[0].capture_time, "2035-01-01T12:00:00")

    def test_preview_export_datetime_is_not_treated_as_camera_capture_time(self) -> None:
        self._write_preview(
            "frame.jpg",
            "2035:01:01 12:00:00",
            exif_tag=306,
        )

        records, skipped = build_image_records(
            self.job_dir,
            self._request("frame.jpg"),
        )

        self.assertEqual(skipped, [])
        self.assertEqual(records[0].capture_time, "")
        self.assertEqual(records[0].capture_time_source, "unknown")

    def test_unknown_capture_time_disables_time_edges_but_keeps_visual_matching(self) -> None:
        self._write_preview("frame_1.jpg", None, color="red")
        self._write_preview("frame_2.jpg", None, color="red")
        request = CreateJobRequest(
            photos=[
                JobPhoto(
                    photo_id="photo-1",
                    filename="frame_1.jpg",
                    preview_filename="frame_1.jpg",
                ),
                JobPhoto(
                    photo_id="photo-2",
                    filename="frame_2.jpg",
                    preview_filename="frame_2.jpg",
                ),
            ]
        )
        records, skipped = build_image_records(self.job_dir, request)
        engine = SimilarityEngine(
            AppSettings(
                comparison_time_window_seconds=60,
                burst_time_window_seconds=20,
                similarity_threshold=0.92,
                exact_duplicate_threshold=0.94,
                same_orientation_preference=False,
            )
        )

        edges = engine.analyze_records(records)

        self.assertEqual(skipped, [])
        self.assertTrue(
            any(edge_type == "near_duplicate" for _, _, _, edge_type in edges)
        )
        self.assertEqual(engine._temporal_burst_chain_edges(records), [])
        candidate_scores = {
            ("photo-1", "photo-2"): (0.95, 0.95, 0, None, 0.0, 1.0)
        }
        self.assertEqual(
            engine._burst_candidate_edges(records, candidate_scores),
            [],
        )
        self.assertEqual(
            engine._bridge_sequence_neighbors(records, candidate_scores),
            [],
        )


if __name__ == "__main__":
    unittest.main()
