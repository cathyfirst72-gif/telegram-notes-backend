#!/usr/bin/env bash
set -euo pipefail

skill_dir="$(cd "$(dirname "$0")/.." && pwd)"
uv="$skill_dir/.tools/uv"
model_dir="$skill_dir/.models/base"

if [[ ! -x "$uv" ]]; then
  installer="$(mktemp)"
  trap 'rm -f "$installer"' EXIT
  curl -fsSL https://astral.sh/uv/install.sh -o "$installer"
  UV_INSTALL_DIR="$skill_dir/.tools" UV_NO_MODIFY_PATH=1 sh "$installer"
fi

export UV_PYTHON_INSTALL_DIR="$skill_dir/.python"
export UV_CACHE_DIR="$skill_dir/.cache"
if [[ ! -x "$skill_dir/.venv/bin/python" ]]; then
  "$uv" venv --python 3.11 "$skill_dir/.venv"
fi
if [[ ! -d "$skill_dir/.venv/lib/python3.11/site-packages/faster_whisper" ]]; then
  "$uv" pip install --python "$skill_dir/.venv/bin/python" faster-whisper
fi

mkdir -p "$model_dir"
for file in config.json model.bin tokenizer.json vocabulary.txt; do
  if [[ ! -s "$model_dir/$file" ]]; then
    curl -fsSL "https://huggingface.co/Systran/faster-whisper-base/resolve/main/$file" -o "$model_dir/$file.tmp"
    mv "$model_dir/$file.tmp" "$model_dir/$file"
  fi
done

"$skill_dir/.venv/bin/python" -c 'from faster_whisper import WhisperModel; import sys; WhisperModel(sys.argv[1], device="cpu", compute_type="int8", local_files_only=True)' "$model_dir"
echo 'Локальный Whisper готов'
