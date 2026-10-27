"""Reusable UI pieces: formatters, cards, badges, banners, run metadata, tables and charts."""
from __future__ import annotations

import html
import math
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Sequence

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from src.ui import theme as T

IST = timezone(timedelta(hours=5, minutes=30), name="IST")
DASH = "—"


# ============================================================================ formatting
def _missing(v: Any) -> bool:
    if v is None:
        return True
    try:
        return bool(pd.isna(v))
    except (TypeError, ValueError):
        return False


def fmt_ms(v: Any, digits: int = 1) -> str:
    return DASH if _missing(v) else f"{float(v):,.{digits}f}"


def fmt_pct(fraction: Any, digits: int = 1) -> str:
    """Fraction (0..1) -> '99.2%'."""
    return DASH if _missing(fraction) else f"{float(fraction) * 100:.{digits}f}%"


def fmt_int(v: Any) -> str:
    return DASH if _missing(v) else f"{int(v):,}"


def fmt_signed(v: Any, unit: str = "", digits: int = 1) -> str:
    if _missing(v):
        return DASH
    return f"{float(v):+,.{digits}f}{unit}"


def parse_ts(value: Any) -> datetime | None:
    if _missing(value) or value == "":
        return None
    try:
        ts = pd.Timestamp(value)
    except (ValueError, TypeError):
        return None
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    return ts.to_pydatetime()


def fmt_ts(value: Any, with_seconds: bool = True) -> str:
    """UTC ISO timestamp -> local India time string (IST, UTC+05:30)."""
    dt = parse_ts(value)
    if dt is None:
        return DASH
    fmt = "%d %b %Y, %H:%M:%S" if with_seconds else "%d %b %Y, %H:%M"
    return dt.astimezone(IST).strftime(fmt) + " IST"


def esc(text: Any) -> str:
    return html.escape("" if text is None else str(text))


# ============================================================================ data prep
def prepare_results(df: pd.DataFrame | None) -> pd.DataFrame:
    """Normalise request-level rows: bool columns, numeric latency, parsed UTC timestamps."""
    if df is None:
        return pd.DataFrame()
    out = df.copy()
    for col in ("is_warmup", "success", "is_timeout", "is_synthetic"):
        if col not in out.columns:
            out[col] = False
        out[col] = out[col].fillna(False).astype(bool)
    for col in ("outcome", "http_status", "latency_ms", "endpoint_id", "endpoint_name"):
        if col not in out.columns:
            out[col] = None
    if "latency_ms" in out.columns:
        out["latency_ms"] = pd.to_numeric(out["latency_ms"], errors="coerce")
    if "http_status" in out.columns:
        out["http_status"] = pd.to_numeric(out["http_status"], errors="coerce").astype("Int64")
    if "started_at" in out.columns:
        out["started_dt"] = pd.to_datetime(out["started_at"], utc=True, errors="coerce")
    return out


def add_categories(metrics_df: pd.DataFrame, endpoints: Iterable[Any]) -> pd.DataFrame:
    """Fill the ``category`` column from the registry (request rows don't carry it)."""
    if metrics_df is None or not len(metrics_df):
        return metrics_df
    cats = {ep.id: ep.category for ep in endpoints}
    out = metrics_df.copy()
    existing = out["category"] if "category" in out.columns else pd.Series([None] * len(out))
    out["category"] = [c if not _missing(c) else cats.get(str(e), "Unregistered")
                       for c, e in zip(existing, out["endpoint_id"])]
    return out


def run_label(run: dict | pd.Series) -> str:
    rid = run.get("run_id", "?")
    when = fmt_ts(run.get("started_at") or run.get("created_at"), with_seconds=False)
    kind = run.get("run_type", "")
    tag = " · SYNTHETIC" if bool(run.get("is_synthetic")) else ""
    status = run.get("status") or ""
    status = f" · {status}" if status and status != "completed" else ""
    return f"{when} · {kind}{tag}{status} · {rid}"


# ============================================================================ cards & badges
def metric_card(label: str, value: str, unit: str = "", sub: str | None = None,
                tone: str = "neutral", sub_warn: bool = False, help: str | None = None) -> None:
    title = f' title="{esc(help)}"' if help else ""
    unit_html = f'<span class="ts-card-unit">{esc(unit)}</span>' if unit and value != DASH else ""
    sub_html = (f'<div class="ts-card-sub{" warn" if sub_warn else ""}">{esc(sub)}</div>'
                if sub else "")
    st.markdown(
        f'<div class="ts-card tone-{tone}"{title}><div class="ts-card-label">{esc(label)}</div>'
        f'<div class="ts-card-value">{esc(value)}{unit_html}</div>{sub_html}</div>',
        unsafe_allow_html=True,
    )


def card_row(cards: Sequence[dict], per_row: int = 4) -> None:
    for start in range(0, len(cards), per_row):
        cols = st.columns(per_row)
        for col, card in zip(cols, cards[start:start + per_row]):
            with col:
                metric_card(**card)
        if start + per_row < len(cards):
            st.write("")


def pill(text: str, kind: str = "neutral") -> str:
    return f'<span class="ts-pill {esc(kind)}">{esc(text)}</span>'


def synthetic_badge(render: bool = True) -> str:
    badge = pill("SYNTHETIC DEMO", "synthetic")
    if render:
        st.markdown(badge, unsafe_allow_html=True)
    return badge


def synthetic_banner(mixed: bool = False) -> None:
    """Visible banner whenever synthetic data is on screen (always paired with the badge)."""
    msg = ("This view **mixes measured runs and synthetic demo runs**. Synthetic numbers are "
           "generated locally and are not TradeSmart measurements. Turn off *Include synthetic "
           "demo data* in the sidebar to see measured data only."
           if mixed else
           "You are looking at **synthetic demo data**. These numbers are generated locally to "
           "show how the dashboard works. They are not TradeSmart measurements.")
    st.markdown(f"{synthetic_badge(render=False)}", unsafe_allow_html=True)
    st.warning(msg, icon=":material/science:")


def data_origin_banner(is_synthetic: Iterable[bool]) -> None:
    flags = {bool(x) for x in is_synthetic}
    if True in flags:
        synthetic_banner(mixed=False in flags)


def live_warning_banner(environment: str) -> None:
    text = None
    try:
        from src.safety import live_account_warning

        text = live_account_warning(environment)
    except Exception:
        if environment == "live":
            text = ("You are connected to a LIVE trading account. Only read-only endpoints are "
                    "benchmarked; order endpoints are never executed.")
    if text:
        st.error(text, icon=":material/warning:")


def sample_size_note(samples: Any, min_p95: int = 20, min_p99: int = 100) -> str | None:
    """Caption text warning about tail percentiles from small samples (None when reliable)."""
    try:
        from src.metrics import MIN_SAMPLES_P95, MIN_SAMPLES_P99

        min_p95, min_p99 = MIN_SAMPLES_P95, MIN_SAMPLES_P99
    except Exception:
        pass
    n = 0 if _missing(samples) else int(samples)
    if n == 0:
        return "No successful samples, so no percentiles."
    if n < min_p95:
        return (f"Based on {n} successful samples. P95 needs ≥ {min_p95} and P99 needs ≥ {min_p99} "
                f"to be meaningful — treat both as indicative only.")
    if n < min_p99:
        return (f"Based on {n} successful samples. P99 needs ≥ {min_p99} samples — treat P99 as "
                f"indicative only.")
    return None


def show_sample_note(samples: Any) -> None:
    note = sample_size_note(samples)
    if note:
        st.caption(f":material/info: {note}")


def reliability_label(samples: Any) -> str:
    n = 0 if _missing(samples) else int(samples)
    if n == 0:
        return "No samples"
    if n < 20:
        return "P95 + P99 indicative"
    if n < 100:
        return "P99 indicative"
    return "OK"


def run_meta_block(run: dict | None, request_count: int | None = None) -> None:
    """Date/time, run id, request count, configuration and connection mode for a run."""
    if not run:
        return
    cfg = run.get("config") or {}
    if request_count is None:
        request_count = run.get("request_count")
    items = [
        ("Run ID", run.get("run_id")),
        ("Started", fmt_ts(run.get("started_at") or run.get("created_at"))),
        ("Completed", fmt_ts(run.get("completed_at"))),
        ("Status", run.get("status") or DASH),
        ("Requests (measured)", fmt_int(request_count)),
        ("Connection", run.get("connection_mode") or cfg.get("connection_mode") or DASH),
        ("Environment", (run.get("environment") or DASH).upper()),
        ("Host", run.get("base_host") or DASH),
    ]
    config_bits = []
    for key, label in (("iterations", "iterations"), ("warmup", "warm-up"),
                       ("timeout_s", "timeout s"), ("delay_ms", "delay ms"),
                       ("concurrency", "concurrency"), ("order", "order"),
                       ("safety_factor", "safety factor")):
        if key in cfg and not _missing(cfg.get(key)):
            config_bits.append(f"{label} {cfg[key]}")
    if config_bits:
        items.append(("Config", " · ".join(config_bits)))
    body = "".join(f'<div><span class="k">{esc(k)}</span><b>{esc(v)}</b></div>' for k, v in items)
    badge = synthetic_badge(render=False) if run.get("is_synthetic") else ""
    st.markdown(f'<div class="ts-meta">{body}<div>{badge}</div></div>', unsafe_allow_html=True)
    if run.get("stop_reason"):
        st.caption(f"Stop reason: {run['stop_reason']}")


# ============================================================================ tables
METRIC_TABLE_COLUMNS = {
    "endpoint_name": "Endpoint", "method": "Method", "category": "Category", "run_label": "Run",
    "samples": "Samples", "p50_ms": "P50 (ms)", "p95_ms": "P95 (ms)", "p99_ms": "P99 (ms)",
    "min_ms": "Min (ms)", "max_ms": "Max (ms)", "mean_ms": "Mean (ms)",
    "success_rate_pct": "Success rate", "failed": "Failures", "timeouts": "Timeouts",
    "reliability": "Tail reliability",
}


def endpoint_table(metrics_df: pd.DataFrame, key: str, *, show_run: bool = False,
                   selectable: bool = True, height: int | str = "auto",
                   on_select: Any = "rerun") -> dict | None:
    """Sortable endpoint metrics table. Returns the selected row (dict) when ``selectable``."""
    if metrics_df is None or not len(metrics_df):
        return None
    df = metrics_df.reset_index(drop=True).copy()
    df["success_rate_pct"] = pd.to_numeric(df.get("success_rate"), errors="coerce") * 100
    df["reliability"] = [reliability_label(s) for s in df["samples"]]
    cols = [c for c in METRIC_TABLE_COLUMNS if c in df.columns]
    if not show_run and "run_label" in cols:
        cols.remove("run_label")
    view = df[cols].rename(columns=METRIC_TABLE_COLUMNS)
    num = st.column_config.NumberColumn
    config = {
        "Samples": num(help="Successful, non-warm-up requests used for percentiles", format="%d"),
        "P50 (ms)": num(format="%.1f"), "P95 (ms)": num(format="%.1f"),
        "P99 (ms)": num(format="%.1f"), "Min (ms)": num(format="%.1f"),
        "Max (ms)": num(format="%.1f"), "Mean (ms)": num(format="%.1f"),
        "Success rate": num(format="%.1f%%"),
        "Failures": num(format="%d", help="All non-successful requests, including timeouts"),
        "Timeouts": num(format="%d"),
        "Tail reliability": st.column_config.TextColumn(
            help="P95 needs ≥ 20 samples, P99 needs ≥ 100 samples to be meaningful"),
    }
    if not selectable:
        st.dataframe(view, hide_index=True, column_config=config, height=height, key=key)
        return None
    event = st.dataframe(view, hide_index=True, column_config=config, height=height, key=key,
                         on_select=on_select, selection_mode="single-row")
    rows = list(getattr(getattr(event, "selection", None), "rows", []) or [])
    if rows and rows[0] < len(df):
        return df.iloc[rows[0]].to_dict()
    return None


def download_csv_button(label: str, data: bytes, filename: str, key: str,
                        help: str | None = None, disabled: bool = False) -> None:
    st.download_button(label, data=data, file_name=filename, mime="text/csv", key=key,
                       help=help, icon=":material/download:", disabled=disabled)


# ============================================================================ charts
def show_chart(fig: go.Figure, key: str) -> None:
    st.plotly_chart(fig, theme=None, key=key, config=T.PLOTLY_CONFIG, width="stretch")


def _base_layout(fig: go.Figure, title: str | None, height: int, **kw: Any) -> go.Figure:
    kw.setdefault("plot_bgcolor", T.SURFACE)
    kw.setdefault("paper_bgcolor", T.SURFACE)
    fig.update_layout(template=T.PLOTLY_TEMPLATE, height=height,
                      title=dict(text=title) if title else None, **kw)
    return fig


def _title_over_legend(fig: go.Figure, top: int = 96) -> None:
    """Title on its own line at the top; a left-aligned legend row sits directly above the plot."""
    fig.update_layout(margin=dict(t=top),
                      title=dict(yref="container", y=1, yanchor="top", pad=dict(t=14)),
                      legend=dict(orientation="h", yanchor="bottom", y=1.01, xanchor="left", x=0))


def _percentile_values(stats: dict) -> list[tuple[str, float]]:
    out = []
    for key in ("p50", "p95", "p99"):
        v = stats.get(f"{key}_ms")
        if not _missing(v):
            out.append((key, float(v)))
    return out


def _add_percentile_markers(fig: go.Figure, stats: dict, labels: bool = True) -> None:
    for i, (key, value) in enumerate(_percentile_values(stats)):
        fig.add_vline(x=value, line_color=T.PERCENTILE_COLORS[key], line_width=2)
        if labels:
            fig.add_annotation(x=value, xref="x", y=1.0 - i * 0.09, yref="paper",
                               text=f"<b>{T.PERCENTILE_LABELS[key]}</b> {value:,.1f} ms",
                               showarrow=False, xanchor="left", xshift=4,
                               font=dict(size=12, color=T.INK), bgcolor="rgba(255,255,255,0.85)")


def latency_histogram(latencies: Sequence[float], stats: dict, title: str | None = None,
                      height: int = 340) -> go.Figure:
    lat = [float(x) for x in latencies if not _missing(x)]
    nbins = int(min(60, max(10, math.sqrt(max(len(lat), 1)) * 2)))
    fig = go.Figure(go.Histogram(
        x=lat, nbinsx=nbins, marker=dict(color="#C3CBDC", line=dict(color=T.SURFACE, width=1)),
        hovertemplate="%{x} ms<br>%{y} requests<extra></extra>", name="Requests"))
    _add_percentile_markers(fig, stats)
    _base_layout(fig, title, height, showlegend=False, bargap=0.04)
    fig.update_xaxes(title_text="Client-observed latency (ms)")
    fig.update_yaxes(title_text="Requests")
    return fig


def latency_box(latencies: Sequence[float], stats: dict, title: str | None = None,
                height: int = 200) -> go.Figure:
    lat = [float(x) for x in latencies if not _missing(x)]
    fig = go.Figure(go.Box(
        x=lat, name="", boxpoints="outliers", quartilemethod="linear", orientation="h",
        marker=dict(color=T.INK_MUTED, size=6), line=dict(color=T.INK_SECONDARY, width=1.5),
        fillcolor=T.TS_BLUE_10, hoverinfo="x"))
    _add_percentile_markers(fig, stats)
    _base_layout(fig, title, height, showlegend=False, margin=dict(l=24, r=24, t=56, b=40))
    fig.update_xaxes(title_text="Client-observed latency (ms)")
    fig.update_yaxes(showticklabels=False, showline=False)
    return fig


def latency_over_time(df: pd.DataFrame, title: str | None = None, height: int = 340) -> go.Figure:
    """Each request as a dot; failures (red ×) and timeouts (amber ▲) stay visible."""
    d = prepare_results(df)
    fig = go.Figure()
    if len(d):
        x = d["started_dt"] if "started_dt" in d else d.index
        d = d.assign(_x=x)
        ok = d[d["success"]]
        timeouts = d[(~d["success"]) & d["is_timeout"]]
        failed = d[(~d["success"]) & ~d["is_timeout"]]
        fig.add_trace(go.Scatter(
            x=ok["_x"], y=ok["latency_ms"], mode="markers", name="Success",
            marker=dict(color=T.TS_BLUE, size=7, line=dict(color=T.SURFACE, width=1)),
            hovertemplate="%{x|%H:%M:%S.%L}<br>%{y:.1f} ms<extra>Success</extra>"))
        if len(failed):
            fig.add_trace(go.Scatter(
                x=failed["_x"], y=failed["latency_ms"].fillna(0), mode="markers", name="Failure",
                marker=dict(color=T.RED, size=10, symbol="x"),
                customdata=failed[["outcome", "http_status"]].astype(str).values,
                hovertemplate="%{x|%H:%M:%S.%L}<br>%{y:.1f} ms<br>%{customdata[0]} · HTTP "
                              "%{customdata[1]}<extra>Failure</extra>"))
        if len(timeouts):
            fig.add_trace(go.Scatter(
                x=timeouts["_x"], y=timeouts["latency_ms"].fillna(0), mode="markers",
                name="Timeout", marker=dict(color=T.AMBER, size=10, symbol="triangle-up",
                                            line=dict(color=T.AMBER_TEXT, width=1)),
                hovertemplate="%{x|%H:%M:%S.%L}<br>%{y:.1f} ms<extra>Timeout</extra>"))
    _base_layout(fig, title, height, hovermode="closest")
    fig.update_xaxes(title_text="Request start (UTC)")
    fig.update_yaxes(title_text="Latency (ms)", rangemode="tozero")
    return fig


def status_breakdown(df: pd.DataFrame, title: str | None = None, height: int | None = None) -> go.Figure:
    d = prepare_results(df)
    fig = go.Figure()
    if len(d):
        status = d["http_status"].astype("string").fillna("no response")
        grp = (d.assign(_status=status).groupby(["outcome", "_status"], dropna=False)
               .size().reset_index(name="count").sort_values("count"))
        labels = [f"{T.OUTCOME_LABELS.get(o, o)} · HTTP {s}" if s != "no response"
                  else f"{T.OUTCOME_LABELS.get(o, o)} · no response"
                  for o, s in zip(grp["outcome"], grp["_status"])]
        fig.add_trace(go.Bar(
            y=labels, x=grp["count"], orientation="h", text=grp["count"],
            textposition="outside", cliponaxis=False,
            marker=dict(color=[T.OUTCOME_COLORS.get(o, T.INK_MUTED) for o in grp["outcome"]],
                        cornerradius=4),
            hovertemplate="%{y}<br>%{x} requests<extra></extra>", width=0.5))
        height = height or max(160, 70 + 44 * len(grp))
    _base_layout(fig, title, height or 200, showlegend=False, margin=dict(l=24, r=48, t=40, b=40))
    fig.update_xaxes(title_text="Requests", rangemode="tozero")
    return fig


# Baseline vs candidate: two shades of one hue (validated ordinal pair, light -> dark).
BASELINE_COLOR = T.PERCENTILE_COLORS["p50"]
CANDIDATE_COLOR = T.PERCENTILE_COLORS["p99"]
_PCTS = ("p50", "p95", "p99")


def _bar_height(groups: int, bars_per_group: int, min_height: int = 260) -> int:
    return max(min_height, 120 + groups * (18 * bars_per_group + 22))


def comparison_bars(metrics_df: pd.DataFrame, label_col: str = "label",
                    title: str | None = None) -> go.Figure:
    """Grouped horizontal bars of P50/P95/P99, one group per row of ``metrics_df`` (one run).

    Rows render top-down in frame order; inside each group P50 sits on top, P99 at the bottom.
    """
    d = metrics_df.iloc[::-1]
    fig = go.Figure()
    for key in reversed(_PCTS):          # last trace is drawn on top of each group
        fig.add_trace(go.Bar(
            y=d[label_col], x=d[f"{key}_ms"], name=T.PERCENTILE_LABELS[key], orientation="h",
            marker=dict(color=T.PERCENTILE_COLORS[key], cornerradius=4,
                        line=dict(color=T.SURFACE, width=1)),
            hovertemplate="%{y}<br>" + T.PERCENTILE_LABELS[key] + " %{x:,.1f} ms<extra></extra>"))
    _base_layout(fig, title, _bar_height(len(d), 3), barmode="group", bargap=0.28,
                 bargroupgap=0.06, legend=dict(traceorder="reversed"))
    fig.update_xaxes(title_text="Latency (ms)", rangemode="tozero")
    fig.update_yaxes(showline=False)
    return fig


def comparison_bars_by_run(metrics_df: pd.DataFrame, run_order: Sequence[str],
                           run_titles: dict[str, str], endpoint_order: Sequence[str],
                           label_col: str = "endpoint_name", title: str | None = None) -> go.Figure:
    """Small multiples: one panel per run (P50/P95/P99 bars per endpoint), shared axes.

    Endpoints keep the same row in every panel and all panels share one latency scale, so
    panels compare by eye without ever pooling samples from different runs.
    """
    from plotly.subplots import make_subplots

    runs = list(run_order)
    fig = make_subplots(rows=1, cols=max(1, len(runs)), shared_yaxes=True, horizontal_spacing=0.03,
                        subplot_titles=[run_titles.get(r, r) for r in runs])
    rows_order = list(endpoint_order)[::-1]
    vmax = 0.0
    for col, rid in enumerate(runs, start=1):
        d = (metrics_df[metrics_df["run_id"] == rid].drop_duplicates(label_col)
             .set_index(label_col).reindex(rows_order))
        for key in reversed(_PCTS):
            vals = pd.to_numeric(d[f"{key}_ms"], errors="coerce")
            if vals.notna().any():
                vmax = max(vmax, float(vals.max()))
            fig.add_trace(go.Bar(
                y=rows_order, x=vals, name=T.PERCENTILE_LABELS[key], orientation="h",
                legendgroup=key, showlegend=col == 1,
                marker=dict(color=T.PERCENTILE_COLORS[key], cornerradius=4,
                            line=dict(color=T.SURFACE, width=1)),
                hovertemplate="%{y}<br>" + T.PERCENTILE_LABELS[key] + " %{x:,.1f} ms<extra>"
                              + esc(run_titles.get(rid, rid)) + "</extra>"), row=1, col=col)
    _base_layout(fig, title, _bar_height(len(rows_order), 3), barmode="group", bargap=0.28,
                 bargroupgap=0.06, legend=dict(traceorder="reversed"),
                 margin=dict(l=56, r=24, t=96, b=48))
    fig.update_xaxes(range=[0, vmax * 1.08 if vmax else 1], title_text="Latency (ms)")
    fig.update_yaxes(showline=False, categoryorder="array", categoryarray=rows_order)
    for ann in fig.layout.annotations:
        ann.update(font=dict(size=12, color=T.INK_SECONDARY))
    return fig


def run_bars(metrics_df: pd.DataFrame, value_col: str, run_order: Sequence[str],
             run_titles: dict[str, str], endpoint_order: Sequence[str],
             label_col: str = "endpoint_name", title: str | None = None,
             x_title: str = "Latency (ms)") -> go.Figure:
    """One group per endpoint, one bar per run (fixed categorical hue per run, oldest first).

    Callers cap ``run_order`` at ``T.MAX_CATEGORICAL_SERIES``; a run that did not measure an
    endpoint simply has no bar there (never a zero bar).
    """
    rows_order = list(endpoint_order)[::-1]
    fig = go.Figure()
    runs = list(run_order)
    for i, rid in reversed(list(enumerate(runs))):   # first run drawn on top of each group
        d = (metrics_df[metrics_df["run_id"] == rid].drop_duplicates(label_col)
             .set_index(label_col).reindex(rows_order))
        samples = pd.to_numeric(d["samples"], errors="coerce") if "samples" in d else None
        fig.add_trace(go.Bar(
            y=rows_order, x=pd.to_numeric(d[value_col], errors="coerce"),
            name=run_titles.get(rid, rid), orientation="h",
            marker=dict(color=T.series_color(i), cornerradius=4, line=dict(color=T.SURFACE, width=1)),
            customdata=(samples.fillna(0).astype(int) if samples is not None else None),
            hovertemplate="%{y}<br>%{x:,.1f} ms · n = %{customdata}<extra>"
                          + esc(run_titles.get(rid, rid)) + "</extra>"))
    _base_layout(fig, title, _bar_height(len(rows_order), len(runs)), barmode="group",
                 bargap=0.28, bargroupgap=0.06,
                 legend=dict(traceorder="reversed"))
    _title_over_legend(fig)
    fig.update_xaxes(title_text=x_title, rangemode="tozero")
    fig.update_yaxes(showline=False, categoryorder="array", categoryarray=rows_order)
    return fig


def change_dumbbell(change: pd.DataFrame, pct: str, base_name: str, cand_name: str,
                    title: str | None = None) -> go.Figure:
    """Baseline -> candidate per endpoint for one percentile (dumbbell, one hue, two shades).

    ``change`` is ``analytics.run_change_frame``; rows render top-down in frame order. The change
    label beside each pair is ink text with an arrow (▲ slower, ▼ faster); the table carries colour.
    """
    d = change.dropna(subset=[f"{pct}_base", f"{pct}_cand"], how="all").iloc[::-1]
    names = d["endpoint_name"].astype(str).tolist()
    base = pd.to_numeric(d[f"{pct}_base"], errors="coerce")
    cand = pd.to_numeric(d[f"{pct}_cand"], errors="coerce")
    xs: list = []
    ys: list = []
    for n, b, c in zip(names, base, cand):
        if not (_missing(b) or _missing(c)):
            xs += [b, c, None]
            ys += [n, n, None]
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=xs, y=ys, mode="lines", line=dict(color=T.AXIS, width=3),
                             hoverinfo="skip", showlegend=False))
    label = T.PERCENTILE_LABELS[pct]
    for vals, name, color in ((base, base_name, BASELINE_COLOR), (cand, cand_name, CANDIDATE_COLOR)):
        fig.add_trace(go.Scatter(
            x=vals, y=names, mode="markers", name=name,
            marker=dict(color=color, size=13, line=dict(color=T.SURFACE, width=2)),
            hovertemplate="%{y}<br>" + label + " %{x:,.1f} ms<extra>" + esc(name) + "</extra>"))
    both = pd.concat([base, cand])
    vmax = float(both.max()) if both.notna().any() else 1.0
    for n, b, c, a, p in zip(names, base, cand, d[f"{pct}_abs"], d[f"{pct}_pct"]):
        if _missing(a):
            continue
        a = float(a)
        arrow = "▲" if a > 0 else ("▼" if a < 0 else "=")
        pct_txt = "" if _missing(p) else f" ({float(p):+.0f}%)"
        fig.add_annotation(x=max(float(b), float(c)), y=n, xref="x", yref="y",
                           text=f"{arrow} {a:+,.1f} ms{pct_txt}", showarrow=False,
                           xanchor="left", xshift=12, font=dict(size=12, color=T.INK_SECONDARY))
    _base_layout(fig, title, max(240, 110 + 40 * len(names)), hovermode="closest",
                 margin=dict(l=56, r=24, t=56, b=48))
    _title_over_legend(fig)
    fig.update_xaxes(title_text=f"{label} latency (ms)", range=[0, vmax * 1.3])
    fig.update_yaxes(showline=False, showgrid=True, categoryorder="array", categoryarray=names)
    return fig


def trend_lines(history: pd.DataFrame, series_col: str, value_col: str, x_col: str = "run_time",
                title: str | None = None, colors: dict[str, str] | None = None,
                y_title: str = "Latency (ms)", dashes: dict[str, str] | None = None,
                x_title: str = "Run start (IST)") -> go.Figure:
    """One 2px line per series across runs; synthetic runs drawn as hollow markers.

    ``dashes`` maps a series to a Plotly dash style (e.g. cold-connection series drawn dotted).
    Timezone-aware x values are shown in India time.
    """
    fig = go.Figure()
    for i, (name, g) in enumerate(history.groupby(series_col, sort=False)):
        g = g.sort_values(x_col)
        x = g[x_col]
        if pd.api.types.is_datetime64_any_dtype(x) and getattr(x.dt, "tz", None) is not None:
            x = x.dt.tz_convert(IST).dt.tz_localize(None)
        color = (colors or {}).get(str(name), T.series_color(i))
        synth = g["is_synthetic"].astype(bool).tolist() if "is_synthetic" in g else [False] * len(g)
        extra = (g["hover"].astype(str) if "hover" in g
                 else g["run_id"].astype(str) if "run_id" in g else pd.Series([""] * len(g)))
        fig.add_trace(go.Scatter(
            x=x, y=g[value_col], name=str(name), mode="lines+markers",
            line=dict(color=color, width=2, dash=(dashes or {}).get(str(name), "solid")),
            marker=dict(size=9, color=[T.SURFACE if s else color for s in synth],
                        line=dict(color=color, width=2)),
            customdata=np.stack([extra.to_numpy(dtype=object),
                                 np.array(["SYNTHETIC" if s else "measured" for s in synth],
                                          dtype=object)], axis=-1),
            hovertemplate="%{x|%d %b %H:%M}<br>%{y:,.1f} ms<br>%{customdata[0]} "
                          "(%{customdata[1]})<extra>" + esc(name) + "</extra>"))
    _base_layout(fig, title, 400, hovermode="closest")
    _title_over_legend(fig, top=104)
    fig.update_xaxes(title_text=x_title)
    fig.update_yaxes(title_text=y_title, rangemode="tozero")
    return fig


# ============================================================================ change tables
def style_change_table(df: pd.DataFrame, lower_is_better: Sequence[str] = (),
                       higher_is_better: Sequence[str] = (), ms_cols: Sequence[str] = (),
                       pct_cols: Sequence[str] = (), pts_cols: Sequence[str] = (),
                       value_cols: Sequence[str] = (), rate_cols: Sequence[str] = ()) -> Any:
    """Pandas Styler for a numeric change table: signed values with ▲/▼ and status colours.

    Improvements (lower latency or failure rate, higher success rate) are green and regressions
    red, always next to the arrow glyph so colour is never the only cue. Columns stay numeric,
    so sorting in ``st.dataframe`` still sorts by value.
    """
    def signed(unit: str, digits: int, none_text: str = DASH):
        def fmt(v: Any) -> str:
            if _missing(v):
                return none_text
            v = float(v)
            arrow = "▲ " if v > 0 else ("▼ " if v < 0 else "")
            return f"{arrow}{v:+,.{digits}f}{unit}"
        return fmt

    def plain(v: Any) -> str:
        return DASH if _missing(v) else f"{float(v):,.1f}"

    def rate(v: Any) -> str:
        return DASH if _missing(v) else f"{float(v):.1f}%"

    fmts: dict[str, Any] = {}
    fmts.update({c: signed(" ms", 1) for c in ms_cols})
    fmts.update({c: signed("%", 1, none_text="n/a") for c in pct_cols})
    fmts.update({c: signed(" pts", 1) for c in pts_cols})
    fmts.update({c: plain for c in value_cols})
    fmts.update({c: rate for c in rate_cols})

    def tone(good_when_negative: bool):
        def css(v: Any) -> str:
            if _missing(v) or float(v) == 0:
                return f"color: {T.INK_MUTED}"
            good = (float(v) < 0) == good_when_negative
            return f"color: {T.GREEN_TEXT if good else T.RED_TEXT}; font-weight: 600"
        return css

    styler = df.style.format({k: v for k, v in fmts.items() if k in df.columns})
    lower = [c for c in lower_is_better if c in df.columns]
    higher = [c for c in higher_is_better if c in df.columns]
    if lower:
        styler = styler.map(tone(True), subset=lower)
    if higher:
        styler = styler.map(tone(False), subset=higher)
    return styler


def interval_histogram(values: Sequence[float], stats: dict, title: str | None = None) -> go.Figure:
    fig = latency_histogram(values, stats, title=title)
    fig.update_xaxes(title_text="Message arrival interval (ms)")
    fig.update_yaxes(title_text="Intervals")
    return fig


# ============================================================================ empty states
def empty_state(title: str, body: str, icon: str = ":material/inbox:") -> None:
    with st.container(border=True):
        st.markdown(f"#### {icon} {title}")
        st.markdown(body)
