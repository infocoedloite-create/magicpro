"""ONNX Runtime session creation with TensorRT -> CUDA -> CPU fallback."""
import numpy as np
import onnxruntime as ort

from . import config

_preloaded = False


def _preload_cuda():
    # onnxruntime-gpu >= 1.21 can load CUDA/cuDNN DLLs from the nvidia-* pip wheels.
    global _preloaded
    if _preloaded:
        return
    _preloaded = True
    if hasattr(ort, "preload_dlls"):
        try:
            ort.preload_dlls()
        except Exception as e:  # noqa: BLE001
            print(f"[onnx] preload_dlls failed: {e}")


def providers():
    _preload_cuda()
    avail = ort.get_available_providers()
    out = []
    if config.USE_TENSORRT and "TensorrtExecutionProvider" in avail:
        config.TRT_CACHE.mkdir(parents=True, exist_ok=True)
        out.append(("TensorrtExecutionProvider", {
            "device_id": config.GPU_ID,
            "trt_fp16_enable": True,
            "trt_engine_cache_enable": True,
            "trt_engine_cache_path": str(config.TRT_CACHE),
            "trt_timing_cache_enable": True,
            "trt_timing_cache_path": str(config.TRT_CACHE),
            "trt_max_workspace_size": 8 << 30,
        }))
    if "CUDAExecutionProvider" in avail:
        out.append(("CUDAExecutionProvider", {
            "device_id": config.GPU_ID,
            # all our shapes are fixed, so an exhaustive search pays off after the first frame
            "cudnn_conv_algo_search": "EXHAUSTIVE",
            "do_copy_in_default_stream": True,
        }))
    out.append("CPUExecutionProvider")
    return out


def create_session(path):
    so = ort.SessionOptions()
    so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    so.log_severity_level = 3
    return ort.InferenceSession(str(path), sess_options=so, providers=providers())


def input_dtype(node):
    return np.float16 if node.type == "tensor(float16)" else np.float32


def runtime_info():
    _preload_cuda()
    return {
        "onnxruntime": ort.__version__,
        "device": ort.get_device(),
        "available_providers": ort.get_available_providers(),
        "tensorrt_enabled": config.USE_TENSORRT,
    }
