# Third-party requirements and notices

The [MIT license](LICENSE) covers original project code and documentation.
It does not relicense dependencies, model weights, services, Adobe software,
or photographs/reference data.

## Dependencies

[Desktop requirements](PhotoPickAI/requirements.txt) include PySide6, Pillow,
OpenCV, and NumPy. [Server requirements](PhotoPickAI/server/requirements.txt)
include FastAPI, Uvicorn, python-multipart, and PyYAML. Transitive dependencies
have their own licenses too. Review metadata and bundled notices for the actual
versions you redistribute; requirement ranges are not a full locked inventory.

PySide6/Qt has its own licensing and distribution obligations. The repository's
MIT license does not replace those obligations. Review your chosen Qt licensing
and packaging arrangement before distributing an application bundle.

ExifTool is an optional separately obtained tool. LM Studio, Ollama, and models
have separate licenses and runtime/hardware requirements. Example model IDs are
not grants to redistribute weights or guarantees of image support.

## Lightroom and user content

Adobe Lightroom Classic is proprietary software, installed/licensed separately.
Adobe and Lightroom are Adobe trademarks. This project is independent and not
endorsed by Adobe or model/runtime providers. No Lightroom or model binaries are
bundled as part of this source documentation work.

Users remain responsible for rights to photos and generated/exported material.
Grouping and keeper judgments are not stock-agency acceptance, factual/legal
clearance, or a guarantee that moving files is appropriate for every workflow.
The maintainer remains responsible for provenance/attribution of contributed
or reused code and assets; this notice does not override third-party terms.
