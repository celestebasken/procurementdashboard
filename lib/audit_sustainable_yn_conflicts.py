"""Read-only audit: finds within-campus merges that happened BEFORE
find_and_merge_within_campus() required matching sustainable_yn (see
CLAUDE.md / this session's work) where the merged-together raw names
actually had a genuine Y-vs-N sustainable_yn conflict.

merge_products() has always preferred a definitive 'Y'/'N' over 'NA' when
reconciling sustainable_yn across a merge, but a genuine 'Y'-vs-'N'
conflict falls back to keep_product_id's value with no trace of the
discarded side -- once merged, the products table only has room for one
value, so there's no way to tell from the live database alone whether a
given merged product's history had a real conflict. This re-derives each
raw name's own sustainable_yn straight from the untouched raw CSVs (same
"don't touch data/raw/, re-derive from it" principle used everywhere else
in this pipeline) and compares them.

Scope: only within-campus merges (a product with 2+ product_aliases rows
at the SAME campus) -- that's the only place raw names get reconciled down
to one sustainable_yn value. Cross-campus merges never reconcile multiple
same-campus raw names against each other, so they're not in scope here.

This is audit-only -- it does not modify the database. Writes findings to
data/processed/sustainable_yn_conflict_audit.csv for review.
"""

import csv
from pathlib import Path

import pandas as pd

from lib.db import get_connection
from lib.ingestion import CAMPUS_FILES, CAMPUS_LOADERS, DATA_RAW_DIR

AUDIT_CSV = Path(__file__).resolve().parent.parent / "data" / "processed" / "sustainable_yn_conflict_audit.csv"


def _campus_abbrev(conn, campus_name: str) -> str:
    row = conn.execute("SELECT abbreviation FROM campuses WHERE campus = ?", (campus_name,)).fetchone()
    return row[0]


def run() -> list[dict]:
    conn = get_connection()

    groups = conn.execute(
        "SELECT product_id, campus, COUNT(*) c FROM product_aliases GROUP BY product_id, campus HAVING c > 1"
    ).fetchall()
    print(f"{len(groups)} (product_id, campus) groups to check (each is a within-campus merge)")

    # Cache one loaded+aggregated-by-raw_name df per campus abbreviation,
    # so each raw CSV is only read once regardless of how many merged
    # products reference that campus.
    per_campus_sustainable_yn: dict[str, dict[str, str]] = {}

    def sustainable_yn_by_raw_name(abbrev: str) -> dict[str, str]:
        if abbrev not in per_campus_sustainable_yn:
            path = DATA_RAW_DIR / CAMPUS_FILES[abbrev]
            df = CAMPUS_LOADERS[abbrev](path)
            df = df[df["raw_name"].notna()]
            lookup = {}
            for raw_name, group in df.groupby("raw_name"):
                mode = group["sustainable_yn"].mode()
                lookup[raw_name] = mode.iloc[0] if not mode.empty else "NA"
            per_campus_sustainable_yn[abbrev] = lookup
        return per_campus_sustainable_yn[abbrev]

    findings = []
    for product_id, campus, _count in groups:
        raw_names = [
            r[0]
            for r in conn.execute(
                "SELECT raw_name FROM product_aliases WHERE product_id = ? AND campus = ?",
                (product_id, campus),
            ).fetchall()
        ]
        abbrev = _campus_abbrev(conn, campus)
        lookup = sustainable_yn_by_raw_name(abbrev)

        per_raw_name_values = {}
        for raw_name in raw_names:
            if raw_name in lookup:
                per_raw_name_values[raw_name] = lookup[raw_name]

        distinct_values = {v for v in per_raw_name_values.values() if v != "NA"}
        if len(distinct_values) > 1:
            current = conn.execute(
                "SELECT canonical_name, sustainable_yn FROM products WHERE product_id = ?", (product_id,)
            ).fetchone()
            findings.append(
                {
                    "product_id": product_id,
                    "campus": campus,
                    "current_canonical_name": current[0],
                    "current_sustainable_yn": current[1],
                    "raw_name_values": "; ".join(f"{rn} = {v}" for rn, v in per_raw_name_values.items()),
                }
            )

    print(f"{len(findings)} products have a genuine Y-vs-N conflict in their merge history")

    AUDIT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with open(AUDIT_CSV, "w", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["product_id", "campus", "current_canonical_name", "current_sustainable_yn", "raw_name_values"],
        )
        writer.writeheader()
        writer.writerows(findings)
    print(f"Wrote {AUDIT_CSV}")

    conn.close()
    return findings


if __name__ == "__main__":
    run()
