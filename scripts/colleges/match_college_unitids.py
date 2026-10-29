"""Suggest a federal IPEDS UNITID for each college by matching the College Scorecard file.

Exact name + state matches (same rule as the import's ``normalize_name``, one
candidate, public/private agreeing) are written to ``unitid-auto.csv``. Everything
else goes to ``unitid-review.csv`` with up to three candidates for a person to
confirm; nothing here writes to the database. Reviewed rows are copied into
``scripts/colleges/college_unitids.csv``, which the workbook converter reads.

The Scorecard file is untrusted input: run with ``python -I`` and pass its path.

    uv run python -I scripts/colleges/match_college_unitids.py \
        --scorecard Most-Recent-Cohorts-Institution.csv --colleges college-aid-2026-09.csv --out OUT_DIR
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path

# -I drops cwd and the script dir from sys.path; locate the repo from this file, never cwd.
REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from src.colleges.name_key import normalize_name

SCORECARD_COLUMNS = ("UNITID", "INSTNM", "CITY", "STABBR", "CONTROL", "ALIAS")
SCORECARD_CONTROL = {"1": "public", "2": "private", "3": "private"}  # 3 = for-profit
_ALIAS_SPLIT = re.compile(r"\s*[|,;]\s*|\s{2,}")
_LOOSE_DROP = re.compile(r"^the |[ ]+main campus$")
MIN_SCORE = 0.6


def loose(name: str) -> str:
    """Looser than normalize_name: punctuation becomes a space; drops a leading 'The' and '-Main Campus'."""
    text = re.sub(r"[^a-z0-9 ]", " ", name.lower().replace("&", " and "))
    return _LOOSE_DROP.sub("", re.sub(r"\s+", " ", text).strip()).strip()


def sheet_safe(text: str) -> str:
    """Scorecard text lands in a CSV people open in a spreadsheet: neutralise formula prefixes."""
    return f"'{text}" if text[:1] in ("=", "+", "-", "@") else text


def read_scorecard(path: Path) -> dict[str, list[dict]]:
    """State -> institutions, keeping only the columns needed."""
    csv.field_size_limit(10_000_000)
    by_state: dict[str, list[dict]] = defaultdict(list)
    with path.open(newline="", encoding="utf-8-sig") as fh:
        for rec in csv.DictReader(fh):
            inst = {k: (rec.get(k) or "").strip() for k in SCORECARD_COLUMNS}
            if not re.fullmatch(r"\d{6}", inst["UNITID"]) or not inst["INSTNM"]:
                continue  # 8-digit ids are Scorecard-only branch rows, not IPEDS UNITIDs
            aliases = [a for a in _ALIAS_SPLIT.split(inst["ALIAS"]) if a and a.upper() not in ("NULL", "NAN")]
            inst |= {"key": normalize_name(inst["INSTNM"]), "loose": loose(inst["INSTNM"]),
                     "aliases": {loose(a) for a in aliases}}
            by_state[inst["STABBR"]].append(inst)
    return by_state


def read_colleges(path: Path) -> list[dict]:
    """Unique (name, state, control) from any CSV with those columns (converter output or admin export)."""
    seen: dict[tuple[str, str], dict] = {}
    with path.open(newline="", encoding="utf-8-sig") as fh:
        for rec in csv.DictReader(fh):
            name, state = rec["name"].strip(), rec["state"].strip().upper()
            seen.setdefault((normalize_name(name), state), {"name": name, "state": state,
                                                            "control": (rec.get("control") or "").strip()})
    return sorted(seen.values(), key=lambda c: (c["state"], c["name"]))


def _score(college: dict, inst: dict) -> float:
    target = loose(college["name"])
    ratios = [SequenceMatcher(None, target, inst["loose"]).ratio()]
    ratios += [SequenceMatcher(None, target, a).ratio() for a in inst["aliases"]]
    return max(ratios)


def match(college: dict, insts: list[dict]) -> tuple[str, list[tuple[float, dict]]]:
    """(method, ranked candidates). Methods: exact, likely, alias, fuzzy, none."""
    key, target = normalize_name(college["name"]), loose(college["name"])
    for method, hits in (
        ("exact", [i for i in insts if i["key"] == key]),
        ("likely", [i for i in insts if i["loose"] == target]),
        ("alias", [i for i in insts if target in i["aliases"]]),
    ):
        if hits:
            return method, [(1.0, i) for i in hits]
    ranked = sorted(((_score(college, i), i) for i in insts), key=lambda p: -p[0])[:3]
    ranked = [(s, i) for s, i in ranked if s >= MIN_SCORE]
    return ("fuzzy" if ranked else "none"), ranked


def main() -> int:
    ap = argparse.ArgumentParser(description="Suggest IPEDS UNITIDs from the College Scorecard file")
    ap.add_argument("--scorecard", required=True, type=Path, help="Most-Recent-Cohorts-Institution*.csv")
    ap.add_argument("--colleges", required=True, type=Path, help="CSV with name, state, control columns")
    ap.add_argument("--out", required=True, type=Path, help="output directory (outside the repo)")
    args = ap.parse_args()
    try:
        by_state, colleges = read_scorecard(args.scorecard), read_colleges(args.colleges)
    except (OSError, KeyError, AttributeError, csv.Error) as exc:  # AttributeError: short row
        print(f"error: {exc}", file=sys.stderr)
        return 1
    auto, review = [], []
    for college in colleges:
        method, ranked = match(college, by_state.get(college["state"], []))
        top = ranked[0][1] if ranked else None
        control_ok = top is not None and SCORECARD_CONTROL.get(top["CONTROL"]) == college["control"]
        if method == "exact" and len(ranked) == 1 and control_ok:
            auto.append({"name": college["name"], "state": college["state"], "unitid": top["UNITID"]})
            continue
        review.append({
            "name": college["name"], "state": college["state"], "control": college["control"], "method": method,
            "suggested_unitid": top["UNITID"] if top else "", "suggested_name": sheet_safe(top["INSTNM"]) if top else "",
            "suggested_city": sheet_safe(top["CITY"]) if top else "", "score": f"{ranked[0][0]:.2f}" if ranked else "",
            "control_matches": "yes" if control_ok else "no",
            "other_candidates": sheet_safe(" | ".join(f"{i['UNITID']} {i['INSTNM']} ({i['CITY']})" for _, i in ranked[1:])),
        })
    args.out.mkdir(parents=True, exist_ok=True)
    for name, rows in (("unitid-auto.csv", auto), ("unitid-review.csv", review)):
        with (args.out / name).open("w", newline="", encoding="utf-8") as fh:
            fields = list(rows[0]) if rows else ["name", "state", "unitid"]
            writer = csv.DictWriter(fh, fieldnames=fields, lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
    print(f"{len(colleges)} colleges: {len(auto)} exact, {len(review)} to review -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
