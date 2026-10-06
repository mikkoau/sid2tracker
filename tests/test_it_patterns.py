"""Multi-pattern .it output from a long IR timeline."""

from __future__ import annotations

import unittest
from pathlib import Path

from sidengine.ir import Channel, Event, Instrument, MusicIR, Timing
from sid2it.write_it import (
    C5_SPEED,
    ENVELOPE_SIZE,
    INSTRUMENT_SIZE,
    IT_EFFECT_B,
    IT_EFFECT_G,
    IT_EFFECT_J,
    MIN_PATTERN_ROWS,
    NOISE_C5_SPEED,
    NOISE_SAMPLE_LEN,
    PATTERN_ROWS,
    _pcm,
    _quantize,
    adsr_envelope_nodes,
    second_loop_start,
    write_it,
    write_module,
)

def pattern_pointer_base(data: bytes) -> int:
    ord_num = int.from_bytes(data[0x20:0x22], "little")
    ins_num = int.from_bytes(data[0x22:0x24], "little")
    smp_num = int.from_bytes(data[0x24:0x26], "little")
    return 0xC0 + ord_num + ins_num * 4 + smp_num * 4

def envelope_end_tick(header: bytes) -> int:
    envelope = header[0x130 : 0x130 + ENVELOPE_SIZE]
    last = envelope[1] - 1
    base = 6 + last * 3
    return int.from_bytes(envelope[base + 1 : base + 3], "little")

def instrument_header(data: bytes, index: int) -> bytes:
    ord_num = int.from_bytes(data[0x20:0x22], "little")
    base = 0xC0 + ord_num + index * 4
    offset = int.from_bytes(data[base : base + 4], "little")
    return data[offset : offset + INSTRUMENT_SIZE]

def long_ir(notes: int, ticks_per_row: int = 6) -> MusicIR:
    events: list[Event] = []
    for index in range(notes):
        tick = index * ticks_per_row
        events.append(Event(tick=tick, type="note_on", note=60, instrument=1))
        events.append(Event(tick=tick + ticks_per_row - 1, type="note_off"))
    return MusicIR(
        title="long",
        timing=Timing(ticks_per_row=ticks_per_row),
        instruments=[Instrument(id=1, name="pulse")],
        channels=[Channel(id=0, name="voice1", events=events)],
    )

class SecondLoopTrimTests(unittest.TestCase):
    def test_second_loop_start_finds_trailing_prefix(self) -> None:
        # Varied body, then a copy of the opening. Distinct rows avoid the
        # monotone false-positive guard. Fingerprints are (note, inst) per ch.
        body = [((i % 17, 1),) for i in range(200)]
        overrun = body[:80]
        loop_at = second_loop_start(body + overrun)
        self.assertEqual(loop_at, 200)

    def test_short_opening_reprise_near_end_is_trimmed(self) -> None:
        # Typical HVSC stutter: a few opening notes, then B00 would jump.
        opening = [((50 + i, 1),) for i in range(12)]
        body = opening + [((70 + (i % 9), 1),) for i in range(88)]
        fps = body + opening[:6] + [((None, None),)] * 2
        self.assertEqual(second_loop_start(fps), len(body))

    def test_restart_note_off_vs_blank_opening_still_trims(self) -> None:
        # Tapper #5: first loop opens with a blank on a quiet channel; the
        # HVSC overrun restarts the same notes while that channel still has a
        # note-off from the previous phrase. Fingerprints must treat off as
        # empty or the match never starts.
        opening = [
            ((None, None), (49, 1), (None, None)),
            ((None, None), (None, None), (None, None)),
            ((None, None), (255, None), (29, 1)),
            ((None, None), (None, None), (None, None)),
            ((None, None), (51, 1), (27, 1)),
            ((None, None), (53, 1), (None, None)),
        ]
        middle = [((60 + (i % 11), 1), (40 + (i % 5), 2), (30, 3)) for i in range(40)]
        overrun = [
            ((None, None), (49, 1), (255, None)),
            ((None, None), (None, None), (None, None)),
            ((None, None), (255, None), (29, 1)),
            ((None, None), (None, None), (None, None)),
            ((None, None), (51, 1), (27, 1)),
            ((None, None), (53, 1), (None, None)),
            ((None, None), (255, None), (255, None)),
        ]
        fps = opening + middle + overrun
        self.assertEqual(second_loop_start(fps), len(opening) + len(middle))

    def test_mid_song_intro_motif_is_not_trimmed(self) -> None:
        # Opening returns briefly in the middle, then unique material continues
        # past MAX_LOOP_TAIL_ROWS. That is not an HVSC end overrun.
        opening = [((60 + (i % 5), 1),) for i in range(8)]
        middle = [((70 + (i % 7), 1),) for i in range(56)]
        unique_tail = [((80 + (i % 9), 1),) for i in range(60)]
        fps = opening + middle + opening + unique_tail
        self.assertIsNone(second_loop_start(fps))

    def test_monotone_timeline_is_not_trimmed(self) -> None:
        fps = [((60, 1),) for _ in range(300)]
        self.assertIsNone(second_loop_start(fps))

    def test_repeated_section_with_longer_coda_is_not_trimmed(self) -> None:
        # Paperboy subtune 4: A plays twice, then a different held final chord.
        # Matching A as "second-loop overrun" would drop the real ending.
        a = [((50 + (i % 12), 1), (40 + (i % 5), 2), (30 + (i % 3), 3)) for i in range(48)]
        coda_onset = [((72, 1), (64, 2), (36, 3))]
        coda_hold = [((None, None), (None, None), (None, None))] * 15
        coda_off = [((255, None), (255, None), (255, None))]
        fps = a + a + coda_onset + coda_hold + coda_off
        self.assertIsNone(second_loop_start(fps))

    def test_write_trims_overrun_and_loops_at_cut(self) -> None:
        # One varied phrase, then the same phrase again with a short tail so
        # HVSC-style length past the loop is visible in the order list.
        tpr = 1
        phrase = [60 + (i % 12) for i in range(128)]
        events: list[Event] = []
        for cycle, notes in enumerate((phrase, phrase, phrase[:40])):
            base = cycle * len(phrase) * tpr
            for index, note in enumerate(notes):
                tick = base + index * tpr
                events.append(
                    Event(tick=tick, type="note_on", note=note, instrument=1)
                )
                events.append(Event(tick=tick, type="note_off"))
        ir = MusicIR(
            title="overrun",
            timing=Timing(ticks_per_row=tpr),
            instruments=[Instrument(id=1, name="pulse")],
            channels=[Channel(id=0, name="voice1", events=events)],
        )
        result = write_module(ir)
        self.assertGreater(result.report.loop_trim_rows, 0)
        data = result.data
        ord_num = int.from_bytes(data[0x20:0x22], "little")
        orders = [o for o in data[0xC0 : 0xC0 + ord_num] if o != 0xFF]
        # One full phrase (two 64-row patterns); partial third copy dropped.
        self.assertEqual(orders, [0, 1])
        rows, _, grid, trimmed = _quantize(ir)
        self.assertEqual(rows, 128)
        self.assertGreater(trimmed, 0)
        jumps = [c for c in grid[rows - 1] if c.get("cmd") == IT_EFFECT_B]
        self.assertEqual(len(jumps), 1)

    def test_long_identical_notes_still_dedupe_without_loop_trim(self) -> None:
        # Regression: blank/monotone dumps must not be cut in half.
        result = write_module(long_ir(notes=150))
        self.assertEqual(result.report.loop_trim_rows, 0)
        data = result.data
        ord_num = int.from_bytes(data[0x20:0x22], "little")
        self.assertEqual(list(data[0xC0 : 0xC0 + ord_num]), [0, 0, 1, 0xFF])

class PatternSplitTests(unittest.TestCase):
    def test_three_order_entries_reuse_identical_patterns(self) -> None:
        # 150 identical one-row notes fill two full 64-row blocks that pack
        # the same, then a short tail with B00. Dedup keeps two patterns and
        # lists the first one twice in the order list.
        data = write_it(long_ir(notes=150))
        ord_num = int.from_bytes(data[0x20:0x22], "little")
        pat_num = int.from_bytes(data[0x26:0x28], "little")
        self.assertEqual(pat_num, 2)
        self.assertEqual(ord_num, 4)

        orders = data[0xC0 : 0xC0 + ord_num]
        self.assertEqual(list(orders), [0, 0, 1, 0xFF])

    def test_pattern_offsets_point_at_row_counts(self) -> None:
        data = write_it(long_ir(notes=150))
        pat_num = int.from_bytes(data[0x26:0x28], "little")
        base = pattern_pointer_base(data)
        counts = []
        for index in range(pat_num):
            offset = int.from_bytes(data[base + index * 4 : base + index * 4 + 4], "little")
            counts.append(int.from_bytes(data[offset + 2 : offset + 4], "little"))
        # One shared full block, then a short editable-minimum tail.
        self.assertEqual(counts, [PATTERN_ROWS, MIN_PATTERN_ROWS])

    def test_trailing_pattern_is_padded_to_the_editable_minimum(self) -> None:
        # 65 notes at one row each end 1 row into a second pattern, which
        # Impulse Tracker would refuse to edit at that length.
        data = write_it(long_ir(notes=65))
        base = pattern_pointer_base(data)
        pat_num = int.from_bytes(data[0x26:0x28], "little")
        self.assertEqual(pat_num, 2)
        last = int.from_bytes(data[base + 4 : base + 8], "little")
        self.assertEqual(int.from_bytes(data[last + 2 : last + 4], "little"), 32)

    def test_short_ir_stays_single_pattern(self) -> None:
        data = write_it(long_ir(notes=8))
        self.assertEqual(int.from_bytes(data[0x26:0x28], "little"), 1)

    def test_distinct_blocks_stay_separate_patterns(self) -> None:
        # Different pitches mean the two 64-row halves cannot share a pattern.
        events: list[Event] = []
        for index in range(128):
            tick = index * 6
            note = 60 if index < 64 else 67
            events.append(Event(tick=tick, type="note_on", note=note, instrument=1))
            events.append(Event(tick=tick + 5, type="note_off"))
        ir = MusicIR(
            title="distinct",
            timing=Timing(ticks_per_row=6),
            instruments=[Instrument(id=1, name="pulse")],
            channels=[Channel(id=0, name="voice1", events=events)],
        )
        data = write_it(ir)
        ord_num = int.from_bytes(data[0x20:0x22], "little")
        pat_num = int.from_bytes(data[0x26:0x28], "little")
        self.assertEqual(pat_num, 3)
        self.assertEqual(list(data[0xC0 : 0xC0 + ord_num]), [0, 1, 2, 0xFF])

class LoopTests(unittest.TestCase):
    def test_last_row_jumps_back_to_the_first_order(self) -> None:
        ir = long_ir(notes=8)
        rows, _, grid, _ = _quantize(ir)
        cells = grid[rows - 1]
        jumps = [cell for cell in cells if cell.get("cmd") == IT_EFFECT_B]
        self.assertEqual(len(jumps), 1)
        self.assertEqual(jumps[0]["param"], 0)

    def test_no_jump_before_the_last_row(self) -> None:
        rows, _, grid, _ = _quantize(long_ir(notes=8))
        for row in range(rows - 1):
            for cell in grid[row]:
                self.assertNotEqual(cell.get("cmd"), IT_EFFECT_B)

    def test_sub_row_note_off_rounds_up_instead_of_disappearing(self) -> None:
        ir = MusicIR(
            title="short note",
            timing=Timing(ticks_per_row=6),
            instruments=[Instrument(id=1, name="pulse")],
            channels=[
                Channel(
                    id=0,
                    events=[
                        Event(tick=2, type="note_on", note=60, instrument=1),
                        Event(tick=5, type="note_off"),
                    ],
                )
            ],
        )
        _, _, grid, _ = _quantize(ir)
        self.assertEqual(grid[0][0]["note"], 48)
        self.assertEqual(grid[1][0]["note"], 255)

    def test_near_double_step_keeps_two_rows_of_spacing(self) -> None:
        # Highnoon on an 11-tick CIA grid: a 21-frame gap is almost two steps.
        # Flooring both onsets packed them onto consecutive rows and made the
        # second chord arrive early; nearest-row keeps the two-row rest.
        ir = MusicIR(
            title="cia jitter gap",
            timing=Timing(ticks_per_row=11),
            instruments=[Instrument(id=1, name="pulse")],
            channels=[
                Channel(
                    id=0,
                    events=[
                        Event(tick=869, type="note_on", note=42, instrument=1),
                        Event(tick=890, type="note_on", note=50, instrument=1),
                    ],
                )
            ],
        )
        _, _, grid, _ = _quantize(ir)
        first = next(r for r, cells in enumerate(grid) if cells[0].get("note") == 30)
        second = next(r for r, cells in enumerate(grid) if cells[0].get("note") == 38)
        self.assertEqual(second - first, 2)

    def test_phase_drifted_cia_gaps_keep_spacing_rows(self) -> None:
        # After CIA phase drift, absolute nearest-row maps ticks 6 then 27 to
        # rows 1 then 2 (a 50% cut). Spacing-preserving placement keeps every
        # 21/22-frame hold as two rows.
        tpr = 11
        gaps = [11, 21, 11, 22, 21]
        tick = 6
        events = [Event(tick=tick, type="note_on", note=60, instrument=1)]
        for gap in gaps:
            tick += gap
            events.append(Event(tick=tick, type="note_on", note=60, instrument=1))
        ir = MusicIR(
            title="cia phase drift",
            timing=Timing(ticks_per_row=tpr),
            instruments=[Instrument(id=1, name="pulse")],
            channels=[Channel(id=0, events=events)],
        )
        _, _, grid, _ = _quantize(ir)
        rows = [r for r, cells in enumerate(grid) if cells[0].get("note") is not None]
        self.assertEqual(len(rows), len(gaps) + 1)
        self.assertEqual(
            [later - earlier for earlier, later in zip(rows, rows[1:])],
            [1, 2, 1, 2, 2],
        )

    def test_exact_grid_onsets_stay_on_their_rows(self) -> None:
        # Conventional players on an exact lattice must not shift.
        tpr = 11
        events = [
            Event(tick=index * tpr, type="note_on", note=60, instrument=1)
            for index in range(6)
        ]
        ir = MusicIR(
            title="exact grid",
            timing=Timing(ticks_per_row=tpr),
            instruments=[Instrument(id=1, name="pulse")],
            channels=[Channel(id=0, events=events)],
        )
        _, _, grid, _ = _quantize(ir)
        rows = [r for r, cells in enumerate(grid) if cells[0].get("note") is not None]
        self.assertEqual(rows, list(range(6)))

    def test_one_frame_per_row_doubles_speed_and_tempo_for_legato(self) -> None:
        ir = MusicIR(
            title="fine grid",
            timing=Timing(ticks_per_row=1),
            instruments=[Instrument(id=1, name="pulse")],
            channels=[
                Channel(
                    id=0,
                    events=[
                        Event(tick=0, type="note_on", note=60, instrument=1),
                        Event(tick=1, type="pitch", note=67, instrument=1),
                    ],
                )
            ],
        )
        data = write_it(ir)
        # Speed 2 at tempo 250 is the same 50 Hz row rate as speed 1 at 125,
        # but the row now has a second tick for Gxx.
        self.assertEqual(data[0x32], 2)
        self.assertEqual(data[0x33], 250)

        _, _, grid, _ = _quantize(ir)
        self.assertEqual(grid[1][0]["cmd"], IT_EFFECT_G)
        self.assertNotIn("inst", grid[1][0])

    def test_held_pitch_change_uses_tone_portamento_without_instrument(self) -> None:
        ir = MusicIR(
            title="legato",
            timing=Timing(ticks_per_row=2),
            instruments=[Instrument(id=1, name="pulse")],
            channels=[
                Channel(
                    id=0,
                    events=[
                        Event(tick=0, type="note_on", note=60, instrument=1),
                        Event(tick=2, type="pitch", note=67, instrument=1),
                    ],
                )
            ],
        )
        _, _, grid, _ = _quantize(ir)
        self.assertEqual(grid[1][0]["note"], 55)
        self.assertNotIn("inst", grid[1][0])
        self.assertEqual(grid[1][0]["cmd"], IT_EFFECT_G)
        self.assertEqual(grid[1][0]["param"], 0xFF)

    def test_effect_only_arpeggio_does_not_retrigger_note(self) -> None:
        ir = MusicIR(
            title="continued arp",
            timing=Timing(ticks_per_row=3),
            instruments=[Instrument(id=1, name="pulse")],
            channels=[
                Channel(
                    id=0,
                    events=[
                        Event(
                            tick=0,
                            type="note_on",
                            note=60,
                            instrument=1,
                            arpeggio=[0, 4, 7],
                        ),
                        Event(tick=3, type="effect", arpeggio=[0, 4, 7]),
                    ],
                )
            ],
        )
        _, _, grid, _ = _quantize(ir)
        self.assertNotIn("note", grid[1][0])
        self.assertEqual(grid[1][0]["cmd"], IT_EFFECT_J)
        self.assertEqual(grid[1][0]["param"], 0x47)

class WaveformTests(unittest.TestCase):
    @staticmethod
    def rms(pcm: bytes) -> float:
        signed = [value if value < 128 else value - 256 for value in pcm]
        return (sum(value * value for value in signed) / len(signed)) ** 0.5

    def test_noise_uses_long_lfsr_sample_at_sid_clock_rate(self) -> None:
        pcm, c5_speed = _pcm("noise", 0, 0x81)
        self.assertEqual(len(pcm), NOISE_SAMPLE_LEN)
        self.assertEqual(c5_speed, NOISE_C5_SPEED)
        self.assertGreater(len(set(pcm)), 100)
        self.assertNotEqual(pcm[:32], pcm[32:64])

    def test_triangle_pulse_is_not_replaced_by_plain_pulse(self) -> None:
        combined, combined_speed = _pcm("pulse", 2000, 0x51)
        pulse, pulse_speed = _pcm("pulse", 2000, 0x41)
        self.assertEqual(combined_speed, C5_SPEED)
        self.assertEqual(pulse_speed, C5_SPEED)
        self.assertNotEqual(combined, pulse)

    def test_narrow_pulse_is_quieter_after_dc_removal(self) -> None:
        narrow, _ = _pcm("pulse", 512, 0x41)
        square, _ = _pcm("pulse", 2048, 0x41)
        self.assertLess(self.rms(narrow), self.rms(square))

    def test_noise_is_not_louder_than_square_wave(self) -> None:
        noise, _ = _pcm("noise", 0, 0x81)
        square, _ = _pcm("pulse", 2048, 0x41)
        self.assertLess(self.rms(noise), self.rms(square))

class EnvelopeTests(unittest.TestCase):
    def test_sustain_zero_decays_to_silence(self) -> None:
        # Decay-only plucks (Super Pipeline II): decay 9 is 750 ms, 38 frames.
        nodes, sustain = adsr_envelope_nodes(
            attack=0, decay=9, sustain=0, release=0, frame_hz=50.0
        )
        self.assertEqual(nodes[0], (0, 0))
        self.assertEqual(nodes[1][0], 64)
        self.assertEqual(nodes[sustain], (0, nodes[1][1] + 38))
        self.assertEqual(sustain, len(nodes) - 1)

    def test_decay_follows_the_resid_exponential_breakpoints(self) -> None:
        nodes, sustain = adsr_envelope_nodes(
            attack=0, decay=9, sustain=0, release=0, frame_hz=50.0
        )
        peak, end = nodes[1][1], nodes[sustain][1]
        span = end - peak
        levels = {value: tick for value, tick in nodes[2:sustain]}
        # 93 of 255 is a quarter of the peak but only a fifth of the way
        # through the decay, and the last sixth of the fall takes a quarter of
        # the time. A straight line would put both at their own fraction.
        self.assertIn(23, levels)
        self.assertAlmostEqual((levels[23] - peak) / span, 0.214, delta=0.02)
        self.assertAlmostEqual((levels[2] - peak) / span, 0.762, delta=0.02)

    def test_decay_stops_at_the_sustain_level(self) -> None:
        nodes, sustain = adsr_envelope_nodes(
            attack=0, decay=9, sustain=8, release=9, frame_hz=50.0
        )
        self.assertEqual(nodes[sustain][0], 34)  # 8 * 0x11 of 255, scaled to 64
        self.assertTrue(all(value > 34 for value, _ in nodes[1:sustain]))
        self.assertTrue(all(value < 34 for value, _ in nodes[sustain + 1 :]))
        self.assertEqual(nodes[-1][0], 0)

    def test_sustain_full_holds_at_peak(self) -> None:
        nodes, sustain = adsr_envelope_nodes(
            attack=0, decay=9, sustain=15, release=9, frame_hz=50.0
        )
        # Nothing to decay, so the sustain node sits right after the peak;
        # it cannot share a tick because IT node ticks must increase.
        self.assertEqual(nodes[sustain][0], 64)
        self.assertEqual(nodes[sustain][1], nodes[1][1] + 1)
        self.assertEqual(nodes[-1][0], 0)

    def test_node_ticks_strictly_increase(self) -> None:
        for attack in range(16):
            for sustain in range(16):
                nodes, _ = adsr_envelope_nodes(attack, 9, sustain, 9, frame_hz=50.0)
                ticks = [tick for _, tick in nodes]
                self.assertEqual(ticks, sorted(set(ticks)))
                self.assertLessEqual(len(nodes), 25)

    def test_envelope_ticks_scale_with_a_doubled_tempo(self) -> None:
        # module_timing doubles speed and tempo at one frame per row, so
        # envelope ticks run at 100 Hz and must be counted at that rate.
        slow, _ = adsr_envelope_nodes(9, 9, 8, 9, frame_hz=50.0)
        fast, _ = adsr_envelope_nodes(9, 9, 8, 9, frame_hz=100.0)
        self.assertAlmostEqual(fast[-1][1] / slow[-1][1], 2.0, delta=0.05)

    def test_doubled_module_writes_doubled_envelope_ticks(self) -> None:
        def sustained(ticks_per_row: int) -> MusicIR:
            ir = long_ir(notes=8, ticks_per_row=ticks_per_row)
            ir.instruments[0].attack = 9
            ir.instruments[0].decay = 9
            ir.instruments[0].sustain = 8
            ir.instruments[0].release = 9
            return ir

        plain = write_it(sustained(6))
        fine = write_it(sustained(1))
        self.assertEqual(fine[0x33], plain[0x33] * 2)
        plain_end = envelope_end_tick(instrument_header(plain, 0))
        fine_end = envelope_end_tick(instrument_header(fine, 0))
        self.assertAlmostEqual(fine_end / plain_end, 2.0, delta=0.05)

    def test_instrument_header_carries_sustain_loop(self) -> None:
        ir = long_ir(notes=8)
        ir.instruments[0].sustain = 0
        data = write_it(ir)
        self.assertEqual(int.from_bytes(data[0x22:0x24], "little"), 1)
        self.assertEqual(int.from_bytes(data[0x2C:0x2E], "little") & 0x04, 0x04)

        header = instrument_header(data, 0)
        self.assertEqual(header[0:4], b"IMPI")
        self.assertEqual(header[0x41], 1)  # note 0 maps to sample 1
        envelope = header[0x130 : 0x130 + ENVELOPE_SIZE]
        self.assertEqual(envelope[0] & 0x05, 0x05)  # enabled + sustain loop
        sustain_node = envelope[4]
        self.assertEqual(envelope[5], sustain_node)
        self.assertEqual(sustain_node, envelope[1] - 1)
        self.assertEqual(envelope[6 + sustain_node * 3], 0)  # sustain 0 is silence

    def test_pattern_never_writes_zero_volume(self) -> None:
        ir = long_ir(notes=8)
        ir.instruments[0].sustain = 0
        data = write_it(ir)
        base = pattern_pointer_base(data)
        pat_off = int.from_bytes(data[base : base + 4], "little")
        length = int.from_bytes(data[pat_off : pat_off + 2], "little")
        packed = data[pat_off + 8 : pat_off + 8 + length]

        index = 0
        volumes = []
        while index < len(packed):
            channel_byte = packed[index]
            index += 1
            if channel_byte == 0:
                continue
            mask = packed[index]
            index += 1
            if mask & 1:
                index += 1
            if mask & 2:
                index += 1
            if mask & 4:
                volumes.append(packed[index])
                index += 1
            if mask & 8:
                index += 2
        self.assertTrue(volumes)
        self.assertNotIn(0, volumes)

if __name__ == "__main__":
    unittest.main()
