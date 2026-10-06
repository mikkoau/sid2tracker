# How a .sid becomes a tracker module

End to end walkthrough of SID2Tracker conversion. CLI flags and defaults
are in the [README](../README.md). Support policy (which files are accepted
or skipped) is in [sid-support.md](sid-support.md).

`python3 -m sid2it` runs every step below and writes one `.it` per subtune.

## 1. Parse the SID header

`sidengine.header` reads the HVSC wrapper: load/init/play, PAL or NTSC,
subtunes, flags. Every multi-byte header field is big-endian.

Which files take the caller-driven `play` path, which take the IRQ harness,
and which are skipped (MUS, 2SID, digi, BASIC RSID) is documented in
[sid-support.md](sid-support.md). Do not guess from the magic string alone:
RSID always has `playAddress = 0`, but some PSIDs do too.

## 2. Decide duration and which subtunes to convert

The header says nothing about duration, so the dump needs a tick count.
Which subtunes convert, HVSC song-length lookup, and the `--seconds` /
`--min-seconds` flags are documented in the README.

## 3. Execute the player and record SID writes

`sidengine.dump` strips the HVSC header and loads only the C64 payload
(the 6510 player and song data) into a flat 64K RAM image. For
caller-driven PSID it runs `init` once with the subtune number in the
accumulator, then calls `play` once per dump tick on a small NMOS 6510
interpreter. For RSID / `play=0` it powers on a C64-like IRQ environment,
runs `init` with IRQs live, then parks the CPU in a small idle loop while
VIC or CIA IRQs drive the player. Changing SID register writes are
recorded. IRQ-path writes are bucketed by video frame so multi-speed IRQs
still land in one dump tick. Hardware details for that harness are in
[sid-support.md](sid-support.md).

The result is a tick timeline: for each tick, the list of registers the
player touched. Conversion is player-independent: any custom or
self-modifying player that writes the SID under this harness can convert,
without a signature database recognising the driver family.

## 4. Lift register writes into music IR

The music intermediate representation (`sidengine.ir`) is the hand-off
between observation and the tracker writer: a tick timeline of note events
per voice, a list of instruments, and a chip-global filter timeline. The IR
does not know about the Impulse Tracker `.it` format; step 6 is the `.it`
writer.

Finding note onsets is the subtle part, because a gate edge alone misses
real tunes. Four idioms count as a note:

- a normal gate rise
- a voice that `init` itself gated on
- pitch articulation (the player gates once and phrases notes by setting a
  frequency and zeroing it again; only a crossing through frequency 0
  counts, so vibrato and portamento stay inside one note)
- a named-waveform change under a held gate (for example one frame of noise
  then pulse). Those follow-on onsets are tagged so they do not collapse
  the row grid.

Pitch that moves while the gate stays on is buffered and classified in
local output-row windows:

- a cycle through two or three pitches becomes an arpeggio event
- a fast wobble inside a semitone or two is vibrato and stays one note
- a changed row base becomes a legato pitch event (no envelope restart)

Octave-stacked cycles wider than a three-step arpeggio can encode use the
lowest pitch in a short local window, so long gated voices keep a musical
root instead of emitting thousands of retriggered notes.

Instruments are deduplicated by waveform bits, ADSR, filter routing, and a
coarsely quantised pulse width. Without that last part a pulse sweep mints
a new instrument every tick and long tunes blow past tracker instrument
limits.

Three things no simple tracker effect can express are recorded on the note
that starts them rather than folded into the instrument: the frequency
ratio against the modulating voice when sync or ring is on, and the full
pulse-width timeline while the gate is held. The `.it` writer decides what
to do with them.

## 5. Choose the row grid

Tracker rows are the quantisation grid: how many SID dump ticks collapse
into one row. The grid is estimated from the *spacing* between note onsets:

- prefer the longest small even step that still covers most of those gaps
- else the dominant longer gap (with a little CIA slack)
- else one tick per row

Spacing matters rather than absolute tick position. A tune whose first note
does not land on tick 0 is only phase shifted; flooring every onset would
move the whole tune by a constant. Aligning to tick 0 instead can collapse
off-grid phrasing to one row per tick. Occasional ornaments should not
override the grid. Mixed gaps may land on one tick per row, a long rest as
the row (with a warning), or a CIA-ish step that is not a clean even
number of ticks.

Tunes that route a voice through the filter still keep the player's own
row. Filter automation is written on that grid by default, sampling the
timeline mid-row so a one-tick-late cutoff open is not lost. Pass
`--no-use-filter` to skip it. `--use-pwm` only adds baked samples, not a
denser row clock. The lift is rerun after choosing the grid so pitch
classification uses the same row duration.

CIA players often use a long row (around 11 frames) while double-length
holds land a frame short as often as they land exactly on two rows.
Snapping every note to a fixed lattice then halves some of those holds,
which is clearly audible. Notes are placed from the gaps between them
instead, so a near-double still occupies two rows. Note ends still round
up so a short note cannot share a cell with its own note-off.

If the dump still cannot fit the `.it` pattern budget, the writer
coarsens the grid, warns, and may suggest a shorter `--seconds` so the
player's own row still fits.

## 6. Write the `.it` module

`sid2it.write_it` turns the IR into an `.it` binary. Every IT
instrument owns one sample, so a note needing different sample data gets a
whole instrument of its own.

- **Samples.** A reSID-shaped oscillator draws triangle, saw and pulse as
  ideal 12-bit DAC values in a 256-sample loop, mean-subtracted to model
  the C64 output coupling, so a narrow pulse stays naturally quieter than
  a square. Noise is a 4096-sample run of the SID's 23-bit LFSR. Combined
  waveforms use a simple AND-logic mix.
- **Modulation.** Sync and ring at a steady ratio still loop, just over as
  many carrier cycles as the ratio needs. When a pulse duty is not baked
  (see below), the static loop uses the duty that carries the note's mean
  energy instead of the hard-restart width at gate-on, which is often near
  silent.
- **Envelopes.** The SID ADSR becomes an IT volume envelope whose decay and
  release follow the chip's exponential breakpoints rather than a straight
  line, with the sustain loop on the node at the sustain level. Sustain 0
  decays to silence even while the gate stays high.
- **Patterns.** Notes, instrument numbers, velocity 64, and by default
  `Zxx` cutoff and resonance on filtered channels, plus `Z90` / `Z91` for
  low-pass versus high-pass. `--no-use-filter` skips that automation so a
  dry voice stays dry. Cutoff follows the measured curve for the header's
  chip model (the same register is about 420 Hz on a 6581 and 3.3 kHz on
  an 8580). Master volume becomes `Vxx`. Arpeggio events become `Jxy`;
  legato pitch becomes `GFF`. The last row carries `B00` so the module
  loops like the C64 player.
- **Optional PWM bake (`--use-pwm`, experimental).** A note whose pulse
  width actually sweeps can be rendered into a private 22050 Hz sample.
  Identical sweeps share one sample; long notes fall back to a static loop;
  `--sample-budget-mb` (default 4) caps baking. This can fatten files
  quickly and often hits the budget on PWM-heavy tunes, so it is off by
  default. Trade-offs versus compact static pulses are still unclear.
  Hitting the budget or the tracker instrument limit is reported.

An `.it` holds one song, so subtunes become separate files. Names keep
enough of the SID path to tell composer, tune, and subtune apart.
Multi-subtune SIDs also label the song inside the module.

## 7. Verify

```
python3 -m unittest discover -s tests
```

Then open the module in a tracker such as OpenMPT (Windows) or Schism
Tracker. In OpenMPT, play along the order list (default shortcut `F5`) or
play only the current pattern (`F6`), which is usually a few seconds.
Audio-only players such as VLC can play `.it` but do not show tracks or
notes.

## Known gaps

The dump path favours a harness that usually works over cycle-exact
emulation. File-format skips (MUS, 2SID, digi, and so on) are in
[sid-support.md](sid-support.md). Gaps in the `.it` output:

- **Band-pass / notch filters.** OpenMPT's `.it` macros expose low-pass or
  high-pass only, so SID band-pass and notch are not mapped as their own
  modes.
- **Vibrato.** Fast pitch wobble stays a held pitch in the IR rather than
  an IT `Hxy` vibrato effect.
- **Wide arpeggios.** Cycles wider than the 15 semitones `Jxy` can carry
  collapse toward a local root pitch.
- **Combined waveforms.** Default is a simple AND-logic mix.
- **Section repeats.** Only byte-identical packed 64-row patterns share an
  order-list entry. A trailing copy of the opening notes from an HVSC
  length overrun past a return-to-start loop is trimmed before `B00`.
- **Hard sync timing.** The oscillator advances once per output sample,
  not once per chip clock, so sync lands on a sample boundary. Short loops
  are oversampled 4x to soften that; baked PWM notes are not.
