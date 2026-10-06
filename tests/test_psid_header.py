"""Synthetic PSID/RSID header tests. No HVSC files in the repo."""

from __future__ import annotations

import struct
import unittest
from pathlib import Path

from sidengine.header import SidHeaderError, parse_sid_bytes

def _pad32(text: str) -> bytes:
    raw = text.encode("cp1252")
    if len(raw) > 32:
        raise ValueError(text)
    return raw + b"\x00" * (32 - len(raw))

def _header(
    *,
    magic: bytes = b"PSID",
    version: int = 2,
    load: int = 0,
    init: int = 0x1000,
    play: int = 0x1003,
    songs: int = 1,
    start: int = 1,
    speed: int = 0,
    flags: int = 0x14,  # PAL + 6581
    start_page: int = 0,
    page_length: int = 0,
    sid2: int = 0,
    sid3: int = 0,
    payload: bytes = b"\x00\x10" + b"\x60\x60\x60",
) -> bytes:
    data_offset = 0x76 if version == 1 else 0x7C
    body = bytearray(data_offset)
    body[0:4] = magic
    struct.pack_into(
        ">HHHHHHH",
        body,
        4,
        version,
        data_offset,
        load,
        init,
        play,
        songs,
        start,
    )
    struct.pack_into(">I", body, 0x12, speed)
    body[0x16:0x36] = _pad32("Test Tune")
    body[0x36:0x56] = _pad32("Unit Test")
    body[0x56:0x76] = _pad32("2026 Test")
    if version >= 2:
        struct.pack_into(">H", body, 0x76, flags)
        body[0x78] = start_page
        body[0x79] = page_length
        body[0x7A] = sid2
        body[0x7B] = sid3
    return bytes(body) + payload

class ParseSidHeaderTests(unittest.TestCase):
    def test_psid_v2_load_in_payload(self) -> None:
        info = parse_sid_bytes(_header())
        self.assertEqual(info["magic"], "PSID")
        self.assertEqual(info["load_address"], 0x1000)
        self.assertTrue(info["load_address_in_payload"])
        self.assertEqual(info["init_address"], 0x1000)
        self.assertEqual(info["play_address"], 0x1003)
        self.assertFalse(info["irq_installed_by_init"])
        self.assertEqual(info["init_accumulator"], 0)
        self.assertEqual(info["song_speed"]["1"], "vblank")
        self.assertEqual(info["flags"]["clock"], "PAL")
        self.assertEqual(info["flags"]["sid_model"], "MOS6581")
        self.assertEqual(info["payload_bytes"], 3)
        self.assertEqual(info["end_address"], 0x1002)

    def test_play_zero_means_irq(self) -> None:
        info = parse_sid_bytes(_header(play=0, payload=b"\x00\x10\x60"))
        self.assertTrue(info["irq_installed_by_init"])
        self.assertEqual(info["play_address"], 0)

    def test_explicit_load_address(self) -> None:
        payload = b"\x60\x60"
        info = parse_sid_bytes(_header(load=0x4000, init=0, play=0x4003, payload=payload))
        self.assertEqual(info["load_address"], 0x4000)
        self.assertFalse(info["load_address_in_payload"])
        self.assertEqual(info["init_address"], 0x4000)
        self.assertEqual(info["payload_bytes"], 2)

    def test_cia_speed_bit(self) -> None:
        info = parse_sid_bytes(_header(songs=2, speed=0x00000002))
        self.assertEqual(info["song_speed"]["1"], "vblank")
        self.assertEqual(info["song_speed"]["2"], "cia")

    def test_rsid_ok(self) -> None:
        payload = b"\xe8\x07" + b"\x60" * 8
        info = parse_sid_bytes(
            _header(
                magic=b"RSID",
                version=2,
                load=0,
                init=0x07E8,
                play=0,
                speed=0,
                flags=0x14,
                payload=payload,
            )
        )
        self.assertEqual(info["magic"], "RSID")
        self.assertEqual(info["load_address"], 0x07E8)
        self.assertTrue(info["irq_installed_by_init"])

    def test_rsid_rejects_nonzero_play(self) -> None:
        with self.assertRaises(SidHeaderError):
            parse_sid_bytes(
                _header(magic=b"RSID", load=0, init=0x1000, play=0x1003, speed=0)
            )

    def test_bad_magic(self) -> None:
        with self.assertRaises(SidHeaderError):
            parse_sid_bytes(_header(magic=b"MIDI"))

    def test_start_song_out_of_range(self) -> None:
        with self.assertRaises(SidHeaderError):
            parse_sid_bytes(_header(songs=1, start=2))

    def test_windows1252_name(self) -> None:
        data = bytearray(_header())
        data[0x16:0x36] = (b"J\xe4ger" + b"\x00" * 26)[:32]
        info = parse_sid_bytes(bytes(data))
        self.assertEqual(info["name"], "Jäger")

    def test_second_sid_address(self) -> None:
        info = parse_sid_bytes(_header(version=3, sid2=0x42, flags=0x14))
        self.assertEqual(info["second_sid_address"], 0xD420)

if __name__ == "__main__":
    unittest.main()
