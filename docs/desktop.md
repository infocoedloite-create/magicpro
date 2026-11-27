# Magic Pro Desktop

The studio as a Windows program that runs on the customer's own NVIDIA card (built for the RTX
5070), sold as a subscription. There is no home page: the program opens straight into the studio.
One subscription key works on one PC and one e-mail address. When the subscription ends, the
program locks until it is renewed. Customers sign in with any e-mail address (Gmail, Outlook,
Yahoo, work) and a 6-digit code sent to it. Updates install from inside the program.

**Everything runs on the customer's PC.** The face swap uses only that PC's NVIDIA GPU and
processor; no video ever leaves the PC and no server or rented GPU is used (the hosted service's
GPU-pool controls are switched off in the desktop app). The internet is used only for the licence
check, updates, and the one-time downloads at install. The installer sets up everything the program
needs: the GPU runtime, the face models, the Microsoft C++ runtime, and optionally OBS Studio.

**One admin, two front doors.** Both are the same code, deployed as two Cloud Run services that
share one Firestore, one set of secrets, one Google sign-in (OAuth client) and one Razorpay gateway:

| Service | Domain | What it's for |
|---|---|---|
| `magicpro-web` | **`magicpro.nacgenai.in`** | the public site, payments, and **the one admin dashboard** for everything: streaming users, packages, GPUs *and* **Desktop keys** (create keys, add days, move PCs, publish releases). Its **Desktop app** link sends people to the desktop site's download page (`DESKTOP_DOWNLOAD_URL`). |
| `magicpro-desk` | **`magicpro-desk.nacgenai.in`** | what the desktop app talks to: activation with e-mail codes, licence renewal, updates, and the installer download page. |

Keys created on the `magicpro.nacgenai.in` admin page work in the desktop app straight away
(shared Firestore). Buy/Renew in the app opens `magicpro.nacgenai.in` (`buy_url` in
`desktop/desktop.json`), so payments stay on the one gateway.

The face-swap code (`app/`, `static/`) is unchanged. `desktop/` wraps it.

```
MagicPro.exe ─► python\pythonw.exe -m desktop
                 ├─ licence client ──── https://magicpro-desk.nacgenai.in/api/desktop/*  (deploy/launcher/licensing.py)
                 ├─ updater ─────────── /api/desktop/latest  (manifest signed with your release key)
                 ├─ setup (at install) ─ PyPI (GPU runtime, pinned in desktop/runtime.lock) + model downloads
                 └─ gate (ASGI) ─┬─ /desktop/*   activation, setup, updates
                                 └─ everything else ─► app.server (the studio), only while licensed
                 window: Microsoft Edge (or Chrome) in app mode, own profile, no tabs or address bar
```

## How the licence works

1. **Keys.** An admin creates keys on the admin page at `magicpro.nacgenai.in/admin` (**Desktop keys**): how many, how many days,
   a plan name, optionally the only e-mail address allowed to use them. Keys look like
   `MPRO-7KQ2M-XW9HD-R4TNB-3CFZA`. The days start counting at the first activation.
2. **Activation.** The customer types the key and their e-mail address in the program and
   presses **E-mail me a code**. The website e-mails a 6-digit code (valid 10 minutes, 5 tries,
   at most one code per 30 s and five per hour per address); they type it in the program and it is
   activated. No browser step. A code is only sent for a valid key, or to an address that already
   owns a key (activating the same PC again without the key), so the form can't be used to send
   mail to strangers. If the address also signed in on the website with Google, it is the same
   account. (The older "sign in with Google in the browser" activation is still on the website,
   `/desktop/activate/<request>`, but the program no longer offers it.)
3. **One PC, one account.** On first activation the key is tied to that e-mail address and that
   PC. Another PC gets "This key is already active on another PC", another address gets "already
   used by another e-mail address". **Move to new PC** on the admin page frees the PC (the same
   address can then activate a new one); **Free from account** frees both (to give the key to
   someone else). A key created with "Only for e-mail" works only with that address.
4. **The PC's identity.** A MAC address alone changes too easily (VPN adapters, USB network
   sticks, Wi-Fi MAC randomisation, a new network card), so the program reads five parts: the
   Windows machine id, the SMBIOS UUID, the motherboard serial, the burned-in MAC of the built-in
   network card and the system disk's serial. Each is hashed on the PC; only hashes are sent. The
   website accepts the PC when most of the parts it knows match (and at least two). So a Windows
   reinstall or a new network card is fine, but the disk moved into another PC, or a cloned
   Windows, is refused. Run `python -m desktop.machine` to see what a PC reports.
5. **The licence file** is an Ed25519-signed token (made with the website's private key, checked
   with the public key built into the program) naming the PC, the e-mail address and two times: how long
   it may work offline (72 h, `DESKTOP_OFFLINE_HOURS`) and the end of the subscription. It is
   stored in `%APPDATA%\MagicPro\license.dat`, encrypted with Windows DPAPI for that Windows user.
6. **Renewal.** At every start and every 30 minutes the program renews the token online. A clear
   "no" from the website (subscription ended, key cancelled, account blocked, other PC) locks the
   program at once: the camera stops, live streams close, and the studio shows **Your subscription
   has ended** with **Renew on the website** and **I renewed: check again**. Without internet it
   keeps working until the 72 h run out, never past the subscription's end. Setting the PC's
   clock back doesn't help (the newest time ever seen is kept).

Admin actions (all in the audit log): **Add days** (also on an expired key: the same PC works
again after **check again**), **Cancel key** / **Restore**, **Move to new PC**, **Free from
account**. Customers see their keys and PCs on `/download` when signed in.

## One-time setup (you)

1. Make the signing keys, once, on your own PC:

   ```powershell
   py -3.12 tools\desktop_keys.py
   ```

   It writes `keys\license.key`, `keys\release.key` (private, git-ignored: **back them up
   somewhere safe**) and puts the two public keys into `desktop\desktop.json` (commit that). Losing
   the licence key means every customer must reinstall a rebuilt program; losing the release key
   means customers must download the next version by hand.
2. Secret Manager (project magicprov2): create the secret **`desktop-license-key`** with the
   contents of `keys\license.key`. `deploy-magicprov2.sh secrets` gives the website read access;
   `deploy` passes it as `DESKTOP_LICENSE_KEY` and passes the release public key from
   `desktop\desktop.json` as `DESKTOP_RELEASE_PUBKEY` (the admin page then refuses manifests you
   didn't sign).
3. Redeploy the website (`bash deploy/launcher/deploy-magicprov2.sh secrets` then `deploy`). The
   admin page's **Desktop keys** panel says if the licence key is missing.
4. **E-mail for the sign-in codes.** Pick any SMTP service: Brevo (free tier: 300 e-mails a day),
   Resend, SendGrid, Zoho, Amazon SES, or a Google Workspace / Gmail app password. Verify your
   sending domain there (SPF/DKIM records at GoDaddy) so the codes don't land in spam. Create the
   secret **`smtp-password`** and deploy with
   `SMTP_HOST=… SMTP_PORT=587 SMTP_USER=… MAIL_FROM="Magic Pro <no-reply@nacgenai.in>"` set
   (port 465 means SSL). Cloud Run allows ports 587 and 465. The admin page says if mail isn't set up.
5. **Both services are deployed** (project magicprov2, asia-southeast1): `magicpro-web` got the new
   code with its live settings kept, plus `DESKTOP_LICENSE_KEY`, `DESKTOP_RELEASE_PUBKEY` and
   `DESKTOP_DOWNLOAD_URL`; `magicpro-desk` is new, with Gmail SMTP for the codes and the domain
   mapping for `magicpro-desk.nacgenai.in`. Still to do by hand:
   * GoDaddy (DNS for nacgenai.in): `CNAME` `magicpro-desk` → `ghs.googlehosted.com.`
   * the Gmail app password as the real value of the `smtp-password` secret (until then the codes
     can't be sent): `gcloud secrets versions add smtp-password --project magicprov2 --data-file=<file>`
   * OAuth client → Authorized redirect URIs: add `https://magicpro-desk.nacgenai.in/auth/callback`,
     so "Sign in" on the desktop site works too (admin work happens on `magicpro.nacgenai.in`, whose
     sign-in already works).

**Shipping new code later** (both services at once, every env var and secret kept):

```bash
bash deploy/launcher/deploy-magicprov2.sh redeploy
```

Don't use `deploy` for existing services: it resets every env var (it would put the live site's
payments in test mode), so it now refuses to run on a service that already exists. To change one
setting: `gcloud run services update <service> --project magicprov2 --region asia-southeast1
--update-env-vars KEY=value` (or `--update-secrets`).

## Build

On a Windows PC with Python 3.12 (`py -3.12`) and Inno Setup 6.3 or newer (`winget install JRSoftware.InnoSetup`):

```powershell
.\desktop\build\build.ps1
```

This makes `dist\MagicPro\` (the program folder: embedded Python 3.12, the web server, the
program files, `MagicPro.exe`, the Microsoft C++ runtime DLLs) and
`dist\MagicPro-Setup-<version>.exe` (about 17 MB). While it installs, the setup downloads and
installs the GPU runtime (exactly the versions and file hashes in `desktop\runtime.lock`) and the
public face models, about 4 GB, with a progress bar (`python -m desktop.install`). If the internet
drops it offers Retry; if the customer cancels, the program finishes the downloads on its first
start. Silent updates run the same step, so a release with a new runtime arrives ready to use.

| Switch | What it does |
|---|---|
| `-Relock` | resolve `desktop\requirements-desktop.txt` (= the studio's `requirements.txt` + cryptography) again with the newest versions, into `runtime.lock`. Every PC reinstalls its runtime at the next start. Test before you release. |
| `-Relock -TensorRT` | also add TensorRT 10 from pypi.nvidia.com (see RTX 5070 notes) |
| `-Protect` | ship `.pyc` instead of `.py` for `app\` and `desktop\` |
| `-WithRuntime` | put the GPU runtime in the build too (an offline installer for testing; ~3 GB) |
| `-NoInstaller` | only the program folder |

The installer installs per user into `%LOCALAPPDATA%\Programs\MagicPro` (no admin rights, so
updates install silently), adds Start menu and optional desktop shortcuts, installs the GPU runtime
and models, offers **Install OBS Studio** when OBS isn't there (via winget, which asks Windows for
permission; otherwise it opens obsproject.com), warns if there's no NVIDIA card or the driver is too
old, and starts the program. The NVIDIA driver itself can't be installed by the setup: customers
get it from nvidia.com (or the NVIDIA App).
Uninstalling removes the program, runtime and models; licence, settings and face library stay in
`%APPDATA%\MagicPro`.

Without code signing, Windows SmartScreen warns on first download ("Windows protected your PC" →
More info → Run anyway). A code-signing certificate (signtool on `MagicPro.exe` and the setup) removes
that; it isn't part of this build.

## Release an update

1. Bump `VERSION` in `desktop\version.py`, build.
2. Upload `dist\MagicPro-Setup-<version>.exe` somewhere public over https (for example a Google
   Cloud Storage bucket with public read, or a GitHub release).
3. Sign the manifest on your PC (needs `keys\release.key`):

   ```powershell
   py -3.12 tools\desktop_release.py dist\MagicPro-Setup-1.1.0.exe --url https://storage.googleapis.com/<bucket>/MagicPro-Setup-1.1.0.exe --notes "What changed"
   ```

   `--min-version 1.1.0` makes older versions update before they can be used again;
   `--channel beta` publishes to testers only (set `"channel": "beta"` in their `desktop.json`).
4. Paste the printed JSON on the admin page under **Desktop keys → Latest release → Publish**.

Programs check at start and every 6 hours. The studio's top bar then shows **Update x.y.z ready**;
the program's page has **Update to x.y.z**. It downloads the installer, checks its SHA-256 and the
manifest's signature with the release public key built into the program, closes, installs and
starts again. A manifest that isn't signed by you is ignored, even if the website were
compromised. The download page `/download` always offers the latest installer for new customers.

## The customer's first start

1. Run the installer. It copies the program, installs the GPU runtime and downloads the face
   models with a progress bar (5–20 minutes, once), optionally installs OBS Studio, and checks the
   NVIDIA card and driver. Magic Pro then opens on **Activate Magic Pro on this PC**.
2. Key + e-mail address → **E-mail me a code** → type the 6-digit code → **Activate**.
3. The studio opens. The licence chip in the top bar shows the days left; clicking it opens the
   licence and update page.

For OBS / Zoom output the studio's virtual camera needs **OBS Studio** installed (its "OBS Virtual
Camera" driver), which the installer offers. Closing the Magic Pro window quits the program.

Logs: `%APPDATA%\MagicPro\logs\magicpro.log`. Update logs: `%LOCALAPPDATA%\MagicPro\updates`.

## RTX 5070 notes

* The pinned runtime is ONNX Runtime GPU 1.32 with **CUDA 13** and cuDNN 9 from PyPI. CUDA 13
  needs NVIDIA driver **580 or newer**; the setup page says so if the driver is older.
* 12 GB of video memory: start with InSwapper 128 FP16 and GFPGAN at 1080p. 512 models and
  CodeFormer need much more memory and time (see the README's quality and latency notes).
* TensorRT: the studio uses it automatically when its libraries are installed, but PyPI has no
  Windows TensorRT wheels (`-TensorRT` tries NVIDIA's own index, pypi.nvidia.com) and the pinned
  runtime is CUDA 13 while `tensorrt-cu12-libs` is built for CUDA 12. The default build therefore
  uses the CUDA provider. Add TensorRT only after testing it on an RTX 5070.
* The InSwapper 512 / 512 Live / Studio models aren't public downloads: copy them into
  `%LOCALAPPDATA%\Programs\MagicPro\models\swappers` (they survive updates).

## Protecting the code

Every build protects the Python in `app\` and `desktop\` so the shipped program holds no readable
source. `desktop\build\build.ps1` does it automatically (the `-Protect` switch picks the mode):

* **`pyarmor`** (default) - PyArmor obfuscates every module: the files on disk are small loaders
  that run an encrypted blob through `pyarmor_runtime` (a compiled `.pyd`). No `.py` and no `.pyc`
  ship, and the code can't be read or decompiled back to Python. This needs PyArmor registered with
  a paid licence (buy at [pyarmor.dashingsoft.com](https://pyarmor.dashingsoft.com/), then once
  `py -3.12 -m pyarmor.cli reg <regfile.zip>`); the **free trial can't obfuscate the whole code**
  (it stops on the larger files), so until a licence is registered the build **falls back to `pyc`
  and prints a warning**.
* **`pyc`** - compiles to `.pyc` and deletes the `.py`. Free and automatic, but `.pyc` can be
  decompiled: it raises the bar, it is not real obfuscation. This is what ships until you register a
  PyArmor licence.
* **`none`** - ships `.py` (development only).

No client-side protection is unbreakable (the code still runs on the customer's PC), but PyArmor
obfuscation plus the signed licence (which can't be forged) is the standard, strong setup. The
licence check, machine binding and the signing keys are all inside the obfuscated code.

Do this once, then every build and every update is obfuscated with no extra step:

```powershell
py -3.12 -m pip install pyarmor
py -3.12 -m pyarmor.cli reg path\to\pyarmor-regfile-xxxx.zip
```

### What the customer sees while installing

The setup window and the in-app setup screen show only a **stage name and a percentage** -
"Installing the graphics engine", then "Downloading the application files" - never a file name, a
model name, a package name, a size or a download speed. The full detail is written to the on-disk
log (`%APPDATA%\MagicPro\logs`) for your own support use.

## Limits (read before selling)

* **Model licence.** `inswapper_128` is released by InsightFace for non-commercial research use
  only. Selling subscriptions to a program that downloads and runs it needs a commercial licence
  for every model it uses. The same applies to the face enhancers. Get that sorted first.
* **Client-side checks can be patched.** The licence can't be forged (it is signed), but the
  program that checks it runs on the customer's PC in Python. Someone determined can edit it to
  skip the check. `-Protect` (no `.py` sources) stops casual edits only. Stronger options are
  compiling `desktop\` with Nuitka or Cython, a code-signing certificate, or keeping part of the
  work on the server. No desktop licence is uncrackable.
* **Purchases.** Keys are created by admins. Selling them automatically (Razorpay plan → key by
  e-mail) is a separate step, not built yet.
* Development: `python -m desktop --no-window` runs the gate from the repository (with the studio's
  `.venv` Python). `MAGIC_LICENSE_SERVER=http://127.0.0.1:8080` points it at a local website. Tests:
  `python deploy/launcher/test_desktop.py` (website + licence client + updater + gate, offline).

## Files

| Path | What |
|---|---|
| `desktop/__main__.py` | start: one copy at a time, web server, app window, quit when the window closes |
| `desktop/gate.py` | the studio behind the licence; `/desktop/*` pages and API |
| `desktop/license.py` | activation, signed licence, DPAPI storage, renewal, lock |
| `desktop/machine.py` | the PC fingerprint |
| `desktop/updater.py` | signed manifest check, download, silent install |
| `desktop/setup.py` | GPU runtime + models install, NVIDIA driver check (also the in-app fallback) |
| `desktop/install.py` | the installer's setup step (progress lines for the setup window) |
| `desktop/window.py` | Edge/Chrome app window |
| `desktop/web/` | activation/setup/update page and the studio's licence chip |
| `desktop/desktop.json` | licence server, update channel, the two public keys |
| `desktop/runtime.lock` | exact GPU runtime for users' PCs (`desktop/build/lock.py`) |
| `desktop/build/` | `build.ps1`, `MagicPro.iss` (Inno Setup), `launcher.cs` (MagicPro.exe), icon, staging |
| `deploy/launcher/licensing.py` | website side: keys, activation, refresh, releases, admin API |
| `tools/desktop_keys.py`, `tools/desktop_release.py` | signing keys, signed release manifests |
