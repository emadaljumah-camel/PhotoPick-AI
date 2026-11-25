# Lightroom, Python helper, and server

This mode compares Lightroom-exported previews and applies Pick/Rejected flags.
It does **not** invoke the desktop app's file mover. See the
[root overview](../README.md) and [architecture](../docs/ARCHITECTURE.md).

## Server setup

From the repository root:

```powershell
cd PhotoPickAI
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install -r server\requirements.txt
```

For an existing environment, preserve installed versions and review any
dependency changes separately.

Create or edit the ignored local `photopickai_server_config.yaml` in this
directory. Do not overwrite an existing configuration. Minimal example:

```yaml
server:
  host: "127.0.0.1"
  port: 8000
model:
  provider: "lmstudio"
  base_url: "http://127.0.0.1:1234/v1"
  model_name: "YOUR-EXACT-LOADED-VISION-MODEL-ID"
  timeout_seconds: 180
storage:
  keep_jobs_days: 7
```

Replace the model placeholder with your runtime's exact loaded image-capable ID.
The current server adapter appends `/chat/completions` to `base_url`. The
desktop reviewer's Ollama endpoint support does not establish interchangeable
server provider behavior.

`server/config.py` reads this fixed YAML path. There is no generic `.env` loader
or external-config environment selector. `PHOTOPICKAI_API_KEY` specifically
overrides the YAML server key, including when the environment value is empty.
Other settings come from YAML/defaults. Relative `jobs_dir` overrides use normal
working-directory resolution; the built-in job root is `server/jobs/` here.

The `keep_jobs_days` setting is read but no automatic expiration/cleanup is
implemented. Do not assume jobs are deleted after seven days.

## Bind and authentication

From `PhotoPickAI`:

```powershell
.\.venv\Scripts\python.exe -m uvicorn server.main:app --host 127.0.0.1 --port 8000
```

The provided `run_server.bat` explicitly binds all interfaces on port 8000,
regardless of a loopback host value in YAML. Use explicit command-line bind
values to control network exposure.

Check `http://127.0.0.1:8000/health`. It reports process availability, not model
readiness. With a configured nonempty key, protected job requests require the
`X-PhotoPickAI-Key` header. Set the same key in the plug-in. Health remains
public. Empty key means authentication is bypassed.

For example, set a secret in the launching environment before starting Uvicorn:

```powershell
$env:PHOTOPICKAI_API_KEY = "YOUR-PRIVATE-KEY"
```

The placeholder is not a secure generated key. Keep real values out of tracked
files and shared console captures. There is no built-in TLS or user-level
authorization.

## Helper and plug-in

1. On the Lightroom computer, install a supported Python interpreter.
2. Add the complete
   [PhotoPickAI_BestPicker.lrdevplugin](../LRPhotoPickAI.lrdevplugin/PhotoPickAI_BestPicker.lrdevplugin/)
   folder in **File > Plug-in Manager**.
3. Select **03 PhotoPickAI Best Similar Picker** and configure its preferences.
4. Set Server URL to `http://127.0.0.1:8000` for a direct local server. The initial
   `http://localhost:8080/photopick` is an external proxy example, not a server
   started by this repository.
5. Configure Helper Python to an executable available on this computer. It
   defaults to `python`, which depends on PATH.
6. Keep the helper script pointing to the bundled `helper/lr_client_helper.py`.
   [Helper requirements](../LRPhotoPickAI.lrdevplugin/PhotoPickAI_BestPicker.lrdevplugin/helper/requirements.txt)
   describe its separate dependency expectations.
7. Match the optional API key and choose a persistent temporary-job root if
   OS temporary-folder cleanup would endanger needed recovery files.
8. Enable dry-run for the first test.

The default preview long edge is 1024 pixels, JPEG quality 85, minimum selected
count 2, and timeout 120 seconds. Dry-run defaults off; automatic rejection of
non-selected photos defaults on. Saved preferences are retained until reset.

The helper is launched as a local Python process. It submits/checks/resumes HTTP
jobs on Lightroom's behalf. Both its executable and bundled script must be
accessible on the Lightroom computer, independently of the remote AI server.

## First pair walkthrough

1. In a disposable/backed-up catalog, select at least two similar test photos.
2. Run **Library > Plug-in Extras > PhotoPickAI: Submit Selected Photos**.
3. Wait for successful queued acknowledgment. Submission does not change flags.
4. Keep the server/model running; Lightroom may disconnect after acknowledgment.
5. Reconnect and run **PhotoPickAI: Check Job Status / Apply Results**.
6. Running jobs report progress; interrupted/resumable failures offer Resume Job.
7. Completed results are validated and processed using the saved dry-run choice.
   A dry-run shows results without changing flags.
8. For real flags, submit a new job with dry-run disabled; inspect the resulting
   representatives (Pick) and, if enabled, rejected alternatives (Rejected).

There is no separate per-photo approval screen before flag application. Existing
flags can be overwritten; no existing-Pick protection switch is implemented.
Unlisted/ungrouped/skipped photos are not deliberately assigned arbitrary
verdicts by the application loop.

## Jobs and API

| Method | Route | Purpose |
|---|---|---|
| GET | `/health` | Public process health |
| POST | `/jobs` | Create/reuse request using client fingerprint |
| POST | `/jobs/{job_id}/photos/{photo_id}/preview` | Multipart preview file, validated against request hash |
| GET | `/jobs/{job_id}/uploads` | Acknowledged preview names/hashes |
| POST | `/jobs/{job_id}/run` | Queue/resume `group_and_pick` processing |
| GET | `/jobs/{job_id}/status` | Progress, errors, resumability |
| GET | `/jobs/{job_id}/result` | Validated completion or current state |

There is no server cancel/delete endpoint. Use one Uvicorn process with the
filesystem store. The app declares `root_path="/photopick"` for proxy metadata;
a proxy base path must be stripped before forwarding. Direct clients use the
routes above without that prefix.

Helper fingerprints include selection/settings/preview hashes and do not
depend on photo order. Incomplete work can recover its server identity and
upload only needed previews. A deliberate submission after completion creates
a fresh review, allowing changed prompts/models to produce new results.

Server checkpoints persist grouping and completed group decisions. Startup
marks stale running jobs interrupted; resume reuses completed groups. Results
are cached locally for flag-application retry. One active client pointer tracks
the current catalog/job.

## Recovery and limitations

Do not delete the local temporary-job root or backend job directory while
recovery is needed. Local state includes `job_state.json`,
`.resume/<fingerprint>.json`, active state, and cached `completed_result.json`.
A missing/non-resumable job can require a deliberate fresh submission.

Catalog identity, photo resolution, membership, and result validity are checked
before flags are applied. The completion marker is saved afterward, not in one
atomic transaction with the catalog. There is no per-photo original-flag
snapshot or automatic flag rollback. Inspect potentially changed photos before
retry; use appropriate Lightroom Undo/catalog backups for recovery.

Closing a local failed job does not cancel already-running backend work.
Stopping the process interrupts execution, not reverses decisions or catalog
changes. Retained originals are not moved or deleted by this integration.

## Two computers and privacy

Use the server's reachable private address instead of loopback on the Lightroom
computer, deliberately bind a reachable server interface, and restrict firewall
access. Configure the same nonempty API key. Transport still needs protection:
keys are not encryption, and job access is not isolated by user.

Previews, EXIF/context, original names/paths, API-key preferences, and diagnostics
can be sensitive. See [SECURITY.md](../SECURITY.md) before exposing a service or
sharing job folders.

## Tests

From `PhotoPickAI`:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py" -q
```

The [manual recovery harness](tests/manual_recovery_e2e.py) starts processes and
uses mocked review for a recovery exercise; it is separate from ordinary unit
discovery and does not establish actual model or Lightroom behavior.
