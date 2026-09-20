"""Fiscal-year handling for Danish filings with skævt (offset) regnskabsår.

Roughly one in eight filings in this dataset does not close on 31 December —
mostly 30 June and 30 September — and one in eight covers something other than
twelve months (first-year stubs, and the long/short period a company files when
it moves its year-end). Both break the naive ``regnskab_slut.year`` split:

* A 1 Jul 2023 – 30 Jun 2024 year lands in the 2024 bucket next to companies
  reporting Jan–Dec 2024, with which it shares no second half at all.
* An 18-month first period is compared to 12-month periods as if it were one
  year, overstating every flow item by ~50%.

Attribution rule (midpoint)
---------------------------
A period belongs to the calendar year containing its midpoint — that is, the
year in which most of the trading actually happened.

    1 Jul 2023 – 30 Jun 2024  → midpoint 30 Dec 2023 → 2023  ("2023/24")
    1 Oct 2023 – 30 Sep 2024  → midpoint 31 Mar 2024 → 2024  ("2023/24")
    1 Jan 2024 – 31 Dec 2024  → midpoint  1 Jul 2024 → 2024  ("2024")

This is deliberately not the Compustat convention (which assigns only
January–May year-ends to the prior year, leaving June-enders on their end
year). Half this dataset's offset filers close on 30 June, so that convention
would leave the problem untouched.

Dates are treated as inclusive on both ends, matching Danish XBRL
``startDato`` / ``slutDato``.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

__all__ = [
    "ANNUALISE_TOLERANCE_DAYS",
    "ANNUAL_TOLERANCE_DAYS",
    "annualisation_factor",
    "fiscal_year",
    "fiscal_year_label",
    "is_annual",
    "is_offset_year_end",
    "period_days",
    "period_months",
    "to_date",
]

#: A period within this many days of 365 is treated as a normal annual period.
#: 45 days admits 11- and 13-month filings (rounding and 52/53-week years)
#: while still catching the 6-month stubs and 18-month first periods.
ANNUAL_TOLERANCE_DAYS = 45

#: A period within this many days of 365 is left alone by annualisation.
#: Deliberately much tighter than ANNUAL_TOLERANCE_DAYS: it only has to absorb
#: the leap day and 52/53-week years, so that a plain 1 Jan – 31 Dec period is
#: never rescaled. Companies closing on time should see no effect at all from
#: this module; only genuine stubs and year-end changes are normalised.
ANNUALISE_TOLERANCE_DAYS = 15

_DAYS_PER_MONTH = 30.436875


def to_date(value: object) -> date | None:
    """Coerce a date, datetime, ISO string, or pandas Timestamp to ``date``.

    Returns ``None`` for anything null or unparseable, so callers can treat a
    missing period the same way they treat a missing figure.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    # pandas.Timestamp / numpy.datetime64 and anything else with .to_pydatetime
    to_py = getattr(value, "to_pydatetime", None)
    if callable(to_py):
        try:
            converted = to_py()
        except (ValueError, TypeError):
            return None
        return converted.date() if isinstance(converted, datetime) else None
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def _span(start: object, end: object) -> tuple[date, date] | None:
    """Normalise a period to ``(start, end)`` dates, inferring a missing start.

    ``regnskab_start`` is populated for every row currently in the database,
    but a filing without one still needs a year: assume the usual twelve
    months so the midpoint rule degrades to something sensible instead of
    dropping the row.
    """
    end_date = to_date(end)
    if end_date is None:
        return None
    start_date = to_date(start)
    if start_date is None:
        start_date = end_date - timedelta(days=364)
    if start_date > end_date:
        return None
    return start_date, end_date


def period_days(start: object, end: object) -> int | None:
    """Length of the period in days, counting both endpoints."""
    span = _span(start, end)
    if span is None:
        return None
    return (span[1] - span[0]).days + 1


def period_months(start: object, end: object) -> float | None:
    """Length of the period in months, to one decimal (12.0 for a normal year)."""
    days = period_days(start, end)
    if days is None:
        return None
    return round(days / _DAYS_PER_MONTH, 1)


def fiscal_year(start: object, end: object) -> int | None:
    """Calendar year containing the midpoint of the period.

    This is the year the filing should be compared against — see the module
    docstring for why the midpoint and not the closing date.
    """
    span = _span(start, end)
    if span is None:
        return None
    start_date, end_date = span
    midpoint = start_date + (end_date - start_date) / 2
    return midpoint.year


def is_offset_year_end(end: object) -> bool:
    """True when the books close on something other than 31 December."""
    end_date = to_date(end)
    if end_date is None:
        return False
    return not (end_date.month == 12 and end_date.day == 31)


def is_annual(start: object, end: object) -> bool:
    """True when the period is close enough to a year to compare as one."""
    days = period_days(start, end)
    if days is None:
        return False
    return abs(days - 365) <= ANNUAL_TOLERANCE_DAYS


def annualisation_factor(start: object, end: object) -> float | None:
    """Multiplier putting a flow item (revenue, EBIT, profit) on a yearly basis.

    Returns exactly 1.0 for any period that is already a normal year — a
    366-day leap year included — so that the ~84% of filers closing on
    31 December are untouched. Only genuine stubs and the long/short period
    around a year-end change are rescaled.

    Only meaningful for flows. Balance-sheet items are point-in-time and must
    never be scaled by this.
    """
    days = period_days(start, end)
    if not days:
        return None
    if abs(days - 365) <= ANNUALISE_TOLERANCE_DAYS:
        return 1.0
    return 365.0 / days


def fiscal_year_label(start: object, end: object) -> str:
    """Human-readable label: ``"2024"`` for a calendar year, ``"2023/24"`` otherwise.

    A stub or long period keeps the straddling label whenever it actually
    straddles a new year, so an 18-month first period never looks like a
    tidy twelve months.
    """
    span = _span(start, end)
    if span is None:
        return ""
    start_date, end_date = span
    if start_date.year == end_date.year:
        return str(end_date.year)
    return f"{start_date.year}/{str(end_date.year)[-2:]}"
