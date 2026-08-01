# PhotoPickAI Lightroom Server Integration

This adds a Lightroom Classic client workflow without changing the existing PhotoPickAI desktop app.

## Pieces

- Lightroom plugin source: `D:\Projects\PhotoPick-AI\LRPhotoPickAI.lrdevplugin\PhotoPickAI_BestPicker.lrdevplugin`
- Lightroom deployment package: `D:\Projects\PhotoPick-AI\dist\PhotoPickAI_BestPicker.lrdevplugin.zip`
- Bundled Lightroom helper: `PhotoPickAI_BestPicker.lrdevplugin\helper\lr_client_helper.py`
- Server: `D:\Projects\PhotoPick-AI\PhotoPickAI\server`
- Source client helper: `D:\Projects\PhotoPick-AI\PhotoPickAI\client_helper\lr_client_helper.py`
- Config: `D:\Projects\PhotoPick-AI\PhotoPickAI\photopickai_server_config.yaml`

The Lightroom plugin exports resized JPEG previews, writes `job.json`, runs the Python helper, reads `result.json`, validates it, and only then applies Lightroom pick/reject flags.

Preview files are named from the original Lightroom filename when Lightroom exposes it. For example, `DSC_1234.NEF` is exported as `DSC_1234.jpg` for server review. The original filename is also written to `job.json` and used in the Lightroom summary dialog.

## Server Setup

From `D:\Projects\PhotoPick-AI\PhotoPickAI`:

```powershell
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m pip install -r server\requirements.txt
uvicorn server.main:app --host 0.0.0.0 --port 8000
```

Health check:

```powershell
curl http://100.108.139.62:8080/photopick/health
```

## Lightroom Setup

1. Open Lightroom Classic.
2. Go to `File > Plug-in Manager`.
3. Add `D:\Projects\PhotoPick-AI\LRPhotoPickAI.lrdevplugin\PhotoPickAI_BestPicker.lrdevplugin` for development, or unzip `dist\PhotoPickAI_BestPicker.lrdevplugin.zip` and add the unzipped `PhotoPickAI_BestPicker.lrdevplugin` folder for deployment.
4. Set `Server URL` to `http://100.108.139.62:8080/photopick`.
5. Run `Library > Plug-in Extras > PhotoPickAI: Pick Best Among Similar Photos`.

Defaults are stored with Lightroom plugin preferences. Dry run is enabled by default.

Fresh deployment installs default to `http://100.108.139.62:8080/photopick` and the helper script bundled inside the `.lrdevplugin` folder. If an older install still shows local development paths, click `Reset to Default Values` in the plugin settings or update the fields manually.

## Network Setup

On the AI workstation:

```powershell
uvicorn server.main:app --host 0.0.0.0 --port 8000
```

On the Lightroom computer, set the plugin `Server URL` preference to:

```text
http://100.108.139.62:8080/photopick
```

Open firewall access for the deployed server port on the AI workstation. For the deployment URL above, that is TCP port `8080`.

## API Key

Set `server.api_key` in `photopickai_server_config.yaml`, or set environment variable `PHOTOPICKAI_API_KEY`.

The Lightroom helper sends:

```text
X-PhotoPickAI-Key: configured-key
```

If the server key is empty, requests are allowed.

## Dry Run

Dry run does not change Lightroom flags. It shows the same group summary that a normal run would show, but no Lightroom pick/reject flags are changed. Turn `dry_run` off only after confirming the server result looks right.

## Lightroom Summary

After validation, Lightroom shows a summary dialog with counts and group membership. The summary does not include AI reasons or confidence scores.

Example:

```text
Groups: 1
Picked flags applied: 1
Rejected flags applied: 1
Skipped: 0
Errors: 0

Groups:
G001_01
Picked: DSC_1235.NEF
Photos:
  DSC_1234.NEF
  DSC_1235.NEF
```

The `Picked:` line names the photo selected for that group. The `Photos:` list shows the photos in the group without repeating a picked marker.

## Safety

- Original images are never uploaded.
- Original files are never modified, moved, deleted, or metadata-written by this integration.
- The server only receives preview JPEGs.
- Lightroom only changes catalog pick/reject flags after result validation.
- Summary filenames come from Lightroom metadata when available; if Lightroom cannot provide an original filename, the plugin falls back to the preview filename.

## Troubleshooting

- Server unreachable: confirm `/health`, IP address, firewall, and `server_url`.
- Wrong API key: confirm the plugin and server values match.
- Timeout: increase `timeout_seconds` or `max_poll_attempts`.
- Missing preview: Lightroom skipped an export; check `plugin.log` under the temp job root.
- Generated-looking names in the summary, such as `photo_0001_260.jpg`, mean Lightroom did not expose the original filename for that photo during export/manifest creation. Check the source file path and Lightroom file metadata.
- Invalid server result: Lightroom refuses to flag anything and logs the validation failure.
- Server logs: `D:\Projects\PhotoPick-AI\PhotoPickAI\server\logs\server.log`
