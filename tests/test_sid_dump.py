"""Synthetic PSID dump and lift tests. No HVSC tune is committed."""

from __future__ import annotations

import struct
import tempfile
import unittest
from pathlib import Path

from sidengine.dump import lift_to_ir, run_psid

def synthetic_dump(init_writes: list, frames: list, ticks: int = 2) -> dict:
    return {
        "source": "<synthetic>",
        "title": "synthetic",
        "author": "tests",
        "released": "2026",
        "subtune": 1,
        "sid_model": "MOS6581",
        "clock": "PAL",
        "frame_hz": 50.125,
        "ticks": ticks,
        "init_writes": init_writes,
        "frames": frames,
    }

def synthetic_sid() -> bytes:
    header = bytearray(0x7C)
    header[0:4] = b"PSID"
    struct.pack_into(
        ">HHHHHHH",
        header,
        4,
        2,       # version
        0x7C,    # data offset
        0x1000,  # explicit load
        0x1000,  # init
        0x1001,  # play
        1,       # songs
        1,       # start
    )
    struct.pack_into(">I", header, 0x12, 0)
    header[0x16:0x16 + 9] = b"Dump Test"
    header[0x36:0x36 + 5] = b"Tests"
    header[0x56:0x56 + 4] = b"2026"
    struct.pack_into(">H", header, 0x76, 0x14)  # PAL, 6581

    # Init RTS. Play performs the common $09 hard restart, sets C4-ish
    # frequency and ADSR, then enables pulse+gate.
    payload = bytes([
        0x60,
        0xA9, 0x09, 0x8D, 0x04, 0xD4,
        0xA9, 0x67, 0x8D, 0x00, 0xD4,
        0xA9, 0x11, 0x8D, 0x01, 0xD4,
        0xA9, 0x08, 0x8D, 0x02, 0xD4,
        0xA9, 0x08, 0x8D, 0x03, 0xD4,
        0xA9, 0x09, 0x8D, 0x05, 0xD4,
        0xA9, 0x94, 0x8D, 0x06, 0xD4,
        0xA9, 0x41, 0x8D, 0x04, 0xD4,
        0x60,
    ])
    return bytes(header) + payload

class SidDumpTests(unittest.TestCase):
    def test_run_and_lift_hard_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "synthetic.sid"
            path.write_bytes(synthetic_sid())
            dump = run_psid(path, ticks=3)
        self.assertEqual(dump["ticks"], 3)
        self.assertEqual(dump["clock"], "PAL")
        self.assertEqual(dump["title"], "Dump Test")
        self.assertEqual(dump["songs"], 1)
        self.assertEqual(dump["subtune"], 1)
        self.assertEqual(dump["author"], "Tests")
        self.assertEqual(dump["released"], "2026")
        self.assertTrue(any(frame["writes"] for frame in dump["frames"]))

        ir = lift_to_ir(dump)
        self.assertEqual(ir.source["author"], "Tests")
        self.assertEqual(ir.source["released"], "2026")
        self.assertEqual(ir.source["songs"], 1)
        self.assertEqual(ir.source["subtune"], 1)
        notes = [
            event
            for channel in ir.channels
            for event in channel.events
            if event.type == "note_on"
        ]
        self.assertEqual(len(notes), 3)
        self.assertEqual(ir.source["ticks"], 3)
        self.assertGreaterEqual(ir.source["last_write_tick"], 0)
        self.assertEqual(notes[0].note, 60)
        self.assertEqual(ir.instruments[0].waveform, "pulse")
        self.assertEqual(ir.instruments[0].pulse_width, 0x808)
        self.assertEqual(ir.instruments[0].ctrl, 0x41)

    def test_run_psid_reports_tick_progress(self) -> None:
        seen: list[tuple[int, int]] = []
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "synthetic.sid"
            path.write_bytes(synthetic_sid())
            run_psid(
                path,
                ticks=3,
                on_progress=lambda done, total: seen.append((done, total)),
            )
        self.assertEqual(seen, [(1, 3), (2, 3), (3, 3)])

    def test_lift_records_voice3_off_and_filter_mode(self) -> None:
        dump = synthetic_dump(
            [],
            [
                {
                    "tick": 0,
                    "writes": [
                        [0, 0x67],
                        [1, 0x11],
                        [4, 0x11],
                        [23, 0x01],
                        [24, 0x9F],
                    ],
                },
                {"tick": 1, "writes": [[4, 0x10]]},
            ],
            ticks=2,
        )
        ir = lift_to_ir(dump)
        self.assertEqual(ir.filter[0].mode, ["lp"])
        self.assertEqual(ir.filter[0].routing, [1, 0, 0])
        self.assertTrue(ir.filter[0].voice3_off)
        self.assertEqual(ir.filter[0].volume, 15)

    def test_ring_modulation_records_the_modulator_ratio(self) -> None:
        # Voice 1 rings against voice 3, so the ratio is FREQ3 over FREQ1.
        dump = synthetic_dump(
            [],
            [
                {
                    "tick": 0,
                    "writes": [
                        [14, 0xCE],
                        [15, 0x22],  # voice 3 an octave above voice 1
                        [0, 0x67],
                        [1, 0x11],
                        [4, 0x15],  # triangle + ring + gate
                    ],
                },
                {"tick": 1, "writes": [[4, 0x14]]},
            ],
        )
        ir = lift_to_ir(dump)
        onset = ir.channels[0].events[0]
        self.assertEqual(onset.type, "note_on")
        self.assertAlmostEqual(onset.modulator_ratio, 2.0, places=3)
        self.assertTrue(ir.instruments[0].ring)
        self.assertFalse(ir.instruments[0].sync)

    def test_pulse_sweep_under_a_held_gate_becomes_pw_points(self) -> None:
        frames = [
            {
                "tick": 0,
                "writes": [[0, 0x67], [1, 0x11], [2, 0x00], [3, 0x08], [4, 0x41]],
            }
        ]
        for tick in range(1, 4):
            frames.append({"tick": tick, "writes": [[2, 0x40 * tick]]})
        frames.append({"tick": 4, "writes": [[4, 0x40]]})
        ir = lift_to_ir(synthetic_dump([], frames, ticks=5))
        onset = ir.channels[0].events[0]
        self.assertEqual([tick for tick, _ in onset.pw_points], [0, 1, 2, 3])
        self.assertEqual([pw for _, pw in onset.pw_points], [2048, 2112, 2176, 2240])

    def test_static_pulse_records_a_single_width(self) -> None:
        dump = synthetic_dump(
            [],
            [
                {
                    "tick": 0,
                    "writes": [[0, 0x67], [1, 0x11], [2, 0x00], [3, 0x08], [4, 0x41]],
                },
                {"tick": 1, "writes": [[4, 0x40]]},
            ],
        )
        onset = lift_to_ir(dump).channels[0].events[0]
        self.assertEqual(onset.pw_points, [[0, 2048]])
        self.assertIsNone(onset.modulator_ratio)

    def test_note_survives_frequency_cleared_after_gate(self) -> None:
        # Commando subtune 7 gates on and then zeroes the frequency in the
        # same frame; sampling pitch at end of frame would drop the note.
        dump = synthetic_dump(
            init_writes=[],
            frames=[
                {
                    "tick": 0,
                    "writes": [
                        [0, 0x67],
                        [1, 0x11],
                        [4, 0x41],
                        [0, 0x00],
                        [1, 0x00],
                    ],
                }
            ],
        )
        ir = lift_to_ir(dump)
        notes = [
            event
            for channel in ir.channels
            for event in channel.events
            if event.type == "note_on"
        ]
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0].note, 60)

    def test_init_gate_counts_as_a_note(self) -> None:
        # A subtune whose init gates a voice on, with play only rewriting the
        # same control value, still has to produce an onset.
        dump = synthetic_dump(
            init_writes=[[0, 0x67], [1, 0x11], [4, 0x41]],
            frames=[{"tick": 1, "writes": [[4, 0x41]]}],
        )
        ir = lift_to_ir(dump)
        notes = [
            event
            for channel in ir.channels
            for event in channel.events
            if event.type == "note_on"
        ]
        self.assertEqual([event.tick for event in notes], [0])

    def test_pitch_only_driver_phrases_on_frequency_zero(self) -> None:
        # Up'n'Down gates once and plays every note by setting a frequency
        # and zeroing it again.
        dump = synthetic_dump(
            init_writes=[[6, 0x70]],
            ticks=4,
            frames=[
                {"tick": 0, "writes": [[0, 0x67], [1, 0x11], [4, 0x11]]},
                {"tick": 1, "writes": [[0, 0x00], [1, 0x00]]},
                {"tick": 2, "writes": [[0, 0x60], [1, 0x16]]},
                {"tick": 3, "writes": [[0, 0x00], [1, 0x00]]},
            ],
        )
        ir = lift_to_ir(dump)
        kinds = [(event.tick, event.type) for event in ir.channels[0].events]
        self.assertEqual(
            kinds,
            [(0, "note_on"), (1, "note_off"), (2, "note_on"), (3, "note_off")],
        )

    def test_vibrato_does_not_retrigger(self) -> None:
        # Frequency wobble that never reaches zero stays inside one note.
        dump = synthetic_dump(
            init_writes=[[6, 0x70]],
            ticks=4,
            frames=[
                {"tick": 0, "writes": [[0, 0x67], [1, 0x11], [4, 0x11]]},
                {"tick": 1, "writes": [[0, 0x70]]},
                {"tick": 2, "writes": [[0, 0x60]]},
                {"tick": 3, "writes": [[0, 0x70]]},
            ],
        )
        ir = lift_to_ir(dump)
        onsets = [e for e in ir.channels[0].events if e.type == "note_on"]
        self.assertEqual(len(onsets), 1)

    def test_fast_three_pitch_cycle_becomes_one_arpeggio(self) -> None:
        # PAL words for C4, D#4 and G4: a minor chord arpeggio under one gate.
        cycle = [(0x67, 0x11), (0xB2, 0x14), (0x13, 0x1A)]
        frames = [{"tick": 0, "writes": [[0, 0x67], [1, 0x11], [4, 0x11]]}]
        for tick in range(1, 12):
            lo, hi = cycle[tick % 3]
            frames.append({"tick": tick, "writes": [[0, lo], [1, hi]]})
        frames.append({"tick": 12, "writes": [[4, 0x10]]})
        ir = lift_to_ir(synthetic_dump([], frames, ticks=13), ticks_per_row=6)

        events = ir.channels[0].events
        onsets = [event for event in events if event.type == "note_on"]
        self.assertEqual(len(onsets), 1)
        self.assertEqual(onsets[0].note, 60)
        self.assertEqual(onsets[0].arpeggio, [0, 3, 7])

    def test_slow_pitch_change_under_a_held_gate_is_a_new_note(self) -> None:
        # One gate, three pitches, each held a full row: melody, not arpeggio.
        frames = [
            {"tick": 0, "writes": [[0, 0x67], [1, 0x11], [4, 0x11]]},
            {"tick": 6, "writes": [[0, 0xB2], [1, 0x14]]},
            {"tick": 12, "writes": [[0, 0x13], [1, 0x1A]]},
            {"tick": 18, "writes": [[4, 0x10]]},
        ]
        ir = lift_to_ir(synthetic_dump([], frames, ticks=19), ticks_per_row=6)

        pitched = [
            e for e in ir.channels[0].events if e.type in ("note_on", "pitch")
        ]
        self.assertEqual(
            [(e.tick, e.type, e.note) for e in pitched],
            [(0, "note_on", 60), (6, "pitch", 63), (12, "pitch", 67)],
        )
        self.assertTrue(all(not e.arpeggio for e in pitched))

    def test_fast_semitone_wobble_stays_one_note(self) -> None:
        # A one-semitone cycle is vibrato, and must not become a Jxy chord.
        cycle = [(0x67, 0x11), (0xE8, 0x12)]
        frames = [{"tick": 0, "writes": [[0, 0x67], [1, 0x11], [4, 0x11]]}]
        for tick in range(1, 12):
            lo, hi = cycle[tick % 2]
            frames.append({"tick": tick, "writes": [[0, lo], [1, hi]]})
        ir = lift_to_ir(synthetic_dump([], frames, ticks=12), ticks_per_row=6)

        onsets = [e for e in ir.channels[0].events if e.type == "note_on"]
        self.assertEqual(len(onsets), 1)
        self.assertEqual(onsets[0].note, 60)
        self.assertEqual(onsets[0].arpeggio, [])

    def test_wide_octave_cycle_keeps_local_root(self) -> None:
        # Last Ninja II uses C, C+24, C+12 cycles. Jxy cannot carry +24,
        # but emitting all three as retriggered notes destroys length and ADSR.
        cycle = [(0x67, 0x11), (0x9C, 0x45), (0xCE, 0x22)]
        frames = [{"tick": 0, "writes": [[0, 0x67], [1, 0x11], [4, 0x11]]}]
        for tick in range(1, 12):
            lo, hi = cycle[tick % 3]
            frames.append({"tick": tick, "writes": [[0, lo], [1, hi]]})
        frames.append({"tick": 12, "writes": [[4, 0x10]]})
        ir = lift_to_ir(synthetic_dump([], frames, ticks=13), ticks_per_row=2)

        pitched = [
            e for e in ir.channels[0].events if e.type in ("note_on", "pitch")
        ]
        self.assertEqual([(e.type, e.note) for e in pitched], [("note_on", 60)])

    def test_wide_stack_hanging_on_high_keeps_root(self) -> None:
        # Cauldron II: octave stack then a long sustain on the top harmonic.
        # Looking ahead into the next lower root, or adopting the hang as
        # melody, produced 54 -> 34 -> 59 -> 84 slides.
        stack = [
            (0x8F, 0x0C),  # ~MIDI 54
            (0x39, 0x35),  # ~MIDI 79
            (0x8F, 0xE1),  # ~MIDI 104
        ]
        low = [
            (0xD8, 0x03),  # ~MIDI 34
            (0x4B, 0x10),  # ~MIDI 59
            (0x0E, 0x45),  # ~MIDI 84
        ]
        frames = [{"tick": 0, "writes": [[0, stack[0][0]], [1, stack[0][1]], [4, 0x11]]}]
        for tick in range(1, 12):
            lo, hi = stack[tick % 3]
            frames.append({"tick": tick, "writes": [[0, lo], [1, hi]]})
        for tick in range(12, 24):
            lo, hi = low[(tick - 12) % 3]
            frames.append({"tick": tick, "writes": [[0, lo], [1, hi]]})
        for tick in range(24, 40):
            frames.append({"tick": tick, "writes": [[0, low[2][0]], [1, low[2][1]]]})
        frames.append({"tick": 40, "writes": [[4, 0x10]]})
        ir = lift_to_ir(synthetic_dump([], frames, ticks=41), ticks_per_row=1)

        pitched = [
            e for e in ir.channels[0].events if e.type in ("note_on", "pitch")
        ]
        notes = [e.note for e in pitched]
        # Roots may move (54 -> 34) but must never climb the stack to 79/84/104.
        self.assertTrue(notes)
        self.assertLessEqual(max(notes), 54)
        self.assertNotIn(59, notes)
        self.assertNotIn(84, notes)
        self.assertNotIn(104, notes)

    def noise_click_dump(self) -> dict:
        # Starball / Huelsbeck bass: one frame of high-frequency noise, then
        # pulse at the musical pitch, all under one gate.
        return synthetic_dump(
            [],
            [
                {
                    "tick": 0,
                    "writes": [
                        [0, 0x6B],
                        [1, 0xAF],
                        [5, 0x09],
                        [6, 0x0F],
                        [4, 0x81],
                    ],
                },
                {
                    "tick": 1,
                    "writes": [
                        [0, 0x67],
                        [1, 0x11],
                        [2, 0x00],
                        [3, 0x08],
                        [4, 0x41],
                    ],
                },
                {"tick": 5, "writes": [[4, 0x40]]},
            ],
            ticks=6,
        )

    def test_noise_click_then_pulse_body_is_two_notes(self) -> None:
        ir = lift_to_ir(self.noise_click_dump(), ticks_per_row=1)
        events = ir.channels[0].events
        kinds = [(e.tick, e.type, e.note) for e in events]
        self.assertEqual(
            kinds,
            [(0, "note_on", 100), (1, "note_on", 60), (5, "note_off", None)],
        )
        self.assertEqual(events[0].extra.get("onset"), None)
        self.assertEqual(events[1].extra.get("onset"), "waveform")
        by_id = {i.id: i for i in ir.instruments}
        self.assertEqual(by_id[events[0].instrument].waveform, "noise")
        self.assertEqual(by_id[events[1].instrument].waveform, "pulse")

    def test_click_sharing_a_row_with_the_body_is_dropped(self) -> None:
        # One tracker row holds one note. The pulse is what rings through the
        # release, so the click must not be what the row plays.
        ir = lift_to_ir(self.noise_click_dump(), ticks_per_row=6)
        events = ir.channels[0].events
        self.assertEqual(
            [(e.tick, e.type, e.note) for e in events],
            [(1, "note_on", 60), (5, "note_off", None)],
        )
        by_id = {i.id: i for i in ir.instruments}
        self.assertEqual(by_id[events[0].instrument].waveform, "pulse")

    def test_same_named_waveform_under_gate_does_not_retrigger(self) -> None:
        # Noise with extra pulse bits still names as noise, so $C1->$81 is not
        # a new note. Combined $51->$41 stays pulse for the same reason.
        dump = synthetic_dump(
            [],
            [
                {"tick": 0, "writes": [[0, 0x67], [1, 0x11], [4, 0xC1]]},
                {"tick": 1, "writes": [[4, 0x81]]},
                {"tick": 2, "writes": [[4, 0x80]]},
            ],
            ticks=3,
        )
        ir = lift_to_ir(dump)
        onsets = [e for e in ir.channels[0].events if e.type == "note_on"]
        self.assertEqual(len(onsets), 1)
        self.assertEqual(ir.instruments[0].waveform, "noise")

    def test_pulse_to_saw_under_a_held_gate_is_a_new_note(self) -> None:
        dump = synthetic_dump(
            [],
            [
                {"tick": 0, "writes": [[0, 0x67], [1, 0x11], [4, 0x41]]},
                {"tick": 3, "writes": [[4, 0x21]]},
                {"tick": 6, "writes": [[4, 0x20]]},
            ],
            ticks=7,
        )
        ir = lift_to_ir(dump, ticks_per_row=3)
        onsets = [e for e in ir.channels[0].events if e.type == "note_on"]
        self.assertEqual(
            [(e.tick, e.extra.get("onset")) for e in onsets],
            [(0, None), (3, "waveform")],
        )
        self.assertEqual(
            [ir.instruments[e.instrument - 1].waveform for e in onsets],
            ["pulse", "saw"],
        )

    def test_gate_only_arm_then_release_phase_pulse(self) -> None:
        # Vlindertjes lead: $01 charges the envelope with no oscillator, then
        # $40 selects pulse with the gate already clear so the note is the
        # release. Gate-edge detection alone misses every pitch.
        dump = synthetic_dump(
            [],
            [
                {
                    "tick": 0,
                    "writes": [
                        [0, 0xA5],
                        [1, 0x1F],
                        [5, 0x00],
                        [6, 0x7F],
                        [4, 0x01],
                    ],
                },
                {"tick": 1, "writes": [[2, 0x80], [3, 0x07]]},
                {"tick": 2, "writes": [[4, 0x40], [2, 0x00], [3, 0x08]]},
                {"tick": 10, "writes": [[0, 0x31], [1, 0x1C], [4, 0x01]]},
                {"tick": 12, "writes": [[4, 0x40]]},
                {"tick": 20, "writes": [[4, 0x00]]},
            ],
            ticks=21,
        )
        ir = lift_to_ir(dump, ticks_per_row=1)
        events = [
            (e.tick, e.type, e.note)
            for e in ir.channels[0].events
            if e.type in ("note_on", "note_off")
        ]
        self.assertEqual(
            events,
            [
                (2, "note_on", 70),
                (10, "note_off", None),
                (12, "note_on", 68),
                (20, "note_off", None),
            ],
        )
        self.assertEqual(ir.instruments[0].waveform, "pulse")
        self.assertEqual(ir.instruments[0].sustain, 7)
        self.assertEqual(ir.instruments[0].release, 15)

    def test_the_waveform_left_at_gate_off_keeps_the_row(self) -> None:
        # A wavetable click, a noise frame, then the pulse the note is made
        # of, and the gate falls back onto that pulse.
        dump = synthetic_dump(
            [],
            [
                {"tick": 0, "writes": [[0, 0x67], [1, 0x01], [4, 0x11]]},
                {"tick": 1, "writes": [[0, 0x00], [1, 0x40], [4, 0x81]]},
                {"tick": 2, "writes": [[0, 0x67], [1, 0x11], [4, 0x41]]},
                {"tick": 3, "writes": [[4, 0x40]]},
            ],
            ticks=4,
        )
        ir = lift_to_ir(dump, ticks_per_row=6)
        onsets = [e for e in ir.channels[0].events if e.type == "note_on"]
        self.assertEqual([e.tick for e in onsets], [2])
        self.assertEqual(ir.instruments[onsets[0].instrument - 1].waveform, "pulse")

    def test_duplicate_register_writes_are_elided(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "synthetic.sid"
            path.write_bytes(synthetic_sid())
            dump = run_psid(path, ticks=2)
        # Each repeat retains $09->$41 because it is a real hard restart,
        # while unchanged frequency and ADSR writes disappear.
        second = dump["frames"][1]["writes"]
        self.assertEqual(second, [[4, 0x09], [4, 0x41]])

if __name__ == "__main__":
    unittest.main()
