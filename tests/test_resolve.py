"""Name resolution: the deterministic local layer (exact symbol / unique name / group name → always a
list) and its fallback to the LLM. In-memory DuckDB + a fake LLM — no network."""

from __future__ import annotations

import duckdb
import pytest

from equity_research.reports import resolve as R

_MASTER = [
    ("HDFCBANK", "HDFC Bank Limited"), ("HDFCLIFE", "HDFC Life Insurance Company Limited"),
    ("HDFCAMC", "HDFC Asset Management Company Limited"), ("INFY", "Infosys Limited"),
    ("HCL-INSYS", "HCL Infosystems Limited"), ("SBIN", "State Bank of India"),
    ("SBICARD", "SBI Cards and Payment Services Limited"), ("SBILIFE", "SBI Life Insurance Company Limited"),
    ("ITC", "ITC Limited"), ("KILITCH", "Kilitch Drugs (India) Limited"),
    ("BAJAJ-AUTO", "Bajaj Auto Limited"), ("BAJFINANCE", "Bajaj Finance Limited"),
]
# avg traded value: HDFCBANK ≫ HDFCLIFE ≫ HDFCAMC
_VALUE = {"HDFCBANK": 900.0, "HDFCLIFE": 90.0, "HDFCAMC": 9.0, "SBICARD": 5.0, "SBILIFE": 50.0}


@pytest.fixture
def con():
    c = duckdb.connect(":memory:")
    c.execute("CREATE TABLE equity_master (symbol VARCHAR, company_name VARCHAR, isin VARCHAR, "
              "updated_at TIMESTAMP)")
    c.executemany("INSERT INTO equity_master VALUES (?, ?, NULL, NULL)", _MASTER)
    c.execute("CREATE TABLE equity_eod (symbol VARCHAR, trade_date DATE, close DOUBLE, "
              "ttl_trd_qnty DOUBLE)")
    c.executemany("INSERT INTO equity_eod VALUES (?, DATE '2026-09-24', ?, 1)", list(_VALUE.items()))
    return c


@pytest.fixture
def llm(monkeypatch):
    """Fake LLM: returns whatever the test sets; records whether it was called."""
    state = {"reply": "[]", "calls": 0}

    def fake(system, query, grounded=False):
        state["calls"] += 1
        return state["reply"]
    monkeypatch.setattr(R.llm, "generate", fake)
    return state


def syms(cands):
    return [c.symbol for c in cands]


@pytest.mark.parametrize("q, want", [("infy", "INFY"), ("SBIN", "SBIN"), ("bajaj-auto", "BAJAJ-AUTO"),
                                     ("itc", "ITC"), ("hdfc amc", "HDFCAMC"), ("hdfc life", "HDFCLIFE")])
def test_exact_symbol_goes_straight_without_llm(con, llm, q, want):
    assert syms(R.resolve(q, con)) == [want] and llm["calls"] == 0


@pytest.mark.parametrize("q, want", [("infosys", "INFY"), ("Infosys Ltd.", "INFY"),
                                     ("hdfc bank", "HDFCBANK"), ("state bank", "SBIN")])
def test_unique_name_goes_straight_without_llm(con, llm, q, want):
    # whole-word prefix: 'infosys' must not also match 'HCL Infosystems'
    assert syms(R.resolve(q, con)) == [want] and llm["calls"] == 0


def test_group_name_always_lists_even_if_llm_is_sure(con, llm):
    llm["reply"] = '[{"symbol": "HDFCBANK", "name": "HDFC Bank", "exchange": "NSE"}]'
    assert syms(R.resolve("hdfc", con)) == ["HDFCBANK", "HDFCLIFE", "HDFCAMC"]


def test_group_list_leads_with_llm_pick_and_adds_names_a_prefix_cannot_see(con, llm):
    llm["reply"] = '[{"symbol": "SBIN", "name": "State Bank of India", "exchange": "NSE"}]'
    assert syms(R.resolve("sbi", con)) == ["SBIN", "SBILIFE", "SBICARD"]


def test_group_list_survives_llm_failure(con, llm):
    llm["reply"] = "sorry, no idea"
    assert syms(R.resolve("hdfc", con)) == ["HDFCBANK", "HDFCLIFE", "HDFCAMC"]


def test_no_local_match_falls_back_to_llm(con, llm):
    llm["reply"] = '[{"symbol": "HDFCAMC", "name": "HDFC AMC", "exchange": "NSE"}]'
    assert syms(R.resolve("hdfc mutual fund company", con)) == ["HDFCAMC"] and llm["calls"] == 1


# ----------------------------- stale / renamed LLM symbols -----------------------------
@pytest.fixture
def big_con(con):
    """A master big enough (≥ _MIN_MASTER) that LLM picks are validated against it."""
    con.executemany("INSERT INTO equity_master VALUES (?, ?, NULL, NULL)",
                    [(f"FILL{i}", f"Filler Company {i} Limited") for i in range(R._MIN_MASTER)]
                    + [("ETERNAL", "ETERNAL LIMITED"),
                       ("TMPV", "Tata Motors Passenger Vehicles Limited")])
    return con


@pytest.fixture
def renames(monkeypatch):
    table = {"ZOMATO": "ETERNAL", "TELCO": "TATAMOTORS", "TATAMOTORS": "TMPV"}
    monkeypatch.setattr(R, "_renames", lambda: table)
    return table


def test_llm_stale_symbols_are_remapped_and_dead_ones_dropped(big_con, llm, renames):
    llm["reply"] = ('[{"symbol": "ZOMATO", "name": "Zomato"}, {"symbol": "TELCO", "name": "Telco"},'
                    ' {"symbol": "ISEC", "name": "ICICI Securities"}]')
    # ZOMATO → ETERNAL; TELCO → TATAMOTORS → TMPV (chain); ISEC is delisted → dropped
    got = R.resolve("some food delivery app", big_con)
    assert syms(got) == ["ETERNAL", "TMPV"] and got[0].name == "ETERNAL LIMITED"


def test_old_ticker_query_resolves_via_rename_without_llm(big_con, llm, renames):
    assert syms(R.resolve("zomato", big_con)) == ["ETERNAL"] and llm["calls"] == 0


def test_symbol_change_csv_parsing(monkeypatch):
    from equity_research.scrapers import nse_archives

    csv = (" ETERNAL LIMITED,ZOMATO,ETERNAL,09-APR-2025\n"
           "Tata Motors Limited,TELCO,TATAMOTORS,26-DEC-2003\n"
           "Tata Motors Passenger Vehicles Limited,TATAMOTORS,TMPV,24-OCT-2025\n"
           "Foo, Bar & Co Limited,FOOOLD,FOONEW,01-JAN-2020\n"      # comma inside the name
           "Foo Bar Limited,FOOOLD,FOONEWER,01-JAN-2022\n"          # later change wins
           "garbage line\n")
    monkeypatch.setattr(nse_archives, "fetch_bytes", lambda url: csv.encode())
    assert nse_archives.fetch_symbol_changes() == {
        "ZOMATO": "ETERNAL", "TELCO": "TATAMOTORS", "TATAMOTORS": "TMPV", "FOOOLD": "FOONEWER"}


def test_unreadable_db_falls_back_to_llm(llm, monkeypatch):
    from equity_research.common import db

    def busy(*a, **k):
        raise duckdb.IOException("being used by another process")
    monkeypatch.setattr(db, "connect", busy)
    llm["reply"] = '[{"symbol": "INFY", "name": "Infosys", "exchange": "NSE"}]'
    assert syms(R.resolve("infosys")) == ["INFY"]
