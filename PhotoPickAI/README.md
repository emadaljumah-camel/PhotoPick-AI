# PhotoPick AI desktop guide

The desktop app scans **JPG/JPEG files directly in one selected folder**,
groups duplicates/related images, suggests keepers, and saves review decisions.
**Process Photos moves originals.** Lightroom server/flagging is a different
workflow; see the [integration guide](README_Lightroom_Server.md).

## Setup

From the repository root:

```powershell
cd PhotoPickAI
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m app.main
```

Use Windows and Python 3.11+ as the app's documented baseline. The current
documentation review ran unit tests on Python 3.14.4; it did not establish a
GUI/platform/version matrix. Direct invocation avoids changing PowerShell's
execution policy or relying on activation.

[requirements.txt](requirements.txt) lists PySide6, Pillow, OpenCV, and NumPy.
ExifTool on PATH is optional for stronger EXIF extraction; Pillow/file modified
time provide fallbacks when metadata is absent. Model weights are not bundled.

## First folder walkthrough

1. Make a disposable copy of a small JPG/JPEG folder.
2. Click **Select Folder**, then **Find Similar Photos**.
3. Inspect each group. Confirm/skip it or choose one or more keepers.
4. Manual review persists choices without moving originals.
5. Optionally configure a Vision model and use **Run Vision LLM All**.
   Inspect its subgroup/keeper choices yourself.
6. Only on the test copy, click **Process Photos**, read its move confirmation,
   then confirm if the displayed workflow matches your intent.
7. Inspect Selected Photos/Rejected Photos and the saved JSON/CSV decisions.

The scanner is not recursive and does not ingest camera RAW through this
desktop folder path. Existing output subfolders are not recursively rescanned.
Lightroom can export other supported originals into previews for its separate
integration; this does not add desktop RAW support.

## Grouping, settings, and optional AI

The shared engine uses capture-time windows, perceptual/visual similarity,
orientation preference, and exact/near-duplicate thresholds to create candidate
groups. Sharpness, file size, and capture time help suggest a keeper. These are
heuristics, not proof that photos are interchangeable or that one is universally
better.

Settings are persisted in `workspace/settings.json`. Initial values include:

| Setting | Default |
|---|---|
| Cached preview long edge | 512 pixels |
| Comparison and burst windows | 20 seconds each |
| Similarity threshold | 0.925 |
| Exact-duplicate threshold | 0.985 |
| Suggest winner / same-orientation preference | Enabled |
| Vision HTTP timeout | 180 seconds |
| Vision endpoint | `http://127.0.0.1:1234/v1/chat/completions` |
| Vision model example | `qwen2.5-vl-7b-instruct`; replace with your loaded image-capable model ID |

Use the app's Settings interface for your actual endpoint/model. LM Studio-style
chat completions and Ollama-style `/api/chat` endpoints are supported by the
desktop reviewer. Server configuration is separate and builds a chat-completions
URL from its base URL; do not copy desktop settings blindly into it.

Vision can split a broad comparison group into semantic subgroups and select
representatives, so more than one keeper can be appropriate. It uses cached
previews, not uploads of original source files. A remote endpoint still sends
those previews off-machine. AI output needs manual review; it does not train
a model or certify stock-agency acceptance.

## Process Photos and file safety

The confirmed finishing workflow can scan first if groups are not loaded,
select single-photo groups, run Vision review on unresolved multi-photo groups,
save decisions to JSON/CSV, then move selected and remaining originals into:

- `Selected Photos`
- `Rejected Photos`

Both are inside the selected source folder. The mover checks that a source is
an existing direct child of that folder, avoids handling the same source twice
within the call, and appends suffixes such as `_001` to avoid overwriting an
existing destination name.

These checks do not make moves atomic. Earlier moves can remain if a later
move fails. There is no dedicated batch undo/move rollback function. File paths
are updated in memory after successful moves. Do not infer that a stale review
cache can automatically reconstruct every original location.

Do not run this workflow over files managed by Lightroom without deliberately
handling catalog paths afterward. It is not the same as flagging a photo.

## Saved state and recovery

Workspace caches store previews, metadata, similarity/grouping, and review
decisions. Reopening a previously scanned folder can resume saved work. Back up
this state and originals before updates or cleanup. Changed files/settings can
invalidate the assumptions behind earlier choices; inspect results after reload.

For partial moves, stop and compare actual file locations against move summaries
and saved review output. Restore from backup or move identified files back
deliberately, handling filename collisions. Cancellation or restarting the app
does not undo moves already completed.

## Troubleshooting

| Symptom | Check |
|---|---|
| GUI cannot import packages | Use `.\.venv\Scripts\python.exe` from this directory and the correct requirements |
| No photos found | Files must be supported JPG/JPEGs directly in the selected folder |
| Unexpected grouping | Inspect capture-time fallback and similarity/window settings |
| Vision unavailable | Load an image-capable model, match the exact ID and endpoint format, inspect errors |
| Files disappeared from source folder | Check Selected Photos/Rejected Photos and move output; Process Photos relocates originals |
| Duplicate filename after move | A suffix may have been added to preserve an existing destination |
| Restored decisions look stale | Check source/settings changes and cached state before processing |

For [updates](../README.md#updates), [architecture](../docs/ARCHITECTURE.md),
[security](../SECURITY.md), and [tests](../CONTRIBUTING.md), see the repository guides.
