# PhotoPick AI

PhotoPick AI is a local Windows desktop MVP for reviewing near-duplicate JPG/JPEG photos in a single folder. It scans one folder, extracts metadata, generates cached previews, groups very similar photos, and lets you choose the preferred image from each group.

## MVP Features

- Scans only the selected folder, not subfolders
- Reads EXIF metadata with ExifTool when available
- Falls back to file modified time when capture time is missing
- Generates cached preview JPGs for fast review
- Computes local similarity using a compact visual vector, perceptual hash, file-size similarity, and sharpness score
- Builds conservative duplicate/burst groups from a similarity graph, with unmatched photos kept as single-photo groups
- Suggests a default winner based on sharpness, file size, and capture time
- Restores reviewed and skipped state for saved folders
- Provides a PySide6 review UI with cached thumbnails and keyboard shortcuts
- Provides optional local Vision LLM review through LM Studio or Ollama-compatible localhost APIs
- Provides a `Run Vision LLM All` button after grouping
- Provides a `Process Photos` automation button that reuses existing groups/selections when available, then moves originals into selected/rejected folders
- Saves grouping and review results to JSON and CSV

## Project Structure

```text
PhotoPickAI/
  app/
    main.py
    config.py
    core/
      scanner.py
      metadata_reader.py
      preview_generator.py
      similarity_engine.py
      duplicate_grouper.py
      sharpness.py
      review_store.py
      models.py
    ui/
      main_window.py
      duplicate_review_widget.py
      group_list_panel.py
      preview_grid.py
      settings_panel.py
    utils/
      file_utils.py
      time_utils.py
      json_utils.py
  workspace/
    cache/
      previews/
      metadata/
      similarity/
      review/
  requirements.txt
  README.md
```

## Setup

1. Install Python 3.11 or newer on Windows.
2. Optional but recommended: install ExifTool and add it to your `PATH`.
3. Create and activate a virtual environment.
4. Install dependencies:

```powershell
python -m pip install -r requirements.txt
```

## Run

From the `PhotoPickAI` directory:

```powershell
python -m app.main
```

## Workflow

1. Click `Select Folder` and choose one folder containing JPG/JPEG files.
2. Click `Find Similar Photos`.
3. Wait for metadata extraction, preview generation, and grouping to finish.
4. Click `Run Vision LLM All` to let the local vision model select keepers, or manually review each group.
5. Click `Process Photos` to move selected originals to `Selected Photos` and the remaining originals to `Rejected Photos`.
6. Review decisions are saved automatically.

## Process Photos Automation

`Process Photos` is the automated workflow. It:

- Uses already loaded groups if `Find Similar Photos` has already been run
- Runs the same scan/grouping flow as `Find Similar Photos` only when no groups are loaded
- Runs `Run Vision LLM All` behavior only for groups that do not already have a selection
- Automatically selects one-photo groups without sending them to the Vision LLM
- Saves review decisions to JSON and CSV
- Moves selected original files into `Selected Photos`
- Moves non-selected original files into `Rejected Photos`
- Assigns every JPG/JPEG in the selected folder to a group, including one-photo groups

This button intentionally moves original files. The manual `Find Similar Photos` review workflow does not move originals.

## Optional Vision LLM

PhotoPick AI can ask a local Vision LLM to inspect a group and recommend which photos to keep. This is optional and uses an LM Studio local endpoint by default:

```text
http://127.0.0.1:1234/v1/chat/completions
```

Default model name:

```text
qwen2.5-vl-7b-instruct
```

Install and run a local vision model separately, then update the endpoint/model in `Settings` if needed. The Vision LLM path sends cached preview images only to the configured local endpoint.

For LM Studio, load a vision-capable model such as `Qwen2.5-VL-7B-Instruct`, start the local server, and use the `/v1/chat/completions` endpoint. Ollama-style `/api/chat` endpoints are also supported.

## Keyboard Shortcuts

- `1..9`: pick that image in the current group
- `Enter`: confirm current group
- `S`: skip current group
- `K`: pick the suggested winner
- `V`: run local Vision LLM on the current group
- `Right Arrow`: next group
- `Left Arrow`: previous group

## Output Files

PhotoPick AI writes caches and review data under `workspace/cache/`:

- `previews/<folder-key>/`: cached preview JPGs
- `metadata/<folder-key>.json`: scanned metadata manifest
- `similarity/<folder-key>_groups.json`: duplicate groups
- `review/<folder-key>_review.json`: review decisions
- `review/<folder-key>_review.csv`: review decisions in CSV form

When using manual review, original photos are not moved, renamed, deleted, or modified. When using `Process Photos`, reviewed originals are moved into `Selected Photos` and `Rejected Photos` subfolders inside the selected folder.

## JSON Schemas Used

Metadata cache example:

```json
{
  "images": [
    {
      "id": "17a6f0d99b7c",
      "filename": "DSC_4213.jpg",
      "path": "H:/Photos/DSC_4213.jpg",
      "file_size": 3812451,
      "width": 6000,
      "height": 4000,
      "orientation": "landscape",
      "capture_time": "2026-04-23T14:22:03",
      "preview_path": "H:/Projects/PhotoPick-AI/PhotoPickAI/workspace/cache/previews/abc123/17a6f0d99b7c.jpg",
      "phash": "010101...",
      "sharpness_score": 114.24,
      "similarity_vector": [],
      "modified_time": "2026-04-23T14:22:09",
      "group_id": null
    }
  ]
}
```

Grouping result example:

```json
{
  "groups": [
    {
      "group_id": "G001",
      "images": [],
      "capture_span_seconds": 3,
      "avg_similarity": 0.9574,
      "suggested_best": "DSC_4213.jpg",
      "user_selected": [],
      "status": "Pending"
    }
  ]
}
```

Review decision example:

```json
{
  "decisions": [
    {
      "group_id": "G014",
      "winner": "DSC_4213.jpg",
      "alternates": [],
      "rejected": [
        "DSC_4211.jpg",
        "DSC_4212.jpg",
        "DSC_4214.jpg"
      ],
      "reviewed": true,
      "status": "Reviewed"
    }
  ]
}
```

## Notes

- ExifTool is optional; the app handles its absence gracefully.
- Similarity is intentionally optimized for local near-duplicates and burst shots, not full semantic scene understanding.
- Thresholds are intentionally conservative to avoid over-grouping different compositions.
- Similarity computation uses cached previews for speed; winner selection remains fully local.
- This is Feature 1 only: local grouping and manual review.
