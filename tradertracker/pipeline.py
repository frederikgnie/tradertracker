#!/usr/bin/env python3
"""
TraderTracker — Danish energy company KPI pipeline

Data flow:
  cvr.dev API  ->  DuckDB (data/tradertracker.duckdb)  ->  Excel (data/kpi_report.xlsx)

Discovery: name-based searches filtered by NACE code (segmentation API requires paid plan).
Financials: downloaded from XBRL annual reports via public regnskaber.virk.dk URLs.

Usage:
    uv run tradertracker --fetch       # Fetch companies + parse financials -> DuckDB
    uv run tradertracker --export      # Compute KPIs -> Excel
    uv run tradertracker               # Both
    uv run tradertracker --inspect <cvr>  # Dump raw facts for a company
"""

import argparse
import io
import math
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from xml.etree import ElementTree as ET

import duckdb
import httpx
import pandas as pd
from dotenv import load_dotenv

from tradertracker import fiscalyear as fy

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# ── Configuration ──────────────────────────────────────────────────────────────

API_BASE = "https://api.cvr.dev"
# NOTE: cvr.dev has a monthly API call quota. If you hit the limit, it resets on the 1st of the next month.
# Monthly quota was exhausted on 2026-05-09 → quota resets 2026-06-01.
DB_PATH = Path("data/tradertracker.duckdb")
XLSX_PATH = Path("data/kpi_report.xlsx")

# NACE codes for energy trading (add more as needed)
TARGET_NACE = {"354000", "351590", "352300", "351400"}

# Name-based search terms — results filtered client-side by TARGET_NACE
SEARCH_TERMS = [
    "energihandel",
    "elhandel",
    "gashandel",
    "energitrading",
    "power trading",
    "energy trading",
    "el trading",
    "energimaegler",
    "energimægler",
    "nordic energy",
    "nordic power",
    "balance energy",
    "commodity trading",
    # Add company names you know about:
    "mercury energy",
    "odigo energy",
    "alpha energy",
    "aarhus trading",
    "mft energy",
    "neas energy",
    "energi dk",
    "el-net",
    "engros el",
    "spot energy",
    "clean energy capital",
    "pöyry energy",
    "thisted el",
    "radius elnet",
    "vindenergi",
    "vattenfall",
    "ørsted trading",
    "clever energy",
]

# Pure intraday firms — trade primarily/exclusively in the short-term electricity spot market
INTRADAY_CVR: set[int] = {
    40213066,  # MFT Energy 1 ApS          NACE 354000
    42377449,  # Twig Energy ApS            NACE 351590
    39711303,  # QUENT ApS                  NACE 351590
    42526991,  # Copenhagen Energy Trading  NACE 351590
    44811960,  # Odigo Energy ApS           NACE 351590
    42985449,  # Alpha Energy ApS           NACE 351590
    38617990,  # Current Commodities A/S    NACE 351590
    42192287,  # Inductive Energy A/S       NACE 351590
    41622296,  # Helios Power Trading A/S   NACE 351590
    45258777,  # Aarhus Trading A/S         NACE 351590
    43343122,  # BD Energy A/S              NACE 351590
    39533685,  # Nordic Energy House ApS    NACE 351590
    43398288,  # Aros Commodities A/S       NACE 351590
    42676330,  # ENZEE Commodities A/S      NACE 351590
    45501248,  # Ampere Commodities ApS     NACE 351590
    44283042,  # Arcane Energy ApS          NACE 351590
    46075307,  # Delta Energy Trading ApS   NACE 351590
    43423320,  # Eio Energi ApS             NACE 351590
    44737949,  # Grid Commodities ApS       NACE 351590
    45247015,  # Impact Energy Trading A/S  NACE 351590
    45702901,  # Mars Trading A/S           NACE 351590
    45116751,  # Mercury Energy Research ApS NACE 351590
    36201770,  # POWERMART ApS              NACE 351590
    44503344,  # Wolfram Trading ApS        NACE 821000 (non-standard)
    40343407,  # Vis Commodities ApS        NACE 351590
    45727076,  # Tilde Energy Trading A/S   NACE 351590
    44323788,  # C A Energy Trading A/S     NACE 351590
    41338741,  # C A Commodities            NACE (to be confirmed)
    44372487,  # Aro.inc ApS                NACE (to be confirmed)
    42643785,  # ENERGETICK APS             NACE 351590
    40300643,  # Yggdrasil Commodities ApS  NACE 821000 (consolidates Dvalin/Nidhog/Asgard/Thordin shells)
    43932454,  # STG (Denmark) ApS          NACE 663000 (Squarepoint group)
    44120623,  # Qube Research & Technologies Denmark ApS
    44240688,  # Balyasny Management (Denmark) ApS
    43431579,  # Trafigura Denmark ApS
    45849066,  # Setpoint Energy ApS        NACE 351590 (already in NACE list)
    44590832,  # Quantum Power Trading ApS  NACE 351590
    44967782,  # ENZOL Energy Trading ApS   NACE 351590
    46729846,  # Halia Energy Europe ApS    NACE 351590 (2026, Halia Energy Group's European arm)
}

# Hedge funds & international commodity trading firms with a Danish entity.
# These also appear in INTRADAY_CVR so they show up in the Pure Intraday tab too.
HEDGE_FUND_CVR: set[int] = {
    43932454,  # STG (Denmark) ApS          NACE 663000 (Squarepoint group)
    44120623,  # Qube Research & Technologies Denmark ApS
    44240688,  # Balyasny Management (Denmark) ApS
    43431579,  # Trafigura Denmark ApS
}

# Multi-desk energy traders (power, gas, long-term etc.) — intraday is one desk among several
EXPLICIT_CVR: set[int] = {
    20293195,  # Centrica Energy Trading A/S   NACE 351590
    28113951,  # DANSKE COMMODITIES A/S        NACE 351590
    17225898,  # Mind Energy A/S (Energi Danmark) NACE 351590
    38381954,  # In Commodities A/S            NACE 351590
    45136132,  # In Commodities Asset Management A/S NACE 351590
    38175130,  # MFT Energy A/S                NACE 354000
    38680781,  # Nitor Energy A/S              NACE 351590
    41419849,  # Norlys Energy Trading A/S     NACE 351590
    46868781,  # Decipher ApS                  NACE 351590 (2026, CIP's energy-trading company)
    46662466,  # LC Commodities A/S            NACE 351590 (2026, Henrik Lind / Lind Capital)
    46585399,  # In Commodities Stack A/S      NACE 351590 (2026, InCommodities group, Tangen 6)
}

# US-focused energy traders — trade primarily on US power markets
US_TRADING_CVR: set[int] = {
    43495127,  # Nabla Technologies ApS      NACE 621000 (non-standard)
    44882906,  # Halia Energy ApS            NACE 351590
    44632691,  # Arcane Energy US ApS        NACE 351590
    45053326,  # BD Energy US ApS            NACE 351590
    44238837,  # ENZEE Commodities US ApS    NACE 351590
    # MFT Energy US 1 — CVR unknown (possibly US-registered entity)
    # MFT Energy US 2 — CVR unknown (possibly US-registered entity)
}

# Full NACE 351590 company list scraped from datacvrapi.dk (261 companies, May 2026)
NACE_351590_CVR: set[int] = {
    10042585, 10238668, 10990076, 12257899, 12352204, 12406010, 17225898, 20293195,
    20810440, 20843187, 21105848, 21191809, 21311332, 21750875, 24213528, 24982629,
    25113284, 25118359, 25119207, 25322754, 25460472, 25481941, 25567714, 25604849,
    25664132, 25794206, 26598613, 26789575, 26891272, 27210538, 28113951, 28130465,
    28271646, 29225311, 29312834, 29685746, 30105583, 30871359, 32285759, 32302157,
    32469469, 32814751, 32989756, 33268645, 33575122, 33884788, 34079145, 34221162,
    34500568, 35412220, 35521720, 35781234, 35786236, 35857842, 36201770, 36273240,
    36898895, 36921056, 36944854, 37001465, 37037176, 37060127, 37195030, 37271632,
    37336505, 37392197, 37565318, 37783838, 37862088, 37960705, 37983403, 38030590,
    38175130, 38381954, 38422952, 38487345, 38617990, 38680781, 38732056, 38732684,
    39067366, 39226197, 39533685, 39577445, 39632977, 39711303, 39905795, 39962764,
    40343407, 40420347, 40450238, 40541365, 40587985, 40739823, 40807721, 40879196,
    40897372, 41296798, 41338741, 41495235, 41517301, 41598441, 41622296, 41718684,
    41866896, 41927321, 42133434, 42192287, 42308382, 42377449, 42420158, 42439258,
    42526991, 42643785, 42676330, 42970239, 42985449, 43021494, 43187775, 43265709,
    43343122, 43398288, 43423320, 43526146, 43562037, 43565729, 43588966, 43608924,
    43783149, 43928074, 43965174, 43983563, 43983652, 44006146, 44016133, 44204681,
    44238071, 44238837, 44240351, 44263637, 44293218, 44301059, 44323788, 44351218,
    44352214, 44373580, 44401479, 44402130, 44421070, 44422778, 44423081, 44436531,
    44459760, 44478188, 44508567, 44510758, 44565188, 44590832, 44606879, 44618524,
    44632691, 44667770, 44710250, 44718243, 44737949, 44747987, 44764261, 44797534,
    44811960, 44834685, 44866587, 44879344, 44882906, 44908476, 44913844, 44913852,
    44913887, 44913895, 44921227, 44925702, 44937395, 44937492, 44944723, 44967782,
    44973545, 44973987, 44988429, 44993821, 44996766, 45053326, 45102688, 45108996,
    45109674, 45112888, 45116751, 45126501, 45136132, 45152022, 45157822, 45165426,
    45170845, 45204863, 45225291, 45247015, 45249220, 45258777, 45259331, 45274950,
    45277844, 45308170, 45319903, 45342050, 45357481, 45357961, 45425673, 45463834,
    45473570, 45484548, 45495256, 45501248, 45511820, 45517888, 45526453, 45529908,
    45539865, 45570002, 45578208, 45585018, 45604489, 45636496, 45679365, 45726770,
    45727076, 45750558, 45786943, 45794598, 45849066, 45865495, 45896390, 45927393,
    46047435, 46075307, 46097866, 46099109, 46141032, 46144910, 46150414, 46180445,
    46192656, 46217373, 46236963, 46252098, 46336860, 46340612, 46372980, 46392191,
    46413431, 46427424, 46433637, 67412559, 81281157,
}

# Companies to exclude from the dashboard — non-trading entities (grid operators, retail utilities,
# cooperative suppliers) that ended up in the NACE 351590 scrape but are not wholesale traders.
EXCLUDED_CVR: set[int] = {
    20843187,  # nef Strøm A/S — shell for NEF Fonden foundation, 1 employee

    # Grid distribution / DSO operators (NACE 351400)
    29915458,  # Radius Elnet A/S
    20806397,  # TREFOR El-net A/S
    32268498,  # TREFOR El-net Øst A/S
    25706900,  # Dinel A/S
    21085200,  # NETSELSKABET ELVVÆRK A/S
    25988477,  # Grindsted Elnet A/S
    39659492,  # El-net Kongerslev A/S
    32654215,  # ELEKTRUS A/S
    45891887,  # NORDIC GREEN ENERGY APS
    46006674,  # Ebbefos Energihandel A/S

    # Large integrated utilities / retail electricity suppliers
    24213528,  # Andel Energi A/S — major cooperative (338 emp)
    20810440,  # EWII ENERGI A/S — regional utility cooperative
    21105848,  # JYSK ENERGI A/S — regional retail utility
    25118359,  # Norlys Energi A/S — major utility grid + retail (291 emp)
    32285759,  # NRGI Elhandel A/S — NRGI cooperative retail arm (110 emp)
    21191809,  # SCANENERGI A/S — utility retail (87 emp)
    25119207,  # SEF ENERGI A/S — Syd Energi cooperative retail
    33884788,  # MODSTRØM DANMARK A/S — consumer electricity retailer (168 emp)
    37960705,  # Velkommen A/S — consumer electricity retailer
    25322754,  # NORSK ELKRAFT DANMARK A/S — consumer retail supplier
    27210538,  # Ørsted Salg & Service A/S — Ørsted retail arm (355 emp, 93 BDKK)
    21311332,  # VATTENFALL A/S — Swedish utility, mainly production + retail (515 emp)
    35857842,  # VESTFORSYNING EL A/S — local cooperative utility
    25460472,  # STRUER ENERGI HANDEL A/S — local utility supply arm
    25794206,  # Bornholms Energi A/S — island utility
    25604849,  # ENERGI VIBORG STRØM A/S — local utility supply
    29225311,  # AARS-HORNUM EL-FORSYNING A.M.B.A. — cooperative, zero activity
    24982629,  # HJERTING ELFORSYNING ApS — local cooperative supply
    44263637,  # Fair Strøm ApS — consumer supplier, no wholesale trading

    # Borderline utility / non-trading (user confirmed exclude)
    25567714,  # AURA Elhandel A/S — AURA utility group wholesale arm
    35521720,  # Entelios ApS — automated VPP/aggregator, not a trader (2 emp, 1.5B rev)
    36898895,  # Holdingselskabet af 6. maj 2015 A/S — holding company
    25481941,  # VERDO GO GREEN A/S — Verdo utility renewable arm
    25113284,  # Elhandelsselskabet af 1. januar 2020 A/S — utility subsidiary
    29685746,  # Ø/strøm A/S — consumer-facing retail brand, 0 employees
    37783838,  # Cheap Energy Danmark ApS — retail consumer supplier
    34079145,  # NE CLIMATE A/S — climate/utility company, not a trader
    38732684,  # Strømlinet A/S — retail electricity supplier (33 emp)
    33268645,  # ENERGI TEAM ApS — retail energy supplier
    42970239,  # TS Vest A/S — local utility supply
    43565729,  # Trasteel Nordic ApS — steel/commodities, not electricity trader
    35412220,  # Peak Vision A/S — not a trader
    42133434,  # ENLY A/S — retail energy supplier (23 emp)
    43526146,  # Reel Energy ApS — not a trader

    # Yggdrasil sub-entity shells — all activity consolidated into Yggdrasil Commodities ApS (40300643)
    40420347,  # Dvalin ApS — shell, zero revenue/employees across all years
    40450238,  # Nidhog ApS — was briefly active to 2022, then folded into Yggdrasil
    40879196,  # Asgard ApS — shell, zero revenue/employees across all years
    39577445,  # Thordin ApS — shell, zero revenue/employees across all years
    46480473,  # Mjolnir Trading ApS — founded 2026, 100% Yggdrasil, same directors
    42439258,  # KØRNFULL ENERGI ApS — not a trader
    44747987,  # BioCirc Carbon & Renewables ApS — carbon/renewables, not electricity trader
    34221162,  # NETTOPOWER ApS — not a trader
    41718684,  # S.C. Nordic A/S — not a trader

    # Personal names / sole proprietorships — individuals who registered as electricity sellers
    10042585,  # KAMMA KRAGELUND — personal name
    81281157,  # Keld Erik Povlsen — personal name
    67412559,  # Mette Marie Bertel Christensen — personal name
    12352204,  # susanne stærmose — personal name
    28271646,  # Bent Gad Thysen — personal name
    30871359,  # Anni V. Nielsen — personal name
    38487345,  # Jakob Tange — personal name
    37392197,  # Jan Bangsgaard — personal name
    40587985,  # Jens Henrik Thøgersen — personal name
    38030590,  # Jesper Holm Kyndesen — personal name
    37195030,  # Jesper Juulsgaard Mathiasen — personal name
    35786236,  # Martin Wøbbe Christensen — personal name
    37060127,  # Steen Sørensen — personal name
    32814751,  # Tage Guldbæk — personal name
    37983403,  # Helle og Morten Levring — personal names
    12406010,  # HELGE LYDERSEN — personal name
    26789575,  # L M Service v/Lars Nørgaard Madsen — sole proprietorship, no trading
    12257899,  # Nykobbel v/Ulrik Dahl — sole proprietorship
    41598441,  # YourBookkeeper v/Nicki Nielsen — bookkeeping firm, not a trader

    # Windmill cooperatives / micro renewable producers — sell turbine output, not traders
    37565318,  # Brund Møllen I/S — single wind turbine cooperative
    32989756,  # LM Mølle I/S — wind mill cooperative
    37001465,  # HS Vejlund Mølle I/S — wind mill cooperative
    36273240,  # Løkkemarken I/S — wind cooperative
    37037176,  # Rosenlund møllelaug — windmill community
    10990076,  # TULSTRUP NØRREGÅRD I/S — farm/rural cooperative
    21750875,  # PS PowerWind — micro wind producer
    45539865,  # Andelsselskabet Bulen — small energy cooperative
    44510758,  # Ærø Borgerenergifællesskab A.M.B.A. — citizen energy community (island)

    # Foreign-registered shells with Danish branch or holding registration only
    10238668,  # Vattenfall Energy Trading GmbH — German GmbH entity, not the Danish trading arm
    44879344,  # STG Switzerland GmbH — Swiss holding (activity in STG Denmark 43932454)
    46252098,  # SSW Energy Trading GmbH — German entity, no Danish activity
    45319903,  # LichtBlick SE — German consumer energy retailer, no wholesale trading in DK

    # Non-trading / holding / dormant with zero revenue across all years
    33575122,  # BRAGENHOLT HOLDING ApS — holding company, 14 years zero revenue
    34500568,  # Global Commodities invest — investment vehicle, no trading activity
    35781234,  # Center for bæredygtig produktion — sustainability NGO/centre
    45786943,  # Branthiq ApS — NACE 731110 (marketing/consulting), not a trader
    30105583,  # Peter Aspe — no financial filings despite 4 employees listed
    41296798,  # BO-EL — zero activity, no filings

    # Holding / investment companies — not electricity market traders
    26891272,  # JB HOLDING, LEMVIG ApS — named holding company, 1 emp, 3.8M over 13 yrs
    32302157,  # Bodal Family Invest ApS — family investment holding, not a trader

    # Wind / solar production (K/S limited partnerships or production companies)
    # These sell their own generation output — they do not trade the wholesale market
    36944854,  # K/S Vindpark Dæstrup Vest Laug — wind park limited partnership
    29312834,  # WIND 22 ApS — wind energy production seller
    28130465,  # WIND DK 1012 ApS — wind energy production seller
    43965174,  # Mermaid Solar Net K/S — solar K/S limited partnership
    39067366,  # ERESI Solar ApS — solar production company, not a market trader

    # Battery storage — sell stored capacity, not electricity market traders
    45585018,  # Lifetime Power Solutions ApS — battery / power solutions
    45204863,  # Imbro Zero2 - Battery Storage Næstved II K/S — battery storage K/S
    44436531,  # IMBRO - ZERO2 ENERGY HUB II P/S — Imbro battery storage group entity
    45511820,  # MC Batteries ApS — battery storage

    # EV charging / non-electricity-trading technology
    44240351,  # Fastned Denmark ApS — Dutch EV fast-charging company
    44996766,  # Smart eMotion ApS — EV-related services, not a power market trader

    # Consumer-facing retail / cooperative brands — no wholesale market activity
    36031204,  # go'energi A/S — consumer electricity supplier, not a wholesale trader
    37336505,  # Lokal Energi ApS — consumer retail brand, 9 years of zero revenue
    39905795,  # Dansk Strøm ApS — consumer retail brand, 7 years of zero revenue
    43983652,  # Vest Energi ApS — retail electricity supplier, ceased operations 2026
    39962764,  # Energynordic Aps — retail electricity supplier to households/SMEs
    43588966,  # Energi Viborg Flex-El A/S — retail spot-price tariff supplier (Energi Viborg utility arm)
    39632977,  # Ustekveikja Energi ApS — Norwegian regional utility subsidiary, not a proprietary trader
    45578208,  # Strømklubben A.m.b.a. — consumer electricity cooperative
    43265709,  # Greenely ApS — household energy management app, consumer-facing tech

    # Technology / services / demand-response / battery storage — not wholesale electricity traders
    40541365,  # Dansk Overskudsenergi A/S — cleantech/energy-storage startup (Cel2 product), not a trader
    44944723,  # SmartRegulering ApS — smart demand-response/aggregator, not wholesale trading
    44718243,  # SystemEnergy ApS — 0 revenue, no web presence, unverifiable
    44988429,  # EcoRise ApS — grid balancing startup in Næstved, not a spot-market trader
    44921227,  # Safe-RES ApS — battery park operator in Bjert village, not a trader
    45357961,  # System Energi ApS — vague energy holding in Give (rural mid-Jutland), not a trader
    44937492,  # TT 1 DK ApS — numbered project vehicle in Skævinge village, not a trader
    46141032,  # Zoega Ras 2026 ApS — shelf vehicle in Haslev (South Zealand), no substance
    44606879,  # ZE — foreign entity registered c/o Skattestyrelsen in Haderslev, no Danish office

    # Consumer energy broker / advisory — negotiate prices for end-consumers, not wholesale traders
    32469469,  # SAMENERGI ApS — consumer energy broker/advisor (1 emp, 0.4M over 13 yrs)

    # Retail electricity supplier shells — registered under energihandel but sell to households/SMEs
    38732056,  # ForskEl El A/S — retail supplier (Cornerstone Capital / Strømlinet group)
    41517301,  # Blue Grid ApS — green energy asset management/installation, not wholesale trading

    # Carbon / gas / non-electricity sectors
    45604489,  # Nordic Carbon Renewables A/S — carbon credits, not electricity trading
    46097866,  # b.energy gas ApS — gas sector, not electricity trading

    # Utility / CHP production / statutory retail supply — sell own generation or serve regulated customers
    37271632,  # Aalborg Decentrale Værker A/S — CHP/heat+power producer near Aalborg, not a market trader
    25664132,  # ENERGI FYN HANDEL A/S — statutory default-supply (forsyningspligt) arm of Energi Fyn
    43983563,  # NOJCH Energi ApS — EV charging focus in Silkeborg, not a wholesale trader

    # Solar / PPA project companies — develop/own assets, not active spot-market traders
    43021494,  # Green Solar ApS — solar panel installer in Nibe (rural), NACE 432100 confirms it
    44973987,  # Plexar ApS — engineering/project SPV c/o Copenhagen Infrastructure Partners

    # Compenso Energy Balance SPV series — balance-responsible vehicles for renewable portfolios in Greve suburb
    # Balance 3 and 5 confirmed at Bag Kirken 16, 2670 Greve; whole numbered series at same suburban address
    44908476,  # Compenso Energy Balance ApS
    44764261,  # Compenso Energy Balance 2 ApS
    44913844,  # Compenso Energy Balance 3 ApS — Greve (residential suburb), SPV series
    44913895,  # Compenso Energy Balance 4 ApS
    44913887,  # Compenso Energy Balance 5 ApS — Greve, confirmed same address as #3
    44913852,  # Compenso Energy Balance 6 ApS

    # Geographic outliers — confirmed in small towns with no wholesale trading hub presence
    46192656,  # Nidaros Energy ApS — Viborg (c/o private address), no substance
    46427424,  # Alpha Altra ApS — Silkeborg (provincial), no revenue
    46372980,  # Valanord ApS — Hinnerup (village, residential area near Aarhus)
    44937395,  # Tectera ApS — Skævinge (tiny village), parent of excluded TT 1 DK ApS
    46433637,  # TV47 ApS — Havdrup (tiny village south of Copenhagen)
    45794598,  # KEDO Energy ApS — Sæby (small coastal town near Frederikshavn)
    45927393,  # Asset Trading ApS — Skanderborg (small town, not a trading hub), 0 revenue
    45529908,  # PK Trade ApS — Hasselager (industrial village south of Aarhus), 0 revenue
    45277844,  # Next Gen Energy ApS — Vanløse (residential Copenhagen suburb), 0 revenue
    44401479,  # Energy Sydsjælland 1 ApS — Ballerup, machinery wholesale secondary; numbered project vehicle
    44352214,  # Moving Energy ApS — Roskilde (residential address), outside trading hubs
    45463834,  # Ecliptic Energy A/S — Viborg (provincial town), 0 revenue
    45165426,  # MAAF Energy ApS — Fjerritslev (rural North Jutland), 0 filings
    44618524,  # Selandia Energy ApS — Køge (provincial), project-development language not trading
    41927321,  # Power Trading Management A/S — Kolding, management vehicle, 0 revenue over 4 years
    41495235,  # Edison EL ApS — Værløse (residential suburb NW Copenhagen), retail/installation roots
    44667770,  # Nordic Power Quantities ApS — Børkop (village between Vejle/Fredericia), rural residential
    43608924,  # NEH US ApS — foreign-market subsidiary of Nordic Energy House, 0 DK revenue
    43928074,  # NC Energy Solutions ApS — Værløse suburb, purpose is O&M/leasing of energy infrastructure
    44993821,  # Gogreentech A/S — green technology company, not a power market trader
    45896390,  # Trendlein Technologies ApS — technology company, not a power market trader

    # Unrelated or dormant shells with no meaningful energy trading activity
    40897372,  # Magpies — no filings, not energy related
    26598613,  # Joubit ApS — no wholesale trading evidence; likely retail reseller (2 emp, 3.8M over 13 yrs)
    40739823,  # THN ENERGY DENMARK APS — reclassified to timber/building materials; no electricity trading
    43187775,  # EETC ApS — no web presence, no market registration; 3 yrs, 0.2M
    44351218,  # Energy Group ApS — holding/micro entity, no confirmed wholesale trading activity
    44797534,  # SMTM Energy ApS — shell / very early stage, 0 revenue, no evidence
    44866587,  # Green Energy Park 1 ApS — renewable generation project SPV, not a trader
    45112888,  # Divus Nova Finance ApS — finance holding, 0 filings
    46217373,  # Nordic Energy House US LLC — US LLC of Nordic Energy House, 0 DK filings
    43783149,  # El og Energi - Egtved Alle 3-5, Kolding ApS — local utility entity, not a trader
    21973505,  # VINDENERGI DANMARK AMBA — wind-owner cooperative, not a trader
    25115627,  # Ikast El Net A/S — grid distribution (DSO)
    22188410,  # Aal El-Net A M B A — grid distribution (DSO)
}

# 2026 start-ups registered as electricity traders (NACE 351590) with no public
# evidence of wholesale trading yet. Not tracked; every --fetch run reports
# when one files its first annual report so it can be classified from that.
WATCHLIST_CVR: dict[int, str] = {
    46864964: "AARK Energy ApS",          # Åbyhøj, owner SFTB Holding ApS
    46746724: "Key Commodities ApS",      # Tilst, owner M&H Holding ApS
    46676556: "VerdeX Energy ApS",        # Åbyhøj, four holding-company owners
    46549724: "Idun Energy ApS",          # Højbjerg
    46721799: "Scandinavian Energy ApS",  # Lystrup
    46611365: "Quant Partners ApS",       # Herlev, probably not wholesale
}

# Pull all pinned firms regardless of NACE code
EXTRA_CVR: set[int] = (INTRADAY_CVR | EXPLICIT_CVR | US_TRADING_CVR | HEDGE_FUND_CVR | NACE_351590_CVR) - EXCLUDED_CVR

# Manual patches for financials where the XBRL filing is incomplete/missing data.
# Applied as SQL UPDATEs after every pipeline run so they survive refreshes.
# Key: (cvr, "YYYY-MM-DD")  Value: {column: value}
MANUAL_PATCHES: dict[tuple[int, str], dict[str, object]] = {
    # STG (Denmark) ApS 2024 — XBRL only tagged 4 zero fields; full figures from PDF annual report.
    # Revenue = sub-manager fee from affiliates; personnel = 7 employees paid by Danish entity.
    (43932454, "2024-12-31"): {
        "omsaetning":           34_256_197,
        "personaleomkostninger": 41_856_813,
        "afskrivninger":            27_088,
        "ebit":                -14_480_525,
        "fin_indt":                114_922,
        "fin_udg":                 335_405,
        "aarsresultat":        -14_699_518,
        "egenkapital":         -68_541_526,
        "aktiver":              41_509_746,
        "ansatte_regnskab":              7,
    },
    # Nitor Energy A/S 2022 — XBRL has multi-entity contexts; pipeline picked up subsidiary
    # data (c435: revenue=0, profit=45K, equity=28M) instead of main entity (c1: 197B rev, 2.2B profit).
    # All values from c1/c7 contexts in the XBRL filing; 31 avg employees confirmed in c1.
    (38680781, "2022-12-31"): {
        "omsaetning":      197_218_077_000,
        "aarsresultat":      2_245_250_000,
        "egenkapital":       1_856_675_000,
        "aktiver":           3_314_905_000,
        "ansatte_regnskab":              31,
    },
    # In Commodities A/S 2023 — XBRL only has cash-flow structural items, no P&L or B/S figures.
    # All figures from PDF annual report (tEUR), converted at EUR/DKK = 7.454 (Dec 31, 2023 rate).
    # 159 avg employees (Note 4); revenue = fair-value adjustments on trading portfolio.
    (38381954, "2023-12-31"): {
        "omsaetning":          1_197_583_000,
        "personaleomkostninger":  148_528_000,
        "afskrivninger":            3_183_000,
        "ebit":                   984_301_000,
        "fin_indt":               137_869_000,
        "fin_udg":                168_976_000,
        "skat":                   225_551_000,
        "aarsresultat":           727_645_000,
        "egenkapital":          4_443_397_000,
        "aktiver":              5_219_865_000,
        "udbytte":              1_490_800_000,
        "ansatte_regnskab":               159,
    },
    # MFT Energy 1 ApS — dividends implied from equity movement (eq_start + profit − eq_end).
    # XBRL filing contains a small sub-entity context with ExtraordinaryDividendPaid tagged,
    # but the figures in our DB correspond to the larger parent context (no dividend tag there).
    # 2022: 232,326,780 + 384,369,040 − 542,125,660 = 74,570,160
    # 2023: 542,125,660 + 153,273,160 − 344,778,820 = 350,620,000
    (40213066, "2022-12-31"): {
        "udbytte": 74_570_160,
    },
    (40213066, "2023-12-31"): {
        "udbytte": 350_620_000,
    },
    # ENZOL Energy Trading ApS — XBRL stub filed blank; all figures from PDF annual report.
    # Period: 14 Jul 2024 – 31 Dec 2025 (18 months). §32 gross presentation; no depreciation.
    # Parent: Hanne Hau Capital Holdings ApS. Equity injected: 500K founding + 4.3M increase = 4.8M.
    (44967782, "2025-12-31"): {
        "bruttoresultat":        46_632_641,
        "personaleomkostninger":  2_181_821,
        "ebit":                  44_450_820,
        "fin_indt":                 152_831,
        "fin_udg":                  173_204,
        "ebt":                   44_430_447,
        "skat":                   9_775_141,
        "aarsresultat":          34_655_306,
        "egenkapital":           39_455_306,
        "aktiver":               50_943_109,
        "kassebeholdning":       34_499_521,
        "omloebsaktiver":        48_309_572,
        "ansatte_regnskab":               2,
    },
    # Quantum Power Trading ApS — XBRL stub filed blank; all figures from PDF annual report.
    # Period: 26 Jan 2024 – 30 Jun 2025 (17 months). Revenue not disclosed (§32 gross presentation).
    # Opening equity (egenkapital primo): 0 at incorporation + 40k at founding + 500k capital increase = 540k total deployed.
    (44590832, "2025-06-30"): {
        "bruttoresultat":        38_395_901,
        "personaleomkostninger":  5_184_880,
        "ebit":                  33_211_021,
        "fin_indt":                  31_848,
        "fin_udg":                2_020_454,
        "ebt":                   31_222_415,
        "skat":                   6_907_780,
        "aarsresultat":          24_314_635,
        "egenkapital_primo":            540_000,
        "udbytte":                3_000_000,
        "egenkapital":           24_854_635,
        "aktiver":               42_122_580,
        "kassebeholdning":        2_531_030,
        "omloebsaktiver":        41_988_292,
        "ansatte_regnskab":               4,
    },
}

# XBRL concept priority lists — first match wins for each metric
# Both IFRS (ifrs-full) and Danish GAAP (fsa) taxonomies covered
_REVENUE = [
    "fsa:Revenue",
    "ifrs-full:Revenue",
    "fsa:GrossProfitLoss",              # Danish GAAP: gross profit = net trading margin for traders
    "ifrs-full:TradingIncomeExpense",   # IFRS: energy traders often report margin here
    "fsa:GrossProfit",
    "ifrs-full:GrossProfit",
]
_EBIT = [
    "fsa:ProfitLossFromOrdinaryOperatingActivities",  # Danish GAAP EBIT
    "fsa:OperatingIncome",
    "ifrs-full:ProfitLossFromOperatingActivities",
    "ifrs-full:OperatingIncome",
    "fsa:ProfitLossBeforeFinancialItems",
]
_NET_PROFIT = [
    "fsa:ProfitLoss",
    "ifrs-full:ProfitLoss",
]
_EQUITY = [
    "fsa:Equity",
    "ifrs-full:Equity",
]
_ASSETS = [
    "fsa:Assets",
    "fsa:LiabilitiesAndEquity",
    "ifrs-full:Assets",
    "ifrs-full:EquityAndLiabilities",
]
_CURRENT_LIAB = [
    "fsa:ShorttermLiabilitiesOtherThanProvisions",  # Danish GAAP
    "fsa:LiabilitiesCurrent",
    "ifrs-full:CurrentLiabilities",
]
_NONCURRENT_LIAB = [
    "fsa:LongtermLiabilitiesOtherThanProvisions",   # Danish GAAP
    "fsa:LiabilitiesNoncurrent",
    "ifrs-full:NoncurrentLiabilities",
]
_RELATED_PARTY_DEBT_LT = [
    "fsa:LongtermPayablesToGroupEnterprises",           # gæld til tilknyttede virksomheder (langfristet)
    "fsa:LongtermPayablesToShareholdersAndManagement",  # gæld til kapitalejere/ledelse (langfristet)
    "ifrs-full:NoncurrentPayablesToRelatedParties",    # IFRS equivalent, so both GAAPs count alike
]
_RELATED_PARTY_DEBT_ST = [
    "fsa:ShorttermPayablesToGroupEnterprises",          # gæld til tilknyttede virksomheder (kortfristet)
    "fsa:ShorttermPayablesToShareholdersAndManagement", # gæld til kapitalejere/ledelse (kortfristet)
    "ifrs-full:TradeAndOtherCurrentPayablesToRelatedParties",  # IFRS equivalent
]
_EBT = [
    "fsa:ProfitLossFromOrdinaryActivitiesBeforeTax",   # Danish GAAP pre-tax profit
    "ifrs-full:ProfitLossBeforeTax",                   # IFRS pre-tax profit
    "fsa:ProfitLossBeforeIncomeTaxes",
]
_CASH = [
    "fsa:CashAndCashEquivalents",                      # Danish GAAP
    "ifrs-full:CashAndCashEquivalents",
    "ifrs-full:Cash",
]
_CURRENT_ASSETS = [
    "fsa:CurrentAssets",                               # Danish GAAP
    "ifrs-full:CurrentAssets",
]
_EMPLOYEES = [
    "fsa:AverageNumberOfEmployees",
    "ifrs-full:NumberOfEmployees",
]
_PERSONNEL_COSTS = [
    "fsa:EmployeeExpenses",              # Danish GAAP: Personaleomkostninger
    "fsa:EmployeeBenefitsExpense",       # Danish GAAP alternate (e.g. Qube)
    "fsa:PersonnelCosts",
    "fsa:WagesAndSalaries",
    "ifrs-full:EmployeeBenefitsExpense", # IFRS equivalent
    "ifrs-full:WagesAndSalaries",
]
_GROSS_PROFIT = [
    # Bruttoresultat — only present when companies use gross P&L presentation
    # (revenue minus COGS). Net-margin presenters (most traders) won't have this.
    "fsa:GrossProfitLoss",
    "fsa:GrossProfit",
    "ifrs-full:GrossProfit",
]
_COST_OF_GOODS = [
    "fsa:CostOfSales",
    "fsa:RawMaterialsAndConsumablesUsed",
    "fsa:DirectCostsOfSales",
    "ifrs-full:CostOfSales",
    "ifrs-full:RawMaterialsAndConsumablesUsed",
]
_DEPRECIATION = [
    "fsa:DepreciationAmortisationImpairmentLossesReversalsAndFairValueAdjustments",
    "fsa:AmortisationDepreciationAndImpairmentLossesOfPropertyPlantAndEquipmentAndIntangibleAssets",
    "fsa:DepreciationAmortisationExpenseAndImpairmentLossesOfPropertyPlantAndEquipmentAndIntangibleAssetsRecognisedInProfitOrLoss",
    "ifrs-full:DepreciationAmortisationAndImpairmentLoss",
    "ifrs-full:DepreciationAndAmortisationExpense",
]
_FIN_INCOME = [
    "fsa:FinancialIncome",
    "fsa:OtherFinanceIncome",           # FSA alternate used by most Danish GAAP energy traders
    "ifrs-full:FinanceIncome",
    "ifrs-full:InterestIncome",
]
_FIN_EXPENSES = [
    "fsa:FinancialExpenses",
    "fsa:OtherFinanceExpenses",         # FSA alternate used by most Danish GAAP energy traders
    "ifrs-full:FinanceCosts",
    "ifrs-full:InterestExpense",
]
_TAX = [
    "fsa:TaxExpense",
    "fsa:IncomeTaxExpense",
    "ifrs-full:IncomeTaxExpense",
]
_DIVIDENDS = [
    "fsa:ProposedDividendRecognisedInEquity",
    "fsa:ProposedExtraordinaryDividendRecognisedInEquity",
    "fsa:DividendRecognisedInEquity",
    "fsa:ExtraordinaryDividendPaid",
    "fsa:DividendsPaidClassifiedAsFinancingActivities",        # FSA cash-flow statement dividend concept
    "ifrs-full:DividendsPaidOrdinaryShares",
    "ifrs-full:DividendsPaid",
    "ifrs-full:DividendsPaidClassifiedAsFinancingActivities",  # IFRS cash-flow statement (e.g. Danske Commodities 11B in 2023)
    "ifrs-full:DividendsPaidToEquityHoldersOfParentClassifiedAsFinancingActivities",  # IFRS when broken out by holder type
]

# Balance-sheet detail for working capital and invested capital. Concept
# names are the ones the tracked companies actually tag (scan of their latest
# filings, Oct 2026). A line a filer omits is zero in practice: Danish
# filings do not tag empty balance-sheet lines.
_NONCURRENT_ASSETS = [
    "fsa:NoncurrentAssets",
    "ifrs-full:NoncurrentAssets",
]
_TOTAL_LIABILITIES = [
    "fsa:LiabilitiesOtherThanProvisions",   # gældsforpligtelser i alt
    "ifrs-full:Liabilities",                # IFRS: includes provisions
]
_PROVISIONS = [
    "fsa:Provisions",                       # hensatte forpligtelser i alt
    "ifrs-full:Provisions",
]
_RECEIVABLES = [
    "fsa:ShorttermReceivables",             # tilgodehavender i alt
    "ifrs-full:TradeAndOtherCurrentReceivables",
    "ifrs-full:CurrentTradeReceivables",
]
_RELATED_PARTY_RECEIVABLES_ST = [           # summed: disjoint lines
    "fsa:ShorttermReceivablesFromGroupEnterprises",
    "ifrs-full:TradeAndOtherCurrentReceivablesDueFromRelatedParties",
]
_INVENTORIES = [
    "fsa:Inventories",
    "ifrs-full:Inventories",
]
# Lines that are summed are listed as groups: (total, *parts). A group
# counts its total when the filing tags it, else the sum of its parts, so a
# filer that tags both a subtotal and its breakdown is not counted twice
# (seen: ShorttermInvestments = OtherShorttermInvestments in 4 filings).
_CURRENT_SECURITIES: list[tuple[str, ...]] = [
    ("fsa:ShorttermInvestments", "fsa:OtherShorttermInvestments"),  # værdipapirer og kapitalandele
    ("ifrs-full:CurrentInvestments",),
    ("ifrs-full:ShorttermDepositsNotClassifiedAsCashEquivalents",),
]
_TRADE_PAYABLES = [
    "fsa:ShorttermTradePayables",           # leverandører af varer og tjenesteydelser
    "ifrs-full:TradeAndOtherCurrentPayablesToTradeSuppliers",
    "ifrs-full:CurrentTradePayables",
]
# Interest-bearing debt to third parties (related-party loans are separate).
# Lease liabilities count under IFRS 16 — tagged as lines of their own next
# to borrowings (different values in all 9 filings tagging both); Danish-GAAP
# lease *commitments* are off-balance notes and do not.
_BANK_DEBT_ST: list[tuple[str, ...]] = [
    ("fsa:ShorttermDebtToCreditInstitutions",
     "fsa:ShorttermDebtToBanks", "fsa:ShorttermDebtToOtherCreditInstitutions"),
    ("fsa:ShorttermMortgageDebt",),
    # Kortfristet del af langfristet gæld: the current portion of bank and
    # mortgage loans, the fsa counterpart of CurrentPortionOfLongtermBorrowings.
    ("fsa:ShorttermPartOfLongtermLiabilitiesOtherThanProvisions",),
    ("ifrs-full:ShorttermBorrowings",),
    ("ifrs-full:CurrentPortionOfLongtermBorrowings",),
    ("ifrs-full:CurrentLeaseLiabilities",),
]
_BANK_DEBT_LT: list[tuple[str, ...]] = [
    ("fsa:LongtermDebtToCreditInstitutions",
     "fsa:LongtermDebtToBanks", "fsa:LongtermDebtToOtherCreditInstitutions"),
    ("fsa:LongtermMortgageDebt",),
    ("ifrs-full:LongtermBorrowings",),
    ("ifrs-full:NoncurrentLeaseLiabilities",),
]
_DERIVATIVE_ASSETS = [                      # summed: current + non-current
    "fsa:DerivativeFinancialInstrumentsShorttermAssets",
    "fsa:DerivativeFinancialInstrumentsLongtermAssets",
    "ifrs-full:CurrentDerivativeFinancialAssets",
    "ifrs-full:NoncurrentDerivativeFinancialAssets",
]
_DERIVATIVE_LIABILITIES = [                 # summed: current + non-current
    "fsa:ShortermDerivativeFinancialInstrumentsLiabilities",  # sic: the taxonomy's spelling
    "fsa:DerivativeFinancialInstrumentsShorttermLiabilities",
    "fsa:DerivativeFinancialInstrumentsLongtermLiabilities",
    "ifrs-full:CurrentDerivativeFinancialLiabilities",
    "ifrs-full:NoncurrentDerivativeFinancialLiabilities",
]

# Namespace -> concept prefix mapping for standard XBRL element tag resolution
_NS_PREFIX: dict[str, str] = {
    "http://xbrl.dcca.dk/fsa": "fsa",
    "http://xbrl.dcca.dk/fsb": "fsb",
    "http://xbrl.dcca.dk/mrv": "mrv",
    "http://xbrl.dcca.dk/gsd": "gsd",
    "http://xbrl.dcca.dk/cmn": "cmn",
}
# All IFRS taxonomy versions share the same prefix
for _ifrs_year in ["2024-03-27", "2023-03-23", "2022-03-24", "2021-03-24", "2020-03-16"]:
    _NS_PREFIX[f"https://xbrl.ifrs.org/taxonomy/{_ifrs_year}/ifrs-full"] = "ifrs-full"


def _elem_concept(tag: str) -> str:
    """Convert {namespace}LocalName element tag to prefix:LocalName concept string."""
    if "{" not in tag:
        return tag
    ns, local = tag[1:].split("}", 1)
    prefix = _NS_PREFIX.get(ns)
    if prefix:
        return f"{prefix}:{local}"
    # Fallback: last path segment of namespace URL as prefix
    return f"{ns.rstrip('/').split('/')[-1]}:{local}"

# ── API client ─────────────────────────────────────────────────────────────────

class _CvrDevDown(Exception):
    pass


_cvrapi_fallback: bool = False
APICVR_BASE = "https://apicvr.dk/api/v1"


def _headers() -> dict[str, str]:
    key = os.environ.get("CVR_DEV_API_KEY", "")
    if not key:
        sys.exit("Set CVR_DEV_API_KEY in .env")
    return {"Authorization": f"Bearer {key}"}


def _get(path: str, params: dict | None = None):
    global _cvrapi_fallback
    if _cvrapi_fallback:
        raise _CvrDevDown()
    r = httpx.get(f"{API_BASE}/{path}", headers=_headers(), params=params, timeout=30)
    if r.status_code == 402:
        _cvrapi_fallback = True
        print("  cvr.dev subscription inactive — falling back to apicvr.dk")
        raise _CvrDevDown()
    r.raise_for_status()
    return r.json() if r.content else []


def _normalize_apicvr(data: dict) -> dict:
    """Wrap apicvr.dk response into the virk-style nested structure the pipeline expects."""
    status = "NORMAL" if not data.get("enddate") and not data.get("bankrupt") else "OPHØRT"
    return {
        "cvrNummer": data.get("vat"),
        "virksomhedMetadata": {
            "sammensatStatus": status,
            "stiftelsesDato": data.get("startdate"),  # already ISO YYYY-MM-DD
            "nyesteNavn": {"navn": data.get("name") or ""},
            "nyesteHovedbranche": {
                "branchekode": str(data.get("industrycode") or ""),
                "branchetekst": data.get("industrydesc") or "",
            },
            "nyesteAarsbeskaeftigelse": {
                "antalAnsatte": data.get("employees"),
                "antalAarsvaerk": None,
            },
            "nyesteBeliggenhedsadresse": {
                "vejnavn": data.get("address") or "",
                "postnummer": data.get("zipcode"),
                "postdistrikt": data.get("city") or "",
            },
        },
        "erstMaanedsbeskaeftigelse": [],
    }


def _get_apicvr(path: str) -> list[dict]:
    """Fetch from apicvr.dk, return a normalized list of companies."""
    r = httpx.get(f"{APICVR_BASE}/{path}", timeout=30)
    if r.status_code == 404:
        return []
    r.raise_for_status()
    data = r.json() if r.content else None
    if not data:
        return []
    if isinstance(data, list):
        return [_normalize_apicvr(c) for c in data]
    return [_normalize_apicvr(data)]


# ── Company discovery ──────────────────────────────────────────────────────────

def _nace(c: dict) -> str:
    return str(
        c.get("virksomhedMetadata", {}).get("nyesteHovedbranche", {}).get("branchekode") or ""
    )


def discover_companies() -> list[dict]:
    """Search by keyword, return unique active companies matching TARGET_NACE."""
    seen: set[int] = set()
    results: list[dict] = []

    for term in SEARCH_TERMS:
        try:
            batch = _get("api/cvr/virksomhed", {"navn": term})
        except _CvrDevDown:
            batch = _get_apicvr(f"search/company/{term}")
        except httpx.HTTPStatusError:
            continue
        if not isinstance(batch, list):
            batch = [batch] if batch else []

        for c in batch:
            cvr = c.get("cvrNummer")
            if not cvr or cvr in seen or cvr in EXCLUDED_CVR:
                continue
            meta = c.get("virksomhedMetadata", {})
            if meta.get("sammensatStatus") != "NORMAL":
                continue
            if _nace(c) not in TARGET_NACE:
                continue
            seen.add(cvr)
            results.append(c)

    # Pull extra CVR numbers the user hardcoded
    for cvr in EXTRA_CVR:
        if cvr in seen:
            continue
        try:
            batch = _get("api/cvr/virksomhed", {"cvr_nummer": cvr})
            if batch and isinstance(batch, list):
                results.append(batch[0])
                seen.add(cvr)
        except _CvrDevDown:
            apicvr = _get_apicvr(str(cvr))
            if apicvr:
                results.append(apicvr[0])
                seen.add(cvr)
        except httpx.HTTPStatusError:
            pass

    return results


# ── Company metadata extraction ────────────────────────────────────────────────

def _g(d, *keys, default=None):
    for k in keys:
        if not isinstance(d, dict):
            return default
        d = d.get(k, default)
    return d


def extract_company_row(c: dict) -> dict:
    meta = c.get("virksomhedMetadata", {})
    start = meta.get("stiftelsesDato")
    alder: int | None = None
    if start:
        try:
            alder = (date.today() - datetime.fromisoformat(start).date()).days // 365
        except (ValueError, TypeError):
            pass
    ansatte = _g(meta, "nyesteAarsbeskaeftigelse", "antalAnsatte") or _g(
        meta, "nyesteAarsbeskaeftigelse", "antalAarsvaerk"
    )
    cvr = c.get("cvrNummer")
    addr = meta.get("nyesteBeliggenhedsadresse") or {}
    vejnavn = addr.get("vejnavn") or ""
    husnr   = addr.get("husnummerFra")
    bogstav = addr.get("bogstavFra") or ""
    etage   = addr.get("etage") or ""
    postnr  = addr.get("postnummer")
    postby  = addr.get("postdistrikt") or ""
    nr_str  = f"{husnr}{bogstav}" if husnr else ""
    adresse_str = vejnavn
    if nr_str:
        adresse_str += f" {nr_str}"
    if etage:
        adresse_str += f", {etage}."
    if postnr:
        adresse_str += f", {postnr} {postby}"
    return {
        "cvr": cvr,
        "navn": _g(meta, "nyesteNavn", "navn", default=""),
        "status": meta.get("sammensatStatus", ""),
        "stiftelsesdato": start,
        "alder_aar": alder,
        "branche_kode": _g(meta, "nyesteHovedbranche", "branchekode", default=""),
        "branche_tekst": _g(meta, "nyesteHovedbranche", "branchetekst", default=""),
        "ansatte": int(ansatte) if ansatte is not None else None,
        "is_intraday": cvr in INTRADAY_CVR,
        "is_multidesk": cvr in EXPLICIT_CVR,
        "is_us_trading": cvr in US_TRADING_CVR,
        "is_hedgefund": cvr in HEDGE_FUND_CVR,
        "adresse": adresse_str.strip() if vejnavn else None,
        "postnr": str(postnr) if postnr else None,
        "postby": postby or None,
    }


def extract_employee_monthly(c: dict) -> list[dict]:
    """Return monthly headcount records from erstMaanedsbeskaeftigelse."""
    cvr = c.get("cvrNummer")
    rows: list[dict] = []
    for rec in c.get("erstMaanedsbeskaeftigelse", []):
        aar = rec.get("aar")
        maaned = rec.get("maaned")
        if aar and maaned:
            rows.append({
                "cvr": cvr,
                "aar": int(aar),
                "maaned": int(maaned),
                "antal_ansatte": rec.get("antalAnsatte"),
                "antal_aarsvaerk": rec.get("antalAarsvaerk"),
            })
    return rows


# ── XBRL parsing ───────────────────────────────────────────────────────────────

_XBRLI = "http://www.xbrl.org/2003/instance"
_IX = "http://www.xbrl.org/2013/inlineXBRL"

# Fixed EUR->DKK rate used to normalise companies that report in EUR
EUR_DKK = 7.46

# The one kind of axis that does not break a total down: it only says whose
# statements a fact belongs to — the group's or the parent company's alone.
# The two taxonomies tag it the opposite way round: IFRS/ESEF leaves the
# group without a dimension and marks the parent SeparateMember, Danish GAAP
# leaves the parent without one and marks the group ConsolidatedMember. The
# group is the trading business, so it wins either way: consolidated (0),
# then no dimension (1), then parent-only (2). Lower rank wins.
_ENTITY_SCOPE_AXES = frozenset({
    "ConsolidatedAndSeparateFinancialStatementsAxis",  # ifrs-full
    "ConsolidatedSoloDimension",                       # cmn (Danish GAAP)
})
_ENTITY_SCOPE_RANK = {"ConsolidatedMember": 0, "SeparateMember": 2, "SoloMember": 2}
_NO_DIMENSION_RANK = 1
#: Balance-sheet totals every filing tags; their best rank sets the filing's scope.
_SCOPE_CONCEPTS = frozenset({"fsa:Assets", "fsa:Equity", "ifrs-full:Assets", "ifrs-full:Equity"})

#: Rank of a context carrying any other dimension: a breakdown (an equity
#: component, a segment, a five-year-summary column holding last year's
#: figure under this year's dates), used only for a concept/period with no
#: total at all (some filers tag headcount so).
DIMENSIONAL_RANK = 3


def _context_rank(ctx: ET.Element) -> int:
    """0 consolidated, 1 no dimension, 2 parent-only, DIMENSIONAL_RANK otherwise."""
    rank = _NO_DIMENSION_RANK
    for tag in (f"{{{_XBRLI}}}segment", f"{{{_XBRLI}}}scenario"):
        for container in ctx.iter(tag):
            for member in container:
                axis = member.get("dimension", "").split(":")[-1]
                if axis not in _ENTITY_SCOPE_AXES:
                    return DIMENSIONAL_RANK
                member_rank = _ENTITY_SCOPE_RANK.get((member.text or "").strip().split(":")[-1])
                if member_rank is None:
                    return DIMENSIONAL_RANK
                rank = member_rank
    return rank


def _plain_number(text: str) -> float | None:
    """Parse a canonical XBRL number ("-1234567", "1234.5"); None if it is not one."""
    try:
        value = float(text.strip())
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def _ix_number(text: str, fmt: str) -> float | None:
    """Parse a displayed inline-XBRL number according to its ixt format.

    Danish reports mostly use ixt:num-comma-decimal: "1.063.876" is a million,
    "97,6" is ninety-seven point six. Stripping only commas used to drop every
    such value of a thousand or more and keep the zeros, so whole IFRS
    filings parsed as equity 0 and profit 0.
    """
    s = text.strip().replace("\xa0", "").replace(" ", "").replace("'", "")
    if s in ("-", "–", "—"):  # ixt:fixed-zero / zerodash render nil as a dash
        return 0.0
    if not s:
        return None
    style = fmt.split(":")[-1].replace("-", "").lower()
    if style in ("numcommadecimal", "numdotcomma", "numspacecomma", "numcomma"):
        s = s.replace(".", "").replace(",", ".")
    else:  # num-dot-decimal or unformatted: commas group thousands
        s = s.replace(",", "")
    return _plain_number(s)


def _parse_xbrl_facts(
    xml_bytes: bytes, ranks_out: dict[tuple[str, str], int] | None = None
) -> dict[str, dict[str, int]]:
    """
    Return {concept_name: {period_end_date: best_value}} with all monetary
    values normalised to DKK.  EUR amounts are converted at EUR_DKK.
    Non-monetary units (shares, ratios) are ignored.

    ranks_out, if given, receives the context rank of every kept fact, keyed
    by (concept, period_end) — DIMENSIONAL_RANK marks a breakdown, not a total.
    """
    root = ET.fromstring(xml_bytes)

    # Map document-local namespace prefixes (e.g. "d:") to our standard prefixes
    # (e.g. "fsa:") so companies that use non-standard prefixes are parsed correctly.
    doc_prefix_to_std: dict[str, str] = {}
    for _, ns_data in ET.iterparse(io.BytesIO(xml_bytes), events=("start-ns",)):
        prefix, uri = ns_data  # type: ignore[misc]
        std = _NS_PREFIX.get(uri)
        if std:
            doc_prefix_to_std[prefix] = std

    def _normalize_concept(name: str) -> str:
        if ":" not in name:
            return name
        prefix, local = name.split(":", 1)
        return f"{doc_prefix_to_std.get(prefix, prefix)}:{local}"

    # Build context -> (period-end date, rank); see _context_rank.
    # A context with a dimension member is one column of the equity statement
    # (share capital, retained earnings), one segment or a five-year-summary
    # column. The XBRL Dimensions spec forbids the default member in a
    # context, so a statement total is tagged without one. Reading
    # dimensional facts let "first occurrence wins" pick e.g. retained
    # earnings as total equity, or last year's revenue from the five-year
    # summary, whenever those preceded the statements — plain-XML filings are
    # in no particular order. The one exception is the group/parent axis.
    ctx_date: dict[str, tuple[str, int]] = {}
    for ctx in root.iter(f"{{{_XBRLI}}}context"):
        ctx_id = ctx.get("id", "")
        rank = _context_rank(ctx)
        period = ctx.find(f"{{{_XBRLI}}}period")
        if period is None:
            continue
        end = period.findtext(f"{{{_XBRLI}}}endDate")
        instant = period.findtext(f"{{{_XBRLI}}}instant")
        if end or instant:
            ctx_date[ctx_id] = (end or instant or "", rank)

    # Build unit ID -> ISO currency code (e.g. "DKK", "EUR")
    unit_currency: dict[str, str] = {}
    for u in root.iter(f"{{{_XBRLI}}}unit"):
        uid = u.get("id", "")
        measure = u.findtext(f"{{{_XBRLI}}}measure", "")
        # measure looks like "iso4217:DKK" or "xbrli:shares"
        currency = measure.split(":")[-1].upper() if measure else ""
        unit_currency[uid] = currency

    facts: dict[str, dict[str, int]] = {}

    def _resolve_currency(unit: str) -> str:
        """Return ISO currency code for a unitRef value, or '' if non-monetary."""
        # Look up in parsed <unit> elements first
        ccy = unit_currency.get(unit, "")
        if ccy:
            return ccy
        # Fallback: unitRef is a direct string like "DKK", "iso4217:DKK", "EUR"
        u = unit.upper()
        if "DKK" in u:
            return "DKK"
        if "EUR" in u:
            return "EUR"
        return ""

    # Employee count concepts use xbrli:pure / xbrli:shares (non-monetary units)
    _EMPLOYEE_CONCEPTS: frozenset[str] = frozenset({
        "fsa:AverageNumberOfEmployees",
        "ifrs-full:NumberOfEmployees",
    })

    # Every candidate value, by rank, first occurrence within a rank: a
    # repeat of (concept, period, rank) is the same total shown twice (profit
    # in the income statement and the equity statement) or a period-end
    # shared by an instant and a duration context.
    candidates: dict[tuple[str, str], dict[int, int]] = {}

    def _record(name: str, ctx: str, unit: str, number: float | None, scale: int = 0) -> None:
        if not unit or number is None:
            return
        period_end, rank = ctx_date.get(ctx, ("", DIMENSIONAL_RANK))
        if not period_end:
            return
        if name in _EMPLOYEE_CONCEPTS:
            # Headcount: accept any unit (xbrli:pure, xbrli:shares, etc.), no currency conversion.
            # Scale is intentionally ignored — employee counts are raw integers; scale=3 on a
            # headcount element is an XBRL authoring error (monetary table scale applied to count).
            value = round(number)
        else:
            currency = _resolve_currency(unit)
            if currency not in ("DKK", "EUR"):
                return  # skip shares, ratios, etc.
            base_value = number * (10 ** scale)
            value = round(base_value * EUR_DKK) if currency == "EUR" else round(base_value)
        candidates.setdefault((name, period_end), {}).setdefault(rank, value)

    # --- Inline XBRL (ix:nonFraction) — used by IFRS filers and newer reports ---
    ix_count = 0
    for elem in root.iter(f"{{{_IX}}}nonFraction"):
        ix_count += 1
        scale_s = elem.get("scale", "0")
        scale = int(scale_s) if scale_s.lstrip("-").isdigit() else 0
        number = _ix_number("".join(elem.itertext()), elem.get("format", ""))
        if number is not None and elem.get("sign") == "-":
            number = -number
        _record(
            _normalize_concept(elem.get("name", "")),
            elem.get("contextRef", ""),
            elem.get("unitRef", ""),
            number,
            scale,
        )

    # --- Standard XBRL facts — used by Danish GAAP (fsa) filers ---
    if ix_count == 0:
        for elem in root.iter():
            ctx = elem.get("contextRef")
            unit = elem.get("unitRef")
            if not ctx or not unit:
                continue
            _record(_elem_concept(elem.tag), ctx, unit, _plain_number(elem.text or ""))

    # Resolve the filing's scope once — group or parent company — from the
    # best-ranked balance-sheet total, and read every concept in that scope.
    # Per concept, a Danish group filing would take group equity (tagged
    # ConsolidatedMember) next to parent-only intercompany loans (tagged
    # without a dimension, eliminated in the group): a line the chosen
    # statements do not show is absent, not borrowed from the other entity.
    scope_ranks = [
        rank
        for (name, _), by_rank in candidates.items() if name in _SCOPE_CONCEPTS
        for rank in by_rank if rank < DIMENSIONAL_RANK
    ]
    scope = min(scope_ranks) if scope_ranks else _NO_DIMENSION_RANK
    fact_rank: dict[tuple[str, str], int] = {} if ranks_out is None else ranks_out
    for (name, period_end), by_rank in candidates.items():
        entity_ranks = [r for r in by_rank if r < DIMENSIONAL_RANK]
        if scope in by_rank:
            rank = scope
        elif entity_ranks and name not in _EMPLOYEE_CONCEPTS:
            continue  # only in the other entity's statements
        else:
            # Headcount (often tagged once for the whole report) or a
            # concept with no total at all: best rank available.
            rank = min(by_rank)
        fact_rank[(name, period_end)] = rank
        facts.setdefault(name, {})[period_end] = by_rank[rank]

    return facts


def _pick_fact(facts: dict, period_end: str, *concepts: str) -> float | None:
    """Return value for the first concept with data for the given period_end."""
    for concept in concepts:
        v = facts.get(concept, {}).get(period_end)
        if v is not None:
            return float(v)
    return None


def _sum_facts(facts: dict, period_end: str, *concepts: str) -> float | None:
    """Sum values across all matching concepts for the given period_end.
    Used when a balance sheet item may be split across multiple XBRL concepts
    (e.g. group enterprise debt + shareholder loans both count toward deployed capital).
    Returns None if no concept has data."""
    total, found = 0.0, False
    for concept in concepts:
        v = facts.get(concept, {}).get(period_end)
        if v is not None:
            total += float(v)
            found = True
    return total if found else None


def _sum_groups(facts: dict, period_end: str, groups: list[tuple[str, ...]]) -> float | None:
    """Sum of line groups: each group's total if tagged, else its parts' sum.
    Returns None if no concept of any group has data."""
    total, found = 0.0, False
    for total_concept, *parts in groups:
        v = _pick_fact(facts, period_end, total_concept)
        if v is None and parts:
            v = _sum_facts(facts, period_end, *parts)
        if v is not None:
            total += v
            found = True
    return total if found else None


def _opening_sum(
    facts: dict, open_date: str | None, has_opening: bool, groups: list[tuple[str, ...]]
) -> float | None:
    """Sum of an opening balance; zero when the opening balance sheet is tagged
    but the line is not (Danish filings omit empty lines), None without one."""
    if not has_opening or open_date is None:
        return None
    return _sum_groups(facts, open_date, groups) or 0.0


VIRK_ES = "http://distribution.virk.dk/offentliggoerelser/_search"


def _get_regnskab_virk(cvr: int) -> list[dict]:
    """Fetch annual report filings from Erhvervsstyrelsen's public Elasticsearch API."""
    r = httpx.post(
        VIRK_ES,
        json={
            "query": {"term": {"cvrNummer": cvr}},
            "_source": ["dokumenter", "regnskab.regnskabsperiode"],
            "size": 50,
            "sort": [{"offentliggoerelsesTidspunkt": "desc"}],
        },
        timeout=30,
    )
    r.raise_for_status()
    return [h["_source"] for h in r.json()["hits"]["hits"]]


def check_watchlist() -> None:
    """Report WATCHLIST_CVR companies that have published an annual report."""
    for cvr, name in WATCHLIST_CVR.items():
        if cvr in EXTRA_CVR or cvr in EXCLUDED_CVR:
            continue
        try:
            filings = _get_regnskab_virk(cvr)
        except httpx.HTTPError:
            continue
        if filings:
            print(f"  Watchlist: {name} (CVR {cvr}) has filed {len(filings)} "
                  "annual report(s) - classify it (track or exclude)")


def fetch_and_parse_financials(
    cvr: int,
    skip_periods: set[str] | None = None,
    facts_out: list[dict] | None = None,
) -> list[dict]:
    """Fetch annual reports for a CVR and parse XBRL financials.

    skip_periods: period-end dates (YYYY-MM-DD) already in the DB — their
    XBRL documents are not re-downloaded, making incremental runs fast.
    facts_out: if given, receives one row per total tagged in each parsed
    filing (rows for the xbrl_facts table).
    """
    try:
        recs = _get("api/cvr/regnskab", {"cvr_nummer": cvr})
    except _CvrDevDown:
        try:
            recs = _get_regnskab_virk(cvr)
        except Exception:
            return []
    except httpx.HTTPStatusError:
        return []
    if not isinstance(recs, list):
        return []

    rows: list[dict] = []
    fetched_periods: set[str] = set()  # deduplicate within this run (amendments etc.)
    for rec in recs:
        period = (rec.get("regnskab") or {}).get("regnskabsperiode") or {}
        period_end = period.get("slutDato")
        if not period_end:
            continue

        # Skip periods we already have in the DB
        if skip_periods and period_end in skip_periods:
            continue

        # Skip duplicate periods within this fetch (e.g. original + amendment)
        if period_end in fetched_periods:
            continue
        fetched_periods.add(period_end)

        # Find the best XBRL document (prefer xhtml inline XBRL, fall back to xml)
        xhtml_url = next(
            (
                d["dokumentUrl"]
                for d in rec.get("dokumenter", [])
                if d.get("dokumentMimeType") == "application/xhtml+xml"
                and d.get("dokumentType") == "AARSRAPPORT"
            ),
            None,
        )
        xml_url = next(
            (
                d["dokumentUrl"]
                for d in rec.get("dokumenter", [])
                if d.get("dokumentMimeType") == "application/xml"
                and d.get("dokumentType") == "AARSRAPPORT"
            ),
            None,
        )

        doc_url = xhtml_url or xml_url
        if not doc_url:
            continue

        try:
            rx = httpx.get(doc_url, timeout=60, follow_redirects=True)
            rx.raise_for_status()
            ranks: dict[tuple[str, str], int] = {}
            facts = _parse_xbrl_facts(rx.content, ranks_out=ranks)
        except Exception:
            continue

        # Opening equity = equity tagged at the day before the period start
        # (prior-year closing balance, always present as a comparative figure)
        period_start = period.get("startDato")
        if period_start:
            try:
                open_date = (
                    date.fromisoformat(period_start) - timedelta(days=1)
                ).isoformat()
            except ValueError:
                open_date = None
        else:
            open_date = None
        egenkapital_primo = _pick_fact(facts, open_date, *_EQUITY) if open_date else None

        has_opening = egenkapital_primo is not None and open_date is not None

        rows.append(
            {
                "cvr": cvr,
                "regnskab_slut": period_end,
                "regnskab_start": period_start,
                "omsaetning": _pick_fact(facts, period_end, *_REVENUE),
                "ebit": _pick_fact(facts, period_end, *_EBIT),
                "aarsresultat": _pick_fact(facts, period_end, *_NET_PROFIT),
                "egenkapital": _pick_fact(facts, period_end, *_EQUITY),
                "aktiver": _pick_fact(facts, period_end, *_ASSETS),
                "kortfristet_gaeld": _pick_fact(facts, period_end, *_CURRENT_LIAB),
                "langfristet_gaeld": _pick_fact(facts, period_end, *_NONCURRENT_LIAB),
                "ansatte_regnskab": _pick_fact(facts, period_end, *_EMPLOYEES),
                # Columns added via ALTER TABLE — kept at end to match DB column order
                "personaleomkostninger": _pick_fact(facts, period_end, *_PERSONNEL_COSTS),
                "egenkapital_primo": egenkapital_primo,
                "bruttoresultat": _pick_fact(facts, period_end, *_GROSS_PROFIT),
                "vareforbrug": _pick_fact(facts, period_end, *_COST_OF_GOODS),
                "afskrivninger": _pick_fact(facts, period_end, *_DEPRECIATION),
                "fin_indt": _pick_fact(facts, period_end, *_FIN_INCOME),
                "fin_udg": _pick_fact(facts, period_end, *_FIN_EXPENSES),
                "skat": _pick_fact(facts, period_end, *_TAX),
                "udbytte": _pick_fact(facts, period_end, *_DIVIDENDS),
                "gaeld_tilknyttede_lt": _sum_facts(facts, period_end, *_RELATED_PARTY_DEBT_LT),
                "gaeld_tilknyttede_st": _sum_facts(facts, period_end, *_RELATED_PARTY_DEBT_ST),
                "ebt":                  _pick_fact(facts, period_end, *_EBT),
                "kassebeholdning":      _pick_fact(facts, period_end, *_CASH),
                "omloebsaktiver":       _pick_fact(facts, period_end, *_CURRENT_ASSETS),
                "anlaegsaktiver":       _pick_fact(facts, period_end, *_NONCURRENT_ASSETS),
                "gaeld_i_alt":          _pick_fact(facts, period_end, *_TOTAL_LIABILITIES),
                "hensatte_forpligtelser": _pick_fact(facts, period_end, *_PROVISIONS),
                "tilgodehavender":      _pick_fact(facts, period_end, *_RECEIVABLES),
                "tilgodehavender_tilknyttede": _sum_facts(facts, period_end, *_RELATED_PARTY_RECEIVABLES_ST),
                "varebeholdninger":     _pick_fact(facts, period_end, *_INVENTORIES),
                "vaerdipapirer":        _sum_groups(facts, period_end, _CURRENT_SECURITIES),
                "leverandoergaeld":     _pick_fact(facts, period_end, *_TRADE_PAYABLES),
                "bankgaeld_st":         _sum_groups(facts, period_end, _BANK_DEBT_ST),
                "bankgaeld_lt":         _sum_groups(facts, period_end, _BANK_DEBT_LT),
                "derivater_aktiver":    _sum_facts(facts, period_end, *_DERIVATIVE_ASSETS),
                "derivater_passiver":   _sum_facts(facts, period_end, *_DERIVATIVE_LIABILITIES),
                # Opening loans and bank debt from the comparative column.
                "gaeld_tilknyttede_primo": _opening_sum(
                    facts, open_date, has_opening,
                    [(c,) for c in (*_RELATED_PARTY_DEBT_LT, *_RELATED_PARTY_DEBT_ST)]),
                "bankgaeld_primo":      _opening_sum(
                    facts, open_date, has_opening, [*_BANK_DEBT_ST, *_BANK_DEBT_LT]),
            }
        )
        if facts_out is not None:
            # Every total in the filing, current year and comparatives, so a
            # new KPI can be derived later without downloading XBRL again.
            facts_out.extend(
                {"cvr": cvr, "regnskab_slut": period_end, "concept": concept,
                 "period_end": fact_end, "value": float(value)}
                for concept, by_end in facts.items()
                for fact_end, value in by_end.items()
                if ranks.get((concept, fact_end), 0) < DIMENSIONAL_RANK
            )

    return rows


# ── DuckDB schema ──────────────────────────────────────────────────────────────

_DDL = """
CREATE TABLE IF NOT EXISTS companies (
    cvr           INTEGER PRIMARY KEY,
    navn          VARCHAR,
    status        VARCHAR,
    stiftelsesdato DATE,
    alder_aar     INTEGER,
    branche_kode  VARCHAR,
    branche_tekst VARCHAR,
    ansatte       INTEGER,
    is_intraday   BOOLEAN DEFAULT FALSE,
    is_multidesk  BOOLEAN DEFAULT FALSE,
    is_us_trading BOOLEAN DEFAULT FALSE,
    is_hedgefund  BOOLEAN DEFAULT FALSE,
    adresse       VARCHAR,
    postnr        VARCHAR,
    postby        VARCHAR
);
CREATE TABLE IF NOT EXISTS financials (
    cvr                INTEGER,
    regnskab_slut      DATE,
    regnskab_start     DATE,
    omsaetning         DOUBLE,
    ebit               DOUBLE,
    aarsresultat       DOUBLE,
    egenkapital        DOUBLE,
    aktiver            DOUBLE,
    kortfristet_gaeld      DOUBLE,
    langfristet_gaeld      DOUBLE,
    ansatte_regnskab       INTEGER,
    personaleomkostninger  DOUBLE,
    egenkapital_primo      DOUBLE,
    bruttoresultat         DOUBLE,
    vareforbrug            DOUBLE,
    afskrivninger          DOUBLE,
    fin_indt               DOUBLE,
    fin_udg                DOUBLE,
    skat                   DOUBLE,
    udbytte                DOUBLE,
    gaeld_tilknyttede_lt   DOUBLE,
    gaeld_tilknyttede_st   DOUBLE,
    ebt                    DOUBLE,
    kassebeholdning        DOUBLE,
    omloebsaktiver         DOUBLE,
    PRIMARY KEY (cvr, regnskab_slut)
);
-- Every total tagged in each parsed filing (current year and comparatives),
-- keyed by the filing it came from. Lets new KPIs be derived without a refetch.
CREATE TABLE IF NOT EXISTS xbrl_facts (
    cvr            INTEGER,
    regnskab_slut  DATE,
    concept        VARCHAR,
    period_end     DATE,
    value          DOUBLE
);
CREATE TABLE IF NOT EXISTS company_locations (
    cvr     INTEGER PRIMARY KEY,
    adresse VARCHAR,
    postby  VARCHAR,
    postnr  VARCHAR,
    lat     DOUBLE,
    lon     DOUBLE
);
CREATE TABLE IF NOT EXISTS employee_monthly (
    cvr             INTEGER,
    aar             INTEGER,
    maaned          INTEGER,
    antal_ansatte   INTEGER,
    antal_aarsvaerk INTEGER,
    PRIMARY KEY (cvr, aar, maaned)
);
"""

_KPI_VIEW = """
CREATE OR REPLACE VIEW kpis AS
WITH base AS (
    SELECT
        c.cvr,
        c.navn,
        c.is_intraday,
        c.is_multidesk,
        c.is_us_trading,
        c.is_hedgefund,
        c.branche_kode,
        c.branche_tekst,
        c.alder_aar,
        c.ansatte                                                            AS ansatte_register,
        f.regnskab_slut,
        f.regnskab_start,
        f.omsaetning,
        f.ebit,
        f.aarsresultat,
        f.egenkapital,
        f.aktiver,
        f.kortfristet_gaeld,
        f.langfristet_gaeld,
        f.personaleomkostninger,
        f.egenkapital_primo,
        f.bruttoresultat,
        f.vareforbrug,
        f.afskrivninger,
        f.fin_indt,
        f.fin_udg,
        f.skat,
        f.udbytte,
        f.gaeld_tilknyttede_lt,
        f.gaeld_tilknyttede_st,
        f.ebt,
        f.kassebeholdning,
        f.omloebsaktiver,
        f.anlaegsaktiver,
        f.gaeld_i_alt,
        f.hensatte_forpligtelser,
        f.tilgodehavender,
        f.tilgodehavender_tilknyttede,
        f.varebeholdninger,
        f.vaerdipapirer,
        f.leverandoergaeld,
        f.bankgaeld_st,
        f.bankgaeld_lt,
        f.derivater_aktiver,
        f.derivater_passiver,
        f.gaeld_tilknyttede_primo,
        f.bankgaeld_primo,
        COALESCE(
            CAST(f.ansatte_regnskab AS INTEGER),
            (SELECT CAST(ROUND(AVG(e.antal_ansatte)) AS INTEGER)
             FROM employee_monthly e
             WHERE e.cvr = c.cvr
               AND MAKE_DATE(e.aar, e.maaned, 1) BETWEEN
                   DATE_TRUNC('month',
                       COALESCE(f.regnskab_start, f.regnskab_slut - INTERVAL 364 DAY))
                   AND f.regnskab_slut),
            c.ansatte
        )                                                                    AS ansatte
    FROM companies c
    JOIN financials f USING (cvr)
)
SELECT
    cvr, navn, is_intraday, is_multidesk, is_us_trading, is_hedgefund, branche_kode, branche_tekst, alder_aar,
    ansatte_register, regnskab_slut, regnskab_start, omsaetning, ebit, aarsresultat,
    egenkapital, egenkapital_primo, aktiver, ansatte,
    personaleomkostninger, bruttoresultat, vareforbrug, afskrivninger,
    fin_indt, fin_udg, skat, udbytte,
    COALESCE(gaeld_tilknyttede_lt, 0) + COALESCE(gaeld_tilknyttede_st, 0) AS gaeld_tilknyttede,
    gaeld_tilknyttede_lt, gaeld_tilknyttede_st, langfristet_gaeld,
    ebt, kassebeholdning, omloebsaktiver,
    anlaegsaktiver, gaeld_i_alt, hensatte_forpligtelser, tilgodehavender,
    tilgodehavender_tilknyttede, varebeholdninger, vaerdipapirer, leverandoergaeld,
    bankgaeld_st, bankgaeld_lt, derivater_aktiver, derivater_passiver,
    gaeld_tilknyttede_primo, bankgaeld_primo,

    -- ── Capital measures ────────────────────────────────────────────────
    -- A balance-sheet line a filer omits is zero in practice (Danish filings
    -- do not tag empty lines), so the components are COALESCEd, but only
    -- for filings that report a balance sheet at all (equity present).

    -- Interest-bearing debt: banks, credit institutions, mortgages, IFRS 16
    -- leases, plus related-party / shareholder loans — at owner-funded
    -- trading firms those loans are how the capital comes in.
    CASE WHEN egenkapital IS NOT NULL THEN
        COALESCE(bankgaeld_st, 0) + COALESCE(bankgaeld_lt, 0)
        + COALESCE(gaeld_tilknyttede_lt, 0) + COALESCE(gaeld_tilknyttede_st, 0)
    END                                                                      AS rentebaerende_gaeld,

    -- Total capital deployed: what the owners have put at work — equity plus
    -- related-party / shareholder loans. Denominator of ROTCD.
    egenkapital + COALESCE(gaeld_tilknyttede_lt, 0) + COALESCE(gaeld_tilknyttede_st, 0)
                                                                             AS total_capital_deployed,

    -- Invested capital (financing side): equity + all interest-bearing debt.
    -- Cash is not netted: at a trading firm it is margin and collateral.
    egenkapital + COALESCE(bankgaeld_st, 0) + COALESCE(bankgaeld_lt, 0)
        + COALESCE(gaeld_tilknyttede_lt, 0) + COALESCE(gaeld_tilknyttede_st, 0)
                                                                             AS investeret_kapital,

    -- Net working capital: current assets - current liabilities.
    omloebsaktiver - kortfristet_gaeld                                       AS arbejdskapital,

    -- Operating working capital: the same without the financing items —
    -- cash and securities out of current assets, short-term bank and
    -- related-party debt out of current liabilities.
    (omloebsaktiver - COALESCE(kassebeholdning, 0) - COALESCE(vaerdipapirer, 0))
    - (kortfristet_gaeld - COALESCE(bankgaeld_st, 0) - COALESCE(gaeld_tilknyttede_st, 0))
                                                                             AS driftsarbejdskapital,

    -- Current ratio
    CASE WHEN kortfristet_gaeld > 0 AND omloebsaktiver IS NOT NULL
        THEN ROUND(omloebsaktiver / kortfristet_gaeld, 2)
    END                                                                      AS current_ratio,

    -- Return on Equity (suppressed when equity < 1M DKK — near-zero equity produces
    -- meaningless ratios, e.g. 3,700% for Current Commodities 2021 with 1K DKK equity)
    CASE WHEN egenkapital >= 1000000
        THEN ROUND(aarsresultat / egenkapital * 100, 2) END                  AS roe_pct,

    -- Return on Capital Employed
    CASE WHEN aktiver IS NOT NULL AND kortfristet_gaeld IS NOT NULL
          AND (aktiver - kortfristet_gaeld) > 0
        THEN ROUND(ebit / (aktiver - kortfristet_gaeld) * 100, 2)
    END                                                                      AS roce_pct,

    -- Net margin
    CASE WHEN omsaetning > 0
        THEN ROUND(aarsresultat / omsaetning * 100, 2) END                  AS net_margin_pct,

    -- Revenue per employee (tDKK)
    CASE WHEN ansatte > 0
        THEN ROUND(omsaetning / ansatte / 1000, 0)
    END                                                                      AS omsaetning_per_ansatte_tdkk,

    -- Net profit per employee (tDKK)
    CASE WHEN ansatte > 0
        THEN ROUND(aarsresultat / ansatte / 1000, 0)
    END                                                                      AS resultat_per_ansatte_tdkk,

    -- Equity per employee (tDKK)
    CASE WHEN ansatte > 0
        THEN ROUND(egenkapital / ansatte / 1000, 0)
    END                                                                      AS egenkapital_per_ansatte_tdkk,

    -- Asset turnover
    CASE WHEN aktiver > 0
        THEN ROUND(omsaetning / aktiver, 2) END                              AS aktiv_omsaetning,

    -- Debt ratio
    CASE WHEN aktiver > 0 AND egenkapital IS NOT NULL
        THEN ROUND((aktiver - egenkapital) / aktiver * 100, 1) END          AS gaeld_ratio_pct,

    -- Salary per employee (tDKK)
    CASE WHEN ansatte > 0 AND personaleomkostninger IS NOT NULL
        THEN ROUND(personaleomkostninger / ansatte / 1000, 0)
    END                                                                      AS personaleomkostninger_per_ansatte_tdkk

FROM base;
"""


#: Balance-sheet detail columns added after the original schema (ALTER TABLE).
_BALANCE_DETAIL_COLS = (
    "anlaegsaktiver", "gaeld_i_alt", "hensatte_forpligtelser", "tilgodehavender",
    "tilgodehavender_tilknyttede", "varebeholdninger", "vaerdipapirer",
    "leverandoergaeld", "bankgaeld_st", "bankgaeld_lt",
    "derivater_aktiver", "derivater_passiver",
    "gaeld_tilknyttede_primo", "bankgaeld_primo",
)


def store(
    company_rows: list[dict],
    financial_rows: list[dict],
    employee_monthly_rows: list[dict] | None = None,
    fact_rows: list[dict] | None = None,
) -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(DB_PATH))
    con.execute(_DDL)
    # Migrate existing DB: add is_intraday column if it doesn't exist yet
    existing_cols = {
        row[0]
        for row in con.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name='companies'"
        ).fetchall()
    }
    if "is_intraday" not in existing_cols:
        con.execute("ALTER TABLE companies ADD COLUMN is_intraday BOOLEAN DEFAULT FALSE")
    if "is_multidesk" not in existing_cols:
        con.execute("ALTER TABLE companies ADD COLUMN is_multidesk BOOLEAN DEFAULT FALSE")
    if "is_us_trading" not in existing_cols:
        con.execute("ALTER TABLE companies ADD COLUMN is_us_trading BOOLEAN DEFAULT FALSE")
    if "is_hedgefund" not in existing_cols:
        con.execute("ALTER TABLE companies ADD COLUMN is_hedgefund BOOLEAN DEFAULT FALSE")
    for _col in ("adresse", "postnr", "postby"):
        if _col not in existing_cols:
            con.execute(f"ALTER TABLE companies ADD COLUMN {_col} VARCHAR")

    fin_cols = {
        row[0]
        for row in con.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name='financials'"
        ).fetchall()
    }
    if "ansatte_regnskab" not in fin_cols:
        con.execute("ALTER TABLE financials ADD COLUMN ansatte_regnskab INTEGER")
    if "personaleomkostninger" not in fin_cols:
        con.execute("ALTER TABLE financials ADD COLUMN personaleomkostninger DOUBLE")
    if "egenkapital_primo" not in fin_cols:
        con.execute("ALTER TABLE financials ADD COLUMN egenkapital_primo DOUBLE")
    if "bruttoresultat" not in fin_cols:
        con.execute("ALTER TABLE financials ADD COLUMN bruttoresultat DOUBLE")
    for _col in ("vareforbrug", "afskrivninger", "fin_indt", "fin_udg", "skat", "udbytte",
                 "gaeld_tilknyttede_lt", "gaeld_tilknyttede_st",
                 "ebt", "kassebeholdning", "omloebsaktiver",
                 *_BALANCE_DETAIL_COLS):
        if _col not in fin_cols:
            con.execute(f"ALTER TABLE financials ADD COLUMN {_col} DOUBLE")

    if company_rows:
        df_c = pd.DataFrame(company_rows)
        # Preserve geocoded lat/lon — company_locations is a separate table and not touched here.
        # But upsert the address string so company_locations.adresse stays in sync with the
        # registered address whenever we re-fetch. We do this by updating company_locations for
        # any row that already has coordinates but an outdated address.
        for row in company_rows:
            if row.get("adresse") and row.get("postnr"):
                con.execute("""
                    UPDATE company_locations
                    SET adresse = ?, postby = ?, postnr = ?
                    WHERE cvr = ? AND lat IS NOT NULL
                """, [row["adresse"], row.get("postby"), row["postnr"], row["cvr"]])
        con.execute("DELETE FROM companies WHERE cvr IN (SELECT cvr FROM df_c)")
        con.execute("INSERT INTO companies SELECT * FROM df_c")

    if financial_rows:
        df_f = pd.DataFrame(financial_rows)
        # Cast date columns so DuckDB doesn't complain about VARCHAR vs DATE comparison
        for col in ("regnskab_slut", "regnskab_start"):
            if col in df_f.columns:
                df_f[col] = pd.to_datetime(df_f[col], errors="coerce").dt.date
        con.execute(
            "DELETE FROM financials WHERE (cvr, CAST(regnskab_slut AS VARCHAR)) IN "
            "(SELECT cvr, CAST(regnskab_slut AS VARCHAR) FROM df_f)"
        )
        # Use explicit column names so insert is resilient to dict/DataFrame column order
        cols = ", ".join(df_f.columns)
        con.execute(f"INSERT INTO financials ({cols}) SELECT {cols} FROM df_f")

    if employee_monthly_rows:
        df_em = pd.DataFrame(employee_monthly_rows)  # referenced by name in SQL below
        con.execute("DELETE FROM employee_monthly WHERE cvr IN (SELECT DISTINCT cvr FROM df_em)")
        con.execute("INSERT INTO employee_monthly SELECT * FROM df_em")

    if fact_rows:
        df_x = pd.DataFrame(fact_rows)  # referenced by name in SQL below
        for col in ("regnskab_slut", "period_end"):
            df_x[col] = pd.to_datetime(df_x[col], errors="coerce").dt.date
        # Replace the facts of each filing that was re-parsed, nothing else.
        con.execute(
            "DELETE FROM xbrl_facts WHERE (cvr, regnskab_slut) IN "
            "(SELECT DISTINCT cvr, regnskab_slut FROM df_x)"
        )
        con.execute(
            "INSERT INTO xbrl_facts (cvr, regnskab_slut, concept, period_end, value) "
            "SELECT cvr, regnskab_slut, concept, period_end, value FROM df_x"
        )

    # Apply manual patches for filings where XBRL is incomplete
    for (patch_cvr, patch_date), fields in MANUAL_PATCHES.items():
        sets = ", ".join(f"{col} = {repr(val)}" for col, val in fields.items())
        con.execute(
            f"UPDATE financials SET {sets} "
            f"WHERE cvr = {patch_cvr} AND CAST(regnskab_slut AS VARCHAR) = '{patch_date}'"
        )

    con.execute(_KPI_VIEW)
    print(
        f"  Stored {len(company_rows)} companies, "
        f"{len(financial_rows)} financial records, "
        f"{len(employee_monthly_rows or [])} monthly headcount records -> {DB_PATH}"
    )
    con.close()


# ── Excel export ───────────────────────────────────────────────────────────────

def export_excel() -> None:
    con = duckdb.connect(str(DB_PATH), read_only=True)
    # Same universe as the dashboard: grid operators, retail utilities and
    # other EXCLUDED_CVR entities stay in the DB but not in the report.
    excluded = ", ".join(str(c) for c in sorted(EXCLUDED_CVR)) or "NULL"
    con.execute(
        f"CREATE TEMP VIEW kpis_export AS SELECT * FROM kpis WHERE cvr NOT IN ({excluded})"
    )

    df_snap = con.execute("""
        SELECT * FROM kpis_export
        WHERE regnskab_slut = (
            SELECT MAX(k2.regnskab_slut) FROM kpis_export k2 WHERE k2.cvr = kpis_export.cvr
        )
        ORDER BY omsaetning DESC NULLS LAST
    """).df()

    df_ts = con.execute("SELECT * FROM kpis_export ORDER BY navn, regnskab_slut").df()

    df_summary = con.execute("""
        SELECT
            branche_tekst,
            COUNT(*)                                AS antal_selskaber,
            ROUND(AVG(roe_pct), 1)                  AS avg_roe_pct,
            ROUND(AVG(roce_pct), 1)                 AS avg_roce_pct,
            ROUND(AVG(net_margin_pct), 1)           AS avg_net_margin_pct,
            ROUND(MEDIAN(omsaetning_per_ansatte_tdkk), 0) AS median_omsaetning_per_ansatte_tdkk,
            ROUND(SUM(omsaetning) / 1e9, 2)         AS total_omsaetning_mia_dkk
        FROM kpis_export
        WHERE regnskab_slut = (
            SELECT MAX(k2.regnskab_slut) FROM kpis_export k2 WHERE k2.cvr = kpis_export.cvr
        )
        GROUP BY branche_tekst
        ORDER BY total_omsaetning_mia_dkk DESC NULLS LAST
    """).df()

    df_intraday_snap = con.execute("""
        SELECT * FROM kpis_export
        WHERE is_intraday = TRUE
          AND regnskab_slut = (
              SELECT MAX(k2.regnskab_slut) FROM kpis_export k2 WHERE k2.cvr = kpis_export.cvr
          )
        ORDER BY omsaetning DESC NULLS LAST
    """).df()

    df_intraday_ts = con.execute("""
        SELECT * FROM kpis_export
        WHERE is_intraday = TRUE
        ORDER BY navn, regnskab_slut
    """).df()

    con.close()

    # Same fiscal-year attribution as the dashboard (skævt regnskabsår), so a
    # 30 June 2024 close reads as FY 2023 in Excel too.
    for frame in (df_snap, df_ts, df_intraday_snap, df_intraday_ts):
        periods = list(zip(frame["regnskab_start"], frame["regnskab_slut"]))
        pos = frame.columns.get_loc("regnskab_start") + 1
        frame.insert(pos, "fiscal_year", [fy.fiscal_year(a, b) for a, b in periods])
        frame.insert(pos + 1, "fy_label", [fy.fiscal_year_label(a, b) for a, b in periods])
        frame.insert(pos + 2, "period_months", [fy.period_months(a, b) for a, b in periods])

    XLSX_PATH.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(XLSX_PATH, engine="openpyxl") as writer:
        df_summary.to_excel(writer, sheet_name="Branche Oversigt", index=False)
        df_snap.to_excel(writer, sheet_name="KPI Snapshot", index=False)
        df_ts.to_excel(writer, sheet_name="Time Series", index=False)
        df_intraday_snap.to_excel(writer, sheet_name="Intraday Snapshot", index=False)
        df_intraday_ts.to_excel(writer, sheet_name="Intraday Time Series", index=False)

        for sheet in writer.sheets.values():
            for col in sheet.columns:
                width = max(len(str(cell.value or "")) for cell in col)
                sheet.column_dimensions[col[0].column_letter].width = min(width + 2, 45)

    print(f"  Exported -> {XLSX_PATH}")


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="TraderTracker KPI pipeline")
    ap.add_argument("--fetch", action="store_true")
    ap.add_argument("--export", action="store_true")
    ap.add_argument("--refresh", action="store_true", help="Re-fetch XBRL even for cached companies")
    ap.add_argument(
        "--inspect",
        metavar="CVR",
        type=int,
        help="Dump parsed XBRL facts for a CVR number",
    )
    args = ap.parse_args()

    run_all = not (args.fetch or args.export or args.inspect)

    if args.inspect:
        cvr = args.inspect
        print(f"Fetching XBRL facts for CVR {cvr}...\n")
        rows = fetch_and_parse_financials(cvr)
        if not rows:
            print("No financial data found.")
            return
        for row in rows:
            print(f"Period ending {row['regnskab_slut']}:")
            for k, v in row.items():
                if k not in ("cvr", "regnskab_slut", "regnskab_start") and v is not None:
                    print(f"  {k:30} {v:>20,.0f}")
        return

    if args.fetch or run_all:
        print(f"Discovering energy trading companies (NACE: {sorted(TARGET_NACE)})...")
        raw = discover_companies()
        print(f"  {len(raw)} companies found")

        company_rows = [extract_company_row(c) for c in raw]
        employee_monthly_rows = [r for c in raw for r in extract_employee_monthly(c)]

        # Load already-cached (cvr, period_end) pairs to skip re-downloading
        # known XBRL while still picking up new filings for existing companies
        known_periods: dict[int, set[str]] = {}
        if DB_PATH.exists() and not args.refresh:
            try:
                _con = duckdb.connect(str(DB_PATH))
                for row in _con.execute(
                    "SELECT cvr, CAST(regnskab_slut AS VARCHAR) FROM financials "
                    "WHERE omsaetning IS NOT NULL OR aarsresultat IS NOT NULL"
                ).fetchall():
                    known_periods.setdefault(row[0], set()).add(row[1])
                _con.close()
            except Exception:
                pass

        financial_rows: list[dict] = []
        fact_rows: list[dict] = []
        for i, crow in enumerate(company_rows, 1):
            cvr = crow["cvr"]
            if not cvr:
                continue
            periods = known_periods.get(cvr, set())
            tag = f" [{len(periods)} periods cached]" if periods else ""
            print(f"  [{i}/{len(company_rows)}] {crow['navn']} (CVR {cvr}){tag}", flush=True)
            financial_rows.extend(
                fetch_and_parse_financials(cvr, skip_periods=periods, facts_out=fact_rows)
            )

        store(company_rows, financial_rows, employee_monthly_rows, fact_rows)
        check_watchlist()

    if args.export or run_all:
        print("Computing KPIs and exporting to Excel...")
        export_excel()

    print("Done.")


if __name__ == "__main__":
    main()
