"""Synthetic IRQ harness and RSID dump tests. No HVSC tune is committed."""

from __future__ import annotations

import struct
import tempfile
import unittest
from pathlib import Path

from sidengine.cpu6502 import CPU6502, CPUError, Memory
from sidengine.irq_harness import (
    IDLE_ADDR,
    HarnessMemory,
    IrqHarness,
    install_kernal_stubs,
)
from sidengine.dump import (
    DigiSkipError,
    _irq_handler_timeout,
    lift_to_ir,
    run_irq_sid,
    run_sid,
)

class IrqHarnessUnitTests(unittest.TestCase):
    def test_raster_compare_137_fires_once_per_frame(self) -> None:
        harness = IrqHarness("PAL")
        harness.reset_rsid(0x4025)
        harness.cra = 0
        harness.icr_mask = 0
        harness._cia_irq = False
        harness.d01a = 0x01
        fires = 0
        for _ in range(harness.cycles_per_frame * 2):
            pending_before = harness.irq_pending()
            harness.advance(1)
            if harness.irq_pending() and not pending_before:
                fires += 1
                harness.d019 = 0
        self.assertEqual(fires, 2)

    def test_inc_d019_acks_raster(self) -> None:
        harness = IrqHarness("PAL")
        harness.reset_rsid(0x4025)
        harness.d01a = 0x01
        harness.d019 = 0x81
        memory = HarnessMemory(harness)
        memory.data[1] = 0x37
        install_kernal_stubs(memory)
        # INC $D019
        memory.data[0x2000:0x2003] = bytes([0xEE, 0x19, 0xD0, 0x60])
        cpu = CPU6502(memory)
        cpu.call(0x2000)
        self.assertFalse(harness.d019 & 0x01)

class IrqTimeoutMessageTests(unittest.TestCase):
    def test_self_jmp_is_called_out_as_demo_lock(self) -> None:
        mem = Memory()
        mem.data[0x48] = 0x4C
        mem.data[0x49] = 0x48
        mem.data[0x4A] = 0x00
        cpu = CPU6502(mem)
        cpu.pc = 0x48
        err = _irq_handler_timeout(cpu, 200_000)
        self.assertIsInstance(err, CPUError)
        self.assertIn("pc=$0048", str(err))
        self.assertIn("infinite JMP", str(err))

    def test_ordinary_pc_stays_a_short_timeout(self) -> None:
        mem = Memory()
        mem.data[0x1000] = 0xEA
        cpu = CPU6502(mem)
        cpu.pc = 0x1000
        err = _irq_handler_timeout(cpu, 200_000)
        self.assertEqual(
            str(err),
            "IRQ handler exceeded 200000 cycles (pc=$1000)",
        )

def synthetic_rsid(irq_exit: int = 0xEA31, *, fffe_handler: bool = False) -> bytes:
    """RSID that installs a VIC IRQ; each IRQ gates a pulse C4 once."""
    header = bytearray(0x7C)
    header[0:4] = b"RSID"
    struct.pack_into(
        ">HHHHHHH",
        header,
        4,
        2,
        0x7C,
        0,  # load in payload
        0x1000,  # init
        0,  # play
        1,
        1,
    )
    struct.pack_into(">I", header, 0x12, 0)  # speed must be 0
    header[0x16:0x16 + 8] = b"IRQ Test"
    header[0x36:0x36 + 8] = b"IRQ Auth"
    header[0x56:0x56 + 4] = b"2026"
    struct.pack_into(">H", header, 0x76, 0x14)  # PAL, 6581
    header[0x78] = 0x10
    header[0x79] = 0x10

    # Payload starts with LE load address $1000.
    # $1000 init: SEI; LDA #$00; STA $0314; LDA #$11; STA $0315;
    #             LDA #1; STA $D01A; CLI; RTS
    # $1100 IRQ:  INC $D019; LDA #$09; STA $D404; LDA #$67; STA $D400;
    #             LDA #$11; STA $D401; LDA #$41; STA $D404; JMP $EA31
    init = bytes([
        0x78,
        0xA9, 0x7F, 0x8D, 0x0D, 0xDC,  # disable CIA1 IRQs
        0xA9, 0x00, 0x8D, 0x14, 0x03,
        0xA9, 0x11, 0x8D, 0x15, 0x03,
        *([0xA9, 0x35, 0x85, 0x01] if fffe_handler else []),
        *([0xA9, 0x00, 0x8D, 0xFE, 0xFF, 0xA9, 0x11, 0x8D, 0xFF, 0xFF] if fffe_handler else []),
        0xA9, 0x01, 0x8D, 0x1A, 0xD0,
        0x58,
        0x60,
    ])
    if fffe_handler:
        # Hardware IRQ only: PHA TYA PHA TXA PHA, ack, SID, PLA TAX PLA TAY PLA RTI.
        irq = bytes([
            0x48, 0x98, 0x48, 0x8A, 0x48,
            0xEE, 0x19, 0xD0,
            0xA9, 0x09, 0x8D, 0x04, 0xD4,
            0xA9, 0x67, 0x8D, 0x00, 0xD4,
            0xA9, 0x11, 0x8D, 0x01, 0xD4,
            0xA9, 0x41, 0x8D, 0x04, 0xD4,
            0x68, 0xAA, 0x68, 0xA8, 0x68, 0x40,
        ])
    else:
        irq = bytes([
            0xEE, 0x19, 0xD0,
            0xA9, 0x09, 0x8D, 0x04, 0xD4,
            0xA9, 0x67, 0x8D, 0x00, 0xD4,
            0xA9, 0x11, 0x8D, 0x01, 0xD4,
            0xA9, 0x41, 0x8D, 0x04, 0xD4,
            0x4C, irq_exit & 0xFF, irq_exit >> 8,
        ])
    payload = bytearray(0x200)
    payload[0:2] = b"\x00\x10"
    payload[2 : 2 + len(init)] = init
    payload[0x102 : 0x102 + len(irq)] = irq  # $1100 relative to load $1000
    return bytes(header) + bytes(payload)

class IrqSidDumpTests(unittest.TestCase):
    def test_run_irq_sid_buckets_writes_per_frame(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "irq.sid"
            path.write_bytes(synthetic_rsid())
            dump = run_irq_sid(path, ticks=4)
        self.assertEqual(dump["execution"], "irq")
        self.assertEqual(dump["clock"], "PAL")
        self.assertEqual(dump["released"], "2026")
        self.assertGreaterEqual(len(dump["frames"]), 3)
        ir = lift_to_ir(dump)
        self.assertEqual(ir.source["author"], "IRQ Auth")
        self.assertEqual(ir.source["released"], "2026")
        notes = [
            event
            for channel in ir.channels
            for event in channel.events
            if event.type == "note_on"
        ]
        self.assertGreaterEqual(len(notes), 3)
        self.assertEqual(notes[0].note, 60)

    def test_run_irq_sid_reports_frame_progress(self) -> None:
        seen: list[tuple[int, int]] = []
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "irq.sid"
            path.write_bytes(synthetic_rsid())
            run_irq_sid(
                path,
                ticks=4,
                on_progress=lambda done, total: seen.append((done, total)),
            )
        self.assertEqual(seen[-1], (4, 4))
        self.assertEqual(seen[0][1], 4)
        self.assertGreaterEqual(len(seen), 2)

    def test_run_sid_dispatches_rsid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "irq.sid"
            path.write_bytes(synthetic_rsid())
            dump = run_sid(path, ticks=2)
        self.assertEqual(dump["execution"], "irq")

    def test_idle_stub_present(self) -> None:
        harness = IrqHarness("PAL")
        memory = HarnessMemory(harness)
        install_kernal_stubs(memory)
        self.assertEqual(memory.data[IDLE_ADDR], 0xEA)
        # $EA80 is PLA (KERNAL restore), not the old idle NOP.
        self.assertEqual(memory.data[0xEA80], 0x68)
        self.assertEqual(memory.data[0xEA7E : 0xEA84], bytes([0x68, 0xA8, 0x68, 0xAA, 0x68, 0x40]))

    def test_ea7e_rti_returns_to_idle_pc(self) -> None:
        harness = IrqHarness("PAL")
        memory = HarnessMemory(harness)
        memory.data[1] = 0x37
        install_kernal_stubs(memory)
        cpu = CPU6502(memory)
        cpu.pc = IDLE_ADDR
        cpu.p &= ~0x04
        cpu.a, cpu.x, cpu.y = 0x11, 0x22, 0x33
        self.assertTrue(cpu.trigger_irq())
        cpu.pc = 0xEA7E
        for _ in range(8):
            cpu.step()
            if cpu.pc == IDLE_ADDR:
                break
        self.assertEqual(cpu.pc, IDLE_ADDR)
        self.assertEqual((cpu.a, cpu.x, cpu.y), (0x11, 0x22, 0x33))
        self.assertFalse(cpu.p & 0x04)

    def test_run_irq_sid_ea7e_exit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "irq_ea7e.sid"
            path.write_bytes(synthetic_rsid(irq_exit=0xEA7E))
            dump = run_irq_sid(path, ticks=4)
        self.assertGreaterEqual(len(dump["frames"]), 3)

    def test_run_irq_sid_fffe_when_kernal_paged_out(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "irq_fffe.sid"
            path.write_bytes(synthetic_rsid(fffe_handler=True))
            dump = run_irq_sid(path, ticks=4)
        ir = lift_to_ir(dump)
        notes = [
            event
            for channel in ir.channels
            for event in channel.events
            if event.type == "note_on"
        ]
        self.assertGreaterEqual(len(notes), 3)

if __name__ == "__main__":
    unittest.main()
