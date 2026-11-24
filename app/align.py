"""Face alignment templates and warps (5-point landmarks)."""
import cv2
import numpy as np

# insightface ArcFace 112x112 template
ARCFACE_112 = np.array([
    [38.2946, 51.6963], [73.5318, 51.5014], [56.0252, 71.7366],
    [41.5493, 92.3655], [70.7299, 92.2041]], dtype=np.float32)

# FFHQ template used by GFPGAN / GPEN / CodeFormer (normalised 0..1)
FFHQ = np.array([
    [0.37691676, 0.46864664], [0.62285697, 0.46912813], [0.50123859, 0.61331904],
    [0.39308822, 0.72541100], [0.61150205, 0.72490465]], dtype=np.float32)


def arcface_template(size):
    """Same rule as insightface estimate_norm: 112-multiples vs 128-multiples."""
    if size % 112 == 0:
        ratio, dx = size / 112.0, 0.0
    else:
        ratio, dx = size / 128.0, 8.0 * size / 128.0
    t = ARCFACE_112 * ratio
    t[:, 0] += dx
    return t.astype(np.float32)


def template(kind, size):
    if kind == "ffhq":
        return (FFHQ * size).astype(np.float32)
    if kind == "arcface_112":
        return (ARCFACE_112 * size / 112.0).astype(np.float32)
    return arcface_template(size)


def estimate_affine(kps, tmpl):
    M, _ = cv2.estimateAffinePartial2D(
        kps.astype(np.float32), tmpl, method=cv2.RANSAC, ransacReprojThreshold=100)
    return M


def warp_face(img, M, size):
    return cv2.warpAffine(img, M, (size, size), flags=cv2.INTER_LINEAR,
                          borderMode=cv2.BORDER_REPLICATE)
