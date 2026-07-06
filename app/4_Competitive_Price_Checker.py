"""Phase 8: Competitive Price Checker page.

CLAUDE.md's exact spec: "tests whether a hypothetical new item would be
cost-competitive enough to enter a campus's optimized purchasing plan."
Rebuild of the legacy R Shiny app's "Hypothetical Proteins" tab
(legacy/optimization/app.R, optimization_backend.R) -- deliberately left out
of app/1_Campus_Roadmap.py's three scenarios for this page instead.

Unlike a simple "is this $/lb cheaper than what we buy now" comparison,
this genuinely re-runs the real optimizer (lib.optimization.
solve_hypothetical_item_check) with the hypothetical injected as a new,
capped-supply sourcing option for one SIMAP category, and reports whether
the solver actually chose to use it -- and how much. A hypothetical can
lose even at a great price if its category or food group is already
pinned near its +/-15% band, and can win even at a middling price if the
scenario's sustainability target needs the extra spend it provides.

Standalone for now, like the other pages in this rebuild -- run directly
with `streamlit run app/4_Competitive_Price_Checker.py`. Reuses the same
st.session_state["selected_campus"] key as the other pages.
"""

import sqlite3
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.db import DEFAULT_DB_PATH
from lib.optimization import (
    CATEGORY_LOWER_MULTIPLIER_DEFAULT,
    CATEGORY_UPPER_MULTIPLIER_DEFAULT,
    GROUP_LOWER_MULTIPLIER_DEFAULT,
    GROUP_UPPER_MULTIPLIER_DEFAULT,
    HypotheticalItem,
    InfeasibleScenarioError,
    build_category_baseline,
    solve_hypothetical_item_check,
)

st.set_page_config(page_title="Competitive Price Checker", layout="wide")


@st.cache_resource
def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DEFAULT_DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@st.cache_data(show_spinner=False)
def _load_baseline(campus: str, fiscal_year: int) -> pd.DataFrame:
    return build_category_baseline(get_conn(), campus, fiscal_year)


def _fmt_currency(x: float) -> str:
    return f"${x:,.2f}" if pd.notna(x) else "N/A"


def _fmt_weight(x: float) -> str:
    return f"{x:,.1f} lbs" if pd.notna(x) else "N/A"


def _render_verdict(result, hypothetical: HypotheticalItem, baseline_row: pd.Series) -> None:
    cat = hypothetical.simap_category
    row = result.category_results[result.category_results["simap_category"] == cat].iloc[0]
    adopted_lbs = row["hypothetical_weight_lbs"]
    has_cap = hypothetical.max_weight_lbs is not None
    cap_pct = (adopted_lbs / hypothetical.max_weight_lbs * 100) if has_cap and hypothetical.max_weight_lbs else 0.0

    if adopted_lbs > 0:
        supply_note = (
            f"({cap_pct:.0f}% of the {_fmt_weight(hypothetical.max_weight_lbs)} you said it could supply) "
            if has_cap
            else "(you didn't set a supply limit, so this reflects the most the category/food-group bands allow) "
        )
        st.success(
            f"**Yes — cost-competitive.** The optimizer chose to use {_fmt_weight(adopted_lbs)} of this item "
            f"{supply_note}in **{cat}**, given everything else it has to balance (the campus's total food "
            f"volume, the {cat} food group's own band, and this scenario's cost target)."
        )
    else:
        st.warning(
            f"**No — not competitive enough, as priced.** The optimizer found nothing worth using this item "
            f"for in **{cat}**: existing options already meet the scenario's targets at least as well at this "
            "price."
        )

    col1, col2 = st.columns(2)
    col1.metric("Adopted", _fmt_weight(adopted_lbs))
    col2.metric("Share of supply cap used", f"{cap_pct:.0f}%" if has_cap else "No cap set")

    col3, col4 = st.columns(2)
    sus_price = baseline_row["sustainable_price_per_lb"]
    conv_price = baseline_row["conventional_price_per_lb"]
    col3.metric(
        "Current avg. sustainable price",
        f"{_fmt_currency(sus_price)}/lb" if pd.notna(sus_price) else "N/A",
        delta=f"hypothetical is {hypothetical.price_per_lb / sus_price:.2f}x this" if pd.notna(sus_price) and sus_price > 0 else None,
        delta_color="off",
    )
    col4.metric(
        "Current avg. conventional price",
        f"{_fmt_currency(conv_price)}/lb" if pd.notna(conv_price) else "N/A",
        delta=f"hypothetical is {hypothetical.price_per_lb / conv_price:.2f}x this" if pd.notna(conv_price) and conv_price > 0 else None,
        delta_color="off",
    )


def main() -> None:
    conn = get_conn()
    st.title("Competitive Price Checker")
    st.markdown(
        "Test whether a hypothetical new item -- a potential new sustainable (or conventional) supplier you're "
        "considering -- would actually get chosen by the optimizer, given everything else it already has to "
        "balance. **This re-runs the real optimization** with your item injected as a new, capped-supply "
        "option; it's not a simple price comparison, so a great price can still lose if there's no room left "
        "in that category's or food group's band, and a middling price can still win if it helps meet a "
        "sustainability target."
    )

    campuses = [r[0] for r in conn.execute("SELECT campus FROM campuses ORDER BY campus").fetchall()]
    default_campus = "UC Davis" if "UC Davis" in campuses else campuses[0]

    with st.sidebar:
        st.header("Settings")
        campus = st.selectbox(
            "Campus", campuses, index=campuses.index(st.session_state.get("selected_campus", default_campus))
        )
        st.session_state["selected_campus"] = campus
        fiscal_year = st.number_input("Fiscal year", value=2025, step=1)

        st.divider()
        cost_reduction_pct = st.slider(
            "Cost-reduction target (%) -- how hard to press on cost while testing this item", 0, 50, 0
        )
        category_pct = st.slider(
            "Max % change allowed per SIMAP category", 0, 100, int((1 - CATEGORY_LOWER_MULTIPLIER_DEFAULT) * 100)
        )
        group_pct = st.slider(
            "Max % change allowed per food group", 0, 100, int((1 - GROUP_LOWER_MULTIPLIER_DEFAULT) * 100)
        )

    try:
        baseline_df = _load_baseline(campus, fiscal_year)
    except ValueError as e:
        st.error(str(e))
        return

    eligible_categories = sorted(baseline_df.loc[baseline_df["baseline_weight_lbs"] > 0, "simap_category"].unique())
    if not eligible_categories:
        st.warning(f"{campus} has no categories with a resolved baseline weight to compare a hypothetical item against.")
        return

    st.subheader("Describe the hypothetical item")
    col1, col2 = st.columns(2)
    item_name = col1.text_input("Item name (for your reference only)", "New hypothetical item")
    simap_category = col2.selectbox("Food Category (from SIMAP)", eligible_categories)

    col3, col4, col5 = st.columns(3)
    price_per_lb = col3.number_input("Price per lb ($)", min_value=0.0, value=1.0, step=0.1, format="%.2f")
    with col4:
        has_cap = not st.checkbox("Unlimited supply (no cap)", value=True)
        max_weight_lbs = (
            st.number_input(
                "Maximum weight (lbs) this supplier could realistically provide",
                min_value=0.0,
                value=100.0,
                step=10.0,
            )
            if has_cap
            else None
        )
    is_sustainable = col5.checkbox("Sustainable Item per AASHE STARS and/or PGH", value=True)

    baseline_row = baseline_df[baseline_df["simap_category"] == simap_category].iloc[0]
    st.caption(
        f"For context, **{simap_category}** currently prices sustainable at "
        f"{_fmt_currency(baseline_row['sustainable_price_per_lb'])}/lb and conventional at "
        f"{_fmt_currency(baseline_row['conventional_price_per_lb'])}/lb."
    )

    run_clicked = st.button("Test This Item", type="primary", use_container_width=True)

    state_key = f"hyp_result_{campus}_{fiscal_year}"
    if run_clicked:
        hypothetical = HypotheticalItem(
            simap_category=simap_category,
            price_per_lb=price_per_lb,
            max_weight_lbs=max_weight_lbs,
            is_sustainable=is_sustainable,
        )
        try:
            result = solve_hypothetical_item_check(
                baseline_df,
                hypothetical,
                cost_reduction_target=cost_reduction_pct / 100,
                category_lower_multiplier=1 - category_pct / 100,
                category_upper_multiplier=1 + category_pct / 100,
                group_lower_multiplier=1 - group_pct / 100,
                group_upper_multiplier=1 + group_pct / 100,
            )
            st.session_state[state_key] = (item_name, hypothetical, result)
        except InfeasibleScenarioError as e:
            st.error(str(e))
            st.session_state.pop(state_key, None)

    stored = st.session_state.get(state_key)
    if stored is None:
        return
    stored_name, stored_hypothetical, result = stored
    if stored_hypothetical.simap_category != simap_category:
        st.info("Settings changed since the last test -- click \"Test This Item\" to re-run with the new inputs.")
        return

    st.divider()
    st.subheader(f"Results for “{stored_name}”")
    baseline_row = baseline_df[baseline_df["simap_category"] == stored_hypothetical.simap_category].iloc[0]
    _render_verdict(result, stored_hypothetical, baseline_row)

    with st.expander("Full scenario totals"):
        t = result.totals
        st.write(
            {
                "Baseline cost": _fmt_currency(t["baseline_cost"]),
                "Optimized cost (with hypothetical)": _fmt_currency(t["optimized_cost"]),
                "Baseline sustainable spend share": f"{t['baseline_sustainable_pct'] * 100:.1f}%",
                "Optimized sustainable spend share": f"{t['optimized_sustainable_pct'] * 100:.1f}%",
            }
        )


if __name__ == "__main__":
    main()
