import os
import threading
import webbrowser

import uvicorn

from app import config

if __name__ == "__main__":
    url = f"http://{config.HOST}:{config.PORT}"
    print(f"Magic Pro Live Face Swap -> {url}", flush=True)
    if os.environ.get("MAGIC_OPEN_BROWSER", "1") == "1":
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    # MAGIC_WORKERS > 1 runs several processes sharing the GPU (for many simultaneous browser
    # streams); the server-camera mode needs 1.
    workers = int(os.environ.get("MAGIC_WORKERS", "1"))
    if os.environ.get("MAGIC_SESSION_SECRET") and workers > 1:
        # usage is counted per process; several processes would each report their own total
        print("Hosted mode runs one worker process (streams already run in parallel threads).")
        workers = 1
    uvicorn.run("app.server:app", host=config.HOST, port=config.PORT, log_level="warning", workers=workers)
