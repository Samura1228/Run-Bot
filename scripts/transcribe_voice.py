"""Transcribe the coach's voice messages from a Telegram Desktop export.

LOCAL, ONE-OFF TOOL — not part of the bot. It is never imported by
``bot.*`` and its dependency (faster-whisper) is deliberately absent from
``requirements.txt``: transcription runs on a laptop, not on Railway, where
the model would be far too heavy.

Why the HTML export and not JSON: the HTML carries everything we need —
``id="message12345"`` (the message id the bot later replies to), the exact
timestamp, the sender and the duration — so there is no need to re-export.

Usage
-----
    .venv/bin/python scripts/transcribe_voice.py --export "<export dir>" \
        --sender Aliaksandra

The result is written to a local JSON file, NOT to the sheet: the point is to
read it and judge the transcription quality before anything is published. The
run is resumable — an existing output file is loaded and already-transcribed
messages are skipped, so a long run can be interrupted safely.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Optional

# Any message block, INCLUDING ``joined`` (Telegram marks consecutive messages
# from the same sender that way, and those blocks carry no ``from_name`` — the
# parser has to remember the last sender it saw).
_MESSAGE_RE = re.compile(
    r'<div class="message[^"]*" id="message(\d+)"(.*?)'
    r'(?=<div class="message[^"]*" id="message|\Z)',
    re.S,
)
_SENDER_RE = re.compile(r'<div class="from_name">\s*(.*?)\s*</div>', re.S)
_VOICE_RE = re.compile(r'href="(voice_messages/[^"]+)"')
_TITLE_RE = re.compile(r'title="([^"]+)"')
_DURATION_RE = re.compile(r'<div class="status details">\s*([\d:]+)\s*</div>')

# One row per chunk of roughly this many seconds. Whisper's own segments are
# sentence-sized, which is too fine to cite ("at 7:20") and too many rows for
# a sheet; they are merged up to this length.
CHUNK_SECONDS = 60


def load_audio(path: str, sample_rate: int = 16000):
    """Decode an audio file to a mono float32 array with ffmpeg.

    faster-whisper would normally decode this itself through PyAV, but the
    PyAV release that ships for Python 3.13 on arm64 dropped a keyword
    argument faster-whisper still passes, and older PyAV has no wheel for this
    platform and will not build. ffmpeg is already a hard requirement for this
    kind of work, handles Telegram's Opus-in-Ogg without complaint, and
    removes that version coupling entirely.
    """

    import numpy as np

    result = subprocess.run(
        [
            "ffmpeg", "-nostdin", "-threads", "0",
            "-i", path,
            "-f", "s16le", "-ac", "1", "-acodec", "pcm_s16le",
            "-ar", str(sample_rate), "-",
        ],
        capture_output=True,
        check=True,
    )
    return np.frombuffer(result.stdout, np.int16).astype(np.float32) / 32768.0


def parse_duration(text: str) -> int:
    """Turn ``"12:20"`` or ``"1:02:03"`` into seconds."""

    parts = [int(p) for p in text.split(":")]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    return 0


def format_timestamp(seconds: float) -> str:
    """Format seconds as ``m:ss`` (or ``h:mm:ss``) for a human to read."""

    total = int(seconds)
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def iter_html_files(export_dir: Path) -> Iterator[Path]:
    """Yield messages.html, messages2.html, ... in their natural order."""

    files = list(export_dir.glob("messages*.html"))

    def order(path: Path) -> int:
        match = re.search(r"messages(\d*)\.html", path.name)
        return int(match.group(1)) if match and match.group(1) else 1

    yield from sorted(files, key=order)


def scan_export(export_dir: Path, sender: Optional[str]) -> list[dict[str, Any]]:
    """Return the voice messages in the export whose audio is on disk.

    Entries whose file was not downloaded are skipped silently — a Telegram
    export routinely references more media than it fetched.
    """

    found: list[dict[str, Any]] = []
    seen_ids: set[int] = set()

    for html_file in iter_html_files(export_dir):
        html = html_file.read_text(encoding="utf-8", errors="replace")
        last_sender = ""
        for raw_id, body in _MESSAGE_RE.findall(html):
            sender_match = _SENDER_RE.search(body)
            if sender_match:
                last_sender = sender_match.group(1).strip()

            voice = _VOICE_RE.search(body)
            if voice is None:
                continue
            if sender and last_sender != sender:
                continue

            message_id = int(raw_id)
            if message_id in seen_ids:
                continue
            audio_path = export_dir / voice.group(1)
            if not audio_path.exists():
                continue

            title = _TITLE_RE.search(body)
            duration = _DURATION_RE.search(body)
            seen_ids.add(message_id)
            found.append(
                {
                    "message_id": message_id,
                    "sender": last_sender,
                    "date": _iso_date(title.group(1) if title else ""),
                    "duration_seconds": (
                        parse_duration(duration.group(1)) if duration else 0
                    ),
                    "audio": str(audio_path),
                }
            )

    found.sort(key=lambda row: row["message_id"])
    return found


def _iso_date(title: str) -> str:
    """Convert ``"16.12.2024 07:44:54 UTC+02:00"`` to ``"2024-12-16"``.

    Falls back to the raw string so a format change degrades to something a
    human can still read rather than crashing the run.
    """

    match = re.match(r"(\d{2})\.(\d{2})\.(\d{4})", title or "")
    if not match:
        return title
    day, month, year = match.groups()
    try:
        return datetime(int(year), int(month), int(day)).date().isoformat()
    except ValueError:
        return title


def chunk_segments(
    segments: list[tuple[float, float, str]], chunk_seconds: int
) -> list[dict[str, Any]]:
    """Merge Whisper's sentence-level segments into ~``chunk_seconds`` blocks."""

    chunks: list[dict[str, Any]] = []
    start: Optional[float] = None
    end = 0.0
    buffer: list[str] = []

    for seg_start, seg_end, text in segments:
        text = text.strip()
        if not text:
            continue
        if start is None:
            start = seg_start
        end = seg_end
        buffer.append(text)
        if end - start >= chunk_seconds:
            chunks.append(
                {
                    "start": round(start, 1),
                    "end": round(end, 1),
                    "timestamp": format_timestamp(start),
                    "text": " ".join(buffer),
                }
            )
            start, buffer = None, []

    if buffer and start is not None:
        chunks.append(
            {
                "start": round(start, 1),
                "end": round(end, 1),
                "timestamp": format_timestamp(start),
                "text": " ".join(buffer),
            }
        )
    return chunks


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--export", required=True, help="Telegram export folder")
    parser.add_argument(
        "--sender",
        default=None,
        help="only this sender's voice messages (e.g. the coach's name)",
    )
    parser.add_argument("--out", default="bot/data/voice_transcripts.json")
    parser.add_argument(
        "--model",
        default="medium",
        help="faster-whisper model: small | medium | large-v3",
    )
    parser.add_argument("--language", default="ru")
    parser.add_argument(
        "--chunk-seconds", type=int, default=CHUNK_SECONDS
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="transcribe only the first N (0 = all) — useful for a quality check",
    )
    args = parser.parse_args()

    export_dir = Path(args.export).expanduser()
    if not export_dir.is_dir():
        print(f"Export folder not found: {export_dir}", file=sys.stderr)
        return 1

    found = scan_export(export_dir, args.sender)
    if not found:
        print("No voice messages with audio on disk matched.", file=sys.stderr)
        return 1

    total = sum(row["duration_seconds"] for row in found)
    who = args.sender or "everyone"
    print(
        f"Found {len(found)} voice messages from {who} "
        f"({total // 60}m {total % 60}s of audio)."
    )

    out_path = Path(args.out)
    done: dict[str, Any] = {}
    if out_path.exists() and out_path.is_file():
        # A half-written or empty file must not abort the run — the whole point
        # of resuming is to survive an interrupted one.
        try:
            done = {
                str(item["message_id"]): item
                for item in json.loads(out_path.read_text(encoding="utf-8"))
            }
            print(f"Resuming: {len(done)} already transcribed in {out_path}.")
        except (json.JSONDecodeError, TypeError, KeyError) as exc:
            print(f"Ignoring unreadable {out_path} ({exc}); starting fresh.")

    todo = [r for r in found if str(r["message_id"]) not in done]
    if args.limit:
        todo = todo[: args.limit]
    if not todo:
        print("Nothing left to do.")
        return 0

    # Imported late so --help and the scan work without the dependency.
    from faster_whisper import WhisperModel

    print(f"Loading the '{args.model}' model (first run downloads it)...")
    # int8 on CPU: the quality drop is not audible on speech and it is several
    # times faster on an Apple-silicon laptop.
    model = WhisperModel(args.model, device="cpu", compute_type="int8")

    for index, row in enumerate(todo, start=1):
        minutes, seconds = divmod(row["duration_seconds"], 60)
        print(
            f"[{index}/{len(todo)}] {row['date']} id={row['message_id']} "
            f"({minutes}:{seconds:02d}) ...",
            flush=True,
        )
        segments, _info = model.transcribe(
            load_audio(row["audio"]),
            language=args.language,
            vad_filter=True,  # drop silence, which otherwise invents text
        )
        collected = [(s.start, s.end, s.text) for s in segments]
        chunks = chunk_segments(collected, args.chunk_seconds)
        done[str(row["message_id"])] = {
            **row,
            "chunks": chunks,
            "text": " ".join(c["text"] for c in chunks),
        }
        # Written after every message so an interrupted run loses nothing.
        out_path.write_text(
            json.dumps(
                sorted(done.values(), key=lambda r: r["message_id"]),
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        words = len(done[str(row["message_id"])]["text"].split())
        print(f"      -> {len(chunks)} chunks, {words} words")

    print(f"\nDone. {len(done)} transcripts in {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
