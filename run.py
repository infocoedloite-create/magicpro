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
    uvicorn.run("app.server:app", host=config.HOST, port=config.PORT, log_level="warning")
