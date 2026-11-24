import threading
import webbrowser

import uvicorn

from app import config

if __name__ == "__main__":
    url = f"http://{config.HOST}:{config.PORT}"
    print(f"Magic Pro Live Face Swap -> {url}")
    threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    uvicorn.run("app.server:app", host=config.HOST, port=config.PORT, log_level="warning")
