"""Minimal VIC raster + CIA1 timer A model for RSID / play=0 dumps.

Enough to fire music IRQs. Not a full chipset: no timer B digi, no NMI, no
badlines. SID writes are observed by the dump recorder, not modelled here.
"""

from __future__ import annotations

from collections.abc import Callable

from .cpu6502 import Memory

# KERNAL-ish IRQ epilogue: ack CIA1, then PLA TAY; PLA TAX; PLA; RTI.
# RSID leaves CIA1 IRQs enabled; without the ack, $EA31 would re-enter forever.
EA31_STUB = bytes([0xAD, 0x0D, 0xDC, 0x68, 0xA8, 0x68, 0xAA, 0x68, 0x40])
# Real KERNAL restore/RTI starts at $EA7E (skip keyboard). Players such as
# Hubbard Last V8 JMP here after INC $D019. $EA81 is the mid-entry (no TAY).
EA7E_STUB = bytes([0x68, 0xA8, 0x68, 0xAA, 0x68, 0x40])
# Idle after the epilogue. Do not park it at $EA80: that overwrites PLA/TAX.
IDLE_ADDR = 0xEA84
IDLE_STUB = bytes([0xEA, 0x4C, 0x84, 0xEA])  # NOP; JMP $EA84

PAL_CYCLES_PER_LINE = 63
PAL_LINES = 312
NTSC_CYCLES_PER_LINE = 65
NTSC_LINES = 263

CIA1_TA_LO = 0xDC04
CIA1_TA_HI = 0xDC05
CIA1_TB_LO = 0xDC06
CIA1_TB_HI = 0xDC07
CIA1_ICR = 0xDC0D
CIA1_CRA = 0xDC0E
CIA1_CRB = 0xDC0F

VIC_D011 = 0xD011
VIC_D012 = 0xD012
VIC_D019 = 0xD019
VIC_D01A = 0xD01A


def io_visible(port: int) -> bool:
    low = port & 7
    return bool((low & 4) and (low & 3))


class IrqHarness:
    """Advance VIC/CIA1 with CPU cycles and report a level-triggered IRQ."""

    def __init__(self, clock: str = "PAL") -> None:
        if clock == "NTSC":
            self.cycles_per_line = NTSC_CYCLES_PER_LINE
            self.lines_per_frame = NTSC_LINES
        else:
            self.cycles_per_line = PAL_CYCLES_PER_LINE
            self.lines_per_frame = PAL_LINES
        self.cycles_per_frame = self.cycles_per_line * self.lines_per_frame
        self.total_cycles = 0
        self.raster_line = 0
        self.cycle_in_line = 0
        # 9-bit raster compare. $D011 bit7 is compare bit8 on write; on read
        # that bit is the live raster bit8 instead.
        self.raster_compare = 0x137
        self.d011 = 0x1B
        self.d019 = 0
        self.d01a = 0
        self._raster_armed = True
        self.ta_latch = 0xFFFF
        self.ta_counter = 0xFFFF
        self.tb_latch = 0xFFFF
        self.cra = 0
        self.crb = 0
        self.icr_data = 0
        self.icr_mask = 0
        self._cia_irq = False

    @property
    def frame_index(self) -> int:
        return self.total_cycles // self.cycles_per_frame

    def reset_rsid(self, cia_latch: int) -> None:
        """HVSC RSID power-on: raster $137 disabled, CIA1 TA 60 Hz running."""
        self.raster_compare = 0x137
        self.d011 = 0x1B
        self.d019 = 0
        self.d01a = 0
        self._raster_armed = True
        self.ta_latch = cia_latch & 0xFFFF
        self.ta_counter = self.ta_latch
        self.tb_latch = 0xFFFF
        self.cra = 0x01
        self.crb = 0
        self.icr_data = 0
        self.icr_mask = 0x01
        self._cia_irq = False
        self.raster_line = 0
        self.cycle_in_line = 0
        self.total_cycles = 0

    def irq_pending(self) -> bool:
        raster = bool(self.d019 & 0x01) and bool(self.d01a & 0x01)
        return raster or self._cia_irq

    def cycles_until_irq(self) -> int:
        """Chip cycles until an IRQ line would assert (CPU still in idle)."""
        if self.irq_pending():
            return 0
        waits: list[int] = []
        if self.d01a & 0x01:
            waits.append(self._cycles_until_raster())
        if (self.cra & 0x01) and (self.icr_mask & 0x01):
            waits.append(self.ta_counter + 1)
        if not waits:
            return self.cycles_per_frame
        return max(1, min(waits))

    def _cycles_until_raster(self) -> int:
        cpl = self.cycles_per_line
        cmp_line = self.raster_compare & 0x1FF
        if cmp_line >= self.lines_per_frame:
            cmp_line %= self.lines_per_frame
        cur = self.raster_line * cpl + self.cycle_in_line
        cmp_abs = cmp_line * cpl
        if self._raster_armed and cur < cmp_abs:
            return cmp_abs - cur
        if self._raster_armed and cur == cmp_abs and self.cycle_in_line == 0:
            return 1
        return self.cycles_per_frame - cur + cmp_abs

    def advance(self, cycles: int) -> None:
        if cycles <= 0:
            return
        self.total_cycles += cycles
        self._advance_vic(cycles)
        self._advance_cia(cycles)

    def _fire_raster(self) -> None:
        self.d019 |= 0x01
        if self.d01a & 0x01:
            self.d019 |= 0x80
        self._raster_armed = False

    def _advance_vic(self, cycles: int) -> None:
        # Batch whole lines, then the remainder. Raster IRQ arms once per visit
        # to the compare line (re-armed when the line changes).
        remaining = cycles
        while remaining > 0:
            room = self.cycles_per_line - self.cycle_in_line
            step = remaining if remaining < room else room
            prev_line = self.raster_line
            self.cycle_in_line += step
            remaining -= step
            if self.cycle_in_line >= self.cycles_per_line:
                self.cycle_in_line = 0
                self.raster_line = (self.raster_line + 1) % self.lines_per_frame
                self._raster_armed = True
            if (
                self._raster_armed
                and self.raster_line == self.raster_compare
                and self.cycle_in_line == 0
            ):
                self._fire_raster()
            elif (
                self._raster_armed
                and prev_line != self.raster_line
                and self.raster_line == self.raster_compare
            ):
                self._fire_raster()

    def _advance_cia(self, cycles: int) -> None:
        if not (self.cra & 0x01):
            return
        period = self.ta_latch + 1
        if period <= 0:
            return
        # Countdown: after `ta_counter` cycles we underflow once, then every
        # `period` cycles. Continuous mode reloads the latch each time.
        left = cycles
        while left > 0:
            if self.ta_counter >= left:
                self.ta_counter -= left
                return
            left -= self.ta_counter + 1
            self.ta_counter = self.ta_latch
            self.icr_data |= 0x01
            if self.icr_mask & 0x01:
                self.icr_data |= 0x80
                self._cia_irq = True

    def read_io(self, address: int) -> int | None:
        address &= 0xFFFF
        if address == VIC_D011:
            return (self.d011 & 0x7F) | (0x80 if self.raster_line & 0x100 else 0)
        if address == VIC_D012:
            return self.raster_line & 0xFF
        if address == VIC_D019:
            status = self.d019 & 0x0F
            if status & self.d01a:
                status |= 0x80
            return status
        if address == VIC_D01A:
            return self.d01a
        if address == CIA1_TA_LO:
            return self.ta_counter & 0xFF
        if address == CIA1_TA_HI:
            return (self.ta_counter >> 8) & 0xFF
        if address == CIA1_TB_LO:
            return self.tb_latch & 0xFF
        if address == CIA1_TB_HI:
            return (self.tb_latch >> 8) & 0xFF
        if address == CIA1_ICR:
            value = self.icr_data
            self.icr_data = 0
            self._cia_irq = False
            return value
        if address == CIA1_CRA:
            return self.cra
        if address == CIA1_CRB:
            return self.crb
        return None

    def write_io(self, address: int, value: int) -> bool:
        address &= 0xFFFF
        value &= 0xFF
        if address == VIC_D011:
            self.d011 = value & 0x7F
            self.raster_compare = (self.raster_compare & 0xFF) | (
                0x100 if value & 0x80 else 0
            )
            self._raster_armed = True
            return True
        if address == VIC_D012:
            self.raster_compare = (self.raster_compare & 0x100) | value
            self._raster_armed = True
            return True
        if address == VIC_D019:
            self.d019 &= ~(value & 0x0F)
            return True
        if address == VIC_D01A:
            self.d01a = value & 0x0F
            return True
        if address == CIA1_TA_LO:
            self.ta_latch = (self.ta_latch & 0xFF00) | value
            return True
        if address == CIA1_TA_HI:
            self.ta_latch = (self.ta_latch & 0x00FF) | (value << 8)
            if not (self.cra & 0x01):
                self.ta_counter = self.ta_latch
            return True
        if address == CIA1_TB_LO:
            self.tb_latch = (self.tb_latch & 0xFF00) | value
            return True
        if address == CIA1_TB_HI:
            self.tb_latch = (self.tb_latch & 0x00FF) | (value << 8)
            return True
        if address == CIA1_ICR:
            if value & 0x80:
                self.icr_mask |= value & 0x1F
            else:
                self.icr_mask &= ~(value & 0x1F)
            if not (self.icr_mask & 0x01):
                self._cia_irq = False
            return True
        if address == CIA1_CRA:
            starting = bool(value & 0x01) and not (self.cra & 0x01)
            self.cra = value
            if starting:
                self.ta_counter = self.ta_latch
            return True
        if address == CIA1_CRB:
            self.crb = value
            return True
        return False


class HarnessMemory(Memory):
    """64K RAM with VIC/CIA1 I/O when $01 maps the I/O block in."""

    def __init__(
        self,
        harness: IrqHarness,
        on_write: Callable[[int, int], None] | None = None,
    ) -> None:
        super().__init__(on_write)
        self.harness = harness

    def read(self, address: int) -> int:
        address &= 0xFFFF
        if io_visible(self.data[1]) and 0xD000 <= address <= 0xDFFF:
            if 0xD400 <= address <= 0xD7FF:
                return self.data[address]
            value = self.harness.read_io(address)
            if value is not None:
                return value
        return self.data[address]

    def write(self, address: int, value: int) -> None:
        address &= 0xFFFF
        value &= 0xFF
        if io_visible(self.data[1]) and 0xD000 <= address <= 0xDFFF:
            if not (0xD400 <= address <= 0xD7FF):
                self.harness.write_io(address, value)
        self.data[address] = value
        if self.on_write is not None:
            self.on_write(address, value)


def install_kernal_stubs(memory: Memory) -> None:
    """Minimal $EA31 / $EA7E chain and idle spin. Not a real KERNAL image."""
    memory.data[0xEA31 : 0xEA31 + len(EA31_STUB)] = EA31_STUB
    memory.data[0xEA7E : 0xEA7E + len(EA7E_STUB)] = EA7E_STUB
    memory.data[IDLE_ADDR : IDLE_ADDR + len(IDLE_STUB)] = IDLE_STUB
    memory.data[0x0314] = 0x31
    memory.data[0x0315] = 0xEA
