"""Smoke test: the dashboard renders against the committed database."""

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "tradertracker.duckdb"

pytestmark = pytest.mark.skipif(not DB.exists(), reason="no database in data/")


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch) -> AppTest:
    monkeypatch.chdir(ROOT)  # DB_PATH is relative to the repository root
    at = AppTest.from_file(str(ROOT / "tradertracker" / "dashboard.py"), default_timeout=120)
    at.run()
    return at


def test_renders_without_exceptions(app: AppTest) -> None:
    assert not app.exception, [e.value for e in app.exception]


def test_capital_metrics_are_selectable(app: AppTest) -> None:
    options = app.selectbox(key="ts_metric").options
    for label in ("ROIC (%)", "Invested Capital (DKK)", "Working Capital (DKK)",
                  "Total Capital Deployed (DKK)"):
        assert label in options


def test_annualise_toggle_renders(app: AppTest) -> None:
    app.checkbox(key="ts_annualise").check().run()
    assert not app.exception, [e.value for e in app.exception]
