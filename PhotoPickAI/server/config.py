from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = PROJECT_ROOT / "photopickai_server_config.yaml"


@dataclass(frozen=True)
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = 8000
    api_key: str = ""
    model_provider: str = "lmstudio"
    model_base_url: str = "http://127.0.0.1:1234/v1"
    model_name: str = ""
    model_timeout_seconds: int = 180
    max_preview_long_edge: int = 1600
    comparison_time_window_seconds: int = 20
    burst_time_window_seconds: int = 20
    default_group_similarity_threshold: float = 0.925
    exact_duplicate_threshold: float = 0.985
    same_orientation_preference: bool = True
    allow_subgroups: bool = True
    retry_model_json_once: bool = True
    jobs_dir: Path = PROJECT_ROOT / "server" / "jobs"
    keep_jobs_days: int = 7

    @property
    def vision_endpoint(self) -> str:
        return self.model_base_url.rstrip("/") + "/chat/completions"


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        payload = yaml.safe_load(handle) or {}
    return payload if isinstance(payload, dict) else {}


def load_config() -> ServerConfig:
    payload = _read_yaml(CONFIG_PATH)
    server = payload.get("server", {}) or {}
    model = payload.get("model", {}) or {}
    processing = payload.get("processing", {}) or {}
    storage = payload.get("storage", {}) or {}
    api_key = os.getenv("PHOTOPICKAI_API_KEY", server.get("api_key", ""))
    return ServerConfig(
        host=server.get("host", "0.0.0.0"),
        port=int(server.get("port", 8000)),
        api_key=str(api_key or ""),
        model_provider=model.get("provider", "lmstudio"),
        model_base_url=model.get("base_url", "http://127.0.0.1:1234/v1"),
        model_name=model.get("model_name", ""),
        model_timeout_seconds=int(model.get("timeout_seconds", 180)),
        max_preview_long_edge=int(processing.get("max_preview_long_edge", 1600)),
        comparison_time_window_seconds=int(processing.get("comparison_time_window_seconds", 20)),
        burst_time_window_seconds=int(processing.get("burst_time_window_seconds", 20)),
        default_group_similarity_threshold=float(processing.get("default_group_similarity_threshold", 0.925)),
        exact_duplicate_threshold=float(processing.get("exact_duplicate_threshold", 0.985)),
        same_orientation_preference=bool(processing.get("same_orientation_preference", True)),
        allow_subgroups=bool(processing.get("allow_subgroups", True)),
        retry_model_json_once=bool(processing.get("retry_model_json_once", True)),
        jobs_dir=Path(storage.get("jobs_dir", PROJECT_ROOT / "server" / "jobs")),
        keep_jobs_days=int(storage.get("keep_jobs_days", 7)),
    )
