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

## Desktop app (Windows, subscription)

`desktop/` packages the studio as a Windows program (`MagicPro-Setup-<version>.exe`) for the
customer's own NVIDIA card (built for the RTX 5070). It opens straight into the studio, needs a
subscription key that works on one PC and one e-mail address (sign-in with a 6-digit e-mail code),
locks when the subscription ends,
and installs signed updates from inside the program. Keys, activation and releases are managed on
the website's admin page. The face-swap code is unchanged. Build, release and setup:
[docs/desktop.md](docs/desktop.md).

```powershell
py -3.12 tools\desktop_keys.py      # once: signing keys
.\desktop\build\build.ps1          # dist\MagicPro-Setup-<version>.exe
```

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
* Razorpay hour packages: order created server-side, Checkout in the page, signature checked
  server-side (`/api/pay/verify`), and the `payment.captured` / `order.paid` webhook
  (`/api/pay/webhook`). Hours are credited once per order, whichever arrives first.
* `/admin` for admins (see **Admin** below): overview, users, packages, limits, invite codes,
  payments, GPUs and an audit log
* GPU handoff exactly as `docs/handoff-contract.md`: Start → a ready node from the pool (or "Starting
  GPU…" while one boots) → HS256 token → `{gpu_base}/session?t=…`; usage reports at
  `/api/gpu/usage` charge only the increase of `total_seconds` per `sid`; a reply with `stop: true` also says why in `reason` (`ended`, `blocked`, `replaced`, `daily_limit`, `balance`). `MAGIC_SESSION_SECRET` is
  used as the HMAC key as stored (its UTF-8 bytes), for the token and for the report signature.
* Data in Firestore (`users`, `ledger`, `orders`, `sessions`, `invites`, `packages`, `config`,
  `stats`, `audit`). All rules are in `store.Store`; Firestore only runs its transactions.

The website is always on (Cloud Run scales it to zero between visits and wakes it for the next
one), so anyone can visit, sign up and buy at any time. A GPU runs only while someone streams: Start
wakes one, and it stops itself once nobody has streamed for the idle time set under Limits.

**Payments stay in test mode.** `RAZORPAY_MODE` defaults to `test` and then only accepts
`rzp_test_` keys. Live payments need `RAZORPAY_MODE=live`, a `rzp_live_` key **and**
`RAZORPAY_LIVE_CONFIRM` set to the exact sentence in `deploy/launcher/payments.py`. Don't do that
before you have a commercial licence for the face-swap model (the current one is non-commercial).

#### Admin

Admins are the e-mails in `ADMIN_EMAILS` (owners: they can't be blocked or demoted from the page)
plus anyone an admin marks as admin. Everything an admin changes is written to the audit log.

* **Overview:** people streaming now, GPUs running and their price per hour, GPU cost today and this
  month (the Vast hourly price × the time the website saw each GPU running), hours sold and used
  today and this month, revenue (marked **test** while payments are in test mode). Days are India
  time.
* **Users:** search by name or e-mail; a profile shows sign-up date, balance, hours used in total
  and today, expiring hours, payments, sessions and the admin actions on that user. Give or take
  hours (a note is required), block or unblock (a blocked user can't start, and a live session
  ends at its next usage report, within about 30 s), make or remove admin, per-user limits, delete
  all data.
* **Packages:** name, hours, price in ₹, "popular" badge, optional "use within N days" (those hours
  expire N days after purchase; expiring hours are spent first), on sale or not, order. The home
  page and the Buy screen show the packages on sale. An order keeps its own copy of hours, price
  and validity, so editing a package never changes orders already made or paid. On the first start
  the packages come from `PACKS` (defaults ₹499 / ₹799 / ₹1,499 for 3 / 5 / 10 h, placeholders).
* **GPUs:** every pool GPU with its status (running, starting, stopping, stopped, unavailable),
  people streaming now, time up in this run and its cost so far (Vast hourly price × uptime), and
  **Turn off now** / **Turn on now**, plus **Turn all GPUs off**. Turning off goes through the Vast
  API, so it works whatever state the GPU's own software is in.
  * With people streaming, the page first says how many will be cut off and asks again. Their
    sessions end at once: their next usage report gets `stop: true` with `reason: "ended"`, nothing
    is charged after the turn-off, and their page's automatic reconnect does not switch a GPU back on
    (pressing **Start the studio** does).
  * A manual off is not a block. The next customer who presses Start, or an admin pressing Turn on,
    starts that GPU again through the pool as usual, and it turns itself off again after the idle
    time (5 minutes after the last stream ends). A GPU turned off by hand is never marked as broken.
  * **Keep GPUs off until I untick this** (maintenance, off by default): Start shows "Service is
    paused, try again later" and the pool starts nothing by itself; GPUs that are on stay on until
    you turn them off; Turn on still works for admins.
  * Every manual on/off goes into the audit log with who, when and how many people were cut off.
* **Limits** (everyone; the first three can be overridden per user):

  | Limit | Default | Enforced |
  |---|---|---|
  | Longest session | 4 h | token `exp` = the smallest of balance, this, and what's left today |
  | Hours per user per day | 0 (none) | Start refuses; usage report answers `stop: true` when reached |
  | One live session per user | on | a new Start ends the old `sid` (`stop: true` at its next report) |
  | Free trial for new accounts | 0 min | credited once at sign-up |
  | Anyone can create an account | on | off: only existing users, owners and people arriving with a valid invite link |
  | Most GPUs running at once | 5 | the pool cap (replaces `POOL_MAX_NODES` once saved) |
  | GPU stops after nobody streams for | 5 min | sent to every node in the heartbeat reply (the dead-man stays 15 min) |

Offline tests (fake Google, fake Razorpay, simulated Vast):

```bash
python deploy/launcher/test_admin.py   # blocked user, daily cap, session cap, single session, signups closed, packages vs paid orders, validity, roles, overview, pool limits, GPU off/on with and without people streaming, auto start after a manual off, auto stop after use, maintenance
python deploy/launcher/test_web.py && python deploy/launcher/test_pool.py
```

#### One-time setup in project magicprov2 (secrets are never typed by anyone but you)

Project: **magicprov2** (billing on; Cloud Run and Artifact Registry APIs already enabled). Every
command passes `--project magicprov2`, so the local `gcloud` default project doesn't matter and
isn't changed. Region: **asia-southeast1** (Singapore), because Cloud Run custom domains (domain
mapping) are not offered in asia-south1 (Mumbai); the video stream never passes through the
website, so the distance doesn't matter. `deploy/launcher/deploy-magicprov2.sh` runs the gcloud
steps one at a time; `bash deploy/launcher/deploy-magicprov2.sh check` only reads.

| Step | Who | What it changes | Expected cost |
|---|---|---|---|
| 1. `deploy-magicprov2.sh apis` | gcloud | enables Cloud Build, Secret Manager, Firestore APIs | free |
| 2. `deploy-magicprov2.sh firestore` | gcloud | Firestore database (Native, asia-southeast1) | free tier (1 GiB, 50k reads / 20k writes a day); beyond that cents |
| 3. OAuth consent screen + client | you, console | Google Auth Platform branding, client id/secret | free |
| 4. Razorpay test keys + webhook | you, Razorpay | test mode only | free |
| 5. Six secrets in Secret Manager | you, console | `magicpro-session-secret`, `google-client-secret`, `razorpay-key-secret`, `razorpay-webhook-secret`, `vast-api-key`, `pool-secret` | about US$0.36/month (US$0.06 per secret version a month) |
| 6. `deploy-magicprov2.sh account` and `secrets` | gcloud | service account `magicpro-web`, Firestore user role, read access to the 6 secrets | free |
| 7. `deploy-magicprov2.sh deploy` | gcloud | Cloud Build builds the image into Artifact Registry, Cloud Run service `magicpro-web` (min 0, max 1 instance, public) | build: within the free build minutes; image storage ~0.2 GB within the 0.5 GB free; Cloud Run: within the free tier at this traffic (requests while GPUs run are short heartbeats) |
| 8. `deploy-magicprov2.sh domain` + GoDaddy records | gcloud + you | domain mapping for magicpro.nacgenai.in (preview feature), TXT and CNAME at GoDaddy | free |

Details for the steps you do yourself:

* **OAuth consent screen:** console → Google Auth Platform (project magicprov2) → Branding: app
  name "Magic Pro", support e-mail, authorised domain `nacgenai.in`, links to
  `https://magicpro.nacgenai.in/privacy` and `/terms`. Audience: External. Data access: `openid`,
  `email`, `profile` only (no Google review needed). Publish the app (while "Testing", only listed
  test users can sign in).
* **OAuth client:** Google Auth Platform → Clients → Create → Web application. Authorised redirect
  URIs: `https://magicpro.nacgenai.in/auth/callback`, and the `https://…run.app/auth/callback`
  address step 7 prints, for testing before the domain works. The client id goes in
  `GOOGLE_CLIENT_ID` below; the secret goes to Secret Manager.
* **Razorpay:** dashboard in **Test mode** → Account & Settings → API keys → Generate test key.
  Webhooks → Add: URL `https://magicpro.nacgenai.in/api/pay/webhook`, events `payment.captured` and
  `order.paid`, a webhook secret of your choice.
* **Secret Manager** (console → Security → Secret Manager, project magicprov2): create the six
  secrets above. `magicpro-session-secret` and `pool-secret` are long random strings, e.g.
  `python -c "import secrets;print(secrets.token_urlsafe(48))"`; the session secret is the same value
  the GPU nodes get as `MAGIC_SESSION_SECRET`. `vast-api-key` is a Vast.ai API key from
  cloud.vast.ai → Account → API Keys.

Then, from the repository root in Cloud Shell:

```bash
bash deploy/launcher/deploy-magicprov2.sh check
bash deploy/launcher/deploy-magicprov2.sh apis
bash deploy/launcher/deploy-magicprov2.sh firestore
bash deploy/launcher/deploy-magicprov2.sh account
bash deploy/launcher/deploy-magicprov2.sh secrets          # after the six secrets exist
GOOGLE_CLIENT_ID=<client id> RAZORPAY_KEY_ID=<rzp_test_…> ADMIN_EMAILS=<your e-mail> \
  bash deploy/launcher/deploy-magicprov2.sh deploy
```

`--allow-unauthenticated` is needed for a public website; the app does its own sign-in. The service
keeps `--max-instances 1` (pool timing is kept in memory). If the first `deploy` stops with a Cloud
Build permission error, the project's default compute service account needs the
`roles/run.builder` role (`gcloud projects add-iam-policy-binding magicprov2 --member
serviceAccount:<project number>-compute@developer.gserviceaccount.com --role roles/run.builder`).
The GPU pool cap and idle time are set on the admin page (Limits); add `POOL_IMAGE` /
`POOL_IMAGE_LOGIN` once the node image exists (section 2).

**Custom domain** `magicpro.nacgenai.in` (step 8):
   * Verify `nacgenai.in` as yours: run `gcloud domains verify nacgenai.in` while signed in with the Google account that owns magicprov2 (opens Search Console);
     it shows a TXT value. In **GoDaddy → nacgenai.in → DNS → Add record**: Type `TXT`, Name `@`,
     Value `google-site-verification=…` (exactly as shown), TTL 1 hour. Click Verify.
   * `bash deploy/launcher/deploy-magicprov2.sh domain` (creates the mapping and prints the record)
   * In GoDaddy add: Type `CNAME`, Name `magicpro`, Value `ghs.googlehosted.com.`, TTL 1 hour.
     (`gcloud beta run domain-mappings describe --domain magicpro.nacgenai.in --region asia-southeast1 --project magicprov2`
     prints the same record.) The HTTPS certificate follows within about 15–60 minutes.
   * Google marks domain mapping as a preview feature with some added latency. For production, the
     recommended upgrade is a global external Application Load Balancer with a serverless NEG
     (about US$18/month); then GoDaddy gets an `A` record `magicpro` → the load balancer's IP instead
     of the CNAME. Firebase Hosting doesn't suit this site: it forwards only a cookie named
     `__session`, so sign-in would break.

**Adopt the existing GPU instance** into the pool: sign in as an admin and run in the browser
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
