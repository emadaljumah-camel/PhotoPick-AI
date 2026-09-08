from __future__ import annotations

import unittest

from app.config import AppSettings
from app.core.models import ImageRecord
from app.core.similarity_engine import SimilarityEngine


def image_record(
    photo_id: str,
    filename: str,
    *,
    width: int,
    height: int,
    capture_time: str,
) -> ImageRecord:
    return ImageRecord(
        id=photo_id,
        filename=filename,
        path=filename,
        file_size=1000,
        width=width,
        height=height,
        orientation="landscape" if width >= height else "portrait",
        capture_time=capture_time,
    )


class CaptureTimeNormalizationTests(unittest.TestCase):
    def test_naive_and_offset_aware_times_are_comparable(self) -> None:
        naive = image_record(
            "naive",
            "naive.jpg",
            width=1200,
            height=800,
            capture_time="2026-01-01T10:00:00",
        )
        aware = image_record(
            "aware",
            "aware.jpg",
            width=1200,
            height=800,
            capture_time="2026-01-01T10:00:01+03:00",
        )

        self.assertEqual(
            sorted([aware, naive], key=lambda item: item.capture_datetime),
            [naive, aware],
        )

    def test_offset_is_ignored_for_local_capture_sequence(self) -> None:
        naive = image_record(
            "naive",
            "naive.jpg",
            width=1200,
            height=800,
            capture_time="2026-01-01T10:00:00",
        )
        aware = image_record(
            "aware",
            "aware.jpg",
            width=1200,
            height=800,
            capture_time="2026-01-01T10:00:00+03:00",
        )

        self.assertEqual(naive.capture_datetime, aware.capture_datetime)
        self.assertIsNone(aware.capture_datetime.tzinfo)


class OrientationNeutralSimilarityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.landscape = image_record(
            "p1",
            "frame_1.jpg",
            width=1200,
            height=800,
            capture_time="2026-01-01T10:00:00",
        )
        self.portrait = image_record(
            "p2",
            "frame_2.jpg",
            width=800,
            height=1200,
            capture_time="2026-01-01T10:00:14",
        )

    def test_rotated_aspect_ratios_match_when_orientation_is_not_required(
        self,
    ) -> None:
        engine = SimilarityEngine(
            AppSettings(same_orientation_preference=False)
        )

        self.assertEqual(
            engine._comparison_aspect_ratio(self.landscape),
            engine._comparison_aspect_ratio(self.portrait),
        )
        self.assertTrue(
            engine._reasonable_burst_sequence(
                self.landscape,
                self.portrait,
            )
        )

    def test_temporal_chain_connects_rotated_consecutive_frames(
        self,
    ) -> None:
        engine = SimilarityEngine(
            AppSettings(
                comparison_time_window_seconds=60,
                burst_time_window_seconds=20,
                same_orientation_preference=False,
            )
        )

        edges = engine._temporal_burst_chain_edges(
            [self.landscape, self.portrait]
        )

        self.assertEqual(len(edges), 1)
        self.assertEqual(edges[0][:2], ("p1", "p2"))

    def test_orientation_preference_still_blocks_cross_orientation_chain(
        self,
    ) -> None:
        engine = SimilarityEngine(
            AppSettings(
                comparison_time_window_seconds=60,
                burst_time_window_seconds=20,
                same_orientation_preference=True,
            )
        )

        edges = engine._temporal_burst_chain_edges(
            [self.landscape, self.portrait]
        )

        self.assertEqual(edges, [])


if __name__ == "__main__":
    unittest.main()
