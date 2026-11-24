"""SCRFD face detector + ArcFace recogniser, implemented directly on ONNX Runtime."""
from dataclasses import dataclass

import cv2
import numpy as np

from .align import arcface_template, estimate_affine, warp_face
from .onnx_utils import create_session, input_dtype


@dataclass
class Face:
    bbox: np.ndarray   # x1, y1, x2, y2
    kps: np.ndarray    # (5, 2): eyes, nose, mouth corners
    score: float

    @property
    def area(self):
        return float((self.bbox[2] - self.bbox[0]) * (self.bbox[3] - self.bbox[1]))

    @property
    def size(self):
        return float(max(self.bbox[2] - self.bbox[0], self.bbox[3] - self.bbox[1]))

    def copy(self):
        return Face(self.bbox.copy(), self.kps.copy(), self.score)


class FaceDetector:
    _LAYOUTS = {
        6: (3, [8, 16, 32], 2, False),
        9: (3, [8, 16, 32], 2, True),
        10: (5, [8, 16, 32, 64, 128], 1, False),
        15: (5, [8, 16, 32, 64, 128], 1, True),
    }

    def __init__(self, path):
        self.session = create_session(path)
        inp = self.session.get_inputs()[0]
        self.input_name = inp.name
        self.dtype = input_dtype(inp)
        h, w = inp.shape[2], inp.shape[3]
        self.fixed_size = (w, h) if isinstance(w, int) and isinstance(h, int) else None
        self.output_names = [o.name for o in self.session.get_outputs()]
        layout = self._LAYOUTS.get(len(self.output_names))
        if layout is None or not layout[3]:
            raise RuntimeError(f"{path}: not an SCRFD detector with 5-point landmarks")
        self.fmc, self.strides, self.num_anchors, _ = layout
        self._centers = {}

    def _anchor_centers(self, fh, fw, stride):
        key = (fh, fw, stride)
        c = self._centers.get(key)
        if c is None:
            c = np.stack(np.mgrid[:fh, :fw][::-1], axis=-1).astype(np.float32)
            c = (c * stride).reshape(-1, 2)
            if self.num_anchors > 1:
                c = np.stack([c] * self.num_anchors, axis=1).reshape(-1, 2)
            self._centers[key] = c
        return c

    def detect(self, img, size=640, thresh=0.5, nms=0.4, max_faces=0):
        iw, ih = self.fixed_size or (size, size)
        h, w = img.shape[:2]
        scale = min(iw / w, ih / h)
        nw, nh = max(1, int(round(w * scale))), max(1, int(round(h * scale)))
        canvas = np.zeros((ih, iw, 3), np.uint8)
        canvas[:nh, :nw] = cv2.resize(
            img, (nw, nh), interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR)
        blob = cv2.dnn.blobFromImage(canvas, 1 / 128, (iw, ih), (127.5, 127.5, 127.5), swapRB=True)
        outs = self.session.run(self.output_names, {self.input_name: blob.astype(self.dtype, copy=False)})

        scores_all, boxes_all, kps_all = [], [], []
        for i, stride in enumerate(self.strides):
            scores = outs[i].reshape(-1).astype(np.float32)
            pos = np.where(scores >= thresh)[0]
            if pos.size == 0:
                continue
            dist = outs[i + self.fmc].reshape(-1, 4).astype(np.float32)[pos] * stride
            kps = outs[i + 2 * self.fmc].reshape(-1, 10).astype(np.float32)[pos] * stride
            centers = self._anchor_centers(ih // stride, iw // stride, stride)[pos]
            boxes_all.append(np.concatenate([centers - dist[:, :2], centers + dist[:, 2:]], axis=1))
            kps_all.append(kps.reshape(-1, 5, 2) + centers[:, None, :])
            scores_all.append(scores[pos])
        if not scores_all:
            return []
        scores = np.concatenate(scores_all)
        boxes = np.concatenate(boxes_all) / scale
        kps = np.concatenate(kps_all) / scale
        xywh = np.concatenate([boxes[:, :2], boxes[:, 2:] - boxes[:, :2]], axis=1)
        keep = np.array(cv2.dnn.NMSBoxes(xywh.tolist(), scores.tolist(), thresh, nms)).reshape(-1)
        faces = [Face(boxes[i], kps[i], float(scores[i])) for i in keep]
        faces.sort(key=lambda f: f.area, reverse=True)
        return faces[:max_faces] if max_faces > 0 else faces


class FaceRecognizer:
    def __init__(self, path):
        self.session = create_session(path)
        inp = self.session.get_inputs()[0]
        self.input_name = inp.name
        self.dtype = input_dtype(inp)
        self.size = inp.shape[2] if isinstance(inp.shape[2], int) else 112
        self.template = arcface_template(self.size)

    def embed(self, img, kps):
        crop = warp_face(img, estimate_affine(kps, self.template), self.size)
        blob = cv2.dnn.blobFromImage(crop, 1 / 127.5, (self.size, self.size),
                                     (127.5, 127.5, 127.5), swapRB=True)
        e = self.session.run(None, {self.input_name: blob.astype(self.dtype, copy=False)})[0]
        e = e.reshape(-1).astype(np.float32)
        n = np.linalg.norm(e)
        return e / n if n > 0 else e


class KpsSmoother:
    """Per-face temporal smoothing of landmarks to stop jitter; backs off on fast motion."""

    def __init__(self):
        self.prev = []

    def __call__(self, faces, strength):
        if strength <= 0:
            self.prev = [f.kps.copy() for f in faces]
            return faces
        out, new_prev = [], []
        for f in faces:
            f = f.copy()
            c = f.kps.mean(0)
            best, best_d = None, f.size * 0.35
            for p in self.prev:
                d = float(np.linalg.norm(p.mean(0) - c))
                if d < best_d:
                    best, best_d = p, d
            if best is not None:
                motion = float(np.abs(f.kps - best).mean()) / max(f.size, 1.0)
                a = strength * float(np.clip(1.0 - motion * 25.0, 0.0, 1.0))
                f.kps = best * a + f.kps * (1.0 - a)
            new_prev.append(f.kps.copy())
            out.append(f)
        self.prev = new_prev
        return out
