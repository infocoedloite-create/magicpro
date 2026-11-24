"""Face restoration (GFPGAN 1.4 / GPEN-BFR / CodeFormer ONNX) applied after the swap.

If models/batched/<name>.onnx exists (GFPGAN: tools/export_gfpgan_batched.py), faces from all
streams in this process are restored together in one GPU call through a Batcher.
"""
import json
from pathlib import Path

import cv2
import numpy as np

from .align import estimate_affine, template, warp_face
from .batcher import Batcher
from .blending import box_mask, paste_back
from .landmarks import outline_mask
from .onnx_utils import create_session, input_dtype


# Enhancers verified correct under TensorRT FP16 (output compared with full precision).
# GFPGAN is NOT: in FP16 its StyleGAN layers overflow and it outputs a flat brown square.
TRT_FP16_OK = ("gpen",)


def _pore_texture(size=512, sigma=1.2, seed=7):
    """Band-pass noise at skin-pore scale, unit std. Fixed: the same pattern every frame, in the
    face's own aligned coordinates, so it moves with the face (no flicker, and video compression
    sees it as part of the face instead of fresh noise)."""
    n = np.random.default_rng(seed).standard_normal((size, size)).astype(np.float32)
    t = cv2.GaussianBlur(n, (0, 0), sigma) - cv2.GaussianBlur(n, (0, 0), sigma * 3)
    return t / (float(t.std()) + 1e-6)


_PORES = {}


def add_skin_texture(out, strength):
    """Natural skin after the enhancer, which leaves skin smooth and a little plastic. On the aligned
    face crop (float BGR): mild micro-contrast, then fine pore texture only where the skin is too
    smooth (eyes, brows, lips and hair already have detail and are left alone), less in shadows.
    strength 0..1; 0.7 = 3.5 grey levels of texture and 0.35 sharpening (chosen from side-by-side
    tests: more looks grainy, a finer grain is lost in the video stream)."""
    if strength <= 0:
        return out
    size = out.shape[0]
    tex = _PORES.get(size)
    if tex is None:
        tex = _PORES[size] = _pore_texture(size)
    out = cv2.addWeighted(out, 1.0 + 0.5 * strength, cv2.GaussianBlur(out, (0, 0), 1.0), -0.5 * strength, 0)
    y = cv2.cvtColor(np.clip(out, 0, 255).astype(np.uint8), cv2.COLOR_BGR2GRAY).astype(np.float32)
    small = cv2.resize(y, (size // 4, size // 4), interpolation=cv2.INTER_AREA)     # detail map at 1/4 size
    hp = small - cv2.GaussianBlur(small, (0, 0), 0.6)
    local = cv2.resize(np.sqrt(cv2.GaussianBlur(hp * hp, (0, 0), 1.5)), (size, size), interpolation=cv2.INTER_LINEAR)
    smooth = np.clip(1.0 - local / 2.0, 0.0, 1.0)
    g = (5.0 * strength) * tex * smooth * np.sqrt(np.clip(y * (1 / 255.0), 0, 1))
    return out + cv2.merge([g, g, g])


class FaceEnhancer:
    def __init__(self, path):
        self.path = Path(path)
        self.name = self.path.stem
        use_trt = self.name.lower().startswith(TRT_FP16_OK)
        batched = self.path.parent.parent / "batched" / self.path.name
        side = batched.with_suffix(".json")
        if batched.exists() and side.exists():
            # e.g. GFPGAN re-exported by tools/export_gfpgan_batched.py (FP16-safe, verified 47-50 dB
            # PSNR against full precision): TensorRT FP16 is fine for that file
            use_trt = bool(json.loads(side.read_text()).get("tensorrt_fp16", use_trt))
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

    def enhance_into(self, frame, kps, blend=0.8, mask_blur=0.3, fidelity=0.7, outline=None, texture=0.0):
        M = estimate_affine(kps, self.template)
        crop = warp_face(frame, M, self.size)
        out = self._run(crop, fidelity)
        if blend < 1.0:
            out = out * blend + crop.astype(np.float32) * (1.0 - blend)
        out = add_skin_texture(out, texture)
        # with a face outline, restore only the face (not hair or background around it)
        mask = outline_mask(outline, M, self.size, mask_blur) if outline is not None else box_mask(self.size, mask_blur)
        paste_back(frame, out, M, mask)
