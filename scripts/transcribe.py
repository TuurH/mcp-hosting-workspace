from __future__ import annotations

import argparse
import json
from pathlib import Path

from faster_whisper import WhisperModel


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Transcribe audio/video using faster-whisper")
    parser.add_argument("input", type=Path)
    parser.add_argument("--model", default="large-v3")
    parser.add_argument("--language", default=None)
    parser.add_argument("--device", default="cuda")
    parser.add_argument("--compute-type", default="float16")
    parser.add_argument("--beam-size", type=int, default=5)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--json-output", type=Path, default=None)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    source = args.input.resolve()
    if not source.is_file():
        raise FileNotFoundError(source)

    output = args.output or source.with_suffix(source.suffix + ".txt")
    json_output = args.json_output or source.with_suffix(source.suffix + ".json")
    output.parent.mkdir(parents=True, exist_ok=True)
    json_output.parent.mkdir(parents=True, exist_ok=True)

    model = WhisperModel(
        args.model,
        device=args.device,
        compute_type=args.compute_type,
        download_root="/workspace/models/whisper",
    )
    segments_iterator, info = model.transcribe(
        str(source),
        language=args.language,
        beam_size=args.beam_size,
        vad_filter=True,
    )

    segments = []
    for segment in segments_iterator:
        text = segment.text.strip()
        segments.append(
            {
                "start": segment.start,
                "end": segment.end,
                "text": text,
            }
        )
        print(f"[{segment.start:8.2f} -> {segment.end:8.2f}] {text}", flush=True)

    transcript = " ".join(item["text"] for item in segments).strip()
    output.write_text(transcript + "\n", encoding="utf-8")
    json_output.write_text(
        json.dumps(
            {
                "input": str(source),
                "model": args.model,
                "language": info.language,
                "language_probability": info.language_probability,
                "duration": info.duration,
                "segments": segments,
                "text": transcript,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"Wrote {output}")
    print(f"Wrote {json_output}")


if __name__ == "__main__":
    main()
