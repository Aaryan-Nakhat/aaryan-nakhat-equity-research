"""Unit tests for the technical levels engine (analysis/technical.py).

Pure-function checks plus an end-to-end ``levels()`` run over a synthetic price series in
an in-memory DuckDB — no network, no real data store.
"""

from __future__ import annotations

import duckdb
import numpy as np
import pandas as pd
import pytest

from equity_research.analysis import technical as T


def _make_con(closes: list[float]) -> duckdb.DuckDBPyConnection:
    """In-memory DuckDB with an ``equity_eod`` table holding a daily series for 'TEST'."""
    con = duckdb.connect(":memory:")
    con.execute(
        "CREATE TABLE equity_eod (symbol VARCHAR, series VARCHAR, trade_date DATE, "
        "open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, ttl_trd_qnty DOUBLE, deliv_per DOUBLE)")
    start = np.datetime64("2024-01-01")
    rows = []
    for i, c in enumerate(closes):
        d = str(start + np.timedelta64(i, "D"))
        rows.append(("TEST", "EQ", d, c, c * 1.01, c * 0.99, c, 100000.0, 55.0))
    con.executemany(
        "INSERT INTO equity_eod VALUES (?,?,?,?,?,?,?,?,?)", rows)
    return con


def _oscillating_series(n: int = 300) -> list[float]:
    """A gently rising price with regular swing highs/lows — gives the pivot/zone code
    real structure to find (deterministic, no randomness)."""
    x = np.arange(n)
    return list(100 + 0.05 * x + 8 * np.sin(x / 9.0))


def test_round_levels_positive_and_near():
    for price in (9.82, 128.0, 1290.0, 2642.0):
        levels = T._round_levels(price)
        assert levels, f"no round levels for {price}"
        assert all(x > 0 for x in levels), "round levels must be positive"


def test_cluster_is_center_bounded():
    # a dense chain 100..110 must NOT collapse into one 10-wide zone (the chaining bug)
    cands = [(100.0 + 0.2 * i, 1.0, "swing") for i in range(51)]  # 100.0 .. 110.0
    zones = T._cluster(cands, tol=1.0)
    assert len(zones) > 1
    assert all(z["hi"] - z["lo"] <= 3.0 for z in zones), "a zone drifted far past tol"


def test_swings_finds_extrema():
    s = pd.Series([1, 2, 3, 2, 1, 2, 3, 4, 3, 2] * 3)
    highs, lows = T._swings(s, s, k=2)
    assert highs and lows


def test_levels_end_to_end():
    con = _make_con(_oscillating_series())
    lv = T.levels(con, "TEST")
    assert lv["history_ok"] and lv["reliable"]
    # supports strictly below price, resistances strictly above
    price = lv["close"]
    assert all(z["mid"] < price for z in lv["supports"])
    assert all(z["mid"] > price for z in lv["resistances"])
    # a setup is always produced with a note
    assert lv["setup"].get("note")


def test_levels_thin_history():
    con = _make_con([100.0 + i for i in range(30)])   # < _MIN_DAYS
    lv = T.levels(con, "TEST")
    assert lv["history_ok"] is False


def test_levels_verdict_deference():
    con = _make_con(_oscillating_series())
    lv = T.levels(con, "TEST", verdict="AVOID")
    assert lv["setup"]["kind"] == "reference-only"
    assert "AVOID" in lv["setup"]["note"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
