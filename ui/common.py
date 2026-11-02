"""Utilidades compartidas del panel Streamlit."""
from __future__ import annotations

from contextlib import contextmanager
from decimal import Decimal

import pandas as pd
import streamlit as st

from mototrade.config import get_rules, get_settings
from mototrade.core.money import fmt_ars
from mototrade.db import session_scope
from mototrade.security.auth import Principal, verify_token

ROLE_RANK = {"viewer": 0, "operator": 1, "admin": 2}


def rules():
    """Reglas EFECTIVAS (YAML + Centro de Control). El panel nunca usa el YAML crudo."""
    from mototrade.control import ControlCenter

    with session_scope() as s:
        return ControlCenter(s).effective_rules(get_rules())


def control_settings():
    from mototrade.control import ControlCenter

    with session_scope() as s:
        return ControlCenter(s).load()


def gate_snapshot():
    from mototrade.control import OperationsGate

    with session_scope() as s:
        g = OperationsGate(s)
        snap = {k: (v.intended, v.effective, v.label, v.reason) for k, v in g.snapshot().items()}
        return snap, g.position.as_dict(), {k: str(v) for k, v in g.ai_budget.items()}


def settings():
    return get_settings()


@contextmanager
def db():
    with session_scope() as s:
        yield s


def principal() -> Principal | None:
    tok = st.session_state.get("token")
    return verify_token(tok) if tok else None


def require_login() -> Principal:
    p = principal()
    if p is None:
        st.stop()
    return p


def has_role(p: Principal, min_role: str) -> bool:
    return ROLE_RANK.get(p.role, -1) >= ROLE_RANK[min_role]


def money(v) -> str:
    return "—" if v is None else fmt_ars(v)


def pct(v) -> str:
    return "—" if v is None else f"{Decimal(v):.1%}"


def df(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows) if rows else pd.DataFrame()


def flash(kind: str, text: str):
    st.session_state["_flash"] = (kind, text)


def show_flash():
    f = st.session_state.pop("_flash", None)
    if f:
        getattr(st, f[0])(f[1])


def pending_badge(session) -> int:
    from mototrade.core import approvals

    return len(approvals.pending(session))


def inject_css():
    st.markdown(
        """
        <style>
        .block-container {padding-top: 1.2rem; padding-bottom: 2rem; max-width: 1400px;}
        [data-testid="stMetricValue"] {font-size: 1.4rem;}
        @media (max-width: 768px) {
            .block-container {padding-left: 0.6rem; padding-right: 0.6rem;}
            [data-testid="stMetricValue"] {font-size: 1.1rem;}
            [data-testid="column"] {width: 100% !important; flex: 1 1 100% !important; min-width: 100% !important;}
        }
        .mt-warn {background:#fff4e5;border-left:4px solid #ff9800;padding:.5rem .8rem;border-radius:4px;margin:.3rem 0;}
        .mt-ok {background:#e8f5e9;border-left:4px solid #4caf50;padding:.5rem .8rem;border-radius:4px;margin:.3rem 0;}
        .mt-sim {background:#ede7f6;border-left:4px solid #673ab7;padding:.3rem .6rem;border-radius:4px;font-size:.85rem;}
        </style>
        """,
        unsafe_allow_html=True,
    )


def simulated_banner(text: str):
    st.markdown(f'<div class="mt-sim">🧪 SIMULADO · {text}</div>', unsafe_allow_html=True)
