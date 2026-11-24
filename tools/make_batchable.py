"""Make a fixed-batch ONNX model accept any batch size (dim 0 -> symbolic 'N').

    python tools/make_batchable.py models/swappers/inswapper_128_fp16.onnx models/batched/inswapper_128_fp16.onnx

Works for models whose graph never hard-codes the batch size in a Reshape; the script verifies by
running batch 1 and batch 4 on CPU and comparing with the original.
"""
import sys

import numpy as np
import onnx
import onnxruntime as ort


def make_dynamic(src, dst):
    m = onnx.load(src)
    names = {i.name for i in m.graph.initializer}
    for v in list(m.graph.input) + list(m.graph.output):
        if v.name in names:
            continue
        d = v.type.tensor_type.shape.dim
        if len(d):
            d[0].ClearField("dim_value")
            d[0].dim_param = "N"
    # drop stale intermediate shape annotations that still say batch 1
    del m.graph.value_info[:]
    onnx.save(m, dst)


def verify(src, dst, batch=4):
    a = ort.InferenceSession(src, providers=["CPUExecutionProvider"])
    b = ort.InferenceSession(dst, providers=["CPUExecutionProvider"])
    rng = np.random.default_rng(0)
    feeds = []
    for _ in range(batch):
        f = {}
        for i in a.get_inputs():
            shape = [1] + [s if isinstance(s, int) else 64 for s in i.shape[1:]]
            f[i.name] = rng.random(shape, dtype=np.float32)
        feeds.append(f)
    ref = [a.run(None, f)[0] for f in feeds]
    out = b.run(None, {k: np.concatenate([f[k] for f in feeds]) for k in feeds[0]})[0]
    err = max(float(np.abs(out[i:i + 1] - ref[i]).max()) for i in range(batch))
    return err


if __name__ == "__main__":
    make_dynamic(sys.argv[1], sys.argv[2])
    print(f"max difference batch vs single: {verify(sys.argv[1], sys.argv[2]):.2e}")
