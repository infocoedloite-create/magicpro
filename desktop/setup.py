"""Setup on the user's PC: the GPU runtime and the face models, then an NVIDIA check.

The installer stays small (~17 MB): it carries Python and the web server only. While installing
(desktop/install.py, run by the setup EXE) this downloads, once:

1. the GPU runtime (ONNX Runtime GPU with its CUDA / cuDNN libraries, OpenCV, NumPy, PyAV...),
   exactly the versions in desktop/runtime.lock, from PyPI into python\\Lib\\site-packages. A release
   with a different lock installs the new runtime during its (silent) update; the old one stays
   until the new one is complete. If the installer couldn't finish, the program does it on start.
2. the public models (same list as download_models.py) into models\\.

Run from the repository (development) the runtime is whatever the current Python has.
"""
import hashlib
import io
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import zipfile
from pathlib import Path

from . import paths

LOCK = paths.ROOT / "desktop" / "runtime.lock"
MARK = ".magicpro-runtime"


def min_driver(gpu_name=""):
    """CUDA 13 libraries (what the lock pins today) need an R580 driver; CUDA 12.8 needs R570 for
    RTX 50 series (Blackwell) cards and R525 for older ones."""
    try:
        cuda13 = bool(re.search(r"^nvidia-cuda-runtime(-cu13)?==13\.", LOCK.read_text(), re.M))
    except OSError:
        cuda13 = False
    if cuda13:
        return 580.0
    return 570.0 if re.search(r"RTX\s*50\d\d", gpu_name) else 525.0


def runtime_id():
    """Same id the build writes into _build.json: a hash of the lock file."""
    try:
        return "rt-" + hashlib.sha256(LOCK.read_bytes()).hexdigest()[:12]
    except OSError:
        return "dev"


def runtime_installed():
    if not paths.INSTALLED:
        return True
    try:
        return (paths.SITE_PACKAGES / MARK).read_text().strip() == runtime_id()
    except OSError:
        return False


def models_missing():
    import download_models as dm
    need = [dm.ROOT / n for n in ("det_10g.onnx", "w600k_r50.onnx", "2d106det.onnx")] + list(dm.FILES)
    return [p for p in need if not p.exists()]


def gpu_info():
    """-> {"name", "driver", "memory_mb", "ok", "note"} from nvidia-smi (no Python packages needed)."""
    exe = shutil.which("nvidia-smi") or r"C:\Windows\System32\nvidia-smi.exe"
    try:
        out = subprocess.run([exe, "--query-gpu=name,driver_version,memory.total", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=15,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout.strip().splitlines()
    except (OSError, subprocess.SubprocessError):
        out = []
    if not out:
        return {"name": "", "driver": "", "memory_mb": 0, "ok": False,
                "note": "No NVIDIA graphics card found (or its driver isn't installed). Magic Pro needs an NVIDIA RTX card."}
    name, driver, mem = [x.strip() for x in out[0].split(",")][:3]
    info = {"name": name, "driver": driver, "memory_mb": int(float(mem or 0)), "ok": True, "note": ""}
    try:
        drv = float(".".join(driver.split(".")[:2]))
    except ValueError:
        drv = 0.0
    need = min_driver(name)
    if drv < need:
        info.update(ok=False, note=f"Your NVIDIA driver ({driver}) is too old: Magic Pro needs {need:.0f} or newer. Install "
                                   f"the latest Game Ready or Studio driver from nvidia.com, then restart Magic Pro.")
    elif info["memory_mb"] and info["memory_mb"] < 6000:
        info["note"] = "This card has less than 6 GB of video memory: use InSwapper 128 and 720p for smooth video."
    return info


class Setup:
    def __init__(self):
        self.lock = threading.Lock()
        self.s = {"state": "idle", "step": "", "progress": 0.0, "detail": "", "error": "", "log": []}
        self.thread = None
        self._gpu = None

    def needed(self):
        return not runtime_installed() or bool(models_missing())

    def status(self):
        """What the app page may see: a stage and a percentage only, never file, model or package
        names (those stay in the on-disk log). The installer reads self.s directly, not this."""
        with self.lock:
            st = dict(self.s)
        if self._gpu is None:
            self._gpu = gpu_info()
        try:
            pending = len(models_missing())
        except Exception:  # noqa: BLE001  (download_models not importable)
            pending = 0
        return {"state": st.get("state", "idle"), "phase": st.get("phase", ""),
                "overall": st.get("overall", 0.0), "progress": st.get("progress", 0.0),
                "error": bool(st.get("error")), "runtime_ok": runtime_installed(),
                "models_pending": pending, "gpu": self._gpu}

    def _set(self, **kw):
        with self.lock:
            line = kw.pop("line", None)
            self.s.update(kw)
            if line:
                self.s["log"] = (self.s["log"] + [line])[-200:]
            # one overall 0..1 for the whole setup, so the customer sees only a stage and a percentage
            # (never a file name, package name, size or speed); phase is "runtime" then "models"
            phase, p = self.s.get("phase", ""), float(self.s.get("progress") or 0)
            if phase == "runtime":
                self.s["overall"] = 0.45 * p
            elif phase == "models":
                i, n = int(self.s.get("m_i", 1)), max(1, int(self.s.get("m_n", 1)))
                self.s["overall"] = 0.45 + 0.55 * ((i - 1) + p) / n
            elif self.s.get("state") == "done":
                self.s["overall"] = 1.0

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self._run, name="setup", daemon=True)
        self.thread.start()

    def _run(self):
        try:
            self._set(state="running", error="")
            if not runtime_installed():
                self._install_runtime()
            self._download_models()
            self._set(state="done", step="Ready", progress=1.0, detail="")
        except Exception as e:  # noqa: BLE001
            self._set(state="error", error=str(e), line=f"error: {e}")

    # ----- 1. runtime -----
    def _install_runtime(self):
        self._set(phase="runtime", step="Installing the GPU runtime", progress=0.0, detail="Getting the list of packages…")
        pkgs = [ln for ln in LOCK.read_text().splitlines() if ln.strip() and not ln.startswith(("#", "-"))]
        target = paths.SITE_PACKAGES.with_name("site-packages.new")
        shutil.rmtree(target, ignore_errors=True)
        cmd = [str(paths.PYTHON_DIR / "python.exe"), "-m", "pip", "install", "--no-input", "--disable-pip-version-check",
               "--no-warn-script-location", "--only-binary=:all:", "--no-deps", "--target", str(target), "-r", str(LOCK)]
        env = {**os.environ, "PYTHONUTF8": "1", "PIP_PROGRESS_BAR": "off"}
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                             errors="replace", env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        seen = 0
        for line in p.stdout:
            line = line.rstrip()
            if not line:
                continue
            self._set(line=line)
            m = re.match(r"\s*(Collecting|Downloading|Using cached)\s+(\S+)", line)
            if m and m.group(1) == "Collecting":
                seen += 1
                self._set(progress=min(0.95, seen / max(1, len(pkgs)) * 0.9), detail=f"{seen} of {len(pkgs)}: {m.group(2)}")
            elif m:
                size = re.search(r"\(([\d.]+\s*[kMG]B)\)", line)
                self._set(detail=f"Downloading {m.group(2).split('/')[-1][:60]}{' (' + size.group(1) + ')' if size else ''}")
            elif line.startswith("Installing collected packages"):
                self._set(progress=0.95, detail="Unpacking (this takes a minute)…")
        if p.wait() != 0:
            raise RuntimeError("Installing the GPU runtime failed. Check your internet connection and press Try again.")
        (target / MARK).write_text(runtime_id())
        old = paths.SITE_PACKAGES.with_name("site-packages.old")
        shutil.rmtree(old, ignore_errors=True)
        if paths.SITE_PACKAGES.exists():
            paths.SITE_PACKAGES.rename(old)
        target.rename(paths.SITE_PACKAGES)
        shutil.rmtree(old, ignore_errors=True)
        import importlib
        import site
        importlib.invalidate_caches()
        site.addsitedir(str(paths.SITE_PACKAGES))
        self._set(progress=1.0, detail="GPU runtime installed.", line="runtime installed")

    # ----- 2. models -----
    def _download_models(self):
        import download_models as dm
        for sub in ("swappers", "enhancers"):
            (dm.ROOT / sub).mkdir(parents=True, exist_ok=True)
        jobs = []
        if not all((dm.ROOT / n).exists() for n in ("det_10g.onnx", "w600k_r50.onnx", "2d106det.onnx")):
            jobs.append(("buffalo_l.zip", [dm.BUFFALO], None))
        jobs += [(dest.name, urls, dest) for dest, urls in dm.FILES.items() if not dest.exists()]
        for i, (name, urls, dest) in enumerate(jobs, 1):
            self._set(phase="models", step="Downloading the face models", m_i=i, m_n=len(jobs), progress=0.0)
            step = f"Downloading face models ({i} of {len(jobs)})"
            for url in urls:
                try:
                    data = self._fetch(url, step, name)
                    break
                except Exception as e:  # noqa: BLE001
                    self._set(line=f"{url} -> {e}")
            else:
                raise RuntimeError(f"Couldn't download {name}. Check your internet connection and press Try again.")
            if dest is None:
                with zipfile.ZipFile(io.BytesIO(data)) as z:
                    for member in z.namelist():
                        n = Path(member).name
                        if n in ("det_10g.onnx", "w600k_r50.onnx", "2d106det.onnx"):
                            (dm.ROOT / n).write_bytes(z.read(member))
            else:
                tmp = dest.with_suffix(".part")
                tmp.write_bytes(data)
                tmp.replace(dest)

    def _fetch(self, url, step, name):
        req = urllib.request.Request(url, headers={"User-Agent": "MagicProDesktop"})
        with urllib.request.urlopen(req, timeout=60) as r:
            total = int(r.headers.get("Content-Length") or 0)
            buf, done, t0 = io.BytesIO(), 0, time.time()
            while chunk := r.read(1 << 20):
                buf.write(chunk)
                done += len(chunk)
                rate = done / max(0.1, time.time() - t0) / 2**20
                self._set(step=step, progress=done / total if total else 0.0,
                          detail=f"{name}: {done / 2**20:.0f} of {total / 2**20:.0f} MB · {rate:.1f} MB/s")
            return buf.getvalue()
