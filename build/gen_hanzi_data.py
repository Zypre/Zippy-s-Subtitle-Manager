#!/usr/bin/env python3
"""Regenerate dualsubs/_hanzi_data.py from the OpenCC dictionaries.

    pip download opencc-python-reimplemented -d /tmp/occ --no-deps
    unzip -q /tmp/occ/*.whl -d /tmp/occ/x
    py -3 build/gen_hanzi_data.py --dict /tmp/occ/x/opencc/dictionary

Only needed when you want a newer OpenCC dataset; the generated file is
checked in so the program has no runtime dependency. OpenCC's dictionaries are
Apache 2.0 -- keep the attribution in the generated header.
"""
from __future__ import annotations

import argparse
from pathlib import Path

def find_project_root() -> Path:
    """Locate the folder holding dual_subs.py and the dualsubs package.

    Works whether this script sits in build\\ or has been moved up beside the
    project, and whatever the working directory happens to be. Assuming a fixed
    depth was a bug: it climbed out of the project entirely when the layout was
    flattened.
    """
    here = Path(__file__).resolve().parent
    for base in (here, here.parent, Path.cwd(), *Path.cwd().parents):
        if (base / "dual_subs.py").is_file() and (base / "dualsubs").is_dir():
            return base
    raise SystemExit(
        "Could not find the project. Expected dual_subs.py and the dualsubs\\ "
        f"folder in or above:\n  {here}\n  {Path.cwd()}\n"
        "Run this from inside the extracted dual_subs folder."
    )


OUT = find_project_root() / "dualsubs" / "_hanzi_data.py"


def read(d: Path, name: str) -> dict[str, str]:
    out: dict[str, str] = {}
    path = d / name
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        src, dst = parts[0], parts[1].split(" ")[0]   # first value = preferred
        if src and dst and src != dst:
            out[src] = dst
    return out


def split_one_to_one(d: dict[str, str]) -> tuple[dict, dict]:
    one, many = {}, {}
    for k, v in d.items():
        (one if len(k) == 1 and len(v) == 1 else many)[k] = v
    return one, many


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dict", type=Path, required=True,
                    help="OpenCC dictionary folder")
    args = ap.parse_args()
    d = args.dict

    var_ph = {**read(d, "TWVariantsRevPhrases.txt"),
              **read(d, "HKVariantsRevPhrases.txt")}
    var_ch = {**read(d, "TWVariantsRev.txt"), **read(d, "HKVariantsRev.txt")}
    ts_ph = read(d, "TSPhrases.txt")
    ts_ch = read(d, "TSCharacters.txt")

    var_one, var_many = split_one_to_one(var_ch)
    ts_one, ts_many = split_one_to_one(ts_ch)
    var_ph.update(var_many)
    ts_ph.update(ts_many)

    def pairs(one):
        keys = sorted(one)
        return "".join(keys), "".join(one[k] for k in keys)

    vT, vS = pairs(var_one)
    tT, tS = pairs(ts_one)
    trad_only = "".join(sorted(set(tT)))
    simp_only = "".join(sorted({v for v in ts_one.values() if v not in ts_one}))

    def dump(x):
        return "{\n" + "".join(f"    {k!r}: {v!r},\n" for k, v in sorted(x.items())) + "}"

    OUT.write_text(f'''"""Traditional -> Simplified conversion tables. GENERATED - do not hand-edit.

Derived from the OpenCC dictionaries (https://github.com/BYVoid/OpenCC),
Apache License 2.0, via build/gen_hanzi_data.py. Two stages, matching OpenCC's
`tw2s`/`hk2s` chain: regional variants are normalised first, then the
Traditional -> Simplified mapping proper. Phrases win over single characters,
which is what keeps 一目瞭然 -> 一目了然 rather than the wrong 瞭 -> 了.
"""

# stage 1: Taiwan / Hong Kong variant forms -> mainland-standard traditional
VARIANT_PHRASES = {dump(var_ph)}

VARIANT_FROM = {vT!r}
VARIANT_TO = {vS!r}

# stage 2: traditional -> simplified
TS_PHRASES = {dump(ts_ph)}

TS_FROM = {tT!r}
TS_TO = {tS!r}

# characters that appear only on the traditional side, for script detection
TRADITIONAL_ONLY = {trad_only!r}
SIMPLIFIED_ONLY = {simp_only!r}
''', encoding="utf-8")
    print(f"{OUT}: {len(tT)} characters, {len(ts_ph)} phrases, "
          f"{OUT.stat().st_size // 1024} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
