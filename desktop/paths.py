"""Where things live, and the environment the studio is started with.

Installed layout (per user, no admin rights, so updates install silently):

    %LOCALAPPDATA%\\Programs\\MagicPro\\    MagicPro.exe, python\\ (embedded 3.12), app\\, static\\, desktop\\, models\\
    %APPDATA%\\MagicPro\\                  license.dat, settings.json, library\\, logs\\   (kept across updates)
    %LOCALAPPDATA%\\MagicPro\\             window\\ (the app window's browser profile), updates\\

Run from the repository (python -m desktop) the same per-user folders are used, with the repo as
the install folder.
"""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYTHON_DIR = ROOT / "python"
INSTALLED = (PYTHON_DIR / "python.exe").exists() and (PYTHON_DIR / "python312._pth").exists()
SITE_PACKAGES = PYTHON_DIR / "Lib" / "site-packages"

_home = Path.home()
DATA = Path(os.environ.get("MAGIC_DESKTOP_DATA") or Path(os.environ.get("APPDATA") or _home / "AppData" / "Roaming") / "MagicPro")
CACHE = Path(os.environ.get("MAGIC_DESKTOP_CACHE") or Path(os.environ.get("LOCALAPPDATA") or _home / "AppData" / "Local") / "MagicPro")
LOGS = DATA / "logs"
LICENSE_FILE = DATA / "license.dat"
UPDATES = CACHE / "updates"
WINDOW_PROFILE = CACHE / "window"
PORT_FILE = CACHE / "port"


def _json(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


CONFIG = {"server": "https://magicpro-desk.nacgenai.in", "buy_url": "https://magicpro.nacgenai.in",
          "channel": "stable", "license_pubkey": "", "release_pubkey": "",
          **_json(ROOT / "desktop" / "desktop.json")}
# the licence server may be pointed elsewhere for testing; the public keys can't be (a licence or
# an update still has to be signed by the keys built into this copy)
CONFIG["server"] = os.environ.get("MAGIC_LICENSE_SERVER", CONFIG["server"]).rstrip("/")
BUILD = {"runtime": "dev", "built": 0, **_json(ROOT / "desktop" / "_build.json")}


def setup_env():
    """Before app.config is imported: per-user data outside the install folder, no browser tab."""
    for d in (DATA, CACHE, LOGS, UPDATES):
        d.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("MAGIC_LIBRARY", str(DATA / "library"))
    os.environ.setdefault("MAGIC_SETTINGS", str(DATA / "settings.json"))
    os.environ.setdefault("MAGIC_DATA", str(DATA / "data"))
    os.environ["MAGIC_OPEN_BROWSER"] = "0"
    os.environ.pop("MAGIC_SESSION_SECRET", None)      # the desktop app is never the hosted service
    os.environ["MAGIC_HOST"] = "127.0.0.1"
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
