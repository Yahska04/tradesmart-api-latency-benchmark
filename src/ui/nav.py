"""Navigation helpers that degrade gracefully when a page runs standalone (e.g. in AppTest)."""
from __future__ import annotations

import streamlit as st

PAGE_REGISTRY = "views/1_Endpoint_Registry.py"
PAGE_RUN = "views/2_Run_Benchmark.py"
PAGE_ANALYTICS = "views/3_Performance_Analytics.py"
PAGE_HISTORY = "views/4_Historical_Comparison.py"
PAGE_SETTINGS = "views/5_Settings.py"
PAGE_METHODOLOGY = "views/6_Methodology.py"
PAGE_WEBSOCKET = "views/7_WebSocket_Benchmark.py"
PAGE_CHECK = "views/8_Endpoint_Check.py"
PAGE_ORDER_TEST = "views/9_Order_Latency_Test.py"


def nav_link(page: str, label: str, icon: str | None = None) -> None:
    try:
        st.page_link(page, label=label, icon=icon)
    except Exception:
        st.markdown(f"{icon + ' ' if icon else ''}{label}")


def go_to(page: str) -> None:
    try:
        st.switch_page(page)
    except Exception:
        st.info(f"Open **{label_for(page)}** from the sidebar.")


def label_for(page: str) -> str:
    return page.split("/")[-1].split("_", 1)[-1].removesuffix(".py").replace("_", " ")
