"""Paths and runtime switches. Override with environment variables."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODELS = ROOT / "models"
SWAPPERS = MODELS / "swappers"      # drop any InSwapper-style .onnx here
ENHANCERS = MODELS / "enhancers"    # GFPGAN / GPEN / CodeFormer .onnx here
DETECTOR = MODELS / "det_10g.onnx"  # SCRFD 10G (insightface buffalo_l)
RECOGNIZER = MODELS / "w600k_r50.onnx"  # ArcFace R50 (insightface buffalo_l)
TRT_CACHE = MODELS / ".trt_cache"
LIBRARY = Path(os.environ.get("MAGIC_LIBRARY", ROOT / "library"))
STATIC = ROOT / "static"
SETTINGS_FILE = Path(os.environ.get("MAGIC_SETTINGS", ROOT / "settings.json"))

GPU_ID = int(os.environ.get("MAGIC_GPU", "0"))
# TensorRT gives the lowest latency on RTX cards but needs TensorRT 10 installed.
# First start builds engines (minutes); they are cached in models/.trt_cache.
USE_TENSORRT = os.environ.get("MAGIC_TRT", "0") == "1"
HOST = os.environ.get("MAGIC_HOST", "127.0.0.1")
PORT = int(os.environ.get("MAGIC_PORT", "7860"))

for d in (SWAPPERS, ENHANCERS, LIBRARY):
    d.mkdir(parents=True, exist_ok=True)
