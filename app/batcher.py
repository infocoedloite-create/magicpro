"""Cross-stream GPU batching.

Many streams call the same model from their own threads. One dispatcher thread per model runs
the queued requests as a single GPU call. With several streams active it waits up to
MAGIC_BATCH_WINDOW_MS (default 0) for the other streams' requests; with one stream it never
waits, so a lone user pays no extra delay.
"""
import os
import queue
import threading
import time
from concurrent.futures import Future

# Waiting for other streams was measured to cost more than it gains (each stream processes its
# frames one after another), so by default nothing waits; requests that are already queued still
# share a GPU call.
WINDOW_S = float(os.environ.get("MAGIC_BATCH_WINDOW_MS", "0")) / 1000.0
active_streams = lambda: 1  # noqa: E731  replaced by the engine with its live stream count
ALL = []                    # every Batcher in this process, for stats


def mean_batch_sizes():
    return {b.name: round(sum(b.sizes) / len(b.sizes), 2) for b in ALL if b.sizes}


class Batcher:
    def __init__(self, run_batch, max_items=16, name="batcher", expected=None):
        """run_batch(list of items) -> list of results (same order).
        expected() -> how many requests a full round would have (active streams)."""
        self.run_batch = run_batch
        self.max_items = max_items
        self.name = name
        self.expected = expected or (lambda: active_streams())
        self.q = queue.Queue()
        self.sizes = []                      # recent batch sizes, for diagnostics
        ALL.append(self)
        threading.Thread(target=self._loop, name=name, daemon=True).start()

    def __call__(self, item):
        fut = Future()
        self.q.put((item, fut))
        return fut.result()

    def _loop(self):
        while True:
            batch = [self.q.get()]
            want = min(self.max_items, max(1, self.expected()))
            deadline = time.perf_counter() + (WINDOW_S if want > 1 else 0.0)
            while len(batch) < want:
                left = deadline - time.perf_counter()
                try:
                    batch.append(self.q.get(timeout=left) if left > 0 else self.q.get_nowait())
                except queue.Empty:
                    break
            while len(batch) < self.max_items:       # anything else already waiting rides along
                try:
                    batch.append(self.q.get_nowait())
                except queue.Empty:
                    break
            self.sizes = (self.sizes + [len(batch)])[-200:]
            try:
                results = self.run_batch([item for item, _ in batch])
                for (_, fut), r in zip(batch, results):
                    fut.set_result(r)
            except Exception as e:  # noqa: BLE001  hand the error to every waiting stream
                for _, fut in batch:
                    if not fut.done():
                        fut.set_exception(e)
