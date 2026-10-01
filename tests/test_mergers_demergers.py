"""Mergers and demergers in your holdings — a buy of a company that later merged is converted at the swap ratio
(the survivor's earlier bonus not applied); a demerger splits the cost by the company's filed notice (else a
labelled market estimate); the notice finder and readers. A made-up group: PARENT did a 3:1 bonus in Jan-2024
and demerged NEWCO in mid-2025 (85 % / 15 %, 1 for 1); OLDCO merged into PARENT at 3 for every 2 the same day.
Throwaway DuckDB; no network, no LLM."""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta

import pytest

from equity_research import portfolio as h
from equity_research.analysis import demerger_costs as dc
from equity_research.analysis import former_companies as fcm
from equity_research.common import db

TODAY = date(2026, 10, 1)
EX = date(2025, 6, 16)          # PARENT's demerger ex-date = OLDCO's merger record date


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setenv("HOLDINGS_CSV", str(tmp_path / "none.csv"))
    c = db.connect(tmp_path / "t.duckdb")
    c.execute("INSERT INTO equity_master (symbol, company_name) VALUES ('PARENT', 'Parentco Industries Limited'), "
              "('NEWCO', 'Newco Ventures Limited')")
    for sym, px in (("PARENT", 10.0), ("NEWCO", 30.0)):
        c.execute("INSERT INTO equity_eod (trade_date, symbol, series, prev_close, open, high, low, last, close, "
                  "avg_price, ttl_trd_qnty, turnover_lacs, no_of_trades, deliv_qty, deliv_per) "
                  "VALUES (?, ?, 'EQ', ?, ?, ?, ?, ?, ?, ?, 1, 1, 1, 1, 50)", [TODAY, sym] + [px] * 7)
    c.execute("INSERT INTO price_adjustments VALUES ('PARENT', '2024-01-02', 0.25, 4, 'bonus', 'nse', 'Bonus 3:1'), "
              "('PARENT', ?, 0.4, 1, 'demerger', 'nse', 'Demerger')", [EX])
    yield c
    c.close()


def _filed(c):
    c.execute("INSERT INTO demerger_costs VALUES ('PARENT', ?, 'NEWCO', 'Newco Ventures Limited', 15, 1, 1, "
              "'filing', 'http://notice.pdf', now())", [EX])


# ------------------------------------------------------------------ received date
def test_received_shares_skip_the_companys_earlier_bonus(con):
    v = h.value_lot(con, h.add_lot(con, "PARENT", 150, 10000 / 150, "2023-06-01", received=EX), today=TODAY)
    assert v["adj_qty"] == 150 and v["adjusted"] == [] and v["demergers"] == []
    assert v["term"] == "long" and v["cost"] == pytest.approx(10000)
    wrong = h.value_lot(con, h.add_lot(con, "PARENT", 150, 10000 / 150, "2023-06-01"), today=TODAY)
    assert wrong["adj_qty"] == 600                                    # without it the bonus would be applied


def test_received_date_is_validated(con):
    with pytest.raises(ValueError):
        h.add_lot(con, "PARENT", 1, 1, "2025-01-01", received="2024-01-01")    # before the buy


# ------------------------------------------------------------------ demergers
def test_demerger_uses_the_filed_split_and_offers_the_new_shares(con):
    _filed(con)
    lot = h.add_lot(con, "PARENT", 100, 100, "2023-06-01")          # 100 @ 100 → bonus → 400 @ 25, cost 10,000
    p = h.portfolio(con, today=TODAY)
    v = p["stocks"][0]["lots"][0]
    assert v["adj_qty"] == 400 and v["cost"] == pytest.approx(8500)  # 85 % stays
    dm = v["demergers"][0]
    assert dm["basis"] == "filing" and dm["parent_pct"] == pytest.approx(85)
    child = dm["children"][0]
    assert child["symbol"] == "NEWCO" and child["qty"] == 400 and child["cost"] == pytest.approx(1500)
    assert child["have"] is False and p["lookups"] == []
    h.add_lot(con, "NEWCO", child["qty"], child["price"], lot["buy_date"], received=EX)
    p2 = h.portfolio(con, today=TODAY)
    newco = next(s for s in p2["stocks"] if s["symbol"] == "NEWCO")
    assert newco["cost"] == pytest.approx(1500) and newco["lots"][0]["term"] == "long"
    assert next(s for s in p2["stocks"] if s["symbol"] == "PARENT")["lots"][0]["demergers"][0]["children"][0]["have"]
    assert p2["total"]["cost"] == pytest.approx(10000)                # nothing lost or double-counted


def test_no_notice_yet_uses_a_labelled_market_estimate_and_asks_for_a_lookup(con):
    h.add_lot(con, "PARENT", 100, 100, "2023-06-01")
    p = h.portfolio(con, today=TODAY)
    v = p["stocks"][0]["lots"][0]
    assert v["demergers"][0]["basis"] == "looking" and v["cost"] == pytest.approx(4000)
    assert p["lookups"] == [("PARENT", "Parentco Industries Limited", EX)]
    con.execute("INSERT INTO demerger_costs VALUES ('PARENT', ?, NULL, NULL, NULL, NULL, NULL, 'none', NULL, now())",
                [EX])
    p = h.portfolio(con, today=TODAY)
    assert p["stocks"][0]["lots"][0]["demergers"][0]["basis"] == "estimate" and p["lookups"] == []


def test_a_buy_after_the_demerger_is_untouched(con):
    _filed(con)
    v = h.value_lot(con, h.add_lot(con, "PARENT", 10, 12, "2026-01-05"), today=TODAY)
    assert v["demergers"] == [] and v["cost"] == 120


def test_not_found_is_retried_after_a_week(con):
    con.execute("INSERT INTO demerger_costs VALUES ('PARENT', ?, NULL, NULL, NULL, NULL, NULL, 'none', NULL, ?)",
                [EX, datetime.now() - timedelta(days=dc.RETRY_DAYS + 1)])
    assert dc.known(con, "PARENT", EX) is None


@pytest.mark.parametrize("text, hit", [
    ("General Updates PARENT_27112025173123_Cost_of_aquisition_Signed.pdf", True),     # the typo is real-world
    ("Apportionment of Cost of Acquisition of Equity Shares", True),
    ("PARENT_22052023214418_Covering_COA_Announcement.pdf", True),
    ("Transcript of the earnings call", False),
])
def test_the_notice_finder(text, hit):
    assert bool(dc._NOTICE.search(text)) is hit


def test_the_notice_reader_keeps_only_splits_that_add_up(monkeypatch):
    from equity_research.common import llm
    from equity_research.reports import synthesize

    good = {"parent_pct": "85%", "resulting": [{"name": "Newco Ventures Limited", "pct": 15,
                                                "ratio_new": 1, "ratio_old": 1}], "source_id": "f1"}
    monkeypatch.setattr(llm, "generate", lambda *a, **k: json.dumps(good))
    got = synthesize.demerger_cost_split("Parentco", EX, {"F1": "notice"})
    assert got["parent_pct"] == 85 and got["resulting"][0]["pct"] == 15 and got["source_id"] == "F1"
    monkeypatch.setattr(llm, "generate", lambda *a, **k: json.dumps({**good, "parent_pct": 50}))   # 50 + 15 ≠ 100
    assert synthesize.demerger_cost_split("Parentco", EX, {"F1": "notice"}) == {}


def test_csv_received_column(con, tmp_path):
    f = tmp_path / "holdings.csv"
    f.write_text(f"symbol,qty,price,date,received\nParentco Industries,150,66.67,2023-06-01,{EX}\n", encoding="utf-8")
    os.environ["HOLDINGS_CSV"] = str(f)
    assert h.sync_csv(con)["imported"] == 1
    assert h.lots(con)[0]["received"] == EX


# ------------------------------------------------------------------ former companies (merged away)
def _oldco(c, *, terms=True):
    c.execute("INSERT INTO equity_eod (trade_date, symbol, series, prev_close, open, high, low, last, close, avg_price, "
              "ttl_trd_qnty, turnover_lacs, no_of_trades, deliv_qty, deliv_per) "
              "VALUES ('2025-06-13', 'OLDCO', 'EQ', 60, 60, 60, 60, 60, 60, 60, 1, 1, 1, 1, 50)")
    c.execute("INSERT INTO former_companies (symbol, name, last_traded, merger_date, fetched_at) "
              "VALUES ('OLDCO', 'Oldco Logistics Limited', '2025-06-13', ?, now())", [EX])
    if terms:
        c.execute("UPDATE former_companies SET into_symbol = 'PARENT', into_name = 'Parentco Industries Limited', "
                  "ratio_new = 3, ratio_old = 2, source = 'filing', url = 'http://scheme.pdf'")


def test_parse_nse_record_finds_name_and_merger_date():
    rows = [{"comp": "Oldco Logistics Limited", "isin": "INE000X01011", "subject": "Merger", "recDate": "16-Jun-2025",
             "exDate": "-"}, {"comp": "Oldco Logistics Limited", "subject": "Annual General Meeting", "exDate": "02-Sep-2024"}]
    assert fcm.parse_actions(rows) == {"name": "Oldco Logistics Limited", "isin": "INE000X01011", "merger_date": EX}
    assert fcm.parse_actions([])["name"] is None


def test_stopped_trading_symbols_are_candidates(con):
    _oldco(con)
    con.execute("DELETE FROM former_companies")
    assert [s for s, _ in fcm.candidates(con)] == ["OLDCO"]            # PARENT / NEWCO still trade


def test_old_company_is_searchable_by_name(con):
    _oldco(con)
    hit = [r for r in h.search(con, "oldco logistics") if r.get("former")]
    assert hit and hit[0]["symbol"] == "OLDCO" and "merged into Parentco Industries" in hit[0]["note"]


def test_an_old_company_buy_becomes_survivor_shares(con):
    _oldco(con)
    _filed(con)                                  # PARENT's own demerger the same day — not OLDCO holders'
    h.add_lot(con, "oldco logistics", 100, 120, "2023-06-01")         # entered exactly as bought
    assert con.execute("SELECT list_type FROM watchlist WHERE symbol = 'PARENT'").fetchone()[0] == "holding"
    p = h.portfolio(con, today=TODAY)
    v = next(x for x in p["stocks"] if x["symbol"] == "PARENT")["lots"][0]
    assert v["adj_qty"] == 150 and v["cost"] == pytest.approx(12000)  # 3 for 2; PARENT's 2024 bonus not applied
    assert v["demergers"] == [] and v["term"] == "long" and v["buy_date"] == "2023-06-01"
    assert v["via"]["name"] == "Oldco Logistics Limited" and v["qty"] == 100 and v["price"] == 120
    assert v["value"] == pytest.approx(1500)


def test_unknown_terms_ask_for_a_lookup(con):
    _oldco(con, terms=False)
    h.add_lot(con, "OLDCO", 100, 120, "2023-06-01")
    p = h.portfolio(con, today=TODAY)
    assert p["merger_lookups"] == ["OLDCO"] and p["lookups"] == []
    assert "reading its filings" in p["stocks"][0]["lots"][0]["note"]


def test_the_survivor_leaves_the_fill_in_list_once_held_via_the_old_company(con):
    _oldco(con)
    con.execute("INSERT INTO watchlist (symbol, company, list_type) VALUES ('PARENT', 'Parentco', 'holding')")
    assert [m["symbol"] for m in h.missing(con)] == ["PARENT"]
    h.add_lot(con, "oldco logistics", 40, 75, "2024-03-15")
    assert h.missing(con) == []


def test_the_merger_reader(monkeypatch):
    from equity_research.common import llm
    from equity_research.reports import synthesize

    monkeypatch.setattr(llm, "generate", lambda *a, **k: json.dumps(
        {"into_name": "Parentco Industries Limited", "ratio_new": "3", "ratio_old": 2, "source_id": "f2"}))
    assert synthesize.merger_terms("Oldco", EX, {"F2": "scheme"}) == {
        "into_name": "Parentco Industries Limited", "ratio_new": 3.0, "ratio_old": 2.0, "source_id": "F2"}
    monkeypatch.setattr(llm, "generate", lambda *a, **k: json.dumps({"into_name": "X", "ratio_new": 0}))
    assert synthesize.merger_terms("Oldco", EX, {"F2": "scheme"}) == {}
