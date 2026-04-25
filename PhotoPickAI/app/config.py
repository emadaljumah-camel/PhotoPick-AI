from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

from app.utils.json_utils import read_json_file, write_json_file

OLD_OLLAMA_DEFAULT_ENDPOINT = "http://127.0.0.1:11434/api/chat"
OLD_OLLAMA_DEFAULT_MODEL = "llava"
DEFAULT_VISION_LLM_ENDPOINT = "http://127.0.0.1:1234/v1/chat/completions"
DEFAULT_VISION_LLM_MODEL = "qwen2.5-vl-7b-instruct"


@dataclass(slots=True)
class AppSettings:
    """User-configurable settings for the local MVP."""

    preview_long_edge: int = 512
    comparison_time_window_seconds: int = 20
    burst_time_window_seconds: int = 20
    similarity_threshold: float = 0.925
    exact_duplicate_threshold: float = 0.985
    auto_suggest_winner: bool = True
    same_orientation_preference: bool = True
    vision_llm_endpoint: str = DEFAULT_VISION_LLM_ENDPOINT
    vision_llm_model: str = DEFAULT_VISION_LLM_MODEL
    vision_llm_timeout_seconds: int = 180


@dataclass(slots=True)
class AppPaths:
    """Resolved application directories."""

    root_dir: Path
    workspace_dir: Path
    previews_dir: Path
    metadata_dir: Path
    similarity_dir: Path
    review_dir: Path
    settings_path: Path


def get_app_paths() -> AppPaths:
    """Return standard paths rooted at the project directory."""

    root_dir = Path(__file__).resolve().parents[1]
    workspace_dir = root_dir / "workspace"
    cache_dir = workspace_dir / "cache"
    return AppPaths(
        root_dir=root_dir,
        workspace_dir=workspace_dir,
        previews_dir=cache_dir / "previews",
        metadata_dir=cache_dir / "metadata",
        similarity_dir=cache_dir / "similarity",
        review_dir=cache_dir / "review",
        settings_path=workspace_dir / "settings.json",
    )


def load_settings(paths: AppPaths | None = None) -> AppSettings:
    """Load persisted settings or fall back to defaults."""

    resolved_paths = paths or get_app_paths()
    data = read_json_file(resolved_paths.settings_path, default=None)
    if not isinstance(data, dict):
        return AppSettings()
    defaults = asdict(AppSettings())
    defaults.update({key: value for key, value in data.items() if key in defaults})
    if defaults["vision_llm_endpoint"] == OLD_OLLAMA_DEFAULT_ENDPOINT:
        defaults["vision_llm_endpoint"] = DEFAULT_VISION_LLM_ENDPOINT
    if defaults["vision_llm_model"] == OLD_OLLAMA_DEFAULT_MODEL:
        defaults["vision_llm_model"] = DEFAULT_VISION_LLM_MODEL
    return AppSettings(**defaults)


def save_settings(settings: AppSettings, paths: AppPaths | None = None) -> None:
    """Persist settings to disk."""

    resolved_paths = paths or get_app_paths()
    write_json_file(resolved_paths.settings_path, asdict(settings))
