"""Generic InSwapper-family runner.

Works with inswapper_128.onnx / inswapper_128_fp16.onnx out of the box. Other variants
(512, 512 Live, Studio) are picked up the same way: input size and dtype are read from the
model. If a variant needs a different alignment or normalisation, put a sidecar JSON next to
it, e.g. models/swappers/inswapper_512.json:

    {"template": "arcface_128", "normalize": "0_1", "use_emap": true}

template:  arcface_128 (insightface default) | arcface_112 | ffhq
normalize: 0_1 (insightface default) | -1_1
use_emap:  project the ArcFace embedding through the model's emap initialiser
"""
import json
from pathlib import Path

import cv2
import numpy as np

from .align import estimate_affine, template, warp_face
from .batcher import Batcher
from .landmarks import outline_mask
from .blending import color_match, paste_back
from .onnx_utils import create_session, input_dtype


def load_emap(path: Path):
    side = path.with_name(path.stem + ".emap.npy")
    if side.exists():
        arr = np.load(side)
        return arr if arr.size else None
    import onnx
    from onnx import numpy_helper
    model = onnx.load(str(path))
    emap = None
    if model.graph.initializer:
        last = model.graph.initializer[-1]
        if list(last.dims) == [512, 512]:
            emap = numpy_helper.to_array(last).astype(np.float32)
    try:
        np.save(side, emap if emap is not None else np.zeros(0, np.float32))
    except OSError:
        pass
    return emap


class FaceSwapper:
    def __init__(self, path):
        self.path = Path(path)
        self.name = self.path.stem
        side = self.path.with_suffix(".json")
        opts = json.loads(side.read_text()) if side.exists() else {}

        # models/batched/<name>.onnx (made by tools/make_batchable.py) accepts many crops per call
        batched = self.path.parent.parent / "batched" / self.path.name
        model = batched if batched.exists() else self.path
        shapes = None
        if batched.exists():   # TensorRT profile: 1..32 crops per call, tuned for 16 (512 pixel boost)
            import onnx
            g = onnx.load(str(model), load_external_data=False).graph
            shapes = {}
            for i in g.input:
                d = [x.dim_value for x in i.type.tensor_type.shape.dim][1:]
                shapes[i.name] = ((1, *d), (16, *d), (32, *d))
        # No TensorRT: in FP16 it destroys InSwapper's identity transfer (similarity to the source
        # 0.06 instead of 0.63, the face stays the original), and in FP32 it is slower than CUDA.
        self.session = create_session(model, trt_shapes=shapes, use_trt=bool(opts.get("tensorrt", False)))
        ins = self.session.get_inputs()
        self.batchable = not isinstance(ins[0].shape[0], int)
        tgt = [i for i in ins if len(i.shape) == 4]
        src = [i for i in ins if len(i.shape) != 4]
        if len(tgt) != 1 or len(src) != 1:
            raise RuntimeError(f"{self.name}: expected one image input and one embedding input")
        self.t_in, self.s_in = tgt[0], src[0]
        self.t_dtype, self.s_dtype = input_dtype(self.t_in), input_dtype(self.s_in)
        dim = self.t_in.shape[2]
        self.size = int(opts.get("size", dim if isinstance(dim, int) else 128))
        self.out_name = self.session.get_outputs()[0].name
        self.template_kind = opts.get("template", "arcface_128")
        self.template = template(self.template_kind, self.size)
        self._boost_templates = {1: self.template}
        self.signed = opts.get("normalize", "0_1") == "-1_1"
        self.emap = load_emap(self.path) if opts.get("use_emap", True) else None
        self.provider = self.session.get_providers()[0]
        # one dispatcher runs the GPU calls of every stream in this process, batched
        self._batcher = Batcher(self._run_rows, max_items=32, name=f"batch-{self.name}") if self.batchable else None
        self._warmup()

    def _run_rows(self, items):
        """items: [(blob (n,3,s,s), latents (n,512))] from several streams -> list of outputs."""
        blob = np.concatenate([b for b, _ in items])
        lat = np.concatenate([lt for _, lt in items])
        outs = []
        for i in range(0, len(blob), 32):          # TensorRT profile max batch
            outs.append(self.session.run([self.out_name], {self.t_in.name: blob[i:i + 32],
                                                           self.s_in.name: lat[i:i + 32]})[0])
        pred = np.concatenate(outs)
        res, k = [], 0
        for b, _ in items:
            res.append(pred[k:k + len(b)])
            k += len(b)
        return res

    def _warmup(self):
        blank = np.zeros((self.size, self.size, 3), np.uint8)
        self._infer(blank, self.latent(np.ones(512, np.float32) / np.sqrt(512)))

    def latent(self, embedding):
        lat = embedding.reshape(1, -1).astype(np.float32)
        if self.emap is not None and self.emap.shape[0] == lat.shape[1]:
            lat = lat @ self.emap
            lat /= np.linalg.norm(lat)
        return lat

    def _infer(self, crop, latent):
        return self._infer_many([crop], latent)[0]

    def _infer_many(self, crops, latent):
        """Swap several aligned crops (same identity). One GPU call when the model is batchable."""
        if not self.batchable and len(crops) > 1:
            return [self._infer_many([c], latent)[0] for c in crops]
        if self.signed:
            blob = cv2.dnn.blobFromImages(crops, 1 / 127.5, (self.size, self.size), (127.5, 127.5, 127.5), swapRB=True)
        else:
            blob = cv2.dnn.blobFromImages(crops, 1 / 255.0, (self.size, self.size), (0, 0, 0), swapRB=True)
        lat = np.repeat(latent.reshape(1, -1), len(crops), axis=0).astype(self.s_dtype, copy=False)
        blob = blob.astype(self.t_dtype, copy=False)
        if self._batcher is not None:
            pred = self._batcher((blob, lat))
        else:
            pred = self.session.run([self.out_name], {self.t_in.name: blob, self.s_in.name: lat})[0]
        pred = pred.astype(np.float32).transpose(0, 2, 3, 1)
        if self.signed:
            pred = (pred + 1.0) * 0.5
        pred = np.clip(pred, 0, 1)[..., ::-1] * 255.0
        return [np.ascontiguousarray(p) for p in pred]

    def output_size(self, boost=1):
        return self.size * boost

    def swap_into(self, frame, kps, latent, mask, color_strength=0.0, boost=1, outline=None,
                  outline_blur=0.3, keep=None, zoom=1.0):
        """boost > 1 = pixel boost: align the face at size*boost, split it into boost x boost
        interleaved sub-images of the model's native size, swap each and re-interleave. Gives a
        real 256/512 swap from a 128 model (mask must be output_size(boost) square).
        outline: face polygon in frame coordinates -> a face-shaped mask that follows the jaw
        (replaces the square `mask`); keep: crop-space multiplier (e.g. keep the real mouth).
        zoom < 1 aligns the face smaller in the crop, so more chin and jaw get repainted."""
        size = self.size * boost
        key = (boost, round(zoom, 3))
        tmpl = self._boost_templates.get(key)
        if tmpl is None:
            tmpl = template(self.template_kind, size)
            if zoom != 1.0:
                c = np.float32(size / 2)
                tmpl = (c + (tmpl - c) * np.float32(zoom)).astype(np.float32)
            self._boost_templates[key] = tmpl
        M = estimate_affine(kps, tmpl)
        if outline is not None:
            mask = outline_mask(outline, M, size, outline_blur)
            if keep is not None:
                mask = mask * keep
        crop = warp_face(frame, M, size)
        if boost == 1:
            out = self._infer(crop, latent)
        else:
            out = np.empty((size, size, 3), np.float32)
            cells = [(i, j) for i in range(boost) for j in range(boost)]
            results = self._infer_many([np.ascontiguousarray(crop[i::boost, j::boost]) for i, j in cells], latent)
            for (i, j), r in zip(cells, results):
                out[i::boost, j::boost] = r
        if color_strength > 0:
            out = color_match(out, crop, mask, color_strength)
        paste_back(frame, out, M, mask)
