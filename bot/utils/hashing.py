"""Deterministic image byte hashing for deduplication."""

from __future__ import annotations

import hashlib


def compute_image_hash(data: bytes) -> str:
    """Return the SHA-256 hex digest of the given image bytes.

    Args:
        data: Raw image bytes as downloaded from Telegram.

    Returns:
        A 64-character lowercase hexadecimal SHA-256 digest.
    """

    return hashlib.sha256(data).hexdigest()