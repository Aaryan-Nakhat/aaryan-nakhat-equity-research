"""Replies about your own book, end to end through the local channel (the same path as email / web / CLI):
`raise ₹X`, `sell`, `theses`, `scorecard`. Made-up holdings; throwaway DuckDB; no network, no SMTP, no LLM."""

from __future__ import annotations

import smtplib
from datetime import date

import pytest

from equity_research.common import db

TODAY = date.today()


@pytest.fixture
def session(tmp_path, monkeypatch):
    from equity_research.bot.local import LocalSession

    path = tmp_path / "t.duckdb"
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", path)
    monkeypatch.setenv("HOLDINGS_CSV", str(tmp_path / "none.csv"))
    monkeypatch.setenv("LLM_MODEL", "")
    monkeypatch.setattr(smtplib, "SMTP", lambda *a, **k: pytest.fail("no SMTP"))
    c = db.connect(path)
    c.execute("INSERT INTO equity_master (symbol, company_name) VALUES ('ALPHA', 'Alpha Industries'), "
              "('BETA', 'Beta Chemicals')")
    for sym, px in (("ALPHA", 200.0), ("BETA", 50.0)):
        c.execute("INSERT INTO equity_eod (trade_date, symbol, series, prev_close, open, high, low, last, close, "
                  "avg_price, ttl_trd_qnty, turnover_lacs, no_of_trades, deliv_qty, deliv_per) "
                  "VALUES (?, ?, 'EQ', ?, ?, ?, ?, ?, ?, ?, 1, 1, 1, 1, 50)", [TODAY, sym] + [px] * 7)
    c.close()
    s = LocalSession(sender="me@eqr.local")
    yield s, path
    s.close()


def text_of(result) -> str:
    return " | ".join(d.body for d in result.deliveries)


def _add(path, *buys):
    from equity_research import portfolio as pf

    c = db.connect(path)
    try:
        for b in buys:
            pf.add_lot(c, *b)
    finally:
        c.close()


def test_raise_without_quantities_asks_for_them(session):
    s, _ = session
    out = text_of(s.ask("raise 50000"))
    assert "My holdings" in out and "₹50,000" in out


def test_raise_plans_the_sale_with_tax(session):
    s, path = session
    _add(path, ("ALPHA", 100, 100, "2024-01-10"), ("BETA", 100, 40, "2026-06-01"))
    out = text_of(s.ask("take out 5000"))
    assert "Raise ₹5,000" in out and "least tax" in out
    assert "ALPHA" in out                      # a long-term holding inside the exemption → ₹0 tax first
    assert "tax ≈ ₹0" in out


def test_sell_ranks_with_value_and_term(session):
    s, path = session
    _add(path, ("ALPHA", 10, 100, "2024-01-10"))
    out = text_of(s.ask("sell"))
    assert "Which to sell first" in out and "ALPHA" in out


def test_theses_and_scorecard_reply_when_empty(session):
    s, _ = session
    assert "No theses yet" in text_of(s.ask("theses"))
    assert text_of(s.ask("scorecard"))          # replies (no calls logged yet) without crashing
