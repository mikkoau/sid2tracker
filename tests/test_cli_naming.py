"""Output naming for single- and multi-subtune SIDs."""

from __future__ import annotations

import unittest
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

from sid2it import cli as sid2it_cli
from sid2it.cli import (
    automatic_ticks_per_row,
    choose_ticks_per_row,
    dump_kind,
    dump_progress,
    execution_blocker,
    hvsc_composer,
    output_stem,
    play_speed_label,
    skip_short_track,
    tick_source_text,
)

class OutputStemTests(unittest.TestCase):
    def test_single_song_has_no_suffix(self) -> None:
        sid = Path("Super_Pipeline_II.sid")
        self.assertEqual(output_stem(sid, subtune=1, songs=1), "Super_Pipeline_II")

    def test_multi_song_gets_subtune_suffix(self) -> None:
        sid = Path("Super_Pipeline_II.sid")
        self.assertEqual(output_stem(sid, subtune=3, songs=6), "Super_Pipeline_II-3")

    def test_spaces_become_underscores(self) -> None:
        sid = PurePosixPath("/hvsc/Commando in the Jungle.sid")
        self.assertEqual(output_stem(sid, subtune=2, songs=4), "Commando_in_the_Jungle-2")

    def test_hvsc_path_prefixes_composer_folder(self) -> None:
        sid = PurePosixPath(
            "/hvsc/C64Music/MUSICIANS/H/Hubbard_Rob/IK_plus.sid"
        )
        self.assertEqual(hvsc_composer(sid), "Hubbard_Rob")
        self.assertEqual(output_stem(sid, subtune=1, songs=3), "Hubbard_Rob-IK_plus-1")

    def test_hvsc_single_song_keeps_composer_without_subtune(self) -> None:
        sid = PurePosixPath(
            "/hvsc/C64Music/MUSICIANS/F/Fitzpatrick_John/Zorro.sid"
        )
        self.assertEqual(
            output_stem(sid, subtune=1, songs=1), "Fitzpatrick_John-Zorro"
        )

    def test_non_hvsc_path_has_no_composer_prefix(self) -> None:
        sid = PurePosixPath("/tmp/IK_plus.sid")
        self.assertIsNone(hvsc_composer(sid))
        self.assertEqual(output_stem(sid, subtune=1, songs=1), "IK_plus")

    def test_hvsc_games_letter_folder_is_not_a_composer(self) -> None:
        sid = PurePosixPath(
            "/hvsc/C64Music/GAMES/S-Z/Up_n_Down.sid"
        )
        self.assertIsNone(hvsc_composer(sid))
        self.assertEqual(output_stem(sid, subtune=1, songs=8), "Up_n_Down-1")

class SkipShortTrackTests(unittest.TestCase):
    def test_default_minimum_skips_jingles(self) -> None:
        self.assertEqual(
            skip_short_track(6.0, 10.0),
            "6.0s is below minimum 10.0s",
        )

    def test_unknown_length_is_kept(self) -> None:
        self.assertIsNone(skip_short_track(None, 10.0))

    def test_zero_minimum_writes_everything(self) -> None:
        self.assertIsNone(skip_short_track(1.0, 0.0))

    def test_exact_minimum_is_kept(self) -> None:
        self.assertIsNone(skip_short_track(10.0, 10.0))

    def test_explicit_subtune_is_not_filtered(self) -> None:
        self.assertIsNone(skip_short_track(9.6, 10.0, requested=True))


class PlaySpeedLabelTests(unittest.TestCase):
    def test_pal_vblank_is_unlabeled(self) -> None:
        self.assertIsNone(play_speed_label(50.125, 50.125))

    def test_cia_triple_is_3x(self) -> None:
        self.assertEqual(play_speed_label(150.375, 50.125), "3x speed")

    def test_cia_double_is_2x(self) -> None:
        self.assertEqual(play_speed_label(100.25, 50.125), "2x speed")

    def test_cia_60_on_pal_is_decimal(self) -> None:
        self.assertEqual(play_speed_label(60.0, 50.125), "1.2x speed")

    def test_tick_source_appends_label(self) -> None:
        self.assertEqual(
            tick_source_text("HVSC song length 76.0s", 150.375, 50.125),
            "HVSC song length 76.0s, 3x speed",
        )
        self.assertEqual(
            tick_source_text("HVSC song length 226.0s", 50.125, 50.125),
            "HVSC song length 226.0s",
        )

class ExecutionBlockerTests(unittest.TestCase):
    def test_caller_driven_psid_is_supported(self) -> None:
        self.assertIsNone(
            execution_blocker(
                {
                    "magic": "PSID",
                    "play_address": 0x1003,
                    "flags": {"mus_player": False},
                    "second_sid_address": None,
                    "third_sid_address": None,
                }
            )
        )

    def test_rsid_is_supported(self) -> None:
        self.assertIsNone(
            execution_blocker(
                {
                    "magic": "RSID",
                    "play_address": 0,
                    "flags": {"mus_player": False},
                    "second_sid_address": None,
                    "third_sid_address": None,
                }
            )
        )

    def test_own_irq_psid_is_supported(self) -> None:
        self.assertIsNone(
            execution_blocker(
                {
                    "magic": "PSID",
                    "play_address": 0,
                    "flags": {"mus_player": False},
                    "second_sid_address": None,
                    "third_sid_address": None,
                }
            )
        )

    def test_dump_kind_names_psid_and_irq_paths(self) -> None:
        self.assertEqual(
            dump_kind({"magic": "PSID", "play_address": 0x6800}),
            "PSID dump",
        )
        self.assertEqual(
            dump_kind({"magic": "RSID", "play_address": 0}),
            "RSID IRQ dump",
        )
        self.assertEqual(
            dump_kind({"magic": "PSID", "play_address": 0}),
            "IRQ dump",
        )

    def test_dump_progress_logs_after_interval(self) -> None:
        now = 0.0

        def clock() -> float:
            return now

        lines: list[str] = []

        def capture(message: str, *, error: bool = False) -> None:
            lines.append(message)

        original = sid2it_cli._log
        sid2it_cli._log = capture
        try:
            report = dump_progress(1, 34, interval=20.0, clock=clock)
            report(100, 1000)
            self.assertEqual(lines, [])
            now = 19.9
            report(400, 1000)
            self.assertEqual(lines, [])
            now = 20.0
            report(500, 1000)
            self.assertEqual(lines, ["subtune 1/34: 50% (500/1000 ticks)"])
            report(600, 1000)
            self.assertEqual(len(lines), 1)
            now = 40.0
            report(800, 1000)
            self.assertEqual(lines[-1], "subtune 1/34: 80% (800/1000 ticks)")
        finally:
            sid2it_cli._log = original

    def test_mus_player_is_blocked(self) -> None:
        blocker = execution_blocker(
            {
                "magic": "PSID",
                "play_address": 0x1000,
                "flags": {"mus_player": True},
                "second_sid_address": None,
                "third_sid_address": None,
            }
        )
        self.assertIn("MUS", blocker)

class AutomaticTimingTests(unittest.TestCase):
    @staticmethod
    def ir(
        ticks: list[int],
        filtered: bool = False,
        extras: list[dict] | None = None,
        gate: int | None = None,
    ) -> SimpleNamespace:
        events = []
        for index, tick in enumerate(ticks):
            extra = extras[index] if extras else {}
            events.append(SimpleNamespace(tick=tick, type="note_on", extra=extra))
            if gate is not None:
                events.append(
                    SimpleNamespace(tick=tick + gate, type="note_off", extra={})
                )
        filters = (
            [SimpleNamespace(mode=["lp"], routing=[1, 0, 0])]
            if filtered
            else []
        )
        return SimpleNamespace(
            channels=[SimpleNamespace(events=events)],
            filter=filters,
        )

    def test_conventional_six_tick_player_keeps_six_tick_rows(self) -> None:
        onsets = [tick * 6 for tick in range(20)]
        self.assertEqual(automatic_ticks_per_row({"ticks": 600}, self.ir(onsets)), 6)

    def test_phase_shifted_player_still_finds_its_own_row(self) -> None:
        # Last Ninja 2 phrases every 6 ticks but starts off tick 0. Judging by
        # absolute position collapsed this to one row per frame.
        onsets = [1 + tick * 6 for tick in range(20)]
        self.assertEqual(automatic_ticks_per_row({"ticks": 600}, self.ir(onsets)), 6)

    def test_ten_tick_player_uses_its_own_row(self) -> None:
        # Super Pipeline II: 10-tick phrases. Prefer that step over algebraic
        # halves (5) or a fake 6 that would put notes off the grid.
        onsets = [tick * 10 for tick in range(20)]
        self.assertEqual(automatic_ticks_per_row({"ticks": 600}, self.ir(onsets)), 10)

    def test_occasional_ornament_does_not_override_the_grid(self) -> None:
        onsets = [tick * 6 for tick in range(20)]
        onsets.append(onsets[-1] + 5)
        self.assertEqual(automatic_ticks_per_row({"ticks": 600}, self.ir(onsets)), 6)

    def test_one_tick_waveform_attack_does_not_collapse_the_grid(self) -> None:
        # Starball-style noise click on the gate frame, pulse on the next.
        onsets: list[int] = []
        extras: list[dict] = []
        for tick in range(0, 120, 6):
            onsets.extend([tick, tick + 1])
            extras.extend([{}, {"onset": "waveform"}])
        self.assertEqual(
            automatic_ticks_per_row({"ticks": 600}, self.ir(onsets, extras=extras)),
            6,
        )

    def test_too_few_onsets_falls_back_to_six(self) -> None:
        dump = {"ticks": 600}
        ir = self.ir([0, 8])
        self.assertEqual(automatic_ticks_per_row(dump, ir), 6)
        _ticks, reason, warning = choose_ticks_per_row(dump, ir)
        self.assertEqual(reason, "player spacing unknown, 6")
        self.assertIn("too few onset gaps", warning)

    def test_filtered_tunes_keep_player_grid(self) -> None:
        # Filter motion must not force one frame per row. Onset spacing is
        # shared whether or not --use-filter is set; the pattern cap is the
        # only reason to coarsen further.
        onsets = [tick * 6 for tick in range(20)]
        ir = self.ir(onsets, filtered=True)
        dump = {"ticks": 25_600}
        self.assertEqual(automatic_ticks_per_row(dump, ir), 6)
        self.assertEqual(
            automatic_ticks_per_row(
                {"ticks": 25_600 * 3}, self.ir(onsets, filtered=True)
            ),
            7,
        )
        _ticks, reason, warning = choose_ticks_per_row(
            {"ticks": 25_600 * 3}, self.ir(onsets, filtered=True)
        )
        self.assertEqual(reason, "player spacing 6, pattern cap needs 7")
        self.assertIn("quality reduced", warning)
        self.assertIn("shorter --seconds", warning)

    def test_one_frame_player_warns_when_pattern_cap_coarsens(self) -> None:
        # Sanxion-length dump: 1-frame spacing cannot fit 200x64-row patterns.
        onsets = list(range(0, 40))
        ir = self.ir(onsets)
        dump = {"ticks": 16_742}
        ticks, reason, warning = choose_ticks_per_row(dump, ir)
        self.assertEqual(ticks, 2)
        self.assertEqual(reason, "player spacing 1, pattern cap needs 2")
        self.assertIn("quality reduced", warning)
        self.assertIn("1 frame/row", warning)
        self.assertIn("200x64-row", warning)
        self.assertIn("shorter --seconds", warning)

    def test_sparse_long_gaps_use_the_musical_step_not_six(self) -> None:
        # Cauldron II subtune 9 (CIA 60 Hz): onsets every 94/95 frames. Use
        # the observed rest as the row clock and warn instead of inventing 6.
        onsets = []
        tick = 0
        for index in range(20):
            onsets.append(tick)
            tick += 94 if index % 4 else 95
        ir = self.ir(onsets)
        dump = {"ticks": 2000}
        self.assertEqual(automatic_ticks_per_row(dump, ir), 94)
        _ticks, reason, warning = choose_ticks_per_row(dump, ir)
        self.assertIn("sparse long gaps 94", reason)
        self.assertIn("about 94 frames apart", warning)
        self.assertIn("long rests", warning)

    def test_mixed_short_gaps_keep_one_tick_rows(self) -> None:
        # Falcon Patrol II: phrases mix 5/6/11/21/43. Nothing in 2..6 reaches
        # 90% alignment, but many gaps are already short, so one tick per row
        # keeps the timing.
        steps = [5, 6, 5, 11, 5, 6, 21, 5, 11, 43, 5, 6, 5, 10, 22, 5, 6, 11]
        onsets = [0]
        for step in steps * 2:
            onsets.append(onsets[-1] + step)
        self.assertEqual(automatic_ticks_per_row({"ticks": 1500}, self.ir(onsets)), 1)

    def test_long_consistent_cia_step_keeps_its_own_row(self) -> None:
        # Bagitman (CIA 60 Hz): onsets every 11 frames, with held notes at
        # 22/33. Nothing in 1..6 divides those gaps, so the 11-tick step is
        # the row.
        onsets = []
        tick = 36
        for index in range(30):
            onsets.append(tick)
            tick += 11 if index % 5 else 22
        self.assertEqual(automatic_ticks_per_row({"ticks": 800}, self.ir(onsets)), 11)

    def test_cia_jitter_around_long_step_still_finds_the_row(self) -> None:
        # Highnoon: dominant step 11, but double-length holds land on 21 as
        # often as 22. Exact modulo only explains ~70%; one-frame slack keeps
        # the 11-tick player row.
        steps = [11, 21, 11, 22, 11, 21, 22, 11, 43, 11, 22, 11, 21, 11, 22]
        onsets = [4]
        for step in steps * 3:
            onsets.append(onsets[-1] + step)
        self.assertEqual(automatic_ticks_per_row({"ticks": 1500}, self.ir(onsets)), 11)

    def test_dense_one_tick_onsets_still_use_one_tick_rows(self) -> None:
        onsets = list(range(0, 40))
        self.assertEqual(automatic_ticks_per_row({"ticks": 600}, self.ir(onsets)), 1)

    def test_dominant_multiple_preferred_when_no_finer_gaps(self) -> None:
        # Paperboy 1: onsets on 8/16/24. Algebraic grid 4 also aligns, but
        # half-rows are noise; prefer the real step.
        steps = [8, 8, 16, 8, 8, 24, 8, 16, 8, 8, 8, 16, 8, 8, 32, 8]
        onsets = [0]
        for step in steps * 4:
            onsets.append(onsets[-1] + step)
        self.assertEqual(automatic_ticks_per_row({"ticks": 2000}, self.ir(onsets)), 8)

    def test_short_gates_block_promoting_to_double_onset_gap(self) -> None:
        # Action Biker: onsets every 12, but most gates are 1 or 9 frames.
        # Promoting 6 -> 12 crushed those shorts onto one row.
        onsets = [tick * 12 for tick in range(40)]
        self.assertEqual(
            automatic_ticks_per_row({"ticks": 2000}, self.ir(onsets, gate=9)),
            6,
        )

    def test_dominant_pulse_does_not_hide_faster_pickups(self) -> None:
        # Paperboy 5: mostly 20-frame steps, but 10-frame runs must keep a
        # finer grid or two notes land on one row and one is dropped.
        steps = [20, 20, 20, 10, 10, 20, 20, 40, 20, 20, 10, 10, 20, 20]
        onsets = [0]
        for step in steps * 8:
            onsets.append(onsets[-1] + step)
        self.assertEqual(automatic_ticks_per_row({"ticks": 3000}, self.ir(onsets)), 5)


if __name__ == "__main__":
    unittest.main()
