#!/usr/bin/env python3
"""
Geocode company addresses and store lat/lon in DuckDB.

Strategy:
  1. Fetch exact registered address per CVR from api.cvr.dev (individual lookup — separate
     quota from the batch company-discovery search, works even when monthly search quota is full).
  2. Geocode the address via OpenStreetMap Nominatim (free, 1 request/second).
     DAWA, used before, was shut down in 2026 (HTTP 410).

Usage:
    uv run python geocode_companies.py           # geocode only missing companies
    uv run python geocode_companies.py --refresh # re-geocode all companies
    uv run python geocode_companies.py --seed    # fallback: city-level approx, no cvr.dev calls
"""

import argparse
import os
import random
import time
from pathlib import Path

import duckdb
import httpx
from dotenv import load_dotenv

load_dotenv()

DB_PATH = Path("data/tradertracker.duckdb")
_CVR_KEY = os.getenv("CVR_DEV_API_KEY", "")
_UA = "TraderTracker/1.0 (contact: fgn@odigoenergy.com)"

_CREATE_TABLE = """
    CREATE TABLE IF NOT EXISTS company_locations (
        cvr     INTEGER PRIMARY KEY,
        adresse VARCHAR,
        postby  VARCHAR,
        postnr  VARCHAR,
        lat     DOUBLE,
        lon     DOUBLE
    )
"""


_NOMINATIM = "https://nominatim.openstreetmap.org/search"
_last_nominatim_call = 0.0


def _nominatim(params: dict[str, str]) -> tuple[float, float] | None:
    """One Nominatim lookup restricted to Denmark, or None.

    The public instance allows at most one request per second and requires an
    identifying User-Agent, both enforced here.
    """
    global _last_nominatim_call
    wait = 1.1 - (time.monotonic() - _last_nominatim_call)
    if wait > 0:
        time.sleep(wait)
    _last_nominatim_call = time.monotonic()
    try:
        r = httpx.get(
            _NOMINATIM,
            params={**params, "countrycodes": "dk", "format": "jsonv2", "limit": "1"},
            headers={"User-Agent": _UA},
            timeout=15,
        )
        hits = r.json() if r.status_code == 200 else []
        if hits:
            return float(hits[0]["lat"]), float(hits[0]["lon"])
    except Exception:
        pass
    return None


def _postcode_centroid(postnr: str | int) -> tuple[float, float] | None:
    return _nominatim({"postalcode": str(postnr)})


def _geocode(vejnavn: str, husnr: str, postnr: str | int) -> tuple[float, float] | None:
    """Return (lat, lon) for a Danish address via OpenStreetMap Nominatim, or None.

    DAWA (api.dataforsyningen.dk), used until 2026, now answers 410 Gone.
    husnr should include the letter suffix (e.g. '17A', not just '17').
    Falls back to the postcode's centroid when the street address is unknown.
    """
    street = f"{husnr} {vejnavn}".strip()
    return (
        _nominatim({"street": street, "postalcode": str(postnr)})
        or _nominatim({"q": f"{vejnavn} {husnr}, {postnr}"})
        or _postcode_centroid(postnr)
    )


def geocode_all(refresh: bool = False) -> None:
    con = duckdb.connect(str(DB_PATH))
    con.execute(_CREATE_TABLE)

    if refresh:
        cvrs = [r[0] for r in con.execute("SELECT cvr FROM companies ORDER BY cvr").fetchall()]
    else:
        # Companies that have no location or only seed data (adresse IS NULL means seeded)
        cvrs = [r[0] for r in con.execute("""
            SELECT c.cvr FROM companies c
            LEFT JOIN company_locations l USING (cvr)
            WHERE l.cvr IS NULL OR l.adresse IS NULL
            ORDER BY c.cvr
        """).fetchall()]

    print(f"Geocoding {len(cvrs)} companies (cvr.dev address + Nominatim coordinates)...")
    ok = skipped = failed = 0

    with httpx.Client(
        timeout=10,
        headers={"Authorization": _CVR_KEY, "User-Agent": _UA},
    ) as client:
        for i, cvr in enumerate(cvrs, 1):
            try:
                resp = client.get(
                    "https://api.cvr.dev/api/cvr/virksomhed",
                    params={"cvr_nummer": cvr},
                )
                if resp.status_code == 429:
                    err = resp.json()
                    print(f"  cvr.dev quota: {err.get('message', 'quota exceeded')} — stopping")
                    break
                if resp.status_code != 200:
                    print(f"  {cvr}: HTTP {resp.status_code}")
                    failed += 1
                    continue

                companies = resp.json()
                if not isinstance(companies, list) or not companies:
                    skipped += 1
                    continue

                meta = companies[0].get("virksomhedMetadata", {})
                addr = meta.get("nyesteBeliggenhedsadresse") or {}
                vejnavn = addr.get("vejnavn")
                husnr   = addr.get("husnummerFra")
                bogstav = addr.get("bogstavFra") or ""
                postnr  = addr.get("postnummer")
                postby  = addr.get("postdistrikt")

                if not vejnavn or not postnr:
                    skipped += 1
                    continue

                # Full house number including letter suffix (e.g. "17A")
                husnr_full = f"{husnr}{bogstav}" if husnr else ""
                etage       = addr.get("etage") or ""
                adresse_str = vejnavn
                if husnr_full:
                    adresse_str += f" {husnr_full}"
                if etage:
                    adresse_str += f", {etage}."
                adresse_str += f", {postnr} {postby or ''}"

                coords = _geocode(vejnavn, husnr_full, postnr)
                if not coords:
                    skipped += 1
                    continue

                lat, lon = coords
                con.execute(
                    "INSERT OR REPLACE INTO company_locations "
                    "(cvr, adresse, postby, postnr, lat, lon) VALUES (?, ?, ?, ?, ?, ?)",
                    [cvr, adresse_str.strip(), postby, str(postnr), round(lat, 6), round(lon, 6)],
                )
                ok += 1
                if i % 20 == 0:
                    print(f"  {i}/{len(cvrs)} — ok={ok} skip={skipped} fail={failed}")
                time.sleep(0.15)

            except Exception as exc:
                print(f"  {cvr}: {exc}")
                failed += 1

    con.close()
    print(f"Done: {ok} geocoded, {skipped} skipped, {failed} failed")


# ── Fallback seed: city-inference + postcode centroids ────────────────────

_CITY_POSTCODES: dict[str, str] = {
    "aarhus": "8000", "aros": "8000", "kolding": "6000", "odense": "5000",
    "energi fyn": "5000", "fyn handel": "5000", "viborg": "8800",
    "struer": "7600", "aalborg": "9000", "frederikshavn": "9900",
    "lemvig": "7620", "esbjerg": "6700", "hjerting": "6710",
    "bornholm": "3700", "horsens": "8700", "vestforsyning": "6950",
    "copenhagen": "1050", "aars-hornum": "9600", "aars hornum": "9600",
}
_KNOWN_AARHUS: set[int] = {
    28113951, 41419849, 25118359, 17225898, 38381954, 43398288,
    38175130, 40213066, 39739623, 40247645, 40151346, 40374558,
    40816291, 44359391, 45342050, 43431579,
}
_DEFAULT_PC = "1050"


def _infer_postcode(cvr: int, navn: str) -> str:
    if cvr in _KNOWN_AARHUS:
        return "8000"
    nl = navn.lower()
    for kw, pc in _CITY_POSTCODES.items():
        if kw in nl:
            return pc
    return _DEFAULT_PC


def seed_locations(refresh: bool = False) -> None:
    con = duckdb.connect(str(DB_PATH))
    con.execute(_CREATE_TABLE)

    rows = con.execute("SELECT cvr, navn FROM companies ORDER BY cvr").fetchall()
    if not refresh:
        existing = {r[0] for r in con.execute("SELECT cvr FROM company_locations").fetchall()}
        rows = [(c, n) for c, n in rows if c not in existing]

    pc_cache: dict[str, tuple[float, float]] = {}
    rng = random.Random(42)
    ok = 0
    for cvr, navn in rows:
        pc = _infer_postcode(cvr, navn or "")
        if pc not in pc_cache:
            pc_cache[pc] = _postcode_centroid(pc) or (55.6761, 12.5683)
        lat, lon = pc_cache[pc]
        lat += rng.uniform(-0.008, 0.008)
        lon += rng.uniform(-0.012, 0.012)
        con.execute(
            "INSERT OR REPLACE INTO company_locations (cvr, postby, postnr, lat, lon) "
            "VALUES (?, ?, ?, ?, ?)",
            [cvr, None, pc, round(lat, 6), round(lon, 6)],
        )
        ok += 1

    con.close()
    print(f"Seeded {ok} companies with approximate city-level coordinates")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true", help="Replace existing entries")
    ap.add_argument("--seed", action="store_true",
                    help="Fast fallback: city-level approximation (no cvr.dev calls)")
    args = ap.parse_args()

    if args.seed:
        seed_locations(refresh=args.refresh)
    else:
        geocode_all(refresh=args.refresh)
