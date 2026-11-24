"""Source-face library: uploaded photos with their ArcFace embeddings on disk."""
import json
import os
import threading
import time
import uuid
from pathlib import Path

import cv2
import numpy as np


class FaceLibrary:
    def __init__(self, root: Path, tools):
        """tools() -> (FaceDetector, FaceRecognizer), loaded lazily."""
        self.root = root
        self.tools = tools
        self.index_file = root / "index.json"
        self.lock = threading.Lock()
        self._emb_cache = {}
        self.items_by_id = {}
        self._mtime = 0.0
        self._load()

    def _load(self):
        items = {}
        if self.index_file.exists():
            self._mtime = self.index_file.stat().st_mtime
            for it in json.loads(self.index_file.read_text(encoding="utf-8")):
                if (self.root / f"{it['id']}.npy").exists():
                    items[it["id"]] = it
        self.items_by_id = items
        self._emb_cache = {}

    def sync(self):
        """Reload if another worker process changed the library."""
        try:
            m = self.index_file.stat().st_mtime
        except FileNotFoundError:
            return
        if m != self._mtime:
            with self.lock:
                self._load()

    def _save(self):
        tmp = self.index_file.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(list(self.items_by_id.values()), indent=1), encoding="utf-8")
        os.replace(tmp, self.index_file)
        self._mtime = self.index_file.stat().st_mtime

    def items(self):
        self.sync()
        with self.lock:
            return sorted(self.items_by_id.values(), key=lambda i: i["created"], reverse=True)

    def add(self, name, data: bytes):
        img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            raise ValueError("Could not read that image. Use JPG, PNG or WEBP.")
        h, w = img.shape[:2]
        if max(h, w) > 2048:
            s = 2048 / max(h, w)
            img = cv2.resize(img, (int(w * s), int(h * s)), interpolation=cv2.INTER_AREA)
        det, rec = self.tools()
        faces = det.detect(img, 640, 0.5)
        if not faces:  # very tight close-ups: give the detector some margin
            pad = int(max(img.shape[:2]) * 0.3)
            padded = cv2.copyMakeBorder(img, pad, pad, pad, pad, cv2.BORDER_CONSTANT, value=0)
            faces = det.detect(padded, 640, 0.45)
            for f in faces:
                f.bbox -= pad
                f.kps -= pad
        if not faces:
            raise ValueError("No face found. Use a clear, well-lit photo where the face is visible.")
        face = faces[0]
        emb = rec.embed(img, face.kps)

        item_id = uuid.uuid4().hex[:12]
        x1, y1, x2, y2 = face.bbox
        cx, cy, side = (x1 + x2) / 2, (y1 + y2) / 2, max(x2 - x1, y2 - y1) * 1.6
        x0, y0 = int(max(cx - side / 2, 0)), int(max(cy - side / 2, 0))
        thumb = img[y0:int(min(cy + side / 2, img.shape[0])), x0:int(min(cx + side / 2, img.shape[1]))]
        thumb = cv2.resize(thumb, (256, 256), interpolation=cv2.INTER_AREA)
        cv2.imwrite(str(self.root / f"{item_id}.jpg"), img, [cv2.IMWRITE_JPEG_QUALITY, 92])
        cv2.imwrite(str(self.root / f"{item_id}_thumb.jpg"), thumb, [cv2.IMWRITE_JPEG_QUALITY, 88])
        np.save(self.root / f"{item_id}.npy", emb)
        item = {
            "id": item_id,
            "name": (name or "Face").strip()[:60],
            "created": time.time(),
            "faces_found": len(faces),
            "score": round(face.score, 3),
            "thumb": f"/library-files/{item_id}_thumb.jpg",
        }
        self.sync()                       # don't overwrite another worker's newer index
        with self.lock:
            self.items_by_id[item_id] = item
            self._save()
        return item

    def delete(self, item_id):
        self.sync()
        with self.lock:
            if self.items_by_id.pop(item_id, None) is None:
                return False
            self._save()
            self._emb_cache.clear()
        for suffix in (".jpg", "_thumb.jpg", ".npy"):
            (self.root / f"{item_id}{suffix}").unlink(missing_ok=True)
        return True

    def embedding(self, ids):
        """Average of the selected faces (several photos of one person give a steadier identity)."""
        key = tuple(sorted(i for i in ids if i in self.items_by_id))
        if not key:
            return None
        cached = self._emb_cache.get(key)
        if cached is not None:
            return cached
        embs = [np.load(self.root / f"{i}.npy") for i in key]
        e = np.mean(embs, axis=0).astype(np.float32)
        e /= np.linalg.norm(e)
        self._emb_cache[key] = e
        return e
