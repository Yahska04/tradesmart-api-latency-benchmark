"""Instrument search widgets backed by the downloaded script master (``src/script_master.py``).

``instrument_picker`` is the small reusable building block (search box + matches). ``instrument_fields``
wraps it for pages whose request bodies use ``{{exch}}``, ``{{token}}``, ``{{tsym}}`` or ``{{sym}}``:
the picked instrument fills those values and an "Enter values manually" toggle keeps typed values
available (a typed value always wins).
"""
from __future__ import annotations

from typing import Iterable, Sequence

import streamlit as st

from src import script_master as sm
from src.script_master import Instrument

# Request-body placeholders an instrument can fill. EODChartData's "sym" is "EXCH:TSYM" per the docs.
INSTRUMENT_PLACEHOLDERS: tuple[str, ...] = ("exch", "token", "tsym", "sym")
# Endpoints that only make sense for derivatives (GetOptionChain needs an option/future symbol).
DERIVATIVE_ONLY_ENDPOINTS: frozenset[str] = frozenset({"GetOptionChain"})


def instrument_values(inst: Instrument | None, names: Iterable[str] = INSTRUMENT_PLACEHOLDERS) -> dict[str, str]:
    """Placeholder values for ``inst``: exch, token, tsym and sym (``"EXCH:TSYM"``)."""
    if inst is None:
        return {}
    values = {"exch": inst.exch, "token": inst.token, "tsym": inst.tsym, "sym": f"{inst.exch}:{inst.tsym}"}
    return {k: values[k] for k in names if k in values}


def option_label(inst: Instrument) -> str:
    """Selectbox label: exchange · trading symbol · token, plus lot/expiry for derivatives."""
    parts = [inst.exch, inst.tsym, f"token {inst.token}"]
    if inst.is_derivative:
        if inst.expiry:
            parts.append(f"exp {inst.expiry}")
        parts.append(f"lot {inst.lot_size:,}")
    return " · ".join(parts)


def _unavailable_message(exchanges: Sequence[str] | None) -> None:
    info = sm.status()
    scope = ", ".join(exchanges) if exchanges else "any exchange"
    if info["state"] == "downloading":
        current = f" (now: {info['current']})" if info.get("current") else ""
        st.info(f"The script master is downloading{current}. Instrument search for {scope} will be "
                "available in a moment — rerun the page, or enter the values manually meanwhile.",
                icon=":material/downloading:")
    else:
        st.warning(f"No script master for {scope} yet, so instruments can't be searched. Open "
                   "**Settings → Script master** and press **Refresh script master**, or enter the "
                   "values manually.", icon=":material/cloud_off:")


def instrument_picker(key: str, *, exchanges: Sequence[str] | None = None, label: str = "Instrument",
                      help: str | None = None, default_tsym: str | None = None) -> Instrument | None:
    """Search box + selectbox of matching instruments. Returns the chosen instrument or ``None``.

    ``key`` namespaces the widgets (``{key}_q`` and ``{key}_sel``). ``exchanges`` limits the search
    (e.g. ``["NSE", "BSE"]``). ``default_tsym`` pre-fills the search box on first render.
    """
    if not sm.has_data(exchanges):
        _unavailable_message(exchanges)
        return None
    qkey, skey = f"{key}_q", f"{key}_sel"
    if qkey not in st.session_state and default_tsym:
        st.session_state[qkey] = default_tsym
    scope = " / ".join(exchanges) if exchanges else "all exchanges"
    c1, c2 = st.columns([1, 2])
    query = c1.text_input(f"{label} search", key=qkey, placeholder="e.g. SBIN, NIFTY 25000 CE",
                          help=help or f"Type a symbol or part of one. Searches {scope} in the "
                                       "downloaded script master; several words narrow it down.")
    query = (query or "").strip()
    if not query:
        c2.selectbox(label, ["Type in the search box to list matches"], disabled=True, key=f"{key}_ph")
        return None
    matches = sm.search(query, exchanges, limit=25)
    if not matches:
        c2.selectbox(label, [f"No match for “{query}”"], disabled=True, key=f"{key}_none")
        st.caption(f"Nothing in {scope} matches “{query}”. Try a shorter or different spelling.")
        return None
    by_key = {m.key: m for m in matches}
    if st.session_state.get(skey) not in by_key:
        st.session_state.pop(skey, None)  # stale choice from an earlier search
    chosen = c2.selectbox(label, list(by_key), key=skey, format_func=lambda k: option_label(by_key[k]),
                          help="Best matches first: exact symbol, then prefix, then contains.")
    return by_key.get(chosen) if chosen else None


def instrument_fields(key: str, names: Iterable[str], *, endpoint_ids: Iterable[str] = (),
                      examples: dict[str, str | None] | None = None) -> dict[str, str]:
    """Picker + "Enter values manually" toggle for the instrument placeholders in ``names``.

    Returns ``{placeholder: value}`` for exch/token/tsym/sym. Manual values override the picker.
    """
    wanted = [n for n in INSTRUMENT_PLACEHOLDERS if n in set(names)]
    if not wanted:
        return {}
    exchanges = list(sm.DERIVATIVE_EXCHANGES) if set(endpoint_ids) & DERIVATIVE_ONLY_ENDPOINTS else None
    available = sm.has_data(exchanges)
    picked = instrument_picker(key, exchanges=exchanges)
    values = instrument_values(picked, wanted)
    if picked is not None:
        st.caption("Fills " + " · ".join(f"`{k}` = `{v}`" for k, v in values.items())
                   + (f" · lot size {picked.lot_size:,}" if picked.is_derivative else ""))
    elif exchanges and available:
        st.caption("Option chain needs a derivative: search NFO, BFO, CDS, BCD or MCX, e.g. “NIFTY 25000 CE”.")
    manual = st.toggle("Enter values manually", key=f"{key}_manual", value=not available,
                       help="Type exch / token / tsym yourself. A typed value overrides the picked instrument.")
    if manual:
        cols = st.columns(len(wanted))
        for col, name in zip(cols, wanted):
            hint = values.get(name) or (examples or {}).get(name) or ""
            typed = col.text_input(name, key=f"{key}_manual_{name}",
                                   placeholder=f"from picker: {hint}" if values.get(name) else hint,
                                   help="Leave empty to use the picked instrument."
                                   + (" Format EXCH:TSYM, e.g. NSE:SBIN-EQ." if name == "sym" else ""))
            if typed.strip():
                values[name] = typed.strip()
    return values


def sidebar_status() -> None:
    """Compact one-line script-master status for the sidebar (auto-refreshes while downloading)."""
    try:
        downloading = sm.is_refreshing()
    except Exception:
        return

    @st.fragment(run_every=3.0 if downloading else None)
    def _line() -> None:
        try:
            info = sm.status()
        except Exception:
            st.caption(":material/cloud_off: Script master: unavailable")
            return
        state = info["state"]
        n_ok, n_total = info["exchanges_ready"], len(info["per_exchange"])
        stamp = sm.to_ist(info.get("updated_at"))
        when = f" · updated {stamp:%H:%M} IST" if stamp else ""
        if state == "downloading":
            now = f" {info['current']}" if info.get("current") else ""
            text = f":material/downloading: Script master: downloading{now}…"
        elif state == "ready":
            text = f":material/check_circle: Script master: ready · {n_ok} exchanges{when}"
        elif state == "partial":
            text = (f":material/warning: Script master: {n_ok} of {n_total} exchanges{when} — "
                    "some failed, retry in Settings")
        elif state == "pending":
            text = (f":material/downloading: Script master: {n_ok} of {n_total} exchanges ready{when} — "
                    "the rest download on the next refresh")
        elif state == "error":
            text = ":material/error: Script master: failed — retry in Settings"
        else:
            text = ":material/cloud_download: Script master: not downloaded yet"
        st.caption(text)

    with st.sidebar:
        _line()
