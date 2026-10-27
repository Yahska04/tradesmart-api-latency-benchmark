"""TradeSmart brand theme for the Streamlit UI: page config, CSS, sidebar logo, Plotly template.

Colour + type tokens come from docs/TradeSmart_Brandbook.md. Chart palettes were checked with the
dataviz palette validator (light surface #FFFFFF):
  * PERCENTILE_COLORS - ordinal one-hue ramp of TradeSmart Blue (P50 light -> P99 dark): PASS.
  * CATEGORICAL - six identity hues led by TradeSmart Blue, fixed order: PASS
    (two slots sit under 3:1 contrast, so charts always carry a legend/table view).
  * Status colours are the brand secondary colours and are reserved for state; they always
    travel with an icon or label, never colour alone.
"""
from __future__ import annotations

from pathlib import Path

import plotly.graph_objects as go
import plotly.io as pio
import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ASSETS_DIR = PROJECT_ROOT / "assets"
LOGO_COLOUR = ASSETS_DIR / "tradesmart_logo.png"
LOGO_WHITE = ASSETS_DIR / "tradesmart_logo_white.png"
MONOGRAM = ASSETS_DIR / "ts_monogram.png"
FAVICON = ASSETS_DIR / "favicon.png"

# --- Brand colour tokens -------------------------------------------------------------------
DARK_BLUE = "#151E34"        # primary
TS_BLUE = "#3F71EB"          # accent (100%)
TS_BLUE_80 = "#658DEF"
TS_BLUE_40 = "#B2C6F7"
TS_BLUE_10 = "#ECF1FD"
GREEN = "#00B48E"            # success
RED = "#E02B59"              # failure
AMBER = "#EEB268"            # warning / synthetic
WHITE = "#FFFFFF"

# Ink + chrome (neutral steps tuned to the Dark Blue primary)
INK = DARK_BLUE
INK_SECONDARY = "#4A5571"
INK_MUTED = "#6F7891"
GRID = "#E6EAF2"
AXIS = "#C9D0DE"
BORDER = "#D9E0EE"
SURFACE = WHITE

# Text-safe darker steps of status colours (for text on tinted pills)
GREEN_TEXT = "#007A60"
RED_TEXT = "#B01E45"
AMBER_TEXT = "#7A5212"

# --- Chart palettes ------------------------------------------------------------------------
PERCENTILE_COLORS: dict[str, str] = {"p50": "#8FAAF3", "p95": TS_BLUE, "p99": "#1C3F99"}
PERCENTILE_LABELS: dict[str, str] = {"p50": "P50", "p95": "P95", "p99": "P99"}
CATEGORICAL: list[str] = ["#3F71EB", "#EB6834", "#1BAF7A", "#C98500", "#E87BA4", "#6250D6"]
MAX_CATEGORICAL_SERIES = len(CATEGORICAL)

OUTCOME_COLORS: dict[str, str] = {
    "success": GREEN,
    "api_error": RED,
    "http_error": RED,
    "rate_limited": AMBER,
    "timeout": AMBER,
    "connect_error": "#8A93A8",
    "dns_error": "#8A93A8",
    "other_error": "#8A93A8",
}
OUTCOME_LABELS: dict[str, str] = {
    "success": "Success",
    "api_error": "API error (stat Not_Ok)",
    "http_error": "HTTP error",
    "rate_limited": "Rate limited (429)",
    "timeout": "Timeout",
    "connect_error": "Connection error",
    "dns_error": "DNS error",
    "other_error": "Other error",
}

FONT_BODY = "'Trebuchet MS', 'Segoe UI', sans-serif"
FONT_HEAD = "Roboto, 'Trebuchet MS', 'Segoe UI', sans-serif"

PLOTLY_TEMPLATE = "tradesmart"
PLOTLY_CONFIG: dict = {"displaylogo": False,
                       "modeBarButtonsToRemove": ["lasso2d", "select2d", "autoScale2d"]}


def _register_plotly_template() -> None:
    if PLOTLY_TEMPLATE in pio.templates:
        return
    base = go.layout.Template(pio.templates["plotly_white"])
    base.layout.update(
        font=dict(family=FONT_BODY, size=13, color=INK_SECONDARY),
        title=dict(font=dict(family=FONT_HEAD, size=16, color=INK), x=0, xanchor="left"),
        colorway=CATEGORICAL,
        paper_bgcolor=SURFACE,
        plot_bgcolor=SURFACE,
        margin=dict(l=56, r=24, t=48, b=48),
        hoverlabel=dict(bgcolor=WHITE, bordercolor=BORDER,
                        font=dict(family=FONT_BODY, size=12, color=INK)),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1,
                    font=dict(size=12, color=INK_SECONDARY), title_text=""),
        bargap=0.35,
        bargroupgap=0.08,
    )
    axis = dict(gridcolor=GRID, gridwidth=1, linecolor=AXIS, zerolinecolor=AXIS,
                tickfont=dict(family=FONT_HEAD, size=12, color=INK_MUTED),
                title=dict(font=dict(size=12, color=INK_SECONDARY)), showline=True,
                ticks="", automargin=True)
    base.layout.xaxis.update(axis)
    base.layout.yaxis.update(axis)
    pio.templates[PLOTLY_TEMPLATE] = base


_register_plotly_template()
pio.templates.default = PLOTLY_TEMPLATE

_CSS = f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=Roboto:wght@400;500;700&display=swap');
html, body, .stMarkdown p, .stMarkdown li {{ font-family: {FONT_BODY}; }}
h1, h2, h3, h4, h5, h6, .stMarkdown h1, .stMarkdown h2, .stMarkdown h3 {{
  font-family: {FONT_HEAD} !important; color: {DARK_BLUE}; letter-spacing: 0;
}}
h1 {{ font-weight: 700 !important; }}
.block-container {{ padding-top: 2.5rem; max-width: 1440px; }}

/* Sidebar: Dark Blue with the white logo variant and generous clear space */
section[data-testid="stSidebar"] {{ background-color: {DARK_BLUE}; }}
[data-testid="stSidebarHeader"] {{ padding: 24px 24px 16px 24px; }}
[data-testid="stSidebarHeader"] img, [data-testid="stLogo"] {{ min-height: 30px; height: 32px; }}
section[data-testid="stSidebar"] [data-testid="stCaptionContainer"] {{ color: #B9C3DA; }}

/* Metric cards */
.ts-card {{
  background: {WHITE}; border: 1px solid {BORDER}; border-radius: 8px;
  padding: 16px 16px 14px 16px; min-height: 112px; height: 100%;
}}
.ts-card-label {{ font-size: 13px; color: {INK_SECONDARY}; margin-bottom: 6px; }}
.ts-card-value {{ font-family: {FONT_HEAD}; font-size: 28px; font-weight: 500;
  color: {DARK_BLUE}; line-height: 1.15; }}
.ts-card-unit {{ font-size: 15px; font-weight: 400; color: {INK_SECONDARY}; margin-left: 2px; }}
.ts-card-sub {{ font-size: 12px; color: {INK_MUTED}; margin-top: 6px; }}
.ts-card-sub.warn {{ color: {AMBER_TEXT}; }}
.ts-card.tone-success {{ border-top: 3px solid {GREEN}; }}
.ts-card.tone-failure {{ border-top: 3px solid {RED}; }}
.ts-card.tone-accent {{ border-top: 3px solid {TS_BLUE}; }}
.ts-card.tone-neutral {{ border-top: 3px solid {AXIS}; }}

/* Status pills (always text + colour, never colour alone) */
.ts-pill {{ display: inline-block; border-radius: 999px; padding: 2px 10px; font-size: 12px;
  font-weight: 700; letter-spacing: .02em; line-height: 18px; vertical-align: middle;
  font-family: {FONT_HEAD}; white-space: nowrap; }}
.ts-pill.synthetic {{ background: {AMBER}; color: {DARK_BLUE}; }}
.ts-pill.success {{ background: #E0F6F1; color: {GREEN_TEXT}; }}
.ts-pill.failure {{ background: #FCE6EC; color: {RED_TEXT}; }}
.ts-pill.warning {{ background: #FCF1E2; color: {AMBER_TEXT}; }}
.ts-pill.info {{ background: {TS_BLUE_10}; color: #2F5BC9; }}
.ts-pill.neutral {{ background: #EEF0F5; color: {INK_SECONDARY}; }}
.ts-pill.live {{ background: {RED}; color: {WHITE}; }}

/* Run meta block */
.ts-meta {{ display: flex; flex-wrap: wrap; gap: 8px 28px; padding: 12px 16px;
  background: {TS_BLUE_10}; border-radius: 8px; font-size: 13px; color: {INK_SECONDARY}; }}
.ts-meta b {{ color: {DARK_BLUE}; font-weight: 600; }}
.ts-meta .k {{ display: block; font-size: 11px; text-transform: uppercase; letter-spacing: .05em;
  color: {INK_MUTED}; }}
.ts-note {{ font-size: 13px; color: {INK_SECONDARY}; }}
.ts-subtitle {{ color: {INK_SECONDARY}; margin-top: -8px; margin-bottom: 16px; font-size: 15px; }}
</style>
"""


def apply_theme(page_title: str, icon: str | Path | None = None, *, subtitle: str | None = None,
                show_title: bool = True) -> None:
    """Configure the page (wide layout + favicon), inject brand CSS and place the sidebar logo.

    Call this first on every page. ``subtitle`` renders a short lead line under the H1.
    """
    page_icon = str(icon) if icon else (str(FAVICON) if FAVICON.exists() else None)
    try:
        st.set_page_config(page_title=f"{page_title} · TradeSmart latency", page_icon=page_icon,
                           layout="wide", initial_sidebar_state="expanded")
    except Exception:  # pragma: no cover - set_page_config already called this run
        pass
    st.markdown(_CSS, unsafe_allow_html=True)
    if LOGO_WHITE.exists():
        st.logo(str(LOGO_WHITE), size="large",
                icon_image=str(MONOGRAM) if MONOGRAM.exists() else None)
    if show_title:
        st.title(page_title)
        if subtitle:
            st.markdown(f'<div class="ts-subtitle">{subtitle}</div>', unsafe_allow_html=True)


def series_color(index: int) -> str:
    """Fixed-order categorical hue (never cycled; callers cap series at MAX_CATEGORICAL_SERIES)."""
    return CATEGORICAL[index] if 0 <= index < len(CATEGORICAL) else INK_MUTED
