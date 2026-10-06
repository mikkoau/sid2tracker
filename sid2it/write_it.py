"""Write an Impulse Tracker .it from a c64-music-ir document."""

from __future__ import annotations

import argparse
import bisect
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sidengine.ir import Channel, IRError, MusicIR, load

from . import __version__
from .sid_osc import ACC_MASK, ratio_cycles, render, snapped_ratio

IT_NOTE_OFF = 255
IT_NOTE_CUT = 254
MIN_PATTERN_ROWS = 32
PATTERN_ROWS = 64
# HVSC song length often runs a few seconds past a return-to-start loop.
# Trim when a copy of the opening reappears near the end. Matches are often
# short (a handful of notes) before B00 cuts back to the real start.
MIN_LOOP_MATCH_ROWS = 4
MIN_LOOP_NOTE_ROWS = 1
MIN_LOOP_DISTINCT_ROWS = 4
# Unique rows after the matched opening (quantize slack only). Larger values
# let a mid-song intro motif look like an end border (Pipeline II #4).
MAX_LOOP_TAIL_ROWS = 32
# Cap how much of the dump may be dropped as overrun (Highnoon needs ~208).
MAX_LOOP_OVERUN_ROWS = 256
MAX_PATTERNS = 200
# Mention dump overrun only when the sounding timeline is clearly shorter
# (default 60s outside HVSC). A few extra HVSC seconds stay quiet.
MIN_SHORT_DUMP_RATIO = 0.9
MIN_SHORT_DUMP_SECONDS = 2.0
C5_HZ = 523.251130601197
# One oscillator cycle per sample loop. 256 points keep pulse duty and the
# triangle fold from quantizing audibly; identical loops share one sample.
SAMPLE_LEN = 256
C5_SPEED = int(round(SAMPLE_LEN * C5_HZ))
ACC_CYCLE = ACC_MASK + 1
# A short static loop is cheap, so smooth its edges rather than alias them.
STATIC_OVERSAMPLE = 4
NOISE_SAMPLE_LEN = 4096
# SID noise clocks its 23-bit LFSR from oscillator accumulator bit 19,
# approximately 16 shifts per oscillator period.
NOISE_C5_SPEED = int(round(16 * C5_HZ))  # 8372

INSTRUMENT_SIZE = 554
ENVELOPE_SIZE = 82
MAX_ENVELOPE_TICK = 9999
# .it numbers instruments and samples in one byte each.
MAX_IT_INSTRUMENTS = 255

# Rate for baked notes, whose sample carries real time rather than one cycle.
BAKED_RATE_HZ = 22050
# Pulse width moving by less than this out of 4096 is driver jitter, not a
# sweep worth spending a private sample on.
PW_MOVE_MIN = 128
# Nearby pitches share one baked sweep and transpose in the tracker. At the
# band edge the PWM rate is off by 2^(n/12); +/-2 st is about 12%, near the
# casual tempo JND, and keeps Action-Biker-scale leads from minting a sample
# per scale degree.
BAKED_PITCH_SHARE_SEMITONES = 2
# A bake covers at most this much of a note and then loops its last cycle.
# Past it the sweep is a pad rather than a lead, and rendering more costs
# sample memory without adding movement anyone hears.
MAX_BAKED_SECONDS = 2.0
# AC RMS, against 0.5 for a square wave, below which a pulse duty is so close
# to DC that its loop is inaudible next to the rest of the tune.
MIN_AUDIBLE_PULSE_RMS = 0.05
DEFAULT_SAMPLE_BUDGET_MB = 4.0
# MOS 6581/8580 datasheet envelope rates, in milliseconds for a full
# 0-to-peak attack. Decay and release cover peak-to-zero and are 3x attack.
ATTACK_MS = (2, 8, 16, 24, 38, 56, 68, 80, 100, 250, 500, 800, 1000, 3000, 5000, 8000)
DECAY_MS = tuple(ms * 3 for ms in ATTACK_MS)


IT_EFFECT_B = 2
IT_EFFECT_G = 7
IT_EFFECT_J = 10
IT_EFFECT_M = 16
IT_EFFECT_V = 22
IT_EFFECT_Z = 26
IT_CUTOFF_OPEN = 0x7F
IT_RESONANCE_BASE = 0x80
IT_FILTER_MODE_LP = 0x90
IT_FILTER_MODE_HP = 0x91
IT_CHANNEL_VOL_FULL = 64
IT_GLOBAL_VOL_FULL = 128
IT_FLAG_STEREO = 0x0001
IT_FLAG_INSTRUMENTS = 0x0004
IT_FLAG_LINEAR = 0x0008
IT_FLAG_EMBED_MIDI = 0x0080
# OpenMPT: doubles the resonant-filter cutoff span (~5 kHz -> ~10 kHz).
IT_FLAG_EXT_FILTER = 0x1000
IT_SPECIAL_SONG_MESSAGE = 0x0001
IT_SPECIAL_EDIT_HISTORY = 0x0002
IT_SPECIAL_EMBED_MIDI = 0x0008
IT_MAX_MESSAGE = 8000
# OpenMPT mix levels: Compatible. Written in the STPM .MMP chunk.
OMPT_MIX_COMPATIBLE = 4
# OpenMPT PlayBehaviour bits from GetSupportedPlaybackBehaviour(IT).
# Indices are fixed in OpenMPT; new flags are only appended.
OMPT_IT_MSF_BITS = (
    0, 7, 8, 9, 10,
    *range(11, 49),
    50, 87, 88, 100, 102, 103, 104, 115, 119, 120,
)
MIDI_MACRO_LEN = 32
MIDI_CFG_SIZE = (9 + 16 + 128) * MIDI_MACRO_LEN

# Measured cutoff curves from reSID (VICE `src/resid/filter.cc`), as
# (register, hertz). The datasheet's "30 Hz to 12 kHz" describes neither part.
# The 6581 is tanh-shaped and barely opens below register 512, and it has a
# real discontinuity where FCHI crosses 0x80: 6 kHz drops back to 4.6 kHz.
# The 8580 is close to linear and stops at 12.5 kHz.
CUTOFF_CURVE_6581 = (
    (0, 220), (128, 230), (256, 250), (384, 300), (512, 420), (640, 780),
    (768, 1600), (832, 2300), (896, 3200), (960, 4300), (992, 5000),
    (1008, 5400), (1016, 5700), (1023, 6000),
    (1024, 4600), (1032, 4800), (1056, 5300), (1088, 6000), (1120, 6600),
    (1152, 7200), (1280, 9500), (1408, 12000), (1536, 14500), (1664, 16000),
    (1792, 17100), (1920, 17700), (2047, 18000),
)
CUTOFF_CURVE_8580 = (
    (0, 0), (128, 800), (256, 1600), (384, 2500), (512, 3300), (640, 4100),
    (768, 4800), (896, 5600), (1024, 6500), (1152, 7500), (1280, 8400),
    (1408, 9200), (1536, 9800), (1664, 10500), (1792, 11000), (1920, 11700),
    (2047, 12500),
)
# Below this the filter is closed as far as anyone can hear, and it keeps the
# 8580 curve's 0 Hz end point out of a logarithm.
CUTOFF_FLOOR_HZ = 30.0


@dataclass
class GenerationReport:
    """How a module was written. Unused features are omitted when formatted."""

    use_pwm: bool = False
    use_filter: bool = False
    ticks_per_row: int = 6
    grid_reason: str = ""
    grid_warning: str = ""
    speed: int = 6
    tempo: int = 125
    timing_doubled: bool = False
    size_bytes: int = 0
    note_ons: int = 0
    instruments: int = 0
    samples: int = 0
    patterns: int = 0
    orders: int = 0
    loop_trim_rows: int = 0
    dump_ticks: int = 0
    content_ticks: int = 0
    frame_hz: float = 50.0
    waveforms: list[str] = field(default_factory=list)
    baked: int = 0
    baked_bytes: int = 0
    pwm_in_source: bool = False
    static_duty: int = 0
    sync_ring: int = 0
    sync_ring_in_source: bool = False
    arpeggio: bool = False
    legato: bool = False
    waveform_switch: bool = False
    filter_in_source: bool = False
    filter_automation: bool = False
    filter_zxx: bool = False
    filter_lp: bool = False
    filter_hp: bool = False
    filter_resonance: bool = False
    mixer_volume: bool = False
    mute: bool = False
    sample_budget_bytes: int = 0
    budget_hit: bool = False
    skipped_budget: int = 0
    instrument_limit_hit: bool = False
    skipped_instrument_limit: int = 0


@dataclass
class ModuleWrite:
    data: bytes
    report: GenerationReport


def _format_size(n: int) -> str:
    if n < 1024:
        return f"{n} bytes"
    kb = n / 1024
    if kb < 1024:
        return f"{kb:.1f} KB"
    return f"{kb / 1024:.2f} MB"


def _seconds(ticks: int, frame_hz: float) -> float:
    hz = frame_hz if frame_hz > 0 else 50.0
    return ticks / hz


def sounding_shorter_than_dump(report: GenerationReport) -> bool:
    """True when notes/writes ended well before the requested dump window."""
    if report.dump_ticks <= 0 or report.content_ticks <= 0:
        return False
    if report.content_ticks >= report.dump_ticks:
        return False
    hz = report.frame_hz if report.frame_hz > 0 else 50.0
    dump_s = _seconds(report.dump_ticks, hz)
    content_s = _seconds(report.content_ticks, hz)
    if dump_s - content_s < MIN_SHORT_DUMP_SECONDS:
        return False
    return report.content_ticks < report.dump_ticks * MIN_SHORT_DUMP_RATIO


def _content_ticks(ir: MusicIR, rows: int, loop_trim_rows: int) -> int:
    last = ir.last_tick
    raw_write = ir.source.get("last_write_tick")
    if raw_write is not None:
        write_tick = int(raw_write)
        if write_tick > 0:
            last = min(last, write_tick) if last else write_tick
    if loop_trim_rows:
        last = min(last, rows * max(1, ir.timing.ticks_per_row))
    return max(0, last)


def format_generation_report(
    report: GenerationReport, *, verbose: bool = True
) -> str:
    """Human log of how the module was built. Skip features the tune did not use.

    The size/ticks/row/timing line always prints. When SID writes or a loop
    trim ended well before the dump window, it also shows written seconds
    versus requested. Routine "player spacing" is omitted; unusual grid
    reasons stay in parentheses. Enabled --use-* flags are not repeated (skipped PWM/filter still say so when verbose). Feature
    tallies (notes, PWM, Jxx, ...) only appear when `verbose` is true.
    Limit/budget hits always print.
    """
    size = _format_size(report.size_bytes)
    unit = "tick/row" if report.ticks_per_row == 1 else "ticks/row"
    grid = f"{report.ticks_per_row} {unit}"
    # "player spacing" is the usual case and already implied by ticks/row.
    reason = report.grid_reason.strip()
    if reason and reason not in ("player spacing", "IR ticks_per_row"):
        grid = f"{grid} ({reason})"
    head = f"  {size}, {grid}, speed {report.speed} tempo {report.tempo}"
    if sounding_shorter_than_dump(report):
        hz = report.frame_hz if report.frame_hz > 0 else 50.0
        written = f"{_seconds(report.content_ticks, hz):.1f}s"
        requested = f"{_seconds(report.dump_ticks, hz):.1f}s"
        head = f"{head} ({written} of {requested})"
    lines = [head]
    if report.timing_doubled:
        lines.append("  doubled speed/tempo so Gxx/Jxy have a spare tick")
    if report.grid_warning:
        lines.append(f"  warning: {report.grid_warning}")
    if verbose:
        inst = f"{report.instruments} instruments"
        if report.waveforms:
            inst = f"{inst} ({', '.join(report.waveforms)})"
        summary = [f"{report.note_ons} notes", inst]
        if report.samples and report.samples != report.instruments:
            summary.append(f"{report.samples} samples")
        summary.append(f"{report.patterns} patterns")
        if report.orders > report.patterns:
            summary.append(f"{report.orders} orders")
        lines.append("  " + ", ".join(summary))
        if report.loop_trim_rows:
            lines.append(
                f"  trimmed {report.loop_trim_rows} rows "
                "(second-loop match at start)"
            )
        if report.baked:
            lines.append(
                f"  PWM bake: {report.baked} samples "
                f"({_format_size(report.baked_bytes)})"
            )
        elif report.pwm_in_source:
            lines.append("  PWM sweeps not baked (pass --use-pwm)")
        if report.static_duty:
            plural = "loop" if report.static_duty == 1 else "loops"
            lines.append(f"  energy-matched duty: {report.static_duty} {plural}")
        if report.sync_ring:
            lines.append(f"  sync/ring loops: {report.sync_ring}")
        elif report.sync_ring_in_source:
            lines.append("  sync/ring present, static waveform used")
        if report.filter_zxx:
            taps: list[str] = []
            if report.filter_lp:
                taps.append("lp")
            if report.filter_hp:
                taps.append("hp")
            if report.filter_resonance:
                taps.append("resonance")
            detail = f" ({', '.join(taps)})" if taps else ""
            lines.append(f"  filter Zxx{detail}")
        elif report.filter_in_source and not report.filter_automation:
            lines.append("  filter timeline not written (--no-use-filter)")
        if report.arpeggio:
            lines.append("  arpeggio Jxx")
        if report.legato:
            lines.append("  legato Gxx")
        if report.waveform_switch:
            lines.append("  waveform-switch onsets")
        if report.mixer_volume:
            lines.append("  master volume Vxx")
        if report.mute:
            lines.append("  channel mute")
    if report.instrument_limit_hit:
        lines.append(
            f"  instrument limit hit ({report.instruments}/{MAX_IT_INSTRUMENTS}); "
            f"{report.skipped_instrument_limit} extra samples fell back to static"
        )
    if report.budget_hit:
        lines.append(
            f"  sample budget hit ({_format_size(report.sample_budget_bytes)}); "
            f"{report.skipped_budget} extra samples fell back to static"
        )
    return "\n".join(lines)


def midi_to_it_note(midi: int) -> int:
    return max(0, min(119, int(midi) - 12))


def _cutoff_curve(model: str) -> tuple[tuple[int, int], ...]:
    return CUTOFF_CURVE_8580 if model == "MOS8580" else CUTOFF_CURVE_6581


def sid_cutoff_hz(cutoff: int, model: str = "MOS6581") -> float:
    """11-bit SID cutoff register as a frequency for the given chip model.

    reSID fits a spline through these points; straight lines between them are
    close enough for choosing a tracker cutoff and cannot overshoot, which
    matters either side of the 6581 discontinuity.
    """
    curve = _cutoff_curve(model)
    value = max(0, min(2047, cutoff))
    for (x0, y0), (x1, y1) in zip(curve, curve[1:]):
        if value <= x1:
            if value <= x0:
                return float(y0)
            return y0 + (y1 - y0) * (value - x0) / (x1 - x0)
    return float(curve[-1][1])


def it_cutoff(cutoff: int, model: str = "MOS6581") -> int:
    """SID cutoff to an IT Zxx cutoff 0-127, log scaled over the chip's range.

    Matching absolute frequency does not work: IT's filter reaches about
    10 kHz at Z7F even with the extended range, while a 6581 goes to 18 kHz,
    so the top of every sweep would saturate. Jimmy sweeps 416-2040 and
    collapsed to a flat Z7F that way. Spreading each model's own range over
    0-127 instead keeps the movement and keeps the two models distinguishable:
    the 6581 stays nearly shut until register 512 where the 8580 is already a
    third of the way open.
    """
    curve = _cutoff_curve(model)
    low = max(float(curve[0][1]), CUTOFF_FLOOR_HZ)
    high = float(curve[-1][1])
    hz = max(low, sid_cutoff_hz(cutoff, model))
    octaves = math.log2(hz / low)
    span = math.log2(high / low)
    return max(0, min(IT_CUTOFF_OPEN, round(IT_CUTOFF_OPEN * octaves / span)))


def it_resonance(resonance: int) -> int:
    """SID resonance 0-15 to the default IT fixed macros Z80-Z8F."""
    return max(0, min(15, resonance))


def it_filter_mode(mode: list[str]) -> int | None:
    """SID filter taps to an OpenMPT filter-mode macro, or None if dry/silent.

    OpenMPT's resonant filter is 12 dB/oct like the SID's LP and HP outputs, but
    it has no band-pass tap and cannot mix taps. Mapping from the 6581 datasheet
    (LP full-bodied, HP tinny, BP thin/open, LP+HP notch):

    | SID taps | IT |
    | -------- | -- |
    | lp, lp+bp, lp+hp, all | low-pass (`Z90`) |
    | hp, hp+bp, bp | high-pass (`Z91`) |

    Band-pass alone becomes high-pass because both are described as thin/open.
    Notch (LP+HP) stays low-pass: dropping the bass is worse than losing the
    notch dip. Combined taps that include LP keep LP for the same reason.
    """
    taps = {tap for tap in mode if tap in ("lp", "bp", "hp")}
    if not taps:
        return None
    if "lp" in taps:
        return IT_FILTER_MODE_LP
    return IT_FILTER_MODE_HP


def it_global_volume(volume: int) -> int:
    """SID 4-bit master volume to IT Vxx (0-128)."""
    return max(0, min(IT_GLOBAL_VOL_FULL, round((volume & 15) * IT_GLOBAL_VOL_FULL / 15)))


def _voice_path(state, channel: int) -> str:
    """`filter`, `dry`, or `mute` for one SID voice at this mixer state.

    A voice routed into the filter with no LP/BP/HP bits selected is removed
    from the dry mix and has no filter output, so it is silent (8580; 6581
    only leaks). Voice 3 Off disconnects voice 3 from the dry path only; a
    filtered voice 3 stays audible. Datasheet: MOS 6581 $18 3OFF note.
    """
    routed = channel < len(state.routing) and bool(state.routing[channel])
    if channel == 2 and state.voice3_off and not routed:
        return "mute"
    if routed and not state.mode:
        return "mute"
    if routed:
        return "filter"
    return "dry"


def _chip_filter_used(ir: MusicIR) -> bool:
    return any(any(point.routing) and point.mode for point in ir.filter)


def _mixer_used(ir: MusicIR) -> bool:
    return any(point.volume != 15 or point.voice3_off for point in ir.filter)


def _macro_bytes(text: str) -> bytes:
    raw = text.encode("ascii")[: MIDI_MACRO_LEN - 1]
    return raw + b"\x00" * (MIDI_MACRO_LEN - len(raw))


def midi_config() -> bytes:
    """IT MIDI macros with OpenMPT filter-mode slots on Z90/Z91.

    Layout is 9 global + 16 parametered + 128 fixed, 32 bytes each (OpenMPT
    wiki, Development: Formats/IT). SF0 is cutoff (`F0F000z`). Z80-Z8F are
    resonance. Z90/Z91 switch low-pass / high-pass (`F0F00200` / `F0F00210`).
    """
    global_macros = ["FF", "FC", "", "9c n v", "9c n 0", "", "", "Bc 0 a 20 b", "Cc p"]
    parametered = ["F0F000z"] + [""] * 15
    fixed = [f"F0F001{index * 8:02X}" for index in range(16)]
    fixed.append("F0F00200")
    fixed.append("F0F00210")
    fixed.extend([""] * (128 - len(fixed)))
    blob = b"".join(_macro_bytes(text) for text in global_macros + parametered + fixed)
    if len(blob) != MIDI_CFG_SIZE:
        raise IRError(f"MIDI config is {len(blob)} bytes, expected {MIDI_CFG_SIZE}")
    return blob


def _note_velocity() -> int:
    """Notes are struck at full level; the ADSR envelope shapes the rest."""
    return 64


def _envelope_frames(milliseconds: int, frame_hz: float) -> int:
    return max(1, round(milliseconds * frame_hz / 1000.0))


def _decay_curve() -> tuple[tuple[int, float], ...]:
    """Normalized shape of a full SID decay, from the reSID rate divisors.

    Attack is a straight ramp, but decay and release slow down in steps: the
    rate counter is divided by 1, 2, 4, 8, 16 and 30 as the envelope passes
    255, 93, 54, 26, 14 and 6. That is what makes a SID pluck fall away fast
    and then hang, and a straight line does not sound like it.
    """
    periods = 0
    points: list[tuple[int, int]] = [(255, 0)]
    for (level, divisor), (next_level, _) in zip(
        EXP_BREAKPOINTS, EXP_BREAKPOINTS[1:]
    ):
        periods += (level - next_level) * divisor
        points.append((next_level, periods))
    return tuple((level, elapsed / periods) for level, elapsed in points)


EXP_BREAKPOINTS = ((255, 1), (93, 2), (54, 4), (26, 8), (14, 16), (6, 30), (0, 1))
DECAY_CURVE = _decay_curve()


def _curve_fraction(level: int) -> float:
    """Where on a full decay the envelope passes `level`, as 0.0-1.0."""
    for (high, start), (low, end) in zip(DECAY_CURVE, DECAY_CURVE[1:]):
        if level >= high:
            return start
        if level > low:
            return start + (end - start) * (high - level) / (high - low)
    return 1.0


def _it_volume(level: int) -> int:
    return max(0, min(64, round(level * 64 / 255)))


def adsr_envelope_nodes(
    attack: int, decay: int, sustain: int, release: int, frame_hz: float
) -> tuple[list[tuple[int, int]], int]:
    """ADSR as (volume 0-64, tick) nodes plus the index of the sustain node.

    `frame_hz` is the rate envelope ticks actually run at, which is the IT
    tick rate rather than the SID frame rate whenever `module_timing` doubles
    speed and tempo.
    """
    peak_tick = _envelope_frames(ATTACK_MS[attack & 15], frame_hz)
    decay_full = _envelope_frames(DECAY_MS[decay & 15], frame_hz)
    release_full = _envelope_frames(DECAY_MS[release & 15], frame_hz)
    sustain_level = (sustain & 15) * 0x11
    reached = _curve_fraction(sustain_level)

    nodes = [(0, 0), (64, peak_tick)]
    for level, fraction in DECAY_CURVE:
        if level >= 255 or level <= sustain_level:
            continue
        nodes.append((_it_volume(level), peak_tick + round(decay_full * fraction)))
    sustain_tick = peak_tick + round(decay_full * reached)
    nodes.append((_it_volume(sustain_level), sustain_tick))
    sustain_node = len(nodes) - 1
    for level, fraction in DECAY_CURVE:
        if level >= sustain_level:
            continue
        nodes.append(
            (_it_volume(level), sustain_tick + round(release_full * (fraction - reached)))
        )
    if nodes[-1][0] != 0:
        nodes.append((0, nodes[-1][1] + 1))

    # IT requires strictly increasing, in-range node ticks.
    previous = -1
    fixed: list[tuple[int, int]] = []
    for value, tick in nodes:
        tick = min(MAX_ENVELOPE_TICK, max(tick, previous + 1))
        previous = tick
        fixed.append((max(0, min(64, value)), tick))
    return fixed, sustain_node


def _envelope(nodes: list[tuple[int, int]], sustain_node: int = 2) -> bytes:
    data = bytearray(ENVELOPE_SIZE)
    data[0] = 0x01 | 0x04  # enabled + sustain loop
    data[1] = len(nodes)
    data[4] = sustain_node
    data[5] = sustain_node
    for index, (value, tick) in enumerate(nodes):
        base = 6 + index * 3
        data[base] = value
        data[base + 1 : base + 3] = tick.to_bytes(2, "little")
    return bytes(data)


def _instrument_header(
    name: str, sample_number: int, envelope: bytes, filtered: bool = False
) -> bytes:
    data = bytearray(INSTRUMENT_SIZE)
    data[0:4] = b"IMPI"
    data[4:16] = _pad(name[:11], 12)
    data[0x11] = 0  # NNA: cut, matching a SID voice steal
    data[0x17] = 60  # pitch-pan centre
    data[0x18] = 128  # global volume
    data[0x19] = 128  # default pan unused (bit 7 = ignore)
    data[0x1C:0x1E] = (0x0214).to_bytes(2, "little")
    data[0x1E] = 1  # one sample
    data[0x20:0x3A] = _pad(name, 26)
    # ITTECH: 0x3A IFC, 0x3B IFR. Bit 7 enables the resonant filter. Filtered
    # instruments start wide open so the first note is not muffled before Zxx.
    # Unfiltered instruments leave the filter off, which is a true bypass;
    # Z7F is still a 12 dB/oct low-pass, not a dry SID voice.
    if filtered:
        data[0x3A] = 0x80 | IT_CUTOFF_OPEN
        data[0x3B] = 0x80
    for note in range(120):
        base = 0x40 + note * 2
        data[base] = note
        data[base + 1] = sample_number
    data[0x130 : 0x130 + ENVELOPE_SIZE] = envelope
    data[0x182] = 0  # panning envelope off
    data[0x1D4] = 0  # pitch envelope off
    return bytes(data)


def _pad(text: str, size: int) -> bytes:
    raw = text.encode("ascii", errors="replace")[: size - 1]
    return raw + b"\x00" * (size - len(raw))


def _source_int(source: dict[str, Any], key: str) -> int | None:
    raw = source.get(key)
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def it_song_title(ir: MusicIR, *, fallback: str = "", max_len: int | None = None) -> str:
    """SID name, plus (n/m) when the file has more than one subtune.

    Impulse Tracker song names are 25 characters plus a NUL. Pass max_len=25
    for the header so a long SID name is trimmed and the suffix still fits.
    The song message keeps the full string.
    """
    base = str(ir.title or "").strip() or fallback
    songs = _source_int(ir.source, "songs")
    subtune = _source_int(ir.source, "subtune")
    if songs is None or songs <= 1 or subtune is None:
        text = base
        suffix = ""
    else:
        suffix = f" ({subtune}/{songs})"
        text = f"{base}{suffix}" if base else suffix.strip()
    if max_len is None or len(text) <= max_len:
        return text
    if not suffix:
        return text[:max_len].rstrip()
    room = max_len - len(suffix)
    if room <= 0:
        return suffix.strip()[:max_len]
    trimmed = (str(ir.title or "").strip() or fallback)[:room].rstrip()
    if not trimmed:
        return suffix.strip()[:max_len]
    return trimmed + suffix


def song_message_text(ir: MusicIR) -> str:
    """SID name/author/released, then a SID2Tracker converter line."""
    credits: list[str] = []
    for value in (
        it_song_title(ir),
        ir.source.get("author"),
        ir.source.get("released"),
    ):
        text = str(value or "").strip()
        if text:
            credits.append(text)
    footer = f"Converted with SID2Tracker {__version__}"
    if credits:
        return "\n".join(credits) + "\n\n" + footer
    return footer


def _encode_song_message(text: str) -> bytes:
    body = text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r")
    payload = body.encode("cp1252", errors="replace") + b"\x00"
    if len(payload) > IT_MAX_MESSAGE:
        payload = payload[: IT_MAX_MESSAGE - 1] + b"\x00"
    return payload


def _edit_history_block() -> bytes:
    """One empty IT 2.08+ history slot.

    OpenMPT labels cwtv 0x0208-0x0214 as Unknown when this block is missing.
    """
    return (1).to_bytes(2, "little") + bytes(8)


def _stpm_chunk(code: bytes, payload: bytes) -> bytes:
    if len(code) != 4:
        raise ValueError(code)
    if len(payload) > 0xFFFF:
        payload = payload[:0xFFFF]
    return code + len(payload).to_bytes(2, "little") + payload


def _it_compat_msf() -> bytes:
    last = max(OMPT_IT_MSF_BITS)
    n = (last + 8) // 8
    bits = bytearray(n)
    for index in OMPT_IT_MSF_BITS:
        bits[index >> 3] |= 1 << (index & 7)
    return bytes(bits)


def _openmpt_song_extensions(artist: str) -> bytes:
    """STPM tail so OpenMPT fills Artist without switching to MPT mix defaults."""
    artist = artist.strip()
    if not artist:
        return b""
    chunks = bytearray(b"STPM")
    chunks.extend(_stpm_chunk(b".MMP", OMPT_MIX_COMPATIBLE.to_bytes(4, "little")))
    chunks.extend(_stpm_chunk(b".FSM", _it_compat_msf()))
    chunks.extend(_stpm_chunk(b"AUTH", artist.encode("utf-8")))
    return bytes(chunks)


def _dac_pcm(values: list[int]) -> bytes:
    """AC-couple ideal 12-bit DAC values into signed 8-bit PCM.

    Subtracting each waveform's mean models the output coupling capacitor and
    removes pulse-width-dependent DC. Scaling against the full DAC range keeps
    a narrow pulse naturally quieter than a square wave instead of normalizing
    both to the same RMS.
    """
    mean = sum(values) / len(values)
    out = bytearray(len(values))
    for index, value in enumerate(values):
        sample = round((value - mean) * 127 / 4095)
        out[index] = max(-128, min(127, sample)) & 0xFF
    return bytes(out)


def _noise_pcm() -> bytes:
    """Deterministic SID-style 23-bit LFSR noise, long enough not to buzz."""
    shift = 0x7FFFFF
    values = []
    output_bits = (22, 20, 16, 13, 11, 7, 4, 2)
    for _ in range(NOISE_SAMPLE_LEN):
        value = sum(((shift >> bit) & 1) << (7 - n) for n, bit in enumerate(output_bits))
        values.append(round(value * 4095 / 255))
        feedback = ((shift >> 22) ^ (shift >> 17)) & 1
        shift = ((shift << 1) & 0x7FFFFF) | feedback
    return _dac_pcm(values)


def _wave_bits(waveform: str, ctrl: int | None) -> int:
    if ctrl is not None:
        return ctrl & 0xF0
    return {
        "triangle": 0x10,
        "saw": 0x20,
        "pulse": 0x40,
        "noise": 0x80,
    }.get(waveform, 0x10)


def _pcm(
    waveform: str,
    pulse_width: int,
    ctrl: int | None = None,
    ratio: float | None = None,
    sync: bool = False,
    ring: bool = False,
    chip_model: str | None = None,
) -> tuple[bytes, int]:
    """Render a looping sample of one steady oscillator setting.

    Without sync or ring that is a single cycle. With them the pair only
    repeats after `ratio_cycles` carrier cycles, so the loop is that long; each
    cycle is still SAMPLE_LEN points, which is why C5 speed does not change.
    """
    wave_bits = _wave_bits(waveform, ctrl)
    if wave_bits & 0x80:
        # Noise clocks from the accumulator rather than tracking it, so it gets
        # its own long sample instead of a one-cycle loop.
        return _noise_pcm(), NOISE_C5_SPEED
    modulating = bool(ratio) and (sync or ring)
    cycles = ratio_cycles(ratio) if modulating else 1
    values = render(
        wave_bits,
        SAMPLE_LEN * cycles,
        ACC_CYCLE // SAMPLE_LEN,
        pulse_width=pulse_width,
        ratio=snapped_ratio(ratio) if modulating else None,
        sync=sync,
        ring=ring,
        oversample=STATIC_OVERSAMPLE,
        chip_model=chip_model,
    )
    return _dac_pcm(values), C5_SPEED


def _bake_base_midi(note: int) -> int:
    """Snap a MIDI note to the center of a pitch-share band.

    Band width is `2 * BAKED_PITCH_SHARE_SEMITONES + 1` so MIDI 60 is a center
    and every note lands within the share tolerance of its base.
    """
    step = 2 * BAKED_PITCH_SHARE_SEMITONES + 1
    return 60 + int(round((int(note) - 60) / step)) * step


def _quantize_pw_frames(frames: list[int]) -> tuple[int, ...]:
    """PW timeline in 128-wide buckets, matching duty/mod sample sharing."""
    return tuple((width & 0xFFF) >> 7 for width in frames)


def _expand_pw_buckets(buckets: tuple[int, ...]) -> list[int]:
    """Mid-bucket pulse widths for a quantized bake key."""
    return [min(4095, (bucket << 7) + 64) for bucket in buckets]


def _midi_hz(note: int) -> float:
    return 440.0 * 2 ** ((int(note) - 69) / 12)


def _baked_pcm(
    wave_bits: int,
    base_midi: int,
    pw_values: list[int],
    frame_hz: float,
    ratio: float | None = None,
    sync: bool = False,
    ring: bool = False,
    chip_model: str | None = None,
) -> tuple[bytes, int, int]:
    """Render a PWM sweep at a reference pitch for tracker transposition.

    Nearby notes reuse this PCM; IT's note column scales both pitch and sweep
    rate. `C5Speed` is set so `midi_to_it_note(base_midi)` plays at
    `BAKED_RATE_HZ` (matching static-loop octave mapping). The loop starts on
    the last whole cycle so a held note repeats steady state.
    """
    sid_hz = _midi_hz(base_midi)
    samples_per_frame = max(1, int(round(BAKED_RATE_HZ / frame_hz)))
    total = max(samples_per_frame, len(pw_values) * samples_per_frame)
    step = max(1, int(round(ACC_CYCLE * sid_hz / BAKED_RATE_HZ)))
    modulating = bool(ratio) and (sync or ring)
    values = render(
        wave_bits,
        total,
        step,
        pw_timeline=pw_values,
        samples_per_frame=samples_per_frame,
        ratio=snapped_ratio(ratio) if modulating else None,
        sync=sync,
        ring=ring,
        chip_model=chip_model,
    )
    cycle = max(1, min(total, int(round(BAKED_RATE_HZ / max(sid_hz, 1.0)))))
    # midi_to_it_note subtracts 12, so C5Speed needs a 2x factor vs a raw
    # "play this MIDI at BAKED_RATE" formula for the base pitch to land in tune.
    c5_speed = int(round(2 * BAKED_RATE_HZ * 2 ** ((60 - base_midi) / 12)))
    return _dac_pcm(values), max(1, c5_speed), max(0, total - cycle)


def _sample_header(
    name: str, length: int, data_offset: int, c5_speed: int, loop_start: int = 0
) -> bytes:
    flags = 0x01 | 0x10  # sample associated, loop
    hdr = bytearray(80)
    hdr[0:4] = b"IMPS"
    hdr[4:16] = _pad(name[:11], 12)
    hdr[17] = 64  # global vol
    hdr[18] = flags
    hdr[19] = 64  # default vol
    hdr[20:46] = _pad(name, 26)
    hdr[46] = 1  # signed PCM
    hdr[47] = 32  # default pan centre, pan off
    hdr[48:52] = length.to_bytes(4, "little")
    hdr[52:56] = loop_start.to_bytes(4, "little")
    hdr[56:60] = length.to_bytes(4, "little")
    hdr[60:64] = c5_speed.to_bytes(4, "little")
    hdr[72:76] = data_offset.to_bytes(4, "little")
    return bytes(hdr)


@dataclass
class Voice:
    """One .it instrument together with the sample it plays."""

    name: str
    base: Any
    pcm: bytes
    c5_speed: int
    loop_start: int = 0


@dataclass
class SamplePlan:
    voices: list[Voice] = field(default_factory=list)
    # (channel id, note_on tick) -> 1-based instrument number, for the notes
    # that need something other than their instrument's plain static loop.
    assign: dict[tuple[int, int], int] = field(default_factory=dict)
    baked: int = 0
    baked_bytes: int = 0
    sync_ring: int = 0
    static_duty: int = 0
    sample_bytes: int = 0
    pwm_candidates: int = 0
    budget_hit: bool = False
    instrument_limit_hit: bool = False
    skipped_budget: int = 0
    skipped_instrument_limit: int = 0


def _note_spans(channel, inst_by_id=None, frame_hz: float = 50.0) -> dict[int, int]:
    """End tick of every note_on: the next onset, or the gate fall plus release.

    A note is not over when its gate falls. The SID envelope keeps the
    oscillator running through the release leg, the driver keeps writing pulse
    width while it does, and all of that is audible. Stopping at the fall reads
    only the hard-restart duty a driver writes in the gate frame, which is why
    a lead gated at width 0 came out as a silent loop.
    """
    onsets = sorted(e.tick for e in channel.events if e.type == "note_on")
    stops = sorted(
        {e.tick for e in channel.events if e.type in ("note_on", "note_off")}
    )
    releases = {
        e.tick: _envelope_frames(
            DECAY_MS[inst_by_id[e.instrument].release & 15], frame_hz
        )
        for e in channel.events
        if e.type == "note_on" and inst_by_id and e.instrument in inst_by_id
    }
    spans: dict[int, int] = {}
    index = 0
    for position, start in enumerate(onsets):
        while index < len(stops) and stops[index] <= start:
            index += 1
        stop = stops[index] if index < len(stops) else start + 1
        next_onset = onsets[position + 1] if position + 1 < len(onsets) else None
        if next_onset is None or stop < next_onset:
            # The gate fell; the voice rings on until it is reused.
            stop += releases.get(start, 0)
            if next_onset is not None:
                stop = min(stop, next_onset)
        spans[start] = max(stop, start + 1)
    return spans


def _pw_frames(pw_points: list[list[int]], start: int, end: int) -> list[int]:
    """Expand sparse pulse-width writes into one value per player frame."""
    if not pw_points:
        return []
    values: list[int] = []
    index = 0
    current = pw_points[0][1]
    for tick in range(start, max(end, start + 1)):
        while index + 1 < len(pw_points) and pw_points[index + 1][0] <= tick:
            index += 1
            current = pw_points[index][1]
        values.append(current)
    return values


def _note_pw_frames(event, end: int, frame_hz: float) -> list[int]:
    """Pulse width per frame over a note's audible life, capped at what bakes.

    A bake renders at most `MAX_BAKED_SECONDS` and then loops its last cycle,
    so a note left ringing over a long gap costs no more sample memory than
    any other and still qualifies for one.
    """
    frames = _pw_frames(event.pw_points, event.tick, end)
    return frames[: max(1, round(MAX_BAKED_SECONDS * frame_hz))]


def _pwm_sweeping(wave_bits: int, frames: list[int]) -> bool:
    return (
        bool(wave_bits & 0x40)
        and not wave_bits & 0x80
        and len(frames) > 1
        and max(frames) - min(frames) >= PW_MOVE_MIN
    )


def _pulse_rms(width: int) -> float:
    """AC RMS of one pulse duty, 0.0-0.5 of full scale.

    The output is high for `(4096 - width) / 4096` of the cycle (`sid-chip`),
    and a mean-removed two-level wave with a high fraction of `d` has an RMS of
    `sqrt(d * (1 - d))`. A 50% square is the loudest at 0.5; an extreme duty is
    nearly DC and inaudible.
    """
    high = (4096 - (width & 0xFFF)) / 4096
    return math.sqrt(max(0.0, high * (1.0 - high)))


def _static_pulse_width(frames: list[int]) -> int | None:
    """One duty carrying the mean energy of a swept one, or None if steady.

    A static loop of the width a note *starts* on is not that note. Drivers
    write a hard-restart duty in the gate frame and sweep away from it, so the
    Eagles lead opens on width 2, which is so close to DC that its loop is
    inaudible while the chip plays a normal lead. Averaging `_pulse_rms` over
    the note and inverting it picks the width that is as loud as the sweep was.
    Inverting is symmetric about 50%, so the side the note spent its time on
    decides the root and a narrow pulse stays narrow.
    """
    if len(frames) < 2 or min(frames) == max(frames):
        return None
    mean_rms = sum(_pulse_rms(width) for width in frames) / len(frames)
    narrow = (1.0 - math.sqrt(max(0.0, 1.0 - 4.0 * mean_rms * mean_rms))) / 2.0
    median = sorted(frames)[len(frames) // 2]
    high = narrow if (4096 - median) / 4096 < 0.5 else 1.0 - narrow
    return max(1, min(4095, round(4096 * (1.0 - high))))


def plan_samples(
    ir: MusicIR, budget_bytes: int, bake: bool = True
) -> SamplePlan:
    """Choose a sample for every note: static loop, modulated loop, or baked.

    Sync, ring, and pulse-width modulation have no tracker effect to carry
    them, so they have to end up in sample data. A steady setting still loops
    in a few hundred bytes; only a moving duty forces a note to be rendered in
    real time, which is what the budget limits. `bake` False keeps PWM as the
    instrument's energy-matched static loop.
    """
    plan = SamplePlan()
    chip_model = ir.sid_model
    base_number: dict[int, int] = {}
    for index, inst in enumerate(ir.instruments):
        pcm, speed = _pcm(
            inst.waveform, inst.pulse_width, inst.ctrl, chip_model=chip_model
        )
        plan.voices.append(Voice(inst.name or f"inst{index + 1}", inst, pcm, speed))
        base_number[inst.id] = len(plan.voices)
    plan.sample_bytes = sum(len(voice.pcm) for voice in plan.voices)

    inst_by_id = {i.id: i for i in ir.instruments}
    variants: dict[tuple, int] = {}

    def add(key: tuple, voice: Voice, against_budget: bool) -> int | None:
        if len(plan.voices) >= MAX_IT_INSTRUMENTS:
            plan.instrument_limit_hit = True
            plan.skipped_instrument_limit += 1
            return None
        if against_budget and plan.sample_bytes + len(voice.pcm) > budget_bytes:
            plan.budget_hit = True
            plan.skipped_budget += 1
            return None
        plan.voices.append(voice)
        plan.sample_bytes += len(voice.pcm)
        variants[key] = len(plan.voices)
        return variants[key]

    for channel in ir.channels:
        spans = _note_spans(channel, inst_by_id, ir.timing.frame_hz)
        for event in channel.events:
            if event.type != "note_on":
                continue
            inst = inst_by_id.get(event.instrument)
            if inst is None:
                continue
            wave_bits = _wave_bits(inst.waveform, inst.ctrl)
            modulating = (inst.sync or inst.ring) and bool(event.modulator_ratio)
            ratio = event.modulator_ratio if modulating else None
            end = spans.get(event.tick, event.tick + 1)
            frames = _note_pw_frames(event, end, ir.timing.frame_hz)
            sweeping = _pwm_sweeping(wave_bits, frames)
            static_pw = inst.pulse_width
            if wave_bits & 0x40 and not wave_bits & 0x80:
                matched = _static_pulse_width(frames)
                # A duty the sweep only passes through can be so extreme that
                # its loop is silent, and then any audible replacement beats
                # sharing a loop with the neighbouring duty bucket.
                if matched is not None and (
                    abs(matched - static_pw) >= PW_MOVE_MIN
                    or _pulse_rms(static_pw) < MIN_AUDIBLE_PULSE_RMS
                ):
                    static_pw = matched
            number = None
            if sweeping:
                plan.pwm_candidates += 1
                if bake:
                    base_midi = _bake_base_midi(int(event.note))
                    pw_key = _quantize_pw_frames(frames)
                    key = ("bake", inst.id, base_midi, pw_key)
                    number = variants.get(key)
                    if number is None:
                        pcm, c5_speed, loop_start = _baked_pcm(
                            wave_bits,
                            base_midi,
                            _expand_pw_buckets(pw_key),
                            ir.timing.frame_hz,
                            ratio,
                            inst.sync,
                            inst.ring,
                            chip_model=chip_model,
                        )
                        number = add(
                            key,
                            Voice(
                                f"{inst.name[:6]}{base_midi}pwm",
                                inst,
                                pcm,
                                c5_speed,
                                loop_start,
                            ),
                            True,
                        )
                        if number is not None:
                            plan.baked += 1
                            plan.baked_bytes += len(pcm)
            if number is None and modulating:
                key = ("mod", inst.id, snapped_ratio(ratio), static_pw >> 7)
                number = variants.get(key)
                if number is None:
                    pcm, c5_speed = _pcm(
                        inst.waveform,
                        static_pw,
                        inst.ctrl,
                        ratio,
                        inst.sync,
                        inst.ring,
                        chip_model=chip_model,
                    )
                    kind = "ring" if inst.ring else "sync"
                    # Cheap looping samples stay available without --use-pwm.
                    number = add(
                        key,
                        Voice(f"{inst.name[:8]}{kind}", inst, pcm, c5_speed),
                        against_budget=bake,
                    )
                    if number is not None:
                        plan.sync_ring += 1
            if number is None and static_pw != inst.pulse_width:
                # Duties within 128 of each other share a loop, the same
                # quantisation the lift uses to keep a sweep from minting an
                # instrument per frame. A 256-byte loop replaces a wrong sound
                # rather than adding fidelity, so it is not charged to the
                # bake budget.
                key = ("duty", inst.id, static_pw >> 7)
                number = variants.get(key)
                if number is None:
                    pcm, c5_speed = _pcm(
                        inst.waveform,
                        static_pw,
                        inst.ctrl,
                        chip_model=chip_model,
                    )
                    number = add(
                        key,
                        Voice(f"{inst.name[:7]}duty", inst, pcm, c5_speed),
                        against_budget=False,
                    )
                    if number is not None:
                        plan.static_duty += 1
            if number is not None and number != base_number[inst.id]:
                plan.assign[(channel.id, event.tick)] = number
    return plan


def module_timing(ir: MusicIR) -> tuple[int, int]:
    """IT speed and tempo for the IR's row grid.

    IT ticks run at `tempo * 2 / 5` Hz, so tempo 125 is the classic 50 Hz tick.
    At one frame per row there is no tick after the first for Gxx or Jxy to act
    on, which would force every held-gate pitch change to retrigger the
    instrument. Doubling speed and tempo together leaves the row rate identical
    and buys that second tick. Tempo is one byte (max 255 => ~102 Hz ticks), so
    faster CIA rates (Paperboy subtune 1 at ~150 Hz) clamp tempo and shrink
    speed to keep the musical row rate.
    """
    tpr = max(1, ir.timing.ticks_per_row)
    frame_hz = ir.timing.frame_hz
    row_hz = frame_hz / tpr
    tempo = int(round(125.0 * frame_hz / 50.0))
    speed = tpr
    if tempo > 255 or tempo < 32:
        tempo = max(32, min(255, tempo))
        tick_hz = tempo * 2.0 / 5.0
        speed = max(1, min(255, int(round(tick_hz / row_hz))))
    if speed == 1 and tempo * 2 <= 255:
        speed, tempo = 2, tempo * 2
    return speed, tempo


def _nearest_row(tick: int, tpr: int) -> int:
    """Absolute nearest row for a dump tick."""
    return (tick + tpr // 2) // tpr


def _onset_anchor_ticks(channel: Channel) -> list[int]:
    """Player-row onsets: skip 1-frame waveform follow-ons (Huelsbeck click)."""
    return [
        event.tick
        for event in channel.events
        if event.type == "note_on"
        and (event.extra or {}).get("onset") != "waveform"
    ]


def _spacing_anchor_rows(onset_ticks: list[int], tpr: int) -> list[int]:
    """Row for each onset from inter-onset spacing, not a fixed phase lattice.

    CIA jitter turns 2*11 into 21 as often as 22. Snapping each tick with
    absolute nearest-row then collapses some 21-frame holds to one row
    (Highnoon). Rounding each gap in player steps keeps 21 and 22 as two rows.
    """
    if not onset_ticks:
        return []
    rows = [_nearest_row(onset_ticks[0], tpr)]
    for earlier, later in zip(onset_ticks, onset_ticks[1:]):
        # 0 is allowed so a sub-row ornament shares the previous cell.
        rows.append(rows[-1] + round((later - earlier) / tpr))
    return rows


def _row_from_spacing(
    tick: int,
    onset_ticks: list[int],
    anchor_rows: list[int],
    tpr: int,
    *,
    note_off: bool,
) -> int:
    """Map a dump tick to a pattern row using spacing-preserving onset anchors."""
    if not onset_ticks:
        if note_off:
            return math.ceil(tick / tpr)
        return _nearest_row(tick, tpr)
    if tick <= onset_ticks[0]:
        if tick == onset_ticks[0]:
            return anchor_rows[0]
        # Rare events before the first note: keep the same phase as the anchor.
        return max(0, anchor_rows[0] + _nearest_row(tick, tpr) - _nearest_row(onset_ticks[0], tpr))
    if tick >= onset_ticks[-1]:
        gap = tick - onset_ticks[-1]
        if note_off:
            # Round ends up so a positive-duration note keeps a note-off cell.
            return anchor_rows[-1] + (math.ceil(gap / tpr) if gap else 0)
        return anchor_rows[-1] + round(gap / tpr)
    index = bisect.bisect_right(onset_ticks, tick) - 1
    t0 = onset_ticks[index]
    t1 = onset_ticks[index + 1]
    r0 = anchor_rows[index]
    r1 = anchor_rows[index + 1]
    if t1 <= t0:
        return r0
    frac = (tick - t0) / (t1 - t0)
    span = r1 - r0
    if note_off:
        # Progress toward the next onset, never before this note's row.
        stepped = r0 + math.ceil(frac * span) if span else r0
        return max(r0, stepped)
    return r0 + round(frac * span)


def _quantize(
    ir: MusicIR,
    plan: SamplePlan | None = None,
    filter_automation: bool = True,
) -> tuple[int, int, list[list[dict]]]:
    tpr = ir.timing.ticks_per_row
    # Gxx and Jxy need a tick after the row's first one to act on.
    legato = module_timing(ir)[0] > 1
    # Keep the dump's exact length so the loop returns where the tune ends.
    # The last pattern is short rather than padded out to 64 rows. Impulse
    # Tracker itself will not edit a pattern under 32 rows, so that is the
    # floor, which only pads the very shortest sound effects.
    # Onsets are placed from inter-onset spacing (round(gap / tpr) rows) so a
    # 21-frame CIA hold on an 11-tick player stays two rows even after phase
    # drift (Highnoon). Absolute nearest-row alone collapses some of those
    # holds to one row. Note ends still round up along that map so a
    # positive-duration note cannot lose its note-off. Exact-grid events are
    # unchanged.
    anchors: dict[int, tuple[list[int], list[int]]] = {}
    max_event_row = 0
    for channel in ir.channels:
        onset_ticks = _onset_anchor_ticks(channel)
        anchor_rows = _spacing_anchor_rows(onset_ticks, tpr)
        anchors[channel.id] = (onset_ticks, anchor_rows)
        for event in channel.events:
            max_event_row = max(
                max_event_row,
                _row_from_spacing(
                    event.tick,
                    onset_ticks,
                    anchor_rows,
                    tpr,
                    note_off=event.type == "note_off",
                ),
            )
    rows = max(
        MIN_PATTERN_ROWS,
        max_event_row + 1,
        math.ceil(ir.last_tick / tpr) + 1,
    )
    if rows > PATTERN_ROWS * MAX_PATTERNS:
        raise IRError(
            f"{rows} rows exceed {MAX_PATTERNS} patterns; dump fewer ticks "
            f"or raise ticks_per_row"
        )
    nch = max((c.id for c in ir.channels), default=0) + 1
    nch = max(nch, 1)
    grid: list[list[dict]] = [[{} for _ in range(nch)] for _ in range(rows)]
    inst_by_id = {i.id: i for i in ir.instruments}
    smp_index = {i.id: n + 1 for n, i in enumerate(ir.instruments)}
    assign = plan.assign if plan else {}
    for channel in ir.channels:
        # A pitch event continues whichever voice the last note_on chose, so
        # a baked or modulated note is not silently swapped mid-note.
        sounding = 0
        onset_ticks, anchor_rows = anchors[channel.id]
        for event in channel.events:
            event_row = _row_from_spacing(
                event.tick,
                onset_ticks,
                anchor_rows,
                tpr,
                note_off=event.type == "note_off",
            )
            row = min(rows - 1, event_row)
            cell = grid[row][channel.id]
            if event.type == "note_on":
                inst = inst_by_id[event.instrument]
                sounding = assign.get(
                    (channel.id, event.tick), smp_index[event.instrument]
                )
                cell["note"] = midi_to_it_note(int(event.note))
                cell["inst"] = sounding
                cell["vol"] = _note_velocity()
                if event.arpeggio and len(event.arpeggio) >= 3:
                    x = max(0, min(15, int(event.arpeggio[1])))
                    y = max(0, min(15, int(event.arpeggio[2])))
                    cell["cmd"] = IT_EFFECT_J
                    cell["param"] = (x << 4) | y
            elif event.type == "pitch":
                cell["note"] = midi_to_it_note(int(event.note))
                if event.arpeggio and len(event.arpeggio) >= 3:
                    x = max(0, min(15, int(event.arpeggio[1])))
                    y = max(0, min(15, int(event.arpeggio[2])))
                    cell["cmd"] = IT_EFFECT_J
                    cell["param"] = (x << 4) | y
                elif legato:
                    # Move the sounding oscillator without restarting the
                    # sample or the instrument envelope.
                    cell["cmd"] = IT_EFFECT_G
                    cell["param"] = 0xFF
                else:
                    cell["inst"] = sounding or smp_index[event.instrument]
                    cell["vol"] = _note_velocity()
            elif event.type == "effect" and event.arpeggio:
                x = max(0, min(15, int(event.arpeggio[1])))
                y = max(0, min(15, int(event.arpeggio[2])))
                cell["cmd"] = IT_EFFECT_J
                cell["param"] = (x << 4) | y
            elif event.type == "note_off" and "note" not in cell:
                cell["note"] = IT_NOTE_OFF
    nch = _apply_filter(ir, grid, rows, nch, tpr, filter_automation)
    rows, trimmed = _trim_second_loop(grid, rows, nch)
    _loop_to_start(grid, rows, nch)
    return rows, nch, grid, trimmed


def _row_fingerprint(cells: list[dict]) -> tuple:
    """Note and instrument only. Vol/effects often differ on the second pass."""
    return _soften_fingerprint(
        tuple((cell.get("note"), cell.get("inst")) for cell in cells)
    )


def _soften_fingerprint(fingerprint: tuple) -> tuple:
    """Treat note-off/cut like empty for second-loop matching.

    The first playthrough has never gated a quiet channel, so its opening
    cells are blank, while an HVSC overrun into the next loop often still
    carries the previous phrase's note-offs on those same rows (Tapper #5).
    """
    out = []
    for note, inst in fingerprint:
        if note is not None and note >= IT_NOTE_CUT:
            note = None
        out.append((note, inst if note is not None else None))
    return tuple(out)


def _fingerprint_has_note(fingerprint: tuple) -> bool:
    return any(
        note is not None and note < IT_NOTE_CUT for note, _inst in fingerprint
    )


def second_loop_start(fingerprints: list[tuple]) -> int | None:
    """Row index where a trailing copy of the opening begins, or None.

    HVSC lengths often continue a few seconds into the next playthrough of a
    tune that loops to the start. When a prefix of the dump reappears near the
    end, that later copy is overrun and the loop point is the cut.

    Vol/effects and note-offs are ignored so a second pass that retriggers
    differently, or still releases the previous phrase, still matches. A
    mid-song intro motif is rejected when too much unique material follows the
    match. A repeated A section that ends on a different onset (coda) is also
    rejected: unique rows after the match may only be blank or note-off slack.
    """
    fingerprints = [_soften_fingerprint(fp) for fp in fingerprints]
    n = len(fingerprints)
    if n < MIN_LOOP_MATCH_ROWS * 2:
        return None
    best_at: int | None = None
    best_match = 0
    min_loop_at = max(MIN_LOOP_MATCH_ROWS, n - MAX_LOOP_OVERUN_ROWS)
    for loop_at in range(min_loop_at, n - MIN_LOOP_MATCH_ROWS + 1):
        match_len = 0
        while (
            loop_at + match_len < n
            and fingerprints[loop_at + match_len] == fingerprints[match_len]
        ):
            match_len += 1
        if match_len < MIN_LOOP_MATCH_ROWS:
            continue
        # Unique material after the matched opening must be quantize slack only
        # (blank rows / note-offs). A new onset there is a coda, not overrun:
        # Paperboy subtune 4 repeats the A section then holds a longer final
        # chord. Treat a new onset in that unique tail as a coda, not overrun.
        unique_tail = fingerprints[loop_at + match_len : n]
        if len(unique_tail) > MAX_LOOP_TAIL_ROWS:
            continue
        if any(_fingerprint_has_note(row) for row in unique_tail):
            continue
        matched = fingerprints[:match_len]
        note_rows = sum(1 for row in matched if _fingerprint_has_note(row))
        if note_rows < MIN_LOOP_NOTE_ROWS:
            continue
        body = fingerprints[:loop_at]
        if len(set(body)) < MIN_LOOP_DISTINCT_ROWS:
            continue
        if len(set(matched)) < 2:
            continue
        # Longest match wins; earlier cut (one loop) breaks ties.
        if match_len > best_match or (
            match_len == best_match and (best_at is None or loop_at < best_at)
        ):
            best_match = match_len
            best_at = loop_at
    return best_at



def _trim_second_loop(
    grid: list[list[dict]], rows: int, nch: int
) -> tuple[int, int]:
    """Drop trailing second-loop overrun; return (new_rows, trimmed_count)."""
    fingerprints = [_row_fingerprint(grid[row][:nch]) for row in range(rows)]
    loop_at = second_loop_start(fingerprints)
    if loop_at is None:
        return rows, 0
    del grid[loop_at:]
    return loop_at, rows - loop_at


def _loop_to_start(grid: list[list[dict]], rows: int, nch: int) -> None:
    """Jump back to the first order on the last row.

    A SID player runs forever, and the HVSC song length we dump is one time
    round. Without this the module just stops at the end of the dump.
    """
    row = rows - 1
    cells = grid[row]
    target = next((ch for ch in range(nch) if "cmd" not in cells[ch]), 0)
    cells[target]["cmd"] = IT_EFFECT_B
    cells[target]["param"] = 0


def _free_effect_cell(cells: list[dict], nch: int) -> dict | None:
    """A cell on this row with its effect slot still free, or None.

    A fully empty cell is preferred, so a note row keeps its slot for the
    cutoff repeat that `_apply_filter` writes there.
    """
    for cell in cells[:nch]:
        if not cell:
            return cell
    for cell in cells[:nch]:
        if "cmd" not in cell:
            return cell
    return None


def _steal_cutoff_cell(cells: list[dict], nch: int) -> dict | None:
    """A Zxx cutoff cell, not mode or resonance. Vxx can replace it.

    Cutoff is already latched on the channel. Mode, resonance, mute, Jxx, and
    Gxx are not stolen: those would drop a mixer or note effect.
    """
    for cell in cells[:nch]:
        if cell.get("cmd") == IT_EFFECT_Z and cell.get("param", 256) < IT_RESONANCE_BASE:
            return cell
    return None


def _place_global_volume(cells: list[dict], nch: int, volume: int) -> bool:
    """Write Vxx onto a SID voice. Never grows the channel count."""
    cell = _free_effect_cell(cells, nch)
    if cell is None:
        cell = _steal_cutoff_cell(cells, nch)
    if cell is None:
        return False
    cell["cmd"] = IT_EFFECT_V
    cell["param"] = volume
    return True


def _apply_filter(
    ir,
    grid: list[list[dict]],
    rows: int,
    nch: int,
    tpr: int,
    filter_automation: bool = True,
) -> int:
    """Write SID mixer state: filter Zxx, channel mute, master Vxx.

    A voice not routed through the filter is forced open, otherwise it would
    keep the cutoff of whatever was routed there earlier. A voice routed with
    no mode taps is muted (SID mixer rule). Voice 3 Off mutes only a dry
    voice 3. Notes mode keeps mute and master volume but skips Zxx.

    Returns the channel count. Always the SID voices: a master volume change
    that finds no free slot steals a cutoff Zxx or waits for a later row
    rather than minting a silent fourth channel.
    """
    points = sorted(ir.filter, key=lambda point: point.tick)
    model = ir.sid_model
    uses_filter = bool(filter_automation) and _chip_filter_used(ir)
    uses_mixer = _mixer_used(ir)
    uses_mute = any(
        _voice_path(point, channel) == "mute"
        for point in points
        for channel in range(3)
    )
    if not points or not (uses_filter or uses_mixer or uses_mute):
        return nch
    index = 0
    state = None
    last_cutoff: list[int | None] = [None] * nch
    last_resonance: list[int | None] = [None] * nch
    last_mode: list[int | None] = [None] * nch
    last_path: list[str | None] = [None] * nch
    last_global: int | None = None
    pending_volume: int | None = None

    for row in range(rows):
        # Sample mid-row, not the left edge. A coarse player grid often starts
        # a note while the previous note's closing cutoff is still latched, and
        # the open for the new note arrives one frame later (Eliminator bass).
        # Edge sampling then muffles the whole row; the midpoint catches the
        # open. At one tick per row the midpoint is the row itself.
        sample_tick = row * tpr + (tpr // 2 if tpr > 1 else 0)
        while index < len(points) and points[index].tick <= sample_tick:
            state = points[index]
            index += 1
        if state is None:
            continue
        for channel in range(min(3, nch)):
            path = _voice_path(state, channel)
            cell = grid[row][channel]
            mute_now = path == "mute"
            mute_was = last_path[channel] == "mute"
            last_path[channel] = path
            if mute_now != mute_was and "cmd" not in cell:
                cell["cmd"] = IT_EFFECT_M
                cell["param"] = 0 if mute_now else IT_CHANNEL_VOL_FULL
                continue
            if "cmd" in cell:
                continue
            if not uses_filter:
                continue
            if path != "filter":
                cutoff = IT_CUTOFF_OPEN
                resonance = 0
                mode = None
            else:
                cutoff = it_cutoff(state.cutoff, model)
                resonance = it_resonance(state.resonance)
                mode = it_filter_mode(state.mode)
            if mode is not None and mode != last_mode[channel]:
                cell["cmd"] = IT_EFFECT_Z
                cell["param"] = mode
                last_mode[channel] = mode
                continue
            if resonance != last_resonance[channel]:
                cell["cmd"] = IT_EFFECT_Z
                cell["param"] = IT_RESONANCE_BASE + resonance
                last_resonance[channel] = resonance
                continue
            # Repeat the cutoff on note rows: a new note can otherwise reset
            # the channel filter to the instrument default.
            if cutoff != last_cutoff[channel] or "note" in cell:
                cell["cmd"] = IT_EFFECT_Z
                cell["param"] = cutoff
                last_cutoff[channel] = cutoff
        if uses_mixer:
            volume = it_global_volume(state.volume)
            if volume != last_global:
                pending_volume = volume
                last_global = volume
            if pending_volume is not None and _place_global_volume(
                grid[row], nch, pending_volume
            ):
                pending_volume = None

    if pending_volume is not None:
        for row in range(rows - 1, -1, -1):
            if _place_global_volume(grid[row], nch, pending_volume):
                break
    return nch


def _pack_pattern(block: list[list[dict]], nch: int) -> bytes:
    packed = bytearray()
    for row_cells in block:
        for ch in range(nch):
            cell = row_cells[ch]
            if not cell:
                continue
            mask = 0
            payload = bytearray()
            if "note" in cell:
                mask |= 1
                payload.append(cell["note"])
            if "inst" in cell:
                mask |= 2
                payload.append(cell["inst"])
            if "vol" in cell:
                mask |= 4
                payload.append(cell["vol"])
            if "cmd" in cell:
                mask |= 8
                payload.append(cell["cmd"])
                payload.append(cell["param"])
            packed.append(0x80 | (ch + 1))
            packed.append(mask)
            packed.extend(payload)
        packed.append(0)
    header = bytearray(8)
    header[0:2] = len(packed).to_bytes(2, "little")
    header[2:4] = len(block).to_bytes(2, "little")
    return bytes(header) + bytes(packed)


@dataclass
class SampleSlot:
    """One unique PCM blob in the module sample table."""

    name: str
    pcm: bytes
    c5_speed: int
    loop_start: int = 0


def _shared_samples(voices: list[Voice]) -> tuple[list[SampleSlot], list[int]]:
    """Collapse identical PCM into one sample; keep a 1-based map per voice.

    Instruments still own their envelopes, so two noise hits with different
    ADSR stay two instruments, but they point at the same 4096-byte LFSR loop.
    """
    slots: list[SampleSlot] = []
    index_of: dict[tuple[bytes, int, int], int] = {}
    mapping: list[int] = []
    for voice in voices:
        key = (voice.pcm, voice.c5_speed, voice.loop_start)
        number = index_of.get(key)
        if number is None:
            slots.append(
                SampleSlot(voice.name, voice.pcm, voice.c5_speed, voice.loop_start)
            )
            number = len(slots)
            index_of[key] = number
        mapping.append(number)
    return slots, mapping


def _dedupe_patterns(
    blocks: list[list[list[dict]]], nch: int
) -> tuple[list[bytes], list[int]]:
    """Store each distinct packed pattern once; return order-list indices."""
    packed_blocks: list[bytes] = []
    index_of: dict[bytes, int] = {}
    order: list[int] = []
    for block in blocks:
        packed = _pack_pattern(block, nch)
        index = index_of.get(packed)
        if index is None:
            index = len(packed_blocks)
            index_of[packed] = index
            packed_blocks.append(packed)
        order.append(index)
    return packed_blocks, order


def _grid_has(grid: list[list[dict]], command: int) -> bool:
    return any(cell.get("cmd") == command for row in grid for cell in row)


def _source_features(ir: MusicIR) -> dict[str, Any]:
    note_ons = 0
    arpeggio = False
    pitch = False
    waveform_switch = False
    pwm = False
    sync_ring = False
    inst_by_id = {i.id: i for i in ir.instruments}
    for channel in ir.channels:
        spans = _note_spans(channel, inst_by_id, ir.timing.frame_hz)
        for event in channel.events:
            if event.arpeggio:
                arpeggio = True
            if event.type == "pitch":
                pitch = True
            if event.type != "note_on":
                continue
            note_ons += 1
            extra = event.extra or {}
            if extra.get("onset") == "waveform":
                waveform_switch = True
            inst = inst_by_id.get(event.instrument)
            if inst is None:
                continue
            if (inst.sync or inst.ring) and event.modulator_ratio:
                sync_ring = True
            end = spans.get(event.tick, event.tick + 1)
            frames = _note_pw_frames(event, end, ir.timing.frame_hz)
            if _pwm_sweeping(_wave_bits(inst.waveform, inst.ctrl), frames):
                pwm = True
    waveforms: list[str] = []
    for inst in ir.instruments:
        if inst.waveform not in waveforms:
            waveforms.append(inst.waveform)
        if inst.sync or inst.ring:
            sync_ring = True
    filter_lp = False
    filter_hp = False
    filter_res = False
    for point in ir.filter:
        if not (point.mode and any(point.routing)):
            continue
        taps = set(point.mode)
        if "lp" in taps:
            filter_lp = True
        if "hp" in taps or "bp" in taps:
            filter_hp = True
        if point.resonance:
            filter_res = True
    return {
        "note_ons": note_ons,
        "arpeggio": arpeggio,
        "pitch": pitch,
        "waveform_switch": waveform_switch,
        "pwm": pwm,
        "sync_ring": sync_ring,
        "waveforms": waveforms,
        "filter_lp": filter_lp,
        "filter_hp": filter_hp,
        "filter_resonance": filter_res,
    }


def write_it(
    ir: MusicIR,
    sample_budget_mb: float = DEFAULT_SAMPLE_BUDGET_MB,
    *,
    use_pwm: bool = False,
    use_filter: bool = True,
    grid_reason: str = "",
    grid_warning: str = "",
) -> bytes:
    return write_module(
        ir,
        sample_budget_mb,
        use_pwm=use_pwm,
        use_filter=use_filter,
        grid_reason=grid_reason,
        grid_warning=grid_warning,
    ).data


def write_module(
    ir: MusicIR,
    sample_budget_mb: float = DEFAULT_SAMPLE_BUDGET_MB,
    *,
    use_pwm: bool = False,
    use_filter: bool = True,
    grid_reason: str = "",
    grid_warning: str = "",
) -> ModuleWrite:
    if not ir.instruments:
        raise IRError("IR has no instruments")
    if len(ir.instruments) > MAX_IT_INSTRUMENTS:
        raise IRError(
            f"{len(ir.instruments)} instruments; .it numbers them in one byte, "
            "so the lift must merge more aggressively"
        )
    budget_bytes = int(sample_budget_mb * 1024 * 1024)
    plan = plan_samples(ir, budget_bytes, bake=use_pwm)
    rows, nch, grid, loop_trim_rows = _quantize(
        ir, plan, filter_automation=use_filter
    )
    raw_speed = ir.timing.ticks_per_row
    speed, tempo = module_timing(ir)

    blocks = [
        grid[start : start + PATTERN_ROWS]
        for start in range(0, rows, PATTERN_ROWS)
    ]
    # Impulse Tracker will not edit a pattern under 32 rows, and a long tune
    # can still end on a short one. The B00 loop jump already sits on the last
    # row with content, so this padding is never played.
    short = MIN_PATTERN_ROWS - len(blocks[-1])
    if short > 0:
        blocks[-1] = blocks[-1] + [[{} for _ in range(nch)] for _ in range(short)]
    packed_blocks, order_indices = _dedupe_patterns(blocks, nch)
    if len(packed_blocks) > MAX_PATTERNS:
        raise IRError(
            f"{len(packed_blocks)} unique patterns exceed the {MAX_PATTERNS} "
            ".it pattern cap; dump fewer ticks or raise ticks_per_row"
        )
    slots, sample_for_voice = _shared_samples(plan.voices)
    smp_num = len(slots)
    ins_num = len(plan.voices)
    pat_num = len(packed_blocks)
    orders = bytes(order_indices + [0xFF])
    ord_num = len(orders)

    header = bytearray(0xC0)
    header[0:4] = b"IMPM"
    header[4:30] = _pad(it_song_title(ir, fallback="untitled", max_len=25), 26)
    header[30] = 4
    header[31] = 16
    header[32:34] = ord_num.to_bytes(2, "little")
    header[34:36] = ins_num.to_bytes(2, "little")
    header[36:38] = smp_num.to_bytes(2, "little")
    header[38:40] = pat_num.to_bytes(2, "little")
    header[40:42] = (0x0214).to_bytes(2, "little")  # created with IT 2.14
    header[42:44] = (0x0214).to_bytes(2, "little")  # compatible
    flags = IT_FLAG_STEREO | IT_FLAG_INSTRUMENTS | IT_FLAG_LINEAR
    special = IT_SPECIAL_SONG_MESSAGE | IT_SPECIAL_EDIT_HISTORY
    midi = b""
    history = _edit_history_block()
    message = _encode_song_message(song_message_text(ir))
    artist = str(ir.source.get("author") or "")
    stpm = _openmpt_song_extensions(artist)
    chip_filter = use_filter and _chip_filter_used(ir)
    if chip_filter:
        # Extended cutoff range is OpenMPT-specific and brings Z7F nearer the
        # SID's 12 kHz top. Embed Z90/Z91 so high-pass and band-pass tunes are
        # not stuck on the default low-pass.
        flags |= IT_FLAG_EXT_FILTER | IT_FLAG_EMBED_MIDI
        special |= IT_SPECIAL_EMBED_MIDI
        midi = midi_config()
    header[44:46] = flags.to_bytes(2, "little")
    header[46:48] = special.to_bytes(2, "little")
    header[48] = 128  # global vol
    header[49] = 48  # mix vol
    header[50] = speed
    header[51] = tempo
    header[52] = 128  # pan separation
    for ch in range(64):
        header[0x40 + ch] = 32 if ch < nch else 32 + 128
        header[0x80 + ch] = 64

    after_header = 0xC0 + ord_num + ins_num * 4 + smp_num * 4 + pat_num * 4
    message_off = after_header + len(history) + len(midi)
    ins_hdr_off = message_off + len(message)
    smp_hdr_off = ins_hdr_off + ins_num * INSTRUMENT_SIZE
    pat_off = smp_hdr_off + smp_num * 80

    ins_ptrs = bytearray()
    ins_headers = bytearray()
    # Envelope nodes are counted in IT ticks, not SID frames. Those rates are
    # the same until module_timing doubles speed and tempo for a fine grid,
    # and without this every envelope there would run twice as fast.
    envelope_hz = tempo * 2.0 / 5.0
    for index, voice in enumerate(plan.voices):
        inst = voice.base
        nodes, sustain_node = adsr_envelope_nodes(
            inst.attack, inst.decay, inst.sustain, inst.release, envelope_hz
        )
        ins_ptrs.extend((ins_hdr_off + len(ins_headers)).to_bytes(4, "little"))
        ins_headers.extend(
            _instrument_header(
                voice.name or f"inst{index + 1}",
                sample_for_voice[index],
                _envelope(nodes, sustain_node),
                filtered=bool(inst.filtered and use_filter),
            )
        )

    pat_ptrs = bytearray()
    pattern_blob = bytearray()
    for packed in packed_blocks:
        pat_ptrs.extend((pat_off + len(pattern_blob)).to_bytes(4, "little"))
        pattern_blob.extend(packed)
    sample_data_off = pat_off + len(pattern_blob)

    smp_ptrs = bytearray()
    smp_headers = bytearray()
    data_blob = bytearray()
    cursor = sample_data_off
    for index, slot in enumerate(slots):
        smp_ptrs.extend((smp_hdr_off + len(smp_headers)).to_bytes(4, "little"))
        smp_headers.extend(
            _sample_header(
                slot.name or f"smp{index + 1}",
                len(slot.pcm),
                cursor,
                slot.c5_speed,
                slot.loop_start,
            )
        )
        data_blob.extend(slot.pcm)
        cursor += len(slot.pcm)

    header[0x36:0x38] = len(message).to_bytes(2, "little")
    header[0x38:0x3C] = message_off.to_bytes(4, "little")

    data = (
        bytes(header)
        + orders
        + bytes(ins_ptrs)
        + bytes(smp_ptrs)
        + bytes(pat_ptrs)
        + history
        + midi
        + message
        + bytes(ins_headers)
        + bytes(smp_headers)
        + bytes(pattern_blob)
        + bytes(data_blob)
        + stpm
    )
    source = _source_features(ir)
    report = GenerationReport(
        use_pwm=use_pwm,
        use_filter=use_filter,
        ticks_per_row=ir.timing.ticks_per_row,
        grid_reason=grid_reason or "IR ticks_per_row",
        grid_warning=grid_warning,
        speed=speed,
        tempo=tempo,
        timing_doubled=raw_speed == 1 and speed > 1,
        size_bytes=len(data),
        note_ons=source["note_ons"],
        instruments=ins_num,
        samples=smp_num,
        patterns=pat_num,
        orders=len(order_indices),
        loop_trim_rows=loop_trim_rows,
        dump_ticks=int(ir.source.get("ticks") or 0),
        content_ticks=_content_ticks(ir, rows, loop_trim_rows),
        frame_hz=float(ir.timing.frame_hz),
        waveforms=source["waveforms"],
        baked=plan.baked,
        baked_bytes=plan.baked_bytes,
        pwm_in_source=source["pwm"],
        static_duty=plan.static_duty,
        sync_ring=plan.sync_ring,
        sync_ring_in_source=source["sync_ring"],
        arpeggio=_grid_has(grid, IT_EFFECT_J),
        legato=_grid_has(grid, IT_EFFECT_G),
        waveform_switch=source["waveform_switch"],
        filter_in_source=_chip_filter_used(ir),
        filter_automation=use_filter,
        filter_zxx=_grid_has(grid, IT_EFFECT_Z),
        filter_lp=source["filter_lp"],
        filter_hp=source["filter_hp"],
        filter_resonance=source["filter_resonance"],
        mixer_volume=_grid_has(grid, IT_EFFECT_V),
        mute=_grid_has(grid, IT_EFFECT_M),
        sample_budget_bytes=budget_bytes,
        budget_hit=plan.budget_hit,
        skipped_budget=plan.skipped_budget,
        instrument_limit_hit=plan.instrument_limit_hit,
        skipped_instrument_limit=plan.skipped_instrument_limit,
    )
    return ModuleWrite(data=data, report=report)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write a .it module from music IR JSON")
    parser.add_argument("ir", type=Path)
    parser.add_argument("out", type=Path)
    parser.add_argument(
        "--use-pwm",
        action="store_true",
        help="bake moving pulse-width sweeps into private 22050 Hz samples "
        "(default: energy-matched static duty loops)",
    )
    parser.add_argument(
        "--use-filter",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="write the SID filter timeline as mid-row Zxx cutoff/resonance "
        "and LP/HP macros (default: on). Use --no-use-filter to skip; mute "
        "and master volume still apply",
    )
    parser.add_argument(
        "--sample-budget-mb",
        type=float,
        default=DEFAULT_SAMPLE_BUDGET_MB,
        help="ceiling for baked sample data with --use-pwm; past it, notes "
        "with moving pulse width fall back to a static loop",
    )
    args = parser.parse_args(argv)
    try:
        ir = load(args.ir)
        result = write_module(
            ir,
            args.sample_budget_mb,
            use_pwm=args.use_pwm,
            use_filter=args.use_filter,
        )
    except (OSError, IRError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_bytes(result.data)
    print(f"wrote {args.out} ({len(result.data)} bytes)")
    print(format_generation_report(result.report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
