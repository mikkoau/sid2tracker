"""reSID-shaped SID oscillator, rendering 12-bit DAC values.

Sync, ring modulation, and pulse-width modulation cannot be written as tracker
effects, so they have to be rendered into sample data. This module is the
oscillator half of that: it reproduces the accumulator arithmetic of reSID
(VICE `src/resid/wave.cc`) closely enough that the resulting spectrum matches,
without emulating the chip cycle by cycle.

Deliberate simplifications:

- The accumulator advances once per output sample rather than once per chip
  clock, so a sync reset lands on a sample boundary. `oversample` trades speed
  for accuracy where it matters.
- Combined tonal waveforms default to the bitwise AND of the ideal shapes.
  Optional reSID OSC3 tables (6581/8580), generated locally, replace that AND.
"""

from __future__ import annotations

from collections.abc import Callable
from fractions import Fraction

ACC_MASK = 0xFFFFFF
ACC_MSB = 0x800000
# The LFSR is clocked when accumulator bit 19 rises, so 16 times per
# oscillator period, every 0x100000 of accumulation starting at 0x80000.
NOISE_PERIOD = 1 << 20
NOISE_PHASE = 1 << 19
NOISE_SEED = 0x7FFFFF
NOISE_OUTPUT_BITS = (22, 20, 16, 13, 11, 7, 4, 2)
# A ratio is snapped to a fraction no finer than this. The denominator is the
# loop length in carrier periods, so an unbounded one would mean an unbounded
# sample.
MAX_RATIO_DENOMINATOR = 16


def ratio_cycles(ratio: float | None, limit: int = MAX_RATIO_DENOMINATOR) -> int:
    """Carrier periods before a sync or ring pair repeats itself.

    With a modulator-to-carrier ratio of p/q in lowest terms, both oscillators
    return to their starting phase after q carrier periods, so that is the
    shortest loop that does not click.
    """
    if not ratio or ratio <= 0:
        return 1
    return Fraction(ratio).limit_denominator(limit).denominator


def snapped_ratio(ratio: float | None, limit: int = MAX_RATIO_DENOMINATOR) -> float:
    """The ratio actually rendered, after snapping to a loopable fraction."""
    if not ratio or ratio <= 0:
        return 0.0
    return float(Fraction(ratio).limit_denominator(limit))


def normalize_chip_model(model: str | None) -> str:
    """`MOS8580` when the header says so, else `MOS6581`."""
    if model and str(model).startswith("MOS8580"):
        return "MOS8580"
    return "MOS6581"


def _load_table_for() -> Callable[[str, str], bytes] | None:
    """OSC3 tables if generated locally; None keeps bitwise-AND combining."""
    try:
        from .resid_wave_tables import table_for
    except ImportError:
        return None
    return table_for


table_for = _load_table_for()


def combined_wave_tables_loaded() -> bool:
    """True when the optional reSID OSC3 table module is present."""
    return table_for is not None


def _noise_clocks(previous: int, current: int) -> int:
    """How many bit-19 rises the accumulator passed through this step.

    Testing the bit before and after would miss rises whenever a step is wider
    than 0x100000, which silences noise at ordinary pitches. Counting
    crossings keeps the LFSR at its real rate however coarse the step is.
    """
    if current < previous:
        current += ACC_MASK + 1
    return (current - NOISE_PHASE) // NOISE_PERIOD - (
        previous - NOISE_PHASE
    ) // NOISE_PERIOD


def _noise_value(shift: int) -> int:
    value = 0
    for index, bit in enumerate(NOISE_OUTPUT_BITS):
        value |= ((shift >> bit) & 1) << (7 - index)
    return value * 4095 // 255


def _triangle(acc: int, ring_acc: int) -> int:
    msb = (acc ^ ring_acc) & ACC_MSB
    return ((~acc if msb else acc) >> 11) & 0xFFF


def _saw(acc: int) -> int:
    return (acc >> 12) & 0xFFF


def _pulse(acc: int, pulse_width: int) -> int:
    return 0xFFF if (acc >> 12) >= pulse_width else 0


def render(
    wave_bits: int,
    samples: int,
    step: int,
    *,
    pulse_width: int = 2048,
    pw_timeline: list[int] | None = None,
    samples_per_frame: int = 0,
    ratio: float | None = None,
    sync: bool = False,
    ring: bool = False,
    oversample: int = 1,
    chip_model: str | None = None,
) -> list[int]:
    """Render `samples` 12-bit DAC values.

    `step` is the carrier accumulator increment per output sample, so the
    caller sets pitch by choosing it: a full cycle is 0x1000000 steps.
    `pw_timeline` holds one 12-bit pulse width per player frame, advancing
    every `samples_per_frame` samples, because the SID's pulse width is a
    register written by the driver and not a function of the oscillator.

    Noise is clocked from accumulator bit 19, so a `step` that is an exact
    power of two can leave that bit static and freeze the LFSR. Pure noise is
    better rendered at its own rate; this path exists for combined waveforms.
    """
    oversample = max(1, oversample)
    sub_step = step // oversample
    modulating = bool(ratio) and (sync or ring)
    mod_step = int(round(sub_step * ratio)) if modulating else 0
    model = normalize_chip_model(chip_model)

    acc = 0
    mod_acc = 0
    shift = NOISE_SEED
    width = pulse_width
    out: list[int] = []

    for index in range(samples):
        if pw_timeline and samples_per_frame:
            frame = index // samples_per_frame
            width = pw_timeline[min(frame, len(pw_timeline) - 1)]
        total = 0
        for _ in range(oversample):
            if modulating:
                previous_mod = mod_acc
                mod_acc = (mod_acc + mod_step) & ACC_MASK
                mod_rising = bool(~previous_mod & mod_acc & ACC_MSB)
            else:
                mod_rising = False
            previous = acc
            acc = (acc + sub_step) & ACC_MASK
            for _ in range(_noise_clocks(previous, acc)):
                feedback = ((shift >> 22) ^ (shift >> 17)) & 1
                shift = ((shift << 1) & 0x7FFFFF) | feedback
            if sync and mod_rising:
                acc = 0
            total += _sample(
                wave_bits,
                acc,
                mod_acc if ring else 0,
                width,
                shift,
                model,
            )
        out.append(total // oversample)
    return out


def _sample(
    wave_bits: int,
    acc: int,
    ring_acc: int,
    pulse_width: int,
    shift: int,
    chip_model: str,
) -> int:
    """One 12-bit oscillator sample.

    Single waveforms match reSID. Combined tonal mixes use OSC3 tables when
    generated locally; otherwise they AND the ideal components.
    """
    selector = 0
    if wave_bits & 0x10:
        selector |= 0x1
    if wave_bits & 0x20:
        selector |= 0x2
    if wave_bits & 0x40:
        selector |= 0x4
    if wave_bits & 0x80:
        selector |= 0x8

    if selector == 0x0:
        return 0
    if selector == 0x1:
        return _triangle(acc, ring_acc)
    if selector == 0x2:
        return _saw(acc)
    if selector == 0x4:
        return _pulse(acc, pulse_width)
    if selector == 0x8:
        return _noise_value(shift)

    lookup = table_for
    if lookup is not None:
        # Classic reSID: noise plus any other waveform is silent.
        if selector & 0x8:
            return 0
        saw = _saw(acc)
        tri = _triangle(acc, ring_acc)
        pulse = _pulse(acc, pulse_width)
        if selector == 0x3:
            return lookup(chip_model, "ST")[saw] << 4
        if selector == 0x5:
            return (lookup(chip_model, "PT")[tri >> 1] << 4) & pulse
        if selector == 0x6:
            return (lookup(chip_model, "PS")[saw] << 4) & pulse
        if selector == 0x7:
            return (lookup(chip_model, "PST")[saw] << 4) & pulse
        return 0

    components: list[int] = []
    if selector & 0x1:
        components.append(_triangle(acc, ring_acc))
    if selector & 0x2:
        components.append(_saw(acc))
    if selector & 0x4:
        components.append(_pulse(acc, pulse_width))
    if selector & 0x8:
        components.append(_noise_value(shift))
    if not components:
        return 0
    value = components[0]
    for component in components[1:]:
        value &= component
    return value
