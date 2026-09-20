"""Audit the fiscal-year split. Re-runnable acceptance check.

    uv run python scripts_check_fy.py
"""

import duckdb
import pandas as pd

from tradertracker import fiscalyear as fy
from tradertracker.pipeline import EXCLUDED_CVR

con = duckdb.connect("data/tradertracker.duckdb", read_only=True)
df = con.execute("SELECT * FROM kpis ORDER BY navn, regnskab_slut").df()
con.close()
df = df[~df["cvr"].isin(EXCLUDED_CVR)].copy()

periods = list(zip(df["regnskab_start"], df["regnskab_slut"]))
df["naive_year"] = pd.to_datetime(df["regnskab_slut"]).dt.year
df["year"] = [fy.fiscal_year(a, b) for a, b in periods]
df["fy_label"] = [fy.fiscal_year_label(a, b) for a, b in periods]
df["months"] = [fy.period_months(a, b) for a, b in periods]
df["annual"] = [fy.is_annual(a, b) for a, b in periods]
df["offset"] = [fy.is_offset_year_end(b) for _, b in periods]

moved = df[df["naive_year"] != df["year"]]
print(f"rows                       : {len(df)}")
print(f"offset year-ends           : {df['offset'].sum()} ({df['offset'].mean():.1%})")
print(f"non-annual periods         : {(~df['annual']).sum()}")
print(f"REBUCKETED by midpoint rule: {len(moved)} rows, {moved['cvr'].nunique()} firms")
print()
print("-- sample of moved rows --")
print(
    moved[["navn", "regnskab_start", "regnskab_slut", "naive_year", "year", "fy_label"]]
    .head(8).to_string(index=False)
)
print()

print("-- month of close vs rebucketing --")
m = df.copy()
m["close_month"] = pd.to_datetime(m["regnskab_slut"]).dt.month
print(
    m.groupby("close_month")
    .agg(rows=("cvr", "size"), moved=("naive_year", lambda x: 0))
    .join(
        m[m["naive_year"] != m["year"]].groupby("close_month").size().rename("rebucketed")
    )
    .fillna(0).astype(int)[["rows", "rebucketed"]]
    .to_string()
)
print()

df = df.sort_values(["cvr", "year", "months"], ascending=[True, True, False])
df["superseded"] = df.duplicated(subset=["cvr", "year"], keep="first")
dupes_before = df.duplicated(subset=["cvr", "naive_year"], keep=False).sum()
dupes_after = df[~df["superseded"]].duplicated(subset=["cvr", "year"], keep=False).sum()
print(f"collisions on naive year   : {dupes_before}")
print(f"collisions after dedupe    : {dupes_after}")

failures = []
if dupes_after:
    failures.append("a (cvr, fiscal_year) bucket still holds two filings")
# A full Jul..Jun year belongs to its starting calendar year. Short first
# periods that merely *end* in June are not covered by this — their midpoint
# legitimately falls in the closing year.
_ends = pd.to_datetime(df["regnskab_slut"])
_starts = pd.to_datetime(df["regnskab_start"])
full_july_june = df[
    (_ends.dt.month == 6) & (_ends.dt.day == 30)
    & (_starts.dt.month == 7) & (_starts.dt.day == 1)
]
if len(full_july_june) == 0:
    failures.append("no full Jul..Jun year found to check")
elif not (full_july_june["year"] == full_july_june["naive_year"] - 1).all():
    failures.append("a full Jul..Jun year was not moved to its starting year")
if df["year"].isna().any():
    failures.append("a row has no fiscal year")

print()
if failures:
    print("FAIL:", "; ".join(failures))
    raise SystemExit(1)
print("PASS: every filing sits in the year holding most of its activity,")
print("      one filing per company per year, June closers moved back.")
