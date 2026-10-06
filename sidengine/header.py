"""Parse HVSC PSID/RSID headers (SID_file_format.txt)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

MAGIC_PSID = b"PSID"
MAGIC_RSID = b"RSID"

CLOCK = {0: "unknown", 1: "PAL", 2: "NTSC", 3: "PAL+NTSC"}
SID_MODEL = {0: "unknown", 1: "MOS6581", 2: "MOS8580", 3: "MOS6581+MOS8580"}


class SidHeaderError(ValueError):
    pass


def _be16(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset : offset + 2], "big")


def _be32(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset : offset + 4], "big")


def _sid_string(data: bytes, offset: int) -> str:
    raw = data[offset : offset + 32]
    if b"\x00" in raw:
        raw = raw.split(b"\x00", 1)[0]
    return raw.decode("cp1252", errors="replace")


def _decode_flags(flags: int, magic: str, version: int) -> dict[str, Any]:
    clock_code = (flags >> 2) & 3
    model_1 = (flags >> 4) & 3
    model_2 = (flags >> 6) & 3
    model_3 = (flags >> 8) & 3
    bit1 = bool(flags & 2)
    out: dict[str, Any] = {
        "raw": flags,
        "mus_player": bool(flags & 1),
        "clock": CLOCK[clock_code],
        "sid_model": SID_MODEL[model_1],
        "sid_model_2": SID_MODEL[model_2] if version >= 3 else None,
        "sid_model_3": SID_MODEL[model_3] if version >= 4 else None,
        "playsid_specific": bit1 if magic == "PSID" else False,
        "c64_basic": bit1 if magic == "RSID" else False,
    }
    if version >= 3 and model_2 == 0:
        out["sid_model_2"] = out["sid_model"]
    if version >= 4 and model_3 == 0:
        out["sid_model_3"] = out["sid_model"]
    return out


def _speed_for_song(speed: int, song: int, wrap_like_v1: bool) -> str:
    # song is 1-based
    if wrap_like_v1:
        bit = (song - 1) % 32
    elif song <= 32:
        bit = song - 1
    else:
        bit = 31
    return "cia" if (speed >> bit) & 1 else "vblank"


def parse_sid_bytes(data: bytes, name: str = "<memory>") -> dict[str, Any]:
    if len(data) < 0x76:
        raise SidHeaderError(f"{name}: truncated header ({len(data)} bytes)")

    magic = data[0:4]
    if magic not in (MAGIC_PSID, MAGIC_RSID):
        raise SidHeaderError(f"{name}: bad magic {magic!r}")
    magic_s = magic.decode("ascii")

    version = _be16(data, 4)
    if version not in (1, 2, 3, 4):
        raise SidHeaderError(f"{name}: bad version {version}")
    if magic_s == "RSID" and version == 1:
        raise SidHeaderError(f"{name}: RSID cannot be version 1")

    data_offset = _be16(data, 6)
    expected_offset = 0x76 if version == 1 else 0x7C
    if data_offset != expected_offset:
        raise SidHeaderError(
            f"{name}: dataOffset ${data_offset:04X}, expected ${expected_offset:04X}"
        )
    if len(data) < data_offset:
        raise SidHeaderError(f"{name}: file shorter than dataOffset")

    load_field = _be16(data, 8)
    init_field = _be16(data, 10)
    play_field = _be16(data, 12)
    songs = _be16(data, 14)
    start_song = _be16(data, 16)
    speed = _be32(data, 18)

    if not 1 <= songs <= 256:
        raise SidHeaderError(f"{name}: songs={songs}")
    if not 1 <= start_song <= songs:
        raise SidHeaderError(f"{name}: startSong={start_song} songs={songs}")

    payload = data[data_offset:]
    if load_field == 0:
        if len(payload) < 2:
            raise SidHeaderError(f"{name}: loadAddress 0 but payload < 2 bytes")
        load_addr = int.from_bytes(payload[0:2], "little")
        c64_binary = payload[2:]
        load_in_payload = True
    else:
        load_addr = load_field
        c64_binary = payload
        load_in_payload = False

    init_addr = load_addr if init_field == 0 else init_field
    play_addr = play_field
    irq_installed = play_addr == 0

    flags_raw = _be16(data, 0x76) if version >= 2 else 0
    flags = (
        _decode_flags(flags_raw, magic_s, version)
        if version >= 2
        else {
            "raw": 0,
            "mus_player": False,
            "clock": "unknown",
            "sid_model": "unknown",
            "sid_model_2": None,
            "sid_model_3": None,
            "playsid_specific": False,
            "c64_basic": False,
        }
    )

    if magic_s == "RSID":
        if load_field != 0 or play_field != 0 or speed != 0:
            raise SidHeaderError(
                f"{name}: RSID requires load=0 play=0 speed=0 "
                f"(got load=${load_field:04X} play=${play_field:04X} speed=${speed:08X})"
            )
        if load_addr < 0x07E8:
            raise SidHeaderError(f"{name}: RSID load ${load_addr:04X} < $07E8")
        if flags["c64_basic"] and init_field != 0:
            raise SidHeaderError(f"{name}: RSID BASIC flag requires initAddress 0")

    wrap_like_v1 = version == 1 or (
        version >= 2 and magic_s == "PSID" and flags["playsid_specific"]
    )
    song_speed = {
        str(song): _speed_for_song(speed, song, wrap_like_v1)
        for song in range(1, songs + 1)
    }

    reloc_start = data[0x78] if version >= 2 else 0
    reloc_pages = data[0x79] if version >= 2 else 0
    sid2 = data[0x7A] if version >= 3 else 0
    sid3 = data[0x7B] if version >= 4 else 0

    def sid_chip_addr(page: int) -> int | None:
        if page == 0:
            return None
        # even $42-$7F or $E0-$FE => $Dxx0
        if page < 0x42 or 0x80 <= page <= 0xDF or page & 1:
            return None
        return 0xD000 | (page << 4)

    return {
        "file": name,
        "magic": magic_s,
        "version": version,
        "data_offset": data_offset,
        "load_address_field": load_field,
        "init_address_field": init_field,
        "play_address_field": play_field,
        "load_address": load_addr,
        "init_address": init_addr,
        "play_address": play_addr,
        "load_address_in_payload": load_in_payload,
        "irq_installed_by_init": irq_installed,
        "init_accumulator": start_song - 1,
        "songs": songs,
        "start_song": start_song,
        "speed_raw": speed,
        "song_speed": song_speed,
        "name": _sid_string(data, 0x16),
        "author": _sid_string(data, 0x36),
        "released": _sid_string(data, 0x56),
        "flags": flags,
        "reloc_start_page": reloc_start,
        "reloc_pages": reloc_pages,
        "second_sid_address": sid_chip_addr(sid2),
        "third_sid_address": sid_chip_addr(sid3),
        "payload_bytes": len(c64_binary),
        "end_address": load_addr + len(c64_binary) - 1 if c64_binary else load_addr,
    }


def parse_sid_file(path: Path) -> dict[str, Any]:
    return parse_sid_bytes(path.read_bytes(), name=str(path))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dump a PSID/RSID header as JSON")
    parser.add_argument("sid", nargs="+", type=Path, help=".sid file(s)")
    args = parser.parse_args(argv)
    rows = []
    errors = 0
    for path in args.sid:
        try:
            rows.append(parse_sid_file(path))
        except (OSError, SidHeaderError) as exc:
            errors += 1
            rows.append({"file": str(path), "error": str(exc)})
    json.dump(rows if len(rows) != 1 else rows[0], sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
