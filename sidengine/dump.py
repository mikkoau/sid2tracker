"""Run a caller-driven PSID or IRQ-driven RSID and record SID register changes."""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

Progress = Callable[[int, int], None]

from .cpu6502 import CPU6502, CPUError, Memory
from .header import SidHeaderError, parse_sid_file
from .irq_harness import (
    IDLE_ADDR,
    HarnessMemory,
    IrqHarness,
    install_kernal_stubs,
)
from .ir import (
    Channel,
    Event,
    FilterPoint,
    IRError,
    Instrument,
    MusicIR,
    Timing,
    save,
)

PAL_CLOCK = 985_248
NTSC_CLOCK = 1_022_727
SID_REG_COUNT = 25
# KERNAL / PSID default CIA 1 timer A latch for ~60 Hz.
CIA1_TIMER_A_LO = 0xDC04
CIA1_TIMER_A_HI = 0xDC05
CIA_60HZ_LATCH_PAL = 0x4025
CIA_60HZ_LATCH_NTSC = 0x4295
# Digi/speech init that spins on CIA timer B must not hang the dump forever.
# Arabian Nights song 1 init is ~84k; digi paths spin far longer.
DEFAULT_INIT_IRQ_CYCLES = 500_000
# After init, bound a single IRQ handler so a runaway cannot stall a frame.
MAX_IRQ_CYCLES = 200_000


class DigiSkipError(SidHeaderError):
    """Raised when a subtune looks like volume-sample / speech, not notes."""


def _bank_for(address: int) -> int:
    if address < 0xA000:
        return 0x37
    if address < 0xD000:
        return 0x36
    if address >= 0xE000:
        return 0x35
    return 0x34


def _io_visible(port: int) -> bool:
    low = port & 7
    return bool((low & 4) and (low & 3))


class SidRecorder:
    def __init__(self) -> None:
        self.memory: Memory | None = None
        self.registers = bytearray(SID_REG_COUNT)
        self.writes: list[list[int]] = []

    def on_write(self, address: int, value: int) -> None:
        if self.memory is None or not _io_visible(self.memory.data[1]):
            return
        if not 0xD400 <= address <= 0xD7FF:
            return
        offset = (address - 0xD400) & 0x1F
        if offset >= SID_REG_COUNT:
            return
        # Preserve transient gate-off/gate-on writes but omit true duplicates.
        if self.registers[offset] == value:
            return
        self.registers[offset] = value
        self.writes.append([offset, value])

    def take(self) -> list[list[int]]:
        writes = self.writes
        self.writes = []
        return writes


def _seed_cia_timer_a(memory: Memory, clock: str) -> None:
    """PSID default: CIA 1 timer A latched for 60 Hz before init runs."""
    latch = CIA_60HZ_LATCH_NTSC if clock == "NTSC" else CIA_60HZ_LATCH_PAL
    memory.data[CIA1_TIMER_A_LO] = latch & 0xFF
    memory.data[CIA1_TIMER_A_HI] = (latch >> 8) & 0xFF


def _cia_timer_a_hz(memory: Memory, clock: str) -> float:
    """Play rate from CIA 1 timer A latch after init (or the 60 Hz default)."""
    latch = memory.data[CIA1_TIMER_A_LO] | (memory.data[CIA1_TIMER_A_HI] << 8)
    chip = NTSC_CLOCK if clock == "NTSC" else PAL_CLOCK
    return chip / (latch + 1)


def _cia_latch_for(clock: str) -> int:
    return CIA_60HZ_LATCH_NTSC if clock == "NTSC" else CIA_60HZ_LATCH_PAL


def play_rate_hz(path: Path, subtune: int | None = None) -> float:
    """Vblank or post-init CIA timer A rate for this subtune.

    The PSID speed bit only says CIA vs vblank. Many tunes reprogram the
    latch in init (Paperboy: ~150 Hz, ~100 Hz, or PAL 50 Hz), so the rate
    must be read from memory after init rather than assumed as 60 Hz.
    """
    info = parse_sid_file(path)
    if info["play_address"] == 0 or info["magic"] != "PSID":
        return _prepare_irq_sid(path, subtune)[6]
    return _prepare_psid(path, subtune)[5]


def _reject_unsupported(info: dict[str, Any]) -> None:
    if info["flags"]["mus_player"]:
        raise SidHeaderError("Compute! MUS payload needs an external player")
    if info["second_sid_address"] or info["third_sid_address"]:
        raise SidHeaderError("2SID/3SID is not converted")


def _load_payload(path: Path, info: dict[str, Any]) -> tuple[bytes, int]:
    data = path.read_bytes()
    start = info["data_offset"] + (2 if info["load_address_in_payload"] else 0)
    payload = data[start:]
    load = info["load_address"]
    if load + len(payload) > 0x10000:
        raise SidHeaderError(
            f"payload ${load:04X}+{len(payload)} exceeds 64K address space"
        )
    return payload, load


def _clock_frame(info: dict[str, Any]) -> tuple[str, float]:
    clock_name = info["flags"]["clock"]
    if clock_name == "NTSC":
        return "NTSC", 59.826
    return "PAL", 50.125


def _prepare_psid(
    path: Path, subtune: int | None = None
) -> tuple[
    dict[str, Any], Memory, CPU6502, SidRecorder, str, float, int, list, int
]:
    info = parse_sid_file(path)
    if info["magic"] != "PSID":
        raise SidHeaderError("caller-driven path supports PSID only")
    if info["play_address"] == 0:
        raise SidHeaderError("playAddress=0 needs the IRQ/CIA/VIC harness")
    _reject_unsupported(info)

    song = info["start_song"] if subtune is None else subtune
    if not 1 <= song <= info["songs"]:
        raise SidHeaderError(f"subtune {song} outside 1-{info['songs']}")

    payload, load = _load_payload(path, info)
    recorder = SidRecorder()
    memory = Memory(recorder.on_write)
    recorder.memory = memory
    memory.data[load : load + len(payload)] = payload
    memory.data[0] = 0x2F
    memory.data[1] = 0x37

    clock, frame_hz = _clock_frame(info)
    memory.data[0x02A6] = 0 if clock == "NTSC" else 1
    _seed_cia_timer_a(memory, clock)
    if info["song_speed"][str(song)] == "cia":
        frame_hz = 60.0

    cpu = CPU6502(memory)
    memory.data[1] = _bank_for(info["init_address"])
    init_cycles = cpu.call(info["init_address"], a=song - 1, max_cycles=1_000_000)
    init_writes = recorder.take()
    if info["song_speed"][str(song)] == "cia":
        frame_hz = _cia_timer_a_hz(memory, clock)

    return (
        info,
        memory,
        cpu,
        recorder,
        clock,
        frame_hz,
        init_cycles,
        init_writes,
        song,
    )


def _append_frame_writes(
    frames_by_tick: dict[int, list[list[int]]], tick: int, writes: list[list[int]]
) -> None:
    if not writes:
        return
    bucket = frames_by_tick.setdefault(tick, [])
    bucket.extend(writes)


def _looks_like_digi(init_writes: list, frames: list[dict[str, Any]]) -> bool:
    """True when the dump is volume-register noise without musical gates."""
    volume = 0
    other = 0
    controls: list[int] = []
    for offset, value in init_writes:
        if offset == 24:
            volume += 1
        else:
            other += 1
            if offset in (4, 11, 18):
                controls.append(value)
    for frame in frames[:64]:
        for offset, value in frame["writes"]:
            if offset == 24:
                volume += 1
            else:
                other += 1
                if offset in (4, 11, 18):
                    controls.append(value)
    if volume < 16:
        return False
    if other and volume / (volume + other) < 0.8:
        return False
    # Digi toggles $D418 and rarely raises a waveform+gate note.
    gated = any(ctrl & 0xF1 == 0x41 or ctrl & 0xF1 == 0x11 for ctrl in controls)
    return not gated


def _irq_handler_timeout(cpu: CPU6502, max_handler_cycles: int) -> CPUError:
    """200k-cycle IRQ cap. Tight JMP $pc is a 256-byte demo lock, not a miss."""
    pc = cpu.pc
    extra = ""
    mem = cpu.mem.data
    if mem[pc] == 0x4C:
        dest = mem[(pc + 1) & 0xFFFF] | (mem[(pc + 2) & 0xFFFF] << 8)
        if dest == pc:
            extra = (
                "; infinite JMP (packed demo lock or non-returning init). "
                "Not a missed $EA31/$EA7E ack"
            )
    return CPUError(
        f"IRQ handler exceeded {max_handler_cycles} cycles "
        f"(pc=${pc:04X}){extra}"
    )


def _step_with_irq(
    cpu: CPU6502,
    harness: IrqHarness,
    max_handler_cycles: int = MAX_IRQ_CYCLES,
) -> int:
    """Execute one instruction, advance chips, and enter IRQ if pending."""
    before = cpu.cycles
    cpu.step()
    harness.advance(cpu.cycles - before)
    # Cap re-entries so an unacked source cannot spin forever inside one step.
    for _ in range(8):
        if not (harness.irq_pending() and not (cpu.p & 0x04)):
            break
        sp_before = cpu.sp
        if not cpu.trigger_irq():
            break
        handler_start = cpu.cycles
        while cpu.sp != sp_before:
            before_h = cpu.cycles
            cpu.step()
            harness.advance(cpu.cycles - before_h)
            if cpu.cycles - handler_start > max_handler_cycles:
                raise _irq_handler_timeout(cpu, max_handler_cycles)
    return cpu.cycles - before


def _cpu_in_idle_spin(cpu: CPU6502) -> bool:
    """True when init returned and the CPU is in the harness idle JMP loop."""
    return cpu.pc in (IDLE_ADDR, IDLE_ADDR + 2) and not (cpu.p & 0x04)


def _run_irq_handler(
    cpu: CPU6502,
    harness: IrqHarness,
    max_handler_cycles: int = MAX_IRQ_CYCLES,
) -> None:
    if not (harness.irq_pending() and not (cpu.p & 0x04)):
        return
    if not cpu.trigger_irq():
        return
    sp_before = cpu.sp
    handler_start = cpu.cycles
    while cpu.sp != sp_before:
        before = cpu.cycles
        cpu.step()
        harness.advance(cpu.cycles - before)
        if cpu.cycles - handler_start > max_handler_cycles:
            raise _irq_handler_timeout(cpu, max_handler_cycles)


def _call_init_with_irq(
    cpu: CPU6502,
    harness: IrqHarness,
    address: int,
    a: int,
    max_cycles: int = DEFAULT_INIT_IRQ_CYCLES,
) -> int:
    sentinel = 0xFFFF
    old_cycles = cpu.cycles
    cpu.a = a & 0xFF
    cpu.x = 0
    cpu.y = 0
    cpu.sp = 0xFF
    return_address = sentinel - 1
    cpu._push(return_address >> 8)
    cpu._push(return_address & 0xFF)
    cpu.pc = address & 0xFFFF
    while cpu.pc != sentinel:
        _step_with_irq(cpu, harness)
        if cpu.cycles - old_cycles > max_cycles:
            raise DigiSkipError(
                "digi/speech or non-returning init "
                f"(exceeded {max_cycles} cycles at pc=${cpu.pc:04X})"
            )
    return cpu.cycles - old_cycles


def _prepare_irq_sid(
    path: Path,
    subtune: int | None = None,
    init_cycles_budget: int = DEFAULT_INIT_IRQ_CYCLES,
) -> tuple[
    dict[str, Any],
    HarnessMemory,
    CPU6502,
    SidRecorder,
    IrqHarness,
    str,
    float,
    int,
    list,
    int,
]:
    info = parse_sid_file(path)
    _reject_unsupported(info)
    if info["play_address"] != 0 and info["magic"] == "PSID":
        raise SidHeaderError("caller-driven PSID should use run_psid")

    song = info["start_song"] if subtune is None else subtune
    if not 1 <= song <= info["songs"]:
        raise SidHeaderError(f"subtune {song} outside 1-{info['songs']}")

    payload, load = _load_payload(path, info)
    clock, frame_hz = _clock_frame(info)
    harness = IrqHarness(clock)
    latch = _cia_latch_for(clock)
    harness.reset_rsid(latch)

    recorder = SidRecorder()
    memory = HarnessMemory(harness, recorder.on_write)
    recorder.memory = memory
    memory.data[load : load + len(payload)] = payload
    memory.data[0] = 0x2F
    memory.data[1] = 0x37
    memory.data[0x02A6] = 0 if clock == "NTSC" else 1
    # Keep a RAM mirror of the CIA latch for post-init rate reads.
    memory.data[CIA1_TIMER_A_LO] = latch & 0xFF
    memory.data[CIA1_TIMER_A_HI] = (latch >> 8) & 0xFF
    install_kernal_stubs(memory)

    cpu = CPU6502(memory)
    # RSID keeps bank $37 for init. PSID play=0 still uses the address bank.
    if info["magic"] == "PSID":
        memory.data[1] = _bank_for(info["init_address"])
    init_cycles = _call_init_with_irq(
        cpu,
        harness,
        info["init_address"],
        a=song - 1,
        max_cycles=init_cycles_budget,
    )
    init_writes = recorder.take()

    # Prefer VIC vblank when raster IRQ was left enabled; else CIA1 rate.
    if harness.d01a & 0x01:
        frame_hz = 59.826 if clock == "NTSC" else 50.125
    elif harness.cra & 0x01 and harness.icr_mask & 0x01:
        frame_hz = (NTSC_CLOCK if clock == "NTSC" else PAL_CLOCK) / (
            harness.ta_latch + 1
        )

    return (
        info,
        memory,
        cpu,
        recorder,
        harness,
        clock,
        frame_hz,
        init_cycles,
        init_writes,
        song,
    )


def run_psid(
    path: Path,
    ticks: int,
    subtune: int | None = None,
    on_progress: Progress | None = None,
) -> dict[str, Any]:
    info, memory, cpu, recorder, clock, frame_hz, init_cycles, init_writes, song = (
        _prepare_psid(path, subtune)
    )

    frames: list[dict[str, Any]] = []
    max_play_cycles = 0
    for tick in range(ticks):
        memory.data[1] = _bank_for(info["play_address"])
        used = cpu.call(info["play_address"], max_cycles=200_000)
        max_play_cycles = max(max_play_cycles, used)
        writes = recorder.take()
        if writes:
            frames.append({"tick": tick, "writes": writes})
        if on_progress is not None:
            on_progress(tick + 1, ticks)

    return {
        "format": "c64-sid-register-dump",
        "version": 1,
        "source": str(path),
        "subtune": song,
        "songs": info["songs"],
        "title": info["name"],
        "author": info["author"],
        "released": info["released"],
        "clock": clock,
        "frame_hz": frame_hz,
        "sid_model": info["flags"]["sid_model"],
        "load_address": info["load_address"],
        "init_address": info["init_address"],
        "play_address": info["play_address"],
        "ticks": ticks,
        "init_cycles": init_cycles,
        "max_play_cycles": max_play_cycles,
        "init_writes": init_writes,
        "frames": frames,
        "execution": "caller",
    }


def run_irq_sid(
    path: Path,
    ticks: int,
    subtune: int | None = None,
    init_cycles_budget: int = DEFAULT_INIT_IRQ_CYCLES,
    on_progress: Progress | None = None,
) -> dict[str, Any]:
    """Execute an RSID / play=0 tune under VIC+CIA1 IRQs; bucket writes per frame."""
    (
        info,
        memory,
        cpu,
        recorder,
        harness,
        clock,
        frame_hz,
        init_cycles,
        init_writes,
        song,
    ) = _prepare_irq_sid(path, subtune, init_cycles_budget=init_cycles_budget)

    frames_by_tick: dict[int, list[list[int]]] = {}
    max_play_cycles = 0
    cpu.pc = IDLE_ADDR
    cpu.p &= ~0x04  # CLI so installed IRQs can run

    # Drain any SID writes that arrived in the last init IRQ into frame 0.
    _append_frame_writes(frames_by_tick, 0, recorder.take())

    target_cycles = ticks * harness.cycles_per_frame
    idle_start = harness.total_cycles
    last_progress = -1

    def _maybe_progress() -> None:
        nonlocal last_progress
        if on_progress is None:
            return
        done = min(
            ticks,
            (harness.total_cycles - idle_start) // harness.cycles_per_frame,
        )
        if done != last_progress:
            last_progress = done
            on_progress(done, ticks)

    while harness.total_cycles - idle_start < target_cycles:
        frame_before = harness.frame_index
        tick = min(frame_before, ticks - 1)
        remaining = target_cycles - (harness.total_cycles - idle_start)

        if _cpu_in_idle_spin(cpu) and not harness.irq_pending() and remaining > 0:
            # Skip idle cycles until the next IRQ. Stepping every chip cycle
            # here would make long HVSC songs take minutes.
            skip = min(harness.cycles_until_irq(), remaining)
            if skip > 1:
                harness.advance(skip)
                cpu.cycles += skip
                _append_frame_writes(frames_by_tick, tick, recorder.take())
                _maybe_progress()
                continue

        if harness.irq_pending() and not (cpu.p & 0x04):
            before = cpu.cycles
            _run_irq_handler(cpu, harness)
            max_play_cycles = max(max_play_cycles, cpu.cycles - before)
            _append_frame_writes(frames_by_tick, tick, recorder.take())
            _maybe_progress()
            continue

        before = cpu.cycles
        _step_with_irq(cpu, harness)
        used = cpu.cycles - before
        max_play_cycles = max(max_play_cycles, used)
        _append_frame_writes(frames_by_tick, tick, recorder.take())
        _maybe_progress()

    frames = [
        {"tick": tick, "writes": writes}
        for tick, writes in sorted(frames_by_tick.items())
        if 0 <= tick < ticks and writes
    ]
    if _looks_like_digi(init_writes, frames):
        raise DigiSkipError("digi/speech subtune (volume-sample pattern)")

    return {
        "format": "c64-sid-register-dump",
        "version": 1,
        "source": str(path),
        "subtune": song,
        "songs": info["songs"],
        "title": info["name"],
        "author": info["author"],
        "released": info["released"],
        "clock": clock,
        "frame_hz": frame_hz,
        "sid_model": info["flags"]["sid_model"],
        "load_address": info["load_address"],
        "init_address": info["init_address"],
        "play_address": info["play_address"],
        "ticks": ticks,
        "init_cycles": init_cycles,
        "max_play_cycles": max_play_cycles,
        "init_writes": init_writes,
        "frames": frames,
        "execution": "irq",
    }


def run_sid(
    path: Path,
    ticks: int,
    subtune: int | None = None,
    init_cycles_budget: int = DEFAULT_INIT_IRQ_CYCLES,
    on_progress: Progress | None = None,
) -> dict[str, Any]:
    """Dump a .sid using caller-driven play or the IRQ harness as required."""
    info = parse_sid_file(path)
    if info["magic"] == "PSID" and info["play_address"] != 0:
        return run_psid(path, ticks, subtune, on_progress=on_progress)
    return run_irq_sid(
        path,
        ticks,
        subtune,
        init_cycles_budget=init_cycles_budget,
        on_progress=on_progress,
    )


def _midi_note(frequency_word: int, clock_name: str) -> int | None:
    if frequency_word == 0:
        return None
    clock = NTSC_CLOCK if clock_name == "NTSC" else PAL_CLOCK
    hz = frequency_word * clock / 16_777_216
    note = round(69 + 12 * math.log2(hz / 440.0))
    return max(0, min(119, note))


def _waveform(ctrl: int) -> str:
    # Keep raw ctrl too. This name only selects the first tracker sample.
    if ctrl & 0x80:
        return "noise"
    if ctrl & 0x40:
        return "pulse"
    if ctrl & 0x20:
        return "saw"
    return "triangle"


def _waveform_label(ctrl: int) -> str:
    names = [
        name
        for bit, name in (
            (0x10, "triangle"),
            (0x20, "saw"),
            (0x40, "pulse"),
            (0x80, "noise"),
        )
        if ctrl & bit
    ]
    return "+".join(names) or "none"


# A fast cycle narrower than this is vibrato, not a chord.
ARPEGGIO_MIN_SPAN = 2
# Jxy carries two offsets in one nibble each.
ARPEGGIO_MAX_SPAN = 15
# Wide SID arpeggios commonly use octave harmonics that Jxy cannot encode.
# Look across this many player ticks to recover their stable lowest pitch.
WIDE_ARPEGGIO_WINDOW = 8


def _held_note_events(
    start: int,
    instrument: int,
    frequency: int,
    segments: list[tuple[int, int]],
    end_tick: int,
    ticks_per_row: int,
    modulator_ratio: float | None = None,
    pw_points: list[list[int]] | None = None,
    extra: dict[str, Any] | None = None,
) -> list[Event]:
    """Turn one gate-held SID voice into row-local note and pitch events."""
    if not segments:
        return []
    payload = {"sid_frequency": frequency}
    if extra:
        payload.update(extra)

    def event(
        tick: int,
        kind: str,
        note: int | None = None,
        arpeggio: list[int] | None = None,
    ) -> Event:
        # Sync/ring ratio and pulse-width movement describe the sounding
        # oscillator, so they ride on the onset that starts it.
        onset = kind == "note_on"
        return Event(
            tick=tick,
            type=kind,
            note=note,
            instrument=instrument if kind in ("note_on", "pitch") else None,
            arpeggio=arpeggio or [],
            modulator_ratio=modulator_ratio if onset else None,
            pw_points=list(pw_points or []) if onset else [],
            extra=dict(payload),
        )

    # Expand the sparse changes over this held span. A longest HVSC subtune is
    # only tens of thousands of player ticks, so this is both simpler and
    # smaller than the generated IT pattern.
    notes: list[int] = []
    index = 0
    current = segments[0][1]
    for tick in range(start, end_tick):
        while index + 1 < len(segments) and segments[index + 1][0] <= tick:
            index += 1
            current = segments[index][1]
        notes.append(current)

    states: list[tuple[int, int, tuple[int, ...]]] = []
    first_row = start // ticks_per_row
    last_row = max(first_row, (end_tick - 1) // ticks_per_row)
    # Octave-stack roots stick until the melody returns near them. Looking
    # ahead into the next chord (Cauldron II) or treating a sustained high
    # harmonic as the new base both turn the stack into a wild pitch slide.
    wide_root: int | None = None
    for row in range(first_row, last_row + 1):
        row_start = max(start, row * ticks_per_row)
        row_end = min(end_tick, (row + 1) * ticks_per_row)
        values = notes[row_start - start : row_end - start]
        distinct = sorted(set(values))
        span = distinct[-1] - distinct[0]
        position = row_start - start
        # Prefer a trailing window so the next cycle's lower root cannot pull
        # this row early. Include the rest of the current row for cycles that
        # straddle the row boundary.
        look_start = max(0, position - WIDE_ARPEGGIO_WINDOW)
        look_end = min(len(notes), row_end - start)
        look_values = notes[look_start:look_end]
        look_span = max(look_values) - min(look_values)
        if look_span > ARPEGGIO_MAX_SPAN:
            # IT Jxy cannot express octave-stacked cycles. Their lowest pitch
            # is normally the musical root. Never raise wide_root inside one
            # gate: after the true root leaves the window the mid harmonic
            # becomes min(look) and would climb 34 -> 59 -> 84.
            candidate = min(look_values)
            if wide_root is None or candidate <= wide_root:
                wide_root = candidate
            base = wide_root
            arp = ()
        elif (
            wide_root is not None
            and distinct[-1] - wide_root > ARPEGGIO_MAX_SPAN
        ):
            # High side of the stack still ringing after the low root left the
            # window. Keep the root rather than adopting the harmonic.
            base = wide_root
            arp = ()
        elif span < ARPEGGIO_MIN_SPAN:
            base = distinct[0]
            if (
                wide_root is not None
                and abs(base - wide_root) <= ARPEGGIO_MAX_SPAN
            ):
                wide_root = None
            arp = ()
        elif len(distinct) <= 3 and span <= ARPEGGIO_MAX_SPAN:
            base = distinct[0]
            wide_root = None
            offsets = tuple(note - base for note in distinct[1:])
            if offsets:
                while len(offsets) < 2:
                    offsets += (offsets[0],)
                arp = (0, offsets[0], offsets[1])
            else:
                arp = ()
        else:
            # More than three local pitches cannot fit Jxy. Keep the perceived
            # root instead of retriggering once for every modulation tick.
            candidate = min(look_values)
            if wide_root is None or candidate <= wide_root:
                wide_root = candidate
            base = wide_root
            arp = ()
        states.append((row_start, base, arp))

    out: list[Event] = []
    previous: tuple[int, tuple[int, ...]] | None = None
    for tick, base, arp in states:
        state = (base, arp)
        if previous is None:
            out.append(event(start, "note_on", base, list(arp)))
        elif state[0] != previous[0]:
            # A pitch event changes an already sounding SID oscillator. It is
            # legato and must not restart the IT instrument envelope.
            out.append(event(tick, "pitch", base, list(arp)))
        elif arp:
            # Jxy lasts for one tracker row, so repeat it without another note.
            out.append(event(tick, "effect", arpeggio=list(arp)))
        previous = state
    return out


def _collapse_gate_group(
    notes: list[dict[str, Any]],
    release_waveform: str | None,
    ticks_per_row: int,
) -> list[dict[str, Any]]:
    """Keep one onset per tracker row out of a gate group's waveform table.

    A wavetable instrument spends its first frames on something other than the
    note: a sub-bass triangle click, a frame of noise, then the waveform the
    note is actually made of. Those frames land on one tracker row as soon as
    the dump is quantized, and taking the last one turns every bass note into
    a noise hit.

    The waveform still selected when the gate falls is the one that rings
    through the release, so that onset is the note and the others are attack
    transients. At one tick per row nothing shares a row and the whole table
    survives.
    """
    if len(notes) <= 1:
        return notes
    body = len(notes) - 1
    for index in range(len(notes) - 1, -1, -1):
        if notes[index]["waveform"] == release_waveform:
            body = index
            break
    body_row = notes[body]["start"] // ticks_per_row
    kept: list[dict[str, Any]] = []
    for index, entry in enumerate(notes):
        row = entry["start"] // ticks_per_row
        if index != body and row == body_row:
            continue
        if kept and kept[-1]["start"] // ticks_per_row == row:
            kept[-1] = entry
        else:
            kept.append(entry)
    return kept


def _last_write_tick(dump: dict[str, Any]) -> int:
    """Last dump tick that changed a SID register, or 0 if none did."""
    last = 0
    for frame in dump.get("frames") or ():
        if frame.get("writes"):
            last = int(frame["tick"])
    return last


def lift_to_ir(dump: dict[str, Any], ticks_per_row: int = 6) -> MusicIR:
    # Start from a reset chip and let init run through the same edge detection
    # as the play frames. Seeding the registers silently would hide the notes
    # of any subtune whose init gates a voice on, since play then only rewrites
    # the same control value and no gate ever rises.
    regs = bytearray(SID_REG_COUNT)

    channels = [Channel(id=i, name=f"voice{i + 1}") for i in range(3)]
    active = [False, False, False]
    pending_onset = [False, False, False]
    onset_frequency = [0, 0, 0]
    previous_frequency = [0, 0, 0]
    # Pitch under a held gate is buffered per voice and only turned into
    # events once the note ends, since arpeggio versus melody can only be
    # told apart from the whole shape.
    held: list[dict[str, Any] | None] = [None, None, None]
    # One gate rise, the waveform-table onsets under it, and the release that
    # follows the fall. Buffered as a unit because which onset is the note can
    # only be told from the waveform left playing at gate-off, and because the
    # pulse-width sweep keeps running into the release.
    group: list[dict[str, Any] | None] = [None, None, None]
    instruments: list[Instrument] = []
    instrument_ids: dict[tuple[int, ...], int] = {}
    filter_points: list[FilterPoint] = []

    def instrument_for(voice: int) -> int:
        base = voice * 7
        ctrl = regs[base + 4]
        pulse = regs[base + 2] | ((regs[base + 3] & 0x0F) << 8)
        if not ctrl & 0x40:
            pulse = 0
        ad = regs[base + 5]
        sr = regs[base + 6]
        filtered = bool(regs[23] & (1 << voice))
        # Quantize pulse width to the 32 duty steps a 32-sample waveform can
        # render. Without this, a pulse sweep mints a new instrument per tick
        # and a long tune blows past the 255 the .it format can number.
        key = (ctrl & 0xFE, pulse >> 7, ad, sr, int(filtered))
        found = instrument_ids.get(key)
        if found is not None:
            return found
        number = len(instruments) + 1
        instrument_ids[key] = number
        instruments.append(
            Instrument(
                id=number,
                name=f"{_waveform_label(ctrl)}-{ad:02x}{sr:02x}",
                waveform=_waveform(ctrl),
                pulse_width=pulse,
                attack=ad >> 4,
                decay=ad & 15,
                sustain=sr >> 4,
                release=sr & 15,
                filtered=filtered,
                ctrl=ctrl,
                sync=bool(ctrl & 0x02),
                ring=bool(ctrl & 0x04),
                extra={"sid_ctrl": ctrl},
            )
        )
        return number

    def pulse_width(voice: int) -> int:
        base = voice * 7
        return regs[base + 2] | ((regs[base + 3] & 0x0F) << 8)

    def modulator_ratio(voice: int, frequency: int) -> float | None:
        """Modulator frequency over carrier frequency, for sync or ring.

        Voice N is modulated by voice N-1, and voice 1 by voice 3, so the
        modulator index wraps. What is audible is the ratio of the two
        oscillators rather than either one alone, which is why this is stored
        instead of the raw modulator frequency.
        """
        ctrl = regs[voice * 7 + 4]
        if not ctrl & 0x06 or not frequency:
            return None
        source = (voice - 1) % 3
        source_base = source * 7
        modulator = regs[source_base] | (regs[source_base + 1] << 8)
        if not modulator:
            return None
        return modulator / frequency

    def flush_group(voice: int, tick: int) -> None:
        pending = group[voice]
        group[voice] = None
        held[voice] = None
        if pending is None:
            return
        off_tick = pending["off_tick"]
        # A group cut short by the next gate rise ends there; one that was
        # released ends at the fall, and its release is the tail after it.
        end = tick if off_tick is None else off_tick
        kept = _collapse_gate_group(
            pending["notes"], pending["release_waveform"], ticks_per_row
        )
        for index, note in enumerate(kept):
            stop = kept[index + 1]["start"] if index + 1 < len(kept) else end
            if stop <= note["start"]:
                continue
            extra = {"onset": "waveform"} if note["waveform_switch"] else None
            channels[voice].events.extend(
                _held_note_events(
                    note["start"],
                    note["instrument"],
                    note["frequency"],
                    note["segments"],
                    stop,
                    ticks_per_row,
                    note["modulator_ratio"],
                    pending["pw_points"],
                    extra,
                )
            )
        if off_tick is not None:
            channels[voice].events.append(Event(tick=off_tick, type="note_off"))

    def begin_note(
        voice: int,
        tick: int,
        frequency: int,
        note: int,
        *,
        waveform_switch: bool = False,
    ) -> None:
        ctrl = regs[voice * 7 + 4]
        entry = {
            "start": tick,
            "instrument": instrument_for(voice),
            "frequency": frequency,
            "waveform": _waveform(ctrl),
            "waveform_switch": waveform_switch,
            "segments": [(tick, note)],
            "modulator_ratio": modulator_ratio(voice, frequency),
        }
        if waveform_switch and group[voice] is not None:
            group[voice]["notes"].append(entry)
        else:
            flush_group(voice, tick)
            group[voice] = {
                "notes": [entry],
                # One pulse-width timeline for the whole group. Each onset
                # windows it by its own span, so the note that owns the
                # release also owns the sweep that runs through it.
                "pw_points": [[tick, pulse_width(voice)]],
                "off_tick": None,
                "release_waveform": None,
            }
        held[voice] = entry
        active[voice] = True

    def end_note(voice: int, tick: int) -> None:
        pending = group[voice]
        if pending is None:
            channels[voice].events.append(Event(tick=tick, type="note_off"))
        else:
            # Hold the group open past the fall: the envelope is still running
            # and drivers keep sweeping pulse width through the release.
            pending["off_tick"] = tick
            ctrl = regs[voice * 7 + 4]
            if ctrl & 0xF0:
                pending["release_waveform"] = _waveform(ctrl)
            elif held[voice] is not None:
                # Gate-only re-arm ($01) clears the waveform bits before we
                # sample them; keep the body that was actually ringing.
                pending["release_waveform"] = held[voice]["waveform"]
            else:
                pending["release_waveform"] = _waveform(ctrl)
        held[voice] = None
        active[voice] = False

    def append_filter(tick: int) -> None:
        mode_reg = regs[24]
        modes = [
            name
            for bit, name in ((0x10, "lp"), (0x20, "bp"), (0x40, "hp"))
            if mode_reg & bit
        ]
        point = FilterPoint(
            tick=tick,
            cutoff=((regs[22] << 3) | (regs[21] & 7)),
            resonance=regs[23] >> 4,
            mode=modes,
            routing=[1 if regs[23] & (1 << i) else 0 for i in range(3)],
            volume=mode_reg & 15,
            # $D418 bit 7 disconnects voice 3 from the dry mix only. A voice
            # still routed through the filter stays audible, matching the
            # 6581/8580 datasheet 3OFF note.
            voice3_off=bool(mode_reg & 0x80),
        )
        if not filter_points or point.to_dict() != filter_points[-1].to_dict():
            filter_points.append(point)

    init_frame = [{"tick": 0, "writes": dump["init_writes"]}]
    for frame in init_frame + dump["frames"]:
        tick = int(frame["tick"])
        rose = [False, False, False]
        fell = [False, False, False]
        filter_changed = False
        for offset, value in frame["writes"]:
            if offset in (4, 11, 18):
                voice = (offset - 4) // 7
                old_ctrl = regs[offset]
                old_audible = bool(
                    old_ctrl & 1 and not old_ctrl & 8 and old_ctrl & 0xF0
                )
                new_audible = bool(value & 1 and not value & 8 and value & 0xF0)
                old_sounding = bool(old_ctrl & 0xF0 and not old_ctrl & 8)
                new_sounding = bool(value & 0xF0 and not value & 8)
                if value & 8:
                    # Common hard restart: $09 (test+gate), followed by the
                    # real waveform+gate. It is not itself an audible onset.
                    pending_onset[voice] = True
                elif (value & 1) and not (value & 0xF0):
                    # Gate-only envelope arm. Vlindertjes lead charges ADSR
                    # with $01, then enables pulse into a long release via $40
                    # (waveform on, gate already clear). The arm is silent.
                    if active[voice]:
                        fell[voice] = True
                    pending_onset[voice] = True
                elif pending_onset[voice] and new_sounding:
                    # Waveform appears after an arm. Counts as an onset even
                    # when this write clears the gate ($01 -> $40).
                    rose[voice] = True
                    pending_onset[voice] = False
                    base = voice * 7
                    onset_frequency[voice] = regs[base] | (regs[base + 1] << 8)
                elif not (value & 1) and not (value & 0xF0):
                    pending_onset[voice] = False
                if new_audible and (not old_audible or pending_onset[voice]):
                    rose[voice] = True
                    pending_onset[voice] = False
                    base = voice * 7
                    onset_frequency[voice] = regs[base] | (regs[base + 1] << 8)
                if old_audible and not value & 1:
                    fell[voice] = True
                elif active[voice] and old_sounding and not new_sounding:
                    # Release-phase notes (gate already clear) end when the
                    # waveform bits drop, not on a gate edge that never rises.
                    fell[voice] = True
            regs[offset] = value
            filter_changed |= offset >= 21

        if filter_changed:
            append_filter(tick)
        for voice in range(3):
            base = voice * 7
            ctrl = regs[base + 4]
            audible = bool(ctrl & 1 and not ctrl & 8 and ctrl & 0xF0)
            # Oscillator output can be live with the gate clear: the envelope
            # is then in release. Gate-only arms use that for the lead voice.
            sounding = bool(ctrl & 0xF0 and not ctrl & 8)
            frequency = regs[base] | (regs[base + 1] << 8)
            if rose[voice] and sounding:
                # Some drivers gate on and then zero the frequency later in the
                # same frame, so fall back to the pitch as it was at the gate.
                onset = frequency or onset_frequency[voice]
                note = _midi_note(onset, dump["clock"])
                if note is not None:
                    begin_note(voice, tick, onset, note)
            elif fell[voice] and not audible and active[voice]:
                end_note(voice, tick)
            elif audible:
                # Drivers that gate once and phrase with the pitch alone, such
                # as Up'n'Down, articulate on a crossing through frequency 0.
                # Vibrato and portamento never reach 0, so they stay inside the
                # note and are handled as pitch movement below.
                if frequency == 0:
                    if active[voice]:
                        end_note(voice, tick)
                elif not active[voice] and previous_frequency[voice] == 0:
                    note = _midi_note(frequency, dump["clock"])
                    if note is not None:
                        begin_note(voice, tick, frequency, note)
                elif (
                    held[voice] is not None
                    and _waveform(ctrl) != held[voice]["waveform"]
                ):
                    # Named waveform changed with the gate still held. Huelsbeck
                    # bass is the usual case: one frame of noise, then pulse
                    # for the body. Do not emit note_off; a same-tick off would
                    # round up onto a later tracker row and mute the body.
                    note = _midi_note(frequency, dump["clock"])
                    if note is not None:
                        begin_note(
                            voice,
                            tick,
                            frequency,
                            note,
                            waveform_switch=True,
                        )
            if held[voice] is not None and sounding and frequency:
                note = _midi_note(frequency, dump["clock"])
                if note is not None and note != held[voice]["segments"][-1][1]:
                    held[voice]["segments"].append((tick, note))
            # Pulse width is a register, not part of the oscillator, so a
            # sweep is timbre movement rather than a new note. Record it for
            # the renderer instead of discarding it. It keeps moving after the
            # gate falls, and that part of the sweep is just as audible.
            if group[voice] is not None and ctrl & 0x40:
                width = pulse_width(voice)
                if width != group[voice]["pw_points"][-1][1]:
                    group[voice]["pw_points"].append([tick, width])
            previous_frequency[voice] = frequency

    end_tick = int(dump["ticks"])
    for voice in range(3):
        if active[voice]:
            end_note(voice, end_tick)
        flush_group(voice, end_tick)

    # The current IR requires at least one instrument and channel. A silent
    # dump should fail loudly rather than produce a deceptive empty module.
    if not instruments:
        raise IRError("dump produced no gate-rising SID notes")

    return MusicIR(
        title=dump["title"],
        source={
            "kind": "sid-dump",
            "file": dump["source"],
            "subtune": dump["subtune"],
            "songs": int(dump["songs"]) if dump.get("songs") is not None else 1,
            "author": dump.get("author") or "",
            "released": dump.get("released") or "",
            "sid_model": dump["sid_model"],
            "ticks": int(dump["ticks"]),
            "last_write_tick": _last_write_tick(dump),
        },
        timing=Timing(
            clock=dump["clock"],
            frame_hz=float(dump["frame_hz"]),
            ticks_per_row=ticks_per_row,
        ),
        instruments=instruments,
        channels=channels,
        filter=filter_points,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run a PSID/RSID and dump SID writes / music IR"
    )
    parser.add_argument("sid", type=Path)
    parser.add_argument("--ticks", type=int, default=600)
    parser.add_argument("--subtune", type=int)
    parser.add_argument("--dump", type=Path)
    parser.add_argument("--ir", type=Path)
    parser.add_argument("--ticks-per-row", type=int, default=6)
    parser.add_argument(
        "--init-max-cycles",
        type=int,
        default=DEFAULT_INIT_IRQ_CYCLES,
        help="IRQ-path init cycle budget before digi/speech skip "
        f"(default {DEFAULT_INIT_IRQ_CYCLES})",
    )
    args = parser.parse_args(argv)
    try:
        dump = run_sid(
            args.sid,
            args.ticks,
            args.subtune,
            init_cycles_budget=args.init_max_cycles,
        )
        ir = lift_to_ir(dump, args.ticks_per_row)
        if args.dump:
            args.dump.parent.mkdir(parents=True, exist_ok=True)
            args.dump.write_text(json.dumps(dump, indent=2) + "\n", encoding="utf-8")
        if args.ir:
            args.ir.parent.mkdir(parents=True, exist_ok=True)
            save(ir, args.ir)
    except (OSError, SidHeaderError, DigiSkipError, CPUError, IRError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    note_count = sum(
        event.type == "note_on"
        for channel in ir.channels
        for event in channel.events
    )
    write_count = len(dump["init_writes"]) + sum(
        len(frame["writes"]) for frame in dump["frames"]
    )
    print(
        f"{dump['title']}: {dump['ticks']} ticks, {write_count} changed writes, "
        f"{note_count} notes, {len(ir.instruments)} instruments"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
