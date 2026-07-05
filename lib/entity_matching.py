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
# A leading brand name's possessive/plural apostrophe-s sometimes gets
# exported with the apostrophe replaced by a space instead of dropped
# entirely (e.g. "BRENTLEY S CHICKEN" where the source was "Brentley's
# Chicken") -- rejoin it to the brand token so it scores as similar to a
# differently-exported copy that kept "BRENTLEYS" fused. Anchored to the
# START of the string only (confirmed with project owner: this is
# specifically a leading-brand-name artifact), so it can't collide with an
# unrelated standalone "S" elsewhere in the name.
_LEADING_BRAND_S_RE = re.compile(r"^(\w+)\s+([Ss])\b")
# Parenthesized descriptors are noise for scoring purposes when they wrap
# an otherwise-ordinary word -- "(FRESH)" vs "FRESH" is a real example
# confirmed by project owner. Strips just the parens, keeps the word
# inside. Applied AFTER the "(SUS)" strip above (which removes that whole
# tag, not just its parens) so the two don't interact; gates that care
# about a specific parenthesized tag (_halal_match's "(H)") read the RAW
# canonical_name directly, never this cleaned copy, so this can't weaken
# that detection.
_PAREN_CHARS_RE = re.compile(r"[()]")
# Word-level synonyms/abbreviations confirmed by project owner to be the
# same word for matching purposes -- normalized to a single canonical
# spelling so the fuzzy scorer doesn't penalize the difference. Each is
# verified against real data first (see git history/CLAUDE.md for the
# per-term audit) to avoid an abbreviation that collides with an unrelated
# word.
_WORD_SYNONYMS = [
    (re.compile(r"\bLIQ\b", re.IGNORECASE), "LIQUID"),
    (re.compile(r"\bPERUVIAN\b", re.IGNORECASE), "PERU"),
    (re.compile(r"\bCT\b", re.IGNORECASE), "COUNT"),
    (re.compile(r"\bPEEL\b", re.IGNORECASE), "PEELED"),
    (re.compile(r"\bGUMMI\b", re.IGNORECASE), "GUMMY"),
    (re.compile(r"\bFCY\b", re.IGNORECASE), "FANCY"),
    (re.compile(r"\bWHL\b", re.IGNORECASE), "WHOLE"),
    (re.compile(r"\bGRTD\b", re.IGNORECASE), "GRATED"),
]
# A "#" directly after a digit is a pounds abbreviation in this data
# ("5#", "20#") -- verified against real data: 904 names have a digit
# immediately before "#", only 5 have a LETTER immediately before it (and
# those all mean "number", e.g. "ITEM#", "OR# 160", not weight), so this is
# scoped to the digit-adjacent case specifically to avoid conflating the
# two. A trailing space is added so a directly-fused following letter
# ("5#BG" -> case/bag code) doesn't get glued onto "LB" ("5 LB BG", not
# "5 LBBG") -- rapidfuzz's default_process collapses the extra whitespace
# during actual scoring either way.
_POUND_SIGN_RE = re.compile(r"(\d)\s*#")


def _clean_for_matching(name: str) -> str:
    """Normalizes noise that's irrelevant to product identity before
    scoring (but never touches the stored canonical_name -- only the copy
    used for comparison): strips a trailing '(SUS)' campus-reporting tag
    (confirmed with project owner these mark the same underlying product --
    ~423 real pending pairs differed by exactly this), normalizes curly
    quotes/apostrophes to straight ones, rejoins a split leading-brand
    possessive ("BRENTLEY S" -> "BRENTLEYS"), strips parens around an
    otherwise-ordinary word ("(FRESH)" -> "FRESH"), normalizes a digit-
    adjacent "#" to "LB", and normalizes known word synonyms/abbreviations
    (see _WORD_SYNONYMS)."""
    cleaned = _SUS_SUFFIX_RE.sub("", name).translate(_QUOTE_TRANSLATION)
    cleaned = _LEADING_BRAND_S_RE.sub(r"\1\2", cleaned)
    cleaned = _PAREN_CHARS_RE.sub("", cleaned)
    cleaned = _POUND_SIGN_RE.sub(r"\1 LB ", cleaned)
    for pattern, replacement in _WORD_SYNONYMS:
        cleaned = pattern.sub(replacement, cleaned)
    # The "#" rewrite above can leave extra/trailing whitespace (e.g.
    # "5#BG" -> "5 LB BG"); collapse it so callers get a clean, consistently
    # formatted string rather than relying on rapidfuzz's own whitespace
    # handling for every caller.
    return re.sub(r"\s+", " ", cleaned).strip()


def _score_matrix(names: list[str]):
    cleaned = [_clean_for_matching(n) for n in names]
    return process.cdist(cleaned, cleaned, scorer=fuzz.token_sort_ratio, processor=rf_utils.default_process)


# A trailing "/denominator" is kept as part of the same token (e.g. "1/4"
# stays "1/4", not "1" + "4") -- see _numbers_match docstring for why: this
# data uses "/" for two different things (a cut-size fraction like 1/4" vs
# 1", and a pack-size multiplier like 4/5-LB), and splitting on digits alone
# lets a fraction's numerator/denominator coincidentally match unrelated
# digits elsewhere in the string.
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?(?:/\d+)?")


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
    from the review queue entirely, not just auto-merge.

    Fractions are matched as whole tokens, not split into separate digits:
    'BELL PEPPER, RED DICED 1/4" 4/5-LB' vs '... 1" 4/5-LB' is a real
    example that used to pass this gate -- a cut size of 1/4" (quarter
    inch) vs 1" (one inch) is a different product, but flattening to bare
    digits gives {1,4,5} for BOTH names (the fraction's own "1" and "4"
    coincidentally cover the same digits as the unrelated "4/5-LB" pack
    size), so the sets matched by accident. Confirmed with project owner:
    differing cut-size fractions are the same class of "almost never the
    same product" as any other differing number."""
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
# State-abbreviation additions (wa/ca/az) and "sto" were verified against
# real data: all real occurrences are asterisk/paren-wrapped location tags
# (e.g. "*WA*", "*AZ*", "(YUMA, AZ)") on UCDMC produce lines, no false
# positives found. "mexi" was considered too (project owner flagged it as a
# location tag "still getting through") but the one real occurrence found
# ("CASSEROLE MEXI FRZ") is a recipe-style descriptor ("Mexi[can]
# casserole"), the same class of false positive as Turkey/French/Italian
# above -- excluded, not added. "sto" is added as its own group since its
# exact place name is unconfirmed (possibly Stockton, CA, given the other
# examples are all Central Valley/CA produce regions) -- functions fine as
# an opaque location marker either way.
ORIGIN_GROUPS = [
    {"mexico", "mex"},
    {"california", "cali", "ca"},
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
    {"washington", "wa"},
    {"oregon", "or"},
    {"arizona", "az"},
    {"idaho"},
    {"florida"},
    {"sto"},
]
# "or" is bare English "or" the overwhelming majority of the time in this
# data (e.g. "FRYER OR FOWL", "7 OR 8 CT") -- verified against real data,
# only ~3 of 14 real hits are the Oregon location tag, and those are always
# written as "*OR*"/"*OR" (asterisk-prefixed, matching the same UCDMC
# location-tag convention as "*WA*"/"*AZ*"). Scoped to that shape
# specifically rather than the bare word, unlike the other abbreviations
# above (which had no such false-positive collision).
_ORIGIN_TERM_OVERRIDE_PATTERNS = {"or": re.compile(r"\*OR\b", re.IGNORECASE)}
_ORIGIN_PATTERNS = [
    (i, _ORIGIN_TERM_OVERRIDE_PATTERNS.get(term, re.compile(r"\b" + re.escape(term) + r"\b", re.IGNORECASE)))
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


# A hanging "(H)" tag OR the spelled-out word "Halal" both mark a
# Halal-certified product -- unlike the "(SUS)" tag (which
# _clean_for_matching() strips before scoring because it's confirmed to
# mark the SAME underlying product, just flagged for campus reporting),
# Halal is a real product distinction confirmed by the project owner: a
# Halal-certified item and its non-Halal counterpart are not the same
# product, full stop, even when the rest of the name is identical. Found
# via a real missed case: "Beef Chuck, Tail Flap Meat, Boneless" vs "...,
# Halal" was sitting in the review queue as a candidate (correctly
# rejected by the project owner) because the original "(H)"-only pattern
# didn't catch UC's supplier-spec-style naming, which spells the word out
# instead ("Beef Loin, Tri Tip, C 185C, Halal") -- verified 125 real hits,
# all genuinely meaning Halal, no collisions found.
_HALAL_TAG_RE = re.compile(r"\(H\)|\bHALAL\b", re.IGNORECASE)


def _has_halal_tag(name: str) -> bool:
    return bool(_HALAL_TAG_RE.search(name))


def _halal_match(name_a: str, name_b: str) -> bool:
    """Same treatment as _numbers_match/_origins_match: gates both
    auto-merge and the review tier, not just auto-merge, since a Halal-vs-
    not mismatch is exactly the kind of small, high-impact textual
    difference that a long-string edit-distance ratio barely penalizes."""
    return _has_halal_tag(name_a) == _has_halal_tag(name_b)


# "FZ", "FRZ", or "FR" marks a frozen product -- confirmed by project
# owner: a frozen item and its otherwise-identical non-frozen counterpart
# are not the same product. "FRZ" was checked against real data
# specifically (192 hits, all genuinely "frozen" -- prepared frozen meals
# like Alpha Foods burritos, Amy's bowls, etc., no collisions found) and is
# a much cleaner signal than bare "FR": real data shows "FR" is genuinely
# overloaded in this dataset -- it also shows up meaning "free" ("GLTN FR"
# = gluten free, "SOY FR" = soy free), not just "frozen"/"fresh". "FR" is
# kept anyway per project owner's explicit call: this gate only ever
# BLOCKS a candidate (same conservative failure mode as every gate here),
# so a "FR" that actually means "free" can only cost a missed
# auto-merge/review-queue slot, never cause an incorrect merge.
_FROZEN_TAG_RE = re.compile(r"\b(?:FZ|FRZ|FR)\b", re.IGNORECASE)


def _has_frozen_tag(name: str) -> bool:
    return bool(_FROZEN_TAG_RE.search(name))


def _frozen_match(name_a: str, name_b: str) -> bool:
    """Same treatment as _halal_match: gates both auto-merge and the
    review tier."""
    return _has_frozen_tag(name_a) == _has_frozen_tag(name_b)


# A standalone "A" or "B" is a USDA-style grade marker on potatoes in this
# data (confirmed by project owner: "POTATO, RED A 50-LB" vs "POTATO, RED
# B 50-LB" is a real, genuinely-different-product example) -- treated like
# a differing number. Deliberately scoped to potato products specifically,
# and to just the letters A/B: a generic "any standalone single letter"
# gate was tried against real data first and rejected -- the overwhelming
# majority of standalone single letters in this corpus are unrelated noise
# that would have blocked huge numbers of legitimate matches ("X" as a
# dimension separator like "1/4 X 1/4 X 2 IN", "W" for "with" ("W/SKIN"),
# "P" as one distributor's item-code prefix, "H" for "hash" ("H/BRN" =
# hash brown), etc.) -- none of which are A or B.
_POTATO_RE = re.compile(r"\bPOTATO(?:ES)?\b", re.IGNORECASE)
_GRADE_LETTER_RE = re.compile(r"\b([AB])\b")


def _potato_grade_tokens(name: str) -> frozenset[str]:
    if not _POTATO_RE.search(name):
        return frozenset()
    return frozenset(m.upper() for m in _GRADE_LETTER_RE.findall(name))


def _grade_letter_match(name_a: str, name_b: str) -> bool:
    """Same treatment as _numbers_match/_origins_match/_halal_match: gates
    both auto-merge and the review tier. Non-potato products always
    compare as a match here (empty set on both sides) -- this gate is a
    no-op outside its verified scope."""
    return _potato_grade_tokens(name_a) == _potato_grade_tokens(name_b)


# "HGL" (half gallon) and "GL"/"GAL"/"GALLON(S)" (gallon) are never the
# same product -- confirmed by project owner. Verified against real data:
# "HGL" only appears on dairy/creamer SKUs, "GL"/"GAL" only on bag-in-box
# beverage SKUs, no false-positive collisions found for either
# abbreviation. "gallon"/"gallons" spelled out belong in the same group as
# "gal"/"gl" -- an initial version treated them as distinct groups (i.e.
# "3 GAL" vs "3 GALLON" incorrectly counted as a mismatch and blocked a
# real pending candidate) until caught during the retroactive queue
# cleanup and fixed.
_VOLUME_GROUPS = [{"hgl"}, {"gl", "gal", "gallon", "gallons"}]
# Plain \bterm\b misses a unit abbreviation fused directly onto a
# preceding number with no space (e.g. "3GAL", "5GAL" -- a real pattern in
# this data for bag-in-box beverage SKUs): digits count as word characters,
# so there's no \b boundary between "3" and "GAL". Use a boundary that only
# cares about adjacent LETTERS, not digits, so "3GAL" still matches "gal"
# while "GALLON" still correctly does NOT match the bare "gal" term (there's
# a following letter, "l", so the negative lookahead fails as intended).
_VOLUME_PATTERNS = [
    (i, re.compile(r"(?<![A-Za-z])" + re.escape(term) + r"(?![A-Za-z])", re.IGNORECASE))
    for i, group in enumerate(_VOLUME_GROUPS)
    for term in group
]


def _volume_tokens(name: str) -> frozenset[int]:
    return frozenset(group_id for group_id, pattern in _VOLUME_PATTERNS if pattern.search(name))


def _volume_match(name_a: str, name_b: str) -> bool:
    """Same treatment as _origins_match: gates both auto-merge and the
    review tier."""
    return _volume_tokens(name_a) == _volume_tokens(name_b)


# "DECAF"/"DECAFFEINATED" marks a meaningfully different product from its
# caffeinated counterpart -- confirmed by project owner: presence must
# match on both sides, full stop, even when the rest of the name (e.g. a
# coffee blend name) is otherwise identical.
_DECAF_TAG_RE = re.compile(r"\bDECAF(?:FEINATED)?\b", re.IGNORECASE)


def _has_decaf_tag(name: str) -> bool:
    return bool(_DECAF_TAG_RE.search(name))


def _decaf_match(name_a: str, name_b: str) -> bool:
    """Same treatment as _halal_match/_frozen_match: gates both auto-merge
    and the review tier."""
    return _has_decaf_tag(name_a) == _has_decaf_tag(name_b)


# Spice/condiment container type is a real product distinction, not
# packaging noise -- confirmed by project owner: "JUG" and "SHAKER" are
# never the same product. Verified against real data: all four terms are
# used consistently (condiments/spices/sauces), no false-positive
# collisions found. "jar" and "bottle" were added after the retroactive
# queue cleanup for the jug/shaker gate coincidentally also caught several
# real jug-vs-jar and jug-vs-bottle mismatches (an unrecognized container
# word fell through to an empty token set, which happened to differ from
# "jug"'s set) -- promoted to their own explicit groups so a jar-vs-bottle
# mismatch (which the coincidental empty-set behavior would have missed)
# is caught too, and so the gate's behavior doesn't depend on that
# fragile coincidence. "cup"/"can"/"pouch" added after real rejected
# review-queue pairs turned up the same pattern for other categories --
# "JUICE, APPLE 100% SS CUP..." vs "...SS CAN..." (juice), "SAUCE,
# MARINARA TOMATO CHUNKY CAN..." vs "...POUCH..." -- verified against real
# data (533/225/154 hits respectively), all genuinely packaging-type
# words, no collisions found.
_CONTAINER_GROUPS = [{"jug"}, {"shaker"}, {"jar"}, {"bottle"}, {"cup"}, {"can"}, {"pouch"}]
_CONTAINER_PATTERNS = [
    (i, re.compile(r"\b" + re.escape(term) + r"\b", re.IGNORECASE))
    for i, group in enumerate(_CONTAINER_GROUPS)
    for term in group
]


def _container_tokens(name: str) -> frozenset[int]:
    return frozenset(group_id for group_id, pattern in _CONTAINER_PATTERNS if pattern.search(name))


def _container_match(name_a: str, name_b: str) -> bool:
    """Same treatment as _origins_match/_volume_match: gates both
    auto-merge and the review tier."""
    return _container_tokens(name_a) == _container_tokens(name_b)


# "DIET"/"ZERO" marks a meaningfully different soda/drink from its regular
# counterpart -- confirmed by project owner, same treatment as
# _decaf_match. Verified against real data: both terms are used
# consistently on beverage SKUs, no false-positive collisions found.
_DIET_ZERO_GROUPS = [{"diet"}, {"zero"}]
_DIET_ZERO_PATTERNS = [
    (i, re.compile(r"\b" + re.escape(term) + r"\b", re.IGNORECASE))
    for i, group in enumerate(_DIET_ZERO_GROUPS)
    for term in group
]


def _diet_zero_tokens(name: str) -> frozenset[int]:
    return frozenset(group_id for group_id, pattern in _DIET_ZERO_PATTERNS if pattern.search(name))


def _diet_zero_match(name_a: str, name_b: str) -> bool:
    """Same treatment as _decaf_match: "diet" and "zero" are both distinct
    from a regular product AND from each other (diet vs zero-sugar aren't
    interchangeable claims), so they're separate groups, not synonyms."""
    return _diet_zero_tokens(name_a) == _diet_zero_tokens(name_b)


# Color words are a real product distinction (red onion vs [unspecified]
# onion, blue vs black sprinkles) -- confirmed by project owner. Each
# color/abbreviation pair below was checked against real data first; a few
# plausible abbreviations were deliberately LEFT OUT after finding a real
# collision risk: "ORG" is overwhelmingly "organic" in this data, not
# "orange" (306 hits, e.g. "MLT AFRICAN NECTAR STITCH ORG"); "WHT" is
# genuinely ambiguous between "white" and "wheat" ("BUN HAMBURGER WHL WHT"
# reads as "whole wheat", not "whole white"); "BLU" mostly abbreviates the
# flavor "blueberry", not the color "blue" ("MUFFIN ASST BLU/APP/BAN");
# "GRAY"/"GREY" hits are proper nouns (Major Grey's Chutney, Earl Grey
# tea), not color descriptions. Excluded the same way Turkey/French/
# Italian/Canadian/Texas were excluded from ORIGIN_GROUPS: a real ambiguity
# found in the data, not a guess.
COLOR_GROUPS = [
    {"red"},
    {"green", "grn"},
    {"blue"},
    {"black", "blk"},
    {"white"},
    {"yellow", "ylw"},
    {"orange"},
    {"purple"},
    {"pink", "pnk"},
    {"brown", "brn"},
    {"gold", "golden"},
]
_COLOR_PATTERNS = [
    (i, re.compile(r"\b" + re.escape(term) + r"\b", re.IGNORECASE))
    for i, group in enumerate(COLOR_GROUPS)
    for term in group
]


def _color_tokens(name: str) -> frozenset[int]:
    return frozenset(group_id for group_id, pattern in _COLOR_PATTERNS if pattern.search(name))


def _color_match(name_a: str, name_b: str) -> bool:
    """Same treatment as _origins_match: gates both auto-merge and the
    review tier."""
    return _color_tokens(name_a) == _color_tokens(name_b)


# "Pear" and "peach" repeatedly showed up in the review queue as
# high-scoring (94-97) candidates that are never actually the same product
# (e.g. "PEACH, PUREE FROZEN SS CUP" vs "PEAR, PUREE FROZEN SS CUP") --
# every real occurrence found in the live queue had already been correctly
# rejected by the project owner one at a time, so this gate exists to stop
# the same confusion from resurfacing on future scans, same rationale as
# _origins_match (a short, high-impact word difference that a long-string
# edit-distance ratio barely penalizes).
_FRUIT_CONFUSION_GROUPS = [{"pear"}, {"peach"}]
_FRUIT_CONFUSION_PATTERNS = [
    (i, re.compile(r"\b" + re.escape(term) + r"\b", re.IGNORECASE))
    for i, group in enumerate(_FRUIT_CONFUSION_GROUPS)
    for term in group
]


def _fruit_confusion_tokens(name: str) -> frozenset[int]:
    return frozenset(group_id for group_id, pattern in _FRUIT_CONFUSION_PATTERNS if pattern.search(name))


def _fruit_confusion_match(name_a: str, name_b: str) -> bool:
    """Same treatment as _origins_match: gates both auto-merge and the
    review tier."""
    return _fruit_confusion_tokens(name_a) == _fruit_confusion_tokens(name_b)


# "PREMIUM", "CHOICE", and "FANCY" are each a real quality-tier claim, not
# interchangeable with each other or with the absence of any -- confirmed
# by project owner. Verified against real data: all three terms are used
# consistently as quality/grade descriptors (produce and USDA grades), no
# false-positive collisions found. "fancy" added after a real rejected
# review-queue pair ("JUICE, TOMATO 100% FANCY CAN..." vs "...100% CAN...")
# turned up the same pattern. Separate groups, not synonyms of each other
# (same treatment as _diet_zero_match).
_QUALITY_GROUPS = [{"premium"}, {"choice"}, {"fancy"}]
_QUALITY_PATTERNS = [
    (i, re.compile(r"\b" + re.escape(term) + r"\b", re.IGNORECASE))
    for i, group in enumerate(_QUALITY_GROUPS)
    for term in group
]


def _quality_tokens(name: str) -> frozenset[int]:
    return frozenset(group_id for group_id, pattern in _QUALITY_PATTERNS if pattern.search(name))


def _quality_match(name_a: str, name_b: str) -> bool:
    """Same treatment as _diet_zero_match: gates both auto-merge and the
    review tier."""
    return _quality_tokens(name_a) == _quality_tokens(name_b)


# "GLUTEN-FREE" and "NO MSG" are each a real dietary claim, not
# interchangeable with each other or with the absence of either --
# confirmed by project owner (same rationale as decaf/diet-zero). Found
# via real rejected review-queue pairs: "CHIP, POTATO KETTLE VINEGAR SEA
# SALT SS BAG..." vs "...GLUTEN-FREE SS BAG...", "DRESSING, VINAIGRETTE
# BALSAMIC PLASTIC JAR..." vs "...NO MSG PLASTIC JAR...". Verified against
# real data (103/46 hits), no collisions found. Matches "gluten free" or
# "gluten-free" (hyphen optional).
_DIETARY_CLAIM_GROUPS = [{"gluten[- ]free"}, {"no msg"}]
_DIETARY_CLAIM_PATTERNS = [
    (i, re.compile(r"\b" + term + r"\b", re.IGNORECASE))
    for i, group in enumerate(_DIETARY_CLAIM_GROUPS)
    for term in group
]


def _dietary_claim_tokens(name: str) -> frozenset[int]:
    return frozenset(group_id for group_id, pattern in _DIETARY_CLAIM_PATTERNS if pattern.search(name))


def _dietary_claim_match(name_a: str, name_b: str) -> bool:
    """Same treatment as _diet_zero_match/_decaf_match: gates both
    auto-merge and the review tier."""
    return _dietary_claim_tokens(name_a) == _dietary_claim_tokens(name_b)


def _all_gates_match(name_a: str, name_b: str) -> bool:
    """True only if none of the hard gates -- differing numbers, origins,
    Halal status, frozen status, potato grade letter, HGL/GL volume, decaf
    status, JUG/SHAKER/JAR/BOTTLE/CUP/CAN/POUCH container, diet/zero,
    color, the pear/peach confusion, PREMIUM/CHOICE/FANCY quality tier, or
    GLUTEN-FREE/NO-MSG dietary claim -- block treating these two names as
    comparable. Applied identically at both the auto-merge and
    review-tier checks in both find_and_merge_within_campus and
    find_and_merge_cross_campus."""
    return (
        _numbers_match(name_a, name_b)
        and _origins_match(name_a, name_b)
        and _halal_match(name_a, name_b)
        and _frozen_match(name_a, name_b)
        and _grade_letter_match(name_a, name_b)
        and _volume_match(name_a, name_b)
        and _decaf_match(name_a, name_b)
        and _container_match(name_a, name_b)
        and _diet_zero_match(name_a, name_b)
        and _color_match(name_a, name_b)
        and _fruit_confusion_match(name_a, name_b)
        and _quality_match(name_a, name_b)
        and _dietary_claim_match(name_a, name_b)
    )


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


def _existing_candidate_pairs(conn: sqlite3.Connection) -> set[tuple[int, int]]:
    """Loads every (product_id_a, product_id_b) pair already present in
    product_match_candidates -- any status (pending/approved/rejected) all
    count as "already known" -- into a set once, so callers can check
    membership in-memory instead of issuing one SQL query per candidate
    pair. Rows are always stored with product_id_a < product_id_b (an
    invariant both find_and_merge_within_campus and
    find_and_merge_cross_campus maintain), so callers must sort their own
    pair the same way before checking/inserting.

    Without this check, re-running either find_and_merge_* function --  a
    normal, expected operation (e.g. after re-ingestion adds new products)
    -- re-inserts a fresh 'pending' row for every candidate pair ever seen,
    including pairs a human already approved/rejected. This inflated the
    live review queue twice in one session before the fix (707->1128 on a
    within-campus re-run, +39 more after a cross-campus run) -- both had to
    be manually cleaned up; see lib.dedupe_stale_candidates for a one-time
    repair tool for a database that accumulated duplicates before this
    fix existed."""
    return {
        (row[0], row[1])
        for row in conn.execute("SELECT product_id_a, product_id_b FROM product_match_candidates").fetchall()
    }


def find_and_merge_within_campus(
    conn: sqlite3.Connection,
    campus: str,
    cert_lookup: list[tuple[str, str, list[str]]],
    campus_type: str,
) -> dict:
    """Runs auto-merge + candidate generation for one campus. Returns
    counts for reporting.

    Also requires (both tiers -- same rationale as find_and_merge_cross_campus)
    that the two products share the same campus-reported `sustainable_yn`.
    This is what correctly resolves a "(SUS)"-suffix pair here too:
    `_clean_for_matching()` still strips "(SUS)" for scoring, but the merge
    itself is blocked unless `sustainable_yn` actually agrees on both
    sides -- e.g. UC Davis's "SANDWICH FRESH ZESTY TURKEY WRAP 9.1 OZ (SUS)"
    vs "...9.1OZ" (no space) are both sustainable_yn='N', so they qualify;
    a hypothetical pair where one side is genuinely 'Y' and the other 'N'
    would not, regardless of how similar the text scores."""
    df = pd.read_sql_query(
        "SELECT p.product_id, p.canonical_name, p.sustainable_yn, pu.vendor FROM products p "
        "JOIN purchases pu ON pu.product_id = p.product_id WHERE pu.campus = ?",
        conn,
        params=(campus,),
    )
    df["vendor_key"] = df["vendor"].apply(normalize_vendor)

    existing_pairs = _existing_candidate_pairs(conn)

    auto_merged = 0
    candidates_created = 0

    for vendor_key, group in df.groupby("vendor_key"):
        if vendor_key is None or len(group) < 2:
            continue
        ids = group["product_id"].tolist()
        names = group["canonical_name"].tolist()
        sustainable_yns = group["sustainable_yn"].tolist()
        scores = _score_matrix(names)

        uf = _UnionFind(ids)
        n = len(ids)
        for i in range(n):
            for j in range(i + 1, n):
                if (
                    scores[i][j] >= AUTO_MERGE_THRESHOLD
                    and _all_gates_match(names[i], names[j])
                    and sustainable_yns[i] == sustainable_yns[j]
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
            surviving_ids, surviving_names, surviving_sus = ids, names, sustainable_yns
        else:
            surviving_ids, surviving_names, surviving_sus = [], [], []
            for pid, name, sus in zip(ids, names, sustainable_yns):
                if pid in redirected:
                    continue
                surviving_ids.append(pid)
                surviving_names.append(name)
                surviving_sus.append(sus)
            scores = _score_matrix(surviving_names) if surviving_names else scores

        m = len(surviving_ids)
        for i in range(m):
            for j in range(i + 1, m):
                score = scores[i][j]
                # Unlike an earlier version, numbers/origin/halal guards now
                # also gate the review tier, not just auto-merge -- confirmed
                # with project owner after manually reviewing ~540 real
                # candidates that a differing quantity or cited origin is
                # "almost never" actually the same product, so it's not
                # worth the review-queue noise (was ~35% of pending
                # candidates, differing numbers alone).
                if (
                    score >= REVIEW_THRESHOLD
                    and _all_gates_match(surviving_names[i], surviving_names[j])
                    and surviving_sus[i] == surviving_sus[j]
                ):
                    a, b = sorted((surviving_ids[i], surviving_ids[j]))
                    if (a, b) in existing_pairs:
                        # Already known -- pending, approved, or rejected --
                        # from a prior run. See _existing_candidate_pairs.
                        continue
                    conn.execute(
                        "INSERT INTO product_match_candidates (campus, campus_a, campus_b, product_id_a, "
                        "product_id_b, match_score, status) VALUES (?, ?, ?, ?, ?, ?, 'pending')",
                        (campus, campus, campus, a, b, float(score)),
                    )
                    existing_pairs.add((a, b))
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

    Also requires (both tiers, not just auto-merge -- confirmed by project
    owner, same rationale as the number/origin/etc. gates: a mismatch here
    means "almost never the same product", so it's not worth the
    review-queue noise) that the two products share the same normalized
    vendor/distributor (`normalize_vendor()`, the same equality gate
    within-campus matching already uses) and the same campus-reported
    `sustainable_yn`. The `sustainable_yn` check is what correctly resolves
    a "(SUS)"-suffix pair: `_clean_for_matching()` still strips "(SUS)" for
    scoring purposes (that's about text noise), but if one side is actually
    sustainable_yn='Y' and the other is 'N'/'NA', this gate blocks them
    regardless of how similar the text scores -- they're two genuinely
    different purchasing lines, not the same product re-tagged. Two
    "(SUS)"-suffixed products that are BOTH sustainable_yn='Y' still merge
    fine, same as before.
    """
    products = pd.read_sql_query(
        "SELECT p.product_id, p.canonical_name, p.simap_category, p.sustainable_yn, "
        "pa.campus, c.campus_type, pu.vendor "
        "FROM products p JOIN product_aliases pa ON pa.product_id = p.product_id "
        "JOIN campuses c ON c.campus = pa.campus "
        "LEFT JOIN purchases pu ON pu.product_id = p.product_id AND pu.campus = pa.campus "
        "WHERE p.simap_category IS NOT NULL",
        conn,
    )
    # A product could in principle have multiple aliases at the same campus
    # post within-campus merging; campus/campus_type are consistent across
    # a product's aliases since cross-campus merging hasn't run yet. The
    # purchases join can in principle produce more than one row per
    # (product_id, campus) (e.g. multiple fiscal years) -- drop_duplicates
    # arbitrarily keeps the first vendor found, same pattern used elsewhere
    # in this codebase (e.g. lib.ingestion.insert_product_and_purchase).
    products = products.drop_duplicates(subset="product_id")
    products["vendor_key"] = products["vendor"].apply(normalize_vendor)

    existing_pairs = _existing_candidate_pairs(conn)

    auto_merged = 0
    candidates_created = 0

    for category, group in products.groupby("simap_category"):
        if len(group) < 2:
            continue
        ids = group["product_id"].tolist()
        names = group["canonical_name"].tolist()
        campuses = group["campus"].tolist()
        campus_types = group["campus_type"].tolist()
        vendor_keys = group["vendor_key"].tolist()
        sustainable_yns = group["sustainable_yn"].tolist()
        scores = _score_matrix(names)

        uf = _UnionFind(ids)
        n = len(ids)
        for i in range(n):
            for j in range(i + 1, n):
                if campuses[i] == campuses[j]:
                    continue  # same-campus pairs are within-campus matching's job
                if (
                    scores[i][j] >= AUTO_MERGE_THRESHOLD
                    and _all_gates_match(names[i], names[j])
                    and campus_types[i] == campus_types[j]
                    and vendor_keys[i] is not None
                    and vendor_keys[i] == vendor_keys[j]
                    and sustainable_yns[i] == sustainable_yns[j]
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
            surviving_ids, surviving_names, surviving_campuses, surviving_vendor_keys, surviving_sus = (
                ids, names, campuses, vendor_keys, sustainable_yns
            )
        else:
            surviving_ids, surviving_names, surviving_campuses, surviving_vendor_keys, surviving_sus = (
                [], [], [], [], []
            )
            for pid, name, camp, vkey, sus in zip(ids, names, campuses, vendor_keys, sustainable_yns):
                if pid in redirected:
                    continue
                surviving_ids.append(pid)
                surviving_names.append(name)
                surviving_campuses.append(camp)
                surviving_vendor_keys.append(vkey)
                surviving_sus.append(sus)
            scores = _score_matrix(surviving_names) if surviving_names else scores

        m = len(surviving_ids)
        for i in range(m):
            for j in range(i + 1, m):
                if surviving_campuses[i] == surviving_campuses[j]:
                    continue
                score = scores[i][j]
                if (
                    score >= REVIEW_THRESHOLD
                    and _all_gates_match(surviving_names[i], surviving_names[j])
                    and surviving_vendor_keys[i] is not None
                    and surviving_vendor_keys[i] == surviving_vendor_keys[j]
                    and surviving_sus[i] == surviving_sus[j]
                ):
                    a_id, b_id = surviving_ids[i], surviving_ids[j]
                    a_campus, b_campus = surviving_campuses[i], surviving_campuses[j]
                    # Keep product_id_a/product_id_b sorted for consistency
                    # with within-campus rows (merge_products() and the
                    # self-reference cleanup rely on this ordering elsewhere).
                    if a_id > b_id:
                        a_id, b_id = b_id, a_id
                        a_campus, b_campus = b_campus, a_campus
                    if (a_id, b_id) in existing_pairs:
                        # Already known -- pending, approved, or rejected --
                        # from a prior run. See _existing_candidate_pairs.
                        continue
                    conn.execute(
                        "INSERT INTO product_match_candidates (campus, campus_a, campus_b, product_id_a, "
                        "product_id_b, match_score, status) VALUES (?, ?, ?, ?, ?, ?, 'pending')",
                        (a_campus, a_campus, b_campus, a_id, b_id, float(score)),
                    )
                    existing_pairs.add((a_id, b_id))
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
