from __future__ import annotations

import argparse
import json
import mimetypes
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
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


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
    job_payload = read_json(Path(args.job_json))

    try:
        created = request_json("POST", f"{server_url}/jobs", job_payload, args.api_key, args.timeout)
        job_id = created["job_id"]

        for photo in job_payload.get("photos", []):
            photo_id = str(photo["photo_id"])
            preview_path = preview_folder / photo["preview_filename"]
            if not preview_path.exists():
                raise FileNotFoundError(f"Missing preview: {preview_path}")
            encoded_photo_id = urllib.parse.quote(photo_id, safe="")
            upload_file(f"{server_url}/jobs/{job_id}/photos/{encoded_photo_id}/preview", preview_path, args.api_key, args.timeout)

        request_json("POST", f"{server_url}/jobs/{job_id}/run", {"mode": "group_and_pick"}, args.api_key, args.timeout)

        for _ in range(args.max_poll_attempts):
            status = request_json("GET", f"{server_url}/jobs/{job_id}/status", None, args.api_key, args.timeout)
            if status.get("status") in {"completed", "failed"}:
                break
            time.sleep(args.poll_interval)
        else:
            raise TimeoutError("PhotoPickAI processing timed out")

        result = request_json("GET", f"{server_url}/jobs/{job_id}/result", None, args.api_key, args.timeout)
        write_json(result_json, result)
        return 0 if result.get("status") == "completed" else 2
    except Exception as exc:
        write_json(result_json, {"status": "failed", "errors": [str(exc)]})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
