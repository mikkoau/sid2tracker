# PSID vs RSID, and what SID2Tracker can convert

Which files we accept, which dump path the CLI takes, and what still fails.
The conversion pipeline after the header is in
[how-it-works.md](how-it-works.md). CLI flags are in the
[README](../README.md).

## What a `.sid` file is

A `.sid` is a small HVSC wrapper plus a 6510 binary that writes the SID
register area (`$D400-$D418`). This project does not parse a player family.
If the binary cannot be executed under the dump harness, there is no
conversion. A file that parses can still be skipped at dump time.

## PSID and RSID

Older PlaySID-style emulators called a `play` address once per frame. They
did not emulate a full CIA/VIC interrupt environment. Tunes that busy-loop,
install extra IRQs, play samples off CIA timer B, or need real power-on
hardware would lock those emulators.

HVSC split the collection:

| Magic | Meaning |
| ----- | ------- |
| `PSID` | PlaySID-compatible *or* close enough that a caller-driven `init`/`play` works. `playAddress` is usually nonzero. |
| `RSID` | Real SID. Must be given a C64 power-on environment and must configure hardware itself. `playAddress` is always 0. |

RSID is not "harder PSID". It is a different contract. The header
`loadAddress`, `playAddress`, and `speed` fields are reserved 0. The real
load address is the first two payload bytes (little-endian), and must be at
least `$07E8` so it does not sit in zero page / stack / KERNAL workspace.

Version 1 files are PSID only. RSID starts at version 2.

## Call convention

After load:

- `$00` / `$01` start as `$2F` / `$37` (I/O + KERNAL + BASIC visible) unless
  the path rewrites `$01`.
- `$02A6` is `1` PAL or `0` NTSC from the header clock flags.
- `init` is called with `.A = subtune - 1` (song 1 means `A = 0`).
- PSID `init`/`play` also force `$01` from the routine address (HVSC PSID
  bank rule). RSID `init` stays at `$37` until the tune banks itself.

The header `songs` and `startSong` are both live; duration is not in the
header. Subtune selection, HVSC song-length lookup, and `--min-seconds`
are documented in the README.

## Two dump paths

`run_sid` does not key off the magic string alone. It keys off whether the
*caller* is supposed to invoke play:

| Condition | Path |
| --------- | ---- |
| `PSID` and `playAddress != 0` | Caller-driven: `init` once, then `play` once per dump tick |
| `RSID`, or `PSID` with `playAddress == 0` | IRQ harness: `init` with IRQs live, then idle |

A PSID with `playAddress == 0` is a real HVSC case (multi-speed, extra IRQs).
Treat it like RSID for execution even though the magic is still `PSID`.

### Caller-driven PSID

Used for the bulk of HVSC: Hubbard Commando, Huelsbeck Katakis, most game
rips that expose a `play` stub.

- CIA 1 timer A is latched to the 60 Hz default before `init` (`$4025` PAL,
  `$4295` NTSC). If the speed bit says CIA, the post-init latch is the real
  rate (Paperboy rewrites it).
- VIC raster is not modelled. `play` is assumed to return. A busy-wait
  inside `play` hits the 200k-cycle cap and raises `CPUError`.
- There is no IRQ, so `$EA31` / `$FFFE` do not matter.
- Long tracks are slow only because Python steps every `play` (Katakis 1 is
  5:33, about 16k ticks). Long dumps print progress.

### IRQ-driven RSID / play=0

Used when `init` installs the interrupt and must not be followed by a fake
`play` call. Last V8, Myth, and any RSID.

Power-on (HVSC RSID environment, as implemented):

- VIC raster compare `$137`, raster IRQ **not** enabled until the tune says
  so.
- CIA 1 timer A running at 60 Hz with IRQs enabled; other CIA timers latched
  `$FFFF` and stopped.
- Processor port `$01 = $37`: I/O mapped in, KERNAL mapped in (the usual
  RSID start bank). Tunes may page KERNAL out later.
- Minimal KERNAL stubs, not a ROM image (see below).

After `init` returns, the CPU is parked in an idle spin at `$EA84` with the
interrupt-disable flag (`I`) clear, so hardware IRQs the player installed
can actually fire. SID writes are bucketed by video frame
(PAL 312 x 63 cycles, NTSC 263 x 65). A multi-speed raster (several IRQs per
frame) still becomes one dump tick.

Idle time is skipped in chip cycles until the next IRQ. If the CPU is *not*
in that idle stub, or an IRQ is stuck pending, the dump steps every
instruction and looks hung. Last V8 and Myth need that idle skip plus the
correct IRQ exit (`$EA7E` / `$FFFE`).

`init` that never returns within `--init-max-cycles` (default 500k) is
treated as digi/speech and skipped (`DigiSkipError`).

## IRQ vectors this harness actually implements

A real C64 maps the KERNAL ROM into the top of memory. This dump does not
ship a ROM image. That range is ordinary RAM we pre-fill with a few small
stubs (idle loop, IRQ exit helpers). Tunes that expect the real KERNAL
beyond those stubs will not work here.

Hardware IRQs can take two paths, depending on whether the player leaves
KERNAL mapped in (common RSID bank `$37`) or pages it out so RAM owns the
top of memory:

| Memory map | What the harness does |
| ---------- | --------------------- |
| KERNAL in | Save registers the way the KERNAL would, then jump through the soft IRQ vector (`$0314`). Default target is our CIA-ack stub. |
| KERNAL out | Jump through the hardware IRQ vector (`$FFFE`) that the player itself must have written. Only the hardware stack frame is pushed. |

Picking the wrong path is fatal: the IRQ never clears the chip that raised
it, so the same interrupt fires again forever and the dump looks hung.
Tunes that page KERNAL out and install their own music IRQ need the second
row; using the soft vector instead is a common failure mode.

KERNAL-ish stubs we install (same addresses players jump to on a real C64):

| Address | Role |
| ------- | ---- |
| `$EA31` | Default IRQ exit: acknowledge CIA 1, restore registers, return. RSID leaves CIA timer IRQs enabled at power-on, so without this ack the same interrupt re-fires forever. |
| `$EA7E` | Shorter IRQ exit: restore registers and return, without the CIA ack (player already cleared VIC or CIA itself). Last V8 jumps here after acknowledging the raster IRQ. Mid-entry `$EA81` is the same path a couple of bytes later. |
| `$EA84` | Idle loop after `init` returns. The CPU spins here until the next IRQ. Parking on `$EA80` instead would land inside `$EA7E` and corrupt the stack. |

Acknowledging a VIC raster IRQ (`INC $D019`) is modelled as a real NMOS
read-modify-write: a dummy write of the old value, which is what clears the
flag. Writing only the incremented value would miss the ack.

Not installed: `$FF48` as ROM (emulated only when KERNAL is "in"), NMI
(`$FFFA` / `$0318`), BRK `$0316`, CIA 2, timer B, badlines, keyboard.

## Supported (intended to convert)

These convert with `python3 -m sid2it`, one `.it` per subtune, three IT
channels (one per SID voice).

| Kind | Result |
| ---- | ------ |
| PSID v1-v4 with nonzero `playAddress` | Converted (caller-driven `init`/`play`) |
| RSID v2-v4 that return from `init` and use VIC/CIA1 IRQs | Converted (IRQ harness) |
| PSID with `playAddress = 0` | Converted on the IRQ path |
| Compute! MUS player flag | Skipped |
| 2SID / 3SID | Skipped |
| Digi / speech / non-returning `init` | Subtune skipped |
| `$D418` volume-sample digi | Subtune skipped |
| Init-only demos (`init` never returns) | Skipped |
| RSID BASIC flag | Header parses; not executed |

Details beyond the table:

- PAL or NTSC, 6581 or 8580 flags, CIA or vblank speed bit.
- Game rips with many subtunes. Short SFX are skipped by length, not by
  magic (README `--min-seconds`).
- Tunes that hijack `$FFFE` with KERNAL paged out, or `JMP $EA7E` / `$EA31`
  with KERNAL mapped.
- Clock "both" or "unknown": treated as PAL unless the flags say NTSC.

Worked HVSC examples:

| Tune | Header | Result | Why it is a useful case |
| ---- | ------ | ------ | ----------------------- |
| Hubbard Commando (`MUSICIANS/H/Hubbard_Rob/Commando.sid`) | PSID | Converts | Long CIA/vblank caller-driven dump, many SFX subtunes. |
| Huelsbeck Katakis (`MUSICIANS/H/Huelsbeck_Chris/Katakis.sid`) | PSID | Converts | 5:33 first track; silent dump needs progress, not a hung init. |
| Galway MicroProse Soccer V1 (`MUSICIANS/G/Galway_Martin/MicroProse_Soccer_V1.sid`) | PSID, `playAddress = 0` | Converts (IRQ path) | PSID that still installs its own IRQs. Dump does not key off the magic string. |
| Hubbard Last V8 (`MUSICIANS/H/Hubbard_Rob/Last_V8.sid`) | RSID | Converts | Raster IRQ exits via `$EA7E`. |
| Tel Myth (`MUSICIANS/T/Tel_Jeroen/Myth.sid`) | RSID | Converts | Pages KERNAL out; music IRQ via the hardware vector. |
| lft A Mind Is Born (`MUSICIANS/L/Lft/A_Mind_Is_Born.sid`) | RSID | Skipped | Init-only demo: writes SID until finished. |

## Parsed but not converted (skip or error)

The header parser still reads these. The CLI / `run_sid` refuse or skip.

| Kind | How we know | What happens |
| ---- | ----------- | ------------ |
| Compute! MUS | flags bit 0 | Skip whole file: needs an external player merged in. |
| 2SID / 3SID | v3/v4 2nd/3rd SID page | Skip whole file. Addresses are parsed as metadata only. |
| Digi / speech, non-returning `init` | IRQ `init` exceeds cycle budget (often CIA timer B sample spin) | Skip that subtune (`DigiSkipError`). Arabian Nights is the usual example. |
| Init-only demos | `init` never returns. The program writes SID itself until it is finished; there is no `play` stub. | Skip: IRQ handler cycle cap (`CPUError`). lft *A Mind Is Born* is one example. |
| Digi / `$D418` volume samples | Probe dump is volume-register dominated and has no musical gates | Skip that subtune. Exploding Fist #1 is the usual example. |
| PlaySID-sample PSIDs | flags bit 1 on PSID, often sample playback | Header parses (speed bits wrap). Dump may run and then look like digi, or produce a bad module. Not a first-class path. |
| RSID BASIC flag | flags bit 1 on RSID, `initAddress` must be 0 | Header parses. Dump does **not** set `$030C` and has no BASIC ROM, so these are not actually executed. Unverified. |
| RSID that fails header rules | load/play/speed nonzero, load `< $07E8`, BASIC with nonzero init | `SidHeaderError` at parse. |

## Not emulated (will mis-dump or hang if the tune needs them)

Mark claims unverified unless a dump proved them.

- CIA 1 timer B and CIA 2 (including NMI from CIA 2).
- Full KERNAL / BASIC / chargen ROM. Only the IRQ stubs above.
- Cycle-exact SID reads (OSC3/ENV3, waveform readback). Generative 256-byte
  tunes that `ORA $D41C` for pitch entropy will not replay even if execution
  were forced.
- Badlines, sprites, VIC bank via `$DD00` except as ignored stores.
- 2SID/3SID register maps.

Writer mapping gaps (band-pass / notch, combined waves, hard sync) are in
[how-it-works.md](how-it-works.md).

A tune that `JMP $FFD2` (CHROUT), waits on `$DC01`, or needs NMI will not
get a real C64. If it still writes the SID register area from a VIC/CIA1 IRQ, the dump
may still be usable.

## How to classify a file quickly

1. Parse the header (`sidengine.header.parse_sid_file`). Check `magic`,
   `play_address`, `songs`, `flags.mus_player`, `second_sid_address`.
2. If MUS or 2SID/3SID: stop.
3. If `play_address != 0` and magic is PSID: caller-driven dump.
4. Else: IRQ dump. A long stall with almost no tick movement still means a
   missed ack or a missed vector (`$EA7E`, `$FFFE`, `$D019`).
5. Digi skip is per-subtune. Later subtunes of the same file can still be
   music.

## Related docs

- CLI and usage: [README](../README.md)
- Pipeline after the dump: [how-it-works.md](how-it-works.md)
