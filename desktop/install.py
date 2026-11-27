"""Run by the installer (MagicPro-Setup.exe) right after it copies the files:

    python\\python.exe -m desktop.install

installs the GPU runtime (desktop/runtime.lock) and downloads the face models, so the program is
ready the first time it opens. Prints lines the installer shows in its window:

    PROGRESS <0-100> <text>
    NOTE <text>          (shown at the end: no NVIDIA card, old driver...)
    ERROR <text>

Exit code 0 when everything is in place, 2 when something failed (the program then finishes the
setup itself on its first start). Nothing here needs the licence or a server GPU: it only
downloads files to this PC.
"""
import sys
import time

from . import paths


def main():
    paths.setup_env()
    from . import setup as S

    # Only a stage name and a percentage ever reach the installer window: never a file name, a
    # package name, a model name, a size or a speed. Full detail goes to the on-disk log for support.
    STAGE = {"runtime": "Installing the graphics engine", "models": "Downloading the application files"}
    last = [-1]

    def show(pct, stage):
        pct = max(0, min(100, int(pct)))
        if pct != last[0]:
            print(f"PROGRESS {pct} {stage}", flush=True)
            last[0] = pct

    class InstallerSetup(S.Setup):
        def _set(self, **kw):
            super()._set(**kw)
            phase = self.s.get("phase", "")
            if phase in STAGE:
                show(float(self.s.get("overall") or 0) * 100, STAGE[phase])

    show(0, "Preparing")
    gpu = S.gpu_info()
    if gpu["note"]:
        print(f"NOTE {gpu['note']}".encode("ascii", "replace").decode(), flush=True)

    st = InstallerSetup()
    if not st.needed():
        show(100, "Ready")
        return 0
    for attempt in (1, 2):
        try:
            if not S.runtime_installed():
                st._install_runtime()
            st._download_models()
            show(100, "Ready")
            return 0
        except Exception as e:  # noqa: BLE001  the message goes to the on-disk log, not the window
            print(f"[install] attempt {attempt} failed: {e}", flush=True)
            if attempt == 2:
                print("ERROR The download could not be completed.", flush=True)
                return 2
            show(0, "Retrying")
            time.sleep(5)
    return 2


if __name__ == "__main__":
    sys.exit(main())
