# Prebuilt Magic Pro GPU node: the app, its .venv, the models (incl. models/batched) and, for one
# GPU model only, the TensorRT engine cache. A pool node from this image is ready in about the
# time Vast takes to pull it (cached per machine after the first pull) instead of ~5 min setup.
# Build context = the repository root with models/ staged in it (see build.sh next to this file).
FROM vastai/base-image:cuda-13.2.1-auto

ENV MAGIC_APP_DIR=/opt/magicpro
# /workspace may be a mounted volume on Vast, so the app lives in /opt
COPY . /opt/magicpro
WORKDIR /opt/magicpro
RUN test -f models/det_10g.onnx && test -d models/batched \
 && MAGIC_IMAGE_BUILD=1 bash deploy/vast/install.sh \
 && MAGIC_IMAGE_BUILD=1 bash deploy/vast/watchdog/install.sh \
 && rm -rf /root/.cache/pip /root/.cache/uv
# Ports and the portal entry for the app are set by the pool when it creates the instance
# (PORTAL_CONFIG "...|localhost:10100:17860:/:Magic Pro", -p 10100:10100); see deploy/launcher/pool.py.
