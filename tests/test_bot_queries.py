"""Every command parser: the subject (or web / CLI command) → the request it means, and plain stock names
falling through to the deep report. Pure functions — no database, no network."""

from __future__ import annotations

import pytest

from equity_research.bot import queries as q


@pytest.mark.parametrize("subject, want", [
    ("screen", "value"), ("screen: value", "value"), ("Re: screen: quality", "quality"),
    ("screen: technical", "technical"), ("screen: hot list", "hotlist"), ("screen: debt payers", "deleverage"),
    ("screen: momentum", "unknown"),            # not a screen → the reply lists them (never a silent value run)
    ("reliance", None),
])
def test_screen(subject, want):
    assert q._screen_query(subject) == want


@pytest.mark.parametrize("subject, want", [
    ("ipo", ("list", "ongoing")), ("IPOs", ("list", "ongoing")), ("ipo: upcoming", ("list", "upcoming")),
    ("ipo: live", ("list", "ongoing")), ("ipo: acme foods", ("name", "acme foods")), ("ipox", None),
])
def test_ipo(subject, want):
    assert q._ipo_query(subject) == want


@pytest.mark.parametrize("subject, want", [
    ("alert: order win", ("add", "order win")), ("alerts", ("list", "")), ("unalert: order win", ("remove", "order win")),
    ("alert clear", ("clear", "")), ("alertness", None),
])
def test_alert(subject, want):
    assert q._alert_query(subject) == want


@pytest.mark.parametrize("fn, yes, no", [
    ("_booking_query", ["booking", "profit booking"], ["reliance"]),
    ("_policy_query", ["policy", "schemes"], ["policybazaar"]),
    ("_tailwind_query", ["tailwind"], ["tailwinds india ltd"]),
    ("_pickaxe_query", ["pickaxe", "demand"], ["demandware"]),
    ("_hotlist_query", ["hotlist"], ["hot"]),
    ("_calls_query", ["concalls", "calls"], ["callisto media"]),
    ("_scorecard_query", ["scorecard"], ["score"]),
    ("_results_query", ["results"], ["result industries"]),
    ("_help_query", ["help", "commands"], ["helpful"]),
    ("_sell_query", ["sell", "trim", "raise"], ["sellwin traders"]),
])
def test_word_commands(fn, yes, no):
    f = getattr(q, fn)
    assert all(f(s) for s in yes), [s for s in yes if not f(s)]
    assert not any(f(s) for s in no), [s for s in no if f(s)]


@pytest.mark.parametrize("fn, subject, want", [
    ("_sector_query", "sector: pharma", "pharma"), ("_suppliers_query", "suppliers: acme motors", "acme motors"),
    ("_investor_query", "investor: some investor", "some investor"), ("_levels_query", "levels: infy", "infy"),
    ("_fund_query", "fund: some flexi cap", "some flexi cap"),
])
def test_named_commands(fn, subject, want):
    assert getattr(q, fn)(subject) == want and getattr(q, fn)("acme motors") is None
