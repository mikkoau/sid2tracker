"""Small NMOS 6502/6510 interpreter for deterministic PSID/RSID calls.

This is not a cycle-exact C64 emulator. Caller-driven PSID runs one init/play
subroutine at a time. The IRQ harness steps the same core under VIC/CIA ticks.
"""

from __future__ import annotations

from collections.abc import Callable

C = 0x01
Z = 0x02
I = 0x04
D = 0x08
B = 0x10
U = 0x20
V = 0x40
N = 0x80


class CPUError(RuntimeError):
    pass


class Memory:
    def __init__(self, on_write: Callable[[int, int], None] | None = None):
        self.data = bytearray(65536)
        self.on_write = on_write

    def read(self, address: int) -> int:
        return self.data[address & 0xFFFF]

    def write(self, address: int, value: int) -> None:
        address &= 0xFFFF
        value &= 0xFF
        self.data[address] = value
        if self.on_write is not None:
            self.on_write(address, value)


# Official opcode matrix. Cycles are sufficient for runaway detection, not
# bus-level timing.
OPS: dict[int, tuple[str, str, int]] = {}


def _add(op: str, mode: str, cycles: int, *codes: int) -> None:
    for code in codes:
        OPS[code] = (op, mode, cycles)


_add("BRK", "imp", 7, 0x00)
_add("ORA", "izx", 6, 0x01)
_add("ORA", "zp", 3, 0x05)
_add("ASL", "zp", 5, 0x06)
_add("PHP", "imp", 3, 0x08)
_add("ORA", "imm", 2, 0x09)
_add("ASL", "acc", 2, 0x0A)
_add("ORA", "abs", 4, 0x0D)
_add("ASL", "abs", 6, 0x0E)
_add("BPL", "rel", 2, 0x10)
_add("ORA", "izy", 5, 0x11)
_add("ORA", "zpx", 4, 0x15)
_add("ASL", "zpx", 6, 0x16)
_add("CLC", "imp", 2, 0x18)
_add("ORA", "aby", 4, 0x19)
_add("ORA", "abx", 4, 0x1D)
_add("ASL", "abx", 7, 0x1E)
_add("JSR", "abs", 6, 0x20)
_add("AND", "izx", 6, 0x21)
_add("BIT", "zp", 3, 0x24)
_add("AND", "zp", 3, 0x25)
_add("ROL", "zp", 5, 0x26)
_add("PLP", "imp", 4, 0x28)
_add("AND", "imm", 2, 0x29)
_add("ROL", "acc", 2, 0x2A)
_add("BIT", "abs", 4, 0x2C)
_add("AND", "abs", 4, 0x2D)
_add("ROL", "abs", 6, 0x2E)
_add("BMI", "rel", 2, 0x30)
_add("AND", "izy", 5, 0x31)
_add("AND", "zpx", 4, 0x35)
_add("ROL", "zpx", 6, 0x36)
_add("SEC", "imp", 2, 0x38)
_add("AND", "aby", 4, 0x39)
_add("AND", "abx", 4, 0x3D)
_add("ROL", "abx", 7, 0x3E)
_add("RTI", "imp", 6, 0x40)
_add("EOR", "izx", 6, 0x41)
_add("EOR", "zp", 3, 0x45)
_add("LSR", "zp", 5, 0x46)
_add("PHA", "imp", 3, 0x48)
_add("EOR", "imm", 2, 0x49)
_add("LSR", "acc", 2, 0x4A)
_add("JMP", "abs", 3, 0x4C)
_add("EOR", "abs", 4, 0x4D)
_add("LSR", "abs", 6, 0x4E)
_add("BVC", "rel", 2, 0x50)
_add("EOR", "izy", 5, 0x51)
_add("EOR", "zpx", 4, 0x55)
_add("LSR", "zpx", 6, 0x56)
_add("CLI", "imp", 2, 0x58)
_add("EOR", "aby", 4, 0x59)
_add("EOR", "abx", 4, 0x5D)
_add("LSR", "abx", 7, 0x5E)
_add("RTS", "imp", 6, 0x60)
_add("ADC", "izx", 6, 0x61)
_add("ADC", "zp", 3, 0x65)
_add("ROR", "zp", 5, 0x66)
_add("PLA", "imp", 4, 0x68)
_add("ADC", "imm", 2, 0x69)
_add("ROR", "acc", 2, 0x6A)
_add("JMP", "ind", 5, 0x6C)
_add("ADC", "abs", 4, 0x6D)
_add("ROR", "abs", 6, 0x6E)
_add("BVS", "rel", 2, 0x70)
_add("ADC", "izy", 5, 0x71)
_add("ADC", "zpx", 4, 0x75)
_add("ROR", "zpx", 6, 0x76)
_add("SEI", "imp", 2, 0x78)
_add("ADC", "aby", 4, 0x79)
_add("ADC", "abx", 4, 0x7D)
_add("ROR", "abx", 7, 0x7E)
_add("STA", "izx", 6, 0x81)
_add("STY", "zp", 3, 0x84)
_add("STA", "zp", 3, 0x85)
_add("STX", "zp", 3, 0x86)
_add("DEY", "imp", 2, 0x88)
_add("TXA", "imp", 2, 0x8A)
_add("STY", "abs", 4, 0x8C)
_add("STA", "abs", 4, 0x8D)
_add("STX", "abs", 4, 0x8E)
_add("BCC", "rel", 2, 0x90)
_add("STA", "izy", 6, 0x91)
_add("STY", "zpx", 4, 0x94)
_add("STA", "zpx", 4, 0x95)
_add("STX", "zpy", 4, 0x96)
_add("TYA", "imp", 2, 0x98)
_add("STA", "aby", 5, 0x99)
_add("TXS", "imp", 2, 0x9A)
_add("STA", "abx", 5, 0x9D)
_add("LDY", "imm", 2, 0xA0)
_add("LDA", "izx", 6, 0xA1)
_add("LDX", "imm", 2, 0xA2)
_add("LDY", "zp", 3, 0xA4)
_add("LDA", "zp", 3, 0xA5)
_add("LDX", "zp", 3, 0xA6)
_add("TAY", "imp", 2, 0xA8)
_add("LDA", "imm", 2, 0xA9)
_add("TAX", "imp", 2, 0xAA)
_add("LDY", "abs", 4, 0xAC)
_add("LDA", "abs", 4, 0xAD)
_add("LDX", "abs", 4, 0xAE)
_add("BCS", "rel", 2, 0xB0)
_add("LDA", "izy", 5, 0xB1)
_add("LDY", "zpx", 4, 0xB4)
_add("LDA", "zpx", 4, 0xB5)
_add("LDX", "zpy", 4, 0xB6)
_add("CLV", "imp", 2, 0xB8)
_add("LDA", "aby", 4, 0xB9)
_add("TSX", "imp", 2, 0xBA)
_add("LDY", "abx", 4, 0xBC)
_add("LDA", "abx", 4, 0xBD)
_add("LDX", "aby", 4, 0xBE)
_add("CPY", "imm", 2, 0xC0)
_add("CMP", "izx", 6, 0xC1)
_add("CPY", "zp", 3, 0xC4)
_add("CMP", "zp", 3, 0xC5)
_add("DEC", "zp", 5, 0xC6)
_add("INY", "imp", 2, 0xC8)
_add("CMP", "imm", 2, 0xC9)
_add("DEX", "imp", 2, 0xCA)
_add("CPY", "abs", 4, 0xCC)
_add("CMP", "abs", 4, 0xCD)
_add("DEC", "abs", 6, 0xCE)
_add("BNE", "rel", 2, 0xD0)
_add("CMP", "izy", 5, 0xD1)
_add("CMP", "zpx", 4, 0xD5)
_add("DEC", "zpx", 6, 0xD6)
_add("CLD", "imp", 2, 0xD8)
_add("CMP", "aby", 4, 0xD9)
_add("CMP", "abx", 4, 0xDD)
_add("DEC", "abx", 7, 0xDE)
_add("CPX", "imm", 2, 0xE0)
_add("SBC", "izx", 6, 0xE1)
_add("CPX", "zp", 3, 0xE4)
_add("SBC", "zp", 3, 0xE5)
_add("INC", "zp", 5, 0xE6)
_add("INX", "imp", 2, 0xE8)
_add("SBC", "imm", 2, 0xE9, 0xEB)
_add("NOP", "imp", 2, 0xEA)
_add("CPX", "abs", 4, 0xEC)
_add("SBC", "abs", 4, 0xED)
_add("INC", "abs", 6, 0xEE)
_add("BEQ", "rel", 2, 0xF0)
_add("SBC", "izy", 5, 0xF1)
_add("SBC", "zpx", 4, 0xF5)
_add("INC", "zpx", 6, 0xF6)
_add("SED", "imp", 2, 0xF8)
_add("SBC", "aby", 4, 0xF9)
_add("SBC", "abx", 4, 0xFD)
_add("INC", "abx", 7, 0xFE)

# Stable undocumented instructions used by many C64 players.
for op, codes_modes in {
    "LAX": [(0xA3, "izx"), (0xA7, "zp"), (0xAF, "abs"), (0xB3, "izy"),
            (0xB7, "zpy"), (0xBF, "aby")],
    "SAX": [(0x83, "izx"), (0x87, "zp"), (0x8F, "abs"), (0x97, "zpy")],
    "DCP": [(0xC3, "izx"), (0xC7, "zp"), (0xCF, "abs"), (0xD3, "izy"),
            (0xD7, "zpx"), (0xDB, "aby"), (0xDF, "abx")],
    "ISC": [(0xE3, "izx"), (0xE7, "zp"), (0xEF, "abs"), (0xF3, "izy"),
            (0xF7, "zpx"), (0xFB, "aby"), (0xFF, "abx")],
    "SLO": [(0x03, "izx"), (0x07, "zp"), (0x0F, "abs"), (0x13, "izy"),
            (0x17, "zpx"), (0x1B, "aby"), (0x1F, "abx")],
    "RLA": [(0x23, "izx"), (0x27, "zp"), (0x2F, "abs"), (0x33, "izy"),
            (0x37, "zpx"), (0x3B, "aby"), (0x3F, "abx")],
    "SRE": [(0x43, "izx"), (0x47, "zp"), (0x4F, "abs"), (0x53, "izy"),
            (0x57, "zpx"), (0x5B, "aby"), (0x5F, "abx")],
    "RRA": [(0x63, "izx"), (0x67, "zp"), (0x6F, "abs"), (0x73, "izy"),
            (0x77, "zpx"), (0x7B, "aby"), (0x7F, "abx")],
}.items():
    for code, mode in codes_modes:
        OPS[code] = (op, mode, 8)

for code, mode in {
    0x04: "zp", 0x0C: "abs", 0x14: "zpx", 0x1A: "imp", 0x1C: "abx",
    0x34: "zpx", 0x3A: "imp", 0x3C: "abx", 0x44: "zp", 0x54: "zpx",
    0x5A: "imp", 0x5C: "abx", 0x64: "zp", 0x74: "zpx", 0x7A: "imp",
    0x7C: "abx", 0x80: "imm", 0x82: "imm", 0x89: "imm", 0xC2: "imm",
    0xD4: "zpx", 0xDA: "imp", 0xDC: "abx", 0xE2: "imm", 0xF4: "zpx",
    0xFA: "imp", 0xFC: "abx",
}.items():
    OPS[code] = ("NOP", mode, 2)

_add("ANC", "imm", 2, 0x0B, 0x2B)
_add("ALR", "imm", 2, 0x4B)
_add("ARR", "imm", 2, 0x6B)
_add("XAA", "imm", 2, 0x8B)
_add("LAX", "imm", 2, 0xAB)
_add("AXS", "imm", 2, 0xCB)
_add("LAS", "aby", 4, 0xBB)
_add("AHX", "izy", 6, 0x93)
_add("AHX", "aby", 5, 0x9F)
_add("TAS", "aby", 5, 0x9B)
_add("SHY", "abx", 5, 0x9C)
_add("SHX", "aby", 5, 0x9E)


class CPU6502:
    def __init__(self, memory: Memory):
        self.mem = memory
        self.a = 0
        self.x = 0
        self.y = 0
        self.sp = 0xFF
        self.p = U | I
        self.pc = 0
        self.cycles = 0

    def _read16(self, address: int) -> int:
        lo = self.mem.read(address)
        hi = self.mem.read((address + 1) & 0xFFFF)
        return lo | (hi << 8)

    def _fetch(self) -> int:
        value = self.mem.read(self.pc)
        self.pc = (self.pc + 1) & 0xFFFF
        return value

    def _fetch16(self) -> int:
        lo = self._fetch()
        return lo | (self._fetch() << 8)

    def _push(self, value: int) -> None:
        self.mem.write(0x100 | self.sp, value)
        self.sp = (self.sp - 1) & 0xFF

    def _pull(self) -> int:
        self.sp = (self.sp + 1) & 0xFF
        return self.mem.read(0x100 | self.sp)

    def _nz(self, value: int) -> int:
        value &= 0xFF
        self.p = (self.p & ~(N | Z)) | (N if value & 0x80 else 0) | (Z if value == 0 else 0)
        return value

    def _set_flag(self, flag: int, condition: bool) -> None:
        self.p = (self.p | flag) if condition else (self.p & ~flag)

    def _address(self, mode: str) -> tuple[int | None, bool]:
        if mode == "imp" or mode == "acc":
            return None, False
        if mode == "imm":
            address = self.pc
            self.pc = (self.pc + 1) & 0xFFFF
            return address, False
        if mode == "zp":
            return self._fetch(), False
        if mode == "zpx":
            return (self._fetch() + self.x) & 0xFF, False
        if mode == "zpy":
            return (self._fetch() + self.y) & 0xFF, False
        if mode == "abs":
            return self._fetch16(), False
        if mode in ("abx", "aby"):
            base = self._fetch16()
            address = (base + (self.x if mode == "abx" else self.y)) & 0xFFFF
            return address, (base & 0xFF00) != (address & 0xFF00)
        if mode == "izx":
            zp = (self._fetch() + self.x) & 0xFF
            return self.mem.read(zp) | (self.mem.read((zp + 1) & 0xFF) << 8), False
        if mode == "izy":
            zp = self._fetch()
            base = self.mem.read(zp) | (self.mem.read((zp + 1) & 0xFF) << 8)
            address = (base + self.y) & 0xFFFF
            return address, (base & 0xFF00) != (address & 0xFF00)
        if mode == "ind":
            pointer = self._fetch16()
            # NMOS JMP ($xxFF) page-wrap bug.
            lo = self.mem.read(pointer)
            hi = self.mem.read((pointer & 0xFF00) | ((pointer + 1) & 0xFF))
            return lo | (hi << 8), False
        if mode == "rel":
            offset = self._fetch()
            return offset - 0x100 if offset & 0x80 else offset, False
        raise CPUError(f"unknown addressing mode {mode}")

    def _adc(self, value: int) -> None:
        carry = 1 if self.p & C else 0
        binary = self.a + value + carry
        result = binary & 0xFF
        self._set_flag(V, bool((~(self.a ^ value) & (self.a ^ result)) & 0x80))
        if self.p & D:
            lo = (self.a & 0x0F) + (value & 0x0F) + carry
            hi = (self.a >> 4) + (value >> 4)
            if lo > 9:
                lo += 6
                hi += 1
            if hi > 9:
                hi += 6
            self._set_flag(C, hi > 15)
            result = ((hi << 4) | (lo & 0x0F)) & 0xFF
        else:
            self._set_flag(C, binary > 0xFF)
        self.a = self._nz(result)

    def _sbc(self, value: int) -> None:
        if self.p & D:
            carry = 1 if self.p & C else 0
            diff = self.a - value - (1 - carry)
            binary_result = diff & 0xFF
            self._set_flag(V, bool(((self.a ^ binary_result) & (self.a ^ value)) & 0x80))
            lo = (self.a & 0x0F) - (value & 0x0F) - (1 - carry)
            hi = (self.a >> 4) - (value >> 4)
            if lo < 0:
                lo -= 6
                hi -= 1
            if hi < 0:
                hi -= 6
            self._set_flag(C, diff >= 0)
            self.a = self._nz(((hi << 4) | (lo & 0x0F)) & 0xFF)
        else:
            self._adc(value ^ 0xFF)

    def _compare(self, register: int, value: int) -> None:
        self._set_flag(C, register >= value)
        self._nz(register - value)

    def _rmw(self, op: str, value: int) -> int:
        if op == "ASL":
            self._set_flag(C, bool(value & 0x80))
            return self._nz(value << 1)
        if op == "LSR":
            self._set_flag(C, bool(value & 1))
            return self._nz(value >> 1)
        if op == "ROL":
            carry = 1 if self.p & C else 0
            self._set_flag(C, bool(value & 0x80))
            return self._nz((value << 1) | carry)
        if op == "ROR":
            carry = 0x80 if self.p & C else 0
            self._set_flag(C, bool(value & 1))
            return self._nz((value >> 1) | carry)
        if op == "INC":
            return self._nz(value + 1)
        if op == "DEC":
            return self._nz(value - 1)
        raise CPUError(f"not read-modify-write: {op}")

    def step(self) -> None:
        start = self.pc
        opcode = self._fetch()
        try:
            op, mode, cycles = OPS[opcode]
        except KeyError as exc:
            raise CPUError(f"unsupported opcode ${opcode:02X} at ${start:04X}") from exc
        address, crossed = self._address(mode)
        self.cycles += cycles

        if op in ("ORA", "AND", "EOR", "ADC", "SBC", "CMP", "CPX", "CPY",
                  "LDA", "LDX", "LDY", "LAX", "ANC", "ALR", "ARR", "XAA",
                  "AXS", "LAS"):
            assert address is not None
            value = self.mem.read(address)
            if op == "ORA":
                self.a = self._nz(self.a | value)
            elif op == "AND":
                self.a = self._nz(self.a & value)
            elif op == "EOR":
                self.a = self._nz(self.a ^ value)
            elif op == "ADC":
                self._adc(value)
            elif op == "SBC":
                self._sbc(value)
            elif op == "CMP":
                self._compare(self.a, value)
            elif op == "CPX":
                self._compare(self.x, value)
            elif op == "CPY":
                self._compare(self.y, value)
            elif op == "LDA":
                self.a = self._nz(value)
            elif op == "LDX":
                self.x = self._nz(value)
            elif op == "LDY":
                self.y = self._nz(value)
            elif op == "LAX":
                self.a = self.x = self._nz(value)
            elif op == "ANC":
                self.a = self._nz(self.a & value)
                self._set_flag(C, bool(self.a & 0x80))
            elif op == "ALR":
                self.a &= value
                self._set_flag(C, bool(self.a & 1))
                self.a = self._nz(self.a >> 1)
            elif op == "ARR":
                self.a &= value
                self.a = self._nz((self.a >> 1) | (0x80 if self.p & C else 0))
                self._set_flag(C, bool(self.a & 0x40))
                self._set_flag(V, bool((self.a ^ (self.a << 1)) & 0x40))
            elif op == "XAA":
                self.a = self._nz(self.x & value)
            elif op == "AXS":
                result = (self.a & self.x) - value
                self._set_flag(C, result >= 0)
                self.x = self._nz(result)
            elif op == "LAS":
                self.a = self.x = self.sp = self._nz(value & self.sp)
            if crossed and mode in ("abx", "aby", "izy"):
                self.cycles += 1
            return

        if op in ("STA", "STX", "STY", "SAX", "AHX", "TAS", "SHX", "SHY"):
            assert address is not None
            if op == "STA":
                value = self.a
            elif op == "STX":
                value = self.x
            elif op == "STY":
                value = self.y
            elif op == "SAX":
                value = self.a & self.x
            else:
                high_plus_1 = ((address >> 8) + 1) & 0xFF
                if op == "TAS":
                    self.sp = self.a & self.x
                    value = self.sp & high_plus_1
                elif op == "SHX":
                    value = self.x & high_plus_1
                elif op == "SHY":
                    value = self.y & high_plus_1
                else:
                    value = self.a & self.x & high_plus_1
            self.mem.write(address, value)
            return

        if op in ("ASL", "LSR", "ROL", "ROR", "INC", "DEC"):
            if mode == "acc":
                self.a = self._rmw(op, self.a)
            else:
                assert address is not None
                original = self.mem.read(address)
                # NMOS RMW writes the original value before the result. Music
                # players rely on that for INC $D019 ack.
                self.mem.write(address, original)
                self.mem.write(address, self._rmw(op, original))
            return

        if op in ("DCP", "ISC", "SLO", "RLA", "SRE", "RRA"):
            assert address is not None
            base_op = {
                "DCP": "DEC", "ISC": "INC", "SLO": "ASL",
                "RLA": "ROL", "SRE": "LSR", "RRA": "ROR",
            }[op]
            original = self.mem.read(address)
            self.mem.write(address, original)
            value = self._rmw(base_op, original)
            self.mem.write(address, value)
            if op == "DCP":
                self._compare(self.a, value)
            elif op == "ISC":
                self._sbc(value)
            elif op == "SLO":
                self.a = self._nz(self.a | value)
            elif op == "RLA":
                self.a = self._nz(self.a & value)
            elif op == "SRE":
                self.a = self._nz(self.a ^ value)
            else:
                self._adc(value)
            return

        if op == "BIT":
            assert address is not None
            value = self.mem.read(address)
            self._set_flag(Z, (self.a & value) == 0)
            self.p = (self.p & ~(N | V)) | (value & (N | V))
        elif op == "JMP":
            assert address is not None
            self.pc = address
        elif op == "JSR":
            assert address is not None
            return_address = (self.pc - 1) & 0xFFFF
            self._push(return_address >> 8)
            self._push(return_address)
            self.pc = address
        elif op == "RTS":
            self.pc = ((self._pull() | (self._pull() << 8)) + 1) & 0xFFFF
        elif op == "RTI":
            self.p = (self._pull() & ~B) | U
            self.pc = self._pull() | (self._pull() << 8)
        elif op == "BRK":
            self.pc = (self.pc + 1) & 0xFFFF
            self._push(self.pc >> 8)
            self._push(self.pc)
            self._push(self.p | B | U)
            self.p |= I
            self.pc = self._read16(0xFFFE)
        elif op == "PHA":
            self._push(self.a)
        elif op == "PHP":
            self._push(self.p | B | U)
        elif op == "PLA":
            self.a = self._nz(self._pull())
        elif op == "PLP":
            self.p = (self._pull() & ~B) | U
        elif op in ("BPL", "BMI", "BVC", "BVS", "BCC", "BCS", "BNE", "BEQ"):
            assert address is not None
            condition = {
                "BPL": not self.p & N, "BMI": bool(self.p & N),
                "BVC": not self.p & V, "BVS": bool(self.p & V),
                "BCC": not self.p & C, "BCS": bool(self.p & C),
                "BNE": not self.p & Z, "BEQ": bool(self.p & Z),
            }[op]
            if condition:
                old = self.pc
                self.pc = (self.pc + address) & 0xFFFF
                self.cycles += 1 + ((old & 0xFF00) != (self.pc & 0xFF00))
        elif op == "CLC":
            self.p &= ~C
        elif op == "SEC":
            self.p |= C
        elif op == "CLI":
            self.p &= ~I
        elif op == "SEI":
            self.p |= I
        elif op == "CLV":
            self.p &= ~V
        elif op == "CLD":
            self.p &= ~D
        elif op == "SED":
            self.p |= D
        elif op == "TAX":
            self.x = self._nz(self.a)
        elif op == "TAY":
            self.y = self._nz(self.a)
        elif op == "TXA":
            self.a = self._nz(self.x)
        elif op == "TYA":
            self.a = self._nz(self.y)
        elif op == "TSX":
            self.x = self._nz(self.sp)
        elif op == "TXS":
            self.sp = self.x
        elif op == "DEX":
            self.x = self._nz(self.x - 1)
        elif op == "DEY":
            self.y = self._nz(self.y - 1)
        elif op == "INX":
            self.x = self._nz(self.x + 1)
        elif op == "INY":
            self.y = self._nz(self.y + 1)
        elif op != "NOP":
            raise CPUError(f"unimplemented {op} at ${start:04X}")

    def trigger_irq(self) -> bool:
        """Enter IRQ the way KERNAL or a hijacked $FFFE handler expects.

        Hardware always pushes P/PC and sets I. If KERNAL is mapped (hiram),
        A/X/Y are pushed and PC becomes ($0314), matching $FF48. If KERNAL is
        RAM ($01 bit 1 clear), PC is ($FFFE) with only the hardware frame, as
        in Jeroen Tel Myth.
        """
        if self.p & I:
            return False
        self._push(self.pc >> 8)
        self._push(self.pc & 0xFF)
        self._push((self.p & ~B) | U)
        self.p |= I
        hiram = bool(self.mem.data[1] & 0x02)
        if hiram:
            self._push(self.a)
            self._push(self.x)
            self._push(self.y)
            self.pc = self._read16(0x0314)
        else:
            self.pc = self._read16(0xFFFE)
        self.cycles += 7
        return True

    def call(self, address: int, a: int = 0, max_cycles: int = 200_000) -> int:
        """Call a subroutine and stop when its outermost RTS returns."""
        sentinel = 0xFFFF
        old_cycles = self.cycles
        self.a = a & 0xFF
        self.x = 0
        self.y = 0
        self.sp = 0xFF
        # RTS adds one, so push sentinel-1 in JSR high/low order.
        return_address = sentinel - 1
        self._push(return_address >> 8)
        self._push(return_address & 0xFF)
        self.pc = address & 0xFFFF
        while self.pc != sentinel:
            self.step()
            if self.cycles - old_cycles > max_cycles:
                raise CPUError(
                    f"routine ${address:04X} exceeded {max_cycles} cycles "
                    f"(pc=${self.pc:04X})"
                )
        return self.cycles - old_cycles
