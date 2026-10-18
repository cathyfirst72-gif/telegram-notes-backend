#!/usr/bin/env python3
"""Transcribe one Telegram voice file with an already downloaded local Whisper model."""

import json
import sys

from faster_whisper import WhisperModel


def main() -> None:
    audio_path, model_dir = sys.argv[1:3]
    model = WhisperModel(
        model_dir,
        device="cpu",
        compute_type="int8",
        local_files_only=True,
    )
    segments, _ = model.transcribe(audio_path, beam_size=5, vad_filter=True)
    text = " ".join(segment.text.strip() for segment in segments).strip()
    print(json.dumps({"text": text}, ensure_ascii=False))


if __name__ == "__main__":
    main()
