"""Load every model once and build the TensorRT engines, so the first stream after an install
(or on a fresh GPU) starts in seconds instead of minutes.

    .venv/bin/python tools/warmup.py

Engines land in models/.trt_cache, which the running service shares; it needs no restart.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402

from app import config  # noqa: E402
from app.enhancer import FaceEnhancer  # noqa: E402
from app.pipeline import Engine  # noqa: E402
from app.swapper import FaceSwapper  # noqa: E402

failed = 0


def timed(label, fn):
    global failed
    t = time.perf_counter()
    try:
        fn()
        print(f"ok    {label:34} {time.perf_counter() - t:6.1f}s", flush=True)
    except Exception as e:  # report and go on with the other models
        failed += 1
        print(f"FAIL  {label:34} {e}", flush=True)


e = Engine()
frame = np.zeros((1080, 1920, 3), np.uint8)
timed("detector (det_10g)", lambda: e.detector.detect(frame))
timed("recognizer (w600k_r50)", lambda: e.recognizer)
timed("landmarks (2d106det)", lambda: e.landmarks and e.landmarks.detect(frame, np.array([760, 340, 1160, 840], np.float32)))
for name in sorted(p.stem for p in config.SWAPPERS.glob("*.onnx")):
    timed(f"swapper {name}", lambda n=name: e._get(e._swappers, config.SWAPPERS, FaceSwapper, n))
for name in sorted(p.stem for p in config.ENHANCERS.glob("*.onnx")):
    timed(f"enhancer {name}", lambda n=name: e._get(e._enhancers, config.ENHANCERS, FaceEnhancer, n))
print("warm-up done" if not failed else f"warm-up done, {failed} failed", flush=True)
sys.exit(1 if failed else 0)
