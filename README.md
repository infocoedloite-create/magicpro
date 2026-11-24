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
