"""Static-loop versus baked-note sample selection and its size budget."""

from __future__ import annotations

import unittest
from pathlib import Path

from sidengine.ir import Channel, Event, Instrument, MusicIR, Timing
from sid2it.write_it import (
    BAKED_PITCH_SHARE_SEMITONES,
    BAKED_RATE_HZ,
    MAX_BAKED_SECONDS,
    PW_MOVE_MIN,
    SAMPLE_LEN,
    _bake_base_midi,
    _note_spans,
    _pulse_rms,
    _static_pulse_width,
    midi_to_it_note,
    plan_samples,
    write_it,
)

BUDGET = 4 * 1024 * 1024

def rms(pcm: bytes) -> float:
    """Level of signed 8-bit sample data."""
    signed = [value - 256 if value > 127 else value for value in pcm]
    return (sum(value * value for value in signed) / len(signed)) ** 0.5

def note_ir(
    instrument: Instrument,
    *,
    length: int = 30,
    gate: int | None = None,
    pw_points: list[list[int]] | None = None,
    modulator_ratio: float | None = None,
    repeats: int = 1,
) -> MusicIR:
    """`length` ticks per note; `gate` falls earlier and leaves a release tail."""
    events: list[Event] = []
    for index in range(repeats):
        start = index * (length + 1)
        events.append(
            Event(
                tick=start,
                type="note_on",
                note=60,
                instrument=instrument.id,
                modulator_ratio=modulator_ratio,
                pw_points=[[start + tick, pw] for tick, pw in (pw_points or [])],
                extra={"sid_frequency": 0x1167},
            )
        )
        events.append(Event(tick=start + (length if gate is None else gate), type="note_off"))
    return MusicIR(
        title="one note",
        timing=Timing(ticks_per_row=6, frame_hz=50.0),
        instruments=[instrument],
        channels=[Channel(id=0, name="voice1", events=events)],
    )

def pulse(**kwargs) -> Instrument:
    return Instrument(id=1, name="pulse", waveform="pulse", ctrl=0x41, **kwargs)

def sweep(steps: int, span: int = 1024) -> list[list[int]]:
    return [[tick, 1024 + tick * span // max(1, steps - 1)] for tick in range(steps)]

class StaticSampleTests(unittest.TestCase):
    def test_plain_instrument_gets_one_cycle_and_no_assignment(self) -> None:
        plan = plan_samples(note_ir(pulse(pulse_width=2048)), BUDGET)
        self.assertEqual(len(plan.voices), 1)
        self.assertEqual(len(plan.voices[0].pcm), SAMPLE_LEN)
        self.assertEqual(plan.assign, {})
        self.assertEqual(plan.baked, 0)

    def test_static_pulse_width_does_not_bake(self) -> None:
        points = [[0, 2048], [5, 2048 + PW_MOVE_MIN // 4]]
        plan = plan_samples(note_ir(pulse(pulse_width=2048), pw_points=points), BUDGET)
        self.assertEqual(plan.baked, 0)
        self.assertEqual(len(plan.voices), 1)

class StaticDutyTests(unittest.TestCase):
    """An unbaked sweep still has to sound like the note, not like frame 0."""

    # Eagles opens every lead note on a hard-restart width of 2, then sweeps
    # from 4002 toward a square. Rendering that onset width leaves the jingle
    # near silent against the fatter pulse that follows it.
    EAGLES = [[0, 2]] + [[tick, 4002 - (tick - 1) * 96] for tick in range(1, 21)]

    def plan(self, points: list[list[int]], repeats: int = 1):
        ir = note_ir(
            pulse(pulse_width=points[0][1]), pw_points=points, repeats=repeats
        )
        return plan_samples(ir, BUDGET, bake=False)

    def test_onset_only_duty_is_replaced_by_an_energy_match(self) -> None:
        plan = self.plan(self.EAGLES)
        self.assertEqual(plan.static_duty, 1)
        self.assertEqual(plan.assign[(0, 0)], 2)
        self.assertGreater(rms(plan.voices[1].pcm), 8 * rms(plan.voices[0].pcm))

    def test_matched_duty_carries_the_mean_energy_of_the_sweep(self) -> None:
        frames = [pw for _tick, pw in self.EAGLES]
        matched = _static_pulse_width(frames)
        mean = sum(_pulse_rms(width) for width in frames) / len(frames)
        self.assertAlmostEqual(_pulse_rms(matched), mean, places=2)
        # The note is narrow throughout, so the wide root would change timbre.
        self.assertGreater(matched, 2048)

    def test_steady_duty_keeps_the_instrument_loop(self) -> None:
        plan = self.plan([[tick, 2048] for tick in range(20)])
        self.assertEqual(plan.static_duty, 0)
        self.assertEqual(plan.assign, {})

    def test_notes_sharing_a_duty_bucket_share_one_loop(self) -> None:
        plan = self.plan(self.EAGLES, repeats=4)
        self.assertEqual(plan.static_duty, 1)
        self.assertEqual(len(set(plan.assign.values())), 1)

class ReleaseTailTests(unittest.TestCase):
    """A note keeps sounding after its gate falls, and so does its sweep."""

    # Zardax's lead is gated for two frames at width 0, which is constant DC,
    # and only sweeps toward a real duty during the release.
    RAMP = [[tick, tick * 128] for tick in range(12)]

    def plan(self, **kwargs):
        ir = note_ir(
            pulse(pulse_width=0, release=8),
            length=12,
            gate=2,
            pw_points=self.RAMP,
            repeats=2,
            **kwargs,
        )
        return plan_samples(ir, BUDGET, bake=False)

    def test_silent_gate_duty_is_replaced_from_the_whole_audible_note(self) -> None:
        plan = self.plan()
        self.assertEqual(plan.assign[(0, 0)], 2)
        # Width 0 renders as DC, so the instrument's own loop is dead silent.
        self.assertEqual(rms(plan.voices[0].pcm), 0.0)
        self.assertGreater(rms(plan.voices[1].pcm), 20.0)

    def test_release_stops_where_the_voice_is_reused(self) -> None:
        ir = note_ir(pulse(pulse_width=0, release=8), length=12, gate=2, repeats=2)
        spans = _note_spans(ir.channels[0], {1: ir.instruments[0]}, 50.0)
        # Release 8 is 300 ms, far longer than the 11 frames until the next
        # note, so the tail is cut where the voice is taken over.
        self.assertEqual(spans[0], 13)
        # Nothing follows the second note, so it rings its release out in full.
        self.assertEqual(spans[13], 15 + 15)

class ModulationSampleTests(unittest.TestCase):
    def test_ring_note_gets_its_own_looping_sample(self) -> None:
        instrument = Instrument(id=1, name="tri", waveform="triangle", ctrl=0x15, ring=True)
        plan = plan_samples(note_ir(instrument, modulator_ratio=1.5), BUDGET)
        self.assertEqual(len(plan.voices), 2)
        # A 3/2 ratio only repeats after two carrier cycles.
        self.assertEqual(len(plan.voices[1].pcm), SAMPLE_LEN * 2)
        self.assertEqual(plan.assign[(0, 0)], 2)

    def test_ring_flag_without_a_ratio_stays_static(self) -> None:
        instrument = Instrument(id=1, name="tri", waveform="triangle", ctrl=0x15, ring=True)
        plan = plan_samples(note_ir(instrument), BUDGET)
        self.assertEqual(len(plan.voices), 1)

    def test_ratio_without_sync_or_ring_is_ignored(self) -> None:
        plan = plan_samples(note_ir(pulse(pulse_width=2048), modulator_ratio=1.5), BUDGET)
        self.assertEqual(len(plan.voices), 1)

    def test_same_ratio_twice_shares_one_sample(self) -> None:
        instrument = Instrument(id=1, name="tri", waveform="triangle", ctrl=0x13, sync=True)
        plan = plan_samples(note_ir(instrument, modulator_ratio=2.0, repeats=3), BUDGET)
        self.assertEqual(len(plan.voices), 2)
        self.assertEqual(len(set(plan.assign.values())), 1)

class BakedSampleTests(unittest.TestCase):
    def test_moving_pulse_width_is_baked_at_note_length(self) -> None:
        plan = plan_samples(
            note_ir(pulse(pulse_width=2048), pw_points=sweep(30)), BUDGET
        )
        self.assertEqual(plan.baked, 1)
        self.assertEqual(len(plan.voices), 2)
        # 30 gated frames plus the one frame the fastest release rings for.
        expected = 31 * round(BAKED_RATE_HZ / 50.0)
        self.assertEqual(len(plan.voices[1].pcm), expected)

    def test_baked_sample_loops_only_its_last_cycle(self) -> None:
        plan = plan_samples(
            note_ir(pulse(pulse_width=2048), pw_points=sweep(30)), BUDGET
        )
        voice = plan.voices[1]
        self.assertGreater(voice.loop_start, 0)
        self.assertLess(len(voice.pcm) - voice.loop_start, len(voice.pcm) // 4)

    def test_identical_sweeps_share_one_baked_sample(self) -> None:
        plan = plan_samples(
            note_ir(pulse(pulse_width=2048), pw_points=sweep(30), repeats=4), BUDGET
        )
        self.assertEqual(plan.baked, 1)
        self.assertEqual(len(plan.voices), 2)

    def test_nearby_pitches_share_one_baked_sample(self) -> None:
        events: list[Event] = []
        for index, note in enumerate((60, 61)):
            start = index * 40
            events.append(
                Event(
                    tick=start,
                    type="note_on",
                    note=note,
                    instrument=1,
                    pw_points=[[start + tick, pw] for tick, pw in sweep(30)],
                )
            )
            events.append(Event(tick=start + 30, type="note_off"))
        ir = MusicIR(
            title="pitch share",
            timing=Timing(ticks_per_row=6, frame_hz=50.0),
            instruments=[pulse(pulse_width=2048)],
            channels=[Channel(id=0, name="voice1", events=events)],
        )
        plan = plan_samples(ir, BUDGET)
        self.assertEqual(plan.baked, 1)
        self.assertEqual(plan.assign[(0, 0)], plan.assign[(0, 40)])
        self.assertEqual(_bake_base_midi(60), _bake_base_midi(61))
        self.assertIn("60pwm", plan.voices[1].name)
        data = write_it(ir)
        # Pattern must still carry both pitches even though the bake is shared.
        self.assertIn(bytes([midi_to_it_note(60)]), data)
        self.assertIn(bytes([midi_to_it_note(61)]), data)

    def test_outside_share_band_gets_its_own_bake(self) -> None:
        low, high = 60, 60 + BAKED_PITCH_SHARE_SEMITONES + 1
        events: list[Event] = []
        for index, note in enumerate((low, high)):
            start = index * 40
            events.append(
                Event(
                    tick=start,
                    type="note_on",
                    note=note,
                    instrument=1,
                    pw_points=[[start + tick, pw] for tick, pw in sweep(30)],
                )
            )
            events.append(Event(tick=start + 30, type="note_off"))
        ir = MusicIR(
            title="pitch split",
            timing=Timing(ticks_per_row=6, frame_hz=50.0),
            instruments=[pulse(pulse_width=2048)],
            channels=[Channel(id=0, name="voice1", events=events)],
        )
        plan = plan_samples(ir, BUDGET)
        self.assertEqual(plan.baked, 2)
        self.assertNotEqual(_bake_base_midi(low), _bake_base_midi(high))

    def test_pw_bucket_jitter_shares_one_bake(self) -> None:
        events: list[Event] = []
        for index, offset in enumerate((0, 40)):
            start = index * 40
            # Same 128-wide bucket each frame; offset stays inside the bucket.
            events.append(
                Event(
                    tick=start,
                    type="note_on",
                    note=60,
                    instrument=1,
                    pw_points=[
                        [start + tick, (8 << 7) + 20 + offset + tick * 128]
                        for tick in range(30)
                    ],
                )
            )
            events.append(Event(tick=start + 30, type="note_off"))
        ir = MusicIR(
            title="pw jitter",
            timing=Timing(ticks_per_row=6, frame_hz=50.0),
            instruments=[pulse(pulse_width=2048)],
            channels=[Channel(id=0, name="voice1", events=events)],
        )
        plan = plan_samples(ir, BUDGET)
        self.assertEqual(plan.baked, 1)

    def test_distinct_pw_buckets_get_separate_bakes(self) -> None:
        events: list[Event] = []
        for index, offset in enumerate((0, PW_MOVE_MIN)):
            start = index * 40
            events.append(
                Event(
                    tick=start,
                    type="note_on",
                    note=60,
                    instrument=1,
                    pw_points=[
                        [start + tick, 1024 + offset + tick * 32]
                        for tick in range(30)
                    ],
                )
            )
            events.append(Event(tick=start + 30, type="note_off"))
        ir = MusicIR(
            title="pw split",
            timing=Timing(ticks_per_row=6, frame_hz=50.0),
            instruments=[pulse(pulse_width=2048)],
            channels=[Channel(id=0, name="voice1", events=events)],
        )
        plan = plan_samples(ir, BUDGET)
        self.assertEqual(plan.baked, 2)

    def test_long_note_bakes_only_its_first_seconds(self) -> None:
        length = int(MAX_BAKED_SECONDS * 50) + 20
        plan = plan_samples(
            note_ir(
                pulse(pulse_width=2048),
                length=length,
                pw_points=sweep(length),
            ),
            BUDGET,
        )
        self.assertEqual(plan.baked, 1)
        capped = int(MAX_BAKED_SECONDS * 50) * round(BAKED_RATE_HZ / 50.0)
        self.assertEqual(len(plan.voices[1].pcm), capped)

    def test_noise_is_never_baked(self) -> None:
        instrument = Instrument(id=1, name="noise", waveform="noise", ctrl=0xC1)
        plan = plan_samples(
            note_ir(instrument, pw_points=sweep(30)), BUDGET
        )
        self.assertEqual(plan.baked, 0)

class BudgetTests(unittest.TestCase):
    def test_exhausted_budget_falls_back_to_static_loops(self) -> None:
        ir = note_ir(pulse(pulse_width=2048), pw_points=sweep(30))
        plan = plan_samples(ir, SAMPLE_LEN)
        self.assertEqual(plan.baked, 0)
        self.assertEqual(len(plan.voices[plan.assign[(0, 0)] - 1].pcm), SAMPLE_LEN)
        self.assertTrue(plan.budget_hit)
        self.assertGreater(plan.skipped_budget, 0)

    def test_notes_mode_does_not_bake(self) -> None:
        ir = note_ir(pulse(pulse_width=2048), pw_points=sweep(30))
        plan = plan_samples(ir, BUDGET, bake=False)
        self.assertEqual(plan.baked, 0)
        self.assertEqual(len(plan.voices[plan.assign[(0, 0)] - 1].pcm), SAMPLE_LEN)
        self.assertGreater(plan.pwm_candidates, 0)
        self.assertFalse(plan.budget_hit)

    def test_budget_is_respected_across_many_distinct_sweeps(self) -> None:
        events: list[Event] = []
        for index in range(40):
            start = index * 31
            events.append(
                Event(
                    tick=start,
                    type="note_on",
                    note=60 + index % 12,
                    instrument=1,
                    pw_points=[
                        [start + tick, 512 + index * 32 + tick * 32]
                        for tick in range(30)
                    ],
                    extra={"sid_frequency": 0x1167},
                )
            )
            events.append(Event(tick=start + 30, type="note_off"))
        ir = MusicIR(
            timing=Timing(ticks_per_row=6, frame_hz=50.0),
            instruments=[pulse(pulse_width=2048)],
            channels=[Channel(id=0, name="voice1", events=events)],
        )
        budget = 200_000
        plan = plan_samples(ir, budget)
        self.assertLessEqual(plan.sample_bytes, budget)
        self.assertGreater(plan.baked, 0)
        self.assertLess(plan.baked, 40)

class ModuleIntegrationTests(unittest.TestCase):
    def test_baked_note_reaches_the_pattern_and_the_sample_table(self) -> None:
        data = write_it(
            note_ir(pulse(pulse_width=2048), pw_points=sweep(30)), use_pwm=True
        )
        self.assertEqual(int.from_bytes(data[0x22:0x24], "little"), 2)
        self.assertEqual(int.from_bytes(data[0x24:0x26], "little"), 2)

    def test_static_module_still_has_one_instrument_per_ir_instrument(self) -> None:
        data = write_it(note_ir(pulse(pulse_width=2048)))
        self.assertEqual(int.from_bytes(data[0x22:0x24], "little"), 1)

    def test_identical_noise_pcm_is_shared_across_instruments(self) -> None:
        # Different ADSR keeps two instruments; the LFSR loop is the same bytes.
        ir = MusicIR(
            title="noise share",
            timing=Timing(ticks_per_row=6, frame_hz=50.0),
            instruments=[
                Instrument(
                    id=1, name="hit", waveform="noise", ctrl=0x81, decay=2, sustain=0
                ),
                Instrument(
                    id=2, name="snare", waveform="noise", ctrl=0x81, decay=8, sustain=0
                ),
            ],
            channels=[
                Channel(
                    id=0,
                    events=[
                        Event(tick=0, type="note_on", note=60, instrument=1),
                        Event(tick=6, type="note_off"),
                        Event(tick=12, type="note_on", note=60, instrument=2),
                        Event(tick=18, type="note_off"),
                    ],
                )
            ],
        )
        data = write_it(ir)
        self.assertEqual(int.from_bytes(data[0x22:0x24], "little"), 2)
        self.assertEqual(int.from_bytes(data[0x24:0x26], "little"), 1)
        # Both instruments map every key to sample 1.
        for index in range(2):
            header = _instrument_header_bytes(data, index)
            self.assertEqual(header[0x41], 1)

    def test_same_duty_pulse_instruments_share_one_loop(self) -> None:
        ir = MusicIR(
            title="pulse share",
            timing=Timing(ticks_per_row=6, frame_hz=50.0),
            instruments=[
                Instrument(
                    id=1, name="lead", waveform="pulse", ctrl=0x41, pulse_width=2048
                ),
                Instrument(
                    id=2,
                    name="bass",
                    waveform="pulse",
                    ctrl=0x41,
                    pulse_width=2048,
                    attack=4,
                ),
            ],
            channels=[
                Channel(
                    id=0,
                    events=[
                        Event(tick=0, type="note_on", note=72, instrument=1),
                        Event(tick=6, type="note_off"),
                        Event(tick=12, type="note_on", note=48, instrument=2),
                        Event(tick=18, type="note_off"),
                    ],
                )
            ],
        )
        data = write_it(ir)
        self.assertEqual(int.from_bytes(data[0x22:0x24], "little"), 2)
        self.assertEqual(int.from_bytes(data[0x24:0x26], "little"), 1)

def _instrument_header_bytes(data: bytes, index: int) -> bytes:
    ord_num = int.from_bytes(data[0x20:0x22], "little")
    base = 0xC0 + ord_num + index * 4
    offset = int.from_bytes(data[base : base + 4], "little")
    return data[offset : offset + 554]

if __name__ == "__main__":
    unittest.main()
