"""Oscillator core checked against hand-computed reSID behaviour."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from sid2it.sid_osc import (
    ACC_MASK,
    combined_wave_tables_loaded,
    ratio_cycles,
    render,
    snapped_ratio,
)

FULL_CYCLE = ACC_MASK + 1

def step_for(cycles: int, samples: int) -> int:
    return (FULL_CYCLE * cycles) // samples

def _restarts(values: list[int]) -> int:
    """How many times a rising ramp drops back, i.e. accumulator wraps."""
    return sum(1 for a, b in zip(values, values[1:]) if b < a)

class WaveformShapeTests(unittest.TestCase):
    def test_sawtooth_rises_then_wraps(self) -> None:
        values = render(0x20, 64, step_for(1, 64))
        self.assertEqual(values[0], 64)
        self.assertLess(values[0], values[31])
        self.assertLess(values[31], values[62])
        # The accumulator wraps exactly once, so the last sample restarts low.
        self.assertLess(values[63], values[62])

    def test_triangle_folds_at_the_accumulator_msb(self) -> None:
        values = render(0x10, 64, step_for(1, 64))
        peak = values.index(max(values))
        self.assertGreaterEqual(peak, 30)
        self.assertLessEqual(peak, 33)
        self.assertLessEqual(min(values), 16)

    def test_pulse_duty_follows_pulse_width(self) -> None:
        for width, expected in ((2048, 32), (1024, 48), (3072, 16)):
            values = render(0x40, 64, step_for(1, 64), pulse_width=width)
            self.assertEqual(sum(1 for v in values if v), expected, width)

    def test_pulse_below_the_lowest_accumulator_step_is_always_high(self) -> None:
        values = render(0x40, 32, step_for(1, 32), pulse_width=0)
        self.assertTrue(all(value == 0xFFF for value in values))

    def test_combined_waveform_is_the_bitwise_and(self) -> None:
        with patch("sid2it.sid_osc.table_for", None):
            saw = render(0x20, 32, step_for(1, 32))
            triangle = render(0x10, 32, step_for(1, 32))
            both = render(0x30, 32, step_for(1, 32))
        self.assertEqual(both, [a & b for a, b in zip(triangle, saw)])

    def test_combined_waveform_uses_resid_osc3_tables(self) -> None:
        if not combined_wave_tables_loaded():
            self.skipTest("optional reSID OSC3 tables not generated")
        from sid2it.resid_wave_tables import table_for

        both = render(0x30, 32, step_for(1, 32), chip_model="MOS6581")
        expected = []
        acc = 0
        step = step_for(1, 32)
        table = table_for("MOS6581", "ST")
        for _ in range(32):
            acc = (acc + step) & ACC_MASK
            expected.append(table[acc >> 12] << 4)
        self.assertEqual(both, expected)
        saw = render(0x20, 32, step_for(1, 32))
        triangle = render(0x10, 32, step_for(1, 32))
        self.assertNotEqual(both, [a & b for a, b in zip(triangle, saw)])

    def test_pulse_triangle_masks_table_with_pulse(self) -> None:
        if not combined_wave_tables_loaded():
            self.skipTest("optional reSID OSC3 tables not generated")
        from sid2it.resid_wave_tables import table_for

        pw = 256
        values = render(
            0x50, 64, step_for(1, 64), pulse_width=pw, chip_model="MOS6581"
        )
        table = table_for("MOS6581", "PT")
        acc = 0
        step = step_for(1, 64)
        for index in range(64):
            acc = (acc + step) & ACC_MASK
            tri = ((~acc if acc & 0x800000 else acc) >> 11) & 0xFFF
            pulse = 0xFFF if (acc >> 12) >= pw else 0
            self.assertEqual(values[index], (table[tri >> 1] << 4) & pulse)

    def test_noise_plus_tone_is_silent_with_resid_tables(self) -> None:
        if not combined_wave_tables_loaded():
            self.skipTest("optional reSID OSC3 tables not generated")
        values = render(0x90, 32, step_for(1, 32))
        self.assertTrue(all(value == 0 for value in values))

    def test_6581_and_8580_combined_tables_differ(self) -> None:
        if not combined_wave_tables_loaded():
            self.skipTest("optional reSID OSC3 tables not generated")
        a = render(0x50, 64, step_for(1, 64), pulse_width=2048, chip_model="MOS6581")
        b = render(0x50, 64, step_for(1, 64), pulse_width=2048, chip_model="MOS8580")
        self.assertNotEqual(a, b)

class SyncTests(unittest.TestCase):
    def test_sync_resets_the_carrier_on_the_modulator_msb_rise(self) -> None:
        # Modulator twice the carrier: the sawtooth is forced to restart
        # halfway through what would otherwise be one rising ramp.
        values = render(0x20, 64, step_for(1, 64), ratio=2.0, sync=True)
        self.assertEqual(_restarts(values), 2)
        self.assertLess(values[15], values[14])

    def test_unity_ratio_sync_leaves_one_ramp_per_cycle(self) -> None:
        plain = render(0x20, 64, step_for(1, 64))
        synced = render(0x20, 64, step_for(1, 64), ratio=1.0, sync=True)
        self.assertEqual(_restarts(synced), _restarts(plain))

    def test_sync_only_applies_when_requested(self) -> None:
        self.assertEqual(
            render(0x20, 64, step_for(1, 64)),
            render(0x20, 64, step_for(1, 64), ratio=2.0),
        )

class RingTests(unittest.TestCase):
    def test_ring_inverts_the_triangle_over_the_modulator_half_cycle(self) -> None:
        plain = render(0x10, 64, step_for(1, 64))
        ringed = render(0x10, 64, step_for(1, 64), ratio=1.0, ring=True)
        self.assertNotEqual(plain, ringed)
        # The XOR only changes the folding MSB, so every sample keeps its
        # magnitude about the mid point and is either kept or mirrored.
        for a, b in zip(plain, ringed):
            self.assertIn(b, (a, 0xFFF - a))

    def test_ring_does_not_affect_the_sawtooth(self) -> None:
        self.assertEqual(
            render(0x20, 64, step_for(1, 64)),
            render(0x20, 64, step_for(1, 64), ratio=1.5, ring=True),
        )

    def test_ring_adds_partials_a_plain_triangle_lacks(self) -> None:
        plain = render(0x10, 128, step_for(2, 128))
        ringed = render(0x10, 128, step_for(2, 128), ratio=1.5, ring=True)
        crossings = lambda v: sum(  # noqa: E731
            1 for a, b in zip(v, v[1:]) if (a < 2048) != (b < 2048)
        )
        self.assertGreater(crossings(ringed), crossings(plain))

class PulseWidthModulationTests(unittest.TestCase):
    def test_pw_timeline_advances_once_per_frame(self) -> None:
        # One full oscillator cycle per frame, so each frame's duty is
        # directly countable.
        values = render(
            0x40,
            128,
            step_for(1, 64),
            pw_timeline=[1024, 3072],
            samples_per_frame=64,
        )
        self.assertEqual(sum(1 for v in values[:64] if v), 48)
        self.assertEqual(sum(1 for v in values[64:] if v), 16)

    def test_timeline_holds_its_last_value_past_the_end(self) -> None:
        values = render(
            0x40,
            64,
            step_for(1, 64),
            pw_timeline=[2048],
            samples_per_frame=16,
        )
        self.assertEqual(sum(1 for v in values if v), 32)

class RatioLoopTests(unittest.TestCase):
    def test_integer_ratio_loops_after_one_carrier_period(self) -> None:
        self.assertEqual(ratio_cycles(2.0), 1)
        self.assertEqual(ratio_cycles(3.0), 1)

    def test_fractional_ratio_needs_the_denominator_in_periods(self) -> None:
        self.assertEqual(ratio_cycles(1.5), 2)
        self.assertEqual(ratio_cycles(4 / 3), 3)

    def test_irrational_ratio_is_snapped_within_the_limit(self) -> None:
        self.assertLessEqual(ratio_cycles(1.4142135), 16)
        self.assertAlmostEqual(snapped_ratio(1.5), 1.5)

    def test_absent_ratio_is_a_single_cycle(self) -> None:
        self.assertEqual(ratio_cycles(None), 1)
        self.assertEqual(ratio_cycles(0.0), 1)

class NoiseTests(unittest.TestCase):
    def test_noise_is_deterministic_and_broadband(self) -> None:
        # An odd step so every accumulator bit really toggles. A step that is
        # an exact power of two can leave bit 19 static and freeze the LFSR.
        first = render(0x80, 256, step_for(16, 250))
        second = render(0x80, 256, step_for(16, 250))
        self.assertEqual(first, second)
        self.assertGreater(len(set(first)), 16)

    def test_noise_holds_between_lfsr_clocks(self) -> None:
        # Bit 19 rises 16 times per accumulator cycle, so a cycle rendered at
        # 256 samples repeats each value for about 16 samples.
        values = render(0x80, 256, step_for(1, 256))
        runs = sum(1 for a, b in zip(values, values[1:]) if a != b)
        self.assertLessEqual(runs, 20)

class OversampleTests(unittest.TestCase):
    def test_oversampling_keeps_the_waveform_in_range(self) -> None:
        values = render(0x20, 64, step_for(1, 64), oversample=4)
        self.assertTrue(all(0 <= value <= 0xFFF for value in values))
        self.assertLess(values[0], values[32])

if __name__ == "__main__":
    unittest.main()
