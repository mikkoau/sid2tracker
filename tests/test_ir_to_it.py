"""IR loader plus .it writer. No copyrighted tunes."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from sidengine.ir import Channel, Event, IRError, Instrument, MusicIR, Timing, load
from sid2it import __version__
from sid2it.write_it import (
    C5_SPEED,
    IT_SPECIAL_EDIT_HISTORY,
    IT_SPECIAL_SONG_MESSAGE,
    it_song_title,
    midi_to_it_note,
    song_message_text,
    write_it,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "scale.ir.json"


class MusicIRTests(unittest.TestCase):
    def test_fixture_loads(self) -> None:
        ir = load(FIXTURE)
        self.assertEqual(ir.title, "IR scale fixture")
        self.assertEqual(len(ir.channels), 2)
        self.assertEqual(ir.last_tick, 108)

    def test_bad_format(self) -> None:
        with self.assertRaises(IRError):
            MusicIR.from_dict({"format": "nope", "version": 1, "channels": []})

    def test_held_pitch_and_effect_events_roundtrip(self) -> None:
        pitch = Event.from_dict(
            {"tick": 6, "type": "pitch", "note": 67, "instrument": 1}
        )
        effect = Event.from_dict(
            {"tick": 7, "type": "effect", "arpeggio": [0, 4, 7]}
        )
        self.assertEqual(pitch.to_dict()["type"], "pitch")
        self.assertEqual(effect.to_dict()["arpeggio"], [0, 4, 7])


class ItWriterTests(unittest.TestCase):
    def test_midi_c4_is_tracker_c4(self) -> None:
        self.assertEqual(midi_to_it_note(60), 48)

    def test_writes_impm_and_imps(self) -> None:
        ir = load(FIXTURE)
        data = write_it(ir)
        self.assertEqual(data[0:4], b"IMPM")
        self.assertEqual(int.from_bytes(data[0x20:0x22], "little"), 2)  # orders
        self.assertEqual(int.from_bytes(data[0x24:0x26], "little"), 2)  # samples
        self.assertEqual(int.from_bytes(data[0x26:0x28], "little"), 1)  # patterns
        self.assertEqual(data[0x32], 6)  # speed = ticks_per_row
        self.assertIn(b"IMPS", data)
        self.assertGreater(len(data), 0xC0 + 80)
        c5 = int.from_bytes(data[data.find(b"IMPS") + 60 : data.find(b"IMPS") + 64], "little")
        self.assertEqual(c5, C5_SPEED)

    def test_cli_roundtrip_file(self) -> None:
        ir = load(FIXTURE)
        blob = write_it(ir)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "scale.it"
            path.write_bytes(blob)
            self.assertTrue(path.stat().st_size == len(blob))

    def test_song_message_copies_sid_credits_and_converter_line(self) -> None:
        ir = MusicIR(
            title="Jäger",
            source={"author": "Unit Test", "released": "2026 Test"},
            instruments=[Instrument(id=1, name="pulse")],
            channels=[
                Channel(
                    id=0,
                    events=[
                        Event(tick=0, type="note_on", note=60, instrument=1),
                        Event(tick=6, type="note_off"),
                    ],
                )
            ],
            timing=Timing(ticks_per_row=6),
        )
        text = song_message_text(ir)
        self.assertEqual(
            text,
            "Jäger\nUnit Test\n2026 Test\n\n"
            f"Converted with SID2Tracker {__version__}",
        )
        data = write_it(ir)
        special = int.from_bytes(data[0x2E:0x30], "little")
        self.assertEqual(special & IT_SPECIAL_SONG_MESSAGE, IT_SPECIAL_SONG_MESSAGE)
        self.assertEqual(special & IT_SPECIAL_EDIT_HISTORY, IT_SPECIAL_EDIT_HISTORY)
        length = int.from_bytes(data[0x36:0x38], "little")
        offset = int.from_bytes(data[0x38:0x3C], "little")
        message = data[offset : offset + length]
        self.assertEqual(message[-1], 0)
        self.assertEqual(message[:-1], text.replace("\n", "\r").encode("cp1252"))
        self.assertNotIn(b"\n", message[:-1])
        self.assertIn(b"STPM", data)
        auth = b"AUTH" + (9).to_bytes(2, "little") + b"Unit Test"
        self.assertIn(auth, data)

    def test_song_message_skips_blank_sid_fields(self) -> None:
        ir = load(FIXTURE)
        text = song_message_text(ir)
        self.assertEqual(
            text,
            "IR scale fixture\n\n"
            f"Converted with SID2Tracker {__version__}",
        )
        data = write_it(ir)
        self.assertNotIn(b"STPM", data)
        self.assertNotIn(b"AUTH", data)

    def test_multi_subtune_labels_song_name_and_comment(self) -> None:
        ir = MusicIR(
            title="Paperboy",
            source={
                "author": "Mark Cooksey",
                "released": "1984 Elite",
                "subtune": 1,
                "songs": 5,
            },
            instruments=[Instrument(id=1, name="pulse")],
            channels=[
                Channel(
                    id=0,
                    events=[
                        Event(tick=0, type="note_on", note=60, instrument=1),
                        Event(tick=6, type="note_off"),
                    ],
                )
            ],
            timing=Timing(ticks_per_row=6),
        )
        self.assertEqual(it_song_title(ir), "Paperboy (1/5)")
        self.assertEqual(
            song_message_text(ir).splitlines()[0],
            "Paperboy (1/5)",
        )
        data = write_it(ir)
        name = data[4:30].split(b"\x00", 1)[0]
        self.assertEqual(name, b"Paperboy (1/5)")

    def test_single_subtune_keeps_plain_title(self) -> None:
        ir = load(FIXTURE)
        ir.source["subtune"] = 1
        ir.source["songs"] = 1
        self.assertEqual(it_song_title(ir), "IR scale fixture")
        self.assertEqual(song_message_text(ir).splitlines()[0], "IR scale fixture")

    def test_long_title_keeps_subtune_in_it_name(self) -> None:
        ir = MusicIR(
            title="012345678901234567890123456789",
            source={"subtune": 12, "songs": 34},
            instruments=[Instrument(id=1, name="pulse")],
            channels=[
                Channel(
                    id=0,
                    events=[
                        Event(tick=0, type="note_on", note=60, instrument=1),
                        Event(tick=6, type="note_off"),
                    ],
                )
            ],
            timing=Timing(ticks_per_row=6),
        )
        self.assertEqual(
            it_song_title(ir),
            "012345678901234567890123456789 (12/34)",
        )
        header_name = it_song_title(ir, fallback="untitled", max_len=25)
        self.assertTrue(header_name.endswith(" (12/34)"))
        self.assertEqual(len(header_name), 25)
        data = write_it(ir)
        name = data[4:30].split(b"\x00", 1)[0].decode("ascii")
        self.assertEqual(name, header_name)
        self.assertEqual(
            song_message_text(ir).splitlines()[0],
            "012345678901234567890123456789 (12/34)",
        )


if __name__ == "__main__":
    unittest.main()
