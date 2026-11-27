"""Make the two Ed25519 key pairs Magic Pro Desktop needs (once).

    python tools/desktop_keys.py            # writes keys\\ (git-ignored) and the public keys into desktop/desktop.json

* licence key: the website signs licences with the private half (Secret Manager secret
  `desktop-license-key`, env DESKTOP_LICENSE_KEY); the app checks them with the public half.
* release key: you sign release manifests with the private half on your own PC
  (tools/desktop_release.py); it never goes to the website. The app installs only updates it signed.

Keep keys\\ private and backed up. Losing the licence key means rebuilding the app and every user
reinstalling; losing the release key means users must download the next version by hand.
Running this again refuses to overwrite existing keys (use --force only if you mean to start over).
"""
import argparse
import base64
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KEYS = ROOT / "keys"
CONFIG = ROOT / "desktop" / "desktop.json"


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def make():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    k = Ed25519PrivateKey.generate()
    seed = k.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    pub = k.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return b64u(seed), b64u(pub)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="replace existing keys (every installed copy stops accepting licences)")
    a = ap.parse_args()
    KEYS.mkdir(exist_ok=True)
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    for name, field in (("license", "license_pubkey"), ("release", "release_pubkey")):
        priv_file = KEYS / f"{name}.key"
        if priv_file.exists() and not a.force:
            print(f"{priv_file} exists: kept")
            continue
        seed, pub = make()
        priv_file.write_text(seed + "\n", encoding="utf-8")
        (KEYS / f"{name}.pub").write_text(pub + "\n", encoding="utf-8")
        cfg[field] = pub
        print(f"new {name} key: {priv_file}")
    for name, field in (("license", "license_pubkey"), ("release", "release_pubkey")):
        cfg[field] = (KEYS / f"{name}.pub").read_text(encoding="utf-8").strip()
    CONFIG.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    print(f"\nPublic keys written to {CONFIG.relative_to(ROOT)} (commit this file).")
    print("Next, in Secret Manager (project magicprov2) create the secret desktop-license-key with the")
    print(f"contents of {KEYS / 'license.key'}, and set DESKTOP_RELEASE_PUBKEY={cfg['release_pubkey']} on the website.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
