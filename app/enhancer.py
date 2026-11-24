"""Face restoration (GFPGAN 1.4 / GPEN-BFR / CodeFormer ONNX) applied after the swap.

If models/batched/<name>.onnx exists (GFPGAN: tools/export_gfpgan_batched.py), faces from all
streams in this process are restored together in one GPU call through a Batcher.
"""
from pathlib import Path

import numpy as np

from .align import estimate_affine, template, warp_face
from .batcher import Batcher
from .blending import box_mask, paste_back
from .landmarks import outline_mask
from .onnx_utils import create_session, input_dtype


# Enhancers verified correct under TensorRT FP16 (output compared with full precision).
# GFPGAN is NOT: in FP16 its StyleGAN layers overflow and it outputs a flat brown square.
TRT_FP16_OK = ("gpen",)


class FaceEnhancer:
    def __init__(self, path):
        self.path = Path(path)
        self.name = self.path.stem
        use_trt = self.name.lower().startswith(TRT_FP16_OK)
        batched = self.path.parent.parent / "batched" / self.path.name
        if batched.exists():
            self.session = create_session(batched, use_trt=use_trt, trt_shapes={
                "input": ((1, 3, 512, 512), (8, 3, 512, 512), (16, 3, 512, 512))})
        else:
            self.session = create_session(self.path, use_trt=use_trt)
        ins = self.session.get_inputs()
        self.img_in = next(i for i in ins if len(i.shape) == 4)
        self.extra = [i for i in ins if i is not self.img_in]  # CodeFormer fidelity weight
        self.dtype = input_dtype(self.img_in)
        dim = self.img_in.shape[2]
        self.size = int(dim) if isinstance(dim, int) else 512
        self.template = template("ffhq", self.size)
        self.out_name = self.session.get_outputs()[0].name
        self.provider = self.session.get_providers()[0]
        batchable = not isinstance(self.img_in.shape[0], int) and not self.extra
        self._batcher = Batcher(self._run_batch, max_items=16, name=f"batch-{self.name}") if batchable else None
        self._run(np.zeros((self.size, self.size, 3), np.uint8), 0.7)

    def _run_batch(self, xs):
        y = self.session.run([self.out_name], {self.img_in.name: np.concatenate(xs)})[0]
        return [y[i:i + 1] for i in range(len(xs))]

    def _run(self, crop, fidelity):
        x = crop[:, :, ::-1].astype(np.float32) / 127.5 - 1.0
        x = x.transpose(2, 0, 1)[None].astype(self.dtype)
        if self._batcher is not None:
            y = self._batcher(x)
        else:
            feeds = {self.img_in.name: x}
            for e in self.extra:
                dt = np.float64 if e.type == "tensor(double)" else np.float32
                feeds[e.name] = np.array([fidelity], dtype=dt)
            y = self.session.run([self.out_name], feeds)[0]
        y = (np.clip(y[0].astype(np.float32), -1, 1) + 1.0) * 127.5
        return np.ascontiguousarray(y.transpose(1, 2, 0)[:, :, ::-1])

    def enhance_into(self, frame, kps, blend=0.8, mask_blur=0.3, fidelity=0.7, outline=None):
        M = estimate_affine(kps, self.template)
        crop = warp_face(frame, M, self.size)
        out = self._run(crop, fidelity)
        if blend < 1.0:
            out = out * blend + crop.astype(np.float32) * (1.0 - blend)
        # with a face outline, restore only the face (not hair or background around it)
        mask = outline_mask(outline, M, self.size, mask_blur) if outline is not None else box_mask(self.size, mask_blur)
        paste_back(frame, out, M, mask)
