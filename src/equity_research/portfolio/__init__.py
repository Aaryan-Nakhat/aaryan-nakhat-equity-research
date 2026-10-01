"""💼 Your portfolio — buys, sells, value, tax lots, realised gains, dividends and XIRR. Local only.

Modules: ``store`` (buys / sells, company search) · ``csvio`` (holdings.csv) · ``instruments`` (ETFs, SME,
REITs, InvITs, BSE-only shares and their prices) · ``timeline`` (one stock's history replayed → tax lots) ·
``valuation`` (the whole portfolio) · ``income`` (dividends, XIRR) · ``tax`` (Indian capital-gains rules).
See docs/TECHNICAL.md → "Your holdings".

**How to enter a buy** (repeated in the UI, the emails and the CSV template): with a date, the quantity and
price as you bought them (splits / bonuses since are applied); without one, what your broker shows today.
"""

from equity_research.portfolio.csvio import csv_path, read_csv, sync_csv
from equity_research.portfolio.store import (add_lot, add_sell, delete_lot, delete_sell, lots, missing, parse_date,
                                             resolve, search, sells, update_lot)
from equity_research.portfolio.valuation import portfolio, realised_this_year, value_lot

HOW_TO_ENTER = ("With a date → enter the quantity and price as you bought them (your contract note); splits and "
                "bonuses since then are applied for you — e.g. 100 @ ₹500 bought before a 1:5 split shows as "
                "500 @ ₹100. Without a date → enter what your broker shows today (quantity and average price). "
                "Don't mix them: today's quantity with an old date counts the split twice.")

__all__ = ["HOW_TO_ENTER", "add_lot", "add_sell", "csv_path", "delete_lot", "delete_sell", "lots", "missing",
           "parse_date", "portfolio", "read_csv", "realised_this_year", "resolve", "search", "sells", "sync_csv",
           "update_lot", "value_lot"]
