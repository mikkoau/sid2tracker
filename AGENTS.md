# Agent guidance

Contributor rules for this clone. Product docs:

- `README.md`: install, CLI, and usage
- `docs/how-it-works.md`: conversion pipeline
- `docs/sid-support.md`: which files convert or skip

## How to contribute

- Prefer small, focused pull requests with a clear before/after description.
- Include a failing unit test when the change is behavioral, or explain why a
  synthetic fixture is enough.
- Do not commit `.sid` files, copyrighted `.it` outputs, or HVSC documents.
  Quote HVSC paths and tune names instead of attaching files.
- Run `python3 -m unittest discover -s tests` before opening a PR.
  Optional: listen in a tracker after dump or writer changes.
- If the change affects user-facing or agent docs, update them in the same
  work (README, `docs/`, `AGENTS.md`). Keep docs high-level: main concepts,
  background, and why the design is this way. Do not walk through the
  implementation in detail.
- Commit messages: say what changed and why, clearly but compact. Subject at
  most 50 characters; wrap body lines at 72.

## Technical constraints

- Stdlib only unless a PR explicitly justifies a dependency.
- Keep the converter portable. Avoid OS-specific paths, shells, or APIs.
- Prefer a dump fallback that always works over a clever player parser that
  works on three drivers.
- Do not reinvent SIDId. Family identification, if added, should wrap an
  existing signature database.
- Ignore digi / volume-sample playback until support is designed and
  documented in `docs/sid-support.md`.
- Mark unverified hardware or player claims. Cite the HVSC SID format doc, a
  dump observation, or a unit test.
- Do not hardcode machine-specific HVSC or OpenMPT paths into the library.

## Code layout

| Path | Role |
| ---- | ---- |
| `sidengine/header.py` | PSID/RSID header parse |
| `sidengine/songlengths.py` | HVSC Songlengths.md5 lookup |
| `sidengine/cpu6502.py` | NMOS 6510 interpreter |
| `sidengine/irq_harness.py` | VIC/CIA1 IRQ environment for RSID / play=0 |
| `sidengine/dump.py` | Execute player, record SID writes, lift to IR |
| `sidengine/ir.py` | Music IR schema and JSON load/save |
| `sid2it/sid_osc.py` | SID-ish oscillator samples for `.it` |
| `sid2it/write_it.py` | IR to Impulse Tracker `.it` format |
| `sid2it/cli.py` | End-to-end CLI (`python3 -m sid2it`) |

Keep shared dump/IR work in `sidengine/`. Format-specific writers and CLIs
live in packages like `sid2it/`.
