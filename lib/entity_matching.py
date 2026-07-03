"""Phase 2: entity resolution, within-campus and cross-campus.

Two matching passes, sharing the same merge mechanics (merge_products())
and confidence tiers:

- >= AUTO_MERGE_THRESHOLD: merged immediately.
- >= REVIEW_THRESHOLD (but below auto): logged to
  product_match_candidates as 'pending' for a human to approve/reject in
  the review queue UI. Nothing in products/purchases/product_aliases
  changes until approved.
- Below REVIEW_THRESHOLD: not recorded at all.

find_and_merge_within_campus() matches near-duplicate raw item names
within the same campus AND vendor (never across vendors -- confirmed with
project owner: different distributors selling the same underlying product
stay separate for now, since the distributor view is load-bearing for
other parts of the dashboard). Purchases get summed across a merge since
duplicate rows here are the same campus's own purchase orders.

find_and_merge_cross_campus() matches the same underlying product bought
by DIFFERENT campuses, blocked by SIMAP category instead of vendor
(distributor overlap across campuses is too sparse to gate on). Purchases
are never summed across a cross-campus merge -- campus differs by
definition, so merge_products() just repoints product_id without any
summing collision. Requires Phase 3 (SIMAP classification) to have run
first; unclassified products are skipped.
"""

import re
import sqlite3

import pandas as pd
from rapidfuzz import fuzz, process
from rapidfuzz import utils as rf_utils

# Auditing real output surfaced two classes of false positive in the 97-98
# band: quantity mismatches (25lb vs 5lb, guarded separately by
# _numbers_match) and word-substitution mismatches (salted vs unsalted,
# peach vs pear) that score identically to genuinely-correct matches (e.g.
# "DI NAPOLI" vs "DINAPOLI" also scores ~97.7) -- no score in that band
# reliably separates the two. Confirmed with project owner: restrict
# auto-merge to near-perfect scores (true formatting noise -- case,
# whitespace, hyphen-spacing -- which scores at or near 100) and push
# everything else to human review instead.
AUTO_MERGE_THRESHOLD = 99.5
REVIEW_THRESHOLD = 90

_CORPORATE_SUFFIXES = re.compile(
    r"\b(corporation|corp|incorporated|inc|llc|l\.l\.c|lp|l\.p|co|company)\.?\s*$",
    re.IGNORECASE,
)


def normalize_vendor(vendor) -> str | None:
    """Case/whitespace/corporate-suffix-insensitive vendor key, e.g.
    'Sysco Corporation', 'SYSCO', and 'Sysco' all normalize to 'sysco'.
    Only ever merges spelling variants of the same string -- never
    different vendors -- so it's safe to apply unconditionally before the
    hard vendor-equality gate."""
    if pd.isna(vendor):
        return None
    v = str(vendor).strip().lower()
    v = re.sub(r"[.,]", "", v)
    v = re.sub(r"\s+", " ", v).strip()
    v = _CORPORATE_SUFFIXES.sub("", v).strip()
    return v or None


class _UnionFind:
    def __init__(self, items):
        self.parent = {item: item for item in items}

    def find(self, x):
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


_SUS_SUFFIX_RE = re.compile(r"\s*\(SUS\)\s*$", re.IGNORECASE)
_QUOTE_TRANSLATION = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"'})


def _clean_for_matching(name: str) -> str:
    """Normalizes noise that's irrelevant to product identity before
    scoring (but never touches the stored canonical_name -- only the copy
    used for comparison): strips a trailing '(SUS)' campus-reporting tag
    (confirmed with project owner these mark the same underlying product --
    ~423 real pending pairs differed by exactly this), and normalizes curly
    quotes/apostrophes to straight ones (no current pairs are actually
    split by this, but it's a real, cheap-to-fix source of false negatives
    going forward)."""
    return _SUS_SUFFIX_RE.sub("", name).translate(_QUOTE_TRANSLATION)


def _score_matrix(names: list[str]):
    cleaned = [_clean_for_matching(n) for n in names]
    return process.cdist(cleaned, cleaned, scorer=fuzz.token_sort_ratio, processor=rf_utils.default_process)


_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")


def _numeric_tokens(name: str) -> frozenset[str]:
    return frozenset(_NUMBER_RE.findall(name))


def _numbers_match(name_a: str, name_b: str) -> bool:
    """A high token_sort_ratio score can still hide a changed quantity --
    e.g. 'ONION, RED JUMBO 25-LB' vs 'ONION, RED JUMBO 5-LB' scores 97.6,
    since a 1-2 character edit in a long string barely moves the ratio, even
    though 25lb vs 5lb is a completely different product. Never a match --
    caught via audit against real data (25% of the pending review queue),
    not theoretical, and confirmed with project owner after manual review
    that differing numbers are "almost never" the same product -- excluded
    from the review queue entirely, not just auto-merge."""
    return _numeric_tokens(name_a) == _numeric_tokens(name_b)


# Synonym groups for common origin/region markers seen on produce and
# imported items. Deliberately excludes words that are far more often a
# food STYLE/TYPE than a literal origin in this data (verified against real
# product names before excluding, not guessed): "Turkey" is overwhelmingly
# the bird/meat (0 geographic uses found), "French"/"Italian" are
# overwhelmingly recipe/variety descriptors (French vanilla, Italian
# seasoning), "Canadian" means Canadian bacon (a dish, not sourcing), and
# "Texas" means Texas Toast (a bread style). Bare "Imported" is excluded
# too -- too unspecific to compare against another "Imported" (could be
# from two different countries and still both just say "Imported").
ORIGIN_GROUPS = [
    {"mexico", "mex"},
    {"california", "cali"},
    {"chile", "chilean", "chl"},
    {"peru", "peruvian"},
    {"guatemala"},
    {"honduras"},
    {"ecuador", "ecuadorian"},
    {"costa rica"},
    {"canada"},
    {"usa"},
    {"china"},
    {"spain"},
    {"italy"},
    {"france"},
    {"argentina", "argentine"},
    {"brazil"},
    {"colombia"},
    {"local"},
    {"domestic"},
    {"washington"},
    {"oregon"},
    {"arizona"},
    {"idaho"},
    {"florida"},
]
_ORIGIN_PATTERNS = [
    (i, re.compile(r"\b" + re.escape(term) + r"\b", re.IGNORECASE))
    for i, group in enumerate(ORIGIN_GROUPS)
    for term in group
]


def _origin_tokens(name: str) -> frozenset[int]:
    return frozenset(group_id for group_id, pattern in _ORIGIN_PATTERNS if pattern.search(name))


def _origins_match(name_a: str, name_b: str) -> bool:
    """'GRAPES RED SEEDLESS (CHILE)' vs '... (PERU)' scores high enough to
    otherwise pass -- a country/region name is exactly the kind of short,
    high-impact word that a long-string edit-distance ratio barely
    penalizes, same failure mode as _numbers_match. Confirmed with project
    owner after manual review: different cited origin means not the same
    product, full stop -- excluded from the review queue entirely, not
    just auto-merge. Synonyms (Mex/Mexico, Chile/Chilean/Chl, ...) are
    grouped so spelling variants of the SAME origin don't block a match."""
    return _origin_tokens(name_a) == _origin_tokens(name_b)


def merge_products(
    conn: sqlite3.Connection,
    keep_product_id: int,
    merge_product_id: int,
    cert_lookup: list[tuple[str, str, list[str]]],
    campus_type: str,
) -> None:
    """Folds merge_product_id into keep_product_id: repoints aliases, sums
    purchases rows that land on the same (campus, fiscal_year) as a result,
    merges product-level fields, and deletes the losing product row.
    cert_lookup/campus_type are needed to recompute certification_validation_flag
    and validated_sustainable_yn if the merge changes which certification text
    survives (see lib.ingestion.build_cert_lookup / validate_certification_text)."""
    if keep_product_id == merge_product_id:
        return

    conn.execute(
        "UPDATE product_aliases SET product_id = ? WHERE product_id = ?",
        (keep_product_id, merge_product_id),
    )

    tier_rank = {"reported": 0, "computed_tier2": 1, "reference_table_tier3": 2, "unresolved": 3}

    rows = conn.execute(
        "SELECT purchase_id, campus, fiscal_year, product_id, vendor, brand, total_price, "
        "total_weight_lbs, weight_source, purchase_type, n_transactions_aggregated, source_report_id "
        "FROM purchases WHERE product_id IN (?, ?)",
        (keep_product_id, merge_product_id),
    ).fetchall()
    by_period: dict[tuple, list] = {}
    for row in rows:
        by_period.setdefault((row[1], row[2]), []).append(row)

    for (_campus, _fy), period_rows in by_period.items():
        if len(period_rows) == 1:
            conn.execute(
                "UPDATE purchases SET product_id = ? WHERE purchase_id = ?",
                (keep_product_id, period_rows[0][0]),
            )
            continue

        total_price = sum(r[6] for r in period_rows if r[6] is not None) or None
        resolved = [r for r in period_rows if r[7] is not None]
        total_weight = sum(r[7] for r in resolved) if resolved else None
        weight_source = (
            max((r[8] for r in resolved), key=lambda s: tier_rank[s]) if resolved else "unresolved"
        )
        # Distributor (vendor) is already guaranteed identical -- that's the
        # matching gate. brand (the smaller-grain sub-vendor/farm/manufacturer
        # field) is a different concept that's frequently blank and was never
        # part of that gate -- preserve every distinct value seen rather than
        # dropping all but one (confirmed with project owner).
        vendor = next((r[4] for r in period_rows if r[4]), None)
        brand = ", ".join(sorted({r[5] for r in period_rows if r[5]})) or None
        purchase_type = period_rows[0][9]
        n_transactions = sum(r[10] for r in period_rows)
        source_report_id = "; ".join(sorted({r[11] for r in period_rows if r[11]})) or None
        unit_price = (total_price / total_weight) if (total_price and total_weight) else None

        keep_purchase_id = period_rows[0][0]
        conn.execute(
            "UPDATE purchases SET vendor = ?, brand = ?, total_price = ?, total_weight_lbs = ?, "
            "weight_source = ?, unit_price = ?, purchase_type = ?, n_transactions_aggregated = ?, "
            "source_report_id = ?, product_id = ? WHERE purchase_id = ?",
            (
                vendor,
                brand,
                total_price,
                total_weight,
                weight_source,
                unit_price,
                purchase_type,
                n_transactions,
                source_report_id,
                keep_product_id,
                keep_purchase_id,
            ),
        )
        for r in period_rows[1:]:
            conn.execute("DELETE FROM purchases WHERE purchase_id = ?", (r[0],))

    keep = conn.execute(
        "SELECT sustainability_certifications, sustainable_yn, certification_validation_flag, "
        "validated_sustainable_yn, first_seen_fy, last_seen_fy FROM products WHERE product_id = ?",
        (keep_product_id,),
    ).fetchone()
    loser = conn.execute(
        "SELECT sustainability_certifications, sustainable_yn, certification_validation_flag, "
        "validated_sustainable_yn, first_seen_fy, last_seen_fy FROM products WHERE product_id = ?",
        (merge_product_id,),
    ).fetchone()
    certs = keep[0] or loser[0]
    first_seen = min(x for x in (keep[4], loser[4]) if x is not None)
    last_seen = max(x for x in (keep[5], loser[5]) if x is not None)
    # Prefer a definitive answer over 'NA' (unknown) -- audited real
    # "(SUS)"-suffix pairs (the same underlying product per project owner,
    # now merged via _clean_for_matching) and found the base/no-suffix row
    # is often 'NA' while the "(SUS)" row has a real 'Y'/'N' campus-reported
    # answer; keeping only keep_product_id's value would silently discard
    # that. A genuine Y-vs-N conflict (rare -- 4 of 569 checked pairs) still
    # falls back to keep_product_id's value, since there's no principled
    # way to resolve two opposite campus-reported claims automatically.
    sustainable_yn = loser[1] if keep[1] == "NA" and loser[1] != "NA" else keep[1]

    if certs != keep[0]:
        # The merged cert text differs from what keep_product_id's flag/
        # validated_sustainable_yn were originally computed against --
        # recompute both rather than leave them stale.
        from lib.ingestion import validate_certification_text

        flag = validate_certification_text(certs, campus_type, cert_lookup)
        validated_sustainable_yn = sustainable_yn if sustainable_yn != "Y" else ("Y" if flag == 0 else "N")
    else:
        flag, validated_sustainable_yn = keep[2], keep[3]

    conn.execute(
        "UPDATE products SET sustainability_certifications = ?, sustainable_yn = ?, "
        "certification_validation_flag = ?, validated_sustainable_yn = ?, first_seen_fy = ?, "
        "last_seen_fy = ? WHERE product_id = ?",
        (certs, sustainable_yn, flag, validated_sustainable_yn, first_seen, last_seen, keep_product_id),
    )

    conn.execute(
        "UPDATE product_match_candidates SET product_id_a = ? WHERE product_id_a = ?",
        (keep_product_id, merge_product_id),
    )
    conn.execute(
        "UPDATE product_match_candidates SET product_id_b = ? WHERE product_id_b = ?",
        (keep_product_id, merge_product_id),
    )
    # A pending candidate that referenced both keep_ and merge_product_id
    # (e.g. approving one candidate while another involving the same pair
    # is still pending) now compares a product to itself -- meaningless,
    # drop it rather than surface it in the review queue. Scoped to
    # 'pending' only: an already-approved/rejected row (including the very
    # candidate a caller just approved, which is why callers must update its
    # status to 'approved' BEFORE calling merge_products) is an audit
    # record, not a live suggestion -- it must survive even though its own
    # product_id_a/b become equal as a result of this same merge.
    conn.execute(
        "DELETE FROM product_match_candidates WHERE product_id_a = product_id_b AND status = 'pending'"
    )
    conn.execute("DELETE FROM products WHERE product_id = ?", (merge_product_id,))


def find_and_merge_within_campus(
    conn: sqlite3.Connection,
    campus: str,
    cert_lookup: list[tuple[str, str, list[str]]],
    campus_type: str,
) -> dict:
    """Runs auto-merge + candidate generation for one campus. Returns
    counts for reporting."""
    df = pd.read_sql_query(
        "SELECT p.product_id, p.canonical_name, pu.vendor FROM products p "
        "JOIN purchases pu ON pu.product_id = p.product_id WHERE pu.campus = ?",
        conn,
        params=(campus,),
    )
    df["vendor_key"] = df["vendor"].apply(normalize_vendor)

    auto_merged = 0
    candidates_created = 0

    for vendor_key, group in df.groupby("vendor_key"):
        if vendor_key is None or len(group) < 2:
            continue
        ids = group["product_id"].tolist()
        names = group["canonical_name"].tolist()
        scores = _score_matrix(names)

        uf = _UnionFind(ids)
        n = len(ids)
        for i in range(n):
            for j in range(i + 1, n):
                if (
                    scores[i][j] >= AUTO_MERGE_THRESHOLD
                    and _numbers_match(names[i], names[j])
                    and _origins_match(names[i], names[j])
                ):
                    uf.union(ids[i], ids[j])

        # Merge each connected component down to its lowest product_id.
        components: dict[int, list[int]] = {}
        for pid in ids:
            components.setdefault(uf.find(pid), []).append(pid)
        redirected = {}
        for root, members in components.items():
            if len(members) < 2:
                continue
            keep_id = min(members)
            for member in members:
                if member == keep_id:
                    continue
                merge_products(conn, keep_id, member, cert_lookup, campus_type)
                auto_merged += 1
                redirected[member] = keep_id

        if not redirected:
            surviving_ids, surviving_names = ids, names
        else:
            surviving_ids, surviving_names = [], []
            for pid, name in zip(ids, names):
                if pid in redirected:
                    continue
                surviving_ids.append(pid)
                surviving_names.append(name)
            scores = _score_matrix(surviving_names) if surviving_names else scores

        m = len(surviving_ids)
        for i in range(m):
            for j in range(i + 1, m):
                score = scores[i][j]
                # Unlike an earlier version, numbers/origin guards now also
                # gate the review tier, not just auto-merge -- confirmed
                # with project owner after manually reviewing ~540 real
                # candidates that a differing quantity or cited origin is
                # "almost never" actually the same product, so it's not
                # worth the review-queue noise (was ~35% of pending
                # candidates, differing numbers alone).
                if score >= REVIEW_THRESHOLD and _numbers_match(surviving_names[i], surviving_names[j]) and _origins_match(surviving_names[i], surviving_names[j]):
                    a, b = sorted((surviving_ids[i], surviving_ids[j]))
                    conn.execute(
                        "INSERT INTO product_match_candidates (campus, campus_a, campus_b, product_id_a, "
                        "product_id_b, match_score, status) VALUES (?, ?, ?, ?, ?, ?, 'pending')",
                        (campus, campus, campus, a, b, float(score)),
                    )
                    candidates_created += 1

    conn.commit()
    return {"auto_merged": auto_merged, "candidates_created": candidates_created}


def find_and_merge_cross_campus(conn: sqlite3.Connection, cert_lookup: list[tuple[str, str, list[str]]]) -> dict:
    """Matches near-duplicate products across DIFFERENT campuses, blocked by
    SIMAP category (`products.simap_category`) rather than vendor --
    distributor overlap across campuses is too sparse to use as a gate
    (confirmed against real data: most campuses use different regional
    distributors even for the same underlying product), so two products are
    only compared if they've already landed in the same SIMAP-57 category.
    Unclassified products (`simap_category IS NULL`) are excluded from this
    pass entirely -- run `lib.simap_classification.classify_all` first.

    Unlike within-campus matching, a cross-campus "merge" never sums
    purchases -- `purchases.campus` differs between the two products' rows
    by definition, so `merge_products()`'s per-(campus, fiscal_year)
    grouping naturally just repoints `product_id` with no summing collision
    (verified this requires no changes to merge_products() itself).

    Auto-merge additionally requires both products share the same
    `campus_type` (Academic/Health) -- crossing that boundary means
    crossing certification frameworks (AASHE STARS vs Practice
    Greenhealth), which is a judgment call for a human in the review queue,
    not an automatic decision, even at a very high text similarity score.
    """
    products = pd.read_sql_query(
        "SELECT p.product_id, p.canonical_name, p.simap_category, pa.campus, c.campus_type "
        "FROM products p JOIN product_aliases pa ON pa.product_id = p.product_id "
        "JOIN campuses c ON c.campus = pa.campus "
        "WHERE p.simap_category IS NOT NULL",
        conn,
    )
    # A product could in principle have multiple aliases at the same campus
    # post within-campus merging; campus/campus_type are consistent across
    # a product's aliases since cross-campus merging hasn't run yet.
    products = products.drop_duplicates(subset="product_id")

    auto_merged = 0
    candidates_created = 0

    for category, group in products.groupby("simap_category"):
        if len(group) < 2:
            continue
        ids = group["product_id"].tolist()
        names = group["canonical_name"].tolist()
        campuses = group["campus"].tolist()
        campus_types = group["campus_type"].tolist()
        scores = _score_matrix(names)

        uf = _UnionFind(ids)
        n = len(ids)
        for i in range(n):
            for j in range(i + 1, n):
                if campuses[i] == campuses[j]:
                    continue  # same-campus pairs are within-campus matching's job
                if (
                    scores[i][j] >= AUTO_MERGE_THRESHOLD
                    and _numbers_match(names[i], names[j])
                    and _origins_match(names[i], names[j])
                    and campus_types[i] == campus_types[j]
                ):
                    uf.union(ids[i], ids[j])

        components: dict[int, list[int]] = {}
        for pid in ids:
            components.setdefault(uf.find(pid), []).append(pid)
        id_to_index = {pid: idx for idx, pid in enumerate(ids)}
        redirected = {}
        for root, members in components.items():
            if len(members) < 2:
                continue
            keep_id = min(members)
            keep_campus_type = campus_types[id_to_index[keep_id]]
            for member in members:
                if member == keep_id:
                    continue
                merge_products(conn, keep_id, member, cert_lookup, keep_campus_type)
                auto_merged += 1
                redirected[member] = keep_id

        if not redirected:
            surviving_ids, surviving_names, surviving_campuses = ids, names, campuses
        else:
            surviving_ids, surviving_names, surviving_campuses = [], [], []
            for pid, name, camp in zip(ids, names, campuses):
                if pid in redirected:
                    continue
                surviving_ids.append(pid)
                surviving_names.append(name)
                surviving_campuses.append(camp)
            scores = _score_matrix(surviving_names) if surviving_names else scores

        m = len(surviving_ids)
        for i in range(m):
            for j in range(i + 1, m):
                if surviving_campuses[i] == surviving_campuses[j]:
                    continue
                score = scores[i][j]
                if (
                    score >= REVIEW_THRESHOLD
                    and _numbers_match(surviving_names[i], surviving_names[j])
                    and _origins_match(surviving_names[i], surviving_names[j])
                ):
                    a_id, b_id = surviving_ids[i], surviving_ids[j]
                    a_campus, b_campus = surviving_campuses[i], surviving_campuses[j]
                    # Keep product_id_a/product_id_b sorted for consistency
                    # with within-campus rows (merge_products() and the
                    # self-reference cleanup rely on this ordering elsewhere).
                    if a_id > b_id:
                        a_id, b_id = b_id, a_id
                        a_campus, b_campus = b_campus, a_campus
                    conn.execute(
                        "INSERT INTO product_match_candidates (campus, campus_a, campus_b, product_id_a, "
                        "product_id_b, match_score, status) VALUES (?, ?, ?, ?, ?, ?, 'pending')",
                        (a_campus, a_campus, b_campus, a_id, b_id, float(score)),
                    )
                    candidates_created += 1

    conn.commit()
    return {"auto_merged": auto_merged, "candidates_created": candidates_created}


def find_and_merge_all(conn: sqlite3.Connection) -> dict:
    from lib.ingestion import build_cert_lookup

    cert_lookup = build_cert_lookup(conn)
    campus_types = dict(
        conn.execute("SELECT DISTINCT pu.campus, c.campus_type FROM purchases pu "
                     "JOIN campuses c ON c.campus = pu.campus").fetchall()
    )
    results = {}
    for campus, campus_type in campus_types.items():
        results[campus] = find_and_merge_within_campus(conn, campus, cert_lookup, campus_type)
    return results


if __name__ == "__main__":
    from lib.db import get_connection

    conn = get_connection()
    results = find_and_merge_all(conn)
    for campus, stats in results.items():
        print(f"{campus}: {stats['auto_merged']} auto-merged, {stats['candidates_created']} pending review")
    conn.close()
