"""SID filter timeline mapped onto IT Zxx cutoff and resonance."""

from __future__ import annotations

import unittest

from sidengine.ir import Channel, Event, FilterPoint, Instrument, MusicIR, Timing
from sid2it.write_it import (
    IT_CHANNEL_VOL_FULL,
    IT_CUTOFF_OPEN,
    IT_EFFECT_M,
    IT_EFFECT_V,
    IT_EFFECT_Z,
    IT_FILTER_MODE_HP,
    IT_FILTER_MODE_LP,
    IT_FLAG_EMBED_MIDI,
    IT_FLAG_EXT_FILTER,
    IT_GLOBAL_VOL_FULL,
    IT_RESONANCE_BASE,
    MIDI_CFG_SIZE,
    _edit_history_block,
    _quantize,
    it_cutoff,
    it_filter_mode,
    it_global_volume,
    it_resonance,
    sid_cutoff_hz,
    write_it,
)

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

class CutoffMappingTests(unittest.TestCase):
    def test_closed_filter_clamps_to_zero(self) -> None:
        self.assertEqual(it_cutoff(0), 0)

    def test_open_filter_is_near_the_top(self) -> None:
        self.assertGreater(it_cutoff(2047), 100)

    def test_cutoff_is_monotonic(self) -> None:
        values = [it_cutoff(c) for c in range(0, 2048, 64)]
        self.assertEqual(values, sorted(values))

    def test_8580_opens_far_earlier_than_6581(self) -> None:
        # The 6581 barely opens below register 512 while the 8580 is already
        # a third of the way up, which is most of why the two parts sound
        # different on the same tune.
        self.assertLess(it_cutoff(512, "MOS6581"), 30)
        self.assertGreater(it_cutoff(512, "MOS8580"), 50)

    def test_6581_discontinuity_at_fchi_0x80(self) -> None:
        self.assertAlmostEqual(sid_cutoff_hz(1023, "MOS6581"), 6000, delta=20)
        self.assertAlmostEqual(sid_cutoff_hz(1024, "MOS6581"), 4600, delta=20)
        self.assertLess(it_cutoff(1024, "MOS6581"), it_cutoff(1023, "MOS6581"))

    def test_8580_curve_has_no_discontinuity(self) -> None:
        values = [it_cutoff(c, "MOS8580") for c in range(0, 2048, 8)]
        self.assertEqual(values, sorted(values))

    def test_both_models_span_the_whole_effect_range(self) -> None:
        for model in ("MOS6581", "MOS8580"):
            self.assertEqual(it_cutoff(0, model), 0)
            self.assertEqual(it_cutoff(2047, model), IT_CUTOFF_OPEN)

    def test_unknown_model_is_treated_as_6581(self) -> None:
        self.assertEqual(it_cutoff(600, "UNKNOWN"), it_cutoff(600, "MOS6581"))

    def test_ir_reports_the_header_model(self) -> None:
        self.assertEqual(MusicIR(source={"sid_model": "MOS8580"}).sid_model, "MOS8580")
        self.assertEqual(MusicIR(source={"sid_model": "UNKNOWN"}).sid_model, "MOS6581")
        self.assertEqual(MusicIR().sid_model, "MOS6581")

    def test_upper_range_does_not_saturate(self) -> None:
        # Jimmy sweeps 416-2040; mapping must keep distinct Zxx values, not
        # collapse the top of the range onto a flat Z7F.
        values = {it_cutoff(c) for c in range(416, 2041, 8)}
        self.assertGreater(len(values), 20)
        self.assertLess(it_cutoff(1800), IT_CUTOFF_OPEN)

    def test_resonance_maps_to_full_range(self) -> None:
        self.assertEqual(it_resonance(0), 0)
        self.assertEqual(it_resonance(15), 0x0F)

class FilterAutomationTests(unittest.TestCase):
    def effects(self, grid: list[list[dict]], channel: int) -> list[tuple[int, int]]:
        return [
            (row, cells[channel]["param"])
            for row, cells in enumerate(grid)
            if cells[channel].get("cmd") == IT_EFFECT_Z
        ]

    def test_routed_voice_gets_cutoff_and_resonance(self) -> None:
        points = [
            FilterPoint(tick=0, cutoff=0, resonance=15, mode=["lp"], routing=[1, 0, 0]),
            FilterPoint(tick=12, cutoff=2047, resonance=15, mode=["lp"], routing=[1, 0, 0]),
        ]
        _, _, grid, _ = _quantize(ir_with_filter(points))
        effects = self.effects(grid, 0)
        self.assertEqual(effects[0][1], IT_FILTER_MODE_LP)
        params = [param for _, param in effects]
        self.assertIn(IT_RESONANCE_BASE + 0x0F, params)
        self.assertIn(it_cutoff(2047), params)

    def test_unrouted_voice_is_forced_open(self) -> None:
        points = [
            FilterPoint(tick=0, cutoff=0, resonance=15, mode=["lp"], routing=[1, 0, 0]),
        ]
        _, _, grid, _ = _quantize(ir_with_filter(points))
        params = [param for _, param in self.effects(grid, 1)]
        # Resonance is neutralised first, then the cutoff is opened.
        self.assertEqual(params[0], IT_RESONANCE_BASE)
        self.assertEqual(params[1], IT_CUTOFF_OPEN)

    def test_no_filter_points_writes_no_effects(self) -> None:
        _, _, grid, _ = _quantize(ir_with_filter([]))
        self.assertEqual(self.effects(grid, 0), [])

    def test_unused_filter_writes_no_effects(self) -> None:
        # Master volume only, nothing routed: do not touch the effect column.
        points = [
            FilterPoint(tick=0, cutoff=0, resonance=0, mode=[], routing=[0, 0, 0]),
        ]
        _, _, grid, _ = _quantize(ir_with_filter(points))
        self.assertEqual(self.effects(grid, 0), [])
        self.assertEqual(self.effects(grid, 1), [])

    def test_note_row_repeats_cutoff(self) -> None:
        points = [
            FilterPoint(tick=0, cutoff=800, resonance=0, mode=["lp"], routing=[1, 0, 0]),
        ]
        _, _, grid, _ = _quantize(ir_with_filter(points))
        # Notes land on rows 0 and 2; both carry the cutoff despite no change.
        self.assertEqual(grid[2][0]["cmd"], IT_EFFECT_Z)
        self.assertEqual(grid[2][0]["param"], it_cutoff(800))

    def test_coarse_grid_samples_filter_mid_row(self) -> None:
        # Note restarts while the previous sweep is still closed; the open
        # arrives one frame later. Edge sampling would muffle the whole row.
        points = [
            FilterPoint(tick=0, cutoff=400, resonance=0, mode=["lp"], routing=[1, 0, 0]),
            FilterPoint(tick=1, cutoff=1600, resonance=0, mode=["lp"], routing=[1, 0, 0]),
            FilterPoint(tick=6, cutoff=400, resonance=0, mode=["lp"], routing=[1, 0, 0]),
            FilterPoint(tick=7, cutoff=1600, resonance=0, mode=["lp"], routing=[1, 0, 0]),
        ]
        ir = ir_with_filter(points)
        ir.timing.ticks_per_row = 6
        _, _, grid, _ = _quantize(ir)
        cutoffs = [
            cells[0]["param"]
            for cells in grid
            if cells[0].get("cmd") == IT_EFFECT_Z
            and cells[0]["param"] < IT_RESONANCE_BASE
        ]
        self.assertIn(it_cutoff(1600), cutoffs)
        self.assertNotIn(it_cutoff(400), cutoffs)

class FilterModeTests(unittest.TestCase):
    def test_bandpass_alone_becomes_highpass(self) -> None:
        self.assertEqual(it_filter_mode(["bp"]), IT_FILTER_MODE_HP)

    def test_lowpass_plus_anything_stays_lowpass(self) -> None:
        self.assertEqual(it_filter_mode(["lp", "hp"]), IT_FILTER_MODE_LP)
        self.assertEqual(it_filter_mode(["lp", "bp"]), IT_FILTER_MODE_LP)

    def test_highpass_emits_mode_macro_before_cutoff(self) -> None:
        points = [
            FilterPoint(tick=0, cutoff=800, resonance=0, mode=["hp"], routing=[1, 0, 0]),
        ]
        _, _, grid, _ = _quantize(ir_with_filter(points))
        self.assertEqual(grid[0][0]["cmd"], IT_EFFECT_Z)
        self.assertEqual(grid[0][0]["param"], IT_FILTER_MODE_HP)

    def test_routed_without_mode_mutes_the_voice(self) -> None:
        points = [
            FilterPoint(tick=0, cutoff=800, resonance=0, mode=[], routing=[1, 0, 0]),
            FilterPoint(tick=6, cutoff=800, resonance=0, mode=["lp"], routing=[1, 0, 0]),
        ]
        _, _, grid, _ = _quantize(ir_with_filter(points))
        self.assertEqual(grid[0][0]["cmd"], IT_EFFECT_M)
        self.assertEqual(grid[0][0]["param"], 0)
        self.assertEqual(grid[1][0]["cmd"], IT_EFFECT_M)
        self.assertEqual(grid[1][0]["param"], IT_CHANNEL_VOL_FULL)

    def test_voice3_off_mutes_only_a_dry_voice_three(self) -> None:
        points = [
            FilterPoint(
                tick=0,
                cutoff=800,
                resonance=0,
                mode=["lp"],
                routing=[1, 0, 0],
                voice3_off=True,
            )
        ]
        ir = ir_with_filter(points)
        ir.channels.append(Channel(id=2, name="voice3"))
        _, nch, grid, _ = _quantize(ir)
        self.assertGreaterEqual(nch, 3)
        self.assertEqual(grid[0][2]["cmd"], IT_EFFECT_M)
        self.assertEqual(grid[0][2]["param"], 0)
        self.assertNotEqual(grid[0][0].get("cmd"), IT_EFFECT_M)

    def test_master_volume_uses_global_volume_effect(self) -> None:
        points = [
            FilterPoint(
                tick=0, cutoff=0, resonance=0, mode=[], routing=[0, 0, 0], volume=8
            )
        ]
        ir = ir_with_filter(points)
        _, nch, grid, _ = _quantize(ir)
        # Vxx is global, so it rides an existing voice rather than minting a
        # fourth channel on a three-voice chip.
        self.assertEqual(nch, len(ir.channels))
        volumes = [
            cell["param"]
            for cells in grid
            for cell in cells
            if cell.get("cmd") == IT_EFFECT_V
        ]
        self.assertEqual(volumes, [it_global_volume(8)])
        self.assertEqual(it_global_volume(15), IT_GLOBAL_VOL_FULL)
        self.assertEqual(it_global_volume(0), 0)

    def test_master_volume_stays_on_sid_voices(self) -> None:
        # Every voice routed and swept fills the effect slots. Vxx steals a
        # cutoff Zxx rather than minting a fourth channel.
        points = [
            FilterPoint(
                tick=tick,
                cutoff=400 + tick,
                resonance=0,
                mode=["lp"],
                routing=[1, 1, 1],
                volume=tick % 16,
            )
            for tick in range(0, 24, 6)
        ]
        ir = ir_with_filter(points)
        ir.channels.append(Channel(id=2, name="voice3"))
        _, nch, grid, _ = _quantize(ir)
        self.assertEqual(nch, 3)
        volumes = [
            cell["param"]
            for cells in grid
            for cell in cells[:nch]
            if cell.get("cmd") == IT_EFFECT_V
        ]
        self.assertTrue(volumes)

    def test_filtered_instrument_enables_ifc_and_embeds_mode_macros(self) -> None:
        ir = ir_with_filter(
            [FilterPoint(tick=0, cutoff=400, resonance=8, mode=["lp"], routing=[1, 0, 0])]
        )
        ir.instruments[0].filtered = True
        data = write_it(ir, use_filter=True)
        flags = int.from_bytes(data[0x2C:0x2E], "little")
        special = int.from_bytes(data[0x2E:0x30], "little")
        self.assertEqual(flags & IT_FLAG_EMBED_MIDI, IT_FLAG_EMBED_MIDI)
        self.assertEqual(flags & IT_FLAG_EXT_FILTER, IT_FLAG_EXT_FILTER)
        self.assertEqual(special & 0x08, 0x08)
        ord_num = int.from_bytes(data[0x20:0x22], "little")
        ins_num = int.from_bytes(data[0x22:0x24], "little")
        smp_num = int.from_bytes(data[0x24:0x26], "little")
        pat_num = int.from_bytes(data[0x26:0x28], "little")
        midi_at = (
            0xC0
            + ord_num
            + ins_num * 4
            + smp_num * 4
            + pat_num * 4
            + len(_edit_history_block())
        )
        midi = data[midi_at : midi_at + MIDI_CFG_SIZE]
        self.assertIn(b"F0F00200", midi)
        self.assertIn(b"F0F00210", midi)
        inst_off = int.from_bytes(data[0xC0 + ord_num : 0xC0 + ord_num + 4], "little")
        self.assertEqual(data[inst_off + 0x3A] & 0x80, 0x80)
        self.assertEqual(data[inst_off + 0x3B] & 0x80, 0x80)

if __name__ == "__main__":
    unittest.main()
