"""Chip-music intermediate representation: load, validate, inspect."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

FORMAT = "c64-music-ir"
VERSION = 1

WAVEFORMS = ("triangle", "saw", "pulse", "noise")
CLOCKS = {"PAL": 50.125, "NTSC": 59.826}


class IRError(ValueError):
    pass


@dataclass
class Timing:
    clock: str = "PAL"
    frame_hz: float = CLOCKS["PAL"]
    ticks_per_row: int = 6

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Timing":
        clock = raw.get("clock", "PAL")
        if clock not in CLOCKS:
            raise IRError(f"unknown clock {clock!r}")
        ticks_per_row = int(raw.get("ticks_per_row", 6))
        if ticks_per_row < 1:
            raise IRError("ticks_per_row must be >= 1")
        return cls(
            clock=clock,
            frame_hz=float(raw.get("frame_hz", CLOCKS[clock])),
            ticks_per_row=ticks_per_row,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "clock": self.clock,
            "frame_hz": self.frame_hz,
            "ticks_per_row": self.ticks_per_row,
        }


@dataclass
class Instrument:
    id: int
    name: str = ""
    waveform: str = "pulse"
    pulse_width: int = 2048
    attack: int = 0
    decay: int = 9
    sustain: int = 9
    release: int = 0
    filtered: bool = False
    ctrl: int | None = None
    # CTRL bits 1 and 2. Derived from ctrl for consumers that render audio and
    # should not have to know the SID register layout.
    sync: bool = False
    ring: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Instrument":
        if "id" not in raw:
            raise IRError("instrument without id")
        waveform = raw.get("waveform", "pulse")
        if waveform not in WAVEFORMS:
            raise IRError(f"instrument {raw['id']}: bad waveform {waveform!r}")
        inst = cls(
            id=int(raw["id"]),
            name=str(raw.get("name", "")),
            waveform=waveform,
            pulse_width=int(raw.get("pulse_width", 2048)),
            attack=int(raw.get("attack", 0)),
            decay=int(raw.get("decay", 9)),
            sustain=int(raw.get("sustain", 9)),
            release=int(raw.get("release", 0)),
            filtered=bool(raw.get("filtered", False)),
            ctrl=raw.get("ctrl"),
            sync=bool(raw.get("sync", False)),
            ring=bool(raw.get("ring", False)),
            extra=dict(raw.get("extra", {})),
        )
        if inst.id < 1:
            raise IRError("instrument ids start at 1")
        for nybble in ("attack", "decay", "sustain", "release"):
            value = getattr(inst, nybble)
            if not 0 <= value <= 15:
                raise IRError(f"instrument {inst.id}: {nybble}={value} out of 0-15")
        if not 0 <= inst.pulse_width <= 4095:
            raise IRError(f"instrument {inst.id}: pulse_width out of 12-bit range")
        return inst

    def to_dict(self) -> dict[str, Any]:
        out = {
            "id": self.id,
            "name": self.name,
            "waveform": self.waveform,
            "pulse_width": self.pulse_width,
            "attack": self.attack,
            "decay": self.decay,
            "sustain": self.sustain,
            "release": self.release,
            "filtered": self.filtered,
        }
        if self.ctrl is not None:
            out["ctrl"] = self.ctrl
        if self.sync:
            out["sync"] = self.sync
        if self.ring:
            out["ring"] = self.ring
        if self.extra:
            out["extra"] = self.extra
        return out


@dataclass
class Event:
    tick: int
    type: str
    note: int | None = None
    instrument: int | None = None
    arpeggio: list[int] = field(default_factory=list)
    # Modulator frequency divided by this voice's frequency, for sync and ring.
    # The audible spectrum is that ratio, not either frequency alone, so it
    # survives transposition and can be rendered once per distinct ratio.
    modulator_ratio: float | None = None
    # [tick, 12-bit pulse width] while this note sounds. One entry means a
    # static duty; more than one is pulse-width modulation.
    pw_points: list[list[int]] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Event":
        kind = raw.get("type")
        if kind not in ("note_on", "note_off", "pitch", "effect"):
            raise IRError(f"bad event type {kind!r}")
        tick = int(raw.get("tick", -1))
        if tick < 0:
            raise IRError("event tick must be >= 0")
        note = raw.get("note")
        instrument = raw.get("instrument")
        event = cls(
            tick=tick,
            type=kind,
            note=None if note is None else int(note),
            instrument=None if instrument is None else int(instrument),
            arpeggio=list(raw.get("arpeggio", [])),
            modulator_ratio=(
                None
                if raw.get("modulator_ratio") is None
                else float(raw["modulator_ratio"])
            ),
            pw_points=[[int(tick), int(pw)] for tick, pw in raw.get("pw_points", [])],
            extra=dict(raw.get("extra", {})),
        )
        for _, pw in event.pw_points:
            if not 0 <= pw <= 4095:
                raise IRError(f"pulse width {pw} outside 12-bit range")
        if event.modulator_ratio is not None and event.modulator_ratio <= 0:
            raise IRError(f"modulator_ratio {event.modulator_ratio} must be positive")
        if kind in ("note_on", "pitch"):
            if event.note is None:
                raise IRError(f"{kind} at tick {tick} without note")
            if not 0 <= event.note <= 119:
                raise IRError(f"note {event.note} outside MIDI 0-119")
        if kind == "note_on":
            if event.instrument is None:
                raise IRError(f"note_on at tick {tick} without instrument")
        if kind == "effect" and not event.arpeggio:
            raise IRError(f"effect at tick {tick} without arpeggio")
        return event

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"tick": self.tick, "type": self.type}
        if self.note is not None:
            out["note"] = self.note
        if self.instrument is not None:
            out["instrument"] = self.instrument
        if self.arpeggio:
            out["arpeggio"] = self.arpeggio
        if self.modulator_ratio is not None:
            out["modulator_ratio"] = self.modulator_ratio
        if self.pw_points:
            out["pw_points"] = self.pw_points
        if self.extra:
            out["extra"] = self.extra
        return out


@dataclass
class Channel:
    id: int
    name: str = ""
    events: list[Event] = field(default_factory=list)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Channel":
        if "id" not in raw:
            raise IRError("channel without id")
        events = [Event.from_dict(e) for e in raw.get("events", [])]
        ticks = [e.tick for e in events]
        if ticks != sorted(ticks):
            raise IRError(f"channel {raw['id']}: events not sorted by tick")
        return cls(id=int(raw["id"]), name=str(raw.get("name", "")), events=events)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "events": [e.to_dict() for e in self.events],
        }


@dataclass
class FilterPoint:
    tick: int
    cutoff: int = 0
    resonance: int = 0
    mode: list[str] = field(default_factory=list)
    routing: list[int] = field(default_factory=list)
    volume: int = 15
    voice3_off: bool = False

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "FilterPoint":
        point = cls(
            tick=int(raw.get("tick", 0)),
            cutoff=int(raw.get("cutoff", 0)),
            resonance=int(raw.get("resonance", 0)),
            mode=list(raw.get("mode", [])),
            routing=list(raw.get("routing", [])),
            volume=int(raw.get("volume", 15)),
            voice3_off=bool(raw.get("voice3_off", False)),
        )
        if not 0 <= point.cutoff <= 2047:
            raise IRError(f"cutoff {point.cutoff} outside 11-bit range")
        if not 0 <= point.resonance <= 15:
            raise IRError(f"resonance {point.resonance} outside 0-15")
        if not 0 <= point.volume <= 15:
            raise IRError(f"volume {point.volume} outside 0-15")
        return point

    def to_dict(self) -> dict[str, Any]:
        return {
            "tick": self.tick,
            "cutoff": self.cutoff,
            "resonance": self.resonance,
            "mode": self.mode,
            "routing": self.routing,
            "volume": self.volume,
            "voice3_off": self.voice3_off,
        }


@dataclass
class MusicIR:
    title: str = ""
    source: dict[str, Any] = field(default_factory=dict)
    timing: Timing = field(default_factory=Timing)
    instruments: list[Instrument] = field(default_factory=list)
    channels: list[Channel] = field(default_factory=list)
    filter: list[FilterPoint] = field(default_factory=list)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "MusicIR":
        if raw.get("format") != FORMAT:
            raise IRError(f"not a {FORMAT} document")
        if int(raw.get("version", 0)) != VERSION:
            raise IRError(f"unsupported IR version {raw.get('version')!r}")
        ir = cls(
            title=str(raw.get("title", "")),
            source=dict(raw.get("source", {})),
            timing=Timing.from_dict(raw.get("timing", {})),
            instruments=[Instrument.from_dict(i) for i in raw.get("instruments", [])],
            channels=[Channel.from_dict(c) for c in raw.get("channels", [])],
            filter=[FilterPoint.from_dict(f) for f in raw.get("filter", [])],
        )
        ir.validate()
        return ir

    def validate(self) -> None:
        if not self.channels:
            raise IRError("IR has no channels")
        ids = [c.id for c in self.channels]
        if len(set(ids)) != len(ids):
            raise IRError("duplicate channel ids")
        known = {i.id for i in self.instruments}
        if len(known) != len(self.instruments):
            raise IRError("duplicate instrument ids")
        for channel in self.channels:
            for event in channel.events:
                if (
                    event.type in ("note_on", "pitch")
                    and event.instrument is not None
                    and event.instrument not in known
                ):
                    raise IRError(
                        f"channel {channel.id} tick {event.tick}: "
                        f"unknown instrument {event.instrument!r}"
                    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": FORMAT,
            "version": VERSION,
            "title": self.title,
            "source": self.source,
            "timing": self.timing.to_dict(),
            "instruments": [i.to_dict() for i in self.instruments],
            "channels": [c.to_dict() for c in self.channels],
            "filter": [f.to_dict() for f in self.filter],
        }

    @property
    def sid_model(self) -> str:
        """`MOS6581` or `MOS8580`, defaulting to 6581 when the header is vague.

        6581 is both the older part and the one whose filter curve differs most
        from the specification, so guessing it keeps a dual-model or unknown
        file closer to what its composer most likely heard.
        """
        model = str(self.source.get("sid_model", "") or "")
        return "MOS8580" if model.startswith("MOS8580") else "MOS6581"

    @property
    def last_tick(self) -> int:
        ticks = [e.tick for c in self.channels for e in c.events]
        ticks += [f.tick for f in self.filter]
        return max(ticks) if ticks else 0


def load(path: Path) -> MusicIR:
    return MusicIR.from_dict(json.loads(path.read_text(encoding="utf-8")))


def save(ir: MusicIR, path: Path) -> None:
    path.write_text(json.dumps(ir.to_dict(), indent=2) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate and summarize a music IR file")
    parser.add_argument("ir", type=Path, help="IR JSON document")
    args = parser.parse_args(argv)
    try:
        ir = load(args.ir)
    except (OSError, IRError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    notes = sum(
        1 for c in ir.channels for e in c.events if e.type == "note_on"
    )
    print(f"title        {ir.title or '(untitled)'}")
    print(f"clock        {ir.timing.clock} {ir.timing.frame_hz:g} Hz")
    print(f"ticks/row    {ir.timing.ticks_per_row}")
    print(f"channels     {len(ir.channels)}")
    print(f"instruments  {len(ir.instruments)}")
    print(f"note_on      {notes}")
    print(f"filter pts   {len(ir.filter)}")
    print(f"last tick    {ir.last_tick}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
