"""Apply MANUAL_PATCHES to the live database without a full pipeline refresh."""
import sys
sys.path.insert(0, ".")
from tradertracker.pipeline import MANUAL_PATCHES, DB_PATH, _KPI_VIEW
import duckdb

con = duckdb.connect(str(DB_PATH))
for (patch_cvr, patch_date), fields in MANUAL_PATCHES.items():
    sets = ", ".join(f"{col} = {repr(val)}" for col, val in fields.items())
    rows = con.execute(
        f"UPDATE financials SET {sets} "
        f"WHERE cvr = {patch_cvr} AND CAST(regnskab_slut AS VARCHAR) = '{patch_date}'"
    ).rowcount
    print(f"CVR {patch_cvr} {patch_date}: updated {rows} row(s) — {list(fields.keys())}")
con.execute(_KPI_VIEW)
con.close()
print("Done.")
