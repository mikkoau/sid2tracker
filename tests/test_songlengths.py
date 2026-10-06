"""HVSC Songlengths.md5 lookup, against a synthetic database."""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from sidengine.songlengths import find_database, in_hvsc_tree, parse_times, song_length, song_lengths

class ParseTimesTests(unittest.TestCase):
    def test_minutes_seconds_and_milliseconds(self) -> None:
        self.assertEqual(parse_times("3:55.594 1:01.288 0:06"), [235.594, 61.288, 6.0])

    def test_empty_field(self) -> None:
        self.assertEqual(parse_times(""), [])

class LookupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name) / "C64Music"
        root.mkdir()
        self.sid = root / "MUSICIANS" / "X" / "Tune.sid"
        self.sid.parent.mkdir(parents=True)
        self.sid.write_bytes(b"PSID-not-really")
        digest = hashlib.md5(self.sid.read_bytes()).hexdigest()
        docs = root / "DOCUMENTS"
        docs.mkdir()
        self.db = docs / "Songlengths.md5"
        self.db.write_text(
            "; comment line\n"
            "; /MUSICIANS/X/Tune.sid\n"
            f"{digest}=3:55.594 1:01.288 0:06\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_finds_database_by_walking_up(self) -> None:
        self.assertEqual(song_lengths(self.sid), [235.594, 61.288, 6.0])

    def test_subtune_is_one_based(self) -> None:
        self.assertAlmostEqual(song_length(self.sid, 1), 235.594)
        self.assertAlmostEqual(song_length(self.sid, 2), 61.288)

    def test_subtune_out_of_range(self) -> None:
        self.assertIsNone(song_length(self.sid, 4))

    def test_unknown_tune(self) -> None:
        other = self.sid.parent / "Other.sid"
        other.write_bytes(b"different bytes")
        self.assertIsNone(song_lengths(other))

    def test_missing_database(self) -> None:
        with tempfile.TemporaryDirectory() as empty:
            stray = Path(empty) / "C64Music" / "Stray.sid"
            stray.parent.mkdir()
            stray.write_bytes(b"x")
            self.assertIsNone(song_lengths(stray))

    def test_ignores_songlengths_outside_c64music(self) -> None:
        with tempfile.TemporaryDirectory() as empty:
            root = Path(empty)
            sid = root / "Tune.sid"
            sid.write_bytes(b"x")
            docs = root / "DOCUMENTS"
            docs.mkdir()
            (docs / "Songlengths.md5").write_text("deadbeef=1:00\n", encoding="utf-8")
            self.assertFalse(in_hvsc_tree(sid))
            self.assertIsNone(find_database(sid))
            self.assertIsNone(song_lengths(sid))

if __name__ == "__main__":
    unittest.main()
