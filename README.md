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
  for the grace period (`MAGIC_IDLE_GRACE`, 300 s = 5 min) the node stops itself; anyone streaming before
  then cancels it. Dead-man rule: no streams and no launcher contact for `MAGIC_DEADMAN` (900 s)
  stops it too. It uses the instance-scoped `CONTAINER_API_KEY` Vast puts in every container.
  **Stop GPU now** in the app page stops that node (refused while others stream, unless confirmed).
* **Website** (`deploy/launcher/`, section 3), always on at Google Cloud Run (scales to zero).
  Users sign in with Google, buy hours and press **Start the studio**; it sends them to the ready
  node with the most free capacity, at that node's current HTTPS address (Cloudflare quick tunnel,
  which changes on every restart; nodes report it in their heartbeat), with a session token. If no
  node has room it starts one and shows "Starting GPU…". If a node dies mid-stream, the page comes
  back to the website and is moved to another node.
* **Autoscaler** (`deploy/launcher/pool.py`, inside the website service). Capacity is in cost units: each
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
scale-up per 20 s, `POOL_MIN_WARM` default 0. The admin page's **GPUs** panel shows every node, its
load and the current $/h, and has **Stop idle GPUs now**.

Pool nodes are the Vast instances labelled `magicpro-pool:<nonce>`. Each node signs its heartbeat
with `MAGIC_NODE_KEY` = HMAC(`POOL_SECRET`, nonce), so a node only ever holds its own key and the
launcher accepts heartbeats only from labelled instances on your account.

Tests (simulated Vast, no network, no money):

```bash
python deploy/launcher/test_pool.py        # scaling, routing, limits, rented GPU, node auth
python deploy/launcher/test_web.py         # sign-in, payments, webhook, invites, admin, token, usage
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

### 3. Website (magicpro.nacgenai.in on Google Cloud Run)

`deploy/launcher/` is the public website and the pool's control service in one Cloud Run service:

* `/` marketing page, `/terms`, `/privacy`, `/acceptable-use` (**drafts for a lawyer**, marked on the page)
* Google sign-in (`/login`, `/auth/callback`), account page `/app`: balance, hour packs, Start, invite
  code, history
* Razorpay hour packs: order created server-side, Checkout in the page, signature checked
  server-side (`/api/pay/verify`), and the `payment.captured` / `order.paid` webhook
  (`/api/pay/webhook`). Hours are credited once per order, whichever arrives first.
* `/admin` for e-mails in `ADMIN_EMAILS`: users and balances, give or take hours, pause or delete
  a user, invite codes worth N hours that must be redeemed within N hours (`/invite/<code>` links),
  withdraw codes, payments, GPUs
* GPU handoff exactly as `docs/handoff-contract.md`: Start → a ready node from the pool (or "Starting
  GPU…" while one boots) → HS256 token → `{gpu_base}/session?t=…`; usage reports at
  `/api/gpu/usage` charge only the increase of `total_seconds` per `sid`. `MAGIC_SESSION_SECRET` is
  used as the HMAC key as stored (its UTF-8 bytes), for the token and for the report signature.
* Data in Firestore (`users`, `ledger`, `orders`, `sessions`, `invites`).

**Payments stay in test mode.** `RAZORPAY_MODE` defaults to `test` and then only accepts
`rzp_test_` keys. Live payments need `RAZORPAY_MODE=live`, a `rzp_live_` key **and**
`RAZORPAY_LIVE_CONFIRM` set to the exact sentence in `deploy/launcher/payments.py`. Don't do that
before you have a commercial licence for the face-swap model (the current one is non-commercial).
Pack sizes and prices: `PACKS='[{"id":"h3","hours":3,"price_inr":499},…]'`; the defaults (₹499 /
₹799 / ₹1,499 for 3 / 5 / 10 h) are placeholders.

Offline tests (fake Google, fake Razorpay, simulated Vast):

```bash
python deploy/launcher/test_web.py && python deploy/launcher/test_pool.py
```

#### One-time setup (you do these; secrets are never typed by anyone else)

Decide the Google Cloud project first (`magicprov2`, or `magic-515508` that the local `gcloud` uses);
below it is `$PROJECT`. Region: **asia-southeast1** (Singapore), because Cloud Run custom domains
(domain mapping) are not offered in asia-south1 (Mumbai); the video stream never passes through the
website, so the distance doesn't matter.

1. **Firestore:** console → Firestore → Create database → Native mode, location `asia-southeast1`.
2. **OAuth consent screen:** console → Google Auth Platform → Branding: app name "Magic Pro",
   support e-mail, authorised domain `nacgenai.in`, links to `https://magicpro.nacgenai.in/privacy` and
   `/terms`. Audience: External. Data access: scopes `openid`, `email`, `profile` only (no review needed).
   Publish the app (while "Testing", only listed test users can sign in).
3. **OAuth client:** Google Auth Platform → Clients → Create → Web application. Authorised redirect
   URI: `https://magicpro.nacgenai.in/auth/callback` (add the `…run.app/auth/callback` URL too for
   testing before the domain works). Keep the client id; the secret goes to Secret Manager.
4. **Razorpay test keys:** dashboard in **Test mode** → Account & Settings → API keys → Generate
   test key. Webhooks → Add: URL `https://magicpro.nacgenai.in/api/pay/webhook`, events
   `payment.captured` and `order.paid`, a webhook secret of your choice.
5. **Secret Manager** (Security → Secret Manager), one secret each:
   `magicpro-session-secret` (≥ 32 random bytes, e.g. `python -c "import secrets;print(secrets.token_urlsafe(48))"`;
   the same value goes to the GPU nodes), `google-client-secret`, `razorpay-key-secret`,
   `razorpay-webhook-secret`, `vast-api-key`, `pool-secret` (another random string).
6. **Deploy** (Cloud Shell, from the project folder):

```bash
PROJECT=<your project>; REGION=asia-southeast1; SA=magicpro-web@$PROJECT.iam.gserviceaccount.com
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com secretmanager.googleapis.com firestore.googleapis.com --project $PROJECT
gcloud iam service-accounts create magicpro-web --project $PROJECT
gcloud projects add-iam-policy-binding $PROJECT --member serviceAccount:$SA --role roles/datastore.user
for s in magicpro-session-secret google-client-secret razorpay-key-secret razorpay-webhook-secret vast-api-key pool-secret; do
  gcloud secrets add-iam-policy-binding $s --project $PROJECT --member serviceAccount:$SA --role roles/secretmanager.secretAccessor
done
gcloud run deploy magicpro-web --source deploy/launcher --project $PROJECT --region $REGION --service-account $SA \
  --allow-unauthenticated --min-instances 0 --max-instances 1 --memory 512Mi \
  --set-env-vars "^@^SITE_URL=https://magicpro.nacgenai.in@STORE=firestore@FIRESTORE_PROJECT=$PROJECT@GOOGLE_CLIENT_ID=<client id>@RAZORPAY_KEY_ID=<rzp_test_… key id>@RAZORPAY_MODE=test@ADMIN_EMAILS=<your e-mail>@POOL_MAX_NODES=2@POOL_MAX_INSTANCES=2@POOL_MIN_WARM=0" \
  --set-secrets MAGIC_SESSION_SECRET=magicpro-session-secret:latest,GOOGLE_CLIENT_SECRET=google-client-secret:latest,RAZORPAY_KEY_SECRET=razorpay-key-secret:latest,RAZORPAY_WEBHOOK_SECRET=razorpay-webhook-secret:latest,VAST_API_KEY=vast-api-key:latest,POOL_SECRET=pool-secret:latest
```

   `--allow-unauthenticated` is needed for a public website; the app does its own sign-in. Keep
   `--max-instances 1` (pool timing is kept in memory). Add `POOL_IMAGE` / `POOL_IMAGE_LOGIN` once
   the node image exists (section 2).

7. **Custom domain** `magicpro.nacgenai.in`:
   * Verify `nacgenai.in` as yours: run `gcloud domains verify nacgenai.in` (opens Search Console);
     it shows a TXT value. In **GoDaddy → nacgenai.in → DNS → Add record**: Type `TXT`, Name `@`,
     Value `google-site-verification=…` (exactly as shown), TTL 1 hour. Click Verify.
   * `gcloud beta run domain-mappings create --service magicpro-web --domain magicpro.nacgenai.in --region asia-southeast1 --project $PROJECT`
   * In GoDaddy add: Type `CNAME`, Name `magicpro`, Value `ghs.googlehosted.com.`, TTL 1 hour.
     (`gcloud beta run domain-mappings describe --domain magicpro.nacgenai.in --region asia-southeast1`
     prints the same record.) The HTTPS certificate follows within about 15–60 minutes.
   * Google marks domain mapping as a preview feature with some added latency. For production, the
     recommended upgrade is a global external Application Load Balancer with a serverless NEG
     (about US$18/month); then GoDaddy gets an `A` record `magicpro` → the load balancer's IP instead
     of the CNAME. Firebase Hosting doesn't suit this site: it forwards only a cookie named
     `__session`, so sign-in would break.

8. **Adopt the existing GPU instance** into the pool: sign in as an admin and run in the browser
   console on `/admin`:
   `fetch("/api/pool/adopt", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({instance_id: "62324193"})}).then(r => r.json()).then(console.log)`.
   It labels the instance `magicpro-pool:<nonce>` and prints `MAGIC_LAUNCHER_URL` and
   `MAGIC_NODE_KEY` for `/etc/magicpro-watchdog.env`; add `MAGIC_TUNNEL_DIRECT=1` and
   `MAGIC_SESSION_SECRET=<the same secret>` there too, then `supervisorctl restart magicpro-watchdog`.

How users reach a node: the node's public quick tunnel goes straight to the app port
(`127.0.0.1:17860`), and the app checks the session token itself. The watchdog opens that tunnel
only when the app answers 401 without a session, so an app build without session checks is never
exposed. The watchdog reads the app's `/api/gpu/status` with the header
`X-Magic-Watchdog: hex(hmac_sha256(MAGIC_SESSION_SECRET, "watchdog"))`, which the node app must accept.

Known gap: each user's face library lives on the node they used. A user sent to another node must
upload their face there again until the library is shared (e.g. a storage bucket).

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
