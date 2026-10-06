"""Look up HVSC Songlengths.md5 so a dump can cover a whole subtune.

HVSC keys song lengths by the MD5 of the entire .sid file:

    ; /MUSICIANS/H/Hubbard_Rob/Commando.sid
    6d019ecba831a9f853675aac29a61c10=3:55.594 1:01.288 0:06 ...

One time per subtune, in file order, so subtune N is entry N-1.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

DB_NAME = "Songlengths.md5"
DOCUMENTS = "DOCUMENTS"
_TIME = re.compile(r"(\d+):(\d+(?:\.\d+)?)")


class SongLengthError(Exception):
    pass


def in_hvsc_tree(sid: Path) -> bool:
    """True when the path is inside an HVSC C64Music checkout."""
    return "c64music" in (part.lower() for part in Path(sid).parts)


def find_database(sid: Path) -> Path | None:
    """Walk up from the .sid looking for HVSC DOCUMENTS/Songlengths.md5.

    Only used when the tune actually lives under C64Music. A stray .sid next
    to some other DOCUMENTS folder must not pick up HVSC times.
    """
    if not in_hvsc_tree(sid):
        return None
    for parent in sid.resolve().parents:
        candidate = parent / DOCUMENTS / DB_NAME
        if candidate.is_file():
            return candidate
    return None


def parse_times(field: str) -> list[float]:
    times = []
    for minutes, seconds in _TIME.findall(field):
        times.append(int(minutes) * 60 + float(seconds))
    return times


def song_lengths(sid: Path, database: Path | None = None) -> list[float] | None:
    """Seconds per subtune, or None when the tune is not in the database."""
    database = database or find_database(sid)
    if database is None:
        return None
    digest = hashlib.md5(sid.read_bytes()).hexdigest()
    with database.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if not line.startswith(digest):
                continue
            _, _, field = line.partition("=")
            times = parse_times(field)
            return times or None
    return None


def song_length(sid: Path, subtune: int, database: Path | None = None) -> float | None:
    times = song_lengths(sid, database)
    if not times or not 1 <= subtune <= len(times):
        return None
    return times[subtune - 1]
