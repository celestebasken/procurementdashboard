"""Phase 6: Dining Dashboard (rebuild of legacy/dining_dashboard on the
canonical schema).

Cross-campus search over VALIDATED-sustainable products only
(`products.validated_sustainable_yn == 'Y'`) -- this tool's whole purpose
is helping a chef discover a sustainable item another campus already buys,
ideally through a distributor they already have a relationship with.
Deliberately price-free (CLAUDE.md): `purchases.total_price`/`unit_price`
are never read here.

SIMAP-57 (`products.simap_category`) is used as the browsing category
instead of the legacy tool's ad hoc, hand-maintained "Category" column --
that's the whole point of rebuilding on the canonical schema (the project
owner's explicit request: "I want to build a similar tool but with SIMAP
categories instead of the ones in the old code").

`product_aliases`/`purchases` (not a separate curated spreadsheet, like the
legacy tool's Google Sheet) are what make this cross-campus view possible
at near-zero extra ingestion cost -- see CLAUDE.md's note that
`product_aliases` is "probably the single highest-leverage table in the
project."
"""

from __future__ import annotations

import sqlite3

import pandas as pd


def load_sustainable_products(conn: sqlite3.Connection) -> pd.DataFrame:
    """One row per validated-sustainable canonical product, with every
    campus/distributor(vendor)/brand that has ever purchased it aggregated
    into list columns -- the cross-campus view this tool exists to provide.

    `simap_category` is filled to the literal string "(Unclassified)"
    rather than dropped -- CLAUDE.md's "surface, don't hide" principle for
    unclassified products applies here too; a sustainable product with no
    SIMAP category yet is still a real, discoverable product.
    """
    products = pd.read_sql_query(
        """
        SELECT product_id, canonical_name, simap_category, sustainability_certifications
        FROM products WHERE validated_sustainable_yn = 'Y'
        """,
        conn,
    )
    if products.empty:
        for col in ("campuses", "vendors", "brands", "cert_list"):
            products[col] = pd.Series([[] for _ in range(len(products))], dtype="object")
        return products

    purchases = pd.read_sql_query(
        """
        SELECT product_id, campus, vendor, brand
        FROM purchases
        WHERE product_id IN (SELECT product_id FROM products WHERE validated_sustainable_yn = 'Y')
        """,
        conn,
    )

    def _distinct_sorted(series: pd.Series) -> list:
        return sorted({v for v in series.dropna() if str(v).strip()})

    agg = (
        purchases.groupby("product_id")
        .agg(
            campuses=("campus", _distinct_sorted),
            vendors=("vendor", _distinct_sorted),
            brands=("brand", _distinct_sorted),
        )
        .reset_index()
    )

    df = products.merge(agg, on="product_id", how="left")
    for col in ("campuses", "vendors", "brands"):
        df[col] = df[col].apply(lambda x: x if isinstance(x, list) else [])

    df["simap_category"] = df["simap_category"].fillna("(Unclassified)")
    # Raw, campus-reported free text (CLAUDE.md: "as reported by campus") --
    # comma-split for filtering only, never re-canonicalized here. That
    # fuzzy-matching-against-certification_types work already happens once,
    # correctly, at ingestion (certification_validation_flag); redoing a
    # fuzzy join in the UI layer would just be a second, divergent copy of
    # that logic for no real benefit.
    df["cert_list"] = df["sustainability_certifications"].fillna("").apply(
        lambda s: [c.strip() for c in s.split(",") if c.strip()]
    )
    return df


def get_campus_vendors(conn: sqlite3.Connection, campus: str) -> set[str]:
    """Distinct distributors `campus` has purchased through, historically --
    used to highlight search results reachable through a vendor the campus
    already has a relationship with (CLAUDE.md's Dining Dashboard spec: "a
    query-time join against purchases.vendor, no schema change needed")."""
    rows = conn.execute(
        "SELECT DISTINCT vendor FROM purchases WHERE campus = ? AND vendor IS NOT NULL", (campus,)
    ).fetchall()
    return {r[0] for r in rows if r[0] and str(r[0]).strip()}


def load_certification_types(conn: sqlite3.Connection) -> pd.DataFrame:
    """The authoritative certification glossary (`certification_types`) --
    used for the glossary display, NOT joined row-by-row against the free-
    text `sustainability_certifications` column (see module docstring)."""
    return pd.read_sql_query(
        "SELECT certification_name, abbreviation, frameworks, qualifier FROM certification_types "
        "ORDER BY certification_name",
        conn,
    )
