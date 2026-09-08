# PhotoPick AI

PhotoPick AI is a local Windows desktop app for reviewing JPG/JPEG bursts, exact duplicates, and near-duplicate photo sets. It scans a folder, reads metadata, builds cached previews, groups related images, suggests likely keepers, and can move reviewed originals into `Selected Photos` and `Rejected Photos`.

Everything runs locally by default. Metadata extraction, preview generation, similarity analysis, grouping, and manual review happen on your machine. Optional Vision LLM review can call a local LM Studio or Ollama-compatible endpoint using cached preview images.

## Features

- Scan JPG/JPEG files in a selected folder
- Read EXIF metadata with ExifTool when available
- Fall back to Pillow and file modified time when EXIF data is missing
- Generate cached preview JPGs for faster repeat reviews
- Group exact duplicates, near-duplicates, burst candidates, and single photos
- Score similarity with perceptual hashes, visual descriptors, edge/focus descriptors, file-size similarity, and capture-time proximity
- Suggest a default keeper from sharpness, file size, and capture time
- Resume saved grouping and review state for previously scanned folders
- Review manually with keyboard shortcuts and support for multiple keepers
- Run optional Vision LLM review for one group or all groups
- Process the full folder by saving decisions and moving originals
- Export review results to JSON and CSV

## Requirements

- Windows
- Python 3.11 or newer
- Optional: ExifTool on `PATH` for stronger metadata extraction
- Optional: LM Studio or an Ollama-compatible local vision model endpoint

Python packages are listed in [requirements.txt](requirements.txt):

- PySide6
- Pillow
- OpenCV
- NumPy

## Setup

From the repository root:

```powershell
cd PhotoPickAI
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

If PowerShell blocks virtual environment activation, allow local scripts for the current user:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

## Run

From the repository root, you can use:

```powershell
.\run_app.bat
```

Or run the app directly from the `PhotoPickAI` directory:

```powershell
python -m app.main
```

The app creates its `workspace` folders automatically on startup.

## Basic Workflow

1. Click `Select Folder` and choose a folder containing JPG/JPEG files.
2. Click `Find Similar Photos`.
3. Review each group manually, or click `Run Vision LLM All` if you have a local vision endpoint configured.
4. Confirm, skip, or choose one or more keepers for each group.
5. Click `Process Photos` only when you are ready for the app to move original files.

Manual review saves decisions but does not move, rename, delete, or modify original photos.

## Process Photos

`Process Photos` is the automated finishing workflow. It shows a confirmation dialog because it moves original files.

It will:

- Use already loaded groups when available
- Scan and group the selected folder first when no groups are loaded
- Run Vision LLM review only for multi-photo groups that still need a selection
- Automatically select single-photo groups
- Save review decisions to JSON and CSV
- Move selected originals into `Selected Photos`
- Move non-selected originals into `Rejected Photos`
- Skip unsafe, missing, or already-moved source files

The destination folders are created inside the selected photo folder. If a destination filename already exists, the app appends a numeric suffix such as `_001`.

## Optional Vision LLM

PhotoPick AI can ask a local Vision LLM to split broad burst groups into visual subgroups and choose one representative photo from each subgroup.

Default endpoint:

```text
http://127.0.0.1:1234/v1/chat/completions
```

Default model:

```text
qwen2.5-vl-7b-instruct
```

For LM Studio, load a vision-capable model, start the local server, and keep the `/v1/chat/completions` endpoint. Ollama-style `/api/chat` endpoints are also supported. You can update the endpoint, model name, and timeout in `Settings`.

Only cached preview images are sent to the configured endpoint. Original files are not uploaded by PhotoPick AI.

## Lightroom Classic Integration

PhotoPick AI also includes an asynchronous Lightroom Classic workflow through a local server. The Lightroom plugin exports resized JPEG previews, submits them, and returns after the server queues the job. `PhotoPickAI: Check Job Status / Apply Results` later reports progress, resumes interrupted work, or validates and applies the completed pick/reject decisions.

See [README_Lightroom_Server.md](README_Lightroom_Server.md) for setup and troubleshooting.

Small-job summaries list each group, the picked photo, and all photos in that group. Large-job dialogs are compact; their full report is retained in the local job's `completed_result.json`:

```text
G001_01
Picked: DSC_1235.NEF
Photos:
  DSC_1234.NEF
  DSC_1235.NEF
```

The summary uses original Lightroom filenames when Lightroom provides them. Preview JPEGs are named from the original filename stem, such as `DSC_1234.jpg`, while original files are not uploaded or modified.

## Keyboard Shortcuts

- `1..9`: pick that image in the current group
- `Enter`: confirm current group
- `S`: skip current group
- `K`: pick the suggested keeper
- `V`: run Vision LLM for the current group
- `Right Arrow`: next group
- `Left Arrow`: previous group

Use `Keep multiple` when a group contains more than one photo worth keeping.

## Project Structure

```text
PhotoPick-AI/
  run_app.bat
  PhotoPickAI/
    app/
      main.py
      config.py
      core/
        duplicate_grouper.py
        local_ai_reviewer.py
        metadata_reader.py
        models.py
        photo_mover.py
        preview_generator.py
        review_store.py
        scanner.py
        sharpness.py
        similarity_engine.py
        vision_llm_reviewer.py
      ui/
        duplicate_review_widget.py
        group_list_panel.py
        main_window.py
        preview_grid.py
        settings_panel.py
      utils/
        file_utils.py
        json_utils.py
        time_utils.py
    workspace/
      cache/
        metadata/
        previews/
        review/
        similarity/
      settings.json
    requirements.txt
    README.md
```

`workspace/settings.json` is created after settings are saved.

## Output Files

PhotoPick AI writes cache and review files under `workspace/cache/`:

- `previews/<folder-key>/`: cached preview JPGs
- `metadata/<folder-key>.json`: scanned image metadata
- `similarity/<folder-key>_groups.json`: grouped photos and analysis state
- `review/<folder-key>_review.json`: review decisions
- `review/<folder-key>_review.csv`: review decisions in CSV form

Folder keys are derived from the selected folder path, so the same folder can be resumed later with `Resume Saved Review`.

## Troubleshooting

- If no capture time is found, file modified time is used.
- If ExifTool is not installed, the app still runs with Pillow and file metadata.
- If Vision LLM review fails, `Process Photos` falls back to the local suggested keeper for that group.
- If groups look too broad, lower the time windows or raise the similarity threshold in `Settings`.
- If groups look too fragmented, raise the time windows or lower the similarity threshold in `Settings`.
- If you moved photos with `Process Photos`, review the `Selected Photos` and `Rejected Photos` folders inside the selected source folder.

## Notes

- The similarity system is optimized for local bursts and near-duplicates, not full semantic photo organization.
- Thresholds are intentionally conservative to reduce accidental grouping of different compositions.
- Similarity computation uses cached previews for speed; sharpness scoring reads the original file.
- Vision LLM review is optional and depends on the model quality, context window, and endpoint configuration.
