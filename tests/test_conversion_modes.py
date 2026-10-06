"""Optional --use-pwm / --use-filter flags and the generation log they print."""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

from sidengine.ir import Channel, Event, FilterPoint, Instrument, MusicIR, Timing, load
from sid2it.write_it import (
    IT_EFFECT_M,
    IT_EFFECT_Z,
    IT_FLAG_EXT_FILTER,
    MAX_IT_INSTRUMENTS,
    SAMPLE_LEN,
    GenerationReport,
    _quantize,
    format_generation_report,
    plan_samples,
    write_it,
    write_module,
)

FIXTURE = ROOT / "tests" / "fixtures" / "scale.ir.json"

def ir_with_filter(points: list[FilterPoint]) -> MusicIR:
    events = [
        Event(tick=0, type="note_on", note=60, instrument=1),
        Event(tick=12, type="note_on", note=62, instrument=1),
    ]
    return MusicIR(
        title="filter",
        timing=Timing(ticks_per_row=6),
        instruments=[Instrument(id=1, name="pulse")],
        channels=[
            Channel(id=0, name="voice1", events=events),
            Channel(id=1, name="voice2"),
        ],
        filter=points,
    )

def pulse(**kwargs) -> Instrument:
    return Instrument(id=1, name="pulse", waveform="pulse", ctrl=0x41, **kwargs)

def sweep(steps: int, span: int = 1024) -> list[list[int]]:
    return [[tick, 1024 + tick * span // max(1, steps - 1)] for tick in range(steps)]

def note_ir(
    instrument: Instrument,
    *,
    length: int = 30,
    pw_points: list[list[int]] | None = None,
) -> MusicIR:
    events = [
        Event(
            tick=0,
            type="note_on",
            note=60,
            instrument=instrument.id,
            pw_points=[[tick, pw] for tick, pw in (pw_points or [])],
            extra={"sid_frequency": 0x1167},
        ),
        Event(tick=length, type="note_off"),
    ]
    return MusicIR(
        title="one note",
        timing=Timing(ticks_per_row=6, frame_hz=50.0),
        instruments=[instrument],
        channels=[Channel(id=0, name="voice1", events=events)],
    )

class OptionalFlagTests(unittest.TestCase):
    def test_default_does_not_bake_pwm(self) -> None:
        ir = note_ir(pulse(pulse_width=2048), pw_points=sweep(30))
        baked = write_module(ir, use_pwm=True)
        compact = write_module(ir)
        self.assertEqual(baked.report.baked, 1)
        self.assertEqual(compact.report.baked, 0)
        self.assertLess(compact.report.size_bytes, baked.report.size_bytes)

    def test_no_use_filter_skips_filter_zxx_and_instrument_filter_flag(self) -> None:
        points = [
            FilterPoint(tick=0, cutoff=400, resonance=8, mode=["lp"], routing=[1, 0, 0]),
        ]
        ir = ir_with_filter(points)
        ir.instruments[0].filtered = True
        data = write_it(ir, use_filter=False)
        flags = int.from_bytes(data[0x2C:0x2E], "little")
        self.assertEqual(flags & IT_FLAG_EXT_FILTER, 0)
        _, _, grid, _ = _quantize(ir, filter_automation=False)
        self.assertFalse(any(cell.get("cmd") == IT_EFFECT_Z for row in grid for cell in row))

    def test_default_writes_filter_on_the_row_grid(self) -> None:
        points = [
            FilterPoint(tick=0, cutoff=400, resonance=8, mode=["lp"], routing=[1, 0, 0]),
        ]
        ir = ir_with_filter(points)
        ir.instruments[0].filtered = True
        result = write_module(ir)
        self.assertTrue(result.report.filter_zxx)
        self.assertFalse(result.report.timing_doubled)
        flags = int.from_bytes(result.data[0x2C:0x2E], "little")
        self.assertEqual(flags & IT_FLAG_EXT_FILTER, IT_FLAG_EXT_FILTER)

    def test_default_still_mutes_a_silent_routed_voice(self) -> None:
        points = [
            FilterPoint(tick=0, cutoff=800, resonance=0, mode=[], routing=[1, 0, 0]),
        ]
        _, _, grid, _ = _quantize(ir_with_filter(points), filter_automation=False)
        self.assertEqual(grid[0][0]["cmd"], IT_EFFECT_M)
        self.assertEqual(grid[0][0]["param"], 0)

class GenerationLogTests(unittest.TestCase):
    def test_minimal_tune_omits_unused_features(self) -> None:
        ir = load(FIXTURE)
        text = format_generation_report(write_module(ir).report)
        self.assertIn("speed 6 tempo 125", text)
        self.assertNotIn("--use-pwm", text)
        self.assertNotIn("--use-filter", text)
        self.assertIn("arpeggio Jxx", text)
        self.assertNotIn("PWM", text)
        self.assertNotIn("filter", text)
        self.assertNotIn("sync/ring", text)
        self.assertNotIn("budget hit", text)
        self.assertNotIn("instrument limit", text)
        self.assertNotIn("legato", text)
        self.assertNotIn("mute", text)
        self.assertNotIn("master volume", text)

    def test_default_writes_filter_and_skips_pwm(self) -> None:
        ir = note_ir(pulse(pulse_width=2048), pw_points=sweep(30))
        ir.filter = [
            FilterPoint(tick=0, cutoff=400, resonance=8, mode=["lp"], routing=[1, 0, 0])
        ]
        ir.instruments[0].filtered = True
        text = format_generation_report(write_module(ir).report)
        self.assertIn("PWM sweeps not baked (pass --use-pwm)", text)
        self.assertIn("filter Zxx (lp, resonance)", text)
        self.assertNotIn("PWM bake", text)
        self.assertNotIn("not written", text)

    def test_no_use_filter_mentions_skip(self) -> None:
        ir = note_ir(pulse(pulse_width=2048), pw_points=sweep(30))
        ir.filter = [
            FilterPoint(tick=0, cutoff=400, resonance=8, mode=["lp"], routing=[1, 0, 0])
        ]
        ir.instruments[0].filtered = True
        text = format_generation_report(
            write_module(ir, use_filter=False).report
        )
        self.assertIn("filter timeline not written (--no-use-filter)", text)
        self.assertNotIn("filter Zxx", text)

    def test_use_pwm_logs_bake_with_default_filter(self) -> None:
        ir = note_ir(pulse(pulse_width=2048), pw_points=sweep(30))
        ir.filter = [
            FilterPoint(tick=0, cutoff=400, resonance=8, mode=["lp"], routing=[1, 0, 0])
        ]
        ir.instruments[0].filtered = True
        text = format_generation_report(
            write_module(ir, use_pwm=True).report
        )
        head = text.splitlines()[0]
        self.assertNotIn("--use-pwm", head)
        self.assertNotIn("--use-filter", head)
        self.assertNotIn("player spacing", head)
        self.assertIn("PWM bake:", text)
        self.assertIn("filter Zxx (lp, resonance)", text)
        self.assertNotIn("not baked", text)
        self.assertNotIn("not written", text)

    def test_summary_omits_routine_grid_reason(self) -> None:
        text = format_generation_report(
            GenerationReport(
                size_bytes=17_000,
                ticks_per_row=9,
                grid_reason="player spacing",
                speed=9,
                tempo=150,
            )
        )
        self.assertEqual(text.splitlines()[0], "  16.6 KB, 9 ticks/row, speed 9 tempo 150")
        unusual = format_generation_report(
            GenerationReport(
                size_bytes=2048,
                ticks_per_row=11,
                grid_reason="pattern cap needs 11",
                speed=11,
                tempo=125,
            )
        )
        self.assertIn("11 ticks/row (pattern cap needs 11)", unusual)
        warned = format_generation_report(
            GenerationReport(
                size_bytes=2_050_000,
                ticks_per_row=2,
                grid_reason="player spacing 1, pattern cap needs 2",
                grid_warning=(
                    "quality reduced: player wants 1 frame/row; dump exceeds "
                    "the .it cap of 200x64-row patterns, so 2 frames share a "
                    "row; use a shorter --seconds to keep the player grid"
                ),
                speed=2,
                tempo=125,
            )
        )
        self.assertIn("2 ticks/row (player spacing 1, pattern cap needs 2)", warned)
        self.assertIn("warning: quality reduced", warned)
        self.assertIn("shorter --seconds", warned)

    def test_summary_notes_when_content_is_shorter_than_dump(self) -> None:
        text = format_generation_report(
            GenerationReport(
                size_bytes=8294,
                ticks_per_row=6,
                speed=6,
                tempo=150,
                dump_ticks=3590,
                content_ticks=700,
                frame_hz=59.826,
            ),
            verbose=False,
        )
        self.assertEqual(
            text.splitlines()[0],
            "  8.1 KB, 6 ticks/row, speed 6 tempo 150 (11.7s of 60.0s)",
        )

    def test_summary_omits_near_full_dump(self) -> None:
        text = format_generation_report(
            GenerationReport(
                size_bytes=17_000,
                ticks_per_row=9,
                grid_reason="player spacing",
                speed=9,
                tempo=150,
                dump_ticks=3000,
                content_ticks=2900,
                frame_hz=50.0,
            )
        )
        self.assertEqual(
            text.splitlines()[0],
            "  16.6 KB, 9 ticks/row, speed 9 tempo 150",
        )

    def test_write_module_uses_last_sid_write_as_content_end(self) -> None:
        ir = note_ir(pulse(), length=3000)
        ir.source = {"ticks": 3000, "last_write_tick": 250}
        report = write_module(ir).report
        self.assertEqual(report.dump_ticks, 3000)
        self.assertEqual(report.content_ticks, 250)
        head = format_generation_report(report, verbose=False).splitlines()[0]
        self.assertIn("5.0s of 60.0s", head)

    def test_budget_hit_is_logged(self) -> None:
        ir = note_ir(pulse(pulse_width=2048), pw_points=sweep(30))
        plan = plan_samples(ir, SAMPLE_LEN, bake=True)
        report = GenerationReport(
            use_pwm=True,
            size_bytes=2048,
            note_ons=1,
            instruments=1,
            patterns=1,
            pwm_in_source=True,
            sample_budget_bytes=SAMPLE_LEN,
            budget_hit=plan.budget_hit,
            skipped_budget=plan.skipped_budget,
        )
        text = format_generation_report(report)
        self.assertIn("sample budget hit", text)
        self.assertIn("fell back to static", text)

    def test_instrument_limit_is_logged(self) -> None:
        report = GenerationReport(
            use_pwm=True,
            size_bytes=50_000,
            instruments=MAX_IT_INSTRUMENTS,
            instrument_limit_hit=True,
            skipped_instrument_limit=12,
        )
        text = format_generation_report(report)
        self.assertIn(
            f"instrument limit hit ({MAX_IT_INSTRUMENTS}/{MAX_IT_INSTRUMENTS})",
            text,
        )
        self.assertIn("12 extra samples fell back to static", text)

if __name__ == "__main__":
    unittest.main()
