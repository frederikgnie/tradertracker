"""Tests for XBRL fact extraction in pipeline._parse_xbrl_facts.

Each case reproduces a shape found in real Danish filings that the parser
used to get wrong: Danish number formatting in inline XBRL, equity-statement
components read as total equity, and ESEF reports that tag every total of a
parent-only report with a SeparateMember dimension.
"""

import pytest

from tradertracker.pipeline import (
    _BANK_DEBT_LT,
    _BANK_DEBT_ST,
    _CURRENT_SECURITIES,
    DIMENSIONAL_RANK,
    EUR_DKK,
    _ix_number,
    _opening_sum,
    _parse_xbrl_facts,
    _sum_groups,
)

_HEAD = (
    '<html xmlns="http://www.w3.org/1999/xhtml" '
    'xmlns:ix="http://www.xbrl.org/2013/inlineXBRL" '
    'xmlns:xbrli="http://www.xbrl.org/2003/instance" '
    'xmlns:xbrldi="http://xbrl.org/2006/xbrldi" '
    'xmlns:iso4217="http://www.xbrl.org/2003/iso4217" '
    'xmlns:fsa="http://xbrl.dcca.dk/fsa" '
    'xmlns:ifrs-full="https://xbrl.ifrs.org/taxonomy/2024-03-27/ifrs-full">'
    "<body><ix:header><ix:resources>"
    '<xbrli:unit id="dkk"><xbrli:measure>iso4217:DKK</xbrli:measure></xbrli:unit>'
    '<xbrli:unit id="eur"><xbrli:measure>iso4217:EUR</xbrli:measure></xbrli:unit>'
    '<xbrli:unit id="pure"><xbrli:measure>xbrli:pure</xbrli:measure></xbrli:unit>'
)


def _ctx(ctx_id: str, instant: str, *members: tuple[str, str]) -> str:
    dims = "".join(
        f'<xbrldi:explicitMember dimension="{axis}">{member}</xbrldi:explicitMember>'
        for axis, member in members
    )
    scenario = f"<xbrli:scenario>{dims}</xbrli:scenario>" if members else ""
    return (
        f'<xbrli:context id="{ctx_id}"><xbrli:entity>'
        '<xbrli:identifier scheme="http://www.dcca.dk/cvr">1</xbrli:identifier>'
        f"</xbrli:entity><xbrli:period><xbrli:instant>{instant}</xbrli:instant>"
        f"</xbrli:period>{scenario}</xbrli:context>"
    )


def _fact(name: str, ctx: str, text: str, unit: str = "dkk", **attrs: str) -> str:
    extra = "".join(f' {k}="{v}"' for k, v in attrs.items())
    return (
        f'<ix:nonFraction name="{name}" contextRef="{ctx}" unitRef="{unit}"{extra}>'
        f"{text}</ix:nonFraction>"
    )


def _doc(contexts: list[str], facts: list[str]) -> bytes:
    return (
        _HEAD + "".join(contexts) + "</ix:resources></ix:header>"
        + "".join(facts) + "</body></html>"
    ).encode()


class TestIxNumberFormats:
    @pytest.mark.parametrize(
        "text, fmt, expected",
        [
            ("1.063.876", "ixt:num-comma-decimal", 1_063_876),
            ("97,6", "ixt:num-comma-decimal", 97.6),
            ("1.234,5", "ixt:numcommadecimal", 1234.5),
            ("1,063,876", "ixt:num-dot-decimal", 1_063_876),
            ("1,234.5", "ixt:numdotdecimal", 1234.5),
            ("1 063 876", "ixt:num-comma-decimal", 1_063_876),
            ("1\xa0063\xa0876", "ixt:num-dot-decimal", 1_063_876),
            ("4711", "", 4711),
            ("-", "ixt:fixed-zero", 0),
            ("–", "ixt-sec:numwordsen", 0),
        ],
    )
    def test_formats(self, text, fmt, expected):
        assert _ix_number(text, fmt) == pytest.approx(expected)

    @pytest.mark.parametrize("text", ["", "n/a", "abc"])
    def test_unparseable_is_none(self, text):
        assert _ix_number(text, "ixt:num-comma-decimal") is None


class TestDanishFormattedInlineFiling:
    def test_thousands_dots_are_not_dropped(self):
        """Regression: In Commodities 2023 parsed as equity 0, profit 0."""
        doc = _doc(
            [_ctx("c1", "2023-12-31")],
            [
                _fact("ifrs-full:Equity", "c1", "698.117", unit="eur",
                      scale="3", format="ixt:num-comma-decimal"),
                _fact("ifrs-full:Assets", "c1", "700.277", unit="eur",
                      scale="3", format="ixt:num-comma-decimal"),
            ],
        )
        facts = _parse_xbrl_facts(doc)
        assert facts["ifrs-full:Equity"]["2023-12-31"] == round(698_117_000 * EUR_DKK)
        assert facts["ifrs-full:Assets"]["2023-12-31"] == round(700_277_000 * EUR_DKK)

    def test_sign_attribute_negates(self):
        doc = _doc(
            [_ctx("c1", "2023-12-31")],
            [_fact("fsa:ProfitLoss", "c1", "1.500", sign="-", scale="3",
                   format="ixt:num-comma-decimal")],
        )
        assert _parse_xbrl_facts(doc)["fsa:ProfitLoss"]["2023-12-31"] == -1_500_000


class TestDimensionalContexts:
    def test_equity_component_before_total_does_not_win(self):
        """Regression: retained earnings read as total equity (In Commodities, POWERMART)."""
        doc = _doc(
            [
                _ctx("comp", "2023-12-31",
                     ("fsa:ComponentsOfEquityAxis", "fsa:RetainedEarningsMember")),
                _ctx("total", "2023-12-31"),
            ],
            [
                _fact("fsa:Equity", "comp", "31927375"),  # equity statement first
                _fact("fsa:Equity", "total", "162762280"),
            ],
        )
        assert _parse_xbrl_facts(doc)["fsa:Equity"]["2023-12-31"] == 162_762_280

    def test_separate_member_counts_as_a_total(self):
        """ESEF parent-only reports tag every total with SeparateMember."""
        axis = "ifrs-full:ConsolidatedAndSeparateFinancialStatementsAxis"
        doc = _doc(
            [
                _ctx("sep", "2023-12-31", (axis, "ifrs-full:SeparateMember")),
                _ctx("comp", "2023-12-31", (axis, "ifrs-full:SeparateMember"),
                     ("ifrs-full:ComponentsOfEquityAxis", "ifrs-full:IssuedCapitalMember")),
            ],
            [
                _fact("ifrs-full:Equity", "comp", "3114"),
                _fact("ifrs-full:Equity", "sep", "596109"),
            ],
        )
        ranks: dict[tuple[str, str], int] = {}
        facts = _parse_xbrl_facts(doc, ranks_out=ranks)
        assert facts["ifrs-full:Equity"]["2023-12-31"] == 596_109
        assert ranks[("ifrs-full:Equity", "2023-12-31")] == 2

    def test_ifrs_group_without_dimension_beats_separate(self):
        """IFRS: the group is the default, the parent is SeparateMember."""
        axis = "ifrs-full:ConsolidatedAndSeparateFinancialStatementsAxis"
        doc = _doc(
            [
                _ctx("sep", "2023-12-31", (axis, "ifrs-full:SeparateMember")),
                _ctx("plain", "2023-12-31"),
            ],
            [
                _fact("ifrs-full:Assets", "sep", "100"),
                _fact("ifrs-full:Assets", "plain", "300"),
            ],
        )
        assert _parse_xbrl_facts(doc)["ifrs-full:Assets"]["2023-12-31"] == 300

    def test_danish_gaap_group_beats_parent_default(self):
        """Danish GAAP: the parent is the default, the group ConsolidatedMember (Mind Energy)."""
        axis = "cmn:ConsolidatedSoloDimension"
        doc = _doc(
            [_ctx("plain", "2014-12-31"), _ctx("group", "2014-12-31", (axis, "cmn:ConsolidatedMember"))],
            [
                _fact("fsa:Assets", "plain", "3047967000"),  # parent alone, listed first
                _fact("fsa:Assets", "group", "3524927000"),
            ],
        )
        assert _parse_xbrl_facts(doc)["fsa:Assets"]["2014-12-31"] == 3_524_927_000

    def test_five_year_summary_column_is_not_this_year(self):
        """The five-year summary tags last year's figure with this year's dates."""
        doc = _doc(
            [
                _ctx("prev", "2019-12-31",
                     ("cmn:RetrospectiveInformationDimension", "cmn:PreviousYearMember")),
                _ctx("now", "2019-12-31"),
            ],
            [
                _fact("fsa:Equity", "prev", "16689000"),
                _fact("fsa:Equity", "now", "18798000"),
            ],
        )
        assert _parse_xbrl_facts(doc)["fsa:Equity"]["2019-12-31"] == 18_798_000

    def test_breakdown_is_a_fallback_when_no_total_exists(self):
        """Some filers tag headcount only in a dimensional context."""
        doc = _doc(
            [_ctx("dim", "2023-12-31", ("fsa:SomeAxis", "fsa:SomeMember"))],
            [_fact("fsa:AverageNumberOfEmployees", "dim", "12", unit="pure")],
        )
        ranks: dict[tuple[str, str], int] = {}
        facts = _parse_xbrl_facts(doc, ranks_out=ranks)
        assert facts["fsa:AverageNumberOfEmployees"]["2023-12-31"] == 12
        assert ranks[("fsa:AverageNumberOfEmployees", "2023-12-31")] == DIMENSIONAL_RANK


class TestPlainXmlFiling:
    def test_scenario_component_does_not_override_total(self):
        doc = (
            '<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" '
            'xmlns:xbrldi="http://xbrl.org/2006/xbrldi" '
            'xmlns:iso4217="http://www.xbrl.org/2003/iso4217" '
            'xmlns:fsa="http://xbrl.dcca.dk/fsa">'
            '<xbrli:unit id="dkk"><xbrli:measure>iso4217:DKK</xbrli:measure></xbrli:unit>'
            + _ctx("comp", "2013-12-31",
                   ("fsa:ComponentsOfEquityAxis", "fsa:ContributedCapitalMember"))
            + _ctx("total", "2013-12-31")
            + '<fsa:Equity contextRef="comp" unitRef="dkk" decimals="0">1363000</fsa:Equity>'
            + '<fsa:Equity contextRef="total" unitRef="dkk" decimals="0">12132000</fsa:Equity>'
            + '<fsa:Assets contextRef="total" unitRef="dkk" decimals="0">25000000.0</fsa:Assets>'
            + "</xbrli:xbrl>"
        ).encode()
        facts = _parse_xbrl_facts(doc)
        assert facts["fsa:Equity"]["2013-12-31"] == 12_132_000
        assert facts["fsa:Assets"]["2013-12-31"] == 25_000_000


class TestFilingScope:
    def test_group_filing_does_not_borrow_parent_only_lines(self):
        """Group equity must not be paired with parent-only intercompany loans."""
        axis = "cmn:ConsolidatedSoloDimension"
        doc = _doc(
            [_ctx("parent", "2024-12-31"),
             _ctx("group", "2024-12-31", (axis, "cmn:ConsolidatedMember"))],
            [
                _fact("fsa:Equity", "parent", "500"),
                _fact("fsa:Equity", "group", "800"),
                _fact("fsa:ShorttermPayablesToGroupEnterprises", "parent", "300"),
                _fact("fsa:AverageNumberOfEmployees", "parent", "40", unit="pure"),
            ],
        )
        facts = _parse_xbrl_facts(doc)
        assert facts["fsa:Equity"]["2024-12-31"] == 800
        assert "fsa:ShorttermPayablesToGroupEnterprises" not in facts
        # Headcount is often tagged once for the whole report: kept.
        assert facts["fsa:AverageNumberOfEmployees"]["2024-12-31"] == 40

    def test_parent_only_esef_reads_separate_member(self):
        axis = "ifrs-full:ConsolidatedAndSeparateFinancialStatementsAxis"
        doc = _doc(
            [_ctx("sep", "2024-12-31", (axis, "ifrs-full:SeparateMember"))],
            [_fact("ifrs-full:Equity", "sep", "10"), _fact("ifrs-full:Borrowings", "sep", "4")],
        )
        facts = _parse_xbrl_facts(doc)
        assert facts["ifrs-full:Equity"]["2024-12-31"] == 10
        assert facts["ifrs-full:Borrowings"]["2024-12-31"] == 4


class TestLineGroups:
    END = "2024-12-31"

    def test_total_wins_over_its_parts(self):
        facts = {"fsa:ShorttermInvestments": {self.END: 100},
                 "fsa:OtherShorttermInvestments": {self.END: 100}}
        assert _sum_groups(facts, self.END, _CURRENT_SECURITIES) == 100

    def test_parts_summed_without_a_total(self):
        facts = {"fsa:ShorttermDebtToBanks": {self.END: 30},
                 "fsa:ShorttermDebtToOtherCreditInstitutions": {self.END: 20},
                 "fsa:ShorttermPartOfLongtermLiabilitiesOtherThanProvisions": {self.END: 5}}
        assert _sum_groups(facts, self.END, _BANK_DEBT_ST) == 55

    def test_separate_lines_are_added(self):
        facts = {"ifrs-full:LongtermBorrowings": {self.END: 70},
                 "ifrs-full:NoncurrentLeaseLiabilities": {self.END: 9}}
        assert _sum_groups(facts, self.END, _BANK_DEBT_LT) == 79

    def test_nothing_tagged_is_none(self):
        assert _sum_groups({}, self.END, _BANK_DEBT_LT) is None

    def test_opening_sum_zero_only_with_an_opening_balance_sheet(self):
        assert _opening_sum({}, "2023-12-31", True, _BANK_DEBT_LT) == 0.0
        assert _opening_sum({}, "2023-12-31", False, _BANK_DEBT_LT) is None
        assert _opening_sum({}, None, True, _BANK_DEBT_LT) is None
