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


def load_completed_result(path: Path, job_id: str) -> dict | None:
    try:
        result = read_json(path)
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if result.get("status") == "completed" and str(result.get("job_id", "")) == job_id:
        return result
    return None


def save_result(result_json: Path, completed_result: Path, payload: dict) -> None:
    write_json(result_json, payload)
    if payload.get("status") == "completed":
        write_json(completed_result, payload)


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
    parser.add_argument("--action", choices=("submit", "check", "resume"), required=True)
    args = parser.parse_args()

    server_url = args.server_url.rstrip("/")
    preview_folder = Path(args.preview_folder)
    result_json = Path(args.result_json)
    completed_result = result_json.with_name("completed_result.json")
    job_payload = read_json(Path(args.job_json))
    local_state = result_json.with_name("job_state.json")
    if args.action in {"check", "resume"}:
        try:
            state = read_json(local_state)
            fingerprint = str(state.get("request_fingerprint", ""))
            if not fingerprint or not state.get("job_id"):
                raise ValueError("PhotoPickAI local job state is missing its job ID or request fingerprint")
        except Exception as exc:
            write_json(
                result_json,
                {"job_id": None, "status": "failed", "resumable": False, "errors": [str(exc)]},
            )
            return 1
    else:
        job_payload = add_preview_hashes(job_payload, preview_folder)
        fingerprint = resume_fingerprint(job_payload)
        state = None
    shared_state = preview_folder.parent.parent / ".resume" / f"{fingerprint}.json"
    state_paths = [local_state, shared_state]
    if state is None:
        state = load_resume_state(state_paths, fingerprint, server_url)
        if args.action == "submit" and state and state.get("status") == "completed":
            state = None
    job_id = str(state["job_id"]) if state else ""

    try:
        if args.action in {"check", "resume"}:
            status = request_json("GET", f"{server_url}/jobs/{job_id}/status", None, args.api_key, args.timeout)
            if args.action == "resume" and status.get("status") != "completed":
                status = request_json(
                    "POST",
                    f"{server_url}/jobs/{job_id}/run",
                    {"mode": "group_and_pick"},
                    args.api_key,
                    args.timeout,
                )
            if status.get("status") == "completed":
                status = request_json("GET", f"{server_url}/jobs/{job_id}/result", None, args.api_key, args.timeout)
            save_result(result_json, completed_result, status)
            save_resume_state(
                state_paths,
                {
                    "schema_version": 1,
                    "job_id": job_id,
                    "request_fingerprint": fingerprint,
                    "server_url": server_url,
                    "status": status.get("status", "unknown"),
                    "resumable": bool(status.get("resumable", status.get("status") != "completed")),
                },
            )
            return 0

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
            save_result(result_json, completed_result, result)
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
            save_result(result_json, completed_result, run_response)
            save_resume_state(state_paths, {"schema_version": 1, "job_id": job_id, "request_fingerprint": fingerprint, "server_url": server_url, "status": "completed", "resumable": False})
            return 0
        if args.action == "submit":
            write_json(result_json, run_response)
            save_resume_state(
                state_paths,
                {
                    "schema_version": 1,
                    "job_id": job_id,
                    "request_fingerprint": fingerprint,
                    "server_url": server_url,
                    "status": run_response.get("status", "running"),
                    "resumable": True,
                },
            )
            return 0

        raise RuntimeError("PhotoPickAI submit returned without a job status")
    except Exception as exc:
        cached_result = load_completed_result(completed_result, job_id)
        if args.action in {"check", "resume"} and cached_result is not None:
            write_json(result_json, cached_result)
            return 0
        message = http_error_message(exc) if isinstance(exc, urllib.error.HTTPError) else str(exc)
        job_missing = (
            args.action in {"check", "resume"}
            and isinstance(exc, urllib.error.HTTPError)
            and exc.code == 404
        )
        error_status = "missing" if job_missing else "failed"
        error_payload = {
            "job_id": job_id or None,
            "status": error_status,
            "resumable": bool(job_id) and not job_missing,
            "errors": [message],
        }
        write_json(result_json, error_payload)
        if job_id:
            save_resume_state(
                state_paths,
                {
                    "schema_version": 1,
                    "job_id": job_id,
                    "request_fingerprint": fingerprint,
                    "server_url": server_url,
                    "status": error_status if job_missing else "disconnected",
                    "resumable": not job_missing,
                    "last_error": message,
                },
            )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
