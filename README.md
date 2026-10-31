# TraderTracker

KPI analysis for Danish energy trading companies via the [cvr.dev](https://cvr.dev) API and public XBRL annual reports from Virk.dk.

## Data flow

```
cvr.dev API ─┐
             ├→  DuckDB (data/tradertracker.duckdb)  →  Excel (data/kpi_report.xlsx)
Virk XBRL  ──┘                                        →  Streamlit dashboard
```

## Features

- **Company discovery** — searches cvr.dev by name keywords, filters by NACE code (energy trading), and pulls explicit CVR numbers for known firms. A watchlist reports newly founded traders once they publish their first annual report.
- **XBRL financial parsing** — extracts P&L and balance sheet data from Danish GAAP (fsa) and IFRS filings, plain and inline XBRL; normalises EUR amounts to DKK. Every total in each filing is also kept in the `xbrl_facts` table, so new KPIs can be derived without refetching.
- **KPI computation** — returns (ROE, ROOE, ROTCD, ROIC, ROCE, ROA, TRADE), capital measures (total capital deployed, invested capital, interest-bearing debt, working capital), margins, per-employee figures and revenue growth
- **Skævt regnskabsår** — filings that do not close on 31 December are attributed to the fiscal year they mostly cover, and stubs and long first periods are annualised for ratios (see `tradertracker/fiscalyear.py`)
- **Manual patch system** — overrides incomplete XBRL filings with figures from PDF annual reports; patches survive pipeline refreshes
- **Streamlit dashboard** — overview, rankings, time series, headcount trends, pure-intraday focus, Sankey money flow, geographic map, data table and a fiscal-year timeline
- **Excel export** — snapshot + time series + intraday focus sheets
- **Audit tool** — validates equity movement, P&L reconstruction, data coverage
- **Geocoding** — resolves registered addresses to lat/lon via OpenStreetMap Nominatim

## Company classification

Companies are categorised based on their trading profile:

| Category | Description |
|---|---|
| **Pure intraday** | Trade exclusively/semi-exclusively in the short-term electricity spot market (NACE 351590) |
| **Multi-desk** | Broader energy trading firms covering power, gas, long-term, and structured products |
| **US trading** | Danish entities focused on US power markets |
| **Hedge fund / intl.** | International quant/commodity firms with a Danish entity (STG/Squarepoint, Qube, Balyasny, Trafigura) |

A manually curated exclusion list filters out grid operators, retail utilities, wind cooperatives, personal names, shells, and other non-wholesale entities.

## Setup

```bash
# 1. Clone and enter directory
cd tradertracker

# 2. Install dependencies (requires uv: https://docs.astral.sh/uv/)
uv sync

# 3. Copy and fill in API key
cp .env.example .env
# Edit .env with your key from https://cvr.dev/settings

# 4. Run the full pipeline (discover + fetch + export)
uv run tradertracker
```

## Usage

```bash
# Fetch companies + parse financials → DuckDB (new filings only)
uv run tradertracker --fetch

# Re-parse every filing (after a parser change)
uv run tradertracker --fetch --refresh

# Compute KPIs → Excel
uv run tradertracker --export

# Both (default)
uv run tradertracker

# Inspect raw XBRL facts for a specific company
uv run tradertracker --inspect <cvr-number>

# Launch the interactive dashboard
uv run streamlit run tradertracker/dashboard.py

# Geocode company addresses (after --fetch)
uv run python geocode_companies.py --seed   # postcode-level approximation
uv run python geocode_companies.py          # street-level geocoding via Nominatim (1 req/s)

# Run financial data audit
uv run python audit_financials.py

# Apply manual patches to database
uv run python apply_patches.py

# Check fiscal-year attribution for collisions
uv run python scripts_check_fy.py

# Tests
uv run --group dev pytest -q
```

## Key metrics

| Metric | Definition |
|---|---|
| **ROE** | Net profit / closing equity |
| **ROOE** | Net profit / opening equity (more accurate for fast-growing firms) |
| **ROTCD** | Net profit / opening total capital deployed — the owners' return when they fund the firm with loans as well as equity |
| **ROIC** | NOPAT / opening invested capital; NOPAT = EBIT × (1 − effective tax rate, 22% default) |
| **ROCE** | EBIT / (assets − current liabilities) |
| **ROA** | Net profit / total assets |
| **TRADE** | Gross trading profit / (headcount × opening deployed capital) — custom metric capturing efficiency per person per krone of owner capital |
| **Total capital deployed** | Equity + related-party and shareholder loans (what the owners have put at work) |
| **Invested capital** | Equity + interest-bearing debt: banks, credit institutions, mortgages, IFRS 16 leases, related-party loans. Cash is not netted (margin collateral) |
| **Working capital** | Current assets − current liabilities |
| **Operating working capital** | Working capital without cash, securities, short-term bank and related-party debt |
| **Net Margin** | Net profit / revenue |
| **Equity Ratio** | Equity / total assets |
| **Asset Turnover** | Revenue / total assets |
| **Revenue / Employee** | Revenue / headcount (tDKK) |

Ratios of a flow to a stock (returns, per-employee figures) are annualised for periods that are not ~12 months; normal years are never rescaled.

## Project structure

```
tradertracker/
├── tradertracker/
│   ├── __init__.py
│   ├── pipeline.py      # ETL: API → XBRL parsing → DuckDB → Excel
│   ├── fiscalyear.py    # Skævt regnskabsår: fiscal-year attribution, annualisation
│   └── dashboard.py     # Streamlit interactive dashboard
├── tests/               # pytest: fiscal years, XBRL parsing, dashboard smoke test
├── geocode_companies.py # Address geocoding via Nominatim
├── audit_financials.py  # Data quality audit
├── apply_patches.py     # Apply manual XBRL patches to DB
├── scripts_check_fy.py  # Fiscal-year collision check
├── pyproject.toml       # Project config + dependencies
├── data/
│   ├── tradertracker.duckdb  # DuckDB database
│   └── kpi_report.xlsx       # Exported Excel report
└── .env                # API key (not committed)
```

## Data sources

- **[cvr.dev](https://cvr.dev)** — Danish business registry API (company metadata, financial document references). Requires a subscription; the pipeline falls back to apicvr.dk and the public Virk distribution API when it is unavailable.
- **[Virk.dk](https://regnskaber.virk.dk)** — Public XBRL annual reports
- **[OpenStreetMap Nominatim](https://nominatim.org)** — geocoding (free, 1 request per second). DAWA, used before, was shut down.

## Notes

- EUR amounts in XBRL filings are converted at a fixed 7.46 DKK/EUR (the DKK peg).
- XBRL parsing reads statement totals only: facts in a dimensional context (one column of the equity statement, one segment) are breakdowns and are used only when a filing tags no total. ESEF reports that tag every total as parent-only (`SeparateMember`) count as totals.
- Inline XBRL numbers are read according to their `ixt` format — Danish reports write "1.063.876" for a million.
- Some companies file XBRL stubs with minimal tagging — manual patches fill in figures from PDF annual reports.
