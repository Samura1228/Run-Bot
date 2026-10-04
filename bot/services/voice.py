"""The coach's voice-message archive.

Transcripts of the coach's voice notes, produced offline by
``scripts/transcribe_voice.py`` and shipped as a data file next to the code.

Why a bundled file and not a worksheet: this is a static archive nobody edits,
it is read on every assistant question, and a Sheets round-trip per question
would add latency and quota for data that never changes. It is loaded once at
startup and lives in memory (a few hundred KB).

**Retrieval is done by the model, not here.** A keyword search was built first
and measured: 9 of 14 real questions found the right message, while nonsense
like "какая завтра погода" still returned hits. Russian is why — the coach
says "cadence" in Latin script while members ask about "каденс", and no amount
of suffix-chopping unifies "боль", "болит" and "боку". So this module only
supplies two things: a compact index of what exists (~1k tokens, cheap enough
to keep in every system prompt) and the full text of a message by id. The
assistant reads the index, names the ids it needs, and gets them back — which
handles synonyms, transliteration and morphology for free.
"""

from __future__ import annotations

import json
import logging
import math
import re
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

DATA_FILE = Path(__file__).resolve().parent.parent / "data" / "voice_transcripts.json"

_MAX_TRANSCRIPT_CHARS = 14000


class VoiceArchive:
    """In-memory index over the coach's transcribed voice messages."""

    def __init__(self, entries: list[dict[str, Any]]) -> None:
        self._entries = entries
        self._by_id = {e["message_id"]: e for e in entries}

    def __len__(self) -> int:
        return len(self._entries)

    @property
    def chunk_count(self) -> int:
        return sum(len(e.get("chunks", ())) for e in self._entries)

    def get(self, message_id: int) -> Optional[dict[str, Any]]:
        return self._by_id.get(message_id)

    def index_text(self) -> str:
        """One line per voice message — what exists, dated, for the prompt.

        Small enough (~1k tokens) to sit in the system prompt permanently, so
        the assistant always knows the archive's shape even when the keyword
        search finds nothing.
        """

        lines = []
        for entry in sorted(self._entries, key=lambda e: e.get("date", "")):
            mark = " [personal reply]" if entry.get("personal") else ""
            lines.append(
                f"- id={entry['message_id']} ({entry.get('date','')}): "
                f"{entry.get('annotation','')}{mark}"
            )
        return "\n".join(lines)

    def transcript_for(self, message_ids: list[int]) -> str:
        """Return the full transcripts of the given messages, with timestamps.

        Each chunk is labelled with the minute it starts at, which is what
        lets the answer cite "from 7:20". Unknown ids are skipped silently —
        the model picks them from the index, but a hallucinated id must not
        raise. The total is capped so one absurd request cannot blow up the
        next prompt.
        """

        blocks: list[str] = []
        budget = _MAX_TRANSCRIPT_CHARS
        for message_id in message_ids:
            entry = self._by_id.get(message_id)
            if entry is None:
                logger.info("Voice: unknown message id %s requested.", message_id)
                continue
            lines = [
                f"=== voice id={entry['message_id']} "
                f"date={entry.get('date','')} ==="
            ]
            for chunk in entry.get("chunks", ()):
                lines.append(f"[{chunk.get('timestamp','0:00')}] {chunk.get('text','')}")
            block = "\n".join(lines)
            if len(block) > budget:
                block = block[:budget] + " …"
            blocks.append(block)
            budget -= len(block)
            if budget <= 0:
                break
        return "\n\n".join(blocks)


def load_archive(path: Path = DATA_FILE) -> Optional[VoiceArchive]:
    """Load the archive, or return ``None`` when it is absent or unreadable.

    Missing data must never stop the bot: the assistant simply answers from
    the club's rules alone, exactly as it did before this feature existed.
    """

    try:
        entries = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.info("No voice archive at %s; answering without it.", path)
        return None
    except (OSError, json.JSONDecodeError) as exc:
        logger.error("Could not read the voice archive at %s: %s", path, exc)
        return None

    usable = [e for e in entries if e.get("chunks")]
    if not usable:
        logger.warning("Voice archive at %s has no usable entries.", path)
        return None

    archive = VoiceArchive(usable)
    logger.info(
        "Voice archive loaded: %d messages, %d chunks.",
        len(archive),
        archive.chunk_count,
    )
    return archive
