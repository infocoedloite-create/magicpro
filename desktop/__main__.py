"""Start Magic Pro Desktop: `MagicPro.exe` runs `python\\pythonw.exe -m desktop`.

    python -m desktop               the app (server + window)
    python -m desktop --no-window   server only, for development (http://127.0.0.1:7860)
    python -m desktop --port 7861

One copy runs at a time (a second start just opens the window again). The app quits when its
window is closed: the window's pages say goodbye on close and send a heartbeat while open.
"""
import argparse
import os
import socket
import sys
import threading
import time

from . import paths

MUTEX = "MagicProDesktop"       # the installer waits for this to disappear before replacing files
IDLE_QUIT = 180                 # no page and no stream for this long: the window is gone
BYE_QUIT = 8                    # a page closed and no other page loaded within this time


def _log_to_file():
    if sys.stdout is not None and sys.stderr is not None and not paths.INSTALLED:
        return
    log = paths.LOGS / "magicpro.log"
    try:
        if log.exists() and log.stat().st_size > 5 * 2**20:
            os.replace(log, log.with_suffix(".log.1"))
    except OSError:
        pass
    f = open(log, "a", buffering=1, encoding="utf-8", errors="replace")   # noqa: SIM115
    sys.stdout = sys.stderr = f
    print(f"\n==== {time.strftime('%Y-%m-%d %H:%M:%S')} Magic Pro Desktop start ====", flush=True)


def _single_instance():
    """-> True when this is the only copy (keeps the mutex for the life of the process)."""
    if sys.platform != "win32":
        return True
    import ctypes
    k32 = ctypes.windll.kernel32
    k32.CreateMutexW.restype = ctypes.c_void_p
    _single_instance.handle = k32.CreateMutexW(None, False, MUTEX)
    return k32.GetLastError() != 183          # ERROR_ALREADY_EXISTS


def _free(port):
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _pick_port(want):
    if _free(want):
        return want
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_up(port, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection(("127.0.0.1", port), 0.5):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def main(argv=None):
    ap = argparse.ArgumentParser(prog="magicpro")
    ap.add_argument("--no-window", action="store_true")
    ap.add_argument("--port", type=int, default=int(os.environ.get("MAGIC_PORT", "7860")))
    a = ap.parse_args(argv)

    paths.setup_env()
    _log_to_file()
    from . import window

    if not _single_instance():
        try:
            port = int(paths.PORT_FILE.read_text())
            window.open_window(f"http://127.0.0.1:{port}/", paths.WINDOW_PROFILE)
        except (OSError, ValueError):
            pass
        return 0

    port = _pick_port(a.port)
    os.environ["MAGIC_PORT"] = str(port)
    paths.PORT_FILE.write_text(str(port))

    import uvicorn

    from .gate import Gate, Life
    from .license import LicenseClient
    from .setup import Setup
    from .updater import Updater

    cfg = paths.CONFIG
    life = Life()
    server = None

    def quit_app():
        print("[desktop] quitting", flush=True)
        if server:
            server.should_exit = True
        w = getattr(life, "window", None)
        if w is not None and w.poll() is None:      # e.g. before an update: close our window too
            try:
                w.terminate()
            except OSError:
                pass
        threading.Timer(8, lambda: os._exit(0)).start()

    lic = LicenseClient(cfg["server"], cfg["license_pubkey"], paths.LICENSE_FILE)
    upd = Updater(cfg["server"], cfg["channel"], cfg["release_pubkey"], paths.UPDATES, on_exit=quit_app)
    setup = Setup()
    gate = Gate(lic, upd, setup, port, life)
    threading.Thread(target=lic.run, name="license", daemon=True).start()
    threading.Thread(target=upd.run, name="updates", daemon=True).start()

    def watch_window():
        if not _wait_up(port):
            print("[desktop] the server didn't start", flush=True)
            return quit_app()
        if a.no_window:
            print(f"Magic Pro Desktop -> http://127.0.0.1:{port}/", flush=True)
            return
        proc = life.window = window.open_window(f"http://127.0.0.1:{port}/", paths.WINDOW_PROFILE)
        life.touch()
        while True:
            time.sleep(2)
            now = time.time()
            if life.sockets > 0:
                continue
            if life.bye and now - life.bye > BYE_QUIT and life.last <= life.bye:
                return quit_app()
            if now - life.last > IDLE_QUIT:
                return quit_app()
            if proc is not None and proc.poll() is not None and now - life.last > 30:
                return quit_app()

    threading.Thread(target=watch_window, name="window", daemon=True).start()
    server = uvicorn.Server(uvicorn.Config(gate, host="127.0.0.1", port=port, log_level="warning", lifespan="on"))
    server.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
