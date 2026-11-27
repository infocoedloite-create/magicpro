"""The app window: Microsoft Edge (always on Windows 10/11) or Chrome in app mode, with its own
profile, so it looks like a program window (no tabs, no address bar) and has camera, WebCodecs
and full-screen support. Without either browser the default browser opens a normal tab."""
import os
import subprocess
import sys
import webbrowser
from pathlib import Path


def _app_paths(exe):
    if sys.platform != "win32":
        return []
    import winreg
    out = []
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe}") as k:
                out.append(winreg.QueryValue(k, None))
        except OSError:
            pass
    return out


def find_browser():
    pf = [os.environ.get(v, "") for v in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA")]
    cands = _app_paths("msedge.exe") + [str(Path(p) / "Microsoft" / "Edge" / "Application" / "msedge.exe") for p in pf if p]
    cands += _app_paths("chrome.exe") + [str(Path(p) / "Google" / "Chrome" / "Application" / "chrome.exe") for p in pf if p]
    return next((c for c in cands if c and Path(c).exists()), None)


def open_window(url, profile: Path):
    """-> the browser process (None when it fell back to the default browser)."""
    exe = find_browser()
    if not exe:
        webbrowser.open(url)
        return None
    profile.mkdir(parents=True, exist_ok=True)
    args = [exe, f"--app={url}", f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check",
            "--window-size=1440,900", "--disable-features=msEdgeStartupBoost,TranslateUI",
            # keep the studio running at full speed when the window is behind OBS or minimised
            "--disable-background-timer-throttling", "--disable-renderer-backgrounding",
            "--disable-backgrounding-occluded-windows"]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.Popen(args, creationflags=flags, close_fds=True)
