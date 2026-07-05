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
WEIGHT_DICTIONARIES_DIR = Path(__file__).resolve().parent.parent / "reference" / "weight_dictionaries"

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

# UCB's raw export is a combined sheet pooling per-distributor PO reports
# into common columns (Pack size / Price / Quantity sold) -- confirmed with
# project owner (after an initial blanket Tier 2 attempt using the product
# NAME text was reverted for being unreliable, see below) that these column
# names do NOT mean the same thing for every distributor, since each
# distributor's own report format got mapped into this shared shape
# differently. What each distributor's "Quantity sold" actually counts was
# reverse-engineered per distributor by checking Price * Quantity sold ==
# Extended Price (holds exactly for distributors with a populated Price
# column) and, more importantly, by computing an implied $/lb and checking
# it's a plausible wholesale food price -- NOT guessed.
#
# A first attempt at UCB Tier 2 weight resolution parsed a per-unit oz/lb
# size out of the product NAME text and multiplied by "Quantity sold"
# directly (e.g. "MEATBALL BEEF ITAL STYLE 1 OZ" -> 1 oz/piece). This was
# reverted: it implied a median $107.60/lb vs $5.45/lb for genuinely
# reported weights, because for many prepackaged items "Quantity sold" is
# actually a CASE count while the oz figure in the name describes one
# piece inside the case, not the whole unit sold.
#
# The real, distributor-specific semantics (each verified with a $/lb
# sanity check against real data):
#   - Sysco, Pacific Seafood: "Pack size" is the weight of ONE CASE (e.g.
#     "5 LB", "6#AVG", "5 2 LB" meaning 5 sub-packs of 2 lb), "Quantity
#     sold" is the number of cases -- multiply them (see
#     _sysco_pack_size_to_lb). "Pack size" is sometimes instead an
#     individual small PORTION within a much bigger case (e.g. "SUGAR SUB
#     SWEETENER SPLENDA" Pack size='1GM', one packet) -- multiplying that
#     by case count the same way produced the same order-of-magnitude
#     error as the reverted attempt above, so results under 1 lb are
#     discarded as untrustworthy (no genuine bulk case size was found
#     under 1 lb in this data; this is a safety margin, not a guess).
#   - Allen Brothers, Cream Co: "Quantity sold" IS the weight in lbs
#     directly already (verified: median implied price ~$5-6/lb, in line
#     with wholesale beef/poultry/lamb) -- "Pack size" here is just
#     packaging description, not a multiplier to apply.
#   - Daylight Foods, Peets, JFC: "Pack size" is either blank (Daylight) or
#     a bare case-item-count that duplicates a number already in the name
#     (Peets/JFC) -- the actual per-case weight is embedded in the product
#     name itself (e.g. "SPRING MIX, SWEET ORG 3-LB", "BROCCOLI, FLORETS
#     4/3-LB" -- 4 sub-packs of 3 lb, "Glico Pocky...12/10/1.45 oz" -- 12
#     cases x 10 boxes x 1.45 oz/piece), same case-count-times-Quantity-sold
#     pattern as Sysco (see _name_embedded_per_case_lb).
#   - Vistar: already fully "reported" in this export -- its own Quantity
#     sold column already carries an explicit lb/oz unit, no distributor-
#     specific handling needed.
#   - Every other distributor (UNFI, Pepsi, Bordanaves, Ben & Jerry's,
#     Kikka Sushi, Espostos, Bimbo Bakery, City Baking, etc.): Pack size is
#     either blank or a pure count (dozens/each/CT) with no weight signal
#     anywhere -- not a parsing gap, a genuine absence of size information.
#     Left unresolved rather than guessed; would need a Tier 3
#     reference-item weight table (e.g. "1 ice cream pint ~ 1 lb"), which
#     is a different kind of estimate (an assumed typical weight, not a
#     value read off this data) and needs human review before being
#     trusted the same way a parsed number is -- out of scope here.
_MIN_PLAUSIBLE_CASE_LB = 1.0


def _lb_unit_factor(unit: str | None) -> float | None:
    """Converts a weight-unit token to a lb-per-unit multiplier. None
    means an implicit '#' (pounds) with no unit word attached at all,
    e.g. Sysco's bare "6#AVG"/"11#AV". Returns None for anything that
    isn't a recognized weight unit (count/volume units are handled by the
    caller declining to match in the first place)."""
    if unit is None:
        return 1.0
    unit = unit.upper()
    if unit in ("LB", "LBS", "POUND", "POUNDS"):
        return 1.0
    if unit == "OZ":
        return 1.0 / 16.0
    if unit in ("KG", "KGS", "KILO"):
        return 2.20462
    if unit in ("G", "GM", "GMS"):
        return 0.00220462
    return None


# Three shapes seen in real Sysco/Pacific Seafood "Pack size" values, tried
# in order: "N/M unit" or "N M unit" (a case of N sub-packs of M each, e.g.
# "4/5 LB", "5 2 LB"), "N-M[unit]" (a size range, averaged, e.g. "8-10#"),
# and a bare "N[#][unit][AVG|AV]" (e.g. "20 LB", "6#AVG", "10 POUND").
_SYSCO_PACK_MULT_RE = re.compile(
    r"^(\d+(?:\.\d+)?)\s*[/ ]\s*(\d+(?:\.\d+)?)\s*(LBS?|POUNDS?|OZ|KGS?|KILO|GMS?|G)\s*(?:AVG?|AV)?$",
    re.IGNORECASE,
)
_SYSCO_PACK_RANGE_RE = re.compile(
    r"^(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*#?\s*(LBS?|POUNDS?|OZ|KGS?|KILO|GMS?|G)?\s*(?:AVG?|AV)?$",
    re.IGNORECASE,
)
_SYSCO_PACK_BARE_RE = re.compile(
    r"^(\d+(?:\.\d+)?)\s*(#)?\s*(LBS?|POUNDS?|OZ|KGS?|KILO|GMS?|G)?\s*(?:AVG?|AV)?$",
    re.IGNORECASE,
)


def _sysco_pack_size_to_lb(pack_size) -> float | None:
    if pd.isna(pack_size):
        return None
    s = str(pack_size).strip()

    m = _SYSCO_PACK_MULT_RE.match(s)
    if m:
        n, unit_size, unit = float(m.group(1)), float(m.group(2)), m.group(3)
        factor = _lb_unit_factor(unit)
        result = n * unit_size * factor if factor else None
    else:
        m = _SYSCO_PACK_RANGE_RE.match(s)
        if m:
            lo, hi, unit = float(m.group(1)), float(m.group(2)), m.group(3)
            factor = _lb_unit_factor(unit)
            result = ((lo + hi) / 2) * factor if factor else None
        else:
            m = _SYSCO_PACK_BARE_RE.match(s)
            if m:
                n, has_hash, unit = float(m.group(1)), m.group(2), m.group(3)
                if unit is None and not has_hash:
                    result = None  # bare count (e.g. Peets' plain "12") -- no weight signal
                else:
                    factor = _lb_unit_factor(unit)
                    result = n * factor if factor else None
            else:
                result = None

    if result is not None and result < _MIN_PLAUSIBLE_CASE_LB:
        return None
    return result


# Daylight Foods (Pack size column entirely blank in this export), Peets,
# and JFC all embed the per-case weight in the product name itself instead
# of a structured column -- same underlying pattern, three distributors.
# Handles chains of any length before the unit, not just two numbers: a
# first version only captured the last two ("N/M unit") and silently
# undercounted real 3-level packs like "Glico Pocky...12/10/1.45 oz" (12
# cases x 10 boxes x 1.45 oz/piece) by 12x -- caught via the same $/lb
# sanity check (implied $231/lb for a snack food, vs. a plausible ~$19/lb
# once all three numbers are multiplied together).
#
# Also reused for UC Riverside's Sysco rows (see load_ucr) -- same shape,
# but that campus's names frequently use a bare "#" for pounds instead of
# the word "LB" (e.g. "CHICKEN BREAST HALAL GROUND 4/10# AVG"), so a
# trailing "#" is accepted as a pounds unit alongside LB/OZ. No trailing
# \b after "#" itself (both sides of "# " are non-word characters, so \b
# would never match there) -- the digit-chain-then-unit shape is
# unambiguous enough on its own; verified against real UCR names with a
# grade marker in "#N" form (e.g. "MUSHROOM, MEDIUM #1 10-LB") that this
# doesn't misfire, since a grade marker has the "#" BEFORE the digit, the
# reverse of the order this regex requires.
_NAME_CASEPACK_RE = re.compile(
    r"((?:\d+(?:\.\d+)?\s*/\s*)+\d+(?:\.\d+)?)\s*-?\s*(?:(LB|OZ)\b|(#))", re.IGNORECASE
)
_NAME_BARE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*-?\s*(?:(LB)\b|(#))", re.IGNORECASE)


def _name_embedded_weight_with_shape(name: str) -> tuple[float | None, str]:
    """Returns (value, shape), with NO plausibility floor applied here --
    that's the caller's job, since what counts as "plausible" depends on
    whether value is already a complete per-case weight or still needs an
    external case-count multiplier (see below).

    shape='chain' means the name's own number chain already fully
    specifies a per-case weight (e.g. "6/5LB" = 6 sub-packs of 5 lb --
    nothing else needed). shape='bare' means the name only states a
    single per-ITEM weight (e.g. "5LB", "1LB") with no case-multiplier of
    its own -- callers with a separate case-pack-count signal (e.g. UC
    Riverside's Sysco "Purchase Unit" column, see load_ucr) must multiply
    it in themselves before applying any plausibility floor. shape='none'
    means no weight token was found at all."""
    m = _NAME_CASEPACK_RE.search(name)
    if m:
        total = 1.0
        for token in m.group(1).split("/"):
            total *= float(token.strip())
        unit_word = m.group(2)
        result = total / 16.0 if unit_word and unit_word.upper() == "OZ" else total
        return result, "chain"

    m2 = _NAME_BARE_RE.search(name)
    if m2:
        return float(m2.group(1)), "bare"
    return None, "none"


def _name_embedded_per_case_lb(name: str) -> float | None:
    value, _shape = _name_embedded_weight_with_shape(name)
    if value is not None and value < _MIN_PLAUSIBLE_CASE_LB:
        return None
    return value


# --------------------------------------------------------------------------
# Tier 3: reference-item weight lookups (an ASSUMED typical weight for a
# named item, not a value read off this data -- see CLAUDE.md's "Weight
# resolution" section). Distinct from every Tier 2 rule above: those parse
# a number that's genuinely stated somewhere (a column or the product
# name); these apply an estimate for items with no size information
# anywhere at all (e.g. "BJ Pt Cherry Garcia" -- just a flavor name and a
# case count, no weight stated). Each dictionary entry carries a
# `human_confirmed` flag (0 = AI-drafted, pending project owner review; 1 =
# reviewed and trusted) -- confirmed via project owner: unconfirmed entries
# are still applied (better than leaving a resolvable item unresolved) but
# are tagged 'reference_table_tier3' rather than 'computed_tier2' so their
# provenance stays visible to anyone auditing weight data downstream.
def _load_pattern_weight_dict(csv_path: Path) -> list[tuple[re.Pattern, float, bool]]:
    """Loads a {pattern, weight_group_key, assumed_each_weight_oz,
    assumed_each_weight_lb, assumption_note, source_url, confidence,
    human_confirmed} CSV into [(compiled pattern, lb, human_confirmed)],
    in file order (first match wins, so more specific patterns should
    precede more general ones in the CSV -- see kikkasushi's SPRING ROLL
    before ROLL)."""
    df = pd.read_csv(csv_path)
    return [
        (re.compile(row["pattern"], re.IGNORECASE), float(row["assumed_each_weight_lb"]), bool(row["human_confirmed"]))
        for _, row in df.iterrows()
    ]


def _load_exact_title_weight_dict(csv_path: Path) -> dict[str, tuple[float, bool]]:
    """Loads a {title, ..., assumed_each_weight_lb, ..., human_confirmed}
    CSV into {title: (lb, human_confirmed)} for exact raw_name lookup."""
    df = pd.read_csv(csv_path)
    return {
        row["title"]: (float(row["assumed_each_weight_lb"]), bool(row["human_confirmed"]))
        for _, row in df.iterrows()
    }


def _load_gal_density_dict(csv_path: Path) -> list[tuple[re.Pattern, float, bool]]:
    """Loads a {pattern, weight_group_key, density_lb_per_gal, ...,
    human_confirmed} CSV into [(compiled pattern, lb_per_gal,
    human_confirmed)] -- same shape/lookup mechanics as
    _load_pattern_weight_dict, but the value is a density (lb/gal) to be
    multiplied by a gallon quantity, not a per-item weight."""
    df = pd.read_csv(csv_path)
    return [
        (re.compile(row["pattern"], re.IGNORECASE), float(row["density_lb_per_gal"]), bool(row["human_confirmed"]))
        for _, row in df.iterrows()
    ]


def _pattern_dict_lookup(
    name: str, dictionary: list[tuple[re.Pattern, float, bool]]
) -> tuple[float, bool] | None:
    for pattern, lb, human_confirmed in dictionary:
        if pattern.search(name):
            return lb, human_confirmed
    return None


# Sysco and Daylight Foods both sell a lot of produce/bakery items by a
# bare piece/bunch/container COUNT in the name ("18-CT", "64-88 CT",
# "(88)") rather than a weight -- resolving these needs (a) the count
# itself, extracted from whichever of several shapes the name uses, and
# (b) a reference weight per count-unit (see count_based_food_dictionary
# / sysco_ct_food_dictionary). Tried in order: a "/"-joined chain before
# CT (e.g. "12/3-CT" = 12 packs of 3 -- ROMAINE HEARTS), a numeric range
# before CT (e.g. "64-88 CT", averaged), a single bare number before CT
# (e.g. "108-CT" -- note this also correctly ignores an unrelated
# hyphenated size-grade code earlier in the name, like "72-SZ", since
# that number isn't adjacent to "CT"), and finally a parenthetical count
# with no "CT" token at all (e.g. citrus/avocado "(88)", "(48)").
_CT_CHAIN_RE = re.compile(r"((?:\d+\s*/\s*)+\d+)\s*-?\s*CT\b", re.IGNORECASE)
_CT_RANGE_RE = re.compile(r"(\d+)\s*-\s*(\d+)\s*-?\s*CT\b", re.IGNORECASE)
_CT_BARE_RE = re.compile(r"(\d+)\s*-?\s*CT\b", re.IGNORECASE)
_CT_PAREN_RE = re.compile(r"\((\d+)\)")


def _extract_count_with_shape(name: str) -> tuple[float | None, str]:
    """Returns (value, shape). shape='chain' means the name's own "/"
    chain (e.g. "12/3-CT" = 12 bags of 3 = 36) is already a complete
    per-case count -- a separate Purchase Unit case-multiplier, if any,
    is redundant with the chain's own leading number in the large
    majority of real cases (same finding as the analogous weight-chain
    logic) and must NOT also be applied. shape='bare'/'range'/'paren'
    mean the name gives only a single number (or a size-grade range, or
    a parenthetical) with no case-multiplier of its own -- callers with a
    separate Purchase Unit "N/CS" signal must multiply it in themselves.
    Real bug this fixes: "BUN BRIOCHE MINI SLICED TURANO 18CT" (bare
    "18CT" in the name) at Purchase Unit "12/CS" was resolved using just
    the bare 18-count, silently dropping Purchase Unit's real "12"
    case-multiplier -- a 12x undercount that implied an absurd
    $41.34/lb."""
    m = _CT_CHAIN_RE.search(name)
    if m:
        total = 1.0
        for token in m.group(1).split("/"):
            total *= float(token.strip())
        return total, "chain"
    m = _CT_RANGE_RE.search(name)
    if m:
        return (float(m.group(1)) + float(m.group(2))) / 2.0, "range"
    m = _CT_BARE_RE.search(name)
    if m:
        return float(m.group(1)), "bare"
    m = _CT_PAREN_RE.search(name)
    if m:
        return float(m.group(1)), "paren"
    return None, "none"


def _extract_count(name: str) -> float | None:
    value, _shape = _extract_count_with_shape(name)
    return value


# Egg items in UCR's export state a dozen-count rather than a piece
# count ("EGG SHELL LARGE WHITE CAGE FREE 15DZ" = 15 dozen = 180 eggs;
# "SNR EGG-LARGE RETAIL 15/1DZ" = a chain, 15 packs of 1 dozen = 15 dozen
# = 180 eggs) -- "DZ" means x12 individual eggs, unlike "CT" which is
# already an individual-piece count, so this is intentionally a separate
# extractor rather than folded into _extract_count.
_DZ_CHAIN_RE = re.compile(r"((?:\d+\s*/\s*)+\d+)\s*-?\s*(?:DZ|DOZ)\b", re.IGNORECASE)
_DZ_BARE_RE = re.compile(r"(\d+)\s*-?\s*(?:DZ|DOZ)\b", re.IGNORECASE)


def _extract_dozen_count(name: str) -> float | None:
    m = _DZ_CHAIN_RE.search(name)
    if m:
        total = 1.0
        for token in m.group(1).split("/"):
            total *= float(token.strip())
        return total * 12.0
    m = _DZ_BARE_RE.search(name)
    if m:
        return float(m.group(1)) * 12.0
    return None


# Daylight Foods embeds gallon quantities in the name in three distinct
# shapes: a "/"-joined chain (e.g. "JUICE, ORANGE 4/1-GAL" = 4 sub-packs
# of 1 gal = 4 gal total), a mixed number (e.g. "MILK, WHOLE 12 1/2
# GALLONS" = 12.5 gal -- the space, not "/", separates the whole part
# from the fraction, so this is NOT the same shape as the chain above),
# or a plain bare number (e.g. "MILK, WHOLE 5 Gal Disp"). All three
# require "GAL" to be a real standalone unit token, not a substring match
# inside an unrelated word -- "GAL(?:LONS?)?\b" deliberately excludes
# "PICO DE GALLO" and "APPLE, GALA" (real false positives found in this
# data), since neither "GALLO" nor "GALA" has a word boundary right after
# a bare "GAL".
# Real bug caught via a $/lb check: the number pattern below originally
# required a leading digit before an optional decimal point (\d+(?:\.\d+)?),
# which fails to match a bare-decimal like ".5" (no leading zero) --
# "WHLFCLS MILK NON-FAT 6/.5GAL" was silently parsed as "5GAL" (dropping
# the leading "." and reading just the "5"), a 10x overcount that implied
# an absurd $0.057/lb. "(?:\d+(?:\.\d+)?|\.\d+)" also matches a
# no-leading-digit decimal.
_GAL_NUM = r"(?:\d+(?:\.\d+)?|\.\d+)"

# Non-food items that happen to use "GAL"/"LB"/etc. as a CAPACITY rating
# rather than a food quantity (trash/compost bag liners, sanitizer,
# cleaner) -- these slip past the general non-food filter (their names
# don't contain any of its keywords) but would be nonsensical to convert
# via a food-liquid density or weight assumption. Shared by every
# GAL-density resolution path (UCB and UCR).
# "PUMP" added after a real find: "SBUX CBS PLAST PUMP 3.75 ML 3/IP" is a
# pump dispenser DEVICE (confirmed by its own "Food Service Supplies"
# cost category in the raw data), not a food liquid -- its "3.75 ML"
# describes the pump mechanism's capacity, not a food volume. Word-
# bounded so it doesn't match "PUMPKIN".
# "CLEANING" added after finding "SBUX CLEANING TABLET JAR CAFIZA 3G" (an
# espresso-machine descaling tablet) wasn't caught by "CLEANER" alone.
_GAL_NON_FOOD_RE = re.compile(r"LINER|SANITIZER|CLEANER|CLEANING|\bPUMP\b|SOAP", re.IGNORECASE)
_GAL_CHAIN_RE = re.compile(rf"({_GAL_NUM})\s*/\s*({_GAL_NUM})\s*-?\s*GAL(?:LONS?)?\b", re.IGNORECASE)
_GAL_MIXED_RE = re.compile(r"(\d+)[\s-]+(\d+)\s*/\s*(\d+)\s*-?\s*GAL(?:LONS?)?\b", re.IGNORECASE)
_GAL_BARE_RE = re.compile(rf"({_GAL_NUM})\s*-?\s*GAL(?:LONS?)?\b", re.IGNORECASE)


def _gal_quantity_with_shape(name: str) -> tuple[float | None, str]:
    """Returns (value, shape). shape='chain' means the name's own number
    already fully specifies a per-case gallon quantity (a "/"-chain like
    "4/1-GAL", or a mixed number like "12 1/2 GALLONS") -- nothing else
    needed. shape='bare' means the name states only a single gallon
    figure (e.g. "0.5GAL", "1GAL") with no case-multiplier of its own --
    callers with a separate case-pack-count signal (e.g. a Purchase Unit
    "N/CS") must multiply it in themselves. Real bug this distinction
    fixes: "OIL CORN 6/1GAL" (chain, 6 gal/case already) combined with
    Purchase Unit "6/CS" would double-count the "6" if chain results were
    also multiplied by the Purchase Unit -- verified via $/lb ($2.03/lb
    using the chain alone vs. an implausible $0.34/lb double-counting)."""
    m = _GAL_MIXED_RE.search(name)
    if m:
        whole, num, den = float(m.group(1)), float(m.group(2)), float(m.group(3))
        return whole + num / den, "chain"
    m = _GAL_CHAIN_RE.search(name)
    if m:
        return float(m.group(1)) * float(m.group(2)), "chain"
    m = _GAL_BARE_RE.search(name)
    if m:
        return float(m.group(1)), "bare"
    return None, "none"


def _daylight_gal_quantity(name: str) -> float | None:
    value, _shape = _gal_quantity_with_shape(name)
    return value


# Liter/mL quantities embedded in the name, same three shapes as GAL --
# converted to gallon-equivalents so the existing gal_density_dictionary
# can be reused directly. "(?<![A-Za-z])" (no letter immediately before)
# stops "L"/"LT" from matching inside "GAL" itself (checked separately,
# before this) or other unrelated words.
_LITER_UNIT = r'(?<![A-Za-z])(?:L(?:TR|T)?|ML)\b'
_LITER_CHAIN_RE = re.compile(rf"({_GAL_NUM})\s*/\s*({_GAL_NUM})\s*-?\s*{_LITER_UNIT}", re.IGNORECASE)
_LITER_BARE_RE = re.compile(rf"({_GAL_NUM})\s*-?\s*{_LITER_UNIT}", re.IGNORECASE)
_ML_BARE_RE = re.compile(rf"({_GAL_NUM})\s*-?\s*ML\b", re.IGNORECASE)
_LITERS_PER_GALLON = 3.78541


def _liter_quantity_with_shape(name: str) -> tuple[float | None, str]:
    """Same chain/bare distinction as _gal_quantity_with_shape, but for
    liter/mL units, returned already converted to gallon-equivalents."""
    m = _LITER_CHAIN_RE.search(name)
    if m:
        is_ml = bool(re.search(r"ML\b", m.group(0), re.IGNORECASE))
        liters = float(m.group(1)) * float(m.group(2)) * (0.001 if is_ml else 1.0)
        return liters / _LITERS_PER_GALLON, "chain"
    m = _LITER_BARE_RE.search(name)
    if m:
        is_ml = bool(re.search(r"ML\b", m.group(0), re.IGNORECASE))
        liters = float(m.group(1)) * (0.001 if is_ml else 1.0)
        return liters / _LITERS_PER_GALLON, "bare"
    return None, "none"


def _load_count_based_dict(csv_path: Path) -> list[tuple[re.Pattern, float, str, bool]]:
    """Loads a {pattern, weight_group_key, reference_weight_lb, formula
    (multiply|case), ..., human_confirmed} CSV. 'multiply' means
    reference_weight_lb is a per-piece/bunch/container average, so
    per-case weight = reference_weight_lb x count (e.g. a broccolini
    bunch, a romaine heart). 'case' means reference_weight_lb is already
    a standard shipping carton's total weight for that commodity (e.g. a
    40 lb standard apple carton) -- the count/size-grade code in the name
    (e.g. "64-88 CT") describes how big the individual pieces are, not
    how much the case weighs, so it is NOT a divisor here. Real bug this
    corrects: an earlier version divided the carton weight by the count,
    which is backwards -- a standard carton weighs ~the same regardless
    of what size grade of fruit is inside (that's the point of a
    standard produce carton), so dividing produced a per-case weight
    100x too small and implied a $4,153/lb apple."""
    df = pd.read_csv(csv_path)
    return [
        (re.compile(row["pattern"], re.IGNORECASE), float(row["reference_weight_lb"]), row["formula"], bool(row["human_confirmed"]))
        for _, row in df.iterrows()
    ]


def _count_based_dict_lookup(
    name: str, dictionary: list[tuple[re.Pattern, float, str, bool]], count_fn=_extract_count
) -> tuple[float, bool] | None:
    for pattern, ref_lb, formula, human_confirmed in dictionary:
        if pattern.search(name):
            if formula == "case":
                return ref_lb, human_confirmed
            count = count_fn(name)
            if count is None:
                return None
            return ref_lb * count, human_confirmed
    return None


# Bordenaves' "Pack size" is the unit the Price/Quantity columns are priced
# in -- "ea" (Quantity is already an individual-item count), "dz" (Quantity
# is counted in DOZENS, verified: e.g. "Sweet Small Round Roll Dz" Price
# $5.75/dz x Quantity 229 = Extended $1316.75, and 229 dozen x 12 x the
# dictionary's 1.5 oz/roll gives a plausible per-roll wholesale price), or
# "pk" (a pack size stated in the name itself, e.g. "Sweet 4.5'' Hams
# 6-Pk" -- packs of 6).
_PACK_N_RE = re.compile(r"(\d+)\s*-?\s*pk\b", re.IGNORECASE)
# Bimbo Bakery's "Pack size" for count-based items ("6Ct", "12Ct").
_PACK_CT_RE = re.compile(r"(\d+)\s*ct\b", re.IGNORECASE)


def _bordenaves_pack_multiplier(pack_size, name: str) -> int | None:
    if pd.isna(pack_size):
        return None
    p = str(pack_size).strip().lower()
    if p == "ea":
        return 1
    if p == "dz":
        return 12
    if p == "pk":
        m = _PACK_N_RE.search(str(name))
        return int(m.group(1)) if m else None
    return None


# Ben & Jerry's "Pack size" is always "N units" (individual items per
# case, e.g. "8 units" for pints, "12 units" for 4 oz cups) -- Quantity
# sold is the case count (verified: Price x Quantity = Extended Price
# holds). Some items already state their own per-item size explicitly
# ("BJ CHOC CHIP DOUGH CUP 4oz", "GH MAGNUM ALMOND 3.04oz") -- prefer that
# real number over the Tier 3 dictionary estimate when it's present.
_BJ_PACK_UNITS_RE = re.compile(r"(\d+)\s*units?\b", re.IGNORECASE)
_EACH_OZ_RE = re.compile(r"(\d+(?:\.\d+)?)\s*oz\b", re.IGNORECASE)


def _bj_pack_units(pack_size) -> int | None:
    if pd.isna(pack_size):
        return None
    m = _BJ_PACK_UNITS_RE.search(str(pack_size))
    return int(m.group(1)) if m else None


def _explicit_each_oz_lb(name: str) -> float | None:
    m = _EACH_OZ_RE.search(name)
    return float(m.group(1)) / 16.0 if m else None


# Confirmed with project owner: a single known raw-data typo. Kikka
# Sushi's "GARDEN VEGETABLE SALAD ROLL" Extended Price ($93,779.78)
# doesn't match Price x Quantity ($5.62 x 1,669 = $9,379.78, a ~9.998x
# discrepancy -- almost certainly a digit-transposition typo during data
# entry, not a genuine price). Every other Kikka Sushi row (and the
# overwhelming majority of rows across this whole file) satisfies Price x
# Quantity == Extended Price exactly, so the reconstructed value is
# trusted here. Deliberately scoped to this one confirmed (distributor,
# raw_name) pair, not a general "always trust Price x Quantity" rule --
# that broader rule was never audited across the rest of the dataset.
_UCB_KNOWN_PRICE_TYPOS = {
    ("Kikka Sushi", "GARDEN VEGETABLE SALAD ROLL"): 9379.78,
}


def load_ucb(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    out = pd.DataFrame(index=df.index)
    out["raw_name"] = _clean_str(df["Product Name or Description "])
    out["vendor"] = df["Distributor"]
    out["brand"] = _clean_str(df["Brand"])
    out["total_price"] = df[" Extended Price "].apply(_parse_currency)
    for (dist, name), corrected in _UCB_KNOWN_PRICE_TYPOS.items():
        mask = (df["Distributor"] == dist) & (out["raw_name"] == name)
        out.loc[mask, "total_price"] = corrected

    qty_str = df[" Quantity sold "].astype(str).str.replace(",", "").str.strip()
    qty_num = pd.to_numeric(qty_str, errors="coerce")

    m = qty_str.str.extract(r"([\d.]+)\s*([A-Za-z]+)")
    num = pd.to_numeric(m[0], errors="coerce")
    unit = m[1].str.lower()
    weight = pd.Series(float("nan"), index=df.index, dtype="float64")
    weight[unit == "lb"] = num[unit == "lb"]
    weight[unit == "oz"] = num[unit == "oz"] / 16.0
    weight[unit == "kg"] = num[unit == "kg"] * 2.20462
    weight_source = pd.Series("unresolved", index=df.index)
    weight_source[weight.notna()] = "reported"

    distributor = df["Distributor"]

    # Sysco / Pacific Seafood / Bimbo Bakery: Pack size (per-case weight) x
    # Quantity sold (case count). Bimbo Bakery's Pack size is sometimes an
    # explicit unit this same parser already handles ("16oz", "24oz",
    # "32oz" for sliced bread) and sometimes a bare count ("6Ct", "12Ct")
    # that this parser correctly declines to match (no recognized weight
    # unit) -- those fall through to the Bimbo-specific Tier 3 lookup below.
    is_pack_size_dist = distributor.isin(["Sysco", "Pacific Seafood", "Bimbo Bakery"])
    per_case = df["Pack size"].apply(_sysco_pack_size_to_lb)
    tier2 = per_case * qty_num
    needs = is_pack_size_dist & weight.isna() & tier2.notna()
    weight[needs] = tier2[needs]
    weight_source[needs] = "computed_tier2"

    # Allen Brothers / Cream Co: Quantity sold IS the weight in lbs already.
    is_direct_weight_dist = distributor.isin(["Allen Brothers", "Cream Co"])
    needs = is_direct_weight_dist & weight.isna() & qty_num.notna()
    weight[needs] = qty_num[needs]
    weight_source[needs] = "computed_tier2"

    # Daylight Foods, Peets, JFC: per-case weight embedded in the product
    # name rather than a structured column.
    is_name_embedded = distributor.isin(["Daylight Foods", "Peets", "JFC"])
    per_case_name = out["raw_name"].astype(str).apply(_name_embedded_per_case_lb)
    tier2_name = per_case_name * qty_num
    needs = is_name_embedded & weight.isna() & tier2_name.notna()
    weight[needs] = tier2_name[needs]
    weight_source[needs] = "computed_tier2"

    # Bordenaves: exact-title match against a project-owner-confirmed Tier
    # 3 reference table, x a pack-size multiplier (ea=1/dz=12/pk=N).
    is_bordenaves = distributor == "Bordanaves"
    if is_bordenaves.any():
        bordenaves_dict = _load_exact_title_weight_dict(WEIGHT_DICTIONARIES_DIR / "bordenaves_weight_dictionary_by_title.csv")
        for idx in df.index[is_bordenaves & weight.isna()]:
            name = out.at[idx, "raw_name"]
            if pd.isna(name) or name not in bordenaves_dict:
                continue
            each_lb, human_confirmed = bordenaves_dict[name]
            mult = _bordenaves_pack_multiplier(df.at[idx, "Pack size"], name)
            qty = qty_num.at[idx]
            if mult is None or pd.isna(qty):
                continue
            weight.at[idx] = each_lb * mult * qty
            weight_source.at[idx] = "reference_table_tier3"

    # Ben & Jerry's: Pack size "N units" (items per case) x Quantity sold
    # (case count) x per-item weight -- either an explicit oz already
    # stated in the name (computed_tier2, not an assumption) or, failing
    # that, a Tier 3 reference-table estimate (e.g. a pint of ice cream).
    is_bj = distributor == "Ben & Jerry's"
    if is_bj.any():
        bj_dict = _load_pattern_weight_dict(WEIGHT_DICTIONARIES_DIR / "benjerrys_weight_dictionary.csv")
        for idx in df.index[is_bj & weight.isna()]:
            name = out.at[idx, "raw_name"]
            units = _bj_pack_units(df.at[idx, "Pack size"])
            qty = qty_num.at[idx]
            if pd.isna(name) or units is None or pd.isna(qty):
                continue
            explicit_lb = _explicit_each_oz_lb(name)
            if explicit_lb is not None:
                weight.at[idx] = explicit_lb * units * qty
                weight_source.at[idx] = "computed_tier2"
                continue
            match = _pattern_dict_lookup(name, bj_dict)
            if match is None:
                continue
            each_lb, human_confirmed = match
            weight.at[idx] = each_lb * units * qty
            weight_source.at[idx] = "reference_table_tier3"

    # Kikka Sushi: no Pack size column and no per-case multiplier -- Price
    # is already per-roll, so Quantity sold is the individual-roll count
    # directly. Tier 3 reference-table lookup by roll type.
    is_kikka = distributor == "Kikka Sushi"
    if is_kikka.any():
        kikka_dict = _load_pattern_weight_dict(WEIGHT_DICTIONARIES_DIR / "kikkasushi_weight_dictionary.csv")
        for idx in df.index[is_kikka & weight.isna()]:
            name = out.at[idx, "raw_name"]
            qty = qty_num.at[idx]
            if pd.isna(name) or pd.isna(qty):
                continue
            match = _pattern_dict_lookup(name, kikka_dict)
            if match is None:
                continue
            each_lb, human_confirmed = match
            weight.at[idx] = each_lb * qty
            weight_source.at[idx] = "reference_table_tier3"

    # Bimbo Bakery: remaining rows have a bare-count Pack size ("6Ct",
    # "12Ct") -- exact-title match against a Tier 3 reference table, x the
    # Ct count x Quantity sold (case count).
    is_bimbo = distributor == "Bimbo Bakery"
    if is_bimbo.any():
        bimbo_dict = _load_exact_title_weight_dict(WEIGHT_DICTIONARIES_DIR / "bimbobakery_weight_dictionary_by_title.csv")
        for idx in df.index[is_bimbo & weight.isna()]:
            name = out.at[idx, "raw_name"]
            if pd.isna(name) or name not in bimbo_dict:
                continue
            each_lb, human_confirmed = bimbo_dict[name]
            ct_match = _PACK_CT_RE.search(str(df.at[idx, "Pack size"]))
            qty = qty_num.at[idx]
            if ct_match is None or pd.isna(qty):
                continue
            weight.at[idx] = each_lb * int(ct_match.group(1)) * qty
            weight_source.at[idx] = "reference_table_tier3"

    # Espostos: Pack size is always "1" (Quantity sold is already an
    # individual-item count) -- Tier 3 reference-table lookup by item type.
    is_espostos = distributor == "Espostos"
    if is_espostos.any():
        espostos_dict = _load_pattern_weight_dict(WEIGHT_DICTIONARIES_DIR / "espostos_weight_dictionary.csv")
        for idx in df.index[is_espostos & weight.isna()]:
            name = out.at[idx, "raw_name"]
            qty = qty_num.at[idx]
            if pd.isna(name) or pd.isna(qty):
                continue
            match = _pattern_dict_lookup(name, espostos_dict)
            if match is None:
                continue
            each_lb, human_confirmed = match
            weight.at[idx] = each_lb * qty
            weight_source.at[idx] = "reference_table_tier3"

    # City Baking: Pack size is a bare integer -- items per case (verified:
    # Pack size 1 for whole bread loaves, where Quantity sold is
    # unambiguously an individual-loaf count, confirms this is a genuine
    # per-case multiplier, not a case-weight or a redundant label). Tier 3
    # reference-table lookup by item type x Pack size x Quantity sold.
    is_city_baking = distributor == "City Baking"
    if is_city_baking.any():
        city_baking_dict = _load_pattern_weight_dict(WEIGHT_DICTIONARIES_DIR / "citybaking_weight_dictionary.csv")
        for idx in df.index[is_city_baking & weight.isna()]:
            name = out.at[idx, "raw_name"]
            qty = qty_num.at[idx]
            pack_size = df.at[idx, "Pack size"]
            if pd.isna(name) or pd.isna(qty) or pd.isna(pack_size):
                continue
            try:
                mult = int(pack_size)
            except (TypeError, ValueError):
                continue
            match = _pattern_dict_lookup(name, city_baking_dict)
            if match is None:
                continue
            each_lb, human_confirmed = match
            weight.at[idx] = each_lb * mult * qty
            weight_source.at[idx] = "reference_table_tier3"

    # Sysco "#10" can: a well-established foodservice standard can size,
    # not a case-pack count -- Quantity sold is the number of cans
    # directly (verified: e.g. "SAUCE MARINARA CA" implies $7.53/lb,
    # "BEAN BLACK" implies $6.32/lb at the dictionary's ~6.5 lb/can
    # estimate -- both plausible institutional canned-food prices).
    is_number10_can = (distributor == "Sysco") & (df["Pack size"].astype(str).str.strip().str.upper() == "#10")
    if is_number10_can.any():
        can_dict = _load_pattern_weight_dict(WEIGHT_DICTIONARIES_DIR / "sysco_number10_can_dictionary.csv")
        for idx in df.index[is_number10_can & weight.isna()]:
            name = out.at[idx, "raw_name"]
            qty = qty_num.at[idx]
            if pd.isna(name) or pd.isna(qty):
                continue
            match = _pattern_dict_lookup(name, can_dict)
            if match is None:
                continue
            each_lb, human_confirmed = match
            weight.at[idx] = each_lb * qty
            weight_source.at[idx] = "reference_table_tier3"

    # GAL-based Pack sizes (Sysco) / name-embedded "GAL" (Daylight Foods):
    # a density assumption per product-type category (see
    # gal_density_dictionary), x the gallon quantity, x Quantity sold.
    # Explicitly excludes non-food items that happen to use "GAL" as a
    # CAPACITY rating rather than a food liquid volume (trash/compost bag
    # liners, sanitizer, cleaner) -- these slipped past the general
    # non-food filter (their names don't contain any of its keywords) but
    # would be nonsensical to convert via a food-liquid density.
    gal_dict = _load_gal_density_dict(WEIGHT_DICTIONARIES_DIR / "gal_density_dictionary.csv")

    sysco_pack_gal = df["Pack size"].astype(str).str.extract(r"([\d.]+)\s*GAL", flags=re.IGNORECASE)[0]
    sysco_gal_qty = pd.to_numeric(sysco_pack_gal, errors="coerce")
    is_sysco_gal = (distributor == "Sysco") & sysco_gal_qty.notna()
    if is_sysco_gal.any():
        for idx in df.index[is_sysco_gal & weight.isna()]:
            name = out.at[idx, "raw_name"]
            qty = qty_num.at[idx]
            if pd.isna(name) or pd.isna(qty) or _GAL_NON_FOOD_RE.search(name):
                continue
            match = _pattern_dict_lookup(name, gal_dict)
            if match is None:
                continue
            density_lb_per_gal, human_confirmed = match
            weight.at[idx] = density_lb_per_gal * sysco_gal_qty.at[idx] * qty
            weight_source.at[idx] = "reference_table_tier3"

    is_daylight = distributor == "Daylight Foods"
    if is_daylight.any():
        for idx in df.index[is_daylight & weight.isna()]:
            name = out.at[idx, "raw_name"]
            qty = qty_num.at[idx]
            if pd.isna(name) or pd.isna(qty) or _GAL_NON_FOOD_RE.search(name):
                continue
            gal_qty = _daylight_gal_quantity(name)
            if gal_qty is None:
                continue
            match = _pattern_dict_lookup(name, gal_dict)
            if match is None:
                continue
            density_lb_per_gal, human_confirmed = match
            weight.at[idx] = density_lb_per_gal * gal_qty * qty
            weight_source.at[idx] = "reference_table_tier3"

    # Count-based produce/bakery/dairy items sold by a bare piece/bunch/
    # container count rather than a weight (see count_based_food_dictionary
    # and _extract_count). Sysco: Pack size ends in "CT" -- prefers a real
    # explicit oz stated in the name (computed_tier2, not a guess) over
    # any dictionary estimate, same "prefer the real number" precedent as
    # Ben & Jerry's. Falls through to the Sysco-specific prepared-food
    # dictionary for items count_based_food_dictionary doesn't cover
    # (eggs, springrolls, tortillas, buns, pastries, energy bars, etc.).
    count_dict = _load_count_based_dict(WEIGHT_DICTIONARIES_DIR / "count_based_food_dictionary.csv")
    sysco_ct_dict = _load_count_based_dict(WEIGHT_DICTIONARIES_DIR / "sysco_ct_food_dictionary.csv")
    is_sysco_ct = (distributor == "Sysco") & (df["Pack size"].astype(str).str.strip().str.upper().str.endswith("CT"))
    if is_sysco_ct.any():
        for idx in df.index[is_sysco_ct & weight.isna()]:
            name = out.at[idx, "raw_name"]
            qty = qty_num.at[idx]
            pack_size = df.at[idx, "Pack size"]
            if pd.isna(name) or pd.isna(qty) or pd.isna(pack_size):
                continue
            ct_match = _PACK_CT_RE.search(str(pack_size))
            if ct_match is None:
                continue
            case_count = int(ct_match.group(1))
            explicit_lb = _explicit_each_oz_lb(name)
            if explicit_lb is not None:
                weight.at[idx] = explicit_lb * case_count * qty
                weight_source.at[idx] = "computed_tier2"
                continue
            # A "1 CT" Pack size on a Tier 3 dictionary match (no explicit
            # oz stated) is excluded here -- real bug caught this way:
            # "EGG HARD COOKED CAGE FREE" at "1 CT" implied $137.75/lb
            # (1.75 oz treated as the WHOLE case) when combined with the
            # dictionary's per-egg estimate, which is calibrated against
            # this data's genuine multi-egg cases ("12 CT"). Every food
            # item resolved by these two dictionaries is bought in bulk,
            # multi-count cases in this export -- a bare "1 CT" here is
            # either a non-food dispenser item or a data-entry convention
            # that doesn't mean what it says, not a real single-item case.
            if case_count == 1:
                continue
            sysco_count_fn = lambda n: case_count  # noqa: E731 -- Sysco's count comes from the "Pack size" column ("12 CT"), not the name
            match = _count_based_dict_lookup(name, count_dict, sysco_count_fn) or _count_based_dict_lookup(name, sysco_ct_dict, sysco_count_fn)
            if match is None:
                continue
            per_case_lb, human_confirmed = match
            weight.at[idx] = per_case_lb * qty
            weight_source.at[idx] = "reference_table_tier3"

    # Daylight Foods: count embedded directly in the name (no Pack size
    # column at all in this export) -- e.g. "AVOCADO, HASS (48) #2 2-LYR",
    # "BROCCOLINI, 18-CT", "ROMAINE, HEARTS 12/3-CT ANDY BOY".
    is_daylight_ct = distributor == "Daylight Foods"
    if is_daylight_ct.any():
        for idx in df.index[is_daylight_ct & weight.isna()]:
            name = out.at[idx, "raw_name"]
            qty = qty_num.at[idx]
            if pd.isna(name) or pd.isna(qty):
                continue
            match = _count_based_dict_lookup(name, count_dict)
            if match is None:
                continue
            per_case_lb, human_confirmed = match
            weight.at[idx] = per_case_lb * qty
            weight_source.at[idx] = "reference_table_tier3"

    out["total_weight_lbs"] = weight
    out["weight_source"] = weight_source

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

    # Real bug found and fixed this session: the previous version computed
    # total_weight = Item Weight x Total Quantity Purchased for every row,
    # uniformly across UOMs -- silently dropping "Pack Size" entirely. Item
    # Weight == Case Net Weight in every row (verified, still true), but
    # that does NOT mean Item Weight is a per-case total -- it's a PER-
    # PIECE weight, and "Pack Size" (pieces per case) has to be multiplied
    # in separately. Verified directly against real per-piece weights this
    # export itself corroborates via the product name: "APPETIZER
    # POTSTICKER CHICKEN LEMONGRASS" Item Weight 0.04875 lb (0.78 oz,
    # exactly a standard potsticker) x Pack Size 150; "SPICE SAFFRON
    # SPANISH 1 OZ" Item Weight 0.0625 lb (= 1 oz, matching the name) x
    # Pack Size 1. Systematic $/lb check across every non-LB UOM confirms
    # this: e.g. "CS" rows had a 13.1% rate of implying >$100/lb before this
    # fix (one single row, "CANDY CHOCOLATE WHITE CURVED GREEN PETALS DUO
    # 160 PC", implied $18,019/lb) vs. 0.4% after -- and whole-dataset
    # median dropped from a implausible $88.90/lb to a plausible $3.81/lb.
    #
    # Bare "LB" is the ONE exception -- there, "Total Quantity Purchased"
    # is ALREADY the total weight directly (verified: "BEEF BRISKET BNLS CH"
    # Item Weight 14.0, Qty 574.70 -- multiplying gives an absurd $0.32/lb
    # for beef; using Qty alone gives a plausible $4.50/lb). Multiplying
    # Item Weight into "LB" rows was the second half of the same bug, and
    # it was hiding in the *most*-trusted tier ('reported') the whole time.
    # "5LB" does NOT get this same exception -- unlike "LB", its Item
    # Weight is a real per-bag weight (~5 lb, matching the "5 LB" bag
    # named in the product) that must still be multiplied by Pack Size and
    # Qty, same as every other non-"LB" UOM; it happened to look identical
    # to "LB" before this fix only because every "5LB" row's Pack Size is 1
    # (verified) -- now correctly tagged 'computed_tier2' like the rest of
    # this bucket, not 'reported'.
    #
    # A handful of residual outliers remain even after this fix (e.g. a
    # burrito implying $250/lb, chewing gum implying $230/lb) where the
    # source file's own Item Weight looks implausibly small for the stated
    # product -- likely a genuine data-entry error in UCD's own export, not
    # something this pipeline can correct without guessing. Left as-is
    # rather than silently overridden, same "don't guess" principle as
    # everywhere else in this project.
    weight_per_unit = pd.to_numeric(df["Item Weight (e.g.5)"], errors="coerce")
    pack_size = pd.to_numeric(df["Pack Size (e.g.4)"], errors="coerce")
    qty = pd.to_numeric(df["Total Quantity Purchased"], errors="coerce")
    uom = df["Item UOM (e.g.LB)"].astype(str).str.strip().str.upper()
    is_lb = uom == "LB"
    out["total_weight_lbs"] = qty.where(is_lb, weight_per_unit * pack_size * qty)
    out["weight_source"] = is_lb.map({True: "reported", False: "computed_tier2"})

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

    # Real bug found and fixed this session: "weight_lb" == 0 is a
    # missing-data placeholder in this export (real products with real
    # spend, e.g. "SAUCE WHITE CHOCOLATE 4CT" $9,197.22, "MONIN SYRUP
    # VANILLA 4/1L" $21,590.18), not a legitimate "weighs nothing" claim --
    # no real food product weighs 0 lb. `weight.notna()` treated 0.0 as a
    # valid reported value (0.0 is not NaN), silently tagging 611 rows
    # ($3.9M of spend) 'reported' with a weight of exactly zero -- the
    # single worst place for this to hide, since 'reported' is supposed to
    # be the most-trusted tier. Fixed to require weight != 0 -- NOT
    # weight > 0: UCD_H has no negative-weight return/credit rows
    # (verified), but the sibling UCLA_H/UCSD_H exports do, and those are
    # legitimate resolved data (a real returned quantity), not a second
    # instance of this same placeholder bug.
    weight = pd.to_numeric(df["weight_lb"], errors="coerce")
    is_reported = weight.notna() & (weight != 0)
    out["total_weight_lbs"] = weight.where(is_reported)
    out["weight_source"] = is_reported.map({True: "reported", False: "unresolved"})

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

    # Same bug as UCD_H/UCSD_H (fixed this session): "weight" == 0 is a
    # missing-data placeholder, not a real "weighs nothing" claim --
    # weight.notna() treated 0.0 as valid, silently tagging $6.3M of real
    # spend (43.5% of this file) 'reported' with a weight of exactly zero.
    # Requires weight != 0, not weight > 0 -- this file has 4 real negative-
    # weight return/credit rows that are legitimate resolved data, a
    # different thing from the exactly-zero placeholder.
    weight = pd.to_numeric(df["weight"], errors="coerce")
    is_reported = weight.notna() & (weight != 0)
    out["total_weight_lbs"] = weight.where(is_reported)
    out["weight_source"] = is_reported.map({True: "reported", False: "unresolved"})

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


_UCR_DOLLAR_ROLLUP_PURCHASE_UNITS = {"DOLR", "DOLR*", "DOLAR", "DOLLR*", "DLR"}


def load_ucr(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    # Category rollup/subtotal rows (Name='PRODUCE', Vendor='Not Found', etc.)
    # use a dollar-total placeholder Purchase Unit rather than a real unit --
    # confirmed with project owner these aren't line items. Real bug caught
    # here: the original filter only excluded the exact spelling "DOLR", but
    # 5 rows ($173,744 total, e.g. "POULTRY" $86,048.87, "TOWELS/GRILL
    # PADS/MOP HEADS/SPONGE" $63,529.25) use spelling variants ("DOLR*",
    # "DOLAR", "DOLLR*", "DLR") that slipped through uncaught -- confirmed
    # these are the same kind of rollup row (each one's "Units" value is
    # within a few dollars of its own "Total Spend", the signature of a
    # dollar-denominated placeholder, not a physical unit count).
    df = df[~df["Purchase Unit"].isin(_UCR_DOLLAR_ROLLUP_PURCHASE_UNITS)].copy()
    # The file also has a trailing grand-total summary block after the real
    # line items (Name='Total Spend'/'Sustainable Spend'/'Plant-Based %'/
    # etc., $10.2M+ in this export) -- a DIFFERENT rollup convention than
    # the DOLR rows above (blank Purchase Unit, not 'DOLR'), so the filter
    # above doesn't catch it. Every real line item has a populated 'Item'
    # code (verified: exactly 11 rows in this file have a blank Item, all
    # in that trailing block, 0 false positives among real line items) --
    # found via a real bug where "Total Spend" ($10.2M) and "Sustainable
    # Spend" ($652K) were being counted as individual products, materially
    # inflating both total and "sustainable" spend figures.
    df = df[df["Item"].notna()].copy()

    out = pd.DataFrame(index=df.index)
    out["raw_name"] = _clean_str(df["Name"])
    out["vendor"] = df["Vendor"]
    out["brand"] = pd.NA  # no separate brand/manufacturer column in this export
    out["total_price"] = df[" Total Spend "].apply(_parse_currency)

    units_num = pd.to_numeric(df["Units"].astype(str).str.replace(",", "").str.strip(), errors="coerce")
    # Purchase Unit values like "20#CS" / "5#BG" embed lbs-per-unit; "12/CS"
    # (count per case, not weight) and similar are left unresolved.
    # A trailing "*" shows up on many Purchase Unit values (its meaning
    # isn't otherwise documented in this export) but doesn't change the
    # underlying unit -- stripped up front so every pattern below doesn't
    # have to account for it separately.
    pu = df["Purchase Unit"].astype(str).str.strip().str.upper().str.rstrip("*")
    m = pu.str.extract(r"^(\d+(?:\.\d+)?)#")
    lb_per_unit = pd.to_numeric(m[0], errors="coerce")
    weight = lb_per_unit * units_num
    weight_source = weight.notna().map({True: "computed_tier2", False: "unresolved"})

    # Bare "LB" / "NLB" purchase units weren't caught by the "#" pattern
    # above at all. Verified against real data with a $/lb sanity check
    # (comparing against the "#"-derived rows, which run ~$4-10/lb for
    # meat/produce): bare "LB" means 'Units' IS the total weight directly
    # (e.g. "CHICKEN THIGH MT B/S JUL PK 5#AVG" has Units=19,711.40 and
    # Spend=$79,691.61 -> $4.04/lb, a plausible per-pound price, not a
    # per-unit count) -- that's a direct figure, so tagged 'reported', not
    # computed. "NLB" (e.g. "SBUX 5LB PIKE PLACE ROAST") is the same
    # per-unit-times-count pattern as the "#" case above (Units=80 * 5lb =
    # 400lb, Spend=$3,907.40 -> $9.77/lb, plausible for coffee) --
    # 'computed_tier2' like the rest of this function. Tolerates an
    # optional space between the number and "LB" (e.g. "20 LB") -- found
    # via a real Sysco row ("BLUEBERRY IQF 20LB") that the original
    # no-space-only regex silently missed.
    is_bare_lb = pu == "LB"
    weight[is_bare_lb] = units_num[is_bare_lb]
    weight_source[is_bare_lb] = "reported"

    m_nlb = pu.str.extract(r"^(\d+(?:\.\d+)?)\s*LB$")
    lb_per_unit_nlb = pd.to_numeric(m_nlb[0], errors="coerce")
    needs_nlb = weight.isna() & lb_per_unit_nlb.notna()
    weight[needs_nlb] = (lb_per_unit_nlb * units_num)[needs_nlb]
    weight_source[needs_nlb] = "computed_tier2"

    # Sysco: the remaining Purchase Unit shapes ("CASE", "N/CS", "EACH",
    # "JAR", "BAG", etc.) are themselves pack-count descriptors, and the
    # actual per-case size is often embedded in the product name instead
    # (same underlying idea as UCB's Daylight Foods/Peets/JFC). But unlike
    # those UCB distributors, UC Riverside's Sysco Purchase Unit column
    # sometimes carries a REAL case-pack multiplier of its own ("30/CS" =
    # 30 individual units per case) that must be combined with the name's
    # size, not ignored -- verified two distinct shapes against real data:
    #   - name has its own full chain (e.g. "6/5LB" = 6 sub-packs of 5 lb):
    #     the chain alone already IS the per-case weight. Checked against
    #     127 real chain-shaped names: the Purchase Unit's leading number,
    #     when present, is redundant with the chain's own leading token in
    #     93% of cases (e.g. Purchase Unit "4/CS" + name "4/10#"), so it's
    #     not reapplied -- doing so would 4x-inflate the majority case. The
    #     ~7% where they disagree (mostly count-grading text like "100-200
    #     CT" being mistaken for a second case-multiplier) is a small,
    #     genuinely ambiguous minority left as-is rather than guessed at.
    #   - name has only a bare per-ITEM weight (e.g. "1LB", "5LB", no
    #     chain): this does NOT already include a case multiplier, so
    #     Purchase Unit's leading number (if "N/CS") or implicit 1 (if a
    #     bare unit-of-sale word like "CASE"/"EACH"/"JAR"/"BAG"/"BOX") must
    #     be multiplied in. Real bug caught this way: "BUTTER SOLID USDA AA
    #     UNSLTD 1LB" with Purchase Unit "30/CS" was implying $100/lb
    #     (152 lb total, ignoring the 30x case multiplier) before this fix
    #     -- 30 x 1 lb x 152 cases = 4,560 lb gives a plausible $3.36/lb.
    #     Similarly "PASTE CHILI KOREAN GOCHUJANG 2.2#" with Purchase Unit
    #     "JAR" (bare, no leading number, multiplier 1): 2.2 lb x 410 jars
    #     = 902 lb, $4,925.68 spend -> $5.46/lb, plausible for chili paste.
    # This shape (name-embedded weight combined with a Purchase Unit
    # case-pack multiplier) was originally verified against Sysco rows
    # only, but the "Purchase Unit"/"Units" columns are a FILE-WIDE
    # convention in this export (one shared report format, not a
    # per-distributor combined sheet like UCB's) -- confirmed by checking
    # the same shapes against Pepsi, Trepco/Coremark, The Berry Man,
    # Sunrise Produce, UNFI, and Naked Juice with $/lb sanity checks
    # (e.g. "AQUAFINA ALUMINUM 16OZ" 24/CS implies $1.13/lb bottled
    # water; "NAKED MIGHTY MANGO 15.2oz" EACH implies $2.58/lb juice --
    # both plausible), so this logic now applies to every vendor, not
    # just Sysco.
    ncs_case_mult = pd.to_numeric(pu.str.extract(r"^(\d+(?:\.\d+)?)\s*/\s*CS$")[0], errors="coerce")
    pu_case_mult = ncs_case_mult.copy()
    # "TRY"/"TRAY" verified only against real bare-LB rows here (e.g. "SNR
    # SALSA PICO DE GALLO FRESH 5LB TRAY" implies a plausible $3.44/lb) --
    # every other real TRY/TRAY row in this file states an oz, not a
    # bare LB, so isn't affected by this addition either way (that path
    # goes through the separate, ceiling-checked explicit-oz logic below,
    # which doesn't consult this multiplier for non-numeric Purchase
    # Units like TRY/TRAY).
    pu_case_mult[pu.isin(["CASE", "EACH", "JAR", "BAG", "BOX", "TUB", "PAIL", "BOTL", "ROLL", "TRY", "TRAY"])] = 1.0

    name_shape = out["raw_name"].astype(str).apply(_name_embedded_weight_with_shape)
    name_value = name_shape.apply(lambda t: t[0])
    shape = name_shape.apply(lambda t: t[1])

    is_chain = shape == "chain"
    chain_weight = name_value.where(is_chain)
    chain_weight[chain_weight < _MIN_PLAUSIBLE_CASE_LB] = pd.NA
    tier2_chain = chain_weight * units_num
    needs_chain = weight.isna() & tier2_chain.notna()
    weight[needs_chain] = tier2_chain[needs_chain]
    weight_source[needs_chain] = "computed_tier2"

    is_bare = shape == "bare"
    bare_per_case = (name_value.where(is_bare) * pu_case_mult)
    bare_per_case[bare_per_case < _MIN_PLAUSIBLE_CASE_LB] = pd.NA
    tier2_bare = bare_per_case * units_num
    needs_bare = weight.isna() & tier2_bare.notna()
    weight[needs_bare] = tier2_bare[needs_bare]
    weight_source[needs_bare] = "computed_tier2"

    # A bare individual-item oz with no chain (e.g. "NAKED MIGHTY MANGO
    # 15.2oz") isn't caught by the "bare" shape above (that shape is LB/#
    # only, calibrated for Daylight/Peets/JFC's PER-CASE bare weights in
    # UCB's export -- reused here for UCR too, so not widened to OZ there
    # to avoid changing already-verified behavior). Handled as a separate
    # per-ITEM oz path instead, same "prefer the real stated number"
    # precedent as elsewhere in this project.
    #
    # Restricted to shape=='none' (no chain/bare-LB/# match at all) --
    # NOT applied when the name already has its own embedded chain that
    # was rejected by the plausibility floor above (e.g. "KODIAK CAKES
    # PORATIN CRUNCH 6/1.59OZ": the chain gives 6x1.59oz=0.6 lb/case,
    # correctly rejected as too small, but re-parsing just the bare
    # "1.59" and multiplying by Purchase Unit's case count double-counts
    # against the name's own "6x" and understates weight ~6x).
    #
    # Purchase Unit "EACH"/"EACH*" genuinely means one item per unit here
    # (verified: "NAKED MIGHTY MANGO 15.2oz" implies $2.58/lb, plausible
    # for juice). "N/CS" is trusted too when the resulting price is
    # plausible, but real bugs found this needs care: small
    # individual-serving items (seaweed snacks, jerky, dessert bars,
    # spices/extracts in small jars) frequently bundle far more than "N"
    # per case -- e.g. "WPD SEAWEED...0.35OZ" at "12/CS" implied
    # $87/lb, "CHICKEN BREAST B/S 5oz CVP HALAL" at "4/CS" implied
    # $72/lb, both clearly wrong. A genuine gap exists in the resulting
    # $/lb distribution across all "N/CS" + explicit-oz rows in this file
    # (smooth up to ~$20/lb, then jumps to $26-113/lb with nothing
    # between) -- capped at $20/lb as a data-driven ceiling, same
    # "verify then trust" discipline as the rest of this project; rows
    # above it are left unresolved rather than committed on a guess.
    _UCR_EXPLICIT_OZ_MAX_DOLLAR_PER_LB = 20.0
    is_each_unit = pu.isin(["EACH", "EACH*"])
    is_none_shape = shape == "none"
    explicit_oz_lb = out["raw_name"].astype(str).apply(_explicit_each_oz_lb)

    tier2_explicit_oz_each = explicit_oz_lb.where(is_each_unit) * units_num
    needs_explicit_oz_each = weight.isna() & tier2_explicit_oz_each.notna()
    weight[needs_explicit_oz_each] = tier2_explicit_oz_each[needs_explicit_oz_each]
    weight_source[needs_explicit_oz_each] = "computed_tier2"

    ncs_per_case = explicit_oz_lb.where(is_none_shape) * ncs_case_mult
    ncs_per_case[ncs_per_case < _MIN_PLAUSIBLE_CASE_LB] = pd.NA
    tier2_explicit_oz_ncs = ncs_per_case * units_num
    implied_dpl = out["total_price"] / tier2_explicit_oz_ncs
    tier2_explicit_oz_ncs[implied_dpl > _UCR_EXPLICIT_OZ_MAX_DOLLAR_PER_LB] = pd.NA
    needs_explicit_oz_ncs = weight.isna() & tier2_explicit_oz_ncs.notna()
    weight[needs_explicit_oz_ncs] = tier2_explicit_oz_ncs[needs_explicit_oz_ncs]
    weight_source[needs_explicit_oz_ncs] = "computed_tier2"

    # A bare "NCT" Purchase Unit (e.g. "8CT", "6CT" -- distinct from the
    # numeric "N/CS" case above) is itself a real per-case count when
    # combined with an explicit per-item oz in the name -- verified against
    # 3 LE CHEF pastry SKUs sharing this exact shape ("LE CHEF SPINACH &
    # KALE CROISSANT 3.7OZ" at "8CT", "CROISSANT JALAPENO CRM CHS 3.7OZ LE
    # CHEF" at "8CT", "LE CHEF ALMOND CROISSANT 4OZ" at "6CT"): all three
    # imply $6.82-7.02/lb, tightly consistent, not a guess.
    #
    # Bare "TRY"/"TRAY" (no leading number) combined with an explicit oz is
    # a DIFFERENT case -- elsewhere in this file (produce sold by total
    # case weight, e.g. "SNR SALSA PICO DE GALLO FRESH 5LB TRAY") "TRAY"
    # means one case per tray, but here the oz is a PER-ITEM weight, so
    # "TRAY" needs its own real per-tray item count, which is NOT 1. LE
    # CHEF's own file reveals this directly: two sibling SKUs state a
    # "6/TRAY" chain explicitly ("LE CHEF DANISH OPEN CHEESE POCKET 4OZ",
    # "LE CHEF CROISSANT STRAIGHT FRENCH BUTTER") -- i.e. a LE CHEF tray is
    # 6 items. Verified against every bare-TRY/TRAY LE CHEF SKU with an
    # explicit oz: implies $6.84-12.72/lb, all plausible for bakery pastry.
    # Deliberately scoped to "LE CHEF" by name -- not assumed to generalize
    # to any other vendor's tray convention.
    pu_bare_nct = pd.to_numeric(pu.str.extract(r"^(\d+)CT\*?$")[0], errors="coerce")
    is_le_chef_tray = out["raw_name"].astype(str).str.contains("LE CHEF", case=False, na=False) & pu.isin(
        ["TRY", "TRAY"]
    )
    other_mult = pu_bare_nct.copy()
    other_mult[is_le_chef_tray] = 6.0
    other_per_case = explicit_oz_lb.where(is_none_shape) * other_mult
    other_per_case[other_per_case < _MIN_PLAUSIBLE_CASE_LB] = pd.NA
    tier2_explicit_oz_other = other_per_case * units_num
    implied_dpl_other = out["total_price"] / tier2_explicit_oz_other
    tier2_explicit_oz_other[implied_dpl_other > _UCR_EXPLICIT_OZ_MAX_DOLLAR_PER_LB] = pd.NA
    needs_explicit_oz_other = weight.isna() & tier2_explicit_oz_other.notna()
    weight[needs_explicit_oz_other] = tier2_explicit_oz_other[needs_explicit_oz_other]
    weight_source[needs_explicit_oz_other] = "computed_tier2"

    # GAL-based Purchase Units: a numeric gallon-per-unit quantity (bare
    # "GAL" means 1 gal per unit, i.e. Units already IS the gallon count;
    # "3GAL"/"5GAL" etc. give a per-case gallon size; "2/1GAL" is a chain,
    # 2 sub-packs of 1 gal each) x a density-per-product-type assumption
    # (see gal_density_dictionary, shared with UCB) x Units.
    gal_qty_chain = pu.str.extract(r"^(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)\s*GAL\*?$")
    gal_qty_bare = pd.to_numeric(pu.str.extract(r"^(\d+(?:\.\d+)?)\s*GAL\*?$")[0], errors="coerce")
    gal_qty = pd.to_numeric(gal_qty_chain[0], errors="coerce") * pd.to_numeric(gal_qty_chain[1], errors="coerce")
    gal_qty = gal_qty.fillna(gal_qty_bare)
    gal_qty[pu.isin(["GAL", "GAL*"])] = 1.0
    is_gal = gal_qty.notna()
    if is_gal.any():
        gal_dict = _load_gal_density_dict(WEIGHT_DICTIONARIES_DIR / "gal_density_dictionary.csv")
        for idx in df.index[is_gal & weight.isna()]:
            name = out.at[idx, "raw_name"]
            qty = units_num.at[idx]
            if pd.isna(name) or pd.isna(qty) or _GAL_NON_FOOD_RE.search(name):
                continue
            match = _pattern_dict_lookup(name, gal_dict)
            if match is None:
                continue
            density_lb_per_gal, human_confirmed = match
            weight.at[idx] = density_lb_per_gal * gal_qty.at[idx] * qty
            weight_source.at[idx] = "reference_table_tier3"

    # Gallon quantities also show up embedded directly in the NAME rather
    # than the Purchase Unit column (e.g. "CREAM HEAVY MANUFACTURING 40%
    # 0.5GAL", "OIL CORN 6/1GAL") -- same shape UCB's Daylight Foods uses.
    # "Chain"-shaped names (e.g. "6/1GAL" = 6 gal/case already) are
    # self-contained and must NOT also be multiplied by the Purchase
    # Unit's case count (real bug caught via $/lb: "OIL CORN 6/1GAL" at
    # Purchase Unit "6/CS" implies a plausible $2.03/lb using the name's
    # chain alone, vs. an implausible $0.34/lb if the "6" from Purchase
    # Unit were applied on top of it). "Bare"-shaped names (e.g. "0.5GAL")
    # need the Purchase Unit's case multiplier -- verified: "CREAM HEAVY
    # MANUFACTURING 40% 0.5GAL" (6/CS) implies $1.69/lb, plausible.
    name_gal_shape = out["raw_name"].astype(str).apply(_gal_quantity_with_shape)
    name_gal_value = name_gal_shape.apply(lambda t: t[0])
    name_gal_shape_kind = name_gal_shape.apply(lambda t: t[1])
    is_name_gal_chain = name_gal_shape_kind == "chain"
    is_name_gal_bare = name_gal_shape_kind == "bare"
    name_gal_qty = pd.Series(float("nan"), index=df.index, dtype="float64")
    name_gal_qty[is_name_gal_chain] = name_gal_value[is_name_gal_chain]
    name_gal_qty[is_name_gal_bare] = (name_gal_value * pu_case_mult)[is_name_gal_bare]
    is_name_gal = name_gal_qty.notna()
    if is_name_gal.any():
        gal_dict = _load_gal_density_dict(WEIGHT_DICTIONARIES_DIR / "gal_density_dictionary.csv")
        for idx in df.index[is_name_gal & weight.isna()]:
            name = out.at[idx, "raw_name"]
            qty = units_num.at[idx]
            if pd.isna(name) or pd.isna(qty) or _GAL_NON_FOOD_RE.search(name):
                continue
            match = _pattern_dict_lookup(name, gal_dict)
            if match is None:
                continue
            density_lb_per_gal, human_confirmed = match
            weight.at[idx] = density_lb_per_gal * name_gal_qty.at[idx] * qty
            weight_source.at[idx] = "reference_table_tier3"

    # Liter/mL quantities embedded in the name (e.g. "JUICE CONC FRZ ORG
    # GV PAS 3L", "CREAMER FRENCH VAN COFFEEMATE 2/1.5LTR") -- same
    # chain-vs-bare distinction as GAL, converted to gallon-equivalents
    # and run through the same density dictionary. Verified: "JUICE CONC
    # FRZ ORG GV PAS 3L" (bare, CASE) implies $0.82/lb, "CREAMER FRENCH
    # VAN COFFEEMATE 2/1.5LTR" (chain, 2/CS Purchase Unit redundant with
    # the chain's own leading "2") implies $1.63/lb, both plausible.
    name_liter_shape = out["raw_name"].astype(str).apply(_liter_quantity_with_shape)
    name_liter_value = name_liter_shape.apply(lambda t: t[0])
    name_liter_shape_kind = name_liter_shape.apply(lambda t: t[1])
    is_name_liter_chain = name_liter_shape_kind == "chain"
    is_name_liter_bare = name_liter_shape_kind == "bare"
    name_liter_gal_qty = pd.Series(float("nan"), index=df.index, dtype="float64")
    name_liter_gal_qty[is_name_liter_chain] = name_liter_value[is_name_liter_chain]
    name_liter_gal_qty[is_name_liter_bare] = (name_liter_value * pu_case_mult)[is_name_liter_bare]
    is_name_liter = name_liter_gal_qty.notna()
    if is_name_liter.any():
        gal_dict = _load_gal_density_dict(WEIGHT_DICTIONARIES_DIR / "gal_density_dictionary.csv")
        for idx in df.index[is_name_liter & weight.isna()]:
            name = out.at[idx, "raw_name"]
            qty = units_num.at[idx]
            if pd.isna(name) or pd.isna(qty) or _GAL_NON_FOOD_RE.search(name):
                continue
            match = _pattern_dict_lookup(name, gal_dict)
            if match is None:
                continue
            density_lb_per_gal, human_confirmed = match
            weight.at[idx] = density_lb_per_gal * name_liter_gal_qty.at[idx] * qty
            weight_source.at[idx] = "reference_table_tier3"

    # A bare "N G" in the name means GALLONS specifically for Sysco/Pepsi
    # soda BIB (bag-in-box syrup concentrate) items -- a real, well-known
    # abbreviation in this data (verified: rows with Purchase Unit "GAL"
    # already spell out the same convention, e.g. "SODA BIB PEPSI 5G" at
    # Purchase Unit "GAL", Units=1314, already resolved elsewhere). Only
    # a few rows have Purchase Unit "BIB"/"BIB*" instead, where the "5G"/
    # "3G" in the name is the only place the gallon size appears at all.
    # Deliberately narrow (requires "BIB" in the name) since a bare "G"
    # is otherwise a real gram unit for other products (candy/snack items
    # like "140G", "250G") -- would be wrong to treat those as gallons.
    is_bib_context = out["raw_name"].astype(str).str.contains("BIB", case=False, na=False)
    if is_bib_context.any():
        bib_gal = pd.to_numeric(
            out["raw_name"].astype(str).str.extract(r"(\d+(?:\.\d+)?)\s*-?\s*G\b")[0], errors="coerce"
        )
        gal_dict = _load_gal_density_dict(WEIGHT_DICTIONARIES_DIR / "gal_density_dictionary.csv")
        for idx in df.index[is_bib_context & weight.isna()]:
            name = out.at[idx, "raw_name"]
            qty = units_num.at[idx]
            gal_qty_val = bib_gal.at[idx]
            if pd.isna(name) or pd.isna(qty) or pd.isna(gal_qty_val):
                continue
            match = _pattern_dict_lookup(name, gal_dict)
            if match is None:
                continue
            density_lb_per_gal, human_confirmed = match
            weight.at[idx] = density_lb_per_gal * gal_qty_val * qty
            weight_source.at[idx] = "reference_table_tier3"

    # Gram quantities embedded in the name (e.g. "SEASONING SHICHIMI
    # TOGARASHI SB 300G", "PACK CARBO BULDAK CHKN FLVR RAMEN 140G") --
    # same chain-vs-bare distinction as weight/volume elsewhere. Excludes
    # anything already resolved above (notably the BIB soda items, where
    # a bare "G" means gallons, not grams). Verified: "SEASONING SHICHIMI
    # TOGARASHI SB 300G" (EACH) implies $2.90/lb, plausible for a spice.
    _GRAM_CHAIN_RE = re.compile(rf"({_GAL_NUM})\s*/\s*({_GAL_NUM})\s*-?\s*G\b", re.IGNORECASE)
    _GRAM_BARE_RE = re.compile(rf"({_GAL_NUM})\s*-?\s*G\b", re.IGNORECASE)
    LB_PER_GRAM = 0.00220462

    def _gram_quantity_with_shape(nm: str) -> tuple[float | None, str]:
        m = _GRAM_CHAIN_RE.search(nm)
        if m:
            return float(m.group(1)) * float(m.group(2)) * LB_PER_GRAM, "chain"
        m = _GRAM_BARE_RE.search(nm)
        if m:
            return float(m.group(1)) * LB_PER_GRAM, "bare"
        return None, "none"

    name_gram_shape = out["raw_name"].astype(str).apply(_gram_quantity_with_shape)
    name_gram_value = name_gram_shape.apply(lambda t: t[0])
    name_gram_shape_kind = name_gram_shape.apply(lambda t: t[1])
    is_name_gram_chain = name_gram_shape_kind == "chain"
    is_name_gram_bare = name_gram_shape_kind == "bare"
    # Bare-shaped gram figures typically describe a SMALL individual item
    # (candy, snack packs, spice jars) -- the same category where a bare
    # "CASE" Purchase Unit hid a real multi-count for OZ-based items
    # earlier in this file (median ~$52-109/lb, wrong), while "EACH"
    # checked out fine there (median ~$7-10/lb, plausible -- verified
    # "SEASONING SHICHIMI TOGARASHI SB 300G" at Purchase Unit "EACH"
    # implies $2.90/lb here too). So a genuine numeric "N/CS" multiplier
    # or a bare "EACH"/"EACH*" (multiplier=1) is trusted, but not the
    # broader pu_case_mult set that also treats bare "CASE"/"BOX"/etc. as
    # multiplier=1.
    gram_bare_mult = ncs_case_mult.copy()
    gram_bare_mult[pu.isin(["EACH", "EACH*"])] = 1.0
    name_gram_lb = pd.Series(float("nan"), index=df.index, dtype="float64")
    name_gram_lb[is_name_gram_chain] = name_gram_value[is_name_gram_chain]
    name_gram_lb[is_name_gram_bare] = (name_gram_value * gram_bare_mult)[is_name_gram_bare]
    # The plausibility floor only makes sense where the result is
    # supposed to represent a genuine multi-item CASE total (chain shape,
    # or a bare shape combined with a real N/CS multiplier > 1) -- a
    # single "EACH" item (multiplier exactly 1) is legitimately allowed
    # to weigh under 1 lb (a spice jar, a candy bar), so the floor must
    # not reject it. Real bug caught this way: "SEASONING SHICHIMI
    # TOGARASHI SB 300G" (EACH, ~0.66 lb/jar) was being rejected by this
    # floor and left unresolved even though 0.66 lb is a perfectly
    # reasonable individual-item weight, not a portion-mistaken-for-case
    # error.
    is_genuine_case_total = is_name_gram_chain | (is_name_gram_bare & (gram_bare_mult != 1.0))
    name_gram_lb[is_genuine_case_total & (name_gram_lb < _MIN_PLAUSIBLE_CASE_LB)] = pd.NA
    # Real bug caught via a $/lb check: "CLEANER VEG FRUIT ANTIMICRO
    # ECOLAB 2.5G" and "SBUX CLEANING TABLET JAR CAFIZA 3G" are cleaning
    # chemicals, not food -- their gram figures describe a tablet/dose
    # size, not a food weight, and implied $16,992/lb and $1,253/lb.
    is_non_food_gram = out["raw_name"].astype(str).apply(lambda n: bool(_GAL_NON_FOOD_RE.search(n)))
    name_gram_lb[is_non_food_gram] = pd.NA
    needs_gram = weight.isna() & name_gram_lb.notna()
    weight[needs_gram] = (name_gram_lb * units_num)[needs_gram]
    weight_source[needs_gram] = "computed_tier2"

    # "#10" (a standard foodservice can size, ~6.5 lb -- same estimate as
    # UCB's Sysco #10-can dictionary, reused here since this is also
    # Sysco) appears embedded directly in the NAME rather than a separate
    # Pack size column, e.g. "TOMATO DICED CANNED NO SALT 6/#10" (6 cans
    # per case) or "SYS CLS PEA BLACKEYE #10" (bare, one can per case
    # unit). Case multiplier comes from, in order: a real Purchase Unit
    # "N/CS" count if present; else a "N/#10" chain in the name itself;
    # else 1 (Purchase Unit "CASE", bare). Verified against real data:
    # "TOMATO DICED CANNED NO SALT 6/#10" (6/CS x 110) implies $0.95/lb,
    # "SYRUP CHOCOLATE SAFARI CANNED 6/#10" (CASE, "6/" from the name x 5)
    # implies $2.06/lb, both plausible for canned goods.
    # Real bug caught via a $/lb check: "#10" isn't always a can size --
    # "CONE CAKE #10 36/20CT" and "MEAD ENVELOPES WHITE #10 50CT" use it
    # as an unrelated product size code (a cone size, an envelope size),
    # implying an absurd $0.20-0.22/lb when treated as a 6.5 lb can.
    # Checked against every real "#10" name in this file -- these two are
    # the only non-can matches, so excluded specifically rather than by a
    # broader (and riskier) keyword allowlist.
    _NAME_NUMBER10_CHAIN_RE = re.compile(r"(\d+)\s*/\s*#10\b")
    _NUMBER10_NON_CAN_NAMES = {"CONE CAKE #10 36/20CT", "MEAD ENVELOPES WHITE #10 50CT"}
    has_number10 = out["raw_name"].astype(str).str.contains(r"#10\b", regex=True, na=False) & ~out[
        "raw_name"
    ].isin(_NUMBER10_NON_CAN_NAMES)
    if has_number10.any():
        can_dict = _load_pattern_weight_dict(WEIGHT_DICTIONARIES_DIR / "sysco_number10_can_dictionary.csv")
        for idx in df.index[has_number10 & weight.isna()]:
            name = out.at[idx, "raw_name"]
            qty = units_num.at[idx]
            if pd.isna(name) or pd.isna(qty):
                continue
            mult = ncs_case_mult.at[idx]
            if pd.isna(mult):
                name_chain = _NAME_NUMBER10_CHAIN_RE.search(name)
                mult = float(name_chain.group(1)) if name_chain else 1.0
            match = _pattern_dict_lookup(name, can_dict)
            if match is None:
                continue
            each_lb, human_confirmed = match
            weight.at[idx] = each_lb * mult * qty
            weight_source.at[idx] = "reference_table_tier3"

    # Sysco is a national distributor -- some SKUs also appear in UCB's
    # Sysco export with a real Pack size (a genuine case weight, not an
    # estimate). Reused here on the assumption the same national SKU is
    # packed the same way regardless of which campus orders it (verified:
    # "SAUSAGE TURKEY PTY CKD 1.6OZ" -- UCB's Sysco data shows Pack size
    # "10LB" for this exact name -- implies $4.38/lb here, plausible for
    # turkey sausage patties). Modest reach (9 of 560 distinct unresolved
    # UCR Sysco names, ~$30K) since most SKUs simply don't recur across
    # both campuses' exports, but it's real data, not a guess, so worth
    # taking where it applies.
    is_sysco_row = df["Vendor"] == "Sysco"
    if is_sysco_row.any():
        cross_campus_lookup = _load_exact_title_weight_dict(
            WEIGHT_DICTIONARIES_DIR / "sysco_cross_campus_case_weight_lookup.csv"
        )
        for idx in df.index[is_sysco_row & weight.isna()]:
            name = out.at[idx, "raw_name"]
            qty = units_num.at[idx]
            if pd.isna(name) or pd.isna(qty):
                continue
            name_up = str(name).strip().upper()
            if name_up not in cross_campus_lookup:
                continue
            case_lb, human_confirmed = cross_campus_lookup[name_up]
            weight.at[idx] = case_lb * qty
            weight_source.at[idx] = "reference_table_tier3"

    # Common produce/prepared-food items sold by a piece/bunch/container
    # count rather than a weight -- "case"-formula entries (standard-
    # carton produce: avocado/orange/apple/pear) use Units directly and
    # sidestep parsing a count at all (this file's count sometimes shows
    # up as a size-grade RANGE, e.g. "88/90CT" -- irrelevant here since
    # the flat carton weight doesn't depend on it). "multiply"-formula
    # entries need a real per-case count: a "chain"-shaped name count
    # (e.g. "12/12CT") is already complete and used alone (a separate
    # Purchase Unit "N/CS", when present, is redundant with the chain's
    # own leading number in the large majority of real cases, same
    # finding as the analogous weight-chain logic, so NOT applied on top
    # -- verified "TORTILLA FLOUR 10\" 12/12CT" at Purchase Unit "12/CS"
    # implies a plausible $2.12/lb using the chain alone). A
    # "bare"/"range"/"paren"-shaped name count has no case-multiplier of
    # its own and needs Purchase Unit's "N/CS" applied on top -- real bug
    # caught via a $/lb check: "BUN BRIOCHE MINI SLICED TURANO 18CT"
    # (bare "18CT") at Purchase Unit "12/CS" was resolved using just the
    # bare count, silently dropping the real "12" case-multiplier -- a
    # 12x undercount that implied an absurd $41.34/lb; fixed to use
    # 18 x 12 = 216, giving a plausible $3.44/lb.
    # Keyword matching alone is too risky here -- "APPLE"/"ORANGE"/etc.
    # also match dozens of unrelated beverage/candy items in this file
    # (CELSIUS FUJI APPLE, GATORADE..., OCEAN SPRAY APPLE, etc.), so every
    # pattern in ucr_count_food_dictionary.csv is anchored to this
    # export's own genuine fresh-produce naming prefixes ("SNR "/"SW "),
    # verified against every real match for each pattern before adding.
    # Also reuses UCB's sysco_ct_food_dictionary.csv (bakery/prepared-food
    # items already verified there -- tortillas, buns, egg, etc.) since
    # this is the same national distributor's naming conventions. Checked
    # first (more specific patterns), falling back to
    # ucr_count_food_dictionary's produce entries.
    # Some names carry no count of their own at all (e.g. "SNR CORN COB
    # YELLOW") -- the count lives only in Purchase Unit's bare "NCT"
    # ("48CT"), used as a last resort when the name gives nothing. "DOZ"
    # (e.g. "ASSORTED DONUT" at Purchase Unit "DOZ") is the same idea --
    # Units already counts dozens, so it's folded into this same bare-count
    # fallback as a literal 12.
    pu_bare_ct = pd.to_numeric(pu.str.extract(r"^(\d+)\s*CT\*?(?:-CS)?$")[0], errors="coerce")
    pu_bare_ct[pu == "DOZ"] = 12.0
    # A "CSN" Purchase Unit (e.g. "CS24", "CS80" -- distinct from "N/CS")
    # is itself a real per-case count -- found via "SW BREAD SUB ROLL/WHOLE
    # WHEAT 80CT" at Purchase Unit "CS80" (confirms 80 rolls/case for this
    # product line) and "PIZZA CRUST 12" PARBAKED WOOD FIRED CS24" at "CS24".
    pu_csn_mult = pd.to_numeric(pu.str.extract(r"^CS(\d+)$")[0], errors="coerce")
    ucr_count_dict = _load_count_based_dict(WEIGHT_DICTIONARIES_DIR / "ucr_count_food_dictionary.csv")
    sysco_ct_dict = _load_count_based_dict(WEIGHT_DICTIONARIES_DIR / "sysco_ct_food_dictionary.csv")
    for idx in df.index[weight.isna()]:
        name = out.at[idx, "raw_name"]
        qty = units_num.at[idx]
        if pd.isna(name) or pd.isna(qty):
            continue
        match_formula = None
        for pattern, ref_lb, formula, human_confirmed in sysco_ct_dict + ucr_count_dict:
            if pattern.search(name):
                match_formula = (ref_lb, formula)
                break
        if match_formula is None:
            continue
        ref_lb, formula = match_formula
        if formula == "case":
            weight.at[idx] = ref_lb * qty
        else:
            count, shape = _extract_count_with_shape(name)
            if shape == "none":
                # No count anywhere in the name -- fall back to whatever
                # Purchase Unit itself gives: its own bare "NCT"/"DOZ"
                # count (pu_bare_ct), else a real "N/CS" case multiplier
                # (real bug found via a $/lb check: "BREAD SOURDOUGH 3/4IN
                # SLI" at Purchase Unit "10/CS" was skipped entirely before
                # this fallback existed, since "10/CS" doesn't match the
                # bare-NCT pattern), else a bare "EACH"/"EACH*" Purchase
                # Unit means one item per unit (verified: "BAGEL JALAPENO
                # CHEESE" at EACH implies $3.38/lb, plausible for bagels;
                # same established "EACH means 1" precedent used elsewhere
                # in this file). The "N/CS" fallback needs the same $/lb
                # ceiling used for the explicit-oz "N/CS" path above -- real
                # bug caught this way: "TORTILLA 6" CORN MISSION 6/5DOZ"
                # (6/CS) implied $82.03/lb, "WPD BREAD PITA 60/CS" (6/CS)
                # implied $53.33/lb, "BUN HAMBURGER 100% WHEAT 4"" (6/CS)
                # implied $29.86/lb, "BREAD PITA FOLD 7"" (12/CS) implied
                # $25.05/lb -- all cases where the bare "N/CS" undercounts
                # the true per-case quantity (bread/tortilla items are
                # often packaged multiple-per-sub-unit, not 1:1 with the
                # Purchase Unit's leading number). pu_bare_ct ("NCT"/"DOZ")
                # and bare "EACH"=1 are NOT subject to this ceiling -- both
                # are separately established as reliable elsewhere in this
                # file. The same ceiling also covers the "CSN" case-count
                # (real per-case count, but still worth a sanity check) and
                # the LE CHEF tray=6 convention (see the explicit-oz
                # version of this same rule above for the "6/TRAY" sibling
                # evidence). Bare "LOAF" is trusted like "EACH" (verified:
                # "AVB PULLMAN TEXAS TOAST WHITE SLICE 1"" at Purchase Unit
                # "LOAF" implies $3.27/lb, plausible).
                count = pu_bare_ct.at[idx]
                used_ncs_fallback = False
                if pd.isna(count):
                    count = ncs_case_mult.at[idx]
                    used_ncs_fallback = not pd.isna(count)
                if pd.isna(count):
                    count = pu_csn_mult.at[idx]
                    used_ncs_fallback = not pd.isna(count)
                if pd.isna(count) and out.at[idx, "raw_name"].upper().find("LE CHEF") != -1 and pu.at[idx] in ("TRY", "TRAY"):
                    count = 6.0
                    used_ncs_fallback = True
                if pd.isna(count) and pu.at[idx] in ("EACH", "EACH*", "LOAF"):
                    count = 1.0
                if pd.isna(count):
                    continue
            elif shape != "chain":
                used_ncs_fallback = False
                pu_mult = ncs_case_mult.at[idx]
                if not pd.isna(pu_mult):
                    count *= pu_mult
            else:
                used_ncs_fallback = False
            candidate_weight = ref_lb * count * qty
            if used_ncs_fallback:
                price = out.at[idx, "total_price"]
                if pd.isna(price) or candidate_weight <= 0 or price / candidate_weight > _UCR_EXPLICIT_OZ_MAX_DOLLAR_PER_LB:
                    continue
            weight.at[idx] = candidate_weight
        weight_source.at[idx] = "reference_table_tier3"

    # Eggs are sold by a dozen-count ("EGG SHELL LARGE WHITE CAGE FREE
    # 15DZ" = 15 dozen = 180 eggs; "SNR EGG-LARGE RETAIL 15/1DZ" = a
    # chain, 15 x 1 dozen = 180 eggs) or a bare piece count ("EGG HARD
    # CKD CAGEFREE 12/12CT" -- doesn't match the dictionary's hardcooked
    # pattern above since Purchase Unit "12/CS" provides the case count
    # separately from the name's own "12CT"; tried here as a fallback) --
    # a standard large shell egg runs ~2 oz. Verified: "EGG SHELL LARGE
    # WHITE CAGE FREE 15DZ" implies $3.44/lb, plausible for eggs sold by
    # weight in bulk.
    is_egg = out["raw_name"].astype(str).str.contains("EGG", case=False, na=False)
    if is_egg.any():
        for idx in df.index[is_egg & weight.isna()]:
            name = out.at[idx, "raw_name"]
            qty = units_num.at[idx]
            if pd.isna(name) or pd.isna(qty):
                continue
            dozen_count = _extract_dozen_count(name)
            if dozen_count is not None:
                weight.at[idx] = 0.125 * dozen_count * qty
                weight_source.at[idx] = "reference_table_tier3"
                continue
            # "EGG HARD CKD CAGEFREE 12/12CT": the name's own chain
            # ("12/12CT" = 12 boxes of 12 eggs = 144 eggs/case) is
            # self-contained -- Purchase Unit's separate "12/CS" is
            # redundant with the chain's own leading token (same
            # established pattern elsewhere in this project), so NOT
            # also multiplied in here. Verified: implies $2.63/lb,
            # plausible for eggs. A bare (non-chain) egg count, if one
            # ever shows up, DOES need Purchase Unit's real "N/CS"
            # multiplier applied -- same fix as the produce/prepared-food
            # pass above.
            egg_count, egg_shape = _extract_count_with_shape(name)
            if egg_count is None:
                continue
            if egg_shape != "chain":
                pu_mult = ncs_case_mult.at[idx]
                if not pd.isna(pu_mult):
                    egg_count *= pu_mult
            weight.at[idx] = 0.125 * egg_count * qty
            weight_source.at[idx] = "reference_table_tier3"

    out["total_weight_lbs"] = weight
    out["weight_source"] = weight_source

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
    # Real bug: 'Total Item cost' is NOT a per-row price -- when a product
    # spans multiple rows (e.g. split across delivery batches), this column
    # repeats the SAME aggregate value on every one of that product's rows,
    # rather than each row's own cost. Verified exhaustively: across all 88
    # (Product Description, Total Item cost) groups with >1 row in this
    # file, Sum(Cost) matches the repeated Total Item cost value exactly
    # (0 mismatches) -- 'Cost' is the real per-row amount. Using 'Total
    # Item cost' directly (the original approach) summed every repeated
    # instance, overstating UCSC's total spend by ~$1.94M (~60%, $5.18M vs
    # the correct $3.23M) -- most visibly on rows literally named "GENERIC
    # FOOD" that all shared one repeated total regardless of their very
    # different individual quantities/costs.
    out["total_price"] = df["Cost"].apply(_parse_currency)

    # 'Total Weight (in lbs)' is unusable (100% of populated cells are a '-'
    # placeholder). Instead, 'Total units ordered' already reports the total
    # quantity in whatever 'Unit Type' specifies -- when that's LB it's a
    # direct weight; OZ/G/GM/GR/KG need only a unit conversion, not
    # estimation.
    unit_type = df["Unit\nType"].astype(str).str.strip().str.upper()
    total_units = pd.to_numeric(
        df["Total units ordered"].astype(str).str.replace(",", "").str.strip(), errors="coerce"
    )
    weight = pd.Series(float("nan"), index=df.index, dtype="float64")
    # "LBS" is a plain spelling variant, and "LB AVG" checked out as a
    # plausible direct weight too (e.g. "PORK CARNITAS MEAT PRCK CAFE H"
    # implies $3.98/lb). "LB AV" (a single row, "PORK BUTT BNLS 1/4
    # 6-9#EA") is deliberately excluded -- its Total units ordered
    # (167,510) implies an implausible $0.28/lb, suggesting this one row's
    # number means something other than a direct lb count; left
    # unresolved rather than guessed.
    is_lb = unit_type.isin(["LB", "LBS", "LB AVG"])
    weight[is_lb] = total_units[is_lb]
    weight[unit_type == "OZ"] = total_units[unit_type == "OZ"] / 16.0
    # "G"/"GM"/"GR" (grams) and "KG" are direct weight units too, just
    # different conversions -- plain unit conversions, not estimates.
    is_grams = unit_type.isin(["G", "GM", "GR"])
    weight[is_grams] = total_units[is_grams] * 0.00220462
    weight[unit_type == "KG"] = total_units[unit_type == "KG"] * 2.20462
    weight_source = weight.notna().map({True: "reported", False: "unresolved"})

    # Count-based unit types (CT/EA/EACH/PK/CN/CAN/DOZ/LOAF). Two guards
    # were needed after this file's "Units" column turned out to be
    # unreliable in a specific, now-understood way:
    #   1. "Units" values for these unit types fall cleanly into two
    #      populations with a wide gap between them: 1-163 (30 distinct
    #      values found, topping out at 163) and 612+ (8 distinct values,
    #      starting at 612 -- nothing in between). The 612+ group is NOT a
    #      case-pack count: "PROD CELERY WHL" (Units=2,430) and "PROD
    #      MELON CANTALOUPE" (Units=612) implied $0.0054/lb and $0.0096/lb
    #      treated that way, both absurd -- some other Foodpro-internal
    #      unit-of-measure is bleeding into this column for those rows.
    #      Gated at <=200 (comfortably above 163, below 612).
    #   2. Even within the safe range, "PROD MELON HONEYDEW APPROX WT"
    #      (Units=48, well under the threshold) still implied an absurd
    #      $0.070/lb -- its own name says "APPROX WT", i.e. this specific
    #      line is itself an approximate-weight estimate, not a real
    #      count, regardless of Unit Type. Excluded by name.
    # "Total units ordered" (== Units Purchased x Units, verified exactly)
    # is the total individual-item count for "multiply"-formula entries
    # once both guards pass. "Case"-formula entries (standard-carton
    # commodities: avocado/orange/apple/pear/grapefruit/lemon/lime) don't
    # need either guard -- they use Units Purchased (case count) alone,
    # sidestepping the unreliable Units column entirely.
    # Real bug also caught via a $/lb check: matching by keyword alone
    # also hit non-produce items sharing a fruit name ("BARRITAS,
    # PINEAPPLE FILL COOKIES", "FRUIT CAN PEAR SLI CHOICE IN JUICE") --
    # 208 of 233 raw keyword matches in this file turned out to be flavor
    # descriptors/condiments/snacks, not fresh produce. UCSC's own naming
    # convention prefixes every genuine fresh-produce line with "PROD "
    # (verified against every commodity this dictionary covers), so
    # matching is restricted to that prefix.
    _UCSC_UNITS_MAX_TRUSTED = 200
    is_count_based = unit_type.isin(["CT", "EA", "EACH", "PK", "CN", "CAN", "DOZ", "LOAF"])
    if is_count_based.any():
        count_dict = _load_count_based_dict(WEIGHT_DICTIONARIES_DIR / "count_based_food_dictionary.csv")
        units_purchased = pd.to_numeric(
            df["Units\nPurchased"].astype(str).str.replace(",", "").str.strip(), errors="coerce"
        )
        units_col = pd.to_numeric(
            df["Units"].astype(str).str.replace(",", "").str.strip(), errors="coerce"
        )
        for idx in df.index[is_count_based & weight.isna()]:
            name = out.at[idx, "raw_name"]
            if pd.isna(name) or not str(name).startswith("PROD "):
                continue
            match_formula = None
            for pattern, ref_lb, formula, human_confirmed in count_dict:
                if pattern.search(name):
                    match_formula = (ref_lb, formula)
                    break
            if match_formula is None:
                continue
            ref_lb, formula = match_formula
            if formula == "case":
                cases = units_purchased.at[idx]
                if pd.isna(cases):
                    continue
                weight.at[idx] = ref_lb * cases
            else:
                if "APPROX WT" in str(name).upper():
                    continue
                units_val = units_col.at[idx]
                item_count = total_units.at[idx]
                if pd.isna(units_val) or units_val > _UCSC_UNITS_MAX_TRUSTED or pd.isna(item_count):
                    continue
                weight.at[idx] = ref_lb * item_count
            weight_source.at[idx] = "reference_table_tier3"

    out["total_weight_lbs"] = weight
    out["weight_source"] = weight_source

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

    # Same bug as UCD_H/UCLA_H (fixed this session): "weight" == 0 is a
    # missing-data placeholder, not a real "weighs nothing" claim --
    # weight.notna() treated 0.0 as valid, silently tagging $2.49M of real
    # spend (24.4% of this file) 'reported' with a weight of exactly zero.
    # Requires weight != 0, not weight > 0 -- this file has 31 real
    # negative-weight return/credit rows that are legitimate resolved
    # data, a different thing from the exactly-zero placeholder.
    weight = pd.to_numeric(df["weight"], errors="coerce")
    is_reported = weight.notna() & (weight != 0)
    out["total_weight_lbs"] = weight.where(is_reported)
    out["weight_source"] = is_reported.map({True: "reported", False: "unresolved"})

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

# Personal-care / OTC-medicine items -- found via a $/lb outlier check on UCR
# weight resolution (small cosmetic/first-aid tubes implied high but
# plausible $/lb, which is what surfaced them as real products rather than
# a parsing bug). Confirmed with project owner these should be excluded the
# same as gloves/napkins. "COUGH DROP" is multi-word since bare "DROP"
# would be far too broad; every other entry here was checked against the
# full product list for food-name collisions before adding (none found).
PERSONAL_CARE_KEYWORDS = [
    "GLUE", "CHAPSTICK", "NEOSPORIN", "VISINE", "OINTMENT", "LOTION", "DEODORANT",
    "SHAMPOO", "TOOTHPASTE", "MOUTHWASH", "BAND-AID", "TYLENOL", "ADVIL", "ALEVE",
    "COUGH DROP",
]

# Cleaning chemicals / janitorial -- project owner explicitly asked for a
# sweep of these. "CHEM" and "WAXIE" alone catch the bulk of UCR's Sysco/
# Waxie-branded chemical SKUs (verified: 40 and 24 real matches
# respectively, zero food collisions). Deliberately excludes bare "BLEACH"
# -- unlike "BLEACHED"/"UNBLEACHED" (which a word-boundary check already
# excludes for free), two real food items use the bare word "BLEACH" as a
# flour-processing descriptor ("FLOUR CAKE & PASTRY BLEACH", "FLOUR-ALL
# PURPOSE ENR BLEACH 50 LB") -- a real false positive found while auditing
# this list, so the 2 genuine bleach-cleaner rows ("AJAX CLEANSER WITH
# BLEACH", "CLORALEN BLEACH") are knowingly left unexcluded rather than
# risk stripping real flour purchases.
CLEANING_CHEMICAL_KEYWORDS = [
    "CHEM", "WAXIE", "CLEANER", "CLEANING", "SANITIZER", "DETERGENT", "DEGREASER",
    "DELIMER", "SOAP", "ANTIBACTERIAL", "DISENFECTANT", "DISINFECTANT",
]

# Janitorial paper goods / foodservice equipment/supplies -- same sweep.
# Bare "LINER" was rejected (matches real produce items like "LETTUCE,
# HONEY GEM 18 CT LINER"), so specific liner phrases are used instead.
# Bare "PAD", "PAN", "BOWL", "TRAY", "WRAP", "FORK", "SPOON", "KNIFE",
# "SCOOP", "BLEACH" were all rejected too -- each collides heavily with
# real food names (Pad Thai, sheet pan, Amy's frozen bowls, produce trays,
# burrito wraps, English muffin "fork split", Dole fruit bowl w/ fork,
# avocado "hand scooped", Fritos/Tostitos "Scoops", etc.) -- left
# unresolved rather than guessed. "COMPOST34X48"/"COMPOST47X60" are exact
# tokens for 2 trash-liner SKUs whose dimension code is glued directly to
# "COMPOST" with no space, which a word-boundary check can't otherwise
# reach.
JANITORIAL_SUPPLY_KEYWORDS = [
    "QUILON", "PAN LINER", "LINER TRASH", "CAN LINER", "REL LINER", "SANITARY LINER",
    "LINERS", "TOILET", "HAIRNET", "APRON", "UTENSIL", "FRESHENER", "THERMOMETER",
    "TONGS", "MITT", "STIRRER", "DOMINION", "ICE SCOOP", "SPONGE", "MOP", "SCOUR",
    "SCRUB", "BROOM", "DUSTPAN", "TOWEL", "WIPER", "STAPLES", "COMPOST", "COMPOSTABLE",
    "COMPOST34X48", "COMPOST47X60",
]

# Non-product financial line items that showed up as ordinary-looking rows
# (a real Purchase Unit and Units value, not the "DOLR"-rollup pattern
# already filtered in load_ucr) -- "TOTE DEPOSIT" ($10,560, UCR) is a
# deposit charge, not a food purchase.
MISC_NON_FOOD_KEYWORDS = ["DEPOSIT"]

# Disposable foodservice equipment/serviceware (forks, spoons, bowls,
# trays, pans, wraps). Deliberately does NOT use bare "FORK"/"SPOON"/
# "BOWL"/"TRAY"/"PAN"/"WRAP" -- each collides heavily with real food names
# checked against the full product list before rejecting:
#   - FORK: "ENGLISH MUFFIN...FORK SPLIT..." (a split-style descriptor,
#     not a utensil), "DOLE FRUIT BWL MIX FORK..." (a fruit cup that
#     happens to bundle a fork).
#   - SPOON: "Spinach Baby Spoon" (a spinach leaf variety name).
#   - BOWL: ~150 real frozen-meal/noodle-bowl/cereal-bowl/salad-bowl food
#     items (Amy's, Annie Chun's, cereal single-serve bowls, etc.).
#   - TRAY: dozens of real food items packaged/sold "in a tray" (sliced
#     cheese, produce, frozen entrees, pastries) -- same packaging-language
#     principle as the existing BAG/CUP exclusions.
#   - PAN: Spanish/Latin bread names ("PAN DE QUESO", "PAN DULCE"), a
#     mushroom-jerky brand ("PAN'S MUSHROOM JERKY"), and cooking-method
#     descriptors ("PAN ROASTED", "PAN RDY") are all real food. "PAN
#     COATING"/"OIL, PAN COATING..." (cooking-oil spray) was deliberately
#     NOT added either -- it's an edible cooking oil, not equipment,
#     unlike the cleaning chemicals above.
#   - WRAP: dozens of real deli sandwich "wraps" and tortilla "wraps" --
#     "WRAP" is a legitimate food-style word here, not just packaging.
# Every phrase below was individually verified against the full product
# list with zero food collisions before being added.
EQUIPMENT_SUPPLY_KEYWORDS = [
    "KNIFE",
    "FORK PLAS", "FORK WOOD", "BIO-BASED FORK", "PAPER FORK", "SW FORK", "WRAPPED FORK",
    "SPOON PLAS", "SPOON WOOD", "PAPER SPOON", "SW SPOON", "WRAPPED SPOON",
    "BOWL LEAF BAMBOO", "BOWL PAPER", "BOWL PULP", "FIBER BOWL", "SW BOWL",
    "TRAY FOOD", "FOOD TRAY", "TRAY PAPER", "PAPER TRAY", "CARRY TRAY", "HUHTAMAKI",
    "PAN RACK", "PAN FOIL", "DUST PAN", "BUN PAN",
    "CHOPSTICK", "CLING WRAP", "WRAP PAPER", "WRAP FOIL", "FILM WRAP",
]

# Packaging supplies -- found while auditing the "unresolved" weight bucket
# for real bakery items and noticing these mixed in (a shelf-life label
# roll, a plastic wrap film roll, an empty cookie display bag) -- none are
# food. "LABEL"/"FOIL"/"ROLL" bare were all rejected -- "LABEL" collides
# with real food brand lines ("PORK BACON BLACK LABEL SLICED HORMEL",
# "YOGURT...BLUE LABEL"), "FOIL" collides with food packaged/wrapped in
# foil ("CHEESE, CREAM PLAIN LOAF BULK FOIL-WRAPPED REF"), "ROLL" collides
# with dozens of real bread/sushi/spring rolls.
PACKAGING_SUPPLY_KEYWORDS = [
    "LABEL ROLL", "SW LABEL", "SW LABELS", "PVC", "WITH WINDOW", "BAG PAPER",
    "FOIL ALMN", "FOIL ALUMINUM", "FOIL SHEET", "PAPER BOX PIZZA", "PAPER PICK",
]

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

NON_FOOD_PATTERN = r"\b(?:" + "|".join(
    NON_FOOD_KEYWORDS
    + WATER_KEYWORDS
    + PERSONAL_CARE_KEYWORDS
    + CLEANING_CHEMICAL_KEYWORDS
    + JANITORIAL_SUPPLY_KEYWORDS
    + MISC_NON_FOOD_KEYWORDS
    + EQUIPMENT_SUPPLY_KEYWORDS
    + PACKAGING_SUPPLY_KEYWORDS
) + r")\b"


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

def insert_product_and_purchase(
    conn: sqlite3.Connection,
    raw_name,
    group: pd.DataFrame,
    campus: str,
    campus_type: str,
    fiscal_year: int,
    source_report_id: str,
    cert_lookup: list[tuple[str, str, list[str]]],
) -> int:
    """Aggregates one raw_name's transaction-level rows into a single
    products/product_aliases/purchases row, exactly as Phase 1 ingestion
    does per distinct raw_name. Factored out of aggregate_and_load() so
    lib.rederive_bad_merges can re-derive individual raw names that were
    incorrectly merged before the entity-matching guards existed (see
    CLAUDE.md "Known data-integrity issue") using the identical logic,
    rather than reimplementing it. Returns the new product_id."""
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
    return product_id


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
        insert_product_and_purchase(
            conn, raw_name, group, campus, campus_type, fiscal_year, source_report_id, cert_lookup
        )
        n_products += 1
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
