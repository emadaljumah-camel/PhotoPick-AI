# Security, file safety, and privacy

## Files and catalog changes

Desktop Process Photos moves originals after confirmation. It is not a
preview-only operation or a reversible batch transaction. Use disposable copies
first and keep backups. Earlier moves can remain after a later failure; suffixes
can change destination filenames. Do not run it on a catalog-managed folder
without accounting for Lightroom's stored paths.

The Lightroom integration writes Pick/Rejected flags, not file moves. Existing
flags can be overwritten. It has no automatic original-flag snapshot/rollback
or crash-safe per-photo application journal. Server results and local completion
state are not one transaction with catalog writes.

## Server access

Job routes use X-PhotoPickAI-Key only if the configured API key is nonempty.
Otherwise they permit access without authentication. Health is public.
PHOTOPICKAI_API_KEY overrides the YAML key, even if explicitly empty.
Matching one shared key does not establish separate user/job ownership.

There is no built-in TLS. The supplied server launcher explicitly binds
0.0.0.0:8000, which can expose it to other network interfaces. Prefer the direct
loopback Uvicorn command for one computer. On trusted networks restrict firewall
access, configure a nonempty key, and provide appropriate encryption/access
controls. Do not expose the API or model runtime directly to the public internet.
The proxy URL in defaults does not configure or secure a proxy automatically.

## Credentials and local helper

Keep machine configuration in ignored local settings, not committed examples.
Use an environment-provided server key and matching Lightroom preferences.
There is no generic `.env` loader here. Do not copy credentials into issues,
logs, shell captures, shared manifests, or public URLs.

The helper command can include the API key as a command-line argument. Local
process inspection and Lightroom preferences are therefore relevant exposure
surfaces; these are not an OS secret vault. Use only a trusted helper script and
Python executable, and restrict access to job folders and preferences.

Ignore rules do not remove secrets or private network details from historical
commits. Rotate exposed credentials and assess history separately before
publication. A clean current-file scan is not a history-removal operation.

## Data and retention

Workspace caches/review exports, local helper jobs, backend previews/grouping,
checkpoints/results, logs, filenames, EXIF, and original/catalog paths can be
sensitive. Review artifacts before sharing; previews are not automatically
anonymous. Model requests use cached/exported previews, and a remote endpoint
still receives image content and prompts even if original source bytes are not
uploaded directly.

Desktop settings default to PhotoPickAI/workspace/settings.json; backend data
defaults to PhotoPickAI/server/jobs. Lightroom jobs default to the OS temporary
folder/PhotoPickAI_LR_Jobs. OS cleanup can remove temporary recovery data; choose
and protect persistent storage when needed.

There is no application-level encryption. The server reads keep_jobs_days but
does not implement automatic expiry. Retained data must be managed deliberately.
Do not delete active recovery data or assume deleting a job reverses moves/flags.
Stop/resolve work before consistent backups or manual maintenance.

## Assessment limits and reporting

Dependency versions are unchanged by documentation preparation. Tests and secret
scans do not establish vulnerability clearance. Review installed packages before
processing untrusted images or widening network exposure.

Use GitHub's private Report a vulnerability option if enabled. Otherwise request
a private contact channel without exploit details, keys, catalogs, photos, or
personal data. Share impact, affected commit/version, and a sanitized reproduction
over that channel. No guaranteed response time or old-version maintenance is
promised.
