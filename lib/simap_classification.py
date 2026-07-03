"""Phase 3: SIMAP-57 classification.

Assigns each product a SIMAP-57 food category (`products.simap_category`)
for GHG-equivalent reporting. **Never used to define "sustainable"** --
that's `products.validated_sustainable_yn`, per CLAUDE.md's repeated
warning that SIMAP membership plays no role in the sustainability
determination.

Three-tier confidence, tracked via `products.simap_classification_source`:

- 'campus_category': several campus exports already include a category-ish
  column of their own (`env_sub_category` for UCD_H/UCLA_H/UCSD_H,
  `Product Category`/`Product Subcategory` for UCD, `Food Type (WRI)`/
  `(RFC)` for UCSC) whose value matches
  `reference/simap_keyword_dictionary.csv` directly -- highest confidence,
  since it's the campus's own classification, not a guess from a product
  name. UCB has no such column at all; UCR's only candidate (`Cost
  Category`) was deliberately excluded from the dictionary -- it's too
  coarse to trust (e.g. "Meats" spans beef/chicken/pork, which have very
  different GHG factors). Both skip straight to keyword_match.
- 'keyword_match': no campus category signal, or it didn't hit the
  dictionary -- fall back to matching dictionary keywords against the
  product's own `canonical_name` text. Longest keyword wins on multiple
  matches (more specific phrases beat generic single words).
- 'unclassified': nothing matched either tier. Left as `NULL`, not guessed
  -- an honest gap, same philosophy as `weight_source = 'unresolved'`.

Re-reads the raw campus CSVs directly (rather than extending the Phase 1
ingestion schema) since the category-ish columns here are only useful for
classification, not a purchase-transaction attribute like vendor/brand.
Products are matched back to raw category values via `product_aliases.raw_name`,
using the exact same raw_name column each campus's ingestion loader uses
(see lib/ingestion.py), so the join lines up column-for-column.
"""

import re
import sqlite3
from pathlib import Path

import pandas as pd

DATA_RAW_DIR = Path(__file__).resolve().parent.parent / "data" / "raw"
DICTIONARY_PATH = Path(__file__).resolve().parent.parent / "reference" / "simap_keyword_dictionary.csv"

CAMPUS_FILES = {
    "UCB": "UCB_FY25.csv",
    "UCD": "UCD_FY25.csv",
    "UCD_H": "UCD_H_FY25.csv",
    "UCLA_H": "UCLA_H_FY25.csv",
    "UCR": "UCR_FY25.csv",
    "UCSC": "UCSC_FY25.csv",
    "UCSD_H": "UCSD_H_FY25.csv",
}

# (raw_name column, [category columns in priority order, finer first], read_kwargs)
# None category-column list means the campus has no usable category signal
# at all -- keyword_match is the only tier available.
CAMPUS_CONFIG = {
    "UCB": ("Product Name or Description ", None, {}),
    "UCD": ("Name", ["Product Subcategory", "Product Category"], {}),
    "UCD_H": ("Name", ["env_sub_category"], {}),
    "UCLA_H": ("notes", ["env_sub_category"], {}),
    "UCR": ("Name", None, {}),
    "UCSC": ("Product Description", ["Food Type (WRI)", "Food Type (RFC)"], {"skiprows": 1, "low_memory": False}),
    "UCSD_H": ("ProductName", ["env_sub_category"], {}),
}


def load_dictionary(path: Path = DICTIONARY_PATH) -> dict[str, str]:
    df = pd.read_csv(path)
    return dict(zip(df["keyword"], df["simap_category"]))


def _clean(v) -> str | None:
    if pd.isna(v):
        return None
    v = str(v).strip()
    return v or None


def build_campus_category_lookup(campus_abbrev: str, data_dir: Path = DATA_RAW_DIR) -> dict[str, str]:
    """Returns {raw_name: category_field_value} for one campus, picking the
    first non-blank value across the campus's category columns (finer
    columns first) when a raw_name appears multiple times."""
    raw_name_col, category_cols, read_kwargs = CAMPUS_CONFIG[campus_abbrev]
    if category_cols is None:
        return {}

    path = data_dir / CAMPUS_FILES[campus_abbrev]
    df = pd.read_csv(path, **read_kwargs)

    lookup: dict[str, str] = {}
    for _, row in df.iterrows():
        raw_name = _clean(row[raw_name_col])
        if raw_name is None or raw_name in lookup:
            continue
        for col in category_cols:
            val = _clean(row[col])
            if val is not None:
                lookup[raw_name] = val
                break
    return lookup


_WORD_RE_CACHE: dict[str, re.Pattern] = {}


def _keyword_pattern(keyword: str) -> re.Pattern:
    if keyword not in _WORD_RE_CACHE:
        # Trailing "s" optional so a singular dictionary entry ("Grape")
        # also matches the plural product name ("GRAPES") -- auditing real
        # output found ~660 otherwise-unclassified products this recovers
        # (chips, cookies, muffins, waffles, seeds, ...). Only covers the
        # simple English plural, not irregular forms (berry/berries) -- an
        # honest partial improvement, not a full stemmer.
        _WORD_RE_CACHE[keyword] = re.compile(r"\b" + re.escape(keyword) + r"s?\b", re.IGNORECASE)
    return _WORD_RE_CACHE[keyword]


def keyword_match(name: str, dictionary: dict[str, str]) -> str | None:
    """Longest matching dictionary keyword wins (more specific phrases like
    'Baked Goods' beat generic single words when both appear). Ties on
    length are broken by earliest position in the text -- e.g. "CHICKEN
    BREAST BUFFALO BITES" has "Chicken" and "Buffalo" both length 7, and the
    first-mentioned ingredient is usually the primary one (Poultry, not the
    "Buffalo" sauce/flavor descriptor) -- found via real-data audit, not
    just this one example: it's a general product-naming pattern (lead
    noun first, flavor/style descriptors after)."""
    best_keyword = None
    best_position = None
    for keyword in dictionary:
        match = _keyword_pattern(keyword).search(name)
        if not match:
            continue
        if (
            best_keyword is None
            or len(keyword) > len(best_keyword)
            or (len(keyword) == len(best_keyword) and match.start() < best_position)
        ):
            best_keyword = keyword
            best_position = match.start()
    return dictionary[best_keyword] if best_keyword else None


def classify_all(conn: sqlite3.Connection, data_dir: Path = DATA_RAW_DIR) -> dict:
    dictionary = load_dictionary()
    campus_lookups = {abbrev: build_campus_category_lookup(abbrev, data_dir) for abbrev in CAMPUS_FILES}
    abbrev_by_campus = dict(conn.execute("SELECT campus, abbreviation FROM campuses").fetchall())

    products = pd.read_sql_query(
        "SELECT p.product_id, p.canonical_name, pa.campus, pa.raw_name FROM products p "
        "JOIN product_aliases pa ON pa.product_id = p.product_id",
        conn,
    )

    counts = {"campus_category": 0, "keyword_match": 0, "unclassified": 0}
    updates = []
    for product_id, group in products.groupby("product_id"):
        category = None
        source = None

        for _, alias in group.iterrows():
            abbrev = abbrev_by_campus.get(alias["campus"])
            campus_val = campus_lookups.get(abbrev, {}).get(alias["raw_name"])
            if campus_val and campus_val in dictionary:
                category = dictionary[campus_val]
                source = "campus_category"
                break

        if category is None:
            category = keyword_match(group["canonical_name"].iloc[0], dictionary)
            if category is not None:
                source = "keyword_match"

        if category is None:
            source = "unclassified"

        counts[source] += 1
        updates.append((category, source, product_id))

    conn.executemany(
        "UPDATE products SET simap_category = ?, simap_classification_source = ? WHERE product_id = ?",
        updates,
    )
    conn.commit()
    return counts


if __name__ == "__main__":
    from lib.db import get_connection, migrate_schema

    conn = get_connection()
    migrate_schema(conn)
    counts = classify_all(conn)
    total = sum(counts.values())
    for source, n in counts.items():
        print(f"{source}: {n} ({n / total:.1%})")
    conn.close()
