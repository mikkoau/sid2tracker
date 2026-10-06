"""Run one .sid through dump -> music IR -> .it, naming output per subtune.

An .it module holds a single song, so a multi-subtune .sid becomes one file
per subtune: Hubbard_Rob-IK_plus-3.it is subtune 3 of an HVSC file.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from collections import Counter
from collections.abc import Callable
from pathlib import Path

from sidengine.cpu6502 import CPUError
from sidengine.dump import (
    DEFAULT_INIT_IRQ_CYCLES,
    DigiSkipError,
    lift_to_ir,
    run_sid,
)
from sidengine.header import SidHeaderError, parse_sid_file
from sidengine.ir import IRError, save
from sidengine.songlengths import in_hvsc_tree, song_length

from .write_it import (
    DEFAULT_SAMPLE_BUDGET_MB,
    MAX_PATTERNS,
    PATTERN_ROWS,
    GenerationReport,
    format_generation_report,
    write_module,
)

DEFAULT_SECONDS = 60.0
DEFAULT_MIN_SECONDS = 10.0
DUMP_PROGRESS_SECONDS = 20.0
MAX_GRID = 6
# Above this, a dominant onset gap is musical spacing (Cauldron II ~94/95),
# not a conventional player row. Still used as the row clock so rhythm stays
# honest; the generation log warns instead of inventing a 6-tick grid.
MAX_PLAYER_STEP = 16
# CIA timer jitter turns 2*11 into 21 as often as 22. Exact modulo then misses
# Highnoon (~70% on 11); one-frame slack keeps the 11-tick player row.
GAP_SLACK = 1
# Share of note spacing a grid must explain before it is accepted. Ornaments,
# such as a grace note landing between rows, are allowed to miss it.
GRID_ALIGNMENT = 0.9
MIN_GRID_SAMPLES = 8


def _log(message: str, *, error: bool = False) -> None:
    """Print progress immediately. Success is stdout; skips go to stderr.

    Without an explicit flush, a long dump can hold the success line in the
    stdout buffer while later skip lines on stderr appear first.
    """
    stream = sys.stderr if error else sys.stdout
    print(message, file=stream, flush=True)


def _nearly_multiple(gap: int, step: int, slack: int = GAP_SLACK) -> bool:
    """True when gap is within `slack` frames of a multiple of step."""
    if step <= 0:
        return False
    rem = gap % step
    return rem <= slack or rem >= step - slack


def _gate_lengths(provisional_ir) -> list[int]:
    """Frames from each note_on to its gate fall or the next onset."""
    lengths: list[int] = []
    for channel in provisional_ir.channels:
        onsets = [event.tick for event in channel.events if event.type == "note_on"]
        offs = sorted(
            event.tick for event in channel.events if event.type == "note_off"
        )
        off_index = 0
        for position, start in enumerate(onsets):
            while off_index < len(offs) and offs[off_index] <= start:
                off_index += 1
            end = onsets[position + 1] if position + 1 < len(onsets) else None
            gate = offs[off_index] if off_index < len(offs) else None
            if gate is not None and (end is None or gate <= end):
                lengths.append(gate - start)
            elif end is not None:
                lengths.append(end - start)
    return lengths


def _many_gates_shorter_than(provisional_ir, frames: int) -> bool:
    """True when enough notes are shorter than `frames` to need a finer row."""
    lengths = _gate_lengths(provisional_ir)
    if len(lengths) < MIN_GRID_SAMPLES:
        return False
    short = sum(1 for length in lengths if 0 < length < frames)
    return short / len(lengths) >= 1.0 - GRID_ALIGNMENT


def note_grid(provisional_ir) -> int | None:
    """Estimate the player's row length from the spacing between onsets.

    Spacing rather than absolute tick position, because a tune whose first note
    falls off tick 0 is only phase shifted: flooring every onset moves the whole
    tune by a constant, which is inaudible, and note lengths survive. Testing
    `tick % grid == 0` instead collapses such a tune to one row per frame and
    loses both arpeggio and legato. Last Ninja 2 phrases on 6 and 8 ticks but
    starts off-grid; aligning to tick 0 would collapse it to one tick per row.

    Returns None only when there are too few gaps to judge. Otherwise prefers
    a real observed step (including sparse long rests) over a default of 6.
    """
    gaps: Counter[int] = Counter()
    for channel in provisional_ir.channels:
        # A 1-frame noise click before a pulse body is a new onset, but not a
        # new player row. Counting those 1-tick gaps would collapse Huelsbeck
        # bass to one frame per row.
        ticks = [
            event.tick
            for event in channel.events
            if event.type == "note_on"
            and (getattr(event, "extra", None) or {}).get("onset") != "waveform"
        ]
        gaps.update(
            later - earlier
            for earlier, later in zip(ticks, ticks[1:])
            if later > earlier
        )
    total = sum(gaps.values())
    if total < MIN_GRID_SAMPLES:
        return None
    # Conventional player rows first (6 down to 2).
    chosen: int | None = None
    for candidate in range(MAX_GRID, 1, -1):
        aligned = sum(count for gap, count in gaps.items() if gap % candidate == 0)
        if aligned / total >= GRID_ALIGNMENT:
            chosen = candidate
            break
    if chosen is not None:
        # Prefer the dominant gap when it is a multiple of that fine grid
        # (Paperboy 1: gaps of 8, not half-rows of 4). Do not jump to a long
        # pulse that hides shorter observed pickups (Paperboy 5: 10-frame
        # runs under a 20-frame beat were crushed onto one row).
        mode_gap, _count = gaps.most_common(1)[0]
        if (
            mode_gap > chosen
            and mode_gap <= MAX_PLAYER_STEP
            and mode_gap % chosen == 0
            and not any(
                gap < mode_gap and mode_gap % gap == 0 for gap in gaps
            )
            and sum(count for gap, count in gaps.items() if gap % mode_gap == 0)
            / total
            >= GRID_ALIGNMENT
            # Onset gaps alone are not enough: Action Biker phrases every 12
            # frames but gates many notes for 1 or 9, so a 12-tick row drops
            # those shorts. Keep the fine grid when short gates are common.
            and not _many_gates_shorter_than(provisional_ir, mode_gap)
        ):
            return mode_gap
        return chosen
    # Dominant gap longer than six frames, with one-frame CIA slack. Covers
    # Bagitman/Highnoon (11) and Cauldron-like sparse rests (94/95). Using that
    # gap as the row keeps onsets on the beat instead of inventing a 6-tick grid.
    mode_gap, _count = gaps.most_common(1)[0]
    if mode_gap > MAX_GRID:
        aligned = sum(
            count for gap, count in gaps.items() if _nearly_multiple(gap, mode_gap)
        )
        if aligned / total >= GRID_ALIGNMENT:
            return mode_gap
    # Mixed short gaps that only share divisor 1 (Falcon Patrol II). One tick
    # per row preserves timing; inventing 6 does not.
    return 1


def choose_ticks_per_row(
    dump: dict,
    provisional_ir,
) -> tuple[int, str, str]:
    """Pick the row grid, a short reason, and an optional warning for the log.

    Uses the player's onset spacing even when a voice is filtered. Filter
    motion lands as Zxx on this coarser grid unless --no-use-filter;
    --use-pwm only adds baked samples, not a denser row clock. Eliminator
    wavetable drums need that shared row, not one frame per click.

    The third value is a warning when notes sit far apart (long rests, not a
    conventional player row) or when the estimator had too little data. Empty
    when the choice is routine.
    """
    ticks = int(dump["ticks"])
    capacity = MAX_PATTERNS * PATTERN_ROWS
    minimum = max(1, math.ceil((ticks + 1) / capacity))
    grid = note_grid(provisional_ir)
    if grid is None:
        chosen = max(minimum, MAX_GRID)
        if minimum > MAX_GRID:
            return (
                chosen,
                f"pattern cap needs {minimum}",
                _pattern_cap_warning(None, chosen)
                + "; too few onset gaps to estimate player spacing",
            )
        return (
            chosen,
            "player spacing unknown, 6",
            "too few onset gaps to estimate player spacing; falling back to 6",
        )
    chosen = max(minimum, grid)
    if grid > MAX_PLAYER_STEP:
        reason = (
            f"sparse long gaps {grid}"
            if minimum <= grid
            else f"sparse long gaps {grid}, pattern cap needs {minimum}"
        )
        warning = (
            f"notes are about {grid} frames apart (long rests, not a typical "
            f"player row); using {chosen} ticks/row so timing stays honest "
            f"instead of packing one tick per row or inventing a 6-tick grid"
        )
        if minimum > grid:
            warning = f"{_pattern_cap_warning(grid, chosen)}; {warning}"
        return chosen, reason, warning
    if minimum > grid:
        return (
            chosen,
            f"player spacing {grid}, pattern cap needs {minimum}",
            _pattern_cap_warning(grid, chosen),
        )
    return chosen, "player spacing", ""


def _pattern_cap_warning(wanted: int | None, chosen: int) -> str:
    """Why a coarser grid than the player is a quality loss."""
    cap = f".it cap of {MAX_PATTERNS}x{PATTERN_ROWS}-row patterns"
    coarsen = f"{chosen} frames share a row; use a shorter --seconds to keep the player grid"
    if wanted is None:
        return f"quality reduced: dump exceeds the {cap}, so {coarsen}"
    unit = "frame" if wanted == 1 else "frames"
    return (
        f"quality reduced: player wants {wanted} {unit}/row; dump exceeds "
        f"the {cap}, so {coarsen}"
    )


def automatic_ticks_per_row(dump: dict, provisional_ir) -> int:
    """Row grid from onset spacing."""
    ticks, _reason, _warning = choose_ticks_per_row(dump, provisional_ir)
    return ticks


def video_frame_hz(info: dict) -> float:
    return 59.826 if info["flags"]["clock"] == "NTSC" else 50.125


def play_speed_label(play_hz: float, video_hz: float) -> str | None:
    """`3x speed` when play() is clearly faster than the video frame rate."""
    if play_hz <= 0 or video_hz <= 0:
        return None
    ratio = play_hz / video_hz
    if abs(ratio - 1.0) < 0.08:
        return None
    nearest = round(ratio)
    if nearest >= 2 and abs(ratio - nearest) <= 0.08:
        return f"{nearest}x speed"
    return f"{ratio:.1f}x speed"


def tick_source_text(source: str, play_hz: float, video_hz: float) -> str:
    label = play_speed_label(play_hz, video_hz)
    if label:
        return f"{source}, {label}"
    return source


def frame_rate(info: dict, subtune: int, sid: Path | None = None) -> float:
    """Play calls per second, matching how sid_dump clocks the same file.

    CIA tunes often rewrite timer A in init. When `sid` is given, measure that
    latch; otherwise fall back to the PSID default of 60 Hz.
    """
    if info["song_speed"][str(subtune)] == "cia":
        if sid is not None:
            from sidengine.dump import play_rate_hz

            return play_rate_hz(sid, subtune)
        return 60.0
    return video_frame_hz(info)


def hvsc_song_length(sid: Path, subtune: int) -> float | None:
    """Seconds for this subtune from Songlengths.md5, if the file is in HVSC."""
    if not in_hvsc_tree(sid):
        return None
    return song_length(sid, subtune)


def skip_short_track(
    seconds: float | None,
    min_seconds: float,
    *,
    requested: bool = False,
) -> str | None:
    """Why a subtune is too short to write, or None to convert it.

    `--min-seconds` filters `--all` and the header start song so game
    jingles are not written by default. An explicit `--subtune` is a
    request to convert that song, so the filter does not apply.
    """
    if requested or min_seconds <= 0 or seconds is None:
        return None
    if seconds < min_seconds:
        return f"{seconds:.1f}s is below minimum {min_seconds:.1f}s"
    return None


def resolve_ticks(
    sid: Path,
    info: dict,
    subtune: int,
    ticks: int | None,
    seconds: float | None,
) -> tuple[int, str]:
    """Dump length in ticks, plus where that length came from."""
    hz = frame_rate(info, subtune, sid)
    video = video_frame_hz(info)
    if ticks is not None:
        return ticks, tick_source_text("requested ticks", hz, video)
    if seconds is None:
        seconds = hvsc_song_length(sid, subtune)
        source = "HVSC song length" if seconds else "default"
        seconds = seconds or DEFAULT_SECONDS
    else:
        source = "requested seconds"
    source = tick_source_text(f"{source} {seconds:.1f}s", hz, video)
    return max(1, round(seconds * hz)), source


def hvsc_composer(sid: Path) -> str | None:
    """Parent folder under C64Music/MUSICIANS, which HVSC uses as the composer."""
    parts = [part.lower() for part in sid.parts]
    try:
        root = parts.index("c64music")
    except ValueError:
        return None
    if "musicians" not in parts[root + 1 :]:
        return None
    parent = sid.parent.name
    if not parent or parent.lower() in {"c64music", "musicians"}:
        return None
    return "_".join(parent.split())


def output_stem(sid: Path, subtune: int, songs: int) -> str:
    """`tune` for a single-song .sid, `tune-3` for subtune 3 of a multi.

    HVSC MUSICIANS paths prefix the containing folder: Hubbard_Rob-IK_plus-3.
    GAMES, DEMOs, and other HVSC trees keep the bare stem.
    """
    stem = "_".join(sid.stem.split())
    composer = hvsc_composer(sid)
    if composer:
        stem = f"{composer}-{stem}"
    return stem if songs <= 1 else f"{stem}-{subtune}"


def execution_blocker(info: dict) -> str | None:
    if info["flags"].get("mus_player"):
        return "Compute! MUS payload needs an external player"
    if info.get("second_sid_address") or info.get("third_sid_address"):
        return "2SID/3SID is not converted"
    return None


def needs_irq_harness(info: dict) -> bool:
    """True when dump must emulate VIC/CIA IRQs (every RSID, some PSID)."""
    return info["magic"] != "PSID" or info["play_address"] == 0


def dump_kind(info: dict) -> str:
    """Short label for the dump path, used before a long silent run."""
    if needs_irq_harness(info):
        return "RSID IRQ dump" if info["magic"] == "RSID" else "IRQ dump"
    return "PSID dump"


def dump_progress(
    subtune: int,
    songs: int,
    interval: float = DUMP_PROGRESS_SECONDS,
    clock: Callable[[], float] = time.monotonic,
) -> Callable[[int, int], None]:
    """Log dump ticks every `interval` wall seconds. Short tunes stay quiet."""
    last = clock()

    def report(done: int, total: int) -> None:
        nonlocal last
        now = clock()
        if now - last < interval:
            return
        last = now
        percent = 100 * done // total if total else 100
        _log(
            f"subtune {subtune}/{songs}: {percent}% ({done}/{total} ticks)",
            error=True,
        )

    return report


def convert(
    sid: Path,
    subtune: int,
    songs: int,
    out_dir: Path,
    ticks: int,
    ticks_per_row: int | None,
    keep_json: bool,
    sample_budget_mb: float | None = None,
    use_pwm: bool = False,
    use_filter: bool = True,
    dump: dict | None = None,
    init_cycles_budget: int = DEFAULT_INIT_IRQ_CYCLES,
    on_progress: Callable[[int, int], None] | None = None,
) -> tuple[Path, GenerationReport]:
    if dump is None:
        dump = run_sid(
            sid,
            ticks,
            subtune,
            init_cycles_budget=init_cycles_budget,
            on_progress=on_progress,
        )
    grid_reason = "requested"
    grid_warning = ""
    if ticks_per_row is None:
        # Six ticks is only a classification probe, not an output default.
        # Re-lift with the selected grid so fast pitch cycles and output rows
        # use the same definition of "shorter than one row".
        provisional = lift_to_ir(dump, 6)
        ticks_per_row, grid_reason, grid_warning = choose_ticks_per_row(
            dump, provisional
        )
    ir = lift_to_ir(dump, ticks_per_row)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = output_stem(sid, subtune, songs)
    if keep_json:
        (out_dir / f"{stem}.dump.json").write_text(
            json.dumps(dump, indent=2) + "\n", encoding="utf-8"
        )
        save(ir, out_dir / f"{stem}.ir.json")
    if sample_budget_mb is None:
        sample_budget_mb = DEFAULT_SAMPLE_BUDGET_MB if use_pwm else 0.0
    it_path = out_dir / f"{stem}.it"
    result = write_module(
        ir,
        sample_budget_mb,
        use_pwm=use_pwm,
        use_filter=use_filter,
        grid_reason=grid_reason,
        grid_warning=grid_warning,
    )
    it_path.write_bytes(result.data)
    return it_path, result.report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sid", type=Path)
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path("out"),
        help="directory for .it files (default: out/ under the current directory)",
    )
    parser.add_argument(
        "--ticks",
        type=int,
        help="dump length in frames; default is HVSC Songlengths.md5 when the "
        "path is under C64Music, otherwise 60s",
    )
    parser.add_argument(
        "--seconds", type=float, help="dump length, overrides HVSC when present"
    )
    parser.add_argument(
        "--min-seconds",
        type=float,
        default=DEFAULT_MIN_SECONDS,
        help="skip short subtunes on --all and the default start song "
        "(HVSC length); ignored with --subtune; 0 writes all",
    )
    parser.add_argument(
        "--use-pwm",
        action="store_true",
        help="bake moving pulse-width sweeps into private 22050 Hz samples; "
        "large files on PWM-heavy tunes. Default keeps compact "
        "energy-matched static duty loops",
    )
    parser.add_argument(
        "--use-filter",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="write the SID filter timeline as mid-row Zxx cutoff/resonance "
        "and LP/HP macros on the player row grid (default: on). "
        "Use --no-use-filter to skip filter automation; mute and master "
        "volume still apply",
    )
    parser.add_argument(
        "--ticks-per-row",
        type=int,
        help="override the row grid; default is player onset spacing",
    )
    parser.add_argument("--subtune", type=int, help="default: header start song")
    parser.add_argument("--all", action="store_true", help="convert every subtune")
    parser.add_argument("--json", action="store_true", help="also keep dump and IR JSON")
    parser.add_argument(
        "--sample-budget-mb",
        type=float,
        default=None,
        help=f"ceiling for baked sample data with --use-pwm "
        f"(default {DEFAULT_SAMPLE_BUDGET_MB:g} MB); ignored without --use-pwm",
    )
    parser.add_argument(
        "--init-max-cycles",
        type=int,
        default=DEFAULT_INIT_IRQ_CYCLES,
        help="IRQ-path init cycle budget before digi/speech skip "
        f"(default {DEFAULT_INIT_IRQ_CYCLES})",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="log notes/instruments/effects tallies and other dump details",
    )
    args = parser.parse_args(argv)

    if args.sample_budget_mb is not None and not args.use_pwm:
        _log(
            "warning: --sample-budget-mb is ignored without --use-pwm",
            error=True,
        )

    try:
        info = parse_sid_file(args.sid)
        songs = info["songs"]
        blocker = execution_blocker(info)
        if blocker:
            _log(
                f"skipped {args.sid}: {blocker}; no subtunes were converted",
                error=True,
            )
            return 0
        if args.all:
            subtunes = list(range(1, songs + 1))
        elif args.subtune is not None:
            subtunes = [args.subtune]
        else:
            start = info["start_song"]
            subtunes = [start]
            _log(
                f"using header start song {start}/{songs} "
                f"(pass --subtune N or --all for others)",
                error=True,
            )
    except (OSError, SidHeaderError) as exc:
        _log(f"error: {exc}", error=True)
        return 1

    failed = 0
    for subtune in subtunes:
        known = hvsc_song_length(args.sid, subtune)
        reason = skip_short_track(
            known, args.min_seconds, requested=args.subtune is not None
        )
        if reason:
            _log(
                f"subtune {subtune}/{songs} skipped: {reason} (HVSC song length)",
                error=True,
            )
            continue
        ticks, source = resolve_ticks(args.sid, info, subtune, args.ticks, args.seconds)
        # Long HVSC tracks (Katakis 5:33, Last V8 RSID) sit in convert() with
        # no other output. Announce before dump so that is not mistaken for a
        # hung init.
        _log(
            f"subtune {subtune}/{songs}: {dump_kind(info)}, "
            f"{ticks} ticks ({source})...",
            error=True,
        )
        try:
            path, report = convert(
                args.sid,
                subtune,
                songs,
                args.out_dir,
                ticks,
                args.ticks_per_row,
                args.json,
                args.sample_budget_mb,
                use_pwm=args.use_pwm,
                use_filter=args.use_filter,
                init_cycles_budget=args.init_max_cycles,
                on_progress=dump_progress(subtune, songs),
            )
        except KeyboardInterrupt:
            _log(
                f"subtune {subtune}/{songs} interrupted during dump or .it write",
                error=True,
            )
            return 130
        except (OSError, IRError, SidHeaderError, DigiSkipError, CPUError) as exc:
            # A silent or unsupported subtune must not abandon the rest.
            failed += 1
            _log(f"subtune {subtune}/{songs} skipped: {exc}", error=True)
            continue
        _log(f"subtune {subtune}/{songs} -> {path} ({ticks} ticks, {source})")
        _log(format_generation_report(report, verbose=args.verbose))
    return 1 if failed and failed == len(subtunes) else 0


if __name__ == "__main__":
    raise SystemExit(main())
