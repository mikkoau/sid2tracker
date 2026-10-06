"""Focused tests for the PSID subroutine interpreter."""

from __future__ import annotations

import unittest

from sidengine.cpu6502 import C, CPU6502, Memory

class CPU6502Tests(unittest.TestCase):
    def run_code(self, code: bytes, address: int = 0x1000) -> CPU6502:
        memory = Memory()
        memory.data[address : address + len(code)] = code
        cpu = CPU6502(memory)
        cpu.call(address)
        return cpu

    def test_load_add_store_and_return(self) -> None:
        # LDA #$7f; CLC; ADC #1; STA $2000; RTS
        cpu = self.run_code(bytes([0xA9, 0x7F, 0x18, 0x69, 0x01, 0x8D, 0x00, 0x20, 0x60]))
        self.assertEqual(cpu.a, 0x80)
        self.assertEqual(cpu.mem.read(0x2000), 0x80)

    def test_jsr_and_stack(self) -> None:
        # JSR $1006; STA $2000; RTS; sub: LDA #$42; RTS
        code = bytes([0x20, 0x07, 0x10, 0x8D, 0x00, 0x20, 0x60, 0xA9, 0x42, 0x60])
        cpu = self.run_code(code)
        self.assertEqual(cpu.mem.read(0x2000), 0x42)

    def test_indexed_indirect_and_branch(self) -> None:
        # Install $1234 in zp $10, load through ($0e,X), compare and branch.
        code = bytes([
            0xA9, 0x34, 0x85, 0x10, 0xA9, 0x12, 0x85, 0x11,
            0xA9, 0x55, 0x8D, 0x34, 0x12, 0xA2, 0x02, 0xA1, 0x0E,
            0xC9, 0x55, 0xD0, 0x03, 0x8D, 0x00, 0x20, 0x60,
        ])
        cpu = self.run_code(code)
        self.assertEqual(cpu.mem.read(0x2000), 0x55)

    def test_undocumented_lax_sax(self) -> None:
        # LAX #$cc; SAX $2000; RTS
        cpu = self.run_code(bytes([0xAB, 0xCC, 0x8F, 0x00, 0x20, 0x60]))
        self.assertEqual(cpu.a, 0xCC)
        self.assertEqual(cpu.x, 0xCC)
        self.assertEqual(cpu.mem.read(0x2000), 0xCC)

    def test_decimal_adc(self) -> None:
        # SED; CLC; LDA #$49; ADC #$51; RTS -> 00 carry
        cpu = self.run_code(bytes([0xF8, 0x18, 0xA9, 0x49, 0x69, 0x51, 0x60]))
        self.assertEqual(cpu.a, 0)
        self.assertTrue(cpu.p & C)

    def test_irq_uses_0314_when_kernal_mapped(self) -> None:
        memory = Memory()
        memory.data[1] = 0x37
        memory.data[0x0314] = 0x31
        memory.data[0x0315] = 0xEA
        cpu = CPU6502(memory)
        cpu.pc = 0x2000
        cpu.p &= ~0x04
        self.assertTrue(cpu.trigger_irq())
        self.assertEqual(cpu.pc, 0xEA31)

    def test_irq_uses_fffe_when_kernal_paged_out(self) -> None:
        memory = Memory()
        memory.data[1] = 0x35
        memory.data[0x0314] = 0x31
        memory.data[0x0315] = 0xEA
        memory.data[0xFFFE] = 0x00
        memory.data[0xFFFF] = 0x1E
        cpu = CPU6502(memory)
        cpu.pc = 0x2000
        cpu.p &= ~0x04
        self.assertTrue(cpu.trigger_irq())
        self.assertEqual(cpu.pc, 0x1E00)

if __name__ == "__main__":
    unittest.main()
