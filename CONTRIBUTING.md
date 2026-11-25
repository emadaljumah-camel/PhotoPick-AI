# Contributing

Read the [mode overview](README.md), [architecture](docs/ARCHITECTURE.md), and
[security/file-safety guide](SECURITY.md). Keep patches focused, preserve unrelated
work, and discuss major behavior changes before implementation.

## Tests

From the PhotoPickAI directory in the existing environment:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py" -q
.\.venv\Scripts\python.exe -m pip check
```

From the repository root, run `git diff --check`.

Tests use synthetic fixtures/mocked review and cover similarity, semantic
subgroups, ingest, helper submission, and resume. The separate manual recovery
harness starts processes and is not part of ordinary unit discovery. Automated
tests do not establish GUI/Lightroom compatibility or real-model accuracy.

Use disposable photo copies/catalogs for manual checks. Test desktop review
separately from confirmed file moves. For Lightroom, start with a pair in
dry-run, then verify representatives/rejections, existing flags, interrupted
submission, resume, and uncertain application. Record actual Python/model/host
versions and distinguish tests performed from configured options.

## Changes and pull requests

- Explain the concrete problem, changed behavior, and verification.
- Preserve path safeguards, preview hashes, fingerprints, checkpoints, and
  result validation. Add focused failure/retry tests for behavioral changes.
- Keep the source helper and bundled plug-in helper consistent when changing
  their behavior; documentation-only work does not modify either implementation.
- Update docs when model/configuration contracts, retention, networking,
  original moves, or catalog effects change.
- Keep dependency updates separate. Never commit credentials, machine config,
  source photos, private paths, catalogs, generated jobs/caches, or model weights.
- Use synthetic examples and inspect branches/history before sharing artifacts.

Original contributions use the [MIT license](LICENSE). Preserve compatible
third-party licenses/notices. Report vulnerabilities through [SECURITY.md](SECURITY.md).
