"""Face restoration (GFPGAN 1.4 / GPEN-BFR / CodeFormer ONNX) applied after the swap."""
from pathlib import Path

import numpy as np

from .align import estimate_affine, template, warp_face
from .blending import box_mask, paste_back
from .onnx_utils import create_session, input_dtype


class FaceEnhancer:
    def __init__(self, path):
        self.path = Path(path)
        self.name = self.path.stem
        self.session = create_session(self.path)
        ins = self.session.get_inputs()
        self.img_in = next(i for i in ins if len(i.shape) == 4)
        self.extra = [i for i in ins if i is not self.img_in]  # CodeFormer fidelity weight
        self.dtype = input_dtype(self.img_in)
        dim = self.img_in.shape[2]
        self.size = int(dim) if isinstance(dim, int) else 512
        self.template = template("ffhq", self.size)
        self.out_name = self.session.get_outputs()[0].name
        self.provider = self.session.get_providers()[0]
        self._run(np.zeros((self.size, self.size, 3), np.uint8), 0.7)

    def _run(self, crop, fidelity):
        x = crop[:, :, ::-1].astype(np.float32) / 127.5 - 1.0
        feeds = {self.img_in.name: x.transpose(2, 0, 1)[None].astype(self.dtype)}
        for e in self.extra:
            dt = np.float64 if e.type == "tensor(double)" else np.float32
            feeds[e.name] = np.array([fidelity], dtype=dt)
        y = self.session.run([self.out_name], feeds)[0][0].astype(np.float32)
        y = (np.clip(y, -1, 1) + 1.0) * 127.5
        return np.ascontiguousarray(y.transpose(1, 2, 0)[:, :, ::-1])

    def enhance_into(self, frame, kps, blend=0.8, mask_blur=0.3, fidelity=0.7):
        M = estimate_affine(kps, self.template)
        crop = warp_face(frame, M, self.size)
        out = self._run(crop, fidelity)
        if blend < 1.0:
            out = out * blend + crop.astype(np.float32) * (1.0 - blend)
        paste_back(frame, out, M, box_mask(self.size, mask_blur))
