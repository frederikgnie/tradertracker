"""
Comprehensive financial data audit for TraderTracker.

Checks:
1. Equity movement: equity[t] ≈ equity[t-1] + net_profit[t] - dividends[t]
   Unexplained gap = implied equity injection / other equity changes.
2. P&L sanity: EBIT + fin_net ≈ EBT ≈ net_profit + tax
3. Missing fields: personnel / fin / tax present when implied by P&L gap
4. Headcount vs personnel costs consistency
"""
import duckdb
import pandas as pd

con = duckdb.connect("data/tradertracker.duckdb", read_only=True)

df = con.execute("""
    SELECT f.cvr, c.navn, c.is_intraday,
           f.regnskab_slut AS period_end,
           f.regnskab_start AS period_start,
           f.omsaetning,
           f.bruttoresultat,
           f.ebit,
           f.aarsresultat,
           f.egenkapital,
           f.egenkapital_primo,
           f.aktiver,
           f.kortfristet_gaeld,
           f.langfristet_gaeld,
           f.ansatte_regnskab,
           f.personaleomkostninger,
           f.afskrivninger,
           f.fin_indt,
           f.fin_udg,
           f.skat,
           f.udbytte,
           f.vareforbrug
    FROM financials f
    JOIN companies c USING (cvr)
    ORDER BY c.navn, f.regnskab_slut
""").df()

con.close()

df["period_end"] = pd.to_datetime(df["period_end"])
df["year"] = df["period_end"].dt.year

# ── 1. Equity movement check ──────────────────────────────────────────────────
# expected: equity[t] = equity[t-1] + profit[t] - dividends[t]
# implied_other = equity[t] - equity[t-1] - profit[t] + dividends[t]
# Large implied_other means: capital injection, revaluation, or missing dividends

df = df.sort_values(["cvr", "period_end"])
df["equity_prev"] = df.groupby("cvr")["egenkapital"].shift(1)
df["udbytte_safe"] = df["udbytte"].fillna(0)
df["profit_safe"] = df["aarsresultat"].fillna(0)

df["equity_implied_end"] = df["equity_prev"] + df["profit_safe"] - df["udbytte_safe"]
df["equity_gap"] = df["egenkapital"] - df["equity_implied_end"]  # unexplained delta

# Flag rows where gap > 10% of equity or > 10M DKK
threshold_abs = 10_000_000
threshold_pct = 0.10

equity_issues = df[
    df["equity_prev"].notna() &
    df["egenkapital"].notna() &
    (df["equity_gap"].abs() > threshold_abs) &
    (df["equity_gap"].abs() / df["egenkapital"].abs().clip(1) > threshold_pct)
].copy()

equity_issues["gap_M"] = (equity_issues["equity_gap"] / 1e6).round(1)
equity_issues["profit_M"] = (equity_issues["profit_safe"] / 1e6).round(1)
equity_issues["div_M"] = (equity_issues["udbytte_safe"] / 1e6).round(1)
equity_issues["eq_M"] = (equity_issues["egenkapital"] / 1e6).round(1)
equity_issues["eq_prev_M"] = (equity_issues["equity_prev"] / 1e6).round(1)

print("=" * 80)
print("EQUITY MOVEMENT ANOMALIES  (gap > 10M DKK and > 10% of equity)")
print("  Positive gap = equity higher than expected (capital injection / unreported equity)")
print("  Negative gap = equity lower than expected (missing dividends / losses not captured)")
print("=" * 80)
for _, r in equity_issues.sort_values("gap_M", key=abs, ascending=False).iterrows():
    print(f"  {r['navn']:45s} {int(r['year'])}  "
          f"eq_prev={r['eq_prev_M']:8.1f}M  profit={r['profit_M']:8.1f}M  "
          f"div={r['div_M']:7.1f}M  eq_end={r['eq_M']:8.1f}M  "
          f"GAP={r['gap_M']:+8.1f}M")

# ── 2. P&L sanity check ───────────────────────────────────────────────────────
# net_profit should ≈ ebit + fin_indt - fin_udg - skat
# If they differ by > threshold, we may be missing financial items

df["fin_net"] = df["fin_indt"].fillna(0) - df["fin_udg"].fillna(0)
df["skat_safe"] = df["skat"].fillna(0)
df["ebit_safe"] = df["ebit"].fillna(0)
df["reconstructed_profit"] = df["ebit_safe"] + df["fin_net"] - df["skat_safe"]
df["pnl_gap"] = df["aarsresultat"] - df["reconstructed_profit"]

# Only flag when all fields are non-null and gap is material
pnl_issues = df[
    df["ebit"].notna() & df["aarsresultat"].notna() & df["skat"].notna() &
    (df["pnl_gap"].abs() > threshold_abs) &
    (df["pnl_gap"].abs() / df["aarsresultat"].abs().clip(1) > threshold_pct)
].copy()

pnl_issues["gap_M"] = (pnl_issues["pnl_gap"] / 1e6).round(1)
pnl_issues["recon_M"] = (pnl_issues["reconstructed_profit"] / 1e6).round(1)
pnl_issues["actual_M"] = (pnl_issues["aarsresultat"] / 1e6).round(1)
pnl_issues["ebit_M"] = (pnl_issues["ebit_safe"] / 1e6).round(1)
pnl_issues["fin_M"] = (pnl_issues["fin_net"] / 1e6).round(1)
pnl_issues["tax_M"] = (pnl_issues["skat_safe"] / 1e6).round(1)

print()
print("=" * 80)
print("P&L RECONSTRUCTION GAPS  (actual net profit vs EBIT + fin_net - tax)")
print("  Large gap usually means financial income/expense not captured in XBRL")
print("=" * 80)
for _, r in pnl_issues.sort_values("gap_M", key=abs, ascending=False).iterrows():
    print(f"  {r['navn']:45s} {int(r['year'])}  "
          f"EBIT={r['ebit_M']:8.1f}M  fin={r['fin_M']:+6.1f}M  tax={r['tax_M']:6.1f}M  "
          f"  recon={r['recon_M']:8.1f}M  actual={r['actual_M']:8.1f}M  "
          f"GAP={r['gap_M']:+7.1f}M")

# ── 3. Missing personnel costs when company has >5 employees ──────────────────
missing_pers = df[
    (df["ansatte_regnskab"] > 5) &
    df["personaleomkostninger"].isna() &
    df["aarsresultat"].notna()
].copy()

print()
print("=" * 80)
print("MISSING PERSONNEL COSTS  (>5 employees in annual report, no personaleomkostninger)")
print("=" * 80)
for _, r in missing_pers.sort_values(["navn", "year"]).iterrows():
    print(f"  {r['navn']:45s} {int(r['year'])}  employees={int(r['ansatte_regnskab'])}")

# ── 4. Gross presenter check ──────────────────────────────────────────────────
# For gross presenters: revenue should > bruttoresultat > 0
# Check for cases where bruttoresultat > revenue (likely a parsing error)
gross_anomaly = df[
    df["bruttoresultat"].notna() & df["omsaetning"].notna() &
    (df["bruttoresultat"] > df["omsaetning"] * 1.02)  # allow 2% rounding
].copy()

print()
print("=" * 80)
print("GROSS PROFIT > REVENUE  (parsing likely wrong for gross presenters)")
print("=" * 80)
for _, r in gross_anomaly.sort_values("year").iterrows():
    rev_M = r["omsaetning"] / 1e6
    gp_M  = r["bruttoresultat"] / 1e6
    print(f"  {r['navn']:45s} {int(r['year'])}  revenue={rev_M:8.1f}M  brutto={gp_M:8.1f}M")

# ── 5. Summary stats ──────────────────────────────────────────────────────────
print()
print("=" * 80)
print("DATA COVERAGE SUMMARY")
print("=" * 80)
total = len(df)
cols = ["omsaetning", "ebit", "aarsresultat", "egenkapital", "aktiver",
        "personaleomkostninger", "bruttoresultat", "afskrivninger",
        "fin_indt", "fin_udg", "skat", "udbytte", "egenkapital_primo"]
for col in cols:
    n_present = df[col].notna().sum()
    pct = n_present / total * 100
    print(f"  {col:35s} {n_present:4d}/{total} ({pct:4.0f}%)")

# ── 6. Companies with revenues but no EBIT ────────────────────────────────────
missing_ebit = df[
    df["omsaetning"].notna() & df["omsaetning"].abs() > 1_000_000 &
    df["ebit"].isna()
]
print()
print("=" * 80)
print("REVENUE > 1M BUT NO EBIT")
print("=" * 80)
for _, r in missing_ebit.sort_values(["navn", "year"]).iterrows():
    print(f"  {r['navn']:45s} {int(r['year'])}  revenue={r['omsaetning']/1e6:.1f}M")

print()
print("Audit complete.")
