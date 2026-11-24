#!/usr/bin/env bash
# Linux / cloud GPU setup (tested target: Vast.ai base image, Ubuntu 24.04, RTX PRO 6000).
set -euo pipefail
cd "$(dirname "$0")"
if command -v uv >/dev/null; then
  [ -d .venv ] || uv venv --python 3.12 .venv
  uv pip install --python .venv/bin/python -r requirements.txt
else
  [ -d .venv ] || python3 -m venv .venv
  .venv/bin/python -m pip install --upgrade pip
  .venv/bin/python -m pip install -r requirements.txt
fi
.venv/bin/python download_models.py
# batch-capable copies of the swap models (pixel boost and several faces in one GPU call)
mkdir -p models/batched
for m in models/swappers/inswapper_128*.onnx; do
  [ -f "models/batched/$(basename "$m")" ] || .venv/bin/python tools/make_batchable.py "$m" "models/batched/$(basename "$m")"
done
echo "Setup finished. Start with: MAGIC_OPEN_BROWSER=0 .venv/bin/python run.py"
