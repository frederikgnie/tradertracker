"""The kpis view's capital measures, on an in-memory DuckDB."""

import duckdb
import pytest

from tradertracker.pipeline import _BALANCE_DETAIL_COLS, _DDL, _KPI_VIEW


@pytest.fixture
def con() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute(_DDL)
    for col in ("ansatte_regnskab INTEGER", "personaleomkostninger DOUBLE",
                "egenkapital_primo DOUBLE", "bruttoresultat DOUBLE",
                *(f"{c} DOUBLE" for c in ("vareforbrug", "afskrivninger", "fin_indt", "fin_udg",
                                          "skat", "udbytte", "gaeld_tilknyttede_lt",
                                          "gaeld_tilknyttede_st", "ebt", "kassebeholdning",
                                          "omloebsaktiver")),
                *(f"{c} DOUBLE" for c in _BALANCE_DETAIL_COLS)):
        name = col.split()[0]
        cols = {r[0] for r in con.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name='financials'"
        ).fetchall()}
        if name not in cols:
            con.execute(f"ALTER TABLE financials ADD COLUMN {col}")
    con.execute("INSERT INTO companies (cvr, navn) VALUES (1, 'Trader')")
    con.execute(_KPI_VIEW)
    return con


def _kpis(con, **fin) -> dict:
    fin = {"cvr": 1, "regnskab_slut": "2024-12-31", "regnskab_start": "2024-01-01", **fin}
    cols = ", ".join(fin)
    con.execute(f"INSERT INTO financials ({cols}) VALUES ({', '.join('?' * len(fin))})",
                list(fin.values()))
    cur = con.execute("SELECT * FROM kpis")
    names = [d[0] for d in cur.description]
    return dict(zip(names, cur.fetchone(), strict=True))


def test_capital_measures(con):
    k = _kpis(con, egenkapital=100.0, gaeld_tilknyttede_lt=40.0, gaeld_tilknyttede_st=10.0,
              bankgaeld_st=5.0, bankgaeld_lt=20.0, omloebsaktiver=300.0,
              kortfristet_gaeld=180.0, kassebeholdning=120.0, vaerdipapirer=30.0)
    assert k["total_capital_deployed"] == 150.0          # equity + related loans
    assert k["rentebaerende_gaeld"] == 75.0              # bank + related loans
    assert k["investeret_kapital"] == 175.0              # equity + interest-bearing debt
    assert k["arbejdskapital"] == 120.0                  # 300 - 180
    # (300 - 120 - 30) - (180 - 5 - 10) = 150 - 165
    assert k["driftsarbejdskapital"] == -15.0


def test_untagged_lines_count_as_zero(con):
    k = _kpis(con, egenkapital=100.0)
    assert k["total_capital_deployed"] == 100.0
    assert k["investeret_kapital"] == 100.0
    assert k["rentebaerende_gaeld"] == 0.0
    assert k["arbejdskapital"] is None                   # no current assets/liabilities


def test_no_balance_sheet_is_null_not_zero(con):
    k = _kpis(con, aarsresultat=5.0)
    assert k["rentebaerende_gaeld"] is None
    assert k["investeret_kapital"] is None
    assert k["total_capital_deployed"] is None
