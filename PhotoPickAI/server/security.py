from __future__ import annotations

from fastapi import Header, HTTPException

from server.config import load_config


def require_api_key(x_photopickai_key: str | None = Header(default=None)) -> None:
    config = load_config()
    if not config.api_key:
        return
    if x_photopickai_key != config.api_key:
        raise HTTPException(status_code=401, detail="Invalid or missing PhotoPickAI API key")
