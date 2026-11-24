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

## GPU pool: on only while someone streams, more GPUs as users arrive

A Vast.ai GPU bills every second it runs, used or not. Three pieces keep the bill to what is used:

* **Node watchdog** (`deploy/vast/watchdog/`), a supervisor service on every GPU instance. It
  reads how many people stream on that node from the app (`/api/gpu/status`). A person counts
  while their stream is connected **and** their page sent a heartbeat in the last 15 s (pages send
  one every 5 s), so closed or crashed tabs and dropped Wi-Fi stop counting. With nobody streaming
  for the grace period (`MAGIC_IDLE_GRACE`, 600 s) the node stops itself; anyone streaming before
  then cancels it. Dead-man rule: no streams and no launcher contact for `MAGIC_DEADMAN` (900 s)
  stops it too. It uses the instance-scoped `CONTAINER_API_KEY` Vast puts in every container.
  **Stop GPU now** in the app page stops that node (refused while others stream, unless confirmed).
* **Launcher** (`deploy/launcher/`), an always-on page on Google Cloud Run (scales to zero, costs
  next to nothing). Sign in, press **Start camera**, and it sends you to the ready node with the
  most free capacity, at that node's current HTTPS address (Cloudflare quick tunnel, which changes
  on every restart; nodes report it in their heartbeat). If no node has room it starts one and
  shows "Starting GPU…". If your node dies mid-stream, the page comes back to the launcher and is
  moved to another node.
* **Autoscaler** (`deploy/launcher/pool.py`, inside the launcher). Capacity is in cost units: each
  node has a budget (`MAGIC_GPU_BUDGET`, 4.0) and each stream costs, by the node's settings,
  1080p30 + GFPGAN 1.0 · plain 1080p30 0.5 · 60 fps ×2 · InSwapper 512 Live / 512 / Studio ×2 / ×5 /
  ×8 on the swap part (from `tools/loadtest.py` on an RTX PRO 6000; retune with `MAGIC_COST_MODEL`).
  When free units across running and booting nodes drop below one stream (`POOL_NEW_STREAM_COST`),
  the next node starts early: first a stopped pool node (1–2 min, but Vast may have rented its GPU
  to someone else; then it is stopped again and skipped for 30 min), otherwise a new instance from
  the cheapest matching offer (`POOL_GPU_NAMES`, `POOL_COUNTRIES`=IN, reliability ≥ 0.98,
  on-demand, ≤ `POOL_MAX_DPH` $1.60/h) using the prebuilt image. The launcher never stops a node
  that has streams; idle nodes stop themselves. A node that users are on their way to (or that
  `POOL_MIN_WARM` keeps warm) is not stopped by its grace period.

**Limits so a bug can't rent GPUs without end:** `POOL_MAX_NODES` (5) running or starting at once,
`POOL_MAX_INSTANCES` (5) pool instances in total (stopped ones still bill storage, about $0.20/h
each here), `POOL_MAX_DPH` per GPU, `POOL_MIN_CREDIT` ($2) left on the Vast account, at most one
scale-up per 20 s, `POOL_MIN_WARM` default 0. The launcher's **GPUs** panel shows every node, its
load and the current $/h, and has **Stop idle GPUs now**.

Pool nodes are the Vast instances labelled `magicpro-pool:<nonce>`. Each node signs its heartbeat
with `MAGIC_NODE_KEY` = HMAC(`POOL_SECRET`, nonce), so a node only ever holds its own key and the
launcher accepts heartbeats only from labelled instances on your account.

Tests (simulated Vast, no network, no money):

```bash
python deploy/launcher/test_pool.py        # scaling, routing, limits, rented GPU, auth, HTTP flow
python deploy/vast/watchdog/test_watchdog.py
python deploy/vast/watchdog/test_gpucontrol.py
```

### 1. Node watchdog

```bash
bash /workspace/magicpro/deploy/vast/watchdog/install.sh                            # test mode: only logs "would stop"
MAGIC_WATCHDOG_DRY_RUN=0 bash /workspace/magicpro/deploy/vast/watchdog/install.sh   # really stop the GPU
tail -f /var/log/portal/magicpro-watchdog.log
```

Settings live in `/etc/magicpro-watchdog.env` (`supervisorctl restart magicpro-watchdog` after an
edit). To keep a node on while you work over SSH with nobody streaming (benchmarks, updates):
`mkdir -p /tmp/magicpro-gpu && touch /tmp/magicpro-gpu/hold` keeps it on 4 h from the touch;
`rm /tmp/magicpro-gpu/hold` releases it. Without `MAGIC_LAUNCHER_URL` the watchdog runs a single
GPU on its own (no pool). Nodes the pool creates get all of this from the image and their env.

### 2. Prebuilt node image (fast new nodes)

A cold instance needs ~5 min of setup; a node from the image only needs the image pull (cached per
machine). `deploy/vast/image/Dockerfile` builds on `vastai/base-image:cuda-13.2.1-auto` with the app,
its `.venv`, `models/` (incl. `models/batched/*`) and the TensorRT cache `models/.trt_cache`. That
cache fits only the GPU model it was built on, so stage it from an RTX PRO 6000 WS node and keep
`POOL_GPU_NAMES` to that model (another model works but rebuilds its engines for minutes on first
start). The models are licensed for non-commercial use, so keep the image **private**: Artifact
Registry in `magicprov2` (recommended, same project as the launcher), or a private Docker Hub or
GHCR repository.

```bash
scp -r -P <ssh port> root@<node ip>:/workspace/magicpro/models ./models    # stage the models (3.3 GB)
REGION=asia-south1 TAG=v1 bash deploy/vast/image/build.sh                    # Cloud Build -> Artifact Registry
```

Vast pulls it with a read-only login: a service account with `roles/artifactregistry.reader`, its
JSON key base64-encoded, saved as secret `pool-image-login` with the value
`-u _json_key_base64 -p <base64 key> asia-south1-docker.pkg.dev`.

### 3. Launcher (Google Cloud Run)

1. **Vast.ai API key:** cloud.vast.ai → Account → API Keys → create a key (instance read/write and
   offer search, if the form offers permissions). Don't paste it into chat or a terminal.
2. **Secret Manager** (console.cloud.google.com → Security → Secret Manager, project `magicprov2`):
   `vast-api-key` (that key), `launcher-password` (the sign-in password), `pool-secret` (any long
   random string, e.g. from `python -c "import secrets;print(secrets.token_hex(32))"`), and
   `pool-image-login` from step 2 once the image exists.
3. **Deploy** (Cloud Shell or a PC with `gcloud`), from the project folder:

```bash
PROJECT=magicprov2; REGION=asia-south1
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com secretmanager.googleapis.com --project $PROJECT
gcloud iam service-accounts create magicpro-launcher --project $PROJECT
for s in vast-api-key launcher-password pool-secret; do
  gcloud secrets add-iam-policy-binding $s --project $PROJECT \
    --member serviceAccount:magicpro-launcher@$PROJECT.iam.gserviceaccount.com --role roles/secretmanager.secretAccessor
done
gcloud run deploy magicpro-launcher --source deploy/launcher --project $PROJECT --region $REGION \
  --service-account magicpro-launcher@$PROJECT.iam.gserviceaccount.com \
  --allow-unauthenticated --min-instances 0 --max-instances 1 --memory 256Mi \
  --set-env-vars POOL_MAX_NODES=5,POOL_MAX_INSTANCES=5,POOL_MIN_WARM=0 \
  --set-secrets VAST_API_KEY=vast-api-key:latest,LAUNCHER_PASSWORD=launcher-password:latest,POOL_SECRET=pool-secret:latest
URL=$(gcloud run services describe magicpro-launcher --project $PROJECT --region $REGION --format 'value(status.url)')
gcloud run services update magicpro-launcher --project $PROJECT --region $REGION --update-env-vars LAUNCHER_URL=$URL
```

Without `POOL_IMAGE` the pool only restarts the instances you adopted and never creates new ones.
Once the image exists, grant the service account access to `pool-image-login` too and add
`--update-env-vars POOL_IMAGE=$REGION-docker.pkg.dev/$PROJECT/magicpro/node:v1 --update-secrets
POOL_IMAGE_LOGIN=pool-image-login:latest`. Keep `--max-instances 1`: reservations and start times
are kept in memory; the node list itself always comes from Vast and the nodes' heartbeats.
`--allow-unauthenticated` only lets the sign-in page and the signed node heartbeats in; everything
else needs the password (signed cookie, 14 days, 10 wrong tries per 10 min per address). With
`POOL_MIN_WARM` > 0, add a Cloud Scheduler job that POSTs every minute to `/api/pool/tick` with
header `X-Tick-Token` = hex HMAC-SHA256 of `tick` keyed with the pool secret.

4. **Adopt the existing instance** into the pool: sign in to the launcher and run this in the
   browser console:
   `fetch("/api/pool/adopt", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({instance_id: "62324193"})}).then(r => r.json()).then(console.log)`.
   It labels the instance `magicpro-pool:<nonce>` and prints two lines (`MAGIC_LAUNCHER_URL`,
   `MAGIC_NODE_KEY`) for `/etc/magicpro-watchdog.env` on that instance; then run
   `supervisorctl restart magicpro-watchdog`.

How users reach a node: the launcher sends the browser to `<node tunnel>/?token=…&autostart=1`
(the token is the instance's Caddy token from the Vast API); Caddy turns it into a cookie and the
page removes it from the address bar. For fixed addresses instead of quick tunnels, launch nodes
with a named Cloudflare tunnel (`CF_TUNNEL_TOKEN`); that bypasses Caddy, so put Cloudflare Access
in front of it.

Known gap: the face library and settings are per node. A user moved to another node (new node or
reconnect) must pick or upload their face there again until the library is shared (e.g. a bucket).

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
