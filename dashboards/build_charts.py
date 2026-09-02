#!/usr/bin/env python3
"""Build the 5 PMN KPI dashboard charts (Delivery, EDCT, Quality, % Epics by AI,
AI Fields Adoption) from a single data JSON, using the dashboard's brand style:
section-matched colors, larger readable fonts, and no in-chart titles (the HTML
wrapper supplies the heading, so a duplicate title inside the image just wastes
space and creates a jarring font-size jump).

Usage:
    python3 build_charts.py --data data.json --outdir charts/

See references/data_sources.md in this skill for how to gather the data.json
fields from Jira + Power BI. If a section is missing from data.json (e.g. the
Power BI pull failed today), pass --carry-forward <previous_data.json> and this
script will reuse that section's last known values rather than erroring out --
it stamps the chart caption with the as-of date so staleness is visible rather
than silently hidden.

data.json schema (all sections optional except "delivery"):
{
  "release": "26.4",
  "delivery": {
    "release_history": {"25.3": 29, "25.4": 42, "26.1": 34, "26.2": 82, "26.3": 84},
    "release_order": ["25.3", "25.4", "26.1", "26.2", "26.3"],
    "current_delivered": 4,
    "release_start_date": "2026-07-15",
    "release_end_date": "2026-10-06",
    "sprint_boundaries": ["2026-07-15", "2026-08-05", "2026-08-26", "2026-09-16", "2026-10-06"],
    "days_into_release_delivered": [1]
  },
  "edct": {
    "months": ["2026/04", "2026/05", "2026/06", "2026/07*"],
    "values": [7, 24, 26, 27],
    "target_all": 16,
    "target_ai": 10,
    "as_of": "2026-07-16"
  },
  "quality": {
    "weeks": ["01Jun", "08Jun", ...],
    "created": [13, 7, ...],
    "resolved": [19, 11, ...],
    "open_trend": [53, 49, ...],
    "as_of": "2026-07-16"
  },
  "ai_epics_pct": {
    "months": ["April", "May", "June", "July"],
    "total_epics": [30, 25, 41, 18],
    "ai_epics": [3, 13, 35, 16],
    "as_of": "2026-07-16"
  },
  "ai_fields_adoption": {
    "months": ["April", "May", "June", "July"],
    "marked_ai": [38, 154, 239, 26],
    "new_metrics": [0, 0, 18, 8],
    "as_of": "2026-07-16"
  }
}
"""
import argparse
import json
import datetime as dt

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

# ---------------- Brand palette (must match the HTML section rail colors) ----------------
NAVY = "#1F3A5F"       # Delivery rail
TEAL = "#2ED9B8"       # secondary / positive accent, also the Delivery fill
TEAL_EDGE = "#12886C"
GREEN = "#2E9E4F"      # Quality rail
AMBER = "#C77700"      # Efficiency rail + universal trend/highlight line color
PURPLE = "#7B4FC7"     # AI Adoption rail
GREY = "#B7B7C9"       # neutral / baseline bars
INK = "#1A1A1A"

plt.rcParams.update({
    "font.family": "DejaVu Sans",
    "axes.edgecolor": "#DEDEDE",
    "axes.labelcolor": INK,
    "axes.labelsize": 13,
    "text.color": INK,
    "xtick.color": "#333333",
    "ytick.color": "#333333",
    "xtick.labelsize": 12,
    "ytick.labelsize": 12,
    "legend.fontsize": 11.5,
    "figure.facecolor": "white",
    "axes.facecolor": "white",
})
# Deliberately no chart titles anywhere -- the HTML .stitle already carries the heading.
# Adding one in-image duplicates it at a mismatched size/weight and wastes vertical space.


def merge_with_carry_forward(data, carry_forward):
    """Fill in any missing top-level sections (except 'delivery') from a previous run."""
    if not carry_forward:
        return data
    for key in ("edct", "quality", "ai_epics_pct", "ai_fields_adoption"):
        if key not in data and key in carry_forward:
            data[key] = carry_forward[key]
    return data


def linear_forecast(release_history, release_order):
    """Pure linear regression over the release sequence -- no subjective discounting.
    Returns the forecast for the release immediately after release_order[-1]."""
    counts = [release_history[r] for r in release_order]
    idx = np.arange(1, len(counts) + 1)
    A = np.vstack([idx, np.ones_like(idx)]).T
    slope, intercept = np.linalg.lstsq(A, np.array(counts), rcond=None)[0]
    next_idx = len(counts) + 1
    return float(slope * next_idx + intercept), float(slope), float(intercept)


def chart_delivery(d, outpath):
    release_history = d["release_history"]
    release_order = d["release_order"]
    forecast_total, slope, intercept = linear_forecast(release_history, release_order)
    # Optional explicit forecast (e.g. committed-scope basis) overrides the pure
    # regression. Use when the 5-release regression misfits -- e.g. a project ramping
    # up from a near-zero baseline, where the early releases flatten the slope and the
    # regression under-predicts the committed backlog.
    if d.get("forecast_override") is not None:
        forecast_total = float(d["forecast_override"])

    start = dt.date.fromisoformat(d["release_start_date"])
    end = dt.date.fromisoformat(d["release_end_date"])
    window = (end - start).days
    days = np.arange(0, window + 1)
    expected = forecast_total * days / days[-1]
    dates = [start + dt.timedelta(days=int(x)) for x in days]

    delivered_days = sorted(x if x > 0 else 0 for x in d.get("days_into_release_delivered", []))
    # Extend the actual-delivered curve out to "today" (the as-of date), holding the
    # cumulative count flat after the last delivery. Without this, when every epic so far
    # resolved on the same day the series collapses to a single point and the teal area
    # under it has zero width -- it renders as just the dot with no fill beneath it.
    as_of = d.get("as_of")
    as_of_date = dt.date.fromisoformat(as_of) if as_of else dt.date.today()
    as_of_day = max(0, (as_of_date - start).days)
    last_delivery_day = max(delivered_days) if delivered_days else 0
    day_now = max(last_delivery_day, as_of_day)
    xs = np.arange(0, day_now + 1)
    ys = np.array([sum(1 for v in delivered_days if v <= x) for x in xs])
    dates_actual = [start + dt.timedelta(days=int(x)) for x in xs]
    actual_today = int(ys[-1]) if len(ys) else 0

    fig, ax = plt.subplots(figsize=(11, 5))
    ax.fill_between(dates_actual, 0, ys, color=TEAL, alpha=0.55, step="post")
    ax.step(dates_actual, ys, where="post", color=TEAL_EDGE, linewidth=2.6,
            label="Actual delivered (cumulative)", zorder=5)
    ax.plot(dates, expected, color=INK, linewidth=2.4, linestyle="--",
            label=f"Expected linear pace to {forecast_total:.0f}", zorder=4)
    if len(dates_actual):
        # Marker for "today" with just the count sitting directly on top of it -- no
        # "Today"/"delivered" wording (redundant with the legend, and the longer callout
        # collided with the dashed trendline).
        ax.scatter([dates_actual[-1]], [actual_today], color=TEAL_EDGE, edgecolor="white", s=90, zorder=6)
        ax.annotate(f"{actual_today}", xy=(dates_actual[-1], actual_today),
                    xytext=(0, 9), textcoords="offset points", ha="center",
                    fontsize=12, fontweight="bold", color=TEAL_EDGE, zorder=7)
    for sb in d.get("sprint_boundaries", []):
        ax.axvline(dt.date.fromisoformat(sb), color=AMBER, linewidth=1.3, alpha=0.85)

    ax.set_ylabel("Cumulative epics delivered")
    ax.spines[["top", "right"]].set_visible(False)
    ax.legend(frameon=False, loc="upper left")
    # Anchor the weekly ticks to the release start date so the axis begins at day 0
    # (the release start), not wherever matplotlib's default epoch-based locator lands.
    ax.set_xticks([start + dt.timedelta(days=int(x)) for x in np.arange(0, window + 1, 7)])
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
    plt.setp(ax.get_xticklabels(), rotation=30, ha="right")
    ax.set_xlim(dates[0], dates[-1])
    ax.set_ylim(0, max(expected.max(), actual_today) * 1.15)
    fig.tight_layout()
    fig.savefig(outpath, dpi=160)
    plt.close(fig)
    return {"forecast_total": round(forecast_total), "actual_today": actual_today,
            "slope": slope, "intercept": intercept}


def chart_edct(d, outpath):
    months = d["months"]
    values = d["values"]
    target_all = d.get("target_all", 16)
    # target_ai is optional: when the report exposes only a single blended target
    # (no separate AI-assisted line), pass null/omit it and only one target line draws.
    target_ai = d.get("target_ai", 10)

    fig, ax = plt.subplots(figsize=(11, 5.2))
    ax.bar(months, values, color=TEAL, edgecolor=TEAL_EDGE, linewidth=1.2, width=0.55, zorder=3)
    for i, v in enumerate(values):
        ax.text(i, v + max(values) * 0.05, str(v), ha="center", fontsize=15, fontweight="bold", color=INK)
    all_label = "Target (all epics)" if target_ai is not None else "Target"
    ax.axhline(target_all, color=AMBER, linewidth=2.2, linestyle="--", zorder=2)
    ax.text(-0.45, target_all + max(values) * 0.03, f"{all_label} ≤{target_all}",
            color=AMBER, fontsize=12.5, fontweight="bold", ha="left")
    if target_ai is not None:
        ax.axhline(target_ai, color=PURPLE, linewidth=2.2, linestyle="--", zorder=2)
        ax.text(-0.45, target_ai + max(values) * 0.03, f"Target (AI-assisted) ≤{target_ai}",
                color=PURPLE, fontsize=12.5, fontweight="bold", ha="left")
    ax.set_ylabel("Avg days in In Progress + Validation\n(excl. flagged time)")
    ax.spines[["top", "right"]].set_visible(False)
    ax.grid(axis="y", color="#eee", zorder=0)
    ax.set_axisbelow(True)
    ax.set_ylim(0, max(values + [target_all] + ([target_ai] if target_ai is not None else [])) * 1.2)
    fig.tight_layout()
    fig.savefig(outpath, dpi=160)
    plt.close(fig)


def chart_quality(d, outpath):
    weeks, created, resolved, open_trend = d["weeks"], d["created"], d["resolved"], d["open_trend"]
    fig, ax = plt.subplots(figsize=(12, 5.8))
    x = np.arange(len(weeks))
    bw = 0.35
    ax.bar(x - bw / 2, created, width=bw, color=GREY, label="Bugs created", zorder=3)
    ax.bar(x + bw / 2, resolved, width=bw, color=GREEN, label="Bugs closed", zorder=3)

    ax2 = ax.twinx()
    ax2.plot(x, open_trend, color=AMBER, marker="o", markersize=7, linewidth=2.6, label="Total Open", zorder=4)
    for i, v in enumerate(open_trend):
        ax2.annotate(str(v), (i, v), textcoords="offset points", xytext=(0, 11),
                     ha="center", fontsize=13, fontweight="bold", color=AMBER,
                     bbox=dict(boxstyle="round,pad=0.25", facecolor="#FDEBD3",
                               edgecolor="none", alpha=0.9))

    ax.set_xticks(x)
    ax.set_xticklabels(weeks, rotation=0, fontsize=13)
    ax.spines[["top"]].set_visible(False)
    ax2.spines[["top"]].set_visible(False)
    ax.grid(axis="y", color="#eee", zorder=0)
    ax.set_axisbelow(True)
    ax2.set_ylim(0, max(open_trend) * 1.3 if open_trend else 1)
    ax.tick_params(axis="both", labelsize=13)
    ax2.tick_params(axis="both", labelsize=13)

    l1, lb1 = ax.get_legend_handles_labels()
    l2, lb2 = ax2.get_legend_handles_labels()
    ax.legend(l1 + l2, lb1 + lb2, loc="upper center", bbox_to_anchor=(0.5, -0.12), ncol=3,
              frameon=False, fontsize=12.5)
    fig.tight_layout()
    fig.savefig(outpath, dpi=160)
    plt.close(fig)


def chart_ai_epics_pct(d, outpath):
    months = d["months"]
    total_epics = d["total_epics"]
    ai_epics = d["ai_epics"]
    pct = [100.0 * a / t if t else 0.0 for a, t in zip(ai_epics, total_epics)]

    fig, ax = plt.subplots(figsize=(11, 5.8))
    x = np.arange(len(months))
    bw = 0.35
    ax.bar(x - bw / 2, total_epics, width=bw, color=GREY, label="Total epics resolved", zorder=3)
    ax.bar(x + bw / 2, ai_epics, width=bw, color=PURPLE, label="Epics developed by AI", zorder=3)
    label_bbox = dict(boxstyle="round,pad=0.25", facecolor="#FDEBD3", edgecolor="none", alpha=0.9)
    for i, v in enumerate(total_epics):
        ax.text(i - bw / 2, v + max(total_epics) * 0.02, str(v), ha="center", fontsize=13, color="#555",
                bbox=label_bbox)
    for i, v in enumerate(ai_epics):
        ax.text(i + bw / 2, v + max(total_epics) * 0.02, str(v), ha="center", fontsize=13,
                fontweight="bold", color=PURPLE, bbox=label_bbox)

    ax2 = ax.twinx()
    ax2.plot(x, pct, color=AMBER, marker="o", markersize=7, linewidth=2.6, label="% developed by AI", zorder=4)
    for i, v in enumerate(pct):
        ax2.annotate(f"{v:.0f}%", (i, v), textcoords="offset points", xytext=(-22, 14),
                     ha="center", fontsize=14, fontweight="bold", color=AMBER, bbox=label_bbox)

    ax.set_xticks(x)
    ax.set_xticklabels(months, fontsize=13)
    ax.set_ylabel("# Epics")
    ax2.set_ylabel("% developed by AI")
    ax.spines[["top"]].set_visible(False)
    ax2.spines[["top"]].set_visible(False)
    ax.grid(axis="y", color="#eee", zorder=0)
    ax.set_axisbelow(True)
    ax2.set_ylim(0, 110)
    ax.set_ylim(0, max(total_epics) * 1.35)
    ax.tick_params(axis="both", labelsize=13)
    ax2.tick_params(axis="both", labelsize=13)

    l1, lb1 = ax.get_legend_handles_labels()
    l2, lb2 = ax2.get_legend_handles_labels()
    ax.legend(l1 + l2, lb1 + lb2, loc="upper center", bbox_to_anchor=(0.5, -0.12), ncol=3,
              frameon=False, fontsize=12.5)
    fig.tight_layout()
    fig.savefig(outpath, dpi=160)
    plt.close(fig)
    return pct


def chart_ai_fields_adoption(d, outpath):
    months = d["months"]
    marked_ai = d["marked_ai"]
    new_metrics = d["new_metrics"]
    pct = [100.0 * n / m if m else 0.0 for n, m in zip(new_metrics, marked_ai)]

    fig, ax = plt.subplots(figsize=(11, 5.8))
    x = np.arange(len(months))
    bw = 0.35
    ax.bar(x - bw / 2, marked_ai, width=bw, color=PURPLE, label="Issues marked as AI", zorder=3)
    ax.bar(x + bw / 2, new_metrics, width=bw, color=TEAL, label="Having new AI metrics", zorder=3)
    label_bbox = dict(boxstyle="round,pad=0.25", facecolor="#FDEBD3", edgecolor="none", alpha=0.9)
    for i, v in enumerate(marked_ai):
        ax.text(i - bw / 2, v + max(marked_ai) * 0.02, str(v), ha="center", fontsize=13,
                fontweight="bold", color=PURPLE, bbox=label_bbox)
    for i, v in enumerate(new_metrics):
        ax.text(i + bw / 2, v + max(marked_ai) * 0.02, str(v), ha="center", fontsize=13, color=TEAL_EDGE,
                bbox=label_bbox)

    ax2 = ax.twinx()
    ax2.plot(x, pct, color=AMBER, marker="o", markersize=7, linewidth=2.6, label="% adoption", zorder=4)
    for i, v in enumerate(pct):
        ax2.annotate(f"{v:.1f}%", (i, v), textcoords="offset points", xytext=(-22, 14),
                     ha="center", fontsize=14, fontweight="bold", color=AMBER, bbox=label_bbox)

    ax.set_xticks(x)
    ax.set_xticklabels(months, fontsize=13)
    ax.set_ylabel("# Issues")
    ax2.set_ylabel("% with new AI metrics")
    ax.spines[["top"]].set_visible(False)
    ax2.spines[["top"]].set_visible(False)
    ax.grid(axis="y", color="#eee", zorder=0)
    ax.set_axisbelow(True)
    ax2.set_ylim(0, max(pct) * 1.6 if max(pct) > 0 else 10)
    ax.set_ylim(0, max(marked_ai) * 1.25)
    ax.tick_params(axis="both", labelsize=13)
    ax2.tick_params(axis="both", labelsize=13)

    l1, lb1 = ax.get_legend_handles_labels()
    l2, lb2 = ax2.get_legend_handles_labels()
    ax.legend(l1 + l2, lb1 + lb2, loc="upper center", bbox_to_anchor=(0.5, -0.12), ncol=3,
              frameon=False, fontsize=12.5)
    fig.tight_layout()
    fig.savefig(outpath, dpi=160)
    plt.close(fig)
    return pct


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--carry-forward", default=None,
                     help="Previous data.json to fall back on for any missing sections")
    args = ap.parse_args()

    import os
    os.makedirs(args.outdir, exist_ok=True)

    data = json.load(open(args.data))
    carry_forward = json.load(open(args.carry_forward)) if args.carry_forward else None
    data = merge_with_carry_forward(data, carry_forward)

    summary = {}
    summary["delivery"] = chart_delivery(data["delivery"], f"{args.outdir}/chart_delivery.png")
    if "edct" in data:
        chart_edct(data["edct"], f"{args.outdir}/chart_edct.png")
    if "quality" in data:
        chart_quality(data["quality"], f"{args.outdir}/chart_quality.png")
    if "ai_epics_pct" in data:
        pct = chart_ai_epics_pct(data["ai_epics_pct"], f"{args.outdir}/chart_ai_epics_pct.png")
        summary["ai_epics_pct_latest"] = round(pct[-1], 1) if pct else None
    if "ai_fields_adoption" in data:
        pct = chart_ai_fields_adoption(data["ai_fields_adoption"], f"{args.outdir}/chart_ai_fields_adoption.png")
        summary["ai_fields_adoption_latest"] = round(pct[-1], 1) if pct else None

    # Persist the merged data.json so tomorrow's run can --carry-forward from it.
    json.dump(data, open(f"{args.outdir}/data_used.json", "w"), indent=2)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
