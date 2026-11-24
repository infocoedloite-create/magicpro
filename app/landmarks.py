"""106-point face landmarks (insightface 2d106det) -> face-shaped swap masks that follow the
real jawline: the swap covers down to the chin and along both sides of the jaw, with a soft edge
only outside the face."""
import cv2
import numpy as np

from .onnx_utils import create_session, input_dtype

# 2d106det contour (jaw) points are indices 0-32; the rest are brows, eyes, nose and mouth.
CONTOUR = list(range(33))


class Landmarks106:
    def __init__(self, path):
        self.session = create_session(path, use_trt=False)
        inp = self.session.get_inputs()[0]
        self.input_name = inp.name
        self.dtype = input_dtype(inp)
        self.size = 192

    def detect(self, img, bbox):
        """bbox x1,y1,x2,y2 in img -> (106, 2) points in img coordinates."""
        w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
        cx, cy = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
        s = self.size / (max(w, h) * 1.5)
        M = np.array([[s, 0, self.size / 2 - cx * s], [0, s, self.size / 2 - cy * s]], np.float32)
        crop = cv2.warpAffine(img, M, (self.size, self.size), borderValue=0)
        # the model normalises internally (Sub/Mul first): mean 0, std 1
        blob = cv2.dnn.blobFromImage(crop, 1.0, (self.size, self.size), (0, 0, 0), swapRB=True)
        pred = self.session.run(None, {self.input_name: blob.astype(self.dtype, copy=False)})[0][0]
        pts = (pred.reshape(-1, 2).astype(np.float32) + 1) * (self.size // 2)
        IM = cv2.invertAffineTransform(M)
        return pts @ IM[:, :2].T + IM[:, 2]


def face_outline(pts106, expand=0.06, forehead=0.35, chin=0.0):
    """Polygon around the face in frame coordinates: the jaw contour pushed outwards by `expand`
    (fraction of face height) so the jaw edge, chin and sideburns are inside, closed over the
    forehead by lifting the upper contour by `forehead` of the face height. `chin` > 0 adds the
    area below the jaw (under the chin, neck beard): the lower jaw contour copied downwards by
    that fraction of the face height, along the face's own vertical axis (works when the head
    tilts or turns)."""
    jaw = pts106[CONTOUR]
    top = pts106.min(axis=0)[1]
    bottom = pts106.max(axis=0)[1]
    height = max(bottom - top, 1.0)
    center = pts106.mean(axis=0)
    out = center + (jaw - center) * (1 + expand)
    # forehead: copy of the contour's upper part, raised
    upper = out[out[:, 1] < center[1]]
    raised = upper - np.array([0, forehead * height], np.float32)
    parts = [out, raised]
    if chin > 0:
        # face "down" direction: from between the eyes towards the chin point (middle of contour)
        down = jaw[len(CONTOUR) // 2] - center
        down = down / max(np.linalg.norm(down), 1.0)
        lower = out[(out - center) @ down > 0]
        parts.append(lower + down * (chin * height))
    return cv2.convexHull(np.concatenate(parts).astype(np.float32)).reshape(-1, 2)


def outline_mask(outline, M, size, blur=0.3):
    """Rasterise a frame-space outline into an aligned crop (affine M, size x size), feathered."""
    poly = outline @ M[:, :2].T + M[:, 2]
    m = np.zeros((size, size), np.float32)
    cv2.fillPoly(m, [np.round(poly).astype(np.int32)], 1.0)
    k = size * 0.5 * blur * 0.25
    if k > 0.5:
        m = cv2.GaussianBlur(m, (0, 0), k)
    # always fade the last few pixels of the crop so its square border never shows
    edge = max(2, size // 64)
    ramp = np.minimum(np.arange(size, dtype=np.float32) + 1, np.arange(size, dtype=np.float32)[::-1] + 1) / edge
    ramp = np.clip(ramp, 0, 1)
    return m * ramp[None, :] * ramp[:, None]
