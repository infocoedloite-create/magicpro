"""JPEG encode/decode: libjpeg-turbo via simplejpeg when available (about 2x faster than OpenCV)."""
import cv2
import numpy as np

try:
    import simplejpeg
except ImportError:  # pragma: no cover
    simplejpeg = None


def decode(data: bytes):
    if simplejpeg is not None:
        try:
            img = simplejpeg.decode_jpeg(data, colorspace="BGR", fastdct=True, fastupsample=True)
            return img if img.flags.writeable else img.copy()
        except Exception:  # noqa: BLE001
            pass
    return cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)


def encode(img, quality=85) -> bytes:
    if simplejpeg is not None:
        return simplejpeg.encode_jpeg(np.ascontiguousarray(img), quality=quality, colorspace="BGR",
                                      colorsubsampling="420", fastdct=True)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
    return buf.tobytes()
