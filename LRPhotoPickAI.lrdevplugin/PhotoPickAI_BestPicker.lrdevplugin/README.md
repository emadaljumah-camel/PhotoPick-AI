# PhotoPickAI Best Similar Picker

This Lightroom Classic plug-in exports previews, calls a local Python helper,
and applies completed representative/rejected decisions as catalog flags.
It does not invoke the desktop app's original-file mover.

See [complete setup](../../PhotoPickAI/README_Lightroom_Server.md#helper-and-plug-in)
and the [first-pair walkthrough](../../PhotoPickAI/README_Lightroom_Server.md#first-pair-walkthrough).

## Installation and defaults

Add this complete folder through File > Plug-in Manager. Select
**03 PhotoPickAI Best Similar Picker** and configure the preferences exposed by
the plug-in.

| Preference | Initial value |
|---|---|
| Server URL | Proxy example `http://localhost:8080/photopick`; direct local server normally uses `http://127.0.0.1:8000` |
| API key | Empty; match the server key if authentication is configured |
| Preview long edge / quality | 1024 pixels / 85 |
| Dry run | Off |
| Auto-reject non-selected | On |
| Timeout | 120 seconds |
| Minimum selected count | 2 |
| Helper Python | `python` on the Lightroom computer's PATH |
| Helper script | Bundled `helper/lr_client_helper.py` |
| Temporary-job root | OS temporary directory/PhotoPickAI_LR_Jobs |

Defaults are in [Settings.lua](Settings.lua). Saved preferences persist until
reset. Do not assume that a Python executable on the server is available on a
separate Lightroom computer. Keep the helper script and executable accessible.

## Commands

- **PhotoPickAI: Submit Selected Photos** exports/saves previews and manifests,
  submits the helper job, and returns after server queue acknowledgment. It does
  not change catalog flags during submission.
- **PhotoPickAI: Check Job Status / Apply Results** checks/recover uploads,
  resumes interrupted/resumable work when requested, validates cached/downloaded
  results, and applies flags for a completed non-dry-run job.

The check command is not a read-only per-photo recommendation preview. It can
set representative photos to Pick and rejected alternatives to Rejected when
automatic rejection was enabled. Existing flags can be replaced; this plug-in
does not provide StockReviewAI's existing-flag protection setting.

Dry-run still exports, uploads, and invokes backend processing. It prevents flag
writes for that submitted job. To apply a dry-run selection, submit a new job
with dry-run disabled. Saved active-job options govern later application.

## State and recovery

The helper persists server identity, preview hashes, fingerprint resume state,
and cached results under the configured job root. The fingerprint is insensitive
to selection order. Incomplete submissions can recover; deliberate new
submissions after completion create fresh jobs.

Use the original catalog and preserve local/server job data until resolved.
Catalog/photo/result checks precede application, but catalog writes and local
completion state are not atomic together. There is no automatic original-flag
snapshot or rollback routine. Inspect uncertain application before retrying.

No server cancellation/deletion endpoint exists. Cancelling an export/helper
dialog or closing local state does not establish that server work stopped.
Deleting recovery data does not undo flags. Use Lightroom's appropriate Undo
tools or catalog backups and verify restoration.

Back up or deliberately relocate temporary-job storage if OS cleanup could
remove recovery state. See [architecture](../../docs/ARCHITECTURE.md),
[updates](../../README.md#updates), and [security](../../SECURITY.md).
