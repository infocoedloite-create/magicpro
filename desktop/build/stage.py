"""Copy the program files into the build folder (dist\\MagicPro) and protect the Python, used by
build.ps1.

    python desktop/build/stage.py <out> --mode pyarmor|pyc|none --python <embedded python.exe>

Protection modes (the face swap code app\\ and desktop\\, and download_models.py):
  pyarmor  Obfuscate every module with PyArmor: the shipped .py are unreadable loaders, the real
           code is an encrypted blob run by pyarmor_runtime (a compiled .pyd). No .py source and no
           .pyc ship. This is the strong option and the default. It needs PyArmor registered with a
           paid licence (`pyarmor reg <regfile.zip>`); the free trial can't obfuscate the whole code
           (it stops with "out of license" on the larger files), so without a licence this mode
           falls back to `pyc` and prints a warning.
  pyc      Compile to .pyc and delete the .py (free, automatic). Raises the bar against casual
           reading, but .pyc can be decompiled: not real obfuscation.
  none     Ship .py as-is (development).

See docs/desktop.md > Protecting the code.
"""
import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SKIP_DIRS = {"__pycache__", "build", ".pytest_cache"}
SKIP_FILES = {"_build.json"}
CODE_DIRS = ("app", "desktop")      # obfuscated/compiled; `static` is browser assets, `desktop/web` is data


def copy_tree(src: Path, dst: Path):
    for p in src.rglob("*"):
        rel = p.relative_to(src)
        if any(part in SKIP_DIRS for part in rel.parts) or p.name in SKIP_FILES or p.name.startswith("test_"):
            continue
        if p.is_file():
            (dst / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, dst / rel)


def to_pyc(python, out):
    """Compile every .py in the code dirs to a sibling .pyc and delete the .py (legacy layout, so the
    embedded Python imports the .pyc directly)."""
    for d in CODE_DIRS:
        subprocess.run([python, "-m", "compileall", "-b", "-q", "-j", "0", str(out / d)], check=True)
        for py in (out / d).rglob("*.py"):
            py.unlink()
        for cache in (out / d).rglob("__pycache__"):
            shutil.rmtree(cache, ignore_errors=True)
    print("protection: compiled to .pyc and removed .py sources (weak; not obfuscation)")


def pyarmor_licensed():
    try:
        out = subprocess.run([sys.executable, "-m", "pyarmor.cli", "--version"], capture_output=True, text=True).stdout
    except OSError:
        return None          # pyarmor not installed
    return "(trial)" not in out and "pyarmor-trial" not in out


def obfuscate(out):
    """Replace the .py in app\\ and desktop\\ with PyArmor-obfuscated loaders; add pyarmor_runtime at
    the install root (it is on sys.path via python312._pth). Raises on failure (e.g. trial limit)."""
    tmp = out.parent / "obf"
    shutil.rmtree(tmp, ignore_errors=True)
    inputs = [str(out / d) for d in CODE_DIRS] + [str(out / "download_models.py")]
    # capture_output so PyArmor's INFO/ERROR lines don't reach the shell (PowerShell would treat
    # native stderr as a failure); re-raised as CalledProcessError so main() can fall back
    r = subprocess.run([sys.executable, "-m", "pyarmor.cli", "gen", "-O", str(tmp), "-r", *inputs],
                       capture_output=True, text=True)
    if r.returncode != 0:
        tail = "\n".join((r.stdout + r.stderr).splitlines()[-3:])
        shutil.rmtree(tmp, ignore_errors=True)
        raise subprocess.CalledProcessError(r.returncode, "pyarmor gen", output=tail)
    runtime = next(tmp.glob("pyarmor_runtime_*"), None)
    if runtime is None:
        raise RuntimeError("PyArmor produced no runtime package.")
    # overlay obfuscated modules over the plain copies (same relative paths)
    for src in list(tmp.glob("app/**/*.py")) + list(tmp.glob("desktop/**/*.py")) + [tmp / "download_models.py"]:
        shutil.copy2(src, out / src.relative_to(tmp))
    shutil.copytree(runtime, out / runtime.name, dirs_exist_ok=True)
    shutil.rmtree(tmp, ignore_errors=True)
    # make sure nothing readable slipped through
    for d in CODE_DIRS:
        for py in (out / d).rglob("*.py"):
            if "from pyarmor_runtime" not in py.read_text(encoding="utf-8", errors="replace")[:200]:
                raise RuntimeError(f"{py} was not obfuscated.")
    print(f"protection: PyArmor-obfuscated app, desktop, download_models.py ({runtime.name})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out", type=Path)
    ap.add_argument("--mode", choices=("pyarmor", "pyc", "none"), default="none")
    ap.add_argument("--python", default="", help="the embedded python.exe (for the pyc mode)")
    a = ap.parse_args()
    out = a.out
    for d in ("app", "static", "desktop"):
        shutil.rmtree(out / d, ignore_errors=True)
        copy_tree(ROOT / d, out / d)
    for stale in out.glob("pyarmor_runtime_*"):      # from an earlier obfuscated build
        shutil.rmtree(stale, ignore_errors=True)
    shutil.copy2(ROOT / "download_models.py", out / "download_models.py")
    (out / "models" / "swappers").mkdir(parents=True, exist_ok=True)
    (out / "models" / "enhancers").mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(ROOT))
    from desktop.version import VERSION
    (out / "desktop" / "_build.json").write_text(json.dumps({"version": VERSION, "built": int(time.time())}), encoding="utf-8")

    mode = a.mode
    if mode == "pyarmor":
        lic = pyarmor_licensed()
        if lic is None:
            print("WARNING: PyArmor is not installed (pip install pyarmor); falling back to .pyc.")
            mode = "pyc"
        else:
            try:
                obfuscate(out)
            except subprocess.CalledProcessError:
                if lic:
                    raise
                print("WARNING: PyArmor is unregistered (trial) and can't obfuscate the whole code. Register a\n"
                      "         licence (`pyarmor reg <regfile.zip>`) for full obfuscation. Falling back to .pyc for\n"
                      "         now (weaker: .pyc can be decompiled).")
                mode = "pyc"
    if mode == "pyc":
        if not a.python:
            raise SystemExit("--python <embedded python.exe> is needed for the pyc fallback.")
        to_pyc(a.python, out)
    elif mode == "none":
        print("protection: none (.py sources shipped) - development build")
    print(f"staged Magic Pro {VERSION} in {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
