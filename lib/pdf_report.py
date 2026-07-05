"""PDF report generator (Phase 5).

Renders a campus's current-purchasing snapshot (% sustainable by
`validated_sustainable_yn`, GHG-equivalent emissions) and, if a
`lib.optimization.OptimizationResult` is supplied, a roadmap/target-setting
section on top of it -- the two deliverables CLAUDE.md's Campus Roadmap
page is meant to produce. Builds a plain HTML string (no Jinja needed for
this) and renders it with WeasyPrint; the category chart is plain CSS bars
(WeasyPrint renders HTML/CSS natively) rather than a rasterized image --
avoids adding matplotlib as a new dependency for one simple bar chart.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import weasyprint

from lib.optimization import OptimizationResult, identify_category_movers, snapshot_totals


def _fmt_currency(x: float) -> str:
    return f"${x:,.0f}" if pd.notna(x) else "N/A"


def _fmt_pct(x: float) -> str:
    return f"{x:.1f}%" if pd.notna(x) else "N/A"


def _fmt_pct_from_fraction(x: float) -> str:
    return _fmt_pct(x * 100) if pd.notna(x) else "N/A"


def _render_category_chart(baseline_df: pd.DataFrame, scenario_result: OptimizationResult | None) -> str:
    """Plain CSS horizontal bar chart of category weight (baseline, or
    baseline-vs-optimized if a scenario was run) -- returns an HTML
    fragment, not an image."""
    if scenario_result is not None:
        data = scenario_result.category_results.sort_values("baseline_weight_lbs", ascending=False)
        max_val = max(data["baseline_weight_lbs"].max(), data["optimized_weight_lbs"].max(), 1)
    else:
        data = baseline_df.sort_values("baseline_weight_lbs", ascending=False)
        max_val = max(data["baseline_weight_lbs"].max(), 1)

    rows = []
    for row in data.itertuples():
        baseline_pct = 100 * row.baseline_weight_lbs / max_val
        rows.append(
            f"<div class='chart-row'><div class='chart-label'>{row.simap_category}</div>"
            f"<div class='chart-bars'>"
            f"<div class='bar baseline-bar' style='width:{baseline_pct:.1f}%'></div>"
        )
        if scenario_result is not None:
            optimized_pct = 100 * row.optimized_weight_lbs / max_val
            rows.append(f"<div class='bar optimized-bar' style='width:{optimized_pct:.1f}%'></div>")
        rows.append("</div></div>")

    legend = (
        "<div class='chart-legend'>"
        "<span class='legend-item'><span class='swatch baseline-bar'></span>Baseline</span>"
        "<span class='legend-item'><span class='swatch optimized-bar'></span>Optimized</span>"
        "</div>"
        if scenario_result is not None
        else ""
    )
    return f"<div class='chart'>{legend}{''.join(rows)}</div>"


def _render_sustainable_pct_chart(scenario_result: OptimizationResult) -> str:
    """Plain CSS bar chart of % of each category's spend that's
    sustainable, status quo vs. this scenario -- the per-category
    sustainability comparison the project owner asked for."""
    data = scenario_result.category_results.sort_values("baseline_spend", ascending=False)
    rows = []
    for row in data.itertuples():
        baseline_pct = 0.0 if pd.isna(row.baseline_sustainable_pct) else row.baseline_sustainable_pct
        optimized_pct = 0.0 if pd.isna(row.optimized_sustainable_pct) else row.optimized_sustainable_pct
        rows.append(
            f"<div class='chart-row'><div class='chart-label'>{row.simap_category}</div>"
            f"<div class='chart-bars'>"
            f"<div class='bar baseline-bar' style='width:{baseline_pct:.1f}%'></div>"
            f"<div class='bar optimized-bar' style='width:{optimized_pct:.1f}%'></div>"
            f"</div></div>"
        )
    legend = (
        "<div class='chart-legend'>"
        "<span class='legend-item'><span class='swatch baseline-bar'></span>Status quo</span>"
        "<span class='legend-item'><span class='swatch optimized-bar'></span>Optimized</span>"
        "</div>"
    )
    return f"<div class='chart'>{legend}{''.join(rows)}</div>"


def _intro_html() -> str:
    """Plain-language explainer for a non-technical stakeholder (chefs and
    purchasing/business staff alike) -- what this report models and its
    main caveats, mirrors app/1_Campus_Roadmap.py's intro paragraph so the
    two never drift apart on what they promise the reader."""
    return """
    <p class="beta-note">Beta -- a planning tool to support conversations with your purchasing team, not a final decision.</p>
    <p>
      This report models three ways a campus could shift its food purchasing toward more certified-sustainable
      options (AASHE STARS for academic campuses, Practice Greenhealth for health systems), starting from that
      campus's actual purchasing data for the year:
    </p>
    <ul>
      <li><strong>Min Spend</strong> -- the cheapest way to buy the same amount of food while keeping at least
        today's level of sustainable spending.</li>
      <li><strong>Max Sustainable Spend</strong> -- the most sustainable mix of purchases possible without
        spending more than today.</li>
      <li><strong>Threshold (1/3 of Scenario 1) then Maximize</strong> -- a smaller, more conservative version of
        Min Spend's savings, with the rest of that budget redirected toward sustainable purchasing.</li>
    </ul>
    <p>
      To keep recommendations realistic, the model only swaps foods for close substitutes (beef for chicken, not
      beef for apples) and limits how much any single food or food group can shift -- 15% by default. It won't
      suggest overhauling a kitchen's purchasing overnight.
    </p>
    <p class="caveat">
      Technical note: "sustainable" always means AASHE STARS / Practice Greenhealth certification status, never
      SIMAP category membership -- SIMAP-57 is used only to group similar foods and estimate greenhouse-gas
      impact. The numbers in this report come from real purchasing records, which aren't perfect: some purchases
      are missing a category or a weight, and a small number of corrupted entries (e.g. distributor data errors)
      are automatically detected and excluded -- both are called out below wherever they apply.
    </p>
    """


def _snapshot_section_html(campus: str, fiscal_year: int, totals: dict) -> str:
    coverage_note = ""
    if pd.notna(totals["weight_resolved_pct"]) and totals["weight_resolved_pct"] < 0.95:
        coverage_note = (
            f"<p class='caveat'>Note: only {_fmt_pct_from_fraction(totals['weight_resolved_pct'])} of "
            f"classified spend has a resolved weight -- GHG totals reflect only that resolved portion.</p>"
        )
    excluded_note = ""
    if totals["excluded_unclassified_spend"] > 0:
        excluded_note = (
            f"<p class='caveat'>{_fmt_currency(totals['excluded_unclassified_spend'])} of spend is on "
            f"products with no SIMAP category assigned yet and is excluded from the category breakdown "
            f"below (but included in Total Spend above).</p>"
        )
    outlier_note = ""
    if totals["price_outlier_count"] > 0:
        outlier_note = (
            f"<p class='caveat'>{totals['price_outlier_count']} line item(s) totaling "
            f"{_fmt_currency(totals['price_outlier_spend'])} have an implausible reported/computed weight (e.g. a "
            f"distributor data error) and are excluded from weight-based math below, though their spend still "
            f"counts in Total Spend above.</p>"
        )
    return f"""
    <h2>Current Purchasing Snapshot — {campus}, FY{fiscal_year}</h2>
    <div class="metrics">
      <div class="metric"><div class="label">Total Spend</div><div class="value">{_fmt_currency(totals['total_spend'])}</div></div>
      <div class="metric"><div class="label">Sustainable Spend</div><div class="value">{_fmt_pct_from_fraction(totals['sustainable_pct'])}</div></div>
      <div class="metric"><div class="label">GHG-Equivalent Emissions</div><div class="value">{totals['total_ghg_metric_tons']:,.1f} MT CO2e</div></div>
    </div>
    {coverage_note}
    {excluded_note}
    {outlier_note}
    """


def _render_key_takeaways_html(scenario_result: OptimizationResult) -> str:
    """Mirrors app/1_Campus_Roadmap.py's _render_key_takeaways -- the main
    finding this report exists to surface: which SIMAP categories the
    solver shifted toward/away from sustainable spend, and whether that
    shift was cost-neutral-or-better (a "free" win) or premium-funded (the
    scenario's objective paying for it, not price). Shares the exact same
    filtering (identify_category_movers) so the PDF and the app always
    agree on what counts as a genuine, material finding."""
    movers = identify_category_movers(scenario_result.category_results)
    gainers = movers[movers["delta_pts"] > 1].head(5)
    losers = movers[movers["delta_pts"] < -1].sort_values("delta_pts").head(5)
    if gainers.empty and losers.empty:
        return ""

    def _bullet(r) -> str:
        if r.cost_neutral_or_better:
            price_note = (
                f"the sustainable option costs about the same or less per pound ({_fmt_currency(r.sustainable_price_per_lb)} "
                f"vs {_fmt_currency(r.conventional_price_per_lb)}/lb) -- a free win"
            )
        else:
            price_note = (
                f"the sustainable option costs {r.price_ratio_sus_to_conv:.2f}x as much per pound "
                f"({_fmt_currency(r.sustainable_price_per_lb)} vs {_fmt_currency(r.conventional_price_per_lb)}/lb) -- "
                "this change is driven by this scenario's targets, not because it's cheaper"
            )
        return (
            f"<li><strong>{r.simap_category}</strong> ({r.food_group}): {r.baseline_sustainable_pct:.0f}% &rarr; "
            f"{r.optimized_sustainable_pct:.0f}% sustainable ({r.delta_pts:+.0f} pts) -- {price_note}</li>"
        )

    gainers_html = ""
    if not gainers.empty:
        n_free_wins = int(gainers["cost_neutral_or_better"].sum())
        if n_free_wins == len(gainers):
            rec = f"All {n_free_wins} of these can become more sustainable at no extra cost -- they're the easiest place to start."
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
        gainers_html = (
            "<p><strong>Moving toward more sustainable purchasing:</strong></p><ul>"
            + "".join(_bullet(r) for r in gainers.itertuples())
            + f"</ul><p class='caveat'>{rec}</p>"
        )

    losers_html = ""
    if not losers.empty:
        losers_html = (
            "<p><strong>Moving toward more conventional purchasing:</strong></p><ul>"
            + "".join(_bullet(r) for r in losers.itertuples())
            + "</ul><p class='caveat'>These categories shift toward the conventional option to help pay for the "
            "gains above -- usually because the sustainable version costs noticeably more per pound here.</p>"
        )

    return f"<h3>Key takeaways</h3>{gainers_html}{losers_html}"


def _fmt_price_ratio(sustainable_price_per_lb: float, conventional_price_per_lb: float) -> str:
    if pd.isna(sustainable_price_per_lb) or pd.isna(conventional_price_per_lb) or conventional_price_per_lb == 0:
        return "N/A"
    return f"{sustainable_price_per_lb / conventional_price_per_lb:.2f}x"


def _scenario_section_html(scenario_result: OptimizationResult) -> str:
    t = scenario_result.totals
    rows = "".join(
        f"<tr><td>{r.simap_category}</td>"
        f"<td>{_fmt_currency(r.baseline_spend)}</td>"
        f"<td>{_fmt_currency(r.optimized_spend)}</td>"
        f"<td>{r.weight_pct_change:+.1f}%</td>"
        f"<td>{_fmt_price_ratio(r.sustainable_price_per_lb, r.conventional_price_per_lb)}</td></tr>"
        if pd.notna(r.weight_pct_change)
        else f"<tr><td>{r.simap_category}</td>"
        f"<td>{_fmt_currency(r.baseline_spend)}</td>"
        f"<td>{_fmt_currency(r.optimized_spend)}</td>"
        f"<td>N/A</td>"
        f"<td>{_fmt_price_ratio(r.sustainable_price_per_lb, r.conventional_price_per_lb)}</td></tr>"
        for r in scenario_result.category_results.itertuples()
    )
    assumptions_text = ", ".join(f"{k}: {v}" for k, v in scenario_result.assumptions.items())
    # Headline is the resulting sustainable-spend SHARE, not just its %
    # change -- campuses care about where they land, per project owner
    # direction. Baseline share + point change still shown as context.
    delta_pp = (t["optimized_sustainable_pct"] - t["baseline_sustainable_pct"]) * 100
    return f"""
    <h2>Roadmap Scenario — {scenario_result.scenario_name}</h2>
    <p class="caveat">Assumptions: {assumptions_text}</p>
    <div class="metrics">
      <div class="metric"><div class="label">Total Cost</div><div class="value">{_fmt_currency(t['optimized_cost'])}</div></div>
      <div class="metric"><div class="label">Sustainable Spend Share</div><div class="value">{_fmt_pct_from_fraction(t['optimized_sustainable_pct'])}</div></div>
      <div class="metric"><div class="label">GHG Change</div><div class="value">{t['ghg_pct_change']:+.1f}%</div></div>
    </div>
    <p class="caveat">
      Cost change: {t['cost_pct_change']:+.1f}% ({_fmt_currency(t['baseline_cost'])} → {_fmt_currency(t['optimized_cost'])}).
      Sustainable spend share: {_fmt_pct_from_fraction(t['baseline_sustainable_pct'])} → {_fmt_pct_from_fraction(t['optimized_sustainable_pct'])}
      ({delta_pp:+.1f} pts).
    </p>
    {_render_key_takeaways_html(scenario_result)}
    <table>
      <thead><tr><th>Category</th><th>Baseline Spend</th><th>Optimized Spend</th><th>Weight % Change</th><th>Sustainable Price vs. Conventional</th></tr></thead>
      <tbody>{rows}</tbody>
    </table>
    <h3>% of spend sustainable by category -- status quo vs. optimized</h3>
    {_render_sustainable_pct_chart(scenario_result)}
    """


_HTML_TEMPLATE = """
<html>
<head>
<style>
  body {{ font-family: -apple-system, Helvetica, Arial, sans-serif; color: #222; margin: 2em; }}
  h1 {{ font-size: 22px; }}
  h2 {{ font-size: 17px; margin-top: 1.6em; border-bottom: 1px solid #ccc; padding-bottom: 0.2em; }}
  h3 {{ font-size: 14px; margin-top: 1.2em; }}
  .generated {{ color: #666; font-size: 12px; }}
  .metrics {{ display: flex; gap: 16px; margin: 1em 0; }}
  .metric {{ flex: 1; padding: 12px; border-radius: 8px; background-color: #f5f5f5; border: 1px solid #ddd; }}
  .metric .label {{ font-size: 12px; color: #555; }}
  .metric .value {{ font-size: 22px; font-weight: 700; }}
  .caveat {{ font-size: 12px; color: #777; font-style: italic; }}
  .beta-note {{ font-size: 11px; color: #8a6d00; background-color: #fff8e1; display: inline-block; padding: 3px 8px; border-radius: 4px; margin-bottom: 0.8em; }}
  ul {{ font-size: 13px; margin: 0.3em 0 0.6em; padding-left: 1.4em; }}
  li {{ margin-bottom: 3px; }}
  table {{ border-collapse: collapse; width: 100%; font-size: 12px; margin-top: 0.5em; }}
  th, td {{ border: 1px solid #ddd; padding: 4px 8px; text-align: right; }}
  th:first-child, td:first-child {{ text-align: left; }}
  .chart {{ margin-top: 1em; }}
  .chart-legend {{ margin-bottom: 8px; font-size: 11px; }}
  .legend-item {{ margin-right: 16px; }}
  .swatch {{ display: inline-block; width: 10px; height: 10px; margin-right: 4px; }}
  .chart-row {{ display: flex; align-items: center; margin: 3px 0; }}
  .chart-label {{ width: 220px; font-size: 10px; text-align: right; padding-right: 8px; flex-shrink: 0; }}
  .chart-bars {{ flex: 1; }}
  .bar {{ height: 8px; margin: 1px 0; border-radius: 2px; }}
  .baseline-bar {{ background-color: #D2B48C; }}
  .optimized-bar {{ background-color: #2E6F57; }}
</style>
</head>
<body>
  <h1>UC Sustainable Procurement — Campus Roadmap Report</h1>
  <p class="generated">Generated {generated_date}</p>
  {intro_section}
  {snapshot_section}
  <h2>Category Breakdown by Weight</h2>
  {chart_html}
  {scenario_section}
</body>
</html>
"""


def generate_pdf_report(
    campus: str,
    baseline_df: pd.DataFrame,
    scenario_result: OptimizationResult | None = None,
    fiscal_year: int = 2025,
) -> bytes:
    """Returns the report as PDF bytes. `baseline_df` is
    `lib.optimization.build_category_baseline(...)`'s output; `scenario_result`
    (optional) is any `lib.optimization.solve_*`'s output -- when omitted,
    the report is the current-purchasing snapshot only."""
    totals = snapshot_totals(baseline_df)
    chart_html = _render_category_chart(baseline_df, scenario_result)

    html = _HTML_TEMPLATE.format(
        generated_date=date.today().isoformat(),
        intro_section=_intro_html(),
        snapshot_section=_snapshot_section_html(campus, fiscal_year, totals),
        chart_html=chart_html,
        scenario_section=_scenario_section_html(scenario_result) if scenario_result is not None else "",
    )

    return weasyprint.HTML(string=html).write_pdf()
