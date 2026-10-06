"""Extract reSID OSC3 combined-wave tables into sid2it/resid_wave_tables.py.

Local add-on only (gitignored, GPL-2). Default tree is a sibling `resid/`
clone. Pass another path if needed.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "sid2it" / "resid_wave_tables.py"
DEFAULT_RESID = REPO.parent / "resid"

FILES = (
    ("wave6581__ST", "MOS6581", "ST"),
    ("wave6581_P_T", "MOS6581", "PT"),
    ("wave6581_PS_", "MOS6581", "PS"),
    ("wave6581_PST", "MOS6581", "PST"),
    ("wave8580__ST", "MOS8580", "ST"),
    ("wave8580_P_T", "MOS8580", "PT"),
    ("wave8580_PS_", "MOS8580", "PS"),
    ("wave8580_PST", "MOS8580", "PST"),
)


def usage_hint(resid: Path) -> str:
    return (
        f"Need a reSID source tree with wave6581_*.dat (or .cc) files "
        f"(tried {resid}).\n"
        "From the sid2tracker clone root:\n"
        "  git clone https://github.com/libsidplayfp/resid.git ../resid\n"
        "  python tools/extract_resid_wave_tables.py\n"
        "Or pass the tree: python tools/extract_resid_wave_tables.py /path/to/resid"
    )


def table_path(resid: Path, stem: str) -> Path | None:
    """libsidplayfp ships .dat; older trees ship samp2src .cc arrays."""
    for suffix in (".dat", ".cc"):
        path = resid / f"{stem}{suffix}"
        if path.is_file():
            return path
    return None


def load_table(path: Path) -> bytes:
    if path.suffix.lower() == ".dat":
        data = path.read_bytes()
        if len(data) != 4096:
            raise SystemExit(
                f"{path}: expected 4096 table bytes, got {len(data)}\n"
                f"{usage_hint(path.parent)}"
            )
        return data
    try:
        text = path.read_text(encoding="utf-8")
        body = text[text.index("[]") :]
        body = body[body.index("{") + 1 : body.rindex("}")]
    except (OSError, ValueError) as exc:
        raise SystemExit(
            f"Could not parse {path}: {exc}\n{usage_hint(path.parent)}"
        ) from exc
    values = bytes(
        int(match.group(1), 16)
        for match in re.finditer(r"0x([0-9a-fA-F]{2}),", body)
    )
    if len(values) != 4096:
        raise SystemExit(
            f"{path}: expected 4096 table bytes, got {len(values)}\n"
            f"{usage_hint(path.parent)}"
        )
    return values


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "resid",
        nargs="?",
        type=Path,
        default=DEFAULT_RESID,
        help="reSID source tree (default: ../resid next to this clone)",
    )
    args = parser.parse_args()
    resid: Path = args.resid
    if not resid.is_dir():
        raise SystemExit(f"not a directory: {resid}\n{usage_hint(resid)}")
    missing = [stem for stem, _model, _kind in FILES if table_path(resid, stem) is None]
    if missing:
        names = ", ".join(missing[:3])
        extra = "..." if len(missing) > 3 else ""
        raise SystemExit(f"missing {names}{extra} in {resid}\n{usage_hint(resid)}")

    chunks: list[str] = []
    chunks.append('"""reSID-derived combined-waveform OSC3 lookup tables.')
    chunks.append("")
    chunks.append("Generated locally. Do not commit this file.")
    chunks.append("Byte values come from reSID wave6581_* / wave8580_* tables")
    chunks.append("(Dag Lem). Those sources are GPL-2.0-or-later; this data")
    chunks.append("module is therefore GPL-2.0-or-later.")
    chunks.append("")
    chunks.append("Copyright (C) 2004 Dag Lem")
    chunks.append("Extraction into this file: SID2Tracker contributors")
    chunks.append('"""')
    chunks.append("")
    chunks.append("from __future__ import annotations")
    chunks.append("")
    chunks.append("# TABLES[(model, kind)] -> 4096 OSC3 sample bytes.")
    chunks.append("# kind: ST=saw+tri, PT=pulse+tri, PS=pulse+saw, PST=all three.")
    chunks.append("TABLES: dict[tuple[str, str], bytes] = {")

    for stem, model, kind in FILES:
        path = table_path(resid, stem)
        assert path is not None
        data = load_table(path)
        chunks.append(f"    ({model!r}, {kind!r}): bytes((")
        for index in range(0, 4096, 32):
            piece = ", ".join(str(value) for value in data[index : index + 32])
            chunks.append(f"        {piece},")
        chunks.append("    )),")

    chunks.append("}")
    chunks.append("")
    chunks.append("")
    chunks.append("def table_for(model: str, kind: str) -> bytes:")
    chunks.append('    """4096-byte OSC3 table for model; 6581 unless 8580."""')
    chunks.append(
        '    chip = "MOS8580" if str(model).startswith("MOS8580") else "MOS6581"'
    )
    chunks.append("    return TABLES[(chip, kind)]")
    chunks.append("")

    OUT.write_text("\n".join(chunks) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes)", file=sys.stderr)
    print(
        "Local add-on only (gitignored, GPL-2). Do not commit this file.",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
