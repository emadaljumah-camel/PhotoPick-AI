from __future__ import annotations

import logging
from pathlib import Path

import cv2
import numpy as np

LOGGER = logging.getLogger(__name__)


def estimate_sharpness(image_path: Path) -> float:
    """Estimate image sharpness with variance of Laplacian."""

    image = cv2.imread(str(image_path), cv2.IMREAD_GRAYSCALE)
    if image is None:
        LOGGER.warning("OpenCV could not read %s for sharpness estimation", image_path)
        return 0.0
    variance = cv2.Laplacian(image, cv2.CV_64F).var()
    return float(np.round(variance, 3))
