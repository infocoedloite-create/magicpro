"""H.264 streaming session: browser WebCodecs video in, swapped H.264 video out.

Three threads so the stages overlap instead of adding up:
  decode  : every packet in order (H.264 needs them all); keeps only the newest decoded frame
  process : GPU face swap on the newest frame
  encode  : NVENC (or libx264) -> back to the browser

Wire format, both directions: 1 byte kind (1 = video), 1 byte keyframe flag, 4 bytes frame id
(little endian), then Annex-B H.264. The reply carries the id of the input frame it was made from.
Text messages are JSON control: {"t": "kf"} asks the other side for a keyframe.
"""
import asyncio
import json
import os
import queue
import struct
import threading
import time
import traceback
from fractions import Fraction

import av
import cv2
import numpy as np

HEADER = struct.Struct("<BBI")


def open_encoder(width, height, fps, bitrate):
    errors = []
    for name in ("h264_nvenc", "libx264"):
        try:
            c = av.CodecContext.create(name, "w")
            c.width, c.height = width, height
            c.pix_fmt = "yuv420p"
            c.time_base = Fraction(1, 1000)
            c.framerate = Fraction(fps, 1)
            c.bit_rate = bitrate
            c.gop_size = fps * 2
            c.max_b_frames = 0
            if name == "h264_nvenc":
                c.options = {"preset": "p2", "tune": "ull", "zerolatency": "1", "rc": "cbr",
                             "delay": "0", "profile": "high", "forced-idr": "1"}
            else:
                c.options = {"preset": "ultrafast", "tune": "zerolatency", "profile": "high"}
            c.open()
            return c, name
        except Exception as e:  # noqa: BLE001
            errors.append(f"{name}: {e}")
    raise RuntimeError("No H.264 encoder: " + "; ".join(errors))


class H264Session:
    def __init__(self, engine, loop, send_bytes, send_text, space=None):
        self.engine = engine
        self.loop = loop
        self.send_bytes = send_bytes
        self.send_text = send_text
        self.stop = threading.Event()
        self.packets = queue.Queue()
        self.dec_cond = threading.Condition()
        self.decoded = None          # (id, bgr) newest decoded frame
        self.enc_cond = threading.Condition()
        self.processed = None        # (id, bgr) newest processed frame
        self.last_processed_id = -1  # never let an older frame replace a newer one
        self.force_key = True
        from .pipeline import TrackState
        self.track = TrackState(space)   # this user's own face tracking, settings and library
        self.encoder = None
        self.encoder_name = ""
        self.t0 = time.perf_counter()
        # Several frames of this stream are processed at once: the GPU work of one overlaps the
        # CPU work of the next, so the frame rate is no longer capped at 1 / (time per frame).
        # measured, 1 stream 720p + GFPGAN: 1 worker 16 fps, 2 -> 28, 3 -> 38, 4 -> 47 fps
        workers = max(1, int(os.environ.get("MAGIC_STREAM_WORKERS", "4")))
        self.threads = [threading.Thread(target=self._decode_loop, daemon=True, name="h264-decode"),
                        threading.Thread(target=self._encode_loop, daemon=True, name="h264-encode")]
        self.threads += [threading.Thread(target=self._process_loop, daemon=True, name=f"h264-process-{i}")
                         for i in range(workers)]
        for t in self.threads:
            t.start()

    # ---------- inputs from the websocket ----------
    def feed(self, data: bytes):
        if len(data) > HEADER.size and data[0] == 1:
            self.packets.put(data)

    def control(self, text: str):
        try:
            msg = json.loads(text)
        except ValueError:
            return
        if msg.get("t") == "kf":
            self.force_key = True

    def close(self):
        self.stop.set()
        self.packets.put(None)
        with self.dec_cond:
            self.dec_cond.notify_all()
        with self.enc_cond:
            self.enc_cond.notify_all()
        for t in self.threads:
            t.join(timeout=2)
        self.encoder = None

    def _text(self, obj):
        asyncio.run_coroutine_threadsafe(self.send_text(json.dumps(obj)), self.loop)

    # ---------- stage 1: decode ----------
    def _decode_loop(self):
        dec = av.CodecContext.create("h264", "r")
        dec.thread_type = "SLICE"       # frame threading would add frames of delay
        dec.options = {"flags": "low_delay"}
        waiting_for_key, asked_at = True, 0.0
        while not self.stop.is_set():
            data = self.packets.get()
            if data is None:
                break
            _, key, fid = HEADER.unpack_from(data)
            if waiting_for_key and not key:
                if time.perf_counter() - asked_at > 0.5:
                    self._text({"t": "kf"})
                    asked_at = time.perf_counter()
                continue
            try:
                frames = dec.decode(av.Packet(data[HEADER.size:]))
            except Exception:  # noqa: BLE001  corrupt or missing reference: restart from a keyframe
                waiting_for_key = True
                continue
            waiting_for_key = False
            for fr in frames:
                yuv = fr.to_ndarray(format="yuv420p")
                bgr = cv2.cvtColor(yuv, cv2.COLOR_YUV2BGR_I420)
                with self.dec_cond:
                    self.decoded = (fid, bgr)       # older undecoded-but-unprocessed frames are dropped
                    self.dec_cond.notify()

    # ---------- stage 2: GPU swap ----------
    def _process_loop(self):
        while not self.stop.is_set():
            with self.dec_cond:
                self.dec_cond.wait_for(lambda: self.decoded is not None or self.stop.is_set(), timeout=0.5)
                if self.decoded is None:
                    continue
                fid, bgr = self.decoded
                self.decoded = None
            t = time.perf_counter()
            out = self.engine._safe_process(bgr, self.track)
            if self.track.space is None:      # hosted users' video never reaches the shared preview
                self.engine._publish(out)
            self.engine.stats["latency_ms"] = round((time.perf_counter() - t) * 1000, 1)
            with self.enc_cond:
                if fid > self.last_processed_id:        # a slower older frame is simply dropped
                    self.last_processed_id = fid
                    self.processed = (fid, out)
                    self.enc_cond.notify()

    # ---------- stage 3: encode and send ----------
    def _encode_loop(self):
        while not self.stop.is_set():
            with self.enc_cond:
                self.enc_cond.wait_for(lambda: self.processed is not None or self.stop.is_set(), timeout=0.5)
                if self.processed is None:
                    continue
                fid, out = self.processed
                self.processed = None
            try:
                self._encode_send(fid, out)
            except Exception as e:  # noqa: BLE001
                traceback.print_exc()
                self.engine.stats.update(status="error", error=f"Video encoder: {e}")
                self.encoder = None

    def _encode_send(self, fid, bgr):
        h, w = bgr.shape[:2]
        s = (self.track.space or self.engine.space).s
        if self.encoder is None or (self.encoder.width, self.encoder.height) != (w, h):
            bitrate = int(s.stream_bitrate_mbps * 1_000_000)
            self.encoder, self.encoder_name = open_encoder(w, h, s.fps, bitrate)
            self.engine.stats["encoder"] = self.encoder_name
            self.force_key = True
        frame = av.VideoFrame.from_ndarray(cv2.cvtColor(bgr, cv2.COLOR_BGR2YUV_I420), format="yuv420p")
        frame.pts = int((time.perf_counter() - self.t0) * 1000)
        if self.force_key:
            try:
                frame.pict_type = av.video.frame.PictureType.I
            except AttributeError:      # older PyAV
                frame.pict_type = "I"
            self.force_key = False
        for pkt in self.encoder.encode(frame):
            payload = HEADER.pack(1, 1 if pkt.is_keyframe else 0, fid) + bytes(pkt)
            fut = asyncio.run_coroutine_threadsafe(self.send_bytes(payload), self.loop)
            fut.result(timeout=5)       # backpressure: never queue more than the socket takes
