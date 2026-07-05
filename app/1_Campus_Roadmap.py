"""Phase 5: Campus Roadmap page.

Runs the optimization engine (lib.optimization, Phase 4) against a
campus's current purchasing data and lets the user explore three
scenarios, mirroring the legacy R Shiny app's tabs
(legacy/optimization/app.R) minus "Hypothetical Proteins" -- that's
CLAUDE.md's Phase 8 (Competitive Price Checker), a separate future page:

  - Min Spend, keeping sustainable spend at or above baseline.
  - Max Sustainable Spend, keeping cost at or below baseline.
  - Threshold-then-maximize: locks in only 1/3 of whatever cost cut
    Scenario 1 found achievable (project owner's exact spec -- e.g.
    Scenario 1 finds 9% possible, Scenario 3 commits to 3%), then
    maximizes sustainable spend under that more conservative cap.

Reallocation is bounded two ways (added this session, per project owner
direction): each SIMAP category may only move +/-20% from its own
baseline weight, AND the total weight within each food group (see
reference/food_groups.csv -- e.g. all meat/poultry/seafood/plant-protein
together) is held fixed, so cutting beef can only shift weight to a
reasonable substitute (chicken, tofu) within its own group, never to an
unrelated category like apples.

Also generates the PDF report (lib.pdf_report) for the current snapshot
and/or whichever scenario was last run.

Standalone for now -- run directly with `streamlit run app/1_Campus_Roadmap.py`.
This is the first page needing cross-tab state, so it introduces
st.session_state["selected_campus"] (CLAUDE.md's "global campus dropdown"),
for future pages to share once a shared multi-tab shell exists.
"""

import sqlite3
import sys
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.db import DEFAULT_DB_PATH
from lib.optimization import (
    CATEGORY_LOWER_MULTIPLIER_DEFAULT,
    CATEGORY_UPPER_MULTIPLIER_DEFAULT,
    GROUP_LOWER_MULTIPLIER_DEFAULT,
    GROUP_UPPER_MULTIPLIER_DEFAULT,
    InfeasibleScenarioError,
    build_category_baseline,
    identify_category_movers,
    snapshot_totals,
    solve_max_sustainable_keep_cost,
    solve_min_spend_keep_sustainability,
    solve_threshold_third_of_scenario1,
)
from lib.pdf_report import generate_pdf_report

st.set_page_config(page_title="Campus Roadmap", layout="wide")

SCENARIOS = {
    "Min Spend (keep sustainability floor)": "min_spend",
    "Max Sustainable Spend (keep cost cap)": "max_sustainable",
    "Threshold (1/3 of Scenario 1) then Maximize": "threshold_third",
}


@st.cache_resource
def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DEFAULT_DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _fmt_currency(x: float) -> str:
    return f"${x:,.0f}" if pd.notna(x) else "N/A"


def _fmt_pct(x: float) -> str:
    return f"{x:.1f}%" if pd.notna(x) else "N/A"


def _fmt_pct_from_fraction(x: float) -> str:
    return _fmt_pct(x * 100) if pd.notna(x) else "N/A"


@st.cache_data(show_spinner=False)
def _load_baseline(campus: str, fiscal_year: int) -> pd.DataFrame:
    conn = get_conn()
    return build_category_baseline(conn, campus, fiscal_year)


def _render_snapshot(baseline_df: pd.DataFrame, campus: str, fiscal_year: int) -> None:
    st.subheader(f"Current Purchasing Snapshot — {campus}, FY{fiscal_year}")
    totals = snapshot_totals(baseline_df)

    col1, col2, col3 = st.columns(3)
    col1.metric("Total Spend", _fmt_currency(totals["total_spend"]))
    col2.metric("Sustainable Spend", _fmt_pct_from_fraction(totals["sustainable_pct"]))
    col3.metric("GHG-Equivalent Emissions", f"{totals['total_ghg_metric_tons']:,.1f} MT CO2e")

    if pd.notna(totals["weight_resolved_pct"]) and totals["weight_resolved_pct"] < 0.95:
        st.caption(
            f"⚠️ Only {_fmt_pct_from_fraction(totals['weight_resolved_pct'])} of classified spend has a "
            "resolved weight -- GHG totals reflect only that resolved portion."
        )
    if totals["excluded_unclassified_spend"] > 0:
        st.caption(
            f"{_fmt_currency(totals['excluded_unclassified_spend'])} of spend is on products with no SIMAP "
            "category assigned yet, and is excluded from the category breakdown below."
        )
    if totals["price_outlier_count"] > 0:
        st.caption(
            f"⚠️ {totals['price_outlier_count']} line item(s) totaling {_fmt_currency(totals['price_outlier_spend'])} "
            "have an implausible reported/computed weight (e.g. a distributor data error) and are excluded from "
            "weight-based math below, though their spend still counts above."
        )


def _render_key_takeaways(result) -> None:
    """The main finding this whole page exists to surface: which SIMAP
    categories the solver shifts toward (or away from) sustainable spend,
    and -- critically -- whether that shift is a "free" cost-neutral-or-
    better win or a premium the scenario's objective is paying for.
    identify_category_movers() already excludes categories with no real
    baseline choice and below-materiality spend, so everything rendered
    here is a genuine, worth-discussing reallocation."""
    movers = identify_category_movers(result.category_results)
    if movers.empty:
        return

    gainers = movers[movers["delta_pts"] > 1].head(5)
    losers = movers[movers["delta_pts"] < -1].sort_values("delta_pts").head(5)
    if gainers.empty and losers.empty:
        return

    st.markdown("#### Key takeaways")

    def _bullet(row) -> str:
        if row["cost_neutral_or_better"]:
            price_note = (
                f"the sustainable option costs about the same or less per pound "
                f"(\\${row['sustainable_price_per_lb']:.2f} vs \\${row['conventional_price_per_lb']:.2f}/lb) -- a free win"
            )
        else:
            price_note = (
                f"the sustainable option costs {row['price_ratio_sus_to_conv']:.2f}x as much per pound "
                f"(\\${row['sustainable_price_per_lb']:.2f} vs \\${row['conventional_price_per_lb']:.2f}/lb) -- this change "
                "is driven by this scenario's targets, not because it's cheaper"
            )
        return (
            f"- **{row['simap_category']}** ({row['food_group']}): "
            f"{row['baseline_sustainable_pct']:.0f}% → {row['optimized_sustainable_pct']:.0f}% sustainable "
            f"({row['delta_pts']:+.0f} pts) — {price_note}"
        )

    if not gainers.empty:
        n_free_wins = int(gainers["cost_neutral_or_better"].sum())
        st.markdown("**Moving toward more sustainable purchasing:**\n" + "\n".join(_bullet(r) for _, r in gainers.iterrows()))
        if n_free_wins == len(gainers):
            rec = (
                f"All {n_free_wins} of these can become more sustainable at no extra cost -- they're the easiest "
                "place to start."
            )
        elif n_free_wins == 0:
            rec = (
                "None of these are free wins -- each increase happens because this scenario's targets call for "
                "it, not because the sustainable option is cheaper. Going further here would cost more."
            )
        else:
            rec = (
                f"{n_free_wins} of these {len(gainers)} categories can become more sustainable at no extra cost -- "
                "start there. The rest cost more per pound, so pushing them further depends on budget, not price."
            )
        st.caption(rec)

    if not losers.empty:
        st.markdown("**Moving toward more conventional purchasing:**\n" + "\n".join(_bullet(r) for _, r in losers.iterrows()))
        st.caption(
            "These categories shift toward the conventional option to help pay for the gains above -- usually "
            "because the sustainable version costs noticeably more per pound here."
        )


def _render_scenario_results(result) -> None:
    t = result.totals
    st.subheader(f"Scenario Results — {result.scenario_name}")
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Total Cost", _fmt_currency(t["optimized_cost"]), delta=f"{t['cost_pct_change']:+.1f}%")
    # Campuses care about the resulting sustainable-spend SHARE, not just
    # how much it changed -- this is the headline number now (was
    # "Sustainable Spend Change" / a %-change figure), per project owner
    # direction. The point change is still shown, just as the secondary
    # delta rather than the primary number.
    delta_pp = (t["optimized_sustainable_pct"] - t["baseline_sustainable_pct"]) * 100
    col2.metric(
        "Sustainable Spend Share",
        _fmt_pct_from_fraction(t["optimized_sustainable_pct"]),
        delta=f"{delta_pp:+.1f} pts vs. {_fmt_pct_from_fraction(t['baseline_sustainable_pct'])} status quo",
    )
    col3.metric("Sustainable Spend $", _fmt_currency(t["optimized_sustainable_spend"]), delta=f"{t['sustainable_spend_pct_change']:+.1f}%")
    col4.metric("GHG Change", f"{t['ghg_pct_change']:+.1f}%")

    _render_key_takeaways(result)

    display_df = result.category_results.copy()
    # How many times as much (or as little) sustainable costs per lb vs.
    # conventional, in the SAME category -- the same price_ratio_sus_to_conv
    # driving identify_category_movers()'s takeaways above, just shown for
    # every category here, not just the top movers. Shown as a multiplier
    # (2.00x, 0.85x) rather than a % increase -- easier to read at a glance
    # for a non-technical audience than "+100%"/"-15%". NaN when one side
    # had no baseline purchases to price -- there's no real ratio to report
    # without a real conventional (or sustainable) comparison.
    display_df["price_ratio_display"] = display_df["sustainable_price_per_lb"] / display_df["conventional_price_per_lb"]
    display_df["price_ratio_display"] = display_df["price_ratio_display"].apply(
        lambda x: f"{x:.2f}x" if pd.notna(x) else "N/A"
    )
    display_df["baseline_spend"] = display_df["baseline_spend"].apply(_fmt_currency)
    display_df["optimized_spend"] = display_df["optimized_spend"].apply(_fmt_currency)
    display_df["weight_pct_change"] = display_df["weight_pct_change"].apply(
        lambda x: f"{x:+.1f}%" if pd.notna(x) else "N/A"
    )
    st.dataframe(
        display_df[
            ["simap_category", "baseline_spend", "optimized_spend", "weight_pct_change", "price_ratio_display"]
        ].rename(
            columns={
                "simap_category": "Category",
                "baseline_spend": "Baseline Spend",
                "optimized_spend": "Optimized Spend",
                "weight_pct_change": "Weight % Change",
                "price_ratio_display": "Sustainable Price vs. Conventional",
            }
        ),
        use_container_width=True,
        hide_index=True,
    )

    _render_food_group_charts(result)


def _render_food_group_charts(result) -> None:
    """One figure per food group with more than one SIMAP category (the
    real substitution umbrellas -- Protein, Dairy & Milk Alternatives,
    Vegetables, Fruits, Grains & Starches, Oils & Fats). Singleton groups
    (Eggs, Cheese, Yogurt, Ice cream, etc.) have nothing to compare
    within-group, so they're skipped here -- their numbers are already in
    the table above. Each figure: baseline vs. optimized spend bars per
    category, each bar stacked sustainable/conventional to the category's
    total spend, with % sustainable labeled on top -- per project owner's
    reference image."""
    merged = result.category_results
    group_sizes = merged.groupby("food_group")["simap_category"].nunique()
    multi_groups = sorted(group_sizes[group_sizes > 1].index.tolist())
    if not multi_groups:
        return

    st.subheader("Spend by Food Group — Baseline vs. Optimized")
    st.markdown(
        "Food groups are culinary-substitution umbrellas (e.g. all animal- and plant-based protein together) -- "
        "the optimizer can shift dollars between categories *inside* a group (beef to chicken) but never across "
        "unrelated groups (beef to apples). Each SIMAP category below gets **two bars**: a lighter, left-hand bar "
        "for **today's actual spend (Baseline)** and a solid, right-hand bar for **this scenario's recommended "
        "spend (Optimized)** -- hover either bar for its exact stage if the legend/position isn't enough. Within "
        "each bar, green is the portion spent on validated-sustainable products and tan is conventional; the % "
        "above each bar is that bar's sustainable share of spend, so you can read at a glance whether this "
        "scenario is raising or lowering a category's sustainable share, not just its total dollars."
    )

    _STAGE_OPACITY = {"Baseline": 0.5, "Optimized": 1.0}

    for food_group in multi_groups:
        group_df = merged[merged["food_group"] == food_group]
        long_rows, label_rows = [], []
        for row in group_df.itertuples():
            for stage, spend, sus_spend, sus_pct in [
                ("Baseline", row.baseline_spend, row.baseline_sustainable_spend, row.baseline_sustainable_pct),
                ("Optimized", row.optimized_spend, row.optimized_sustainable_spend, row.optimized_sustainable_pct),
            ]:
                conv_spend = spend - sus_spend
                long_rows.append(
                    {"category": row.simap_category, "stage": stage, "spend_type": "Sustainable", "spend": sus_spend}
                )
                long_rows.append(
                    {"category": row.simap_category, "stage": stage, "spend_type": "Conventional", "spend": conv_spend}
                )
                label_rows.append(
                    {
                        "category": row.simap_category,
                        "stage": stage,
                        "total_spend": spend,
                        "label": f"{sus_pct:.1f}%" if pd.notna(sus_pct) else "N/A",
                    }
                )

        long_df = pd.DataFrame(long_rows)
        label_df = pd.DataFrame(label_rows)
        n_categories = group_df["simap_category"].nunique()
        chart_width = max(500, 110 * n_categories)

        bars = (
            alt.Chart(long_df)
            .mark_bar()
            .encode(
                x=alt.X("category:N", title=None, axis=alt.Axis(labelAngle=-40, labelOverlap=False)),
                xOffset=alt.XOffset("stage:N", sort=["Baseline", "Optimized"]),
                y=alt.Y("spend:Q", title="Spend ($)"),
                color=alt.Color(
                    "spend_type:N",
                    title="Spend type",
                    sort=["Sustainable", "Conventional"],
                    scale=alt.Scale(domain=["Sustainable", "Conventional"], range=["#2E6F57", "#D2B48C"]),
                ),
                opacity=alt.Opacity(
                    "stage:N",
                    title="Stage",
                    sort=["Baseline", "Optimized"],
                    scale=alt.Scale(domain=["Baseline", "Optimized"], range=[_STAGE_OPACITY["Baseline"], _STAGE_OPACITY["Optimized"]]),
                ),
                order=alt.Order("spend_type:N", sort="ascending"),
                tooltip=[
                    alt.Tooltip("category:N", title="Category"),
                    alt.Tooltip("stage:N", title="Stage"),
                    alt.Tooltip("spend_type:N", title="Spend type"),
                    alt.Tooltip("spend:Q", title="Spend ($)", format=",.0f"),
                ],
            )
        )
        labels = (
            alt.Chart(label_df)
            .mark_text(dy=-6, fontSize=10)
            .encode(
                x=alt.X("category:N"),
                xOffset=alt.XOffset("stage:N", sort=["Baseline", "Optimized"]),
                y=alt.Y("total_spend:Q"),
                text="label:N",
            )
        )
        st.altair_chart(
            (bars + labels).properties(title=food_group, width=chart_width, height=320), use_container_width=False
        )
        st.caption(f"{food_group}: left/lighter bar = Baseline, right/solid bar = Optimized, per category above.")


def main() -> None:
    conn = get_conn()
    st.title("Campus Roadmap")
    st.caption("🧪 Beta -- a planning tool to support conversations with your purchasing team, not a final decision.")
    st.markdown(
        "This page models three ways a campus could shift its food purchasing toward more certified-sustainable "
        "options (AASHE STARS for academic campuses, Practice Greenhealth for health systems), starting from that "
        "campus's actual purchasing data for the year:\n\n"
        "- **Min Spend** -- the cheapest way to buy the same amount of food while keeping at least today's level "
        "of sustainable spending.\n"
        "- **Max Sustainable Spend** -- the most sustainable mix of purchases possible without spending more than "
        "today.\n"
        "- **Threshold (1/3 of Scenario 1) then Maximize** -- a smaller, more conservative version of Min Spend's "
        "savings, with the rest of that budget redirected toward sustainable purchasing.\n\n"
        "To keep recommendations realistic, the model only swaps foods for close substitutes (beef for chicken, "
        "not beef for apples) and limits how much any single food or food group can shift -- 15% by default, "
        "adjustable in the sidebar. It won't suggest overhauling a kitchen's purchasing overnight."
    )
    st.caption(
        "Technical note: \"sustainable\" here always means AASHE STARS / Practice Greenhealth certification "
        "status (validated_sustainable_yn), never SIMAP category membership -- SIMAP-57 is used only to group "
        "similar foods and estimate greenhouse-gas impact. The numbers below come from real purchasing records, "
        "which aren't perfect: some purchases are missing a category or a weight, and a small number of corrupted "
        "entries (e.g. distributor data errors) are automatically detected and excluded -- both are called out "
        "below wherever they apply."
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
        scenario_label = st.radio("Scenario", list(SCENARIOS.keys()))
        scenario = SCENARIOS[scenario_label]
        if scenario == "threshold_third":
            st.caption("Cost-reduction target is derived automatically -- 1/3 of Scenario 1's achievable cut.")

        st.caption("Category and food-group bounds nest inside a fixed global total weight (see module docstring).")
        category_pct = st.slider(
            "Max % change allowed per SIMAP category", 0, 100, int((1 - CATEGORY_LOWER_MULTIPLIER_DEFAULT) * 100)
        )
        group_pct = st.slider(
            "Max % change allowed per food group", 0, 100, int((1 - GROUP_LOWER_MULTIPLIER_DEFAULT) * 100)
        )
        category_lower_multiplier = 1 - category_pct / 100
        category_upper_multiplier = 1 + category_pct / 100
        group_lower_multiplier = 1 - group_pct / 100
        group_upper_multiplier = 1 + group_pct / 100

        run_clicked = st.button("Run Optimization", type="primary", use_container_width=True)

    try:
        baseline_df = _load_baseline(campus, fiscal_year)
    except ValueError as e:
        st.error(str(e))
        return

    _render_snapshot(baseline_df, campus, fiscal_year)

    state_key = f"scenario_result_{campus}_{fiscal_year}"
    if run_clicked:
        try:
            bound_args = (
                category_lower_multiplier,
                category_upper_multiplier,
                group_lower_multiplier,
                group_upper_multiplier,
            )
            if scenario == "min_spend":
                result = solve_min_spend_keep_sustainability(baseline_df, *bound_args)
            elif scenario == "max_sustainable":
                result = solve_max_sustainable_keep_cost(baseline_df, *bound_args)
            else:
                result = solve_threshold_third_of_scenario1(baseline_df, *bound_args)
            st.session_state[state_key] = result
        except InfeasibleScenarioError as e:
            st.error(str(e))
            st.session_state.pop(state_key, None)

    result = st.session_state.get(state_key)
    if result is not None:
        st.divider()
        _render_scenario_results(result)

    st.divider()
    pdf_bytes = generate_pdf_report(campus, baseline_df, scenario_result=result, fiscal_year=fiscal_year)
    st.download_button(
        "Download PDF Report",
        data=pdf_bytes,
        file_name=f"{campus.replace(' ', '_')}_roadmap_report_FY{fiscal_year}.pdf",
        mime="application/pdf",
    )


if __name__ == "__main__":
    main()
