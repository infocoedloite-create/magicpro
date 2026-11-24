# Magic Pro: live webcam face swap

Local web app. It swaps a face from your library onto your webcam in real time, restores
detail with a face enhancer, and outputs 720p or 1080p to a virtual camera for OBS, Zoom,
Meet or a streaming tool. Built for an NVIDIA RTX PRO 6000 on Windows.

```
webcam ─► capture thread (newest frame only) ─► SCRFD detect ─► landmark smoothing
      ─► InSwapper (128 / 128 FP16 / 512 / 512 Live / Studio) ─► mask + colour match
      ─► GFPGAN / GPEN enhancer ─► virtual camera (full res)  +  browser preview (MJPEG)
```

## Setup

```powershell
.\setup.ps1
```

This creates `.venv` with Python 3.12, installs ONNX Runtime GPU with its CUDA 12 / cuDNN 9 pip
packages, and downloads:

| File | Where | Used for |
|---|---|---|
| `det_10g.onnx`, `w600k_r50.onnx` | `models/` | face detection, identity embedding |
| `inswapper_128.onnx`, `inswapper_128_fp16.onnx` | `models/swappers/` | swap |
| `gfpgan_1.4.onnx`, `gpen_bfr_512.onnx` | `models/enhancers/` | enhancer |

**InSwapper 512, 512 Live and Studio are not public downloads.** Copy your `.onnx` files into
`models/swappers/`, for example `inswapper_512.onnx`, `inswapper_512_live.onnx` and
`inswapper_studio.onnx`. They appear in the Swap model list on the next page load. Input size and
FP16/FP32 are read from the model. If a model expects different alignment or normalisation, add a
sidecar JSON with the same name (see the top of `app/swapper.py`).

For the virtual camera output, install **OBS Studio** (30+). Its "OBS Virtual Camera" driver is
what `pyvirtualcam` writes to. Leave OBS's own "Start Virtual Camera" off.

## Run

```powershell
.\run.ps1
```

Opens <http://127.0.0.1:7860>. Lowest latency on RTX (needs TensorRT 10 installed; the first start
builds engines for a few minutes, then they are cached):

```powershell
.\run.ps1 -TensorRT
```

1. Upload one or more photos under **Face library** and tick the consent box. Ctrl-click several
   photos of the same person to blend them; this gives a steadier identity from different angles.
2. Pick a swap model and an enhancer, then press **Start camera**.
3. In OBS (or Zoom, Meet, Discord) choose **OBS Virtual Camera** as the video source. For an OBS
   browser source instead, use `http://127.0.0.1:7860/video.mjpg?full=1`.

## Run on a rented GPU (Vast.ai)

Use the **NVIDIA CUDA** template (CUDA 12.8 or newer). Copy the project to `/workspace/magicpro`
on the instance, then:

```bash
bash /workspace/magicpro/deploy/vast/install.sh
```

This installs everything into `.venv`, downloads the models, runs the app as the supervisor
service `magicpro` on `127.0.0.1:17860`, and puts it behind the instance's token auth on open
port 10100. Logs: `/var/log/portal/magicpro.log`.

Open it from your PC with an SSH tunnel. It is private, needs no token, and the browser counts it
as localhost, so webcam access is allowed:

```powershell
ssh -i $HOME\.ssh\vast_magicpro -p <ssh port> -N -L 7860:127.0.0.1:17860 root@<instance ip>
```

Then browse to <http://localhost:7860>, leave **Webcam input** on "This browser's webcam", and
press **Start camera**. Your webcam streams to the GPU and the swapped video comes back. Round
trip is shown on screen. For OBS, full-screen the output and use Window Capture.

## GPU on only while someone streams (auto on/off)

A Vast.ai GPU bills every second it runs, used or not. Two pieces keep it off when nobody streams:

* **Watchdog** on the instance (`deploy/vast/watchdog/`), a supervisor service next to `magicpro`.
  It reads how many people are streaming from the app (`/api/gpu/status`). A person counts while
  their stream is connected **and** their page sent a heartbeat in the last 15 s (pages send one
  every 5 s), so closed or crashed tabs and dropped Wi-Fi stop counting. When the count is 0 for
  the grace period (`MAGIC_IDLE_GRACE`, default 600 s) it stops the instance; anyone streaming again
  before then cancels it. Dead-man rule: no streams and no launcher contact for `MAGIC_DEADMAN`
  (900 s) stops it as well, even with the grace turned off (`MAGIC_IDLE_GRACE=0`). It stops the
  instance with the instance-scoped `CONTAINER_API_KEY` that Vast puts in every container, so it
  needs no key from you. **Stop GPU now** in the page asks the watchdog to stop at once (refused
  while someone else streams, unless you confirm).
* **Launcher** (`deploy/launcher/`), a small always-on page on Google Cloud Run, so there is
  something to open while the GPU is off. Sign in, press **Start camera**: it starts the instance,
  shows "Starting GPU…", waits until the app answers, then opens the app's current HTTPS address
  (the Cloudflare quick tunnel changes on every restart) and starts the camera. If the stopped GPU
  was rented by someone else, it says so and offers **Cancel start**. Cloud Run costs nothing
  noticeable at this traffic (it scales to zero).

Stopping keeps the disk (storage is still billed, about $0.20/h here). Restarting takes 1–2 min.

### 1. Watchdog (on the instance)

```bash
bash /workspace/magicpro/deploy/vast/watchdog/install.sh                       # test mode: only logs "would stop"
MAGIC_WATCHDOG_DRY_RUN=0 bash /workspace/magicpro/deploy/vast/watchdog/install.sh   # really stop the GPU
tail -f /var/log/portal/magicpro-watchdog.log
```

Settings live in `/etc/magicpro-watchdog.env` (`supervisorctl restart magicpro-watchdog` after an
edit). To keep the GPU on while you work over SSH with nobody streaming (benchmarks, updates):
`mkdir -p /tmp/magicpro-gpu && touch /tmp/magicpro-gpu/hold` keeps it on for 4 h from the touch;
`rm /tmp/magicpro-gpu/hold` releases it. Rule tests: `python3 deploy/vast/watchdog/test_watchdog.py`.

### 2. Launcher (Google Cloud Run)

1. **Vast.ai API key:** cloud.vast.ai → Account → API Keys → create a key (limit it to instance
   read/write if the form offers permissions). Don't paste it into chat or a terminal.
2. **Secret Manager** (console.cloud.google.com → Security → Secret Manager, project `magicprov2`):
   create secret `vast-api-key` with that key as its value, and secret `launcher-password` with
   the password people will use to sign in to the launcher.
3. **Deploy** (Cloud Shell or a PC with `gcloud`), from the project folder:

```bash
PROJECT=magicprov2; REGION=asia-south1       # any region works; the launcher carries no video
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com secretmanager.googleapis.com --project $PROJECT
gcloud iam service-accounts create magicpro-launcher --project $PROJECT
for s in vast-api-key launcher-password; do
  gcloud secrets add-iam-policy-binding $s --project $PROJECT \
    --member serviceAccount:magicpro-launcher@$PROJECT.iam.gserviceaccount.com --role roles/secretmanager.secretAccessor
done
gcloud run deploy magicpro-launcher --source deploy/launcher --project $PROJECT --region $REGION \
  --service-account magicpro-launcher@$PROJECT.iam.gserviceaccount.com \
  --allow-unauthenticated --min-instances 0 --max-instances 1 --memory 256Mi \
  --set-env-vars VAST_INSTANCE_ID=62324193 \
  --set-secrets VAST_API_KEY=vast-api-key:latest,LAUNCHER_PASSWORD=launcher-password:latest
```

The command prints the launcher's `https://…run.app` address; bookmark it. `--allow-unauthenticated`
only lets the sign-in page load; everything else needs the password (a signed cookie, 14 days,
10 wrong tries per 10 min per address). Keep `--max-instances 1`: start/rented-GPU timing is kept
in memory. Change the password by adding a new version of `launcher-password` and redeploying.

How the launcher reaches the instance: the Vast API gives it the instance's public address, the
mapped port of container port 10100 (the app behind Caddy token auth) and that token. It asks the
app for its quick-tunnel URL (`/api/gpu/tunnel`) and sends the browser to
`<tunnel>/?token=…&autostart=1`; Caddy turns the token into a cookie and the page removes it from
the address bar. For a fixed address instead of the changing quick tunnel, create a named
Cloudflare tunnel and launch the instance with `CF_TUNNEL_TOKEN` (it then bypasses Caddy, so put
Cloudflare Access in front of it).

## Quality and latency notes

- **Lip sync.** Every frame is swapped from your live face, so the mouth follows your speech
  exactly; the audio is never touched. Turn on **Keep real mouth** to show your real teeth and
  tongue. In OBS, delay the microphone by the **Latency** number on screen
  (Audio Mixer ► Advanced Audio Properties ► Sync Offset) so audio and picture line up.
- **Side views.** Detector size 640, threshold around 0.3 and 3 to 6 hold frames keep the swap on
  through head turns. Past about 70° from the front, the 5-point models lose quality. That is a
  limit of every InSwapper model, not a setting.
- **Speed.** A frame is never queued: if processing falls behind, older camera frames are dropped,
  so latency stays at about one frame plus processing time. For 60 fps at 1080p: TensorRT, InSwapper
  128 FP16, GFPGAN, detector size 320 to 480, one face. 512 models and CodeFormer cost the most.
- **Coverage.** Use the Trim sliders and Mask feather to fit the swap to the face edge (hairline,
  jaw). Skin tone match evens out lighting differences.

## Responsible use

Only swap faces of people who agreed to it, including your own. Live streaming as someone else
without consent, or to deceive viewers, is illegal in many places and breaks most platforms' rules.
The "AI face swap" label on the output is on by default. `inswapper_128` is released by InsightFace
for **non-commercial research use only**; check the licence of every model you add.
