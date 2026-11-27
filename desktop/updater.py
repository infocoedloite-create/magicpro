"""Updates: check the website for a newer signed release, download it, run its installer.

The website serves a manifest (GET /api/desktop/latest) that tools/desktop_release.py signed with
the release key. This copy installs a download only when the manifest's Ed25519 signature checks
out with desktop.json's `release_pubkey` and the file's SHA-256 and size match the manifest. The
installer replaces the program files and keeps settings, the face library, the licence, the
models and the GPU runtime (a new runtime, when a release needs one, is installed at the next
start by desktop/setup.py).
"""
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request

from .license import Offline, Refused, b64u_dec, http_json
from .version import VERSION

CHECK_EVERY = 6 * 3600


def canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def vtuple(v):
    try:
        return tuple(int(x) for x in str(v).split(".")[:3])
    except ValueError:
        return (0,)


def verify_manifest(m: dict, pub_b64: str) -> bool:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    if not pub_b64 or not isinstance(m, dict) or not m.get("sig"):
        return False
    body = {k: v for k, v in m.items() if k != "sig"}
    try:
        Ed25519PublicKey.from_public_bytes(b64u_dec(pub_b64)).verify(b64u_dec(m["sig"]), canonical(body))
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


class Updater:
    def __init__(self, server, channel, pubkey, folder, http=http_json, on_exit=None, allow_http=False):
        self.server, self.channel, self.pubkey, self.folder = server.rstrip("/"), channel, pubkey, folder
        self.http, self.on_exit, self.allow_http = http, on_exit, allow_http
        self.lock = threading.Lock()
        self.manifest = None
        self.s = {"state": "idle", "current": VERSION, "latest": "", "notes": "", "size": 0, "progress": 0.0,
                  "mandatory": False, "message": "", "checked": 0}
        self._busy = False

    def status(self):
        with self.lock:
            return dict(self.s)

    def _set(self, **kw):
        with self.lock:
            self.s.update(kw)

    def check(self):
        if self._busy:
            return self.status()
        self._set(state="checking", message="")
        try:
            m = self.http("GET", f"{self.server}/api/desktop/latest?channel={self.channel}")
        except (Offline, Refused) as e:
            self._set(state="error", message=f"Couldn't check for updates: {e}", checked=time.time())
            return self.status()
        if not m:
            self._set(state="latest", latest=VERSION, checked=time.time())
            return self.status()
        if not verify_manifest(m, self.pubkey):
            self._set(state="error", message="The update information isn't signed by Magic Pro, so it was ignored.",
                      checked=time.time())
            return self.status()
        newer = vtuple(m["version"]) > vtuple(VERSION)
        self.manifest = m if newer else None
        self._set(state="available" if newer else "latest", latest=m["version"], notes=m.get("notes", ""),
                  size=int(m["full"]["size"]), checked=time.time(),
                  mandatory=bool(newer and vtuple(VERSION) < vtuple(m.get("min_version", "0.0.0"))))
        return self.status()

    def install(self):
        """Download and verify in a thread, then start the installer and close the app."""
        if self._busy or not self.manifest:
            return self.status()
        self._busy = True
        threading.Thread(target=self._install, name="update", daemon=True).start()
        return self.status()

    def _install(self):
        m = self.manifest
        f = m["full"]
        try:
            url = f["url"]
            if not (url.startswith("https://") or (self.allow_http and url.startswith("http://127.0.0.1"))):
                raise ValueError("The update address isn't https.")
            self.folder.mkdir(parents=True, exist_ok=True)
            dest = self.folder / f"MagicPro-Setup-{m['version']}.exe"
            if not (dest.exists() and self._sha(dest) == f["sha256"]):
                self._download(url, dest, int(f["size"]), f["sha256"])
            self._set(state="installing", progress=1.0, message="Installing the update. Magic Pro restarts by itself.")
            log = self.folder / f"install-{m['version']}.log"
            args = [str(dest), "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS", f"/LOG={log}"]
            if sys.platform == "win32":
                flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
                subprocess.Popen(args, creationflags=flags, close_fds=True, cwd=str(self.folder))
            else:
                print("[update] would run:", args, flush=True)
            for old in self.folder.glob("MagicPro-Setup-*.exe"):     # keep only this one
                if old != dest:
                    try:
                        old.unlink()
                    except OSError:
                        pass
            if self.on_exit:
                threading.Timer(1.0, self.on_exit).start()
        except Exception as e:  # noqa: BLE001
            self._set(state="error", message=f"Update failed: {e}")
        finally:
            self._busy = False

    @staticmethod
    def _sha(path):
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            while chunk := fh.read(1 << 20):
                h.update(chunk)
        return h.hexdigest()

    def _download(self, url, dest, size, sha):
        self._set(state="downloading", progress=0.0, message="")
        part = dest.with_suffix(".part")
        h, done = hashlib.sha256(), 0
        req = urllib.request.Request(url, headers={"User-Agent": f"MagicProDesktop/{VERSION}"})
        with urllib.request.urlopen(req, timeout=60) as r, open(part, "wb") as out:
            while chunk := r.read(1 << 20):
                out.write(chunk)
                h.update(chunk)
                done += len(chunk)
                if done > size:
                    raise ValueError("The download is bigger than the release says.")
                self._set(progress=round(done / size, 3))
        if done != size or h.hexdigest() != sha:
            os.remove(part)
            raise ValueError("The download is damaged or isn't the signed release (checksum mismatch).")
        os.replace(part, dest)

    def run(self):
        time.sleep(20)          # let the app settle first
        while True:
            try:
                self.check()
            except Exception as e:  # noqa: BLE001
                self._set(state="error", message=str(e))
            time.sleep(CHECK_EVERY)
