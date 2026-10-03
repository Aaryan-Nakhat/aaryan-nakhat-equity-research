"""Report reads that once misled: ownership labels, the reverse-DCF wording, small peer sets, the pledge line,
fund ranks and leaked working notes. Invented companies and figures only."""
from __future__ import annotations

from datetime import date

import duckdb

from equity_research.analysis import funds, ownership, quant
from equity_research.reports import deep_brief
from equity_research.reports.synthesize import strip_working_notes


def test_index_funds_are_passive():
    assert ownership.is_passive("Example Nifty 50 Index Fund")
    assert ownership.is_passive("Sample Bank ETF")
    assert ownership.is_passive("Example Arbitrage Fund")
    assert not ownership.is_passive("Example Flexi Cap Fund")


def test_only_discretionary_institutions_count_as_smart_money():
    f = ownership._is_discretionary_institution
    assert f("mutual fund", "", False, "Example Flexi Cap Fund")
    assert not f("mutual fund", "", False, "Example Nifty Next 50 Index Fund")        # follows an index
    assert not f("mutual fund", "", True, "Example Flexi Cap Fund")                   # promoter
    assert not f("body corporate", "LISTED company", False, "Example Holdings Ltd")   # group company
    assert not f("individual/HUF", "", False, "A Person")


def test_implied_path_is_an_average_not_perpetual():
    cagr, multiple = quant.implied_path(0.30, 0.05)
    assert 0.05 < cagr < 0.30                                  # fades from 30 % towards 5 %
    assert abs(multiple - (1 + cagr) ** quant._PROJ_YEARS) < 1e-6


def _zscores(monkeypatch, n_peers):
    peers = [f"P{i}" for i in range(n_peers)]
    vals = {"T": 15.0, **{p: float(10 + i) for i, p in enumerate(peers)}}
    monkeypatch.setattr(quant.sector, "peers", lambda _c, _s: peers)
    monkeypatch.setattr(quant.sector, "peer_industry_of", lambda _c, _s: "Example industry")
    monkeypatch.setattr(quant, "_ratios", lambda _c, s, _cons: {"ROE%": vals[s]})
    return quant.sector_zscores(None, "T")["ratios"]["ROE%"]


def test_small_peer_sets_get_a_rank_not_a_z_score(monkeypatch):
    row = _zscores(monkeypatch, 4)                               # peers 10..13, target 15
    assert row["z"] is None and row["rank"] == 1 and row["n"] == 4


def test_enough_peers_get_a_z_score(monkeypatch):
    row = _zscores(monkeypatch, quant.MIN_Z_PEERS)
    assert row["z"] is not None and row["rank"] >= 1


def test_pledge_line_for_a_company_without_promoters():
    p = (date(2026, 6, 30), 13.3, 18.3, 2.4)                     # a snapshot that misreads a promoter
    no_promoters = (date(2026, 6, 30), None, 0)
    line = deep_brief._pledge_line(p, no_promoters)
    assert "no promoter group" in line and "not promoter pledges" in line
    assert "of that is pledged" not in line


def test_pledge_line_falls_back_to_the_holder_list():
    line = deep_brief._pledge_line(None, (date(2026, 6, 30), 51.2, 1))
    assert "51.2%" in line and "n/a (no shareholding" not in line


def test_pledge_line_normal_case():
    line = deep_brief._pledge_line((date(2026, 6, 30), 60.0, 5.0, 3.0), (date(2026, 6, 30), 60.0, 3))
    assert "of that is pledged" in line


def test_fund_rank_wording(monkeypatch):
    rets = {1: 10.0, 2: 12.0, 3: 14.0, 4: 16.0, 5: 18.0}
    con = duckdb.connect()
    con.execute("CREATE TABLE mf_scheme (scheme_code INT, category VARCHAR, plan VARCHAR, option VARCHAR)")
    con.executemany("INSERT INTO mf_scheme VALUES (?, 'Flexi Cap', 'Direct', 'Growth')", [[k] for k in rets])
    monkeypatch.setattr(funds, "nav_series", lambda _c, code: code)
    monkeypatch.setattr(funds, "point_returns", lambda code: {"3y": rets[code]})
    r = funds.category_percentile(con, 2, "3y")                  # 12 % — 4th of 5
    assert r["rank"] == 4 and r["beats"] == 1 and r["top_pct"] == 80 and r["beats_pct"] == 25


def test_working_notes_are_stripped():
    text = "## Verdict\nStrong franchise.\nLet me re-list this more cleanly.\nMargins held at 20%."
    out = strip_working_notes(text)
    assert "Let me" not in out and "Margins held" in out and "Strong franchise" in out
