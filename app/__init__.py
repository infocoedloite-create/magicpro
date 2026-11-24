import os

import cv2

# Many streams call OpenCV from their own threads; per-call thread pools of every core
# oversubscribe the CPU. A few threads per call is faster overall.
cv2.setNumThreads(int(os.environ.get("MAGIC_CV_THREADS", "4")))
