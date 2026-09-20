"""Tests for the skævt-regnskabsår attribution rule.

Cases are taken from real shapes present in data/tradertracker.duckdb:
30 June and 30 September closers, 18-month first periods, and the short
transition period a company files when it moves its year-end.
"""

from datetime import date

import pytest

from tradertracker.fiscalyear import (
    annualisation_factor,
    fiscal_year,
    fiscal_year_label,
    is_annual,
    is_offset_year_end,
    period_days,
    period_months,
    to_date,
)


class TestFiscalYear:
    @pytest.mark.parametrize(
        "start, end, expected",
        [
            # Calendar year — unchanged by the new rule.
            ("2024-01-01", "2024-12-31", 2024),
            ("2019-01-01", "2019-12-31", 2019),
            # 30 June closers: the bulk of this dataset's offset filers.
            # Jul-2023..Jun-2024 is mostly 2023 trading, not 2024.
            ("2023-07-01", "2024-06-30", 2023),
            ("2021-07-01", "2022-06-30", 2021),
            # 30 September closers: nine of twelve months fall in the end year.
            ("2023-10-01", "2024-09-30", 2024),
            # 30 April closer: eight of twelve months in the start year.
            ("2023-05-01", "2024-04-30", 2023),
            # 31 August closer.
            ("2024-09-01", "2025-08-31", 2025),
            # Short transition period after moving to a calendar year-end.
            ("2025-09-01", "2025-12-31", 2025),
            # 18-month first period.
            ("2023-01-01", "2024-06-30", 2023),
            # 6-month stub.
            ("2023-07-01", "2023-12-31", 2023),
        ],
    )
    def test_midpoint_attribution(self, start, end, expected):
        assert fiscal_year(start, end) == expected

    def test_june_closer_moves_off_its_end_year(self):
        """The regression this module exists for."""
        naive = date.fromisoformat("2024-06-30").year
        assert naive == 2024
        assert fiscal_year("2023-07-01", "2024-06-30") == 2023

    def test_missing_start_assumes_twelve_months(self):
        assert fiscal_year(None, "2024-06-30") == 2023
        assert fiscal_year(None, "2024-12-31") == 2024

    def test_missing_end_is_none(self):
        assert fiscal_year("2024-01-01", None) is None
        assert fiscal_year(None, None) is None

    def test_reversed_period_is_none(self):
        assert fiscal_year("2024-12-31", "2024-01-01") is None

    def test_accepts_date_objects(self):
        assert fiscal_year(date(2023, 7, 1), date(2024, 6, 30)) == 2023


class TestPeriodLength:
    def test_normal_year_is_twelve_months(self):
        assert period_days("2024-01-01", "2024-12-31") == 366  # leap year, inclusive
        assert period_months("2023-01-01", "2023-12-31") == 12.0

    def test_eighteen_month_first_period(self):
        assert period_months("2023-01-01", "2024-06-30") == 18.0

    def test_short_transition_period(self):
        assert period_months("2025-09-01", "2025-12-31") == 4.0

    def test_is_annual_admits_offset_years(self):
        assert is_annual("2023-07-01", "2024-06-30")
        assert is_annual("2023-10-01", "2024-09-30")

    def test_is_annual_rejects_stubs_and_long_periods(self):
        assert not is_annual("2023-01-01", "2024-06-30")  # 18 months
        assert not is_annual("2025-09-01", "2025-12-31")  # 4 months
        assert not is_annual("2023-01-01", "2023-06-30")  # 6 months

    def test_annualisation_scales_flows_to_a_year(self):
        # An 18-month period's revenue must be scaled down by ~1/3.
        factor = annualisation_factor("2023-01-01", "2024-06-30")
        assert factor == pytest.approx(0.667, abs=0.01)
        # A 6-month stub scales up by ~2x.
        assert annualisation_factor("2023-07-01", "2023-12-31") == pytest.approx(1.98, abs=0.02)
        # A normal year is left essentially alone.
        assert annualisation_factor("2023-01-01", "2023-12-31") == pytest.approx(1.0, abs=0.01)

    def test_annualisation_none_without_period(self):
        assert annualisation_factor(None, None) is None

    @pytest.mark.parametrize(
        "start, end",
        [
            ("2023-01-01", "2023-12-31"),  # 365 days
            ("2024-01-01", "2024-12-31"),  # 366 days — leap year
            ("2020-01-01", "2020-12-31"),  # 366 days — leap year
            ("2023-07-01", "2024-06-30"),  # offset year, still a full year
            ("2023-10-01", "2024-09-30"),  # offset year, still a full year
        ],
    )
    def test_normal_years_are_never_rescaled(self, start, end):
        """The majority who close on time must be untouched by this module.

        A leap year is still one year of trading; scaling it by 365/366 would
        move every calendar filer's growth by ~0.27% for no reason.
        """
        assert annualisation_factor(start, end) == 1.0

    def test_genuine_stubs_are_still_rescaled(self):
        # 4-month transition stub: 122 days -> 365/122
        assert annualisation_factor("2025-09-01", "2025-12-31") == pytest.approx(2.99, abs=0.02)
        # 18-month first period: 547 days -> 365/547
        assert annualisation_factor("2023-01-01", "2024-06-30") == pytest.approx(0.667, abs=0.01)
        # 11-month period is outside the 15-day no-op band and is rescaled
        assert annualisation_factor("2023-08-01", "2024-06-30") > 1.05


class TestLabels:
    @pytest.mark.parametrize(
        "start, end, expected",
        [
            ("2024-01-01", "2024-12-31", "2024"),
            ("2023-07-01", "2024-06-30", "2023/24"),
            ("2023-10-01", "2024-09-30", "2023/24"),
            ("2024-09-01", "2025-08-31", "2024/25"),
            ("2025-09-01", "2025-12-31", "2025"),
            ("2023-01-01", "2024-06-30", "2023/24"),
            ("2019-07-01", "2020-06-30", "2019/20"),
        ],
    )
    def test_label(self, start, end, expected):
        assert fiscal_year_label(start, end) == expected

    def test_label_empty_without_end(self):
        assert fiscal_year_label("2024-01-01", None) == ""


class TestOffsetDetection:
    def test_december_close_is_not_offset(self):
        assert not is_offset_year_end("2024-12-31")

    @pytest.mark.parametrize("end", ["2024-06-30", "2024-09-30", "2024-04-30", "2024-08-31"])
    def test_summer_and_autumn_closes_are_offset(self, end):
        assert is_offset_year_end(end)

    def test_december_but_not_month_end_is_offset(self):
        assert is_offset_year_end("2024-12-15")


class TestToDate:
    def test_parses_iso_and_timestamps(self):
        assert to_date("2024-06-30") == date(2024, 6, 30)
        assert to_date("2024-06-30T00:00:00") == date(2024, 6, 30)
        assert to_date(date(2024, 6, 30)) == date(2024, 6, 30)

    def test_rejects_garbage(self):
        assert to_date("not-a-date") is None
        assert to_date(None) is None
