"""Sign a release manifest for the installer you built and uploaded.

    python tools/desktop_release.py dist\\MagicPro-Setup-1.1.0.exe --url https://storage.googleapis.com/<bucket>/MagicPro-Setup-1.1.0.exe \\
        --notes "Faster start, new enhancer" [--channel beta] [--min-version 1.0.0]

Prints the signed manifest (and writes it next to the installer as .manifest.json). Paste it in
the admin page under Desktop keys > Latest release, or POST it to /api/admin/desktop/release.
Apps on that channel offer the update within 6 hours or at their next start; --min-version makes
older versions update before they can be used again.
"""
import argparse
import base64
import hashlib
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def b64u_dec(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def sign_manifest(manifest: dict, seed_b64: str) -> dict:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    k = Ed25519PrivateKey.from_private_bytes(b64u_dec(seed_b64.strip()))
    body = {k_: v for k_, v in manifest.items() if k_ != "sig"}
    return {**body, "sig": b64u(k.sign(canonical(body)))}


def main():
    from desktop.version import VERSION
    ap = argparse.ArgumentParser()
    ap.add_argument("installer", type=Path)
    ap.add_argument("--url", required=True, help="https address users download the installer from")
    ap.add_argument("--notes", default="")
    ap.add_argument("--channel", default="stable", choices=["stable", "beta"])
    ap.add_argument("--min-version", default="0.0.0")
    ap.add_argument("--version", default=VERSION)
    ap.add_argument("--key", type=Path, default=ROOT / "keys" / "release.key")
    a = ap.parse_args()
    if not a.url.startswith("https://"):
        sys.exit("--url must be https")
    if not re.fullmatch(r"\d+\.\d+\.\d+", a.version):
        sys.exit("version must look like 1.2.0")
    data = a.installer.read_bytes()
    m = {"channel": a.channel, "version": a.version, "min_version": a.min_version, "notes": a.notes[:300],
         "published": int(time.time()),
         "full": {"url": a.url, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}}
    signed = sign_manifest(m, a.key.read_text(encoding="utf-8"))
    out = json.dumps(signed, indent=2)
    a.installer.with_suffix(".manifest.json").write_text(out + "\n", encoding="utf-8")
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
