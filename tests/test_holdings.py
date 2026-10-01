"""💼 Holdings — lots with optional dates, holdings.csv import (broker headers, errors, re-import), valuation
(splits, tax term, yearly return, benchmark) and the web API. Throwaway DuckDB; no network."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from equity_research import holdings as h
from equity_research.common import db

TODAY = date(2026, 9, 30)


def _seed(con):
    con.execute("INSERT INTO equity_master (symbol, company_name) VALUES ('ACME', 'Acme Ltd'), ('BETA', 'Beta Ltd')")
    for sym, px in (("ACME", 200.0), ("BETA", 50.0)):
        con.execute("INSERT INTO equity_eod (trade_date, symbol, series, prev_close, open, high, low, last, close, "
                    "avg_price, ttl_trd_qnty, turnover_lacs, no_of_trades, deliv_qty, deliv_per) "
                    "VALUES (?, ?, 'EQ', ?, ?, ?, ?, ?, ?, ?, 1000, 1, 1, 500, 50)", [TODAY, sym] + [px] * 7)
    for d, c in ((date(2024, 1, 1), 100.0), (TODAY, 150.0)):
        con.execute("INSERT INTO index_close (trade_date, index_name, close) VALUES (?, 'Nifty 500', ?)", [d, c])


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setenv("HOLDINGS_CSV", str(tmp_path / "holdings.csv"))
    c = db.connect(tmp_path / "t.duckdb")
    _seed(c)
    yield c
    c.close()


def test_add_lot_validates_and_marks_a_holding(con):
    lot = h.add_lot(con, "acme", 10, "1,000", "12-03-2025")
    assert (lot["symbol"], lot["qty"], lot["price"], lot["buy_date"]) == ("ACME", 10, 1000, date(2025, 3, 12))
    assert con.execute("SELECT list_type FROM watchlist WHERE symbol = 'ACME'").fetchone()[0] == "holding"
    with pytest.raises(ValueError):
        h.add_lot(con, "ACME", 0, 10)
    with pytest.raises(ValueError):
        h.add_lot(con, "ACME", 1, 10, date.today() + timedelta(days=2))
    with pytest.raises(ValueError):
        h.add_lot(con, "ACME", 1, 10, "31/31/2025")


def test_dated_lot_gets_split_term_yearly_and_benchmark(con):
    # bought 100 @ 300 on 1-Jan-2024; a 1:1 bonus since → 200 shares @ 150; close 200
    con.execute("INSERT INTO price_adjustments VALUES ('ACME', '2025-06-02', 0.5, 2, 'bonus', 'nse', 'Bonus 1:1')")
    lot = h.add_lot(con, "ACME", 100, 300, "2024-01-01")
    v = h.value_lot(con, {**lot}, today=TODAY)
    assert v["adj_qty"] == 200 and v["adj_price"] == 150 and v["adjusted"]
    assert v["value"] == 40000 and round(v["pnl_pct"], 2) == 33.33
    assert v["term"] == "long" and v["days_to_long"] is None and v["yearly_pct"] > 0
    assert v["bench_pct"] == pytest.approx(50.0)


def test_undated_lot_is_pnl_only_and_short_term_counts_down(con):
    v = h.value_lot(con, h.add_lot(con, "BETA", 10, 40), today=TODAY)
    assert v["pnl"] == 100 and "term" not in v and "yearly_pct" not in v
    v2 = h.value_lot(con, h.add_lot(con, "BETA", 1, 40, TODAY - timedelta(days=300)), today=TODAY)
    assert v2["term"] == "short" and v2["days_to_long"] == 66 and "yearly_pct" not in v2


def test_portfolio_groups_lots_and_totals(con):
    h.add_lot(con, "ACME", 10, 100)
    h.add_lot(con, "ACME", 10, 300)
    h.add_lot(con, "BETA", 20, 50)
    p = h.portfolio(con, today=TODAY)
    acme = next(s for s in p["stocks"] if s["symbol"] == "ACME")
    assert acme["qty"] == 20 and acme["avg_price"] == 200 and acme["pnl"] == 0 and len(acme["lots"]) == 2
    assert p["total"]["cost"] == 5000 and p["total"]["value"] == 5000 and p["total"]["n_lots"] == 3


def test_csv_broker_headers_errors_and_reimport(con, tmp_path):
    f = tmp_path / "holdings.csv"
    f.write_text("# my buys\nInstrument,Qty.,Avg. cost,Buy date\nACME,10,150,2024-01-01\nBeta Ltd,5,40,\n"
                 "NOPE,1,1,\nACME,x,1,\n", encoding="utf-8")
    h.add_lot(con, "BETA", 1, 45)                          # typed in the UI — survives imports
    r = h.sync_csv(con)
    assert r["imported"] == 2 and len(r["errors"]) == 2 and "line 5 (NOPE)" in r["errors"][0] and "isn't a number" in r["errors"][1]
    assert h.sync_csv(con)["status"] == "unchanged"
    f.write_text("symbol,qty,price\nACME,3,100\n", encoding="utf-8")
    import os
    os.utime(f, ns=(f.stat().st_atime_ns, f.stat().st_mtime_ns + 10_000_000))
    assert h.sync_csv(con)["imported"] == 1
    got = sorted((lt["symbol"], lt["qty"], lt["source"]) for lt in h.lots(con))
    assert got == [("ACME", 3, "csv"), ("BETA", 1, "ui")]
    csv_lot = next(lt for lt in h.lots(con) if lt["source"] == "csv")
    with pytest.raises(ValueError):
        h.delete_lot(con, csv_lot["id"])
    f.unlink()
    h.sync_csv(con)
    assert [lt["source"] for lt in h.lots(con)] == ["ui"]   # file removed → its lots go too


def test_csv_needs_the_core_columns(con, tmp_path):
    (tmp_path / "holdings.csv").write_text("name,price\nACME,1\n", encoding="utf-8")
    r = h.sync_csv(con)
    assert r["status"] == "error" and "qty" in r["errors"][0]


def test_web_api(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", tmp_path / "w.duckdb")
    monkeypatch.setenv("HOLDINGS_CSV", str(tmp_path / "none.csv"))
    c0 = db.connect()
    _seed(c0)
    c0.close()
    from equity_research.web.jobs import JobManager
    from equity_research.web.server import create_app

    manager = JobManager(max_workers=1)
    with TestClient(create_app(manager)) as c:
        r = c.post("/api/holdings", json={"stock": "ACME", "qty": 5, "price": 100, "date": ""})
        assert r.status_code == 200
        lot_id = r.json()["id"]
        assert c.post("/api/holdings", json={"stock": "ZZZZ", "qty": 1, "price": 1}).status_code == 400
        assert c.put(f"/api/holdings/{lot_id}", json={"qty": 6, "price": 100, "date": "2025-01-10"}).json()["ok"]
        view = c.get("/api/holdings").json()
        assert view["total"]["value"] == 1200 and view["stocks"][0]["lots"][0]["buy_date"] == "2025-01-10"
        assert view["csv"]["status"] == "none"
        assert c.delete(f"/api/holdings/{lot_id}").json()["ok"]
        assert c.delete(f"/api/holdings/{lot_id}").status_code == 404
    manager.close()


def test_no_personal_data_is_tracked_in_git():
    """The repo never carries a holdings file, a database or a .env (holdings.example.csv is the template)."""
    import re
    import shutil
    import subprocess

    if not shutil.which("git"):
        pytest.skip("git not available")
    from equity_research.common.db import _REPO_ROOT

    r = subprocess.run(["git", "ls-files"], cwd=_REPO_ROOT, capture_output=True, text=True)
    if r.returncode:
        pytest.skip("not a git checkout")
    bad = [f for f in r.stdout.splitlines()
           if re.search(r"(^|/)(holdings[^/]*\.csv|\.env)$|\.duckdb(\.wal)?$", f) and not f.endswith("holdings.example.csv")]
    assert bad == []


def test_search_by_company_name(con):
    con.execute("INSERT INTO equity_master (symbol, company_name) VALUES ('BEL', 'Bharat Electronics Limited'), "
                "('BHEL', 'Bharat Heavy Electricals Limited')")
    assert [r["symbol"] for r in h.search(con, "bharat elec")] == ["BEL", "BHEL"]
    assert [r["symbol"] for r in h.search(con, "heavy")] == ["BHEL"]
    assert h.search(con, "bel")[0]["symbol"] == "BEL"                  # the exact symbol wins
    assert h.search(con, "  ") == []


def test_watchlist_holdings_without_numbers_are_listed_to_fill(con):
    con.execute("INSERT INTO watchlist (symbol, company, list_type) VALUES ('ACME', 'Acme', 'holding'), "
                "('BETA', 'Beta', 'holding'), ('GAMMA', 'Gamma', 'tracking')")
    assert [m["name"] for m in h.missing(con)] == ["Acme Ltd", "Beta Ltd"]
    h.add_lot(con, "ACME", 1, 100)
    assert [m["symbol"] for m in h.portfolio(con)["missing"]] == ["BETA"]


def test_todays_numbers_with_an_old_date_are_flagged(con):
    """100 @ 500 bought, then a 1:5 split. As bought → 500 @ 100, no warning.
    Today's 500 @ 100 typed with the old date → split applied twice, flagged."""
    con.execute("INSERT INTO equity_master (symbol, company_name) VALUES ('SPLITCO', 'Splitco')")
    con.execute("INSERT INTO equity_eod (trade_date, symbol, series, prev_close, open, high, low, last, close, avg_price, "
                "ttl_trd_qnty, turnover_lacs, no_of_trades, deliv_qty, deliv_per) VALUES "
                "('2024-11-15', 'SPLITCO', 'EQ', 500, 500, 500, 500, 500, 500, 500, 1, 1, 1, 1, 50), "
                "(?, 'SPLITCO', 'EQ', 120, 120, 120, 120, 120, 120, 120, 1, 1, 1, 1, 50)", [TODAY])
    con.execute("INSERT INTO price_adjustments VALUES ('SPLITCO', '2025-09-01', 0.2, 5, 'split', 'nse', 'FV 10 to 2')")
    right = h.value_lot(con, h.add_lot(con, "SPLITCO", 100, 500, "2024-11-15"), today=TODAY)
    assert right["adj_qty"] == 500 and right["adj_price"] == 100 and "warn" not in right
    wrong = h.value_lot(con, h.add_lot(con, "SPLITCO", 500, 100, "2024-11-15"), today=TODAY)
    assert wrong["adj_qty"] == 2500 and "looks like today's numbers" in wrong["warn"]
    undated = h.value_lot(con, h.add_lot(con, "SPLITCO", 500, 100), today=TODAY)
    assert undated["adj_qty"] == 500 and "warn" not in undated


def test_the_check_bridges_gaps_in_price_history(con):
    """No price on the buy date (history gap) → the nearest one before the split is used."""
    con.execute("INSERT INTO equity_master (symbol, company_name) VALUES ('GAP', 'Gappy')")
    con.execute("INSERT INTO equity_eod (trade_date, symbol, series, prev_close, open, high, low, last, close, avg_price, "
                "ttl_trd_qnty, turnover_lacs, no_of_trades, deliv_qty, deliv_per) VALUES "
                "('2025-01-01', 'GAP', 'EQ', 540, 540, 540, 540, 540, 540, 540, 1, 1, 1, 1, 50), "
                "(?, 'GAP', 'EQ', 120, 120, 120, 120, 120, 120, 120, 1, 1, 1, 1, 50)", [TODAY])
    con.execute("INSERT INTO price_adjustments VALUES ('GAP', '2025-09-01', 0.2, 5, 'split', 'nse', 'FV 10 to 2')")
    assert "warn" in h.value_lot(con, h.add_lot(con, "GAP", 500, 100, "2024-11-15"), today=TODAY)
    assert "warn" not in h.value_lot(con, h.add_lot(con, "GAP", 100, 500, "2024-11-15"), today=TODAY)
