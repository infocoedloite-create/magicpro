"""Masks, colour matching and fast ROI paste-back."""
from functools import lru_cache

import cv2
import numpy as np

from .align import arcface_template


@lru_cache(maxsize=64)
def box_mask(size, blur=0.3, top=0, right=0, bottom=0, left=0):
    blur_amount = int(size * 0.5 * blur)
    edge = max(blur_amount // 2, 1)
    m = np.ones((size, size), np.float32)
    m[:max(edge, int(size * top / 100))] = 0
    m[size - max(edge, int(size * bottom / 100)):] = 0
    m[:, :max(edge, int(size * left / 100))] = 0
    m[:, size - max(edge, int(size * right / 100)):] = 0
    if blur_amount > 0:
        m = cv2.GaussianBlur(m, (0, 0), blur_amount * 0.25)
    m.setflags(write=False)
    return m


@lru_cache(maxsize=32)
def mouth_mask(size, scale=1.0):
    """Ellipse around the mouth in aligned-crop space (mouth corners sit on the template)."""
    t = arcface_template(size)
    left, right = t[3], t[4]
    d = float(np.linalg.norm(right - left))
    cx, cy = (left + right) / 2 + np.array([0, 0.18 * d])
    m = np.zeros((size, size), np.float32)
    cv2.ellipse(m, (int(cx), int(cy)), (max(1, int(d * 0.75 * scale)), max(1, int(d * 0.5 * scale))),
                0, 0, 360, 1.0, -1)
    m = cv2.GaussianBlur(m, (0, 0), max(1.0, d * 0.15))
    m.setflags(write=False)
    return m


@lru_cache(maxsize=32)
def swap_mask(size, blur, top, right, bottom, left, mouth_scale):
    m = box_mask(size, blur, top, right, bottom, left).copy()
    if mouth_scale > 0:
        m *= 1.0 - mouth_mask(size, mouth_scale)
    m.setflags(write=False)
    return m


def color_match(swapped, original, mask, strength):
    """Match LAB mean/std of the swapped face to the live face (lighting consistency)."""
    sel = mask > 0.6
    if sel.sum() < 64:
        return swapped
    s = cv2.cvtColor(np.clip(swapped, 0, 255).astype(np.uint8), cv2.COLOR_BGR2LAB).astype(np.float32)
    r = cv2.cvtColor(original, cv2.COLOR_BGR2LAB).astype(np.float32)
    for c in range(3):
        sc, rc = s[..., c][sel], r[..., c][sel]
        ratio = np.clip((rc.std() + 1e-6) / (sc.std() + 1e-6), 0.6, 1.6)
        s[..., c] = (s[..., c] - sc.mean()) * ratio + rc.mean()
    out = cv2.cvtColor(np.clip(s, 0, 255).astype(np.uint8), cv2.COLOR_LAB2BGR).astype(np.float32)
    return swapped * (1.0 - strength) + out * strength


def paste_back(frame, crop, M, mask):
    """Warp an aligned crop back into `frame` in place, touching only the face ROI."""
    h, w = frame.shape[:2]
    s = crop.shape[0]
    IM = cv2.invertAffineTransform(M)
    corners = np.array([[0, 0], [s, 0], [0, s], [s, s]], np.float32)
    pts = corners @ IM[:, :2].T + IM[:, 2]
    x0 = max(int(np.floor(pts[:, 0].min())) - 2, 0)
    y0 = max(int(np.floor(pts[:, 1].min())) - 2, 0)
    x1 = min(int(np.ceil(pts[:, 0].max())) + 2, w)
    y1 = min(int(np.ceil(pts[:, 1].max())) + 2, h)
    if x1 <= x0 or y1 <= y0:
        return
    IMr = IM.copy()
    IMr[0, 2] -= x0
    IMr[1, 2] -= y0
    size = (x1 - x0, y1 - y0)
    warped = cv2.warpAffine(crop.astype(np.float32, copy=False), IMr, size,
                            flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)
    m = cv2.warpAffine(np.ascontiguousarray(mask), IMr, size, flags=cv2.INTER_LINEAR,
                       borderMode=cv2.BORDER_CONSTANT, borderValue=0)[..., None]
    roi = frame[y0:y1, x0:x1].astype(np.float32)
    frame[y0:y1, x0:x1] = np.clip(roi + (warped - roi) * m + 0.5, 0, 255).astype(np.uint8)


def draw_label(frame, text="AI face swap"):
    h, w = frame.shape[:2]
    fs = max(0.45, h / 1080 * 0.7)
    th = max(1, int(round(fs * 2)))
    (tw, tht), base = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, fs, th)
    pad = int(10 * fs) + 4
    x1, y1 = w - pad, h - pad
    x0, y0 = x1 - tw - 2 * pad, y1 - tht - base - pad
    roi = frame[y0:y1, x0:x1]
    roi[:] = (roi * 0.45).astype(np.uint8)
    cv2.putText(frame, text, (x0 + pad, y1 - base - pad // 2), cv2.FONT_HERSHEY_SIMPLEX,
                fs, (255, 255, 255), th, cv2.LINE_AA)
