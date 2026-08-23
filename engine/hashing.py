"""Content hashing — the fingerprint primitive.

Lived in kgstore.py, which meant anything needing a file hash had to import the
knowledge store. Extracted so insight_cache (and anything else) can hash a file
without pulling in versioning/certification machinery.
"""
from __future__ import annotations

import hashlib
from pathlib import Path


def file_sha256(path: str | Path) -> str:
    h = hashlib.sha256()
    h.update(Path(path).read_bytes())
    return h.hexdigest()
