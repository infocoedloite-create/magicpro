"""Download the public models. InSwapper 512 / 512 Live / Studio are not public: copy those
.onnx files into models/swappers yourself."""
import io
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent / "models"
FF = "https://github.com/facefusion/facefusion-assets/releases/download"

FILES = {
    ROOT / "swappers" / "inswapper_128.onnx": [f"{FF}/models-3.0.0/inswapper_128.onnx", f"{FF}/models/inswapper_128.onnx"],
    ROOT / "swappers" / "inswapper_128_fp16.onnx": [f"{FF}/models-3.0.0/inswapper_128_fp16.onnx", f"{FF}/models/inswapper_128_fp16.onnx"],
    ROOT / "enhancers" / "gfpgan_1.4.onnx": [f"{FF}/models-3.0.0/gfpgan_1.4.onnx", f"{FF}/models/gfpgan_1.4.onnx"],
    ROOT / "enhancers" / "gpen_bfr_512.onnx": [f"{FF}/models-3.0.0/gpen_bfr_512.onnx", f"{FF}/models/GPEN-BFR-512.onnx"],
}
BUFFALO = "https://github.com/deepinsight/insightface/releases/download/v0.7/buffalo_l.zip"


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "model-downloader"})
    with urllib.request.urlopen(req, timeout=60) as r:
        total = int(r.headers.get("Content-Length") or 0)
        buf, done = io.BytesIO(), 0
        while chunk := r.read(1 << 20):
            buf.write(chunk)
            done += len(chunk)
            if total:
                print(f"\r  {done / 2**20:7.1f} / {total / 2**20:.1f} MB", end="", flush=True)
        print()
        return buf.getvalue()


def main():
    for sub in ("swappers", "enhancers"):
        (ROOT / sub).mkdir(parents=True, exist_ok=True)

    if not all((ROOT / n).exists() for n in ("det_10g.onnx", "w600k_r50.onnx", "2d106det.onnx")):
        print("buffalo_l (SCRFD detector + ArcFace + 106 landmarks)")
        with zipfile.ZipFile(io.BytesIO(fetch(BUFFALO))) as z:
            for member in z.namelist():
                name = Path(member).name
                if name in ("det_10g.onnx", "w600k_r50.onnx", "2d106det.onnx"):
                    (ROOT / name).write_bytes(z.read(member))

    failed = []
    for dest, urls in FILES.items():
        if dest.exists():
            continue
        print(dest.name)
        for url in urls:
            try:
                dest.write_bytes(fetch(url))
                break
            except Exception as e:  # noqa: BLE001
                print(f"  {url} -> {e}")
        else:
            failed.append(dest.name)

    print("\nDone." if not failed else f"\nCould not download: {', '.join(failed)}")
    print("Put InSwapper 512 / 512 Live / Studio .onnx files in models/swappers to use them.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
