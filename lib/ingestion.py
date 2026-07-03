"""Phase 1 ingestion pipeline: header mapping -> non-food/rollup removal ->
transaction-level aggregation -> weight resolution (Tiers 1-2 only) ->
certification validation -> canonical products/purchases rows.

Each campus's raw export uses different column names and conventions (see
CLAUDE.md). Each `load_<campus>` function below maps one campus's raw CSV
into a standardized intermediate DataFrame with these columns:

    raw_name, vendor, total_price, total_weight_lbs, weight_source,
    sustainable_yn, sustainability_certifications, purchase_type

Mapping decisions below were confirmed with the project owner (not guessed)
where CLAUDE.md flags ambiguity as a corruption risk: the UCD_H sustainable
signal, the UCR rollup-row exclusion, and UCSC's inclusion despite its very
different (manual tracking spreadsheet) structure. Tier 2 weight parsing is
intentionally conservative per campus -- anything not mechanically
straightforward is left `unresolved` for later, incremental work.
"""

import re
import sqlite3
from pathlib import Path

import pandas as pd
from rapidfuzz import fuzz, process
from rapidfuzz import utils as rf_utils

DATA_RAW_DIR = Path(__file__).resolve().parent.parent / "data" / "raw"

FRAMEWORK_BY_CAMPUS_TYPE = {"Academic": "AASHE STARS", "Health": "Practice Greenhealth"}

STANDARD_COLUMNS = [
    "raw_name",
    "vendor",
    "brand",
    "total_price",
    "total_weight_lbs",
    "weight_source",
    "sustainable_yn",
    "sustainability_certifications",
    "purchase_type",
]

# Diet/product-type tags that show up in campus certification-ish columns
# but aren't third-party certifications (confirmed with project owner) --
# excluded from sustainability_certifications rather than left to pollute
# the cert-validation review queue.
NON_CERT_VALUES = frozenset({"vegetarian", "vegan", "plant-based alternative proteins"})

# Campuses report bare "Organic" with no qualifier; certification_types.csv
# only has qualified variants. Confirmed with project owner: treat
# unqualified "Organic" as USDA Organic specifically.
ORGANIC_ALIAS_TARGET = "USDA Organic (incl. CCOF)"

# certification_types.csv has 9 distinct antibiotic-use-claim certifications
# (NAE, RWNAE, CRAU, etc.), all Practice Greenhealth-only, all sharing the
# identical qualifier "Livestock/poultry antibiotic-use claim" -- so for
# validation purposes they're interchangeable. UCD_H's generic "Antibiotic
# Free" doesn't name a specific program; confirmed with project owner to
# alias it to NAE as the representative match rather than leave it flagged.
# Framework-agnostic aliases: shortened/reworded campus phrasings confirmed
# by project owner to mean a specific certification_types.csv row, where the
# fuzzy scorer alone doesn't clear the threshold (score noted per pair).
CERT_ALIASES = {
    "organic": ORGANIC_ALIAS_TARGET,
    "antibiotic free": "No Antibiotics Ever (NAE)",
    # "MONTEREY AQUARIUM BEST CHOICE GREEN" (65.7) and "Monterey Bay Aquarium
    # (MBA)" (91.3, already clears threshold unaided) both name the same
    # program. Note: this cert is AASHE STARS-only in certification_types.csv
    # (no Practice Greenhealth equivalent exists there), so Health-campus
    # mentions will still correctly fail validation after this alias --
    # that's a framework-coverage gap in the reference table, not a naming
    # issue this alias can or should paper over.
    "monterey aquarium best choice green": "Monterey Bay Aquarium Seafood Watch",
    # "GAP 4 Certified" (75.0, below threshold) -- GAP itself already matches
    # via exact abbreviation lookup; this covers the fuller phrasing.
    "gap 4 certified": "Global Animal Partnership Certified",
}

# Campus-type-scoped aliases: certification_types.csv has separate rows for
# the same underlying concept split by framework -- "Fair Trade Certifications"
# (AASHE STARS, abbrev FT) vs. "Fairtrade International" (Practice
# Greenhealth, no abbrev). Bare "Fair Trade" already matches the AASHE STARS
# row at a perfect score for Academic campuses; Health campuses need to be
# routed to the PGH-specific row instead, which scores too low (54.5) to be
# picked by fuzzy matching alone ("Fairtrade" as one word vs "Fair Trade" as
# two hurts token-based scoring).
CERT_ALIASES_BY_CAMPUS_TYPE = {
    "Health": {"fair trade": "Fairtrade International"},
}


# --------------------------------------------------------------------------
# Shared parsing helpers
# --------------------------------------------------------------------------

def _parse_currency(value) -> float | None:
    if pd.isna(value):
        return None
    s = str(value).strip().replace("$", "").replace(",", "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _clean_str(series: pd.Series) -> pd.Series:
    """Strip whitespace; normalize NaN and whitespace-only strings to pd.NA."""

    def clean(v):
        if pd.isna(v):
            return pd.NA
        v = str(v).strip()
        return v if v else pd.NA

    return series.apply(clean)


def _combine_text_columns(df: pd.DataFrame, cols: list[str], drop_values=frozenset({"x"}) | NON_CERT_VALUES) -> pd.Series:
    """Comma-join non-blank values across several free-text cert columns,
    dropping placeholder values like a bare 'x' checkbox mark."""

    def combine(row):
        parts = []
        for c in cols:
            v = row[c]
            if pd.isna(v):
                continue
            v = str(v).strip()
            if not v or v.lower() in drop_values:
                continue
            parts.append(v)
        return ", ".join(parts) if parts else pd.NA

    return df.apply(combine, axis=1)


# --------------------------------------------------------------------------
# Per-campus loaders
# --------------------------------------------------------------------------

def load_ucb(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    out = pd.DataFrame(index=df.index)
    out["raw_name"] = _clean_str(df["Product Name or Description "])
    out["vendor"] = df["Distributor"]
    out["brand"] = _clean_str(df["Brand"])
    out["total_price"] = df[" Extended Price "].apply(_parse_currency)

    qty = df[" Quantity sold "].astype(str).str.replace(",", "").str.strip()
    m = qty.str.extract(r"([\d.]+)\s*([A-Za-z]+)")
    num = pd.to_numeric(m[0], errors="coerce")
    unit = m[1].str.lower()
    weight = pd.Series(float("nan"), index=df.index, dtype="float64")
    weight[unit == "lb"] = num[unit == "lb"]
    weight[unit == "oz"] = num[unit == "oz"] / 16.0
    weight[unit == "kg"] = num[unit == "kg"] * 2.20462
    out["total_weight_lbs"] = weight
    out["weight_source"] = weight.notna().map({True: "reported", False: "unresolved"})

    out["sustainable_yn"] = df["AASHE?"].map({"Yes": "Y", "No": "N"}).fillna("NA")
    # 'Certification' sometimes holds a bare 'x' checkbox mark instead of a
    # cert name -- the real abbreviation is in 'Cert' for those rows.
    out["sustainability_certifications"] = _combine_text_columns(df, ["Certification", "Cert"])
    out["purchase_type"] = "purchasing"
    return out[STANDARD_COLUMNS]


def load_ucd(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    out = pd.DataFrame(index=df.index)
    out["raw_name"] = _clean_str(df["Name"])
    out["vendor"] = df["Distributor Name (delivering entity)"]
    # 'Manufacturer/Farm Name' is 100% blank in this export (verified) --
    # not worth capturing. 'Brand Name' is the populated smaller-grain field.
    out["brand"] = _clean_str(df["Brand Name (if different from Column F)"])
    out["total_price"] = df["Total Spend"].apply(_parse_currency)

    # Item Weight == Case Net Weight in every row (verified) -- weight of one
    # unit of "Item UOM (e.g.LB)" (== Total Quantity Purchased UOM, also
    # verified identical). total_weight = per-unit weight x quantity
    # purchased, uniformly across UOMs -- when UOM is already LB, per-unit
    # weight is ~1 so this reduces to using the reported quantity directly.
    weight_per_unit = pd.to_numeric(df["Item Weight (e.g.5)"], errors="coerce")
    qty = pd.to_numeric(df["Total Quantity Purchased"], errors="coerce")
    uom = df["Item UOM (e.g.LB)"].astype(str).str.strip().str.upper()
    out["total_weight_lbs"] = weight_per_unit * qty
    out["weight_source"] = uom.isin(["LB", "5LB"]).map({True: "reported", False: "computed_tier2"})

    out["sustainable_yn"] = df["STARS Certification"].apply(
        lambda v: "NA" if pd.isna(v) else ("N" if str(v).strip() == "No" else "Y")
    )
    # 'Institution - Affirmed Production' deliberately excluded: its values
    # are 'Yes' / 'Women Owned Business', not certification names (verified) --
    # an institutional/equity attribute, not a sustainability certification.
    cert_cols = [
        "Sustainable Farming",
        "Sustainable Agriculture",
        "Humane Animal Care",
        "Sustainable Seafood",
        "Fair Trade / Labor",
    ]
    out["sustainability_certifications"] = _combine_text_columns(df, cert_cols)
    out["purchase_type"] = "purchasing"
    return out[STANDARD_COLUMNS]


def load_ucd_h(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    out = pd.DataFrame(index=df.index)
    out["raw_name"] = _clean_str(df["Name"])
    out["vendor"] = df["supplier"]
    out["brand"] = _clean_str(df["Brand"])
    out["total_price"] = df[" total_cost "].apply(_parse_currency)

    weight = pd.to_numeric(df["weight_lb"], errors="coerce")
    out["total_weight_lbs"] = weight
    out["weight_source"] = weight.notna().map({True: "reported", False: "unresolved"})

    cert = _clean_str(df["env_type_cert"])
    # UCD_H has no explicit sustainable Y/N column (unlike UCLA_H/UCSD_H) --
    # confirmed with project owner: infer from whether any certification was
    # reported at all.
    out["sustainable_yn"] = cert.notna().map({True: "Y", False: "N"})
    out["sustainability_certifications"] = cert
    out["purchase_type"] = "purchasing"
    return out[STANDARD_COLUMNS]


def load_ucla_h(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    out = pd.DataFrame(index=df.index)
    # 'description' is 100% empty in this export; the actual item text is in
    # 'notes' (verified).
    out["raw_name"] = _clean_str(df["notes"])
    out["vendor"] = df["supplier"]
    out["brand"] = _clean_str(df["brand"])
    out["total_price"] = df["total_cost"].apply(_parse_currency)

    weight = pd.to_numeric(df["weight"], errors="coerce")
    out["total_weight_lbs"] = weight
    out["weight_source"] = weight.notna().map({True: "reported", False: "unresolved"})

    edt = _clean_str(df["env_defined_type"])
    out["sustainable_yn"] = edt.map({"Sustainable": "Y", "Conventional": "N"}).fillna("NA")
    cert = _clean_str(df["env_type_cert"])
    # env_type_cert also holds diet/product-type tags (e.g. 'VEGETARIAN')
    # rather than certifications -- UCLA_H has no separate diet-attribute
    # column to hold these, unlike UCD (confirmed with project owner).
    cert = cert.where(~cert.str.lower().isin(NON_CERT_VALUES))
    out["sustainability_certifications"] = cert
    out["purchase_type"] = "purchasing"
    return out[STANDARD_COLUMNS]


def load_ucr(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    # Category rollup/subtotal rows (Name='PRODUCE', Vendor='Not Found', etc.)
    # use Purchase Unit == 'DOLR' as a dollar-total placeholder rather than a
    # real unit -- confirmed with project owner these aren't line items.
    df = df[df["Purchase Unit"] != "DOLR"].copy()

    out = pd.DataFrame(index=df.index)
    out["raw_name"] = _clean_str(df["Name"])
    out["vendor"] = df["Vendor"]
    out["brand"] = pd.NA  # no separate brand/manufacturer column in this export
    out["total_price"] = df[" Total Spend "].apply(_parse_currency)

    units_num = pd.to_numeric(df["Units"].astype(str).str.replace(",", "").str.strip(), errors="coerce")
    # Purchase Unit values like "20#CS" / "5#BG" embed lbs-per-unit; "12/CS"
    # (count per case, not weight) and similar are left unresolved.
    m = df["Purchase Unit"].astype(str).str.extract(r"^(\d+(?:\.\d+)?)\s*#")
    lb_per_unit = pd.to_numeric(m[0], errors="coerce")
    weight = lb_per_unit * units_num
    out["total_weight_lbs"] = weight
    out["weight_source"] = weight.notna().map({True: "computed_tier2", False: "unresolved"})

    sus = _clean_str(df["Sustainability"])
    out["sustainable_yn"] = sus.notna().map({True: "Y", False: "N"})
    out["sustainability_certifications"] = sus
    out["purchase_type"] = "purchasing"
    return out[STANDARD_COLUMNS]


def load_ucsc(path: Path) -> pd.DataFrame:
    # Row 1 is a stray summary row; the real header is row 2.
    df = pd.read_csv(path, skiprows=1, low_memory=False)
    out = pd.DataFrame(index=df.index)
    out["raw_name"] = _clean_str(df["Product Description"])
    out["vendor"] = df["Vendor"]
    out["brand"] = _clean_str(df["Label/Brand"])
    out["total_price"] = df["Total Item cost"].apply(_parse_currency)

    # 'Total Weight (in lbs)' is unusable (100% of populated cells are a '-'
    # placeholder). Instead, 'Total units ordered' already reports the total
    # quantity in whatever 'Unit Type' specifies -- when that's LB it's a
    # direct weight; OZ needs only a unit conversion, not estimation. Other
    # unit types (CT/EA/GAL/PK/...) would need per-item reference weights
    # (Tier 3), out of scope for Phase 1.
    unit_type = df["Unit\nType"].astype(str).str.strip().str.upper()
    total_units = pd.to_numeric(
        df["Total units ordered"].astype(str).str.replace(",", "").str.strip(), errors="coerce"
    )
    weight = pd.Series(float("nan"), index=df.index, dtype="float64")
    weight[unit_type == "LB"] = total_units[unit_type == "LB"]
    weight[unit_type == "OZ"] = total_units[unit_type == "OZ"] / 16.0
    out["total_weight_lbs"] = weight
    out["weight_source"] = weight.notna().map({True: "reported", False: "unresolved"})

    # AASHE STARS is UCSC's applicable standard (Academic) -- use the STARS
    # column specifically, not the broader 'Real Food' calculator flag,
    # per CLAUDE.md's rule that sustainable_yn is always the AASHE/PGH signal.
    stars = _clean_str(df["STARS 3.0 Cert"])
    out["sustainable_yn"] = stars.map({"✅": "Y", "❌": "N"}).fillna("NA")
    out["sustainability_certifications"] = pd.NA
    out["purchase_type"] = "purchasing"
    return out[STANDARD_COLUMNS]


def load_ucsd_h(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    out = pd.DataFrame(index=df.index)
    out["raw_name"] = _clean_str(df["ProductName"])
    # 'Distributor' is the delivering entity and is never blank (verified) --
    # it's the correct field for the entity-matching vendor gate on its own.
    # 'Supplier' is a distinct, smaller-grain concept (64% blank) that was
    # previously wrongly used as a Distributor fallback, conflating the two;
    # it belongs in 'brand' instead (confirmed with project owner).
    out["vendor"] = _clean_str(df["Distributor"])
    out["brand"] = _clean_str(df["Supplier"])
    out["total_price"] = df["total_cost"].apply(_parse_currency)

    weight = pd.to_numeric(df["weight"], errors="coerce")
    out["total_weight_lbs"] = weight
    out["weight_source"] = weight.notna().map({True: "reported", False: "unresolved"})

    sus = _clean_str(df["Sustainable?"])
    out["sustainable_yn"] = sus.map({"Sustainable": "Y", "Conventional": "N"}).fillna("NA")
    out["sustainability_certifications"] = _clean_str(df["Certification"])
    out["purchase_type"] = "purchasing"
    return out[STANDARD_COLUMNS]


CAMPUS_LOADERS = {
    "UCB": load_ucb,
    "UCD": load_ucd,
    "UCD_H": load_ucd_h,
    "UCLA_H": load_ucla_h,
    "UCR": load_ucr,
    "UCSC": load_ucsc,
    "UCSD_H": load_ucsd_h,
}


# --------------------------------------------------------------------------
# Non-food line removal (step 2 of ingestion, per CLAUDE.md)
# --------------------------------------------------------------------------

# Conservative, confirmed-safe keywords only (checked against real data for
# false positives first). BAG and CUP were deliberately excluded: both key
# on packaging language that also appears on real food items (candy "in a
# bag," yogurt/applesauce/ice cream "cups") -- deferred to Phase 3, where a
# SIMAP-category signal exists instead of a keyword guess.
NON_FOOD_KEYWORDS = ["NAPKIN", "GLOVE", "LID", "UNIFORM"]

# Water -- confirmed with project owner: there's no such thing as
# "sustainable water" for this project's purposes, so plain/sparkling water
# is treated as non-food, same as gloves/napkins. Deliberately narrow:
# covers unambiguous plain/sparkling water (a brand that's always water
# regardless of flavor -- Bubly -- plus generic spring/mineral/bottled
# water phrasing and known plain-water-only brands) and stops there. Bare
# "Water" is NOT included -- auditing real data found it matches "Tuna in
# Water," "Ham & Water," "Cake Mix Add Water," "Water Crackers," etc., a
# huge false-positive rate. Ambiguous flavored waters (coconut water,
# Propel, fruit-infused water) are explicitly out of scope for now --
# project owner flagged that line as a genuine, unresolved judgment call,
# not something to guess at here.
WATER_KEYWORDS = [
    "Bubly",
    "Spring Water",
    "Mineral Water",
    "Bottled Water",
    "Water Bottled",
    "Aquafina",
    "Voss",
]

NON_FOOD_PATTERN = r"\b(?:" + "|".join(NON_FOOD_KEYWORDS + WATER_KEYWORDS) + r")\b"


def split_non_food(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Returns (food_rows, non_food_rows). non_food_rows is logged for audit,
    never silently dropped without a trace."""
    mask = df["raw_name"].str.contains(NON_FOOD_PATTERN, case=False, na=False, regex=True)
    return df[~mask].copy(), df[mask].copy()


# --------------------------------------------------------------------------
# Certification validation (QA only -- never overwrites sustainable_yn)
# --------------------------------------------------------------------------

def build_cert_lookup(conn: sqlite3.Connection) -> list[tuple[str, str, list[str]]]:
    rows = conn.execute(
        "SELECT certification_name, abbreviation, frameworks FROM certification_types"
    ).fetchall()
    return [
        (name, abbreviation, [f.strip() for f in frameworks.split(";")])
        for name, abbreviation, frameworks in rows
    ]


def validate_certification_text(
    reported_text, campus_type: str, cert_lookup: list[tuple[str, str, list[str]]], threshold: int = 85
) -> int:
    """Returns certification_validation_flag: 0 = every reported cert
    matched a known certification (by name or abbreviation) whose frameworks
    cover this campus type's standard; 1 = at least one reported cert
    didn't validate. Blank input (no cert claimed) is not a mismatch -> 0."""

    if pd.isna(reported_text) or not str(reported_text).strip():
        return 0

    applicable = FRAMEWORK_BY_CAMPUS_TYPE[campus_type]
    names = [name for name, _, _ in cert_lookup]
    frameworks_by_name = {name: frameworks for name, _, frameworks in cert_lookup}
    # Abbreviations are short (2-4 letters) -- fuzzy-matching those against
    # each other or against full names is unreliable, so they're matched
    # exactly (case-insensitive) rather than via rapidfuzz.
    frameworks_by_abbrev = {
        abbrev.strip().lower(): frameworks for _, abbrev, frameworks in cert_lookup if abbrev and abbrev.strip()
    }

    scoped_aliases = CERT_ALIASES_BY_CAMPUS_TYPE.get(campus_type, {})

    parts = [p.strip() for p in re.split(r"[,;]", str(reported_text)) if p.strip()]
    for part in parts:
        part = scoped_aliases.get(part.lower()) or CERT_ALIASES.get(part.lower(), part)

        abbrev_frameworks = frameworks_by_abbrev.get(part.lower())
        if abbrev_frameworks is not None:
            if applicable not in abbrev_frameworks:
                return 1
            continue

        # token_set_ratio safely matches a shortened campus phrasing that's a
        # true subset of the full certification name (e.g. "Fair Trade" ->
        # "Fair Trade Certifications", "Rainforest Alliance" -> "...
        # Certified"). It's too permissive for single-word input though --
        # "Organic" is a token-subset of half a dozen unrelated *_Organic
        # certifications, so single-word parts stay on the stricter
        # token_sort_ratio scorer only.
        scorer = fuzz.token_set_ratio if len(part.split()) > 1 else fuzz.token_sort_ratio
        match = process.extractOne(
            part, names, scorer=scorer, score_cutoff=threshold, processor=rf_utils.default_process
        )
        if match is None:
            return 1
        matched_name = match[0]
        if applicable not in frameworks_by_name[matched_name]:
            return 1
    return 0


# --------------------------------------------------------------------------
# Aggregation + canonical table writes
# --------------------------------------------------------------------------

def aggregate_and_load(
    df: pd.DataFrame,
    campus: str,
    campus_type: str,
    fiscal_year: int,
    conn: sqlite3.Connection,
    source_report_id: str,
    cert_lookup: list[tuple[str, list[str]]],
) -> dict:
    df = df[df["raw_name"].notna()].copy()
    before = len(df)

    grouped = df.groupby("raw_name", dropna=False)

    n_products = 0
    n_purchases = 0
    for raw_name, group in grouped:
        total_price = group["total_price"].sum(min_count=1)
        total_weight = group["total_weight_lbs"].sum(min_count=1)

        # If every row in the group is unresolved, the aggregate is
        # unresolved. Otherwise use the weakest tier among rows that
        # actually contributed weight (a mixed group is only as trustworthy
        # as its least-certain contributor).
        tier_rank = {"reported": 0, "computed_tier2": 1, "reference_table_tier3": 2, "unresolved": 3}
        resolved_sources = group.loc[group["total_weight_lbs"].notna(), "weight_source"]
        weight_source = (
            "unresolved" if resolved_sources.empty else max(resolved_sources, key=lambda s: tier_rank[s])
        )
        if resolved_sources.empty:
            total_weight = None

        vendor = group["vendor"].dropna().iloc[0] if group["vendor"].notna().any() else None
        brand = group["brand"].dropna().iloc[0] if group["brand"].notna().any() else None
        sustainable_yn = group["sustainable_yn"].mode().iloc[0] if not group["sustainable_yn"].mode().empty else "NA"
        certs = group["sustainability_certifications"].dropna()
        certs_text = certs.iloc[0] if not certs.empty else None
        purchase_type = group["purchase_type"].mode().iloc[0]
        unit_price = (total_price / total_weight) if (total_price and total_weight) else None

        flag = validate_certification_text(certs_text, campus_type, cert_lookup)

        # sustainable_yn is campus-reported and stays untouched, per CLAUDE.md,
        # for audit/provenance. validated_sustainable_yn is the derived signal
        # for optimizer/reporting use: a campus 'Y' only counts once its
        # claimed certification(s) actually validate against the known
        # vocabulary (or none were claimed, i.e. nothing to invalidate); 'N'
        # and 'NA' pass through unchanged since the claim isn't positive.
        validated_sustainable_yn = sustainable_yn if sustainable_yn != "Y" else ("Y" if flag == 0 else "N")

        cur = conn.execute(
            "INSERT INTO products (canonical_name, sustainability_certifications, sustainable_yn, "
            "certification_validation_flag, validated_sustainable_yn, first_seen_fy, last_seen_fy) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (raw_name, certs_text, sustainable_yn, flag, validated_sustainable_yn, fiscal_year, fiscal_year),
        )
        product_id = cur.lastrowid
        n_products += 1

        conn.execute(
            "INSERT INTO product_aliases (raw_name, campus, product_id, match_confidence, human_confirmed) "
            "VALUES (?, ?, ?, ?, ?)",
            (raw_name, campus, product_id, 1.0, 0),
        )

        conn.execute(
            "INSERT INTO purchases (campus, fiscal_year, product_id, vendor, brand, total_price, "
            "total_weight_lbs, weight_source, unit_price, purchase_type, n_transactions_aggregated, "
            "source_report_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                campus,
                fiscal_year,
                product_id,
                vendor,
                brand,
                total_price,
                total_weight,
                weight_source,
                unit_price,
                purchase_type,
                len(group),
                source_report_id,
            ),
        )
        n_purchases += 1

    conn.commit()
    return {
        "rows_in": before,
        "products_created": n_products,
        "purchases_created": n_purchases,
    }


# --------------------------------------------------------------------------
# Orchestrator
# --------------------------------------------------------------------------

CAMPUS_FILES = {
    "UCB": "UCB_FY25.csv",
    "UCD": "UCD_FY25.csv",
    "UCD_H": "UCD_H_FY25.csv",
    "UCLA_H": "UCLA_H_FY25.csv",
    "UCR": "UCR_FY25.csv",
    "UCSC": "UCSC_FY25.csv",
    "UCSD_H": "UCSD_H_FY25.csv",
}


def ingest_all(
    conn: sqlite3.Connection,
    data_dir: Path = DATA_RAW_DIR,
    fiscal_year: int = 2025,
    non_food_log_path: Path | None = None,
) -> dict:
    conn.execute("DELETE FROM purchases")
    conn.execute("DELETE FROM product_aliases")
    conn.execute("DELETE FROM products")
    conn.commit()

    cert_lookup = build_cert_lookup(conn)
    campus_types = dict(conn.execute("SELECT abbreviation, campus_type FROM campuses").fetchall())
    campus_names = dict(conn.execute("SELECT abbreviation, campus FROM campuses").fetchall())

    results = {}
    non_food_rows = []
    for abbrev, filename in CAMPUS_FILES.items():
        path = data_dir / filename
        if not path.exists():
            continue
        loader = CAMPUS_LOADERS[abbrev]
        campus_type = campus_types[abbrev]
        campus_name = campus_names[abbrev]
        df = loader(path)
        df, non_food = split_non_food(df)
        if not non_food.empty:
            non_food = non_food.copy()
            non_food.insert(0, "campus", campus_name)
            non_food_rows.append(non_food)
        stats = aggregate_and_load(
            df, campus_name, campus_type, fiscal_year, conn, source_report_id=filename, cert_lookup=cert_lookup
        )
        stats["non_food_removed"] = len(non_food)
        results[abbrev] = stats

    if non_food_log_path is not None and non_food_rows:
        pd.concat(non_food_rows, ignore_index=True).to_csv(non_food_log_path, index=False)

    return results


if __name__ == "__main__":
    from lib.db import get_connection, init_db

    conn = get_connection()
    init_db(conn)
    non_food_log = DATA_RAW_DIR.parent / "processed" / "non_food_removed.csv"
    results = ingest_all(conn, non_food_log_path=non_food_log)
    for campus, stats in results.items():
        weight_stats = conn.execute(
            "SELECT weight_source, COUNT(*) FROM purchases p JOIN campuses c ON p.campus = c.campus "
            "WHERE c.abbreviation = ? GROUP BY weight_source",
            (campus,),
        ).fetchall()
        print(
            f"{campus}: {stats['rows_in']} rows -> {stats['products_created']} products "
            f"({stats['non_food_removed']} non-food rows removed); weight_source: {dict(weight_stats)}"
        )
    conn.close()
