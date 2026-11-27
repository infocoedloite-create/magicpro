"""This PC's fingerprint for the one-PC-per-key rule.

The licence server needs to tell "the same PC" from "another PC" without seeing raw hardware ids,
so every part is hashed here first. A MAC address alone is a poor id (VPNs, USB adapters, Wi-Fi
MAC randomisation, a new network card), so five parts are read and the server asks that most of
them match (deploy/launcher/licensing.py `same_machine`):

    guid    Windows MachineGuid (changes on a Windows reinstall)
    smbios  motherboard/system UUID
    board   motherboard serial number
    mac     burned-in MAC of the first built-in (PCI) network adapter
    disk    serial number of the system disk

Placeholder values that many motherboards report ("To be filled by O.E.M.", all zeros...) count
as unknown rather than as a shared id.
"""
import hashlib
import json
import os
import platform
import re
import socket
import subprocess
import sys
import uuid

SALT = "magicpro-desktop|v1"
PARTS = ("guid", "smbios", "board", "mac", "disk")
JUNK = re.compile(r"^(|0+|f+|none|null|default string|to be filled by o\.?e\.?m\.?|system serial number|"
                  r"not applicable|not specified|serial ?number|base board serial number|chassis serial number|"
                  r"o\.?e\.?m\.?|n/?a|123456789|03000200-0400-0500-0006-000700080009|"
                  r"00000000-0000-0000-0000-000000000000|ffffffff-ffff-ffff-ffff-ffffffffffff)$", re.I)

PS_SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'
$o = [ordered]@{}
$o.smbios = (Get-CimInstance Win32_ComputerSystemProduct).UUID
$o.board = (Get-CimInstance Win32_BaseBoard).SerialNumber
$o.cpu = (Get-CimInstance Win32_Processor | Select-Object -First 1).Name
$o.gpu = (Get-CimInstance Win32_VideoController | Where-Object { $_.Name -like '*NVIDIA*' } | Select-Object -First 1).Name
$o.os = (Get-CimInstance Win32_OperatingSystem).Caption
$o.disk = (Get-CimInstance -Namespace root\Microsoft\Windows\Storage -ClassName MSFT_Disk -Filter 'IsBoot=TRUE' |
  Select-Object -First 1).SerialNumber
$o.mac = Get-CimInstance -Namespace root\StandardCimv2 -ClassName MSFT_NetAdapter | Where-Object { $_.PnPDeviceID -like 'PCI\*' } |
  ForEach-Object { if ($_.PermanentAddress) { $_.PermanentAddress } else { $_.NetworkAddresses | Select-Object -First 1 } } |
  Sort-Object | Select-Object -First 1
$o | ConvertTo-Json -Compress
"""


def _clean(v) -> str:
    s = re.sub(r"\s+", " ", str(v or "")).strip().strip(".")
    return "" if JUNK.match(s) or len(set(s.replace("-", "").replace(":", ""))) <= 1 else s.upper()


def _hash(name, value) -> str:
    return hashlib.sha256(f"{SALT}|{name}|{value}".encode()).hexdigest()


def _windows_guid() -> str:
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography", 0,
                            winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as k:
            return winreg.QueryValueEx(k, "MachineGuid")[0]
    except OSError:
        return ""


def _windows_hw() -> dict:
    try:
        out = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                              "-Command", PS_SCRIPT], capture_output=True, text=True, timeout=30,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return json.loads(out.stdout.strip() or "{}")
    except (OSError, ValueError, subprocess.SubprocessError):
        return {}


def _fallback_mac() -> str:
    n = uuid.getnode()
    return "" if (n >> 40) & 1 else f"{n:012X}"       # multicast bit set = a random number, not a MAC


def collect() -> dict:
    """-> {"id", "name", "parts": {part: hash}, "os", "cpu", "gpu"} (only hashes leave the PC)."""
    raw, info = {}, {}
    if sys.platform == "win32":
        hw = _windows_hw()
        raw = {"guid": _windows_guid(), "smbios": hw.get("smbios"), "board": hw.get("board"),
               "mac": hw.get("mac"), "disk": hw.get("disk")}
        info = {k: str(hw.get(k) or "").strip() for k in ("os", "cpu", "gpu")}
    else:   # development on Linux/macOS
        for p in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
            if os.path.exists(p):
                raw["guid"] = open(p).read().strip()
                break
        info = {"os": platform.platform(), "cpu": platform.processor()}
    if not _clean(raw.get("mac")):
        raw["mac"] = _fallback_mac()
    parts = {}
    for name in PARTS:
        v = _clean(raw.get(name))
        if name == "mac":
            v = re.sub(r"[^0-9A-F]", "", v)
            v = "" if len(v) != 12 or JUNK.match(v) else v
        if v:
            parts[name] = _hash(name, v)
    mid = hashlib.sha256("|".join(f"{p}={parts.get(p, '')}" for p in PARTS).encode()).hexdigest()[:32]
    name = os.environ.get("COMPUTERNAME") or socket.gethostname() or "PC"
    return {"id": mid, "name": name[:60], "parts": parts, **{k: v[:120] for k, v in info.items() if v}}


if __name__ == "__main__":
    m = collect()
    print(json.dumps({**m, "parts": {k: v[:12] + "…" for k, v in m["parts"].items()}}, indent=2))
