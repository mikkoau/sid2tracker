# SID2Tracker

Convert Commodore 64 SID tunes into editable tracker modules. The shipped
writer targets the Impulse Tracker `.it` file format (`python3 -m sid2it`).
Dump code lives in `sidengine` and produces a music intermediate
representation for the `.it` writer in `sid2it`.

A `.sid` file is not a modern song container full of notes. It is a short
header plus Commodore 64 machine code: a 6510 routine that drives the SID,
bundled with its song data. That routine is usually a custom player for the
tune or the composer's toolkit, not one shared player, and it is
essentially the same bytes that run on a real C64 and write the SID chip
registers directly. Overview of the file format:
[SID](https://www.vgmpf.com/Wiki/index.php?title=SID).

The converter loads that payload into a simplified C64 environment (NMOS
6510 interpreter plus enough VIC/CIA to drive `init`/`play` or IRQ tunes),
records writes into the SID register area (`$D400-$D418`) each player tick,
lifts those writes into a music intermediate representation (IR: a tick
timeline of notes, instruments, and filter), and writes a tracker module.

A tick is one dump step: usually one host `play` call, or one C64 video
frame of IRQ-driven writes. A video frame is one screen draw; multi-speed
tunes may run the player several times per frame, but IRQ dumps still bucket
those writes into one tick per frame.

Open the module in a tracker such as [OpenMPT](https://openmpt.org/)
(Windows; also Wine) or [Schism Tracker](https://schismtracker.org/)
(cross-platform). The point is editable patterns: notes, instruments, and
arrangement you can inspect and change. Players such as VLC can play `.it`
audio but do not show tracks or notes.

The conversion aims to map SID behaviour onto Impulse Tracker `.it` features
that fit (notes, envelopes, filter macros, arpeggio, and so on) while keeping
modules compact.

## Requirements

- Python 3.11 or newer (`python3` on macOS/Linux; some Windows installs
  expose the same interpreter as `python`)
- Standard library only
- SID files, for example from your own copy of
  [HVSC](https://www.hvsc.c64.org/)
- A tracker to open and edit the result, for example
  [OpenMPT](https://openmpt.org/) on Windows or
  [Schism Tracker](https://schismtracker.org/)

```bash
git clone https://github.com/mikkoau/sid2tracker.git
cd sid2tracker
python3 -m sid2it path/to/tune.sid --all
```

Run from the clone root so the packages resolve on `PYTHONPATH` (the current
directory is enough).

### Combined-wave tables (optional)

By default this converter mixes combined waveforms (triangle+pulse,
saw+triangle, and so on) with simple AND-logic on the ideal shapes. The real 6581/8580 sound,
as in SIDPlay, is richer than that.

[reSID](https://github.com/libsidplayfp/resid)'s OSC3 sample tables get
closer to the chip. They are
[GPL-2](https://www.gnu.org/licenses/old-licenses/gpl-2.0.html), so they
are not shipped here. This project stays
[MIT](https://choosealicense.com/licenses/mit/). Only the tables you generate
locally remain GPL-2 and stay on your machine.

```bash
git clone https://github.com/libsidplayfp/resid.git ../resid
python3 tools/extract_resid_wave_tables.py
```

Run from the sid2tracker clone root so `../resid` is the sibling tree.
Then convert as usual. Without the extract step, combined waves stay on
the simple default mix.

## Quick start

```bash
python3 -m sid2it tune.sid --all            # filter on, compact samples
python3 -m sid2it tune.sid --all --use-pwm  # bake pulse-width sweeps (files grow fast)
```

`--seconds 30` shortens a dump. Each subtune becomes its own `.it` (the
format has no multi-song container), written to `out/` unless you pass
`--out-dir`. `--verbose` (`-v`) adds a short note of which conversion
features were actually used.

## How it works

```
.sid
  -> parse PSID/RSID header
  -> run the player on the 6510 emulator and dump SID register writes per tick
  -> lift writes into music IR (intermediate representation)
  -> choose a tracker row grid from note spacing
  -> write Impulse Tracker .it format
```

Any player that writes the SID under the emulator harness can convert,
including custom and self-modifying drivers.

Longer walkthrough: [docs/how-it-works.md](docs/how-it-works.md).

## PSID and RSID

HVSC tags files `PSID` or `RSID`. Both still carry 6510 code. The difference
is the runtime contract: most PSIDs are caller-driven (`init`, then `play`
once per tick). RSID, and a PSID with `playAddress = 0`, run from their own
VIC/CIA IRQs. Which files convert or skip, and worked HVSC examples:
[docs/sid-support.md](docs/sid-support.md).

## Options

```bash
python3 -m sid2it --help
```

| Mode | Flags | What you get |
| ---- | ----- | ------------ |
| Default | none | Player row grid, static oscillator loops, SID filter as mid-row `Zxx`. |
| No filter | `--no-use-filter` | Skip filter automation; mute and master volume still apply. |
| PWM (experimental) | `--use-pwm` | Also bake moving pulse width into samples; files grow quickly. |

## Song length and subtunes

The SID header has no duration. Paths that look like HVSC (`C64Music`) use
that collection's song-length table. Otherwise the dump defaults to 60
seconds. Override with `--seconds` or `--ticks`. Early-ending dumps and
multi-speed players are noted so the tick count is not mistaken for wall
time.

Subtunes shorter than `--min-seconds` (default 10) are skipped on `--all`
and the default start song so game jingles and SFX are not written as
modules. `--subtune N` always converts that song. Pass `--min-seconds 0`
to keep short subtunes in a bulk run.
Without `--subtune` / `--all`, the header start song is used.

Output names include enough of the SID path to tell composer, tune, and
subtune apart. Multi-subtune SIDs also label the song inside the module.

## Limitations

The Impulse Tracker file format cannot express every SID effect the same way
the chip does. Filter-heavy and effect-heavy tunes often sound notably
different after conversion, but the note structure and SID arrangement
usually stay recognisable enough to read and edit.

Which files convert or skip (MUS, 2SID, digi, and so on) is in
[docs/sid-support.md](docs/sid-support.md). Mapping gaps in the `.it`
output (filter modes, vibrato, wide arpeggios, combined waves, hard sync)
are in [docs/how-it-works.md](docs/how-it-works.md).

Pull requests are welcome.

## Tests

```bash
python3 -m unittest discover -s tests
```

## License

[MIT](https://choosealicense.com/licenses/mit/). Optional generated OSC3
tables stay GPL-2 (reSID / Dag Lem) and local.
