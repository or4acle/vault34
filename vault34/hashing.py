"""Content hashing used for deduplication.

Two complementary hashes are produced per file:

* ``sha256``  - exact byte identity, cheap and collision free.
* ``phash`` / ``dhash`` - perceptual hashes (64-bit DCT and gradient hashes).
  These survive re-encoding, rescaling and mild recompression, which lets the
  index spot visually identical files that are byte-different.

Deduplication is two-stage: a cheap exact-hash probe first, then a bounded
perceptual scan limited to the same 16-bit bucket prefix.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import imagehash
from PIL import Image, ImageFile

ImageFile.LOAD_TRUNCATED_IMAGES = True
Image.MAX_IMAGE_PIXELS = None

PHASH_BUCKET_BITS = 16  # 16 hex chars == 64 bits


def sha256_file(path: str | Path, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


def perceptual_hashes(image: Image.Image) -> tuple[str, str]:
    """Return ``(phash, dhash)`` as 16-char hex strings."""
    ph = str(imagehash.phash(image, hash_size=8))
    dh = str(imagehash.dhash(image, hash_size=8))
    return ph, dh


def hamming(a: str, b: str) -> int:
    """Population count of the XOR between two hex hash strings."""
    if not a or not b or len(a) != len(b):
        return 64
    try:
        return (int(a, 16) ^ int(b, 16)).bit_count()
    except ValueError:
        return 64


def bucket(phash: str) -> str:
    """Group key so perceptual comparisons only run against a narrow slice."""
    return phash[:PHASH_BUCKET_BITS // 4] if phash else ""


def load_index(path: str | Path) -> dict:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def save_index(path: str | Path, data: dict) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, separators=(",", ":"))


class DuplicateMatcher:
    """Looks up incoming files against the index.

    Exact SHA-256 matches short-circuit; otherwise a perceptual match is
    accepted when both the pHash and dHash distances are within tolerance,
    which keeps false positives low for recompressed or rescaled copies.
    """

    def __init__(self, phash_tolerance: int = 4, dhash_tolerance: int = 6):
        self.phash_tolerance = phash_tolerance
        self.dhash_tolerance = dhash_tolerance
        self._by_sha: dict[str, dict] = {}
        self._by_bucket: dict[str, list[dict]] = {}

    def add(self, sha: str | None, phash: str | None, dhash: str | None,
            record: dict) -> None:
        if sha:
            self._by_sha[sha] = record
        if phash:
            self._by_bucket.setdefault(bucket(phash), []).append(
                {"phash": phash, "dhash": dhash or "", **record})

    def match(self, sha: str | None, phash: str | None, dhash: str | None) -> tuple[dict | None, str]:
        if sha and sha in self._by_sha:
            return self._by_sha[sha], "exact"
        if not phash:
            return None, "none"
        for candidate in self._by_bucket.get(bucket(phash), ()):
            if phash == candidate["phash"]:
                return candidate, "perceptual"
            dp = hamming(phash, candidate["phash"])
            dd = hamming(dhash, candidate.get("dhash", "") or "")
            if dp <= self.phash_tolerance and dd <= self.dhash_tolerance:
                return candidate, "perceptual"
        return None, "none"

    def __len__(self) -> int:
        return len(self._by_sha)
