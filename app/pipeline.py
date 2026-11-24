"""Live pipeline: camera thread -> GPU swap thread -> virtual camera + MJPEG preview.

Latency design:
  * the camera thread always keeps only the newest frame (stale frames are dropped, never queued)
  * the processing thread works on the newest frame and hands it straight to the outputs
  * preview JPEGs are encoded per viewer, downscaled, outside the processing thread
"""
import json
import os
import threading
import time
import traceback
from dataclasses import asdict, dataclass, field, fields

import cv2
import numpy as np

from . import config
from .blending import draw_label, swap_mask
from .enhancer import FaceEnhancer
from .face import FaceDetector, FaceRecognizer, KpsSmoother
from .library import FaceLibrary
from .onnx_utils import runtime_info
from .swapper import FaceSwapper

RESOLUTIONS = {"720p": (1280, 720), "1080p": (1920, 1080)}
RESTART_KEYS = {"camera", "camera_backend", "resolution", "fps"}


@dataclass
class Settings:
    # capture / output
    camera: int = 0
    camera_backend: str = "dshow"      # dshow | msmf | any
    resolution: str = "1080p"
    fps: int = 30
    mirror: bool = False
    virtual_cam: bool = True
    # identity
    source_ids: list = field(default_factory=list)
    swap_enabled: bool = True
    swapper: str = ""
    many_faces: bool = False
    # detection / tracking (side views: lower threshold, a few hold frames)
    det_size: int = 640
    det_thresh: float = 0.35
    smoothing: float = 0.5
    hold_frames: int = 4
    # blending
    mask_blur: float = 0.3
    mask_top: int = 0
    mask_bottom: int = 0
    mask_left: int = 0
    mask_right: int = 0
    mouth_mask: bool = False
    mouth_size: float = 1.0
    color_match: float = 0.0
    # enhancer
    enhancer: str = ""
    enhancer_blend: float = 0.8
    enhancer_fidelity: float = 0.7
    # preview / disclosure
    preview_width: int = 960
    preview_quality: int = 80
    disclosure_label: bool = True


def pretty(stem):
    words = stem.replace("-", "_").split("_")
    fix = {"inswapper": "InSwapper", "fp16": "FP16", "gfpgan": "GFPGAN", "gpen": "GPEN",
           "bfr": "BFR", "codeformer": "CodeFormer"}
    return " ".join(fix.get(w.lower(), w.capitalize()) for w in words if w)


def list_models(folder):
    return [{"id": p.stem, "name": pretty(p.stem), "size_mb": round(p.stat().st_size / 2**20)}
            for p in sorted(folder.glob("*.onnx"))]


class Engine:
    def __init__(self):
        self.s = Settings()
        self._load_settings()
        self._lock = threading.RLock()
        self._det = self._rec = None
        self._swappers, self._enhancers = {}, {}
        self.library = FaceLibrary(config.LIBRARY, lambda: (self.detector, self.recognizer))

        self.running = False
        self._stop = threading.Event()
        self._restart_cam = threading.Event()
        self._threads = []
        self._in_cond = threading.Condition()
        self._in_frame, self._in_time, self._in_seq = None, 0.0, 0
        self._out_cond = threading.Condition()
        self._out_frame, self._out_seq = None, 0
        self._vcam = None
        self._smoother = KpsSmoother()
        self._last_faces, self._miss = [], 0
        self.stats = {"fps": 0.0, "latency_ms": 0.0, "detect_ms": 0.0, "swap_ms": 0.0,
                      "enhance_ms": 0.0, "faces": 0, "camera": "", "output": "",
                      "swap_provider": "", "status": "idle", "error": "", "vcam": "off"}
        self._pick_default_models()

    # ---------- settings ----------
    def _load_settings(self):
        if config.SETTINGS_FILE.exists():
            try:
                self._apply(json.loads(config.SETTINGS_FILE.read_text(encoding="utf-8")))
            except Exception as e:  # noqa: BLE001
                print(f"[settings] ignored broken settings.json: {e}")

    def _pick_default_models(self):
        sw = [m["id"] for m in list_models(config.SWAPPERS)]
        if self.s.swapper not in sw:
            pref = [m for m in ("inswapper_128_fp16", "inswapper_128") if m in sw]
            self.s.swapper = pref[0] if pref else (sw[0] if sw else "")
        en = [m["id"] for m in list_models(config.ENHANCERS)]
        if self.s.enhancer and self.s.enhancer not in en:
            self.s.enhancer = "gfpgan_1.4" if "gfpgan_1.4" in en else ""

    def _apply(self, patch):
        changed = set()
        types = {f.name: f.type for f in fields(Settings)}
        for k, v in patch.items():
            if k not in types:
                continue
            cur = getattr(self.s, k)
            if isinstance(cur, bool):
                v = v if isinstance(v, bool) else str(v).lower() in ("1", "true", "on", "yes")
            elif isinstance(cur, int):
                v = int(float(v))
            elif isinstance(cur, float):
                v = float(v)
            elif isinstance(cur, list):
                v = [str(x) for x in v]
            else:
                v = str(v)
            if v != cur:
                setattr(self.s, k, v)
                changed.add(k)
        if self.s.resolution not in RESOLUTIONS:
            self.s.resolution = "1080p"
        self.s.fps = int(np.clip(self.s.fps, 15, 60))
        return changed

    def update(self, patch):
        changed = self._apply(patch)
        if changed & RESTART_KEYS and self.running:
            self._restart_cam.set()
        if changed:
            config.SETTINGS_FILE.write_text(json.dumps(asdict(self.s), indent=1), encoding="utf-8")
        return asdict(self.s)

    def settings_dict(self):
        return asdict(self.s)

    # ---------- models ----------
    @property
    def detector(self):
        with self._lock:
            if self._det is None:
                if not config.DETECTOR.exists():
                    raise RuntimeError("Missing models/det_10g.onnx. Run setup.ps1.")
                self._det = FaceDetector(config.DETECTOR)
            return self._det

    @property
    def recognizer(self):
        with self._lock:
            if self._rec is None:
                if not config.RECOGNIZER.exists():
                    raise RuntimeError("Missing models/w600k_r50.onnx. Run setup.ps1.")
                self._rec = FaceRecognizer(config.RECOGNIZER)
            return self._rec

    def _get(self, cache, folder, cls, name):
        if not name:
            return None
        with self._lock:
            m = cache.get(name)
            if m is None:
                path = folder / f"{name}.onnx"
                if not path.exists():
                    raise RuntimeError(f"Model not found: {path.name}")
                self.stats["status"] = f"loading {pretty(name)}"
                t = time.perf_counter()
                m = cls(path)
                print(f"[model] {name} loaded on {m.provider} in {time.perf_counter() - t:.1f}s")
                cache[name] = m
            return m

    def runtime(self):
        info = runtime_info()
        info["loaded"] = {
            "swappers": {k: v.provider for k, v in self._swappers.items()},
            "enhancers": {k: v.provider for k, v in self._enhancers.items()},
        }
        return info

    # ---------- lifecycle ----------
    def start(self):
        if self.running:
            return
        self._stop.clear()
        self.running = True
        self.stats.update(status="starting", error="")
        self._threads = [threading.Thread(target=self._capture_loop, name="capture", daemon=True),
                         threading.Thread(target=self._process_loop, name="process", daemon=True)]
        for t in self._threads:
            t.start()

    def stop(self):
        if not self.running:
            return
        self._stop.set()
        with self._in_cond:
            self._in_cond.notify_all()
        for t in self._threads:
            t.join(timeout=3)
        self.running = False
        self._close_vcam()
        self.stats.update(status="idle", fps=0.0)

    # ---------- capture ----------
    def _backend(self):
        if self.s.camera_backend == "dshow" and os.name == "nt":
            return cv2.CAP_DSHOW
        if self.s.camera_backend == "msmf" and os.name == "nt":
            return cv2.CAP_MSMF
        return cv2.CAP_ANY

    def _open_camera(self):
        w, h = RESOLUTIONS[self.s.resolution]
        cap = cv2.VideoCapture(self.s.camera, self._backend())
        if not cap.isOpened():
            return None
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, w)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
        cap.set(cv2.CAP_PROP_FPS, self.s.fps)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        aw, ah = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        afps = cap.get(cv2.CAP_PROP_FPS)
        self.stats["camera"] = f"{aw}x{ah} @ {afps:.0f}fps"
        return cap

    def _capture_loop(self):
        while not self._stop.is_set():
            self._restart_cam.clear()
            cap = self._open_camera()
            if cap is None:
                self.stats.update(status="error", error=f"Camera {self.s.camera} could not be opened")
                self._stop.wait(1.0)
                continue
            self.stats["error"] = ""
            fails = 0
            while not self._stop.is_set() and not self._restart_cam.is_set():
                ok, frame = cap.read()
                if not ok:
                    fails += 1
                    if fails > 30:
                        break
                    continue
                fails = 0
                with self._in_cond:
                    self._in_frame, self._in_time = frame, time.perf_counter()
                    self._in_seq += 1
                    self._in_cond.notify_all()
            cap.release()

    # ---------- processing ----------
    def _process_loop(self):
        seen, fps_t, fps_n = 0, time.perf_counter(), 0
        while not self._stop.is_set():
            with self._in_cond:
                self._in_cond.wait_for(lambda: self._in_seq != seen or self._stop.is_set(), timeout=0.5)
                if self._stop.is_set() or self._in_seq == seen:
                    continue
                frame, t_in, seen = self._in_frame, self._in_time, self._in_seq
            try:
                out = self._process(frame)
                self.stats.update(status="live", error="")
            except Exception as e:  # noqa: BLE001  keep the stream alive, show the error
                traceback.print_exc()
                self.stats.update(status="error", error=str(e))
                out = frame
            self._emit(out)
            self.stats["latency_ms"] = round((time.perf_counter() - t_in) * 1000, 1)
            fps_n += 1
            now = time.perf_counter()
            if now - fps_t >= 1.0:
                self.stats["fps"] = round(fps_n / (now - fps_t), 1)
                fps_t, fps_n = now, 0

    def _process(self, frame):
        s = self.s
        if s.mirror:
            frame = cv2.flip(frame, 1)
        w, h = RESOLUTIONS[s.resolution]
        if frame.shape[1] != w or frame.shape[0] != h:
            frame = cv2.resize(frame, (w, h), interpolation=cv2.INTER_LINEAR)
        self.stats["output"] = f"{w}x{h}"

        emb = self.library.embedding(s.source_ids) if s.swap_enabled else None
        swapper = self._get(self._swappers, config.SWAPPERS, FaceSwapper, s.swapper) if emb is not None else None
        if swapper is None:
            self.stats.update(faces=0, detect_ms=0.0, swap_ms=0.0, enhance_ms=0.0)
            if s.disclosure_label and emb is not None:
                draw_label(frame)
            return frame
        self.stats["swap_provider"] = swapper.provider

        t0 = time.perf_counter()
        faces = self.detector.detect(frame, s.det_size, s.det_thresh, max_faces=0 if s.many_faces else 1)
        if faces:
            self._last_faces, self._miss = faces, 0
        elif self._last_faces and self._miss < s.hold_frames:
            faces, self._miss = self._last_faces, self._miss + 1   # bridge brief misses on side views
        faces = self._smoother(faces, s.smoothing)
        t1 = time.perf_counter()

        latent = swapper.latent(emb)
        mask = swap_mask(swapper.size, round(s.mask_blur, 2), s.mask_top, s.mask_right, s.mask_bottom,
                         s.mask_left, round(s.mouth_size, 2) if s.mouth_mask else 0.0)
        for f in faces:
            swapper.swap_into(frame, f.kps, latent, mask, s.color_match)
        t2 = time.perf_counter()

        enhancer = self._get(self._enhancers, config.ENHANCERS, FaceEnhancer, s.enhancer)
        if enhancer is not None and faces:
            for f in faces:
                enhancer.enhance_into(frame, f.kps, s.enhancer_blend, s.mask_blur, s.enhancer_fidelity)
        t3 = time.perf_counter()

        if s.disclosure_label:
            draw_label(frame)
        self.stats.update(faces=len(faces), detect_ms=round((t1 - t0) * 1000, 1),
                          swap_ms=round((t2 - t1) * 1000, 1), enhance_ms=round((t3 - t2) * 1000, 1))
        return frame

    # ---------- outputs ----------
    def _close_vcam(self):
        if self._vcam is not None:
            try:
                self._vcam.close()
            except Exception:  # noqa: BLE001
                pass
            self._vcam = None
        self.stats["vcam"] = "off"

    def _emit(self, frame):
        if self.s.virtual_cam:
            try:
                h, w = frame.shape[:2]
                if self._vcam is None or (self._vcam.width, self._vcam.height, self._vcam.fps) != (w, h, self.s.fps):
                    self._close_vcam()
                    import pyvirtualcam
                    self._vcam = pyvirtualcam.Camera(w, h, self.s.fps, fmt=pyvirtualcam.PixelFormat.BGR)
                    self.stats["vcam"] = self._vcam.device
                self._vcam.send(frame)
            except Exception as e:  # noqa: BLE001
                self._vcam = None
                self.stats["vcam"] = f"unavailable: {e}"
                self.s.virtual_cam = False
        elif self._vcam is not None:
            self._close_vcam()
        with self._out_cond:
            self._out_frame = frame
            self._out_seq += 1
            self._out_cond.notify_all()

    def _idle_frame(self):
        w, h = RESOLUTIONS[self.s.resolution]
        img = np.full((h // 2, w // 2, 3), 18, np.uint8)
        cv2.putText(img, "Camera stopped", (40, h // 4), cv2.FONT_HERSHEY_SIMPLEX, 1.0,
                    (120, 120, 120), 2, cv2.LINE_AA)
        return img

    def jpeg(self, full=False):
        frame = self._out_frame if self.running and self._out_frame is not None else self._idle_frame()
        pw = self.s.preview_width
        if not full and frame.shape[1] > pw:
            frame = cv2.resize(frame, (pw, int(frame.shape[0] * pw / frame.shape[1])),
                               interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 92 if full else self.s.preview_quality])
        return buf.tobytes()

    def mjpeg(self, full=False):
        seen = -1
        while True:
            with self._out_cond:
                self._out_cond.wait_for(lambda: self._out_seq != seen, timeout=1.0)
                seen = self._out_seq
            data = self.jpeg(full)
            yield (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                   + str(len(data)).encode() + b"\r\n\r\n" + data + b"\r\n")

    def probe_cameras(self, max_index=6):
        found = []
        for i in range(max_index):
            if self.running and i == self.s.camera:
                found.append({"index": i, "label": f"Camera {i} (in use)"})
                continue
            cap = cv2.VideoCapture(i, self._backend())
            if cap.isOpened():
                found.append({"index": i, "label": f"Camera {i}"})
            cap.release()
        return found
