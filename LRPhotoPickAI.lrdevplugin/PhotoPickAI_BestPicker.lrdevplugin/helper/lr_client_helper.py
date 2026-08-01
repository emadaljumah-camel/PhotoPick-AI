from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from uuid import uuid4


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def resume_fingerprint(job_payload: dict) -> str:
    stable_payload = json.loads(json.dumps(job_payload))
    stable_payload.pop("job_id", None)
    stable_payload.pop("skipped", None)
    stable_payload["photos"] = sorted(
        stable_payload.get("photos", []),
        key=lambda item: (str(item.get("photo_id", "")), json.dumps(item, sort_keys=True, separators=(",", ":"))),
    )
    canonical = json.dumps(stable_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def add_preview_hashes(job_payload: dict, preview_folder: Path) -> dict:
    payload = json.loads(json.dumps(job_payload))
    for photo in payload.get("photos", []):
        path = preview_folder / photo["preview_filename"]
        if path.is_file():
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            photo["preview_sha256"] = digest.hexdigest()
    return payload


def http_error_message(exc: urllib.error.HTTPError) -> str:
    detail = ""
    try:
        payload = json.loads(exc.read().decode("utf-8"))
        detail = str(payload.get("detail", ""))
    except (OSError, ValueError, UnicodeDecodeError):
        pass
    suffix = f": {detail}" if detail else ""
    if exc.code == 409:
        return f"PhotoPickAI resume conflict (HTTP 409){suffix}"
    return f"PhotoPickAI server returned HTTP {exc.code}{suffix}"


def load_resume_state(paths: list[Path], fingerprint: str, server_url: str) -> dict | None:
    for path in paths:
        try:
            state = read_json(path)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if state.get("request_fingerprint") == fingerprint and state.get("server_url") == server_url and state.get("job_id"):
            return state
    return None


def save_resume_state(paths: list[Path], payload: dict) -> None:
    payload = {**payload, "updated_at": time.time()}
    for path in paths:
        write_json(path, payload)


def request_json(method: str, url: str, payload: dict | None = None, api_key: str = "", timeout: int = 60) -> dict:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Accept": "application/json"}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    if api_key:
        headers["X-PhotoPickAI-Key"] = api_key
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def upload_file(url: str, path: Path, api_key: str, timeout: int) -> dict:
    boundary = "----PhotoPickAI" + uuid4().hex
    mime_type = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    body = bytearray()
    body.extend(f"--{boundary}\r\n".encode("utf-8"))
    body.extend(f'Content-Disposition: form-data; name="file"; filename="{path.name}"\r\n'.encode("utf-8"))
    body.extend(f"Content-Type: {mime_type}\r\n\r\n".encode("utf-8"))
    body.extend(path.read_bytes())
    body.extend(f"\r\n--{boundary}--\r\n".encode("utf-8"))
    headers = {
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Accept": "application/json",
    }
    if api_key:
        headers["X-PhotoPickAI-Key"] = api_key
    request = urllib.request.Request(url, data=bytes(body), headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description="PhotoPickAI Lightroom client helper")
    parser.add_argument("--server-url", required=True)
    parser.add_argument("--api-key", default="")
    parser.add_argument("--job-json", required=True)
    parser.add_argument("--preview-folder", required=True)
    parser.add_argument("--result-json", required=True)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--poll-interval", type=float, default=2)
    parser.add_argument("--max-poll-attempts", type=int, default=300)
    args = parser.parse_args()

    server_url = args.server_url.rstrip("/")
    preview_folder = Path(args.preview_folder)
    result_json = Path(args.result_json)
    job_payload = add_preview_hashes(read_json(Path(args.job_json)), preview_folder)
    fingerprint = resume_fingerprint(job_payload)
    local_state = result_json.with_name("job_state.json")
    shared_state = preview_folder.parent.parent / ".resume" / f"{fingerprint}.json"
    state_paths = [local_state, shared_state]
    state = load_resume_state(state_paths, fingerprint, server_url)
    job_id = str(state["job_id"]) if state else ""

    try:
        status = None
        if job_id:
            try:
                status = request_json("GET", f"{server_url}/jobs/{job_id}/status", None, args.api_key, args.timeout)
            except urllib.error.HTTPError as exc:
                if exc.code != 404:
                    raise
                job_id = ""

        if not job_id:
            created = request_json("POST", f"{server_url}/jobs", job_payload, args.api_key, args.timeout)
            job_id = str(created["job_id"])
            status = created
        save_resume_state(
            state_paths,
            {
                "schema_version": 1,
                "job_id": job_id,
                "request_fingerprint": fingerprint,
                "server_url": server_url,
                "status": (status or {}).get("status", "created"),
                "resumable": True,
            },
        )

        if (status or {}).get("status") == "completed":
            result = request_json("GET", f"{server_url}/jobs/{job_id}/result", None, args.api_key, args.timeout)
            write_json(result_json, result)
            if result.get("status") == "completed":
                save_resume_state(state_paths, {"schema_version": 1, "job_id": job_id, "request_fingerprint": fingerprint, "server_url": server_url, "status": "completed", "resumable": False})
                return 0
            status = result

        uploads = request_json("GET", f"{server_url}/jobs/{job_id}/uploads", None, args.api_key, args.timeout)
        uploaded_ids = {str(item) for item in uploads.get("uploaded_photo_ids", [])}
        uploaded_hashes = {str(key): str(value) for key, value in uploads.get("uploaded_preview_sha256", {}).items()}
        for photo in job_payload.get("photos", []):
            photo_id = str(photo["photo_id"])
            local_hash = str(photo.get("preview_sha256", ""))
            if photo_id in uploaded_ids and (not local_hash or uploaded_hashes.get(photo_id) == local_hash):
                continue
            preview_path = preview_folder / photo["preview_filename"]
            if not preview_path.exists():
                raise FileNotFoundError(f"Missing preview: {preview_path}")
            encoded_photo_id = urllib.parse.quote(photo_id, safe="")
            upload_file(f"{server_url}/jobs/{job_id}/photos/{encoded_photo_id}/preview", preview_path, args.api_key, args.timeout)

        run_response = request_json("POST", f"{server_url}/jobs/{job_id}/run", {"mode": "group_and_pick"}, args.api_key, args.timeout)
        if run_response.get("status") == "completed":
            write_json(result_json, run_response)
            save_resume_state(state_paths, {"schema_version": 1, "job_id": job_id, "request_fingerprint": fingerprint, "server_url": server_url, "status": "completed", "resumable": False})
            return 0

        for _ in range(args.max_poll_attempts):
            try:
                status = request_json("GET", f"{server_url}/jobs/{job_id}/status", None, args.api_key, args.timeout)
            except (urllib.error.URLError, TimeoutError) as exc:
                save_resume_state(
                    state_paths,
                    {
                        "schema_version": 1,
                        "job_id": job_id,
                        "request_fingerprint": fingerprint,
                        "server_url": server_url,
                        "status": "disconnected",
                        "resumable": True,
                        "last_error": str(exc),
                    },
                )
                time.sleep(args.poll_interval)
                continue
            if status.get("status") in {"completed", "failed"}:
                break
            time.sleep(args.poll_interval)
        else:
            raise TimeoutError("PhotoPickAI processing timed out")

        result = request_json("GET", f"{server_url}/jobs/{job_id}/result", None, args.api_key, args.timeout)
        write_json(result_json, result)
        save_resume_state(
            state_paths,
            {
                "schema_version": 1,
                "job_id": job_id,
                "request_fingerprint": fingerprint,
                "server_url": server_url,
                "status": result.get("status", "failed"),
                "resumable": result.get("status") != "completed",
            },
        )
        return 0 if result.get("status") == "completed" else 2
    except Exception as exc:
        message = http_error_message(exc) if isinstance(exc, urllib.error.HTTPError) else str(exc)
        error_payload = {"job_id": job_id or None, "status": "failed", "resumable": bool(job_id), "errors": [message]}
        write_json(result_json, error_payload)
        if job_id:
            save_resume_state(
                state_paths,
                {
                    "schema_version": 1,
                    "job_id": job_id,
                    "request_fingerprint": fingerprint,
                    "server_url": server_url,
                    "status": "disconnected",
                    "resumable": True,
                    "last_error": message,
                },
            )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
