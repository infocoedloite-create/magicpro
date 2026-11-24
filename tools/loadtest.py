"""Multi-stream load test for the H.264 endpoint.

Simulates N users at once: each streams a pre-encoded 1080p H.264 clip containing faces at the
target fps and reports delivered fps and end-to-end delay per stream.

    python tools/loadtest.py --base http://127.0.0.1:17861 --image face.jpg --streams 1 5 10 --fps 30

The server must have a source face selected (see --setup). Run it on the GPU box against a test
server so the clients' network is not part of the measurement.
"""
import argparse
import asyncio
import json
import multiprocessing as mp
import statistics
import struct
import subprocess
import time
import urllib.request
from fractions import Fraction

H = struct.Struct("<BBI")


def make_clip(image, w, h, fps, seconds=3, bitrate=8_000_000):
    import av
    import cv2
    src = cv2.imread(image)
    big = cv2.resize(src, (int(w * 1.15), int(h * 1.15)))
    enc = av.CodecContext.create("libx264", "w")
    enc.width, enc.height, enc.pix_fmt = w, h, "yuv420p"
    enc.time_base, enc.framerate = Fraction(1, 1000), Fraction(fps)
    enc.bit_rate, enc.gop_size, enc.max_b_frames = bitrate, fps * seconds, 0
    enc.options = {"preset": "veryfast", "tune": "zerolatency"}
    packets = []
    n = fps * seconds
    for i in range(n):                       # slow pan so every frame differs
        dx = int((big.shape[1] - w) * (0.5 + 0.5 * __import__("math").sin(i / n * 6.283)))
        dy = int((big.shape[0] - h) * 0.5)
        f = av.VideoFrame.from_ndarray(cv2.cvtColor(big[dy:dy + h, dx:dx + w], cv2.COLOR_BGR2YUV_I420), format="yuv420p")
        f.pts = i * 1000 // fps
        packets += [(bool(p.is_keyframe), bytes(p)) for p in enc.encode(f)]
    packets += [(bool(p.is_keyframe), bytes(p)) for p in enc.encode(None)]
    return packets


def client(idx, base, packets, fps, seconds, warmup, q):
    import websockets

    async def run():
        url = base.replace("http", "ws") + "/ws/h264"
        async with websockets.connect(url, max_size=None) as ws:
            sent, rtts, got = {}, [], []
            start = time.perf_counter()

            async def sender():
                i, nxt = 0, time.perf_counter()
                while time.perf_counter() - start < seconds:
                    key, data = packets[i % len(packets)]
                    i += 1
                    sent[i] = time.perf_counter()
                    await ws.send(H.pack(1, 1 if key else 0, i) + data)
                    nxt += 1 / fps
                    await asyncio.sleep(max(0.0, nxt - time.perf_counter()))

            async def receiver():
                while time.perf_counter() - start < seconds + 1.5:
                    try:
                        msg = await asyncio.wait_for(ws.recv(), 3)
                    except asyncio.TimeoutError:
                        return
                    if isinstance(msg, str):
                        continue
                    _, _, i = H.unpack_from(msg)
                    now = time.perf_counter()
                    if now - start > warmup and i in sent:
                        got.append(now)
                        rtts.append((now - sent[i]) * 1000)

            await asyncio.gather(sender(), receiver())
            span = (got[-1] - got[0]) if len(got) > 1 else 1
            q.put({"idx": idx, "fps": (len(got) - 1) / span if len(got) > 1 else 0.0, "rtts": rtts})

    asyncio.run(run())


def api(base, path, data=None, method=None):
    req = urllib.request.Request(base + path, json.dumps(data).encode() if data is not None else None,
                                 {"Content-Type": "application/json"}, method=method)
    return json.load(urllib.request.urlopen(req))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:17861")
    ap.add_argument("--image", required=True)
    ap.add_argument("--streams", type=int, nargs="+", default=[1, 5, 10])
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--res", default="1080p")
    ap.add_argument("--enhancer", default="gfpgan_1.4")
    ap.add_argument("--swapper", default="inswapper_128_fp16")
    ap.add_argument("--seconds", type=int, default=20)
    ap.add_argument("--setup", action="store_true", help="upload --image as the source face and select it")
    a = ap.parse_args()

    if a.setup:
        up = json.loads(subprocess.run(["curl", "-s", "-F", f"file=@{a.image}", "-F", "name=loadtest", "-F", "consent=true",
                                        a.base + "/api/library"], capture_output=True, text=True).stdout)
        api(a.base, "/api/settings", {"source_ids": [up["id"]]})
    api(a.base, "/api/settings", {"enhancer": a.enhancer, "resolution": a.res, "fps": a.fps,
                                  "swapper": a.swapper, "swap_enabled": True})
    w, h = (1920, 1080) if a.res == "1080p" else (1280, 720)
    packets = make_clip(a.image, w, h, a.fps)
    print(f"clip {w}x{h}@{a.fps}: {len(packets)} packets, {sum(len(p) for _, p in packets) * 8 / 3 / 1e6:.1f} Mbit/s; "
          f"enhancer={a.enhancer or 'off'} swapper={a.swapper}")

    # warm-up: every worker process loads its models before anything is measured
    q = mp.Queue()
    warm = [mp.Process(target=client, args=(i, a.base, packets, a.fps, 8, 1, q)) for i in range(max(a.streams))]
    for p in warm:
        p.start()
    for _ in warm:
        q.get(timeout=300)
    for p in warm:
        p.join()

    for n in a.streams:
        q = mp.Queue()
        procs = [mp.Process(target=client, args=(i, a.base, packets, a.fps, a.seconds, 5, q)) for i in range(n)]
        for p in procs:
            p.start()
        res = [q.get(timeout=a.seconds + 60) for _ in procs]
        for p in procs:
            p.join()
        fps = [r["fps"] for r in res]
        rtt = sorted(x for r in res for x in r["rtts"])
        p50 = rtt[len(rtt) // 2] if rtt else 0
        p95 = rtt[int(len(rtt) * 0.95)] if rtt else 0
        stats = api(a.base, "/api/stats")
        print(f"{n:3d} streams: fps per stream min {min(fps):5.1f} / median {statistics.median(fps):5.1f} of {a.fps}"
              f" | delay p50 {p50:4.0f} ms p95 {p95:4.0f} ms | server: detect {stats['detect_ms']} swap {stats['swap_ms']}"
              f" enhance {stats['enhance_ms']} ms | mean batch {stats.get('batch', {})}", flush=True)


if __name__ == "__main__":
    main()
