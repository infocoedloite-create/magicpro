"""Live pipeline: camera thread -> GPU swap thread -> virtual camera + MJPEG preview.

Latency design:
  * the camera thread always keeps only the newest frame (stale frames are dropped, never queued)
  * the processing thread works on the newest frame and hands it straight to the outputs
  * preview JPEGs are encoded per viewer, downscaled, outside the processing thread
"""
import json
import os
import tempfile
import threading
import time
import traceback
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

import cv2
import numpy as np

from . import batcher, codec, config
from .blending import draw_label, mouth_mask, swap_mask
from .landmarks import Landmarks106, face_outline, mouth_polygon
from .enhancer import FaceEnhancer
from .face import FaceDetector, FaceRecognizer, KpsSmoother
from .library import FaceLibrary
from .onnx_utils import runtime_info
from .swapper import FaceSwapper

RESOLUTIONS = {"720p": (1280, 720), "1080p": (1920, 1080)}
STATS_DIR = Path(tempfile.gettempdir()) / f"magicpro-stats-{config.PORT}"
RESTART_KEYS = {"camera", "camera_backend", "resolution", "fps"}


@dataclass
class Settings:
    # capture / output
    input_source: str = "browser"      # browser (webcam streamed from the page) | server (camera on this machine)
    stream_quality: int = 85           # JPEG quality (fallback mode for browsers without WebCodecs)
    stream_bitrate_mbps: float = 8.0   # H.264 bitrate, both directions
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
    swap_passes: int = 1               # 2 = swap again in crop space: measured only +0.01 likeness
    mask_mode: str = "face"            # face = follows the real jawline (106 landmarks) | box = square crop
    face_mask_expand: float = 0.05     # push the face mask past the jaw edge (fraction of face height)
    chin_extend: float = 0.04          # also cover below the jaw: under the chin, neck beard
    swap_zoom: float = 0.9             # < 1 repaints more chin/jaw around the face (less detail)
    edge_detail: float = 1.0           # keep real skin/beard texture in the mask edge (no blurry halo)
    mouth_keep: str = "inner"          # off | inner (real teeth + tongue) | lips (real lips too)
    # detection / tracking (side views: lower threshold, a few hold frames)
    det_size: int = 640
    det_thresh: float = 0.35
    smoothing: float = 0.5
    hold_frames: int = 4
    # blending
    mask_blur: float = 0.3             # higher blends more of the real face back in (weaker swap)
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


# High-resolution swap presets built on inswapper_128 with pixel boost. A real .onnx file with the
# same id in models/swappers takes precedence over the preset.
SWAP_PRESETS = {
    "inswapper_512_live": {"base": "inswapper_128_fp16", "boost": 2, "name": "InSwapper 512 Live",
                           "detail": "256 px swap, fast"},
    "inswapper_512": {"base": "inswapper_128_fp16", "boost": 4, "name": "InSwapper 512",
                      "detail": "512 px swap"},
    "inswapper_studio": {"base": "inswapper_128", "boost": 4, "name": "InSwapper Studio",
                         "detail": "512 px, full precision + skin tone match", "color_match": 0.6},
}


def list_swappers():
    files = list_models(config.SWAPPERS)
    ids = {m["id"] for m in files}
    presets = [{"id": k, "name": v["name"], "detail": v["detail"]}
               for k, v in SWAP_PRESETS.items() if v["base"] in ids and k not in ids]
    return files + presets


def resolve_swapper(name):
    """-> (model file id, pixel boost, preset colour match)"""
    p = SWAP_PRESETS.get(name)
    if p and not (config.SWAPPERS / f"{name}.onnx").exists():
        return p["base"], p["boost"], p.get("color_match", 0.0)
    return name, 1, 0.0


FULL_DETECT_EVERY = 5      # full-frame detection every N frames; in between, only around the last face


class TrackState:
    """Per-stream face tracking (smoothing + hold frames). Never shared between users."""

    def __init__(self):
        self.smoother = KpsSmoother()
        self.last_faces = []
        self.miss = 0
        self.since_full = FULL_DETECT_EVERY
        self.outlines = []


def detect_tracked(detector, frame, s, track):
    """Full-frame detection every FULL_DETECT_EVERY frames (and whenever the face is lost);
    otherwise detect at 320 px inside a box around the previous face, which costs a fraction of
    the GPU and CPU time and keeps the landmarks just as precise."""
    if not s.many_faces and track.last_faces and track.miss == 0 and track.since_full < FULL_DETECT_EVERY:
        f = track.last_faces[0]
        h, w = frame.shape[:2]
        cx, cy = (f.bbox[0] + f.bbox[2]) / 2, (f.bbox[1] + f.bbox[3]) / 2
        half = f.size * 1.1
        x0, y0 = int(max(cx - half, 0)), int(max(cy - half, 0))
        x1, y1 = int(min(cx + half, w)), int(min(cy + half, h))
        if x1 - x0 > 32 and y1 - y0 > 32:
            faces = detector.detect(frame[y0:y1, x0:x1], 320, s.det_thresh, max_faces=1)
            if faces:
                for g in faces:
                    g.bbox += (x0, y0, x0, y0)
                    g.kps += (x0, y0)
                track.since_full += 1
                return faces
    track.since_full = 0
    return detector.detect(frame, s.det_size, s.det_thresh, max_faces=0 if s.many_faces else 1)


class Engine:
    def __init__(self):
        self.s = Settings()
        self._settings_mtime = 0.0
        self._settings_checked = 0.0
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
        self._out_frame, self._out_seq, self._out_time = None, 0, 0.0
        self._fps_lock = threading.Lock()
        self._fps_t, self._fps_n = time.perf_counter(), 0
        self.browser_clients = 0
        self._vcam = None
        self._camera_track = TrackState()       # the server-camera stream's own tracking state
        batcher.active_streams = self.active_streams
        self.stats = {"fps": 0.0, "latency_ms": 0.0, "detect_ms": 0.0, "swap_ms": 0.0,
                      "enhance_ms": 0.0, "faces": 0, "camera": "", "output": "",
                      "swap_provider": "", "status": "idle", "error": "", "vcam": "off", "encoder": ""}
        self._pick_default_models()
        STATS_DIR.mkdir(parents=True, exist_ok=True)
        threading.Thread(target=self._stats_writer, name="stats", daemon=True).start()

    # ---------- stats shared between worker processes ----------
    def _stats_writer(self):
        path = STATS_DIR / f"{os.getpid()}.json"
        while True:
            try:
                tmp = path.with_suffix(".tmp")
                tmp.write_text(json.dumps({**self.stats, "batch": batcher.mean_batch_sizes(),
                                           "pid": os.getpid(), "t": time.time(),
                                           "browser_clients": self.browser_clients, "running": self.running}))
                os.replace(tmp, path)
            except OSError:
                pass
            time.sleep(1.0)

    def cluster_stats(self):
        """Stats of all worker processes combined (fps and timings weighted by streams)."""
        now, workers = time.time(), []
        for f in STATS_DIR.glob("*.json"):
            try:
                d = json.loads(f.read_text())
            except (OSError, ValueError):
                continue
            if now - d.get("t", 0) < 3:
                workers.append(d)
            elif now - d.get("t", 0) > 60:
                f.unlink(missing_ok=True)
        if not workers:
            return {"running": self.running, "browser_clients": self.browser_clients, **self.stats}
        active = [w for w in workers if w["browser_clients"] or w["running"]]
        out = dict(max(workers, key=lambda w: (w["browser_clients"], w["t"])))
        out["browser_clients"] = sum(w["browser_clients"] for w in workers)
        out["running"] = any(w["running"] for w in workers)
        out["workers"] = len(workers)
        if active:
            weight = lambda w: w["browser_clients"] + (1 if w["running"] else 0)  # noqa: E731
            total = sum(weight(w) for w in active)
            for k in ("fps", "latency_ms", "detect_ms", "swap_ms", "enhance_ms"):
                out[k] = round(sum(w.get(k, 0) * weight(w) for w in active) / total, 1)
            out["status"] = "live"
        out["error"] = next((w["error"] for w in workers if w.get("error")), "")
        for k in ("pid", "t"):
            out.pop(k, None)
        return out

    # ---------- settings ----------
    def _load_settings(self):
        if config.SETTINGS_FILE.exists():
            try:
                self._settings_mtime = config.SETTINGS_FILE.stat().st_mtime
                self._apply(json.loads(config.SETTINGS_FILE.read_text(encoding="utf-8")))
            except Exception as e:  # noqa: BLE001
                print(f"[settings] ignored broken settings.json: {e}")

    def sync(self):
        """Pick up settings and library changes made by other worker processes (cheap: one stat
        per file at most twice a second)."""
        now = time.perf_counter()
        if now - self._settings_checked < 0.5:
            return
        self._settings_checked = now
        try:
            m = config.SETTINGS_FILE.stat().st_mtime
            if m != self._settings_mtime:
                self._load_settings()
        except FileNotFoundError:
            pass
        self.library.sync()

    def _pick_default_models(self):
        sw = [m["id"] for m in list_swappers()]
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
            tmp = config.SETTINGS_FILE.with_suffix(f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps(asdict(self.s), indent=1), encoding="utf-8")
            os.replace(tmp, config.SETTINGS_FILE)       # atomic: other workers never read half a file
            self._settings_mtime = config.SETTINGS_FILE.stat().st_mtime
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
    def landmarks(self):
        """106-point landmarks for face-shaped masks; None if the model file is missing."""
        with self._lock:
            if getattr(self, "_lm", None) is None:
                path = config.MODELS / "2d106det.onnx"
                self._lm = Landmarks106(path) if path.exists() else False
            return self._lm or None

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
    def _safe_process(self, frame, track=None):
        """Thread-safe: several streams process concurrently (ONNX Runtime and OpenCV release the
        GIL), each with its own TrackState so faces never leak between users."""
        self.sync()
        try:
            out = self._process(frame, track or self._camera_track)
            self.stats.update(status="live", error="")
        except Exception as e:  # noqa: BLE001  keep the stream alive, show the error
            traceback.print_exc()
            self.stats.update(status="error", error=str(e))
            out = frame
        with self._fps_lock:
            self._fps_n += 1
            now = time.perf_counter()
            if now - self._fps_t >= 1.0:
                # frames per second per stream
                self.stats["fps"] = round(self._fps_n / (now - self._fps_t) / max(1, self.active_streams()), 1)
                self._fps_t, self._fps_n = now, 0
        return out

    def active_streams(self):
        return self.browser_clients + (1 if self.running else 0)

    def _process_loop(self):
        seen = 0
        while not self._stop.is_set():
            with self._in_cond:
                self._in_cond.wait_for(lambda: self._in_seq != seen or self._stop.is_set(), timeout=0.5)
                if self._stop.is_set() or self._in_seq == seen:
                    continue
                frame, t_in, seen = self._in_frame, self._in_time, self._in_seq
            out = self._safe_process(frame)
            self._emit(out)
            self.stats["latency_ms"] = round((time.perf_counter() - t_in) * 1000, 1)

    def process_jpeg(self, data: bytes, track=None):
        """Browser mode: JPEG in -> swapped JPEG out. Returns None for an unreadable frame."""
        t = time.perf_counter()
        frame = codec.decode(data)
        if frame is None:
            return None
        out = self._safe_process(frame, track)
        self._publish(out)
        jpg = codec.encode(out, self.s.stream_quality)
        self.stats["latency_ms"] = round((time.perf_counter() - t) * 1000, 1)
        return jpg

    def _process(self, frame, track):
        s = self.s
        if s.mirror:
            frame = cv2.flip(frame, 1)
        w, h = RESOLUTIONS[s.resolution]
        if frame.shape[1] != w or frame.shape[0] != h:
            frame = cv2.resize(frame, (w, h), interpolation=cv2.INTER_LINEAR)
        self.stats["output"] = f"{w}x{h}"

        emb = self.library.embedding(s.source_ids) if s.swap_enabled else None
        model_id, boost, preset_color = resolve_swapper(s.swapper)
        swapper = self._get(self._swappers, config.SWAPPERS, FaceSwapper, model_id) if emb is not None else None
        if swapper is None:
            self.stats.update(faces=0, detect_ms=0.0, swap_ms=0.0, enhance_ms=0.0)
            if s.disclosure_label and emb is not None:
                draw_label(frame)
            return frame
        self.stats["swap_provider"] = swapper.provider

        t0 = time.perf_counter()
        faces = detect_tracked(self.detector, frame, s, track)
        if faces:
            track.last_faces, track.miss = faces, 0
        elif track.last_faces and track.miss < s.hold_frames:
            faces, track.miss = track.last_faces, track.miss + 1   # bridge brief misses on side views
        faces = track.smoother(faces, s.smoothing)
        t1 = time.perf_counter()

        # face-shaped masks that follow each real jawline, and the real mouth, from 106 landmarks
        # measured on the frame before swapping
        mouth_mode = s.mouth_keep if s.mouth_keep in ("inner", "lips") else ("lips" if s.mouth_mask else "off")
        outlines, mouths = [None] * len(faces), [None] * len(faces)
        lm = self.landmarks if s.mask_mode == "face" else None
        if lm is not None and faces:
            outlines, mouths, all_pts = [], [], []
            for i, f in enumerate(faces):
                pts = lm.detect(frame, f.bbox)
                prev = track.outlines[i] if i < len(track.outlines) else None
                if prev is not None and prev.shape == pts.shape and s.smoothing > 0:
                    # steady jitter only; real movement (jaw dropping while talking) passes through
                    motion = float(np.abs(pts - prev).mean()) / max(f.size, 1.0)
                    a = min(s.smoothing, 0.6) * float(np.clip(1.0 - motion * 40.0, 0.0, 1.0))
                    sm = prev * a + pts * (1.0 - a)
                    sm[52:72] = pts[52:72]                  # lips never smoothed: lip sync stays exact
                    pts = sm.astype(np.float32)
                all_pts.append(pts)
                outlines.append(face_outline(pts, expand=s.face_mask_expand, chin=s.chin_extend))
                mouths.append(mouth_polygon(pts, mouth_mode, s.mouth_size) if mouth_mode != "off" else None)
            track.outlines = all_pts

        latent = swapper.latent(emb)
        size = swapper.output_size(boost)
        mask = swap_mask(size, round(s.mask_blur, 2), s.mask_top, s.mask_right, s.mask_bottom,
                         s.mask_left, round(s.mouth_size, 2) if s.mouth_mask else 0.0)
        keep = (1.0 - mouth_mask(size, round(s.mouth_size, 2))) if s.mouth_mask else None
        color = max(s.color_match, preset_color)
        for f, o, mp in zip(faces, outlines, mouths):
            swapper.swap_into(frame, f.kps, latent, mask, color, boost, outline=o,
                              outline_blur=s.mask_blur, keep=keep, zoom=s.swap_zoom,
                              keep_poly=mp, edge_detail=s.edge_detail, passes=max(1, min(3, s.swap_passes)))
        t2 = time.perf_counter()

        enhancer = self._get(self._enhancers, config.ENHANCERS, FaceEnhancer, s.enhancer)
        if enhancer is not None and faces:
            for f, o in zip(faces, outlines):
                enhancer.enhance_into(frame, f.kps, s.enhancer_blend, s.mask_blur, s.enhancer_fidelity, outline=o)
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
        self._publish(frame)

    def _publish(self, frame):
        with self._out_cond:
            self._out_frame, self._out_time = frame, time.perf_counter()
            self._out_seq += 1
            self._out_cond.notify_all()

    def _idle_frame(self):
        w, h = RESOLUTIONS[self.s.resolution]
        img = np.full((h // 2, w // 2, 3), 18, np.uint8)
        cv2.putText(img, "Camera stopped", (40, h // 4), cv2.FONT_HERSHEY_SIMPLEX, 1.0,
                    (120, 120, 120), 2, cv2.LINE_AA)
        return img

    def jpeg(self, full=False):
        fresh = self._out_frame is not None and time.perf_counter() - self._out_time < 2.0
        frame = self._out_frame if fresh else self._idle_frame()
        pw = self.s.preview_width
        if not full and frame.shape[1] > pw:
            frame = cv2.resize(frame, (pw, int(frame.shape[0] * pw / frame.shape[1])),
                               interpolation=cv2.INTER_AREA)
        return codec.encode(frame, 92 if full else self.s.preview_quality)

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
