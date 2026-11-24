"""ONNX Runtime session creation: TensorRT FP16 -> CUDA -> CPU.

TensorRT is used when its libraries are installed (pip package tensorrt-cu12-libs) unless
MAGIC_TRT=0. Engines are built on first load (up to a minute or two per model) and cached in
models/.trt_cache, so later starts are fast. Run tools/warmup.py after install to build them all.
"""
import ctypes
import glob
import os

import numpy as np
import onnxruntime as ort

from . import config

_preloaded = False
_trt_ok = False


def _preload():
    """Load CUDA/cuDNN (from the nvidia-* wheels) and TensorRT libraries once."""
    global _preloaded, _trt_ok
    if _preloaded:
        return
    _preloaded = True
    ort.set_default_logger_severity(3)       # TensorRT prints hundreds of harmless warnings otherwise
    if hasattr(ort, "preload_dlls"):
        try:
            ort.preload_dlls()
        except Exception as e:  # noqa: BLE001
            print(f"[onnx] preload_dlls failed: {e}")
    if config.USE_TENSORRT == "0":
        return
    for site in __import__("site").getsitepackages():
        libdir = os.path.join(site, "tensorrt_libs")
        if not os.path.isdir(libdir):
            continue
        try:
            for name in ("libnvinfer.so.10", "libnvinfer_plugin.so.10", "libnvonnxparser.so.10",
                         "nvinfer_10.dll", "nvinfer_plugin_10.dll", "nvonnxparser_10.dll"):
                hits = glob.glob(os.path.join(libdir, name))
                if hits:
                    ctypes.CDLL(hits[0], mode=getattr(ctypes, "RTLD_GLOBAL", 0))
                    _trt_ok = True
        except OSError as e:
            print(f"[onnx] TensorRT libraries found but could not load: {e}")
            _trt_ok = False
        break


def tensorrt_available():
    _preload()
    return _trt_ok and "TensorrtExecutionProvider" in ort.get_available_providers()


def providers(trt_profile=None, use_trt=True):
    _preload()
    out = []
    if use_trt and tensorrt_available():
        config.TRT_CACHE.mkdir(parents=True, exist_ok=True)
        opts = {
            "device_id": config.GPU_ID,
            "trt_fp16_enable": True,
            "trt_engine_cache_enable": True,
            "trt_engine_cache_path": str(config.TRT_CACHE),
            "trt_timing_cache_enable": True,
            "trt_timing_cache_path": str(config.TRT_CACHE),
            "trt_builder_optimization_level": 3,
        }
        if trt_profile:
            opts.update(trt_profile)
        out.append(("TensorrtExecutionProvider", opts))
    if "CUDAExecutionProvider" in ort.get_available_providers():
        out.append(("CUDAExecutionProvider", {
            "device_id": config.GPU_ID,
            "cudnn_conv_algo_search": "EXHAUSTIVE",
        }))
    out.append("CPUExecutionProvider")
    return out


def trt_profile(shapes):
    """shapes: {input_name: (min, opt, max)} with each a tuple of dims -> TensorRT profile options."""
    fmt = lambda i: ",".join(f"{n}:{'x'.join(str(d) for d in s[i])}" for n, s in shapes.items())  # noqa: E731
    return {"trt_profile_min_shapes": fmt(0), "trt_profile_opt_shapes": fmt(1), "trt_profile_max_shapes": fmt(2)}


def create_session(path, trt_shapes=None, use_trt=True):
    so = ort.SessionOptions()
    so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    so.log_severity_level = 3
    prof = trt_profile(trt_shapes) if trt_shapes else None
    return ort.InferenceSession(str(path), sess_options=so, providers=providers(prof, use_trt))


def input_dtype(node):
    return np.float16 if node.type == "tensor(float16)" else np.float32


def runtime_info():
    _preload()
    return {
        "onnxruntime": ort.__version__,
        "device": ort.get_device(),
        "available_providers": ort.get_available_providers(),
        "tensorrt": tensorrt_available(),
    }
