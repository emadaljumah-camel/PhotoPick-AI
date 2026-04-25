from __future__ import annotations

from datetime import datetime


EXIF_TIME_FORMATS = (
    "%Y:%m:%d %H:%M:%S",
    "%Y:%m:%d %H:%M:%S%z",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
)


def parse_capture_time(value: str | None) -> datetime | None:
    """Parse EXIF or ISO timestamps into a datetime."""

    if not value:
        return None

    normalized = value.strip()
    for fmt in EXIF_TIME_FORMATS:
        try:
            return datetime.strptime(normalized, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def to_iso_string(value: datetime) -> str:
    """Convert datetime to an ISO string without microseconds."""

    return value.replace(microsecond=0).isoformat()


def format_display_time(value: str) -> str:
    """Format persisted ISO timestamps for the UI."""

    try:
        parsed = datetime.fromisoformat(value)
        return parsed.strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return value or "Unknown"
