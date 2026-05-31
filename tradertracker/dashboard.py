#!/usr/bin/env python3
"""
TraderTracker interactive dashboard.

Run with:
    uv run streamlit run tradertracker/dashboard.py
"""

from datetime import datetime, timedelta
from pathlib import Path

import duckdb
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from tradertracker.pipeline import EXCLUDED_CVR

DB_PATH = Path("data/tradertracker.duckdb")

st.set_page_config(
    page_title="TraderTracker",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Metric catalogue ───────────────────────────────────────────────────────────

METRICS: dict[str, dict] = {
    "Revenue (DKK)": {
        "col": "omsaetning",
        "fmt": ",.0f",
        "help": "Total revenue. For traders this is the gross trading margin, not the full notional volume.",
    },
    "Net Profit (DKK)": {
        "col": "aarsresultat",
        "fmt": ",.0f",
        "help": "Bottom-line profit after all costs, interest and tax.",
    },
    "EBIT (DKK)": {
        "col": "ebit",
        "fmt": ",.0f",
        "help": "Earnings Before Interest & Tax — operational profit before financing costs.",
    },
    "Opening Equity (DKK)": {
        "col": "egenkapital_primo",
        "fmt": ",.0f",
        "help": "Shareholders' equity at the start of the period (egenkapital primo). Parsed from XBRL comparative figures.",
    },
    "Equity (DKK)": {
        "col": "egenkapital",
        "fmt": ",.0f",
        "help": "Shareholders' equity at the end of the period.",
    },
    "Employees": {
        "col": "ansatte",
        "fmt": ",.0f",
        "help": "Number of employees (from annual report or CVR register).",
    },
    "ROE (%)": {
        "col": "roe_pct",
        "fmt": ".1f",
        "help": "Return on Equity = net profit / equity. Measures how hard the owners' money is working. 20 %+ is excellent for trading firms.",
    },
    "ROCE (%)": {
        "col": "roce_pct",
        "fmt": ".1f",
        "help": "Return on Capital Employed = EBIT / (assets − current liabilities). Like ROE but also accounts for borrowed capital — better for comparing firms with different debt levels.",
    },
    "ROA (%)": {
        "col": "roa_pct",
        "fmt": ".1f",
        "help": "Return on Assets = net profit / total assets. Shows how efficiently a firm uses all its resources, regardless of how they are financed.",
    },
    "Net Margin (%)": {
        "col": "net_margin_pct",
        "fmt": ".1f",
        "help": "Net Margin = net profit / revenue. How many øre of profit per DKK of revenue. Traders often have thin margins (1–5 %) but on very large volumes.",
    },
    "EBIT Margin (%)": {
        "col": "ebit_margin_pct",
        "fmt": ".1f",
        "help": "EBIT Margin = EBIT / revenue. Same as net margin but before interest and tax — better for comparing the pure trading business.",
    },
    "Equity Ratio (%)": {
        "col": "equity_ratio_pct",
        "fmt": ".1f",
        "help": "Equity / total assets. Higher = more financially stable and less dependent on debt. Below 10 % can be risky for volatile trading businesses.",
    },
    "Debt Ratio (%)": {
        "col": "gaeld_ratio_pct",
        "fmt": ".1f",
        "help": "Debt / total assets. The flip side of equity ratio. High debt amplifies returns in good years but hurts badly in bad ones.",
    },
    "Asset Turnover": {
        "col": "aktiv_omsaetning",
        "fmt": ".2f",
        "help": "Revenue / total assets. Energy traders should score very high (10 x+) — they generate huge revenue without holding many physical assets.",
    },
    "Revenue / Employee (tDKK)": {
        "col": "omsaetning_per_ansatte_tdkk",
        "fmt": ",.0f",
        "help": "Revenue per employee in thousands DKK. Top energy traders generate 50–500 million DKK per person per year.",
    },
    "Profit / Employee (tDKK)": {
        "col": "resultat_per_ansatte_tdkk",
        "fmt": ",.0f",
        "help": "Net profit per employee. The best single measure of team quality and efficiency for small trading firms.",
    },
    "Equity / Employee (tDKK)": {
        "col": "egenkapital_per_ansatte_tdkk",
        "fmt": ",.0f",
        "help": "Equity per employee — how much capital each person 'stewards'.",
    },
    "Revenue Growth YoY (%)": {
        "col": "rev_growth_pct",
        "fmt": ".1f",
        "help": "Year-over-year revenue growth. Very important for these young, fast-growing firms.",
    },
    "ROOE (%)": {
        "col": "rooe_pct",
        "fmt": ".1f",
        "help": "Return on Opening Equity = net profit / equity at start of the year. More accurate than ROE for fast-growing firms because it measures return on the capital you actually deployed, not inflated by the same year's profit.",
    },
    "ROTCD (%)": {
        "col": "rotcd_pct",
        "fmt": ".1f",
        "help": "Return on Total Capital Deployed = net profit / opening (equity + related-party loans). Identical to ROOE for companies with no intercompany debt. More realistic for thinly-capitalised firms funded via shareholder loans — common for new ApS setups where the owner injects capital as a loan rather than formal equity.",
    },
    "Total Capital Deployed (DKK)": {
        "col": "total_capital_deployed",
        "fmt": ",.0f",
        "help": "Closing equity + related-party loans (gæld til tilknyttede virksomheder). Captures the full capital the owner has committed to the business, regardless of whether it is structured as formal equity or intercompany debt.",
    },
    "TRADE (%)": {
        "col": "trade_pct",
        "fmt": ".1f",
        "help": "TRADE — Trading Return Adjusted for Deployed Equity and Employment. Gross trading profit divided by headcount multiplied by owner-deployed capital at the start of the year, where deployed capital = opening equity plus opening related-party loans (gæld til tilknyttede virksomheder). Including related-party loans captures the full capital the owner has committed regardless of whether it is structured as formal equity or intercompany debt.",
    },
    "Personnel Costs (DKK)": {
        "col": "personaleomkostninger",
        "fmt": ",.0f",
        "help": "Total personnel costs (Personaleomkostninger) — wages, salaries, pension and social contributions.",
    },
    "Salary / Employee (tDKK)": {
        "col": "personaleomkostninger_per_ansatte_tdkk",
        "fmt": ",.0f",
        "help": "Mean salary cost per employee = total personnel costs / headcount. Proxy for average compensation; includes pension and social costs.",
    },
}

# Grouped order for dropdowns — entries starting with "──" are visual separators
_METRIC_ORDER = [
    "── Returns ──",
    "ROE (%)", "ROOE (%)", "ROTCD (%)", "TRADE (%)", "ROCE (%)", "ROA (%)",
    "── Absolute ──",
    "Net Profit (DKK)", "EBIT (DKK)", "Revenue (DKK)", "Opening Equity (DKK)", "Equity (DKK)", "Total Capital Deployed (DKK)",
    "── Margins ──",
    "Net Margin (%)", "EBIT Margin (%)",
    "── Per Employee ──",
    "Profit / Employee (tDKK)", "Revenue / Employee (tDKK)", "Equity / Employee (tDKK)",
    "── Capital Structure ──",
    "Equity Ratio (%)", "Debt Ratio (%)", "Asset Turnover",
    "── Salary ──",
    "Personnel Costs (DKK)", "Salary / Employee (tDKK)",
    "── Other ──",
    "Employees", "Revenue Growth YoY (%)",
]


def _build_metric_options() -> list[str]:
    return [item if item.startswith("──") else _ccy(item) for item in _METRIC_ORDER]


def _resolve_metric(selected: str, options: list[str]) -> str:
    """If a separator was accidentally selected, return the first real metric after it."""
    if not selected.startswith("──"):
        return selected
    idx = options.index(selected)
    for opt in options[idx + 1:]:
        if not opt.startswith("──"):
            return opt
    return next(o for o in options if not o.startswith("──"))


def _metric_key(label: str) -> str:
    """Map a display label (possibly in EUR) back to the METRICS dict key."""
    if label in METRICS:
        return label
    original = label.replace(currency, "DKK").replace(f"t{currency}", "tDKK")
    return original if original in METRICS else label


# ── Data loading ───────────────────────────────────────────────────────────────

@st.cache_data
def load_employee_monthly() -> pd.DataFrame:
    con = duckdb.connect(str(DB_PATH), read_only=True)
    try:
        df = con.execute("""
            SELECT e.cvr, c.navn, c.is_intraday, c.is_multidesk, c.is_us_trading, c.is_hedgefund,
                   e.aar, e.maaned, e.antal_ansatte, e.antal_aarsvaerk,
                   make_date(e.aar, e.maaned, 1) AS dato
            FROM employee_monthly e
            JOIN companies c USING (cvr)
            ORDER BY c.navn, e.aar, e.maaned
        """).df()
    except Exception:
        df = pd.DataFrame()
    con.close()
    return df


@st.cache_data
def load_company_locations() -> pd.DataFrame:
    excl = tuple(EXCLUDED_CVR)
    con = duckdb.connect(str(DB_PATH), read_only=True)
    try:
        df = con.execute(f"""
            SELECT
                c.cvr, c.navn, c.is_intraday, c.is_multidesk, c.is_us_trading, c.is_hedgefund,
                l.adresse, l.postby, l.postnr, l.lat, l.lon
            FROM companies c
            JOIN company_locations l USING (cvr)
            WHERE l.lat IS NOT NULL
              AND c.cvr NOT IN {excl}
        """).df()
    except Exception:
        df = pd.DataFrame()
    con.close()
    return df


@st.cache_data
def load_data() -> pd.DataFrame:
    con = duckdb.connect(str(DB_PATH), read_only=True)
    df = con.execute("SELECT * FROM kpis ORDER BY navn, regnskab_slut").df()
    con.close()

    df = df[~df["cvr"].isin(EXCLUDED_CVR)].copy()
    df["year"] = pd.to_datetime(df["regnskab_slut"]).dt.year

    # Derived metrics not in the SQL view
    # Use float NaN (not pd.NA) so .round() works on all pandas versions
    _nan = float("nan")
    assets = df["aktiver"].where(df["aktiver"] != 0, _nan)
    revenue = df["omsaetning"].where(df["omsaetning"] != 0, _nan)
    equity = df["egenkapital"].where(df["egenkapital"] != 0, _nan)

    df["roa_pct"] = (df["aarsresultat"] / assets * 100).round(2)
    df["ebit_margin_pct"] = (df["ebit"] / revenue * 100).round(2)
    df["equity_ratio_pct"] = (equity / assets * 100).round(1)

    # YoY revenue growth per company
    df = df.sort_values(["cvr", "year"])
    df["rev_growth_pct"] = (
        df.groupby("cvr")["omsaetning"].pct_change().mul(100).round(1)
    )

    # Opening equity: prefer egenkapital_primo parsed from XBRL comparative figures,
    # fall back to prior-year closing equity (row-shift).
    # For genuine first-year companies (no prior row at all), use closing equity minus net
    # profit as a proxy for total equity deployed (founding capital + any mid-year injections).
    # Guard: only apply when there is no prior-year row in the DB (shift is NaN), so we
    # don't silently patch older companies that merely have missing XBRL comparative data.
    prior_equity_shift = df.groupby("cvr")["egenkapital"].shift(1)
    first_year_deployed = (df["egenkapital"] - df["aarsresultat"]).where(
        prior_equity_shift.isna()  # no prior row → genuine first year
        & df["egenkapital_primo"].isna()  # no XBRL comparative either
        & ((df["egenkapital"] - df["aarsresultat"]) >= 1e6)  # suppress minimum-capital shells
    )
    opening_equity = (
        df["egenkapital_primo"].where(df["egenkapital_primo"] >= 1_000_000)
        .fillna(prior_equity_shift.where(prior_equity_shift >= 1_000_000))
        .fillna(first_year_deployed)  # already guarded at >= 1M
    )
    df["rooe_pct"] = (df["aarsresultat"] / opening_equity * 100).round(2)

    # Opening related-party loans — used by both ROTCD and TRADE below.
    opening_related = df.groupby("cvr")["gaeld_tilknyttede"].shift(1).fillna(0)

    # ROTCD: same as ROOE but denominator includes opening related-party loans.
    # For first-year companies where opening_equity is NaN due to thin equity capitalisation
    # (e.g. ApS funded via shareholder loans), falls back to closing (equity - profit + loans)
    # as the opening capital proxy, bypassing the 1M equity-only guard.
    opening_total_normal = opening_equity + opening_related
    first_year_total = (df["egenkapital"] - df["aarsresultat"] + df["gaeld_tilknyttede"]).where(
        opening_total_normal.isna()
        & ((df["egenkapital"] - df["aarsresultat"] + df["gaeld_tilknyttede"]) >= 1e6)
    )
    opening_total_capital = opening_total_normal.fillna(first_year_total)
    df["rotcd_pct"] = (df["aarsresultat"] / opening_total_capital.where(opening_total_capital >= 1e6) * 100).round(2)
    df["total_capital_deployed"] = df["egenkapital"] + df["gaeld_tilknyttede"]

    # TRADE: Gross Trading Profit / (Headcount × Opening Equity) × 100
    # Use bruttoresultat (gross profit line) when available — fixes gross-presenters like
    # Mind Energy whose omsaetning is full retail revenue, not the net trading margin.
    # Net-margin presenters (most intraday traders) don't report bruttoresultat separately,
    # so omsaetning is already their gross trading profit.
    headcount = df["ansatte"].where(df["ansatte"] > 0)
    gross_trading_profit = df["bruttoresultat"].where(df["bruttoresultat"].notna(), df["omsaetning"])
    # Deployed capital = opening equity + opening related-party loans.
    # Related-party loans (gæld til tilknyttede virksomheder) are often how owners fund
    # trading operations instead of formal equity — economically equivalent, so both count.
    deployed_capital = opening_equity + opening_related
    # Suppress TRADE when deployed capital is near-zero — ratio is undefined for dormant firms.
    valid_deployed = deployed_capital.where(deployed_capital >= 1e6)
    df["trade_pct"] = (gross_trading_profit / (headcount * valid_deployed) * 100).round(2)

    # Restructuring flag: years where no revenue is filed but a large profit/EBIT exists.
    # These are one-time disposal gains from mergers/wind-downs, not trading performance.
    no_rev = df["omsaetning"].isna() | (df["omsaetning"] == 0)
    big_item = (df["aarsresultat"].abs().fillna(0) > 50e6) | (df["ebit"].abs().fillna(0) > 50e6)
    df["is_restructuring"] = no_rev & big_item

    return df


# ── Guard ──────────────────────────────────────────────────────────────────────

if not DB_PATH.exists():
    st.error(
        f"Database not found at **{DB_PATH}**. "
        "Run `uv run tradertracker --fetch` first, then reload this page."
    )
    st.stop()

df_all = load_data()
df_emp_monthly = load_employee_monthly()
df_locations = load_company_locations()
all_years = sorted(df_all["year"].dropna().unique().astype(int), reverse=True)

# ── Chart helpers ─────────────────────────────────────────────────────────────

def _bar_with_negatives(
    df: pd.DataFrame,
    col: str,
    label: str,
    title: str,
    height: int = 420,
    use_intraday_color: bool = True,
    color_map: "dict | None" = None,
) -> go.Figure:
    """Horizontal bar chart where negative values are always shown in red."""
    df = df.copy()
    if color_map is not None:
        df["_bar_color"] = df.apply(
            lambda r: "#ef4444" if r[col] < 0 else color_map.get(r["navn"], "#3b82f6"),
            axis=1,
        )
    elif use_intraday_color:
        df["_bar_color"] = df.apply(
            lambda r: "#ef4444" if r[col] < 0
            else ("#f97316" if r.get("is_intraday")
                  else ("#a855f7" if r.get("is_us_trading") else "#3b82f6")),
            axis=1,
        )
    else:
        df["_bar_color"] = df[col].apply(
            lambda v: "#ef4444" if v < 0 else "#3b82f6"
        )

    # Pad xaxis range so outside labels on the longest bar are never clipped
    vals = df[col].dropna()
    v_max = vals.max() if not vals.empty else 1
    v_min = min(vals.min(), 0) if not vals.empty else 0
    span = max(v_max - v_min, abs(v_max) * 0.01)
    x_range = [v_min - span * 0.02, v_max + span * 0.22]

    fig = go.Figure(go.Bar(
        x=df[col],
        y=df["navn"],
        orientation="h",
        marker_color=df["_bar_color"],
        text=df[col].apply(lambda v: f"{v:,.1f}"),
        textposition="outside",
        hovertemplate="%{y}: %{x:,.1f}<extra></extra>",
    ))
    fig.add_vline(x=0, line_color="white", line_width=1.5, opacity=0.6)
    fig.update_layout(
        title=title,
        height=height,
        yaxis=dict(autorange="reversed"),
        xaxis=dict(title=label, range=x_range),
        margin=dict(l=10, r=10, t=40, b=10),
    )
    return fig


_QUAL_PALETTE = [
    "#06b6d4", "#8b5cf6", "#f59e0b", "#10b981", "#f43f5e",
    "#3b82f6", "#f97316", "#84cc16", "#ec4899", "#14b8a6",
    "#a855f7", "#22d3ee", "#fb923c", "#a3e635", "#818cf8",
]


def _category_color_map(df: pd.DataFrame) -> "dict | None":
    """Per-company color map when a filter is active; None for 'All companies'."""
    if intraday_filter == "All companies":
        return None
    names = sorted(df["navn"].unique())
    return {name: _QUAL_PALETTE[i % len(_QUAL_PALETTE)] for i, name in enumerate(names)}


# ── Sidebar ────────────────────────────────────────────────────────────────────

with st.sidebar:
    st.title("⚡ TraderTracker")
    st.divider()

    selected_year = st.selectbox(
        "Financial year",
        options=all_years,
        index=0,
    )

    intraday_filter = st.radio(
        "Company category",
        options=["All companies", "Pure intraday firms", "Multi-desk traders", "US trading", "Hedge funds & intl. trading"],
        index=0,
        help="Pure intraday = trade only in the short-term electricity spot market. Multi-desk = broader firms with power, gas, long-term desks. US trading = firms focused on US power markets. Hedge funds & intl. trading = international quant/commodity firms (STG/Squarepoint, Qube, Balyasny, Trafigura).",
    )

    st.divider()

    currency = st.radio(
        "Currency",
        options=["DKK", "EUR"],
        index=0,
        horizontal=True,
        help="All values in DB are stored as DKK. EUR divides by 7.46.",
    )
    EUR_DKK_DISPLAY = 7.46

    st.divider()

    _FETCH_COOLDOWN = timedelta(hours=1)
    _last_fetch = st.session_state.get("last_fetch_time")
    _cooldown_active = _last_fetch is not None and (datetime.now() - _last_fetch) < _FETCH_COOLDOWN
    if _cooldown_active:
        _remaining = _FETCH_COOLDOWN - (datetime.now() - _last_fetch)
        _mins, _secs = divmod(int(_remaining.total_seconds()), 60)
        st.caption(f"Next refresh available in **{_mins}m {_secs}s**")

    if st.button(
        "🔄 Refresh Data",
        use_container_width=True,
        type="primary",
        disabled=_cooldown_active,
        help="Fetch latest company data and annual reports from cvr.dev + Virk.dk",
    ):
        import re
        import subprocess

        _status = st.empty()
        _bar = st.progress(0.0, text="Starting…")
        _log = st.empty()

        log_lines: list[str] = []
        _status.info("Launching pipeline…")

        try:
            proc = subprocess.Popen(
                ["uv", "run", "tradertracker", "--fetch", "--export"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=str(Path(__file__).parent.parent),
            )

            for raw in iter(proc.stdout.readline, ""):
                line = raw.rstrip()
                if not line:
                    continue
                log_lines.append(line)
                _log.code("\n".join(log_lines[-12:]), language="shell")

                m = re.search(r"\[(\d+)/(\d+)\]", line)
                if m:
                    i, n = int(m.group(1)), int(m.group(2))
                    _bar.progress(0.05 + (i / n) * 0.85, text=f"Company {i} / {n}…")
                    _status.info(f"Fetching financials — **{i} / {n}** companies done")
                elif "companies found" in line.lower():
                    _bar.progress(0.04, text="Company list built…")
                    _status.info(line.strip())
                elif "Computing KPIs" in line:
                    _bar.progress(0.93, text="Computing KPIs…")
                    _status.info("Computing KPIs and exporting to Excel…")

            proc.wait()

            if proc.returncode == 0:
                _bar.progress(1.0, text="Complete!")
                _status.success("Data refresh complete! Click below to reload.")
                st.session_state["last_fetch_time"] = datetime.now()
                st.cache_data.clear()
                if st.button("↺ Reload dashboard", key="reload_after_fetch"):
                    st.rerun()
            else:
                _status.error("Pipeline failed — see log above for details.")

        except FileNotFoundError:
            _status.error("`uv` not found in PATH. Is the environment active?")
        except Exception as exc:
            _status.error(f"Unexpected error: {exc}")


# ── Apply filters + currency ──────────────────────────────────────────────────

# Columns stored in DKK that should be converted when displaying in EUR
_DKK_ABS_COLS = [
    "omsaetning", "bruttoresultat", "ebit", "aarsresultat",
    "egenkapital", "egenkapital_primo", "aktiver",
    "kortfristet_gaeld", "langfristet_gaeld", "personaleomkostninger",
]
# Per-employee columns are already in tDKK — convert label only
_DKK_PER_EMP_COLS = [
    "omsaetning_per_ansatte_tdkk",
    "resultat_per_ansatte_tdkk",
    "egenkapital_per_ansatte_tdkk",
    "personaleomkostninger_per_ansatte_tdkk",
]

def _apply_currency(df: pd.DataFrame) -> pd.DataFrame:
    if currency == "DKK":
        return df
    df = df.copy()
    for col in _DKK_ABS_COLS:
        if col in df.columns:
            df[col] = df[col] / EUR_DKK_DISPLAY
    for col in _DKK_PER_EMP_COLS:
        if col in df.columns:
            df[col] = df[col] / EUR_DKK_DISPLAY
    return df

def _ccy(label: str) -> str:
    """Append currency unit to a label."""
    return label.replace("DKK", currency).replace("tDKK", f"t{currency}")

def _apply_category(df: pd.DataFrame) -> pd.DataFrame:
    if intraday_filter == "Pure intraday firms":
        return df[df["is_intraday"] == True]
    if intraday_filter == "Multi-desk traders":
        return df[df["is_multidesk"] == True]
    if intraday_filter == "US trading":
        return df[df["is_us_trading"] == True]
    if intraday_filter == "Hedge funds & intl. trading":
        return df[df["is_hedgefund"] == True]
    return df


df_snap = _apply_currency(_apply_category(df_all[df_all["year"] == selected_year].copy()))
df_ts = _apply_currency(_apply_category(df_all.copy()))

# Operational snapshot: excludes restructuring years (disposal gains, wind-downs) from charts
df_snap_ops = df_snap[~df_snap["is_restructuring"].fillna(False)]

# ── Page header + compact KPI strip ───────────────────────────────────────────

_rev      = df_snap["omsaetning"].sum()
_profit   = df_snap["aarsresultat"].sum()
_roe      = df_snap["roe_pct"].median()
_rotcd    = df_snap["rotcd_pct"].median()
_nm       = df_snap["net_margin_pct"].median()
_emp_tot  = df_snap["ansatte"].sum()
_emp_med  = df_snap["ansatte"].median()
_sal_med  = df_snap["personaleomkostninger_per_ansatte_tdkk"].median()
_cat = (" · pure intraday" if intraday_filter == "Pure intraday firms"
        else " · multi-desk" if intraday_filter == "Multi-desk traders"
        else " · US trading" if intraday_filter == "US trading" else "")

def _bn(v):
    if pd.notna(v) and v != 0:
        return f"{v/1e9:,.2f} bn {currency}"
    return "N/A"

def _pct(v):
    return f"{v:.1f} %" if pd.notna(v) else "N/A"

def _num(v, decimals=0):
    return f"{v:,.{decimals}f}" if pd.notna(v) and v > 0 else "N/A"

_kpis = [
    ("Companies",            f"{len(df_snap)}"),
    ("Total Revenue",        _bn(_rev)),
    ("Total Net Profit",     _bn(_profit)),
    ("Median ROE",           _pct(_roe)),
    ("Median ROTCD",          _pct(_rotcd)),
    ("Median Net Margin",    _pct(_nm)),
    ("Total Employees",      _num(_emp_tot)),
    ("Median Headcount",     _num(_emp_med, 0)),
    (f"Median Salary/FTE (t{currency})", _num(_sal_med, 0)),
]

_kpi_html = "".join(
    f'<div><span style="font-size:0.7rem;color:#888;text-transform:uppercase;'
    f'letter-spacing:.05em;">{label}</span><br>'
    f'<strong style="font-size:1rem;">{val}</strong></div>'
    for label, val in _kpis
)

st.markdown(
    f"""
    <div style="display:flex; align-items:baseline; gap:0.6rem; margin-bottom:0.1rem;">
      <span style="font-size:1.9rem; font-weight:700;">⚡ Danish Energy Trading</span>
      <span style="font-size:0.85rem; color:#888;">{selected_year}{_cat}</span>
    </div>
    <div style="display:flex; flex-wrap:wrap; gap:1.6rem 2.4rem;
                padding:0.45rem 0 0.6rem 0; border-bottom:1px solid #333; margin-bottom:0.6rem;">
      {_kpi_html}
    </div>
    """,
    unsafe_allow_html=True,
)

# ── Tabs ───────────────────────────────────────────────────────────────────────

tab_overview, tab_rankings, tab_timeseries, tab_headcount, tab_intraday, tab_sankey, tab_map, tab_table = st.tabs([
    "Overview", "Rankings", "Time Series", "Headcount", "Pure Intraday", "Money Flow", "Map", "Data Table"
])

_COLOR_MAP = {True: "#f97316", False: "#3b82f6"}
_COLOR_LABEL = "is_intraday"


# ─── Tab: Overview ────────────────────────────────────────────────────────────

with tab_overview:
    _overview_cmap = _category_color_map(df_snap)

    col_l, col_r = st.columns(2)

    with col_l:
        df_profit = df_snap_ops.dropna(subset=["aarsresultat"]).sort_values("aarsresultat", ascending=False).head(15)
        st.plotly_chart(
            _bar_with_negatives(df_profit, "aarsresultat", _ccy("Net Profit (DKK)"),
                                f"Net Profit ({currency}) — top 15 ({selected_year})", height=460,
                                color_map=_overview_cmap),
            width="stretch",
        )

    with col_r:
        df_roe = df_snap_ops.dropna(subset=["roe_pct"]).sort_values("roe_pct", ascending=False).head(15)
        st.plotly_chart(
            _bar_with_negatives(df_roe, "roe_pct", "ROE (%)",
                                f"ROE (%) — top 15 ({selected_year})", height=460,
                                color_map=_overview_cmap),
            width="stretch",
        )

    col_l2, col_r2 = st.columns(2)

    with col_l2:
        df_ppe = (
            df_snap_ops.dropna(subset=["resultat_per_ansatte_tdkk"])
            .sort_values("resultat_per_ansatte_tdkk", ascending=False)
            .head(15)
        )
        st.plotly_chart(
            _bar_with_negatives(df_ppe, "resultat_per_ansatte_tdkk",
                                _ccy("Profit / Employee (tDKK)"),
                                f"Profit per Employee (t{currency}) — top 15 ({selected_year})",
                                color_map=_overview_cmap),
            width="stretch",
        )

    with col_r2:
        df_rotcd = df_snap_ops.dropna(subset=["rotcd_pct"]).sort_values("rotcd_pct", ascending=False).head(15)
        st.plotly_chart(
            _bar_with_negatives(df_rotcd, "rotcd_pct", "ROTCD (%)",
                                f"ROTCD (%) — top 15 ({selected_year})", height=420,
                                color_map=_overview_cmap),
            width="stretch",
        )


# ─── Tab: Rankings ────────────────────────────────────────────────────────────

with tab_rankings:
    metric_options = _build_metric_options()
    metric_label_display = st.selectbox(
        "Metric to rank by",
        options=metric_options,
        index=metric_options.index(_ccy("Net Profit (DKK)")),
        key="rank_metric",
    )
    metric_label_display = _resolve_metric(metric_label_display, metric_options)
    metric_label = _metric_key(metric_label_display)
    meta = METRICS[metric_label]
    col = meta["col"]

    st.info(f"**{metric_label_display}** — {meta['help']}")

    df_rank = df_snap_ops.dropna(subset=[col]).sort_values(col, ascending=False)

    if df_rank.empty:
        st.warning("No data available for this metric in the selected year.")
    else:
        st.plotly_chart(
            _bar_with_negatives(
                df_rank, col, metric_label,
                f"{metric_label} — all companies, {selected_year}",
                height=max(420, len(df_rank) * 24),
            ),
            width="stretch",
        )


# ─── Tab: Time Series ─────────────────────────────────────────────────────────

with tab_timeseries:
    intraday_names = sorted(df_all[df_all["is_intraday"] == True]["navn"].unique())
    all_names = sorted(df_ts["navn"].unique())

    if intraday_filter == "Pure intraday firms":
        default_companies = intraday_names
    elif intraday_filter == "Hedge funds & intl. trading":
        default_companies = sorted(df_all[df_all["is_hedgefund"] == True]["navn"].unique())
    elif intraday_filter in ("Multi-desk traders", "US trading"):
        default_companies = all_names
    else:
        default_companies = intraday_names[:6] if intraday_names else all_names[:6]

    selected_companies = st.multiselect(
        "Companies",
        options=all_names,
        default=[c for c in default_companies if c in all_names],
        help="Select one or more companies to compare over time.",
        key=f"ts_companies_{intraday_filter}",
    )

    ts_options = _build_metric_options()
    ts_metric_display = st.selectbox(
        "Metric",
        options=ts_options,
        index=ts_options.index(_ccy("Net Profit (DKK)")),
        key="ts_metric",
    )
    ts_metric_display = _resolve_metric(ts_metric_display, ts_options)
    ts_metric_label = _metric_key(ts_metric_display)
    ts_meta = METRICS[ts_metric_label]
    ts_col = ts_meta["col"]

    st.info(f"**{ts_metric_display}** — {ts_meta['help']}")

    _ts_min_year = int(df_ts["year"].min()) if not df_ts.empty else 2017
    _ts_max_year = int(df_ts["year"].max()) if not df_ts.empty else 2025
    ts_year_range = st.slider("Year range", _ts_min_year, _ts_max_year,
                              (_ts_min_year, _ts_max_year), key="ts_years")

    if not selected_companies:
        st.info("Select at least one company above.")
    else:
        df_plot = (
            df_ts[
                df_ts["navn"].isin(selected_companies) &
                df_ts["year"].between(ts_year_range[0], ts_year_range[1]) &
                ~df_ts["is_restructuring"].fillna(False)
            ]
            .dropna(subset=[ts_col, "year"])
        )
        if df_plot.empty:
            st.warning("No data for the selected companies and metric.")
        else:
            fig = px.line(
                df_plot,
                x="year",
                y=ts_col,
                color="navn",
                markers=True,
                labels={"year": "Year", ts_col: ts_metric_display, "navn": "Company"},
                title=f"{ts_metric_display} over time",
            )
            if df_plot[ts_col].min() < 0 or ts_meta.get("can_be_negative", True):
                fig.add_hline(
                    y=0,
                    line_color="white",
                    line_width=2,
                    line_dash="dot",
                    opacity=0.7,
                    annotation_text="0",
                    annotation_font_color="white",
                    annotation_position="right",
                )
            fig.update_layout(
                height=520,
                legend=dict(orientation="v", yanchor="middle", y=0.5, xanchor="left", x=1.02, title_text=""),
            )
            st.plotly_chart(fig, width="stretch")


# ─── Tab: Headcount ───────────────────────────────────────────────────────────

with tab_headcount:
    hc_all_names = sorted(df_emp_monthly["navn"].unique()) if not df_emp_monthly.empty else []

    if intraday_filter == "Pure intraday firms":
        hc_default = sorted(df_emp_monthly[df_emp_monthly["is_intraday"] == True]["navn"].unique()) if not df_emp_monthly.empty else []
    elif intraday_filter == "Multi-desk traders":
        hc_default = sorted(df_emp_monthly[df_emp_monthly["is_multidesk"] == True]["navn"].unique()) if not df_emp_monthly.empty else []
    elif intraday_filter == "US trading":
        hc_default = sorted(df_emp_monthly[df_emp_monthly["is_us_trading"] == True]["navn"].unique()) if not df_emp_monthly.empty else []
    elif intraday_filter == "Hedge funds & intl. trading":
        hc_default = sorted(df_emp_monthly[df_emp_monthly["is_hedgefund"] == True]["navn"].unique()) if not df_emp_monthly.empty else []
    else:
        hc_default = sorted(df_emp_monthly[df_emp_monthly["is_intraday"] == True]["navn"].unique())[:6] if not df_emp_monthly.empty else []

    hc_selected = st.multiselect(
        "Companies",
        options=hc_all_names,
        default=[c for c in hc_default if c in hc_all_names],
        key=f"hc_companies_{intraday_filter}",
    )

    if not df_emp_monthly.empty:
        _hc_min_year = int(df_emp_monthly["aar"].min())
        _hc_max_year = int(df_emp_monthly["aar"].max())
        hc_year_range = st.slider("Year range", _hc_min_year, _hc_max_year,
                                  (_hc_min_year, _hc_max_year), key="hc_years")
    else:
        hc_year_range = (2019, 2026)

    if not hc_selected:
        st.info("Select at least one company above.")
    else:
        df_hc = df_emp_monthly[
            df_emp_monthly["navn"].isin(hc_selected) &
            df_emp_monthly["aar"].between(hc_year_range[0], hc_year_range[1])
        ]
        if df_hc.empty:
            st.warning("No monthly headcount data for the selected companies.")
        else:
            fig_hc = px.line(
                df_hc,
                x="dato",
                y="antal_ansatte",
                color="navn",
                markers=False,
                labels={"dato": "Month", "antal_ansatte": "Employees", "navn": "Company"},
                title="Monthly headcount (CVR register)",
            )
            fig_hc.update_layout(
                height=540,
                legend=dict(orientation="v", yanchor="middle", y=0.5, xanchor="left", x=1.02, title_text=""),
            )
            st.plotly_chart(fig_hc, width="stretch")


# ─── Tab: Intraday Focus ──────────────────────────────────────────────────────

with tab_intraday:
    df_intra = _apply_currency(df_all[
        (df_all["is_intraday"] == True) & (df_all["year"] == selected_year)
    ].copy())

    st.subheader(f"Pure intraday firms — {selected_year}")

    if df_intra.empty:
        st.warning(
            f"No intraday firm data for {selected_year}. "
            "Many intraday firms are young — try a recent year or check if they have filed yet."
        )
    else:
        i1, i2, i3, i4, i5 = st.columns(5)
        i1.metric("Firms with data", len(df_intra))
        roe_med = df_intra["roe_pct"].median()
        i2.metric("Median ROE", f"{roe_med:.1f} %" if pd.notna(roe_med) else "N/A",
                  help="Return on Equity = net profit / closing equity.")
        rotcd_med = df_intra["rotcd_pct"].median()
        i3.metric("Median ROTCD", f"{rotcd_med:.1f} %" if pd.notna(rotcd_med) else "N/A",
                  help="Return on Total Capital Deployed = net profit / opening (equity + related-party loans). "
                       "Identical to ROOE when there are no intercompany loans; more realistic for thinly-capitalised "
                       "first-year firms funded via shareholder debt.")
        profit_med = df_intra["aarsresultat"].median()
        i4.metric(
            "Median Net Profit",
            _bn(profit_med) if pd.notna(profit_med) else "N/A",
            help="Median net profit across intraday firms for the selected year.",
        )
        ppe_med = df_intra["resultat_per_ansatte_tdkk"].median()
        i5.metric(
            "Median Profit / Employee",
            f"{ppe_med:,.0f} t{currency}" if pd.notna(ppe_med) else "N/A",
            help="Net profit per employee — proxy for team quality and capital efficiency.",
        )

        st.divider()

        col1, col2 = st.columns(2)

        with col1:
            df_plot = df_intra.dropna(subset=["aarsresultat"]).sort_values("aarsresultat", ascending=False)
            st.plotly_chart(
                _bar_with_negatives(df_plot, "aarsresultat", _ccy("Net Profit (DKK)"),
                                    f"Net Profit ({selected_year})",
                                    height=max(360, len(df_plot) * 26),
                                    use_intraday_color=False),
                width="stretch",
            )

        with col2:
            df_plot = df_intra.dropna(subset=["roe_pct"]).sort_values("roe_pct", ascending=False)
            st.plotly_chart(
                _bar_with_negatives(df_plot, "roe_pct", "ROE (%)",
                                    f"Return on Equity ({selected_year})",
                                    height=max(360, len(df_plot) * 26),
                                    use_intraday_color=False),
                width="stretch",
            )

        col3, col4 = st.columns(2)

        with col3:
            df_plot = df_intra.dropna(subset=["resultat_per_ansatte_tdkk"]).sort_values(
                "resultat_per_ansatte_tdkk", ascending=False
            )
            st.plotly_chart(
                _bar_with_negatives(df_plot, "resultat_per_ansatte_tdkk", f"t{currency}",
                                    f"Profit per Employee (t{currency}, {selected_year})",
                                    height=max(360, len(df_plot) * 26),
                                    use_intraday_color=False),
                width="stretch",
            )

        with col4:
            df_plot = df_intra.dropna(subset=["rotcd_pct"]).sort_values("rotcd_pct", ascending=False)
            st.plotly_chart(
                _bar_with_negatives(df_plot, "rotcd_pct", "ROTCD (%)",
                                    f"Return on Total Capital Deployed ({selected_year})",
                                    height=max(360, len(df_plot) * 26),
                                    use_intraday_color=False),
                width="stretch",
            )

        # Bubble chart + profit pie side by side
        df_sc = df_intra.dropna(subset=["equity_ratio_pct", "roe_pct", "aarsresultat"]).copy()
        df_pie = df_intra.dropna(subset=["aarsresultat"])
        df_pie = df_pie[df_pie["aarsresultat"] > 0].sort_values("aarsresultat", ascending=False)

        if len(df_sc) >= 3 or not df_pie.empty:
            bc_col, pie_col = st.columns(2)

            if len(df_sc) >= 3:
                with bc_col:
                    st.subheader("Risk vs Return")
                    st.caption(
                        "Companies top-right are both financially safe (high equity) and highly profitable. "
                        "Bottom-left = low equity AND low return, which is a warning sign."
                    )
                    df_sc["_bubble_size"] = df_sc["aarsresultat"].clip(lower=1)
                    fig = px.scatter(
                        df_sc,
                        x="equity_ratio_pct",
                        y="roe_pct",
                        size="_bubble_size",
                        text="navn",
                        color="rotcd_pct",
                        color_continuous_scale="RdYlGn",
                        hover_name="navn",
                        hover_data={
                            "aarsresultat": ":,.0f",
                            "rotcd_pct": ":.1f",
                            "equity_ratio_pct": ":.1f",
                            "roe_pct": ":.1f",
                        },
                        labels={
                            "equity_ratio_pct": "Equity Ratio (%)",
                            "roe_pct": "ROE (%)",
                            "rotcd_pct": "ROTCD (%)",
                            "aarsresultat": _ccy("Net Profit (DKK)"),
                        },
                    )
                    fig.add_hline(y=0, line_dash="dot", line_color="gray", opacity=0.5)
                    fig.add_vline(x=0, line_dash="dot", line_color="gray", opacity=0.5)
                    fig.update_traces(textposition="top center")
                    fig.update_layout(height=480)
                    st.plotly_chart(fig, use_container_width=True)

            if not df_pie.empty:
                with pie_col:
                    st.subheader("Profit share")
                    fig_pie = px.pie(
                        df_pie,
                        names="navn",
                        values="aarsresultat",
                        title=f"Share of total profit — {selected_year}",
                        hole=0.35,
                    )
                    fig_pie.update_traces(
                        textposition="inside",
                        textinfo="percent+label",
                        hovertemplate="<b>%{label}</b><br>Profit: %{value:,.0f}<br>Share: %{percent}<extra></extra>",
                    )
                    fig_pie.update_layout(
                        height=480,
                        showlegend=False,
                        margin=dict(t=40, b=10, l=10, r=10),
                    )
                    st.plotly_chart(fig_pie, use_container_width=True)
                    n_neg = len(df_intra[df_intra["aarsresultat"] <= 0].dropna(subset=["aarsresultat"]))
                    if n_neg:
                        st.caption(f"{n_neg} firm(s) with zero or negative profit excluded.")


# ─── Tab: Money Flow (Sankey) ─────────────────────────────────────────────────

def _build_sankey(row: pd.Series, company: str, year: int) -> "go.Figure | None":
    def _v(col):
        v = row.get(col)
        return float(v) if pd.notna(v) and v is not None else 0.0

    revenue      = _v("omsaetning")
    brutto       = _v("bruttoresultat")
    cogs_db      = max(0.0, _v("vareforbrug"))   # direct cost-of-goods from XBRL
    personnel    = max(0.0, _v("personaleomkostninger"))
    depreciation = max(0.0, _v("afskrivninger"))
    ebit         = _v("ebit")
    fin_indt     = max(0.0, _v("fin_indt"))
    fin_udg      = max(0.0, _v("fin_udg"))
    skat         = max(0.0, _v("skat"))
    net_profit   = _v("aarsresultat")
    udbytte      = max(0.0, _v("udbytte"))

    if revenue <= 0:
        return None

    # Gross presentation: bruttoresultat explicitly reported, OR vareforbrug > 50% of revenue
    # (catches gross-volume energy traders where bruttoresultat is missing but COGS is tagged)
    if brutto > 0 and brutto < revenue * 0.98:
        is_gross    = True
        gross_profit = brutto
        vareforbrug  = max(0.0, revenue - brutto)
    elif cogs_db > revenue * 0.5:
        is_gross    = True
        gross_profit = revenue - cogs_db
        vareforbrug  = cogs_db
    else:
        is_gross    = False
        gross_profit = revenue
        vareforbrug  = 0.0

    # Detect genuinely missing EBIT (None in DB) vs zero EBIT
    ebit_raw = row.get("ebit")
    has_ebit = pd.notna(ebit_raw) and ebit_raw is not None
    ebit_val = float(ebit_raw) if has_ebit else 0.0

    # Other opex only meaningful when EBIT is known — otherwise it absorbs all revenue
    if has_ebit:
        other_opex = max(0.0, gross_profit - personnel - depreciation - max(0.0, ebit_val))
    else:
        other_opex = 0.0

    ebt      = net_profit + skat if skat > 0 else (max(0.0, ebit_val + fin_indt - fin_udg) if has_ebit else net_profit)
    retained = net_profit - udbytte

    M    = 1_000_000
    base = revenue

    def _lbl(name: str, val: float) -> str:
        pct = val / base * 100 if base > 0 else 0
        pct_str = f"{pct:.0f}%" if pct >= 5 else f"{pct:.1f}%"
        return f"{name}<br>{val/M:,.1f}M  ·  {pct_str}"

    _FLOW = "rgba(148, 196, 243, 0.45)"
    _C_TOP    = "#1e40af"
    _C_GROSS  = "#1d4ed8"
    _C_EBIT   = "#2563eb"
    _C_EBT    = "#4338ca"
    _C_COST   = "#475569"
    _C_DEPR   = "#64748b"
    _C_FIN_IN = "#047857"
    _C_FIN_OU = "#b91c1c"
    _C_TAX    = "#6d28d9"
    _C_NET    = "#065f46"
    _C_DIV    = "#92400e"
    _C_RET    = "#0f766e"

    labels, node_colors, node_xs, node_ys = [], [], [], []
    sources, targets, values = [], [], []

    def node(label, color, x, y):
        idx = len(labels)
        labels.append(label); node_colors.append(color)
        node_xs.append(max(0.001, min(0.999, x)))
        node_ys.append(max(0.001, min(0.999, y)))
        return idx

    def link(src, tgt, val):
        if val > 0.001:
            sources.append(src); targets.append(tgt)
            values.append(round(val / M, 4))

    # ── Layout strategy ────────────────────────────────────────────────────
    # Cost/exit nodes are placed in the TOP portion of the chart (low y).
    # Profit/pass-through nodes are in the BOTTOM portion (high y).
    # Links from each source are listed TOP-TARGET first → BOTTOM-TARGET last,
    # so Plotly stacks them in the same order at each node and flows never cross.

    # When COGS is so large it can't be shown without overflow (>88% of revenue),
    # suppress the gross split and use the net trading margin as the Sankey base.
    # This makes percentages meaningful (relative to net margin, not gross volume).
    _was_cogs_suppressed = False
    _gross_vol = 0.0
    if is_gross:
        cogs_ratio = vareforbrug / revenue if revenue > 0 else 0
        if cogs_ratio > 0.88:
            _was_cogs_suppressed = True
            _gross_vol = revenue
            is_gross = False
            revenue = gross_profit   # net margin becomes the Sankey base
            base = revenue           # _lbl closure picks this up immediately

    has_pers = personnel > 0.005 * revenue
    has_depr = depreciation > 0.005 * revenue
    has_oth  = has_ebit and other_opex > 0.005 * revenue   # only show when EBIT is known
    has_fin_in  = fin_indt > 0
    has_fin_out = fin_udg > 0
    has_tax     = skat > 0
    has_div     = udbytte > 0
    has_ret     = retained > 0

    # Opex y positions: pack into top 30% of chart (keep away from edge)
    opex_slots = [v for v, ok in [(personnel, has_pers), (depreciation, has_depr),
                                   (other_opex, has_oth)] if ok]
    n_opex = len(opex_slots)
    _oy = {1: [0.15], 2: [0.10, 0.24], 3: [0.08, 0.18, 0.28]}.get(n_opex, [])

    if is_gross:
        x0, x1, x2, x3, x4, x5, x6 = 0.01, 0.20, 0.38, 0.56, 0.70, 0.84, 0.99
        n_rev   = node(_lbl("Trading revenue", revenue),      _C_TOP,   x0, 0.50)
        # Adapt COGS y to keep the band inside the chart area for mid-range margins
        cogs_ratio = vareforbrug / revenue if revenue > 0 else 0
        y_cogs  = 0.12 if cogs_ratio < 0.65 else (0.18 if cogs_ratio < 0.78 else 0.24)
        y_gross = 0.72 if cogs_ratio < 0.65 else (0.78 if cogs_ratio < 0.78 else 0.83)
        n_cogs  = node(_lbl("Direct costs",    vareforbrug),  _C_COST,  x1, y_cogs)
        n_gross = node(_lbl("Gross profit",    gross_profit), _C_GROSS, x1, y_gross)
        src = n_gross
    else:
        x0, x2, x3, x4, x5, x6 = 0.01, 0.32, 0.54, 0.68, 0.84, 0.99
        if _was_cogs_suppressed:
            # Show net margin as the chart base; gross volume in parentheses for context
            _gv_lbl = f"Net trading margin<br>{revenue/M:,.1f}M  ·  100%  (gross vol. {_gross_vol/M:,.0f}M)"
            n_rev = node(_gv_lbl, _C_GROSS, x0, 0.50)
        else:
            n_rev = node(_lbl("Trading income", revenue), _C_TOP, x0, 0.50)
        src = n_rev

    oi = 0
    if has_pers:
        n_pers = node(_lbl("Personnel costs", personnel),    _C_COST, x2, _oy[oi]); oi += 1
    if has_depr:
        n_depr = node(_lbl("Depreciation",    depreciation), _C_DEPR, x2, _oy[oi]); oi += 1
    if has_oth:
        n_oth  = node(_lbl("Other opex",      other_opex),   _C_DEPR, x2, _oy[oi])

    # ── Adaptive y-positions ────────────────────────────────────────────────────
    # Plotly Sankey bands occupy ≈ (flow/revenue) of the chart height centred on
    # the node y.  For high-margin companies the band clips at y > 1 (bottom).
    # Pull profit nodes upward proportionally when EBIT margin > 40 %.
    ebit_frac = max(0.0, ebit_val) / base if base > 0 else 0.0
    net_frac  = max(0.0, net_profit) / base if base > 0 else 0.0
    _pull     = max(0.0, ebit_frac - 0.40) * 0.52   # 0 below 40 %, grows above
    y_ebit    = round(0.78 - _pull, 3)               # default 0.78, rises for high margins
    y_tax     = round(max(0.22, y_ebit * 0.44), 3)
    y_fin_in  = round(max(0.18, y_tax - 0.06), 3)
    y_fin_out = round(max(0.35, (y_ebit + y_tax) / 2 + 0.04), 3)
    y_net     = round(min(0.88, y_ebit + max(0.06, net_frac * 0.08)), 3)
    y_ret     = round(min(0.93, y_net  + max(0.05, net_frac * 0.06)), 3)
    y_div     = round(min(0.91, (y_net + y_ret) / 2 + 0.01), 3)

    if has_ebit:
        # Full path: opex nodes → EBIT → (fin items) → EBT → tax/net
        has_fin_items = has_fin_in or has_fin_out
        n_ebit = node(_lbl("EBIT", max(0.0, ebit_val)), _C_EBIT, x3, y_ebit)

        if has_fin_in:
            n_fin_in  = node(_lbl("Financial income",   fin_indt), _C_FIN_IN, x4, y_fin_in)
        if has_fin_out:
            n_fin_out = node(_lbl("Financial expenses", fin_udg),  _C_FIN_OU, x4, y_fin_out)

        # Only create EBT node when financial items exist (EBIT = EBT otherwise,
        # so a separate node would sit on top of EBIT at the same position).
        if has_fin_items:
            n_ebt = node(_lbl("Pre-tax profit", ebt), _C_EBT, x4, y_ebit)
            pre_tax_src = n_ebt
        else:
            pre_tax_src = n_ebit

        if has_tax:
            n_tax = node(_lbl("Tax",       skat),               _C_TAX, x5, y_tax)
        n_net = node(_lbl("Net profit", max(0.0, net_profit)), _C_NET, x5, y_net)
    else:
        # Simplified layout when EBIT not reported — no opex nodes.
        xs_ebt = 0.38
        xs_tax = 0.65
        xs_net = 0.65
        xs_div = 0.99
        xs_ret = 0.99

        if skat > 0:
            n_ebt = node(_lbl("Pre-tax profit", ebt), _C_EBT, xs_ebt, 0.45)
            n_tax = node(_lbl("Tax",       skat),               _C_TAX, xs_tax, 0.20)
            n_net = node(_lbl("Net profit", max(0.0, net_profit)), _C_NET, xs_net, 0.72)
        else:
            n_net = node(_lbl("Net profit", max(0.0, net_profit)), _C_NET, xs_net, 0.72)

    if has_div:
        _xd = xs_div if not has_ebit else x6
        n_div = node(_lbl("Dividends", udbytte),  _C_DIV, _xd, y_div)
    if has_ret:
        _xr = xs_ret if not has_ebit else x6
        n_ret = node(_lbl("Retained",  retained), _C_RET, _xr, y_ret)

    # ── Links — top target first so stacking matches y order (no crossings) ─
    if is_gross:
        link(n_rev, n_cogs,  vareforbrug)   # top: cogs y=0.12
        link(n_rev, n_gross, gross_profit)  # bottom: gross y=0.72

    if has_ebit:
        if has_pers:  link(src, n_pers, personnel)
        if has_depr:  link(src, n_depr, depreciation)
        if has_oth:   link(src, n_oth,  other_opex)
        link(src, n_ebit, max(0.0, ebit_val))              # last = bottom (y=0.75)

        if has_fin_items:
            if has_fin_in:  link(n_fin_in, n_ebt, fin_indt)  # top input to ebt
            link(n_ebit, n_ebt, max(0.0, ebit_val))           # main input to ebt

        if has_fin_out:  link(pre_tax_src, n_fin_out, fin_udg)  # top exit
        if has_tax:      link(pre_tax_src, n_tax,     skat)     # next exit (y=0.40)
        link(pre_tax_src, n_net, max(0.0, net_profit))          # bottom (y=0.80)
    else:
        # Simplified — cap incoming at gross_profit to maintain flow conservation
        # (ebt may exceed gross_profit when there's financial income we don't track)
        if skat > 0:
            link(src, n_ebt, gross_profit)                 # revenue → pre-tax
            link(n_ebt, n_tax, skat)                       # pre-tax → tax (top, y=0.20)
            link(n_ebt, n_net, max(0.0, net_profit))       # pre-tax → net (bottom, y=0.75)
        else:
            link(src, n_net, max(0.0, net_profit))         # revenue → net directly

    if has_div:  link(n_net, n_div, udbytte)               # top
    if has_ret:  link(n_net, n_ret, retained)              # bottom

    if not values:
        return None

    unit = f"M {currency}"
    fig = go.Figure(go.Sankey(
        arrangement="snap",
        node=dict(
            label=labels,
            color=node_colors,
            x=node_xs,
            y=node_ys,
            pad=6,
            thickness=36,
            line=dict(color="rgba(255,255,255,0.08)", width=0.5),
        ),
        link=dict(
            source=sources,
            target=targets,
            value=values,
            color=[_FLOW] * len(values),
        ),
    ))
    fig.update_layout(
        paper_bgcolor="#0f172a",
        plot_bgcolor="#0f172a",
        font=dict(family="Inter, Arial, sans-serif", size=11, color="#e2e8f0"),
        title=dict(
            text=f"<b>{company}</b>  ·  {year}  <sup>values in {unit}</sup>",
            font=dict(size=15, color="#f8fafc"),
            x=0.0, xanchor="left",
        ),
        height=680,
        margin=dict(l=20, r=20, t=55, b=40),
    )
    return fig


with tab_sankey:
    st.caption("P&L money flow from trading income through to retained earnings. "
               "Values in millions. Widths proportional to share of trading income.")

    _intra_names = sorted(df_all[df_all["is_intraday"] == True]["navn"].unique())

    s_col1, s_col2 = st.columns([3, 1])
    with s_col1:
        sankey_company = st.selectbox(
            "Company", options=sorted(df_all["navn"].unique()),
            index=next((i for i, n in enumerate(sorted(df_all["navn"].unique()))
                        if n in _intra_names), 0),
            key="sankey_company",
        )
    with s_col2:
        _co_years = sorted(
            df_all[df_all["navn"] == sankey_company]["year"].dropna().unique().astype(int),
            reverse=True,
        )
        sankey_year = st.selectbox("Year", options=_co_years, index=0, key="sankey_year")

    _row_df = _apply_currency(
        df_all[(df_all["navn"] == sankey_company) & (df_all["year"] == sankey_year)]
    )
    if _row_df.empty:
        st.warning("No data for this company / year combination.")
    else:
        _row = _row_df.iloc[0]
        if _row.get("is_restructuring"):
            st.warning(
                f"**{sankey_company} {sankey_year}** filed no trading revenue. "
                "The profit figure likely reflects a one-time disposal/transfer gain from a merger or wind-down, "
                "not operating trading profit. No Sankey is shown for restructuring years."
            )
        _fig = _build_sankey(_row, sankey_company, sankey_year)
        if _fig is None and not _row.get("is_restructuring"):
            st.warning("Insufficient data to draw Sankey — trading income is zero or missing.")
        else:
            st.plotly_chart(_fig, use_container_width=True)

        # Quick data availability summary
        _fields = {
            "Trading income": "omsaetning", "Gross profit": "bruttoresultat",
            "Personnel costs": "personaleomkostninger", "Depreciation": "afskrivninger",
            "Fin. income": "fin_indt", "Fin. expenses": "fin_udg",
            "Tax": "skat", "Dividends": "udbytte",
        }
        _avail = {k: ("✓" if pd.notna(_row.get(v)) and (_row.get(v) or 0) != 0 else "—")
                  for k, v in _fields.items()}
        st.caption("Data availability: " + "  ·  ".join(f"{k} {v}" for k, v in _avail.items()))


# ─── Tab: Data Table ──────────────────────────────────────────────────────────

with tab_table:
    st.caption("Full snapshot for the selected year and category filter. Click column headers to sort.")

    display_cols = {
        "navn": "Company",
        "is_intraday": "Intraday",
        "is_hedgefund": "Hedge fund / intl.",
        "year": "Year",
        "is_restructuring": "Restructuring",
        "omsaetning": "Revenue (DKK)",
        "bruttoresultat": "Gross Profit (DKK)",
        "ebit": "EBIT (DKK)",
        "aarsresultat": "Net Profit (DKK)",
        "egenkapital_primo": "Opening Equity (DKK)",
        "egenkapital": "Closing Equity (DKK)",
        "aktiver": "Assets (DKK)",
        "ansatte": "Employees",
        "roe_pct": "ROE %",
        "roa_pct": "ROA %",
        "net_margin_pct": "Net Margin %",
        "ebit_margin_pct": "EBIT Margin %",
        "equity_ratio_pct": "Equity Ratio %",
        "gaeld_ratio_pct": "Debt Ratio %",
        "aktiv_omsaetning": "Asset Turnover",
        "omsaetning_per_ansatte_tdkk": "Rev/Employee (tDKK)",
        "resultat_per_ansatte_tdkk": "Profit/Employee (tDKK)",
        "rev_growth_pct": "Revenue Growth %",
    }

    available = [c for c in display_cols if c in df_snap.columns]
    df_show = df_snap[available].rename(columns=display_cols).sort_values(
        "Net Profit (DKK)", ascending=False, na_position="last"
    )

    st.dataframe(
        df_show,
        width="stretch",
        hide_index=True,
        height=600,
    )


# ─── Tab: Map ─────────────────────────────────────────────────────────────────

with tab_map:
    if df_locations.empty:
        st.info(
            "No location data yet. Run `python geocode_companies.py --seed` to populate "
            "approximate city-level coordinates, or `python geocode_companies.py` once "
            "the cvrapi.dk quota resets (June 1)."
        )
    else:
        # Merge latest-year financials into location data for tooltips
        latest = (
            df_all.sort_values("regnskab_slut")
            .groupby("cvr")
            .last()
            .reset_index()[["cvr", "omsaetning", "aarsresultat", "ansatte", "regnskab_slut"]]
        )
        map_df = df_locations.merge(latest, on="cvr", how="left")

        # Apply category filter (same as sidebar)
        if intraday_filter == "Pure intraday firms":
            map_df = map_df[map_df["is_intraday"] == True]
        elif intraday_filter == "Multi-desk traders":
            map_df = map_df[map_df["is_multidesk"] == True]
        elif intraday_filter == "US trading":
            map_df = map_df[map_df["is_us_trading"] == True]
        elif intraday_filter == "Hedge funds & intl. trading":
            map_df = map_df[map_df["is_hedgefund"] == True]

        def _cat_label(row: pd.Series) -> str:
            if row.get("is_hedgefund"):
                return "Hedge fund / intl."
            if row.get("is_multidesk"):
                return "Multi-desk"
            if row.get("is_us_trading"):
                return "US trading"
            if row.get("is_intraday"):
                return "Pure intraday"
            return "Other"

        map_df["category"] = map_df.apply(_cat_label, axis=1)
        _M = 1_000_000

        def _fmt_val(v, unit: str = "M") -> str:
            if pd.isna(v):
                return "N/A"
            return f"{v / _M:,.1f} {unit}"

        map_df["Revenue"] = map_df["omsaetning"].apply(_fmt_val)
        map_df["Net Profit"] = map_df["aarsresultat"].apply(_fmt_val)
        map_df["Employees"] = map_df["ansatte"].apply(
            lambda v: "N/A" if pd.isna(v) else f"{int(v)}"
        )
        map_df["Latest year"] = map_df["regnskab_slut"].apply(
            lambda v: str(v)[:4] if pd.notna(v) else "N/A"
        )

        _CAT_COLORS = {
            "Pure intraday":      "#f97316",
            "Multi-desk":         "#3b82f6",
            "US trading":         "#a855f7",
            "Hedge fund / intl.": "#ef4444",
            "Other":              "#6b7280",
        }

        fig_map = px.scatter_mapbox(
            map_df,
            lat="lat",
            lon="lon",
            color="category",
            color_discrete_map=_CAT_COLORS,
            hover_name="navn",
            hover_data={
                "category": True,
                "Revenue": True,
                "Net Profit": True,
                "Employees": True,
                "Latest year": True,
                "lat": False,
                "lon": False,
            },
            zoom=6,
            center={"lat": 56.27, "lon": 10.60},
            mapbox_style="open-street-map",
            height=700,
        )
        fig_map.update_traces(marker=dict(size=10, opacity=0.85))
        fig_map.update_layout(
            margin=dict(l=0, r=0, t=0, b=0),
            legend=dict(title="Category", x=0.01, y=0.99, bgcolor="rgba(0,0,0,0.5)"),
        )

        st.plotly_chart(fig_map, use_container_width=True, config={"scrollZoom": True})
        st.caption(
            f"Showing {len(map_df)} companies. Coordinates are city-level approximations "
            "seeded from company names — run `python geocode_companies.py` for exact addresses "
            "once cvrapi.dk quota resets (June 1)."
        )
