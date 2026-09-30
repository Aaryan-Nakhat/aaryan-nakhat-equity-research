"""🔍 Reality Check — reading a post, the command, the anti-hallucination guard and the verdict rules.
No network: fetches and the LLM are stubbed."""

from __future__ import annotations

import json

import pytest

from equity_research.analysis import reality_check as rc
from equity_research.reports import synthesize
from equity_research.scrapers import post_fetch


# ------------------------------------------------------------------ reading the post
def test_split_input_finds_the_link_and_keeps_the_words():
    url, rest = post_fetch.split_input("is this true? https://x.com/a/status/1). thanks")
    assert url == "https://x.com/a/status/1" and rest == "is this true?  thanks"
    assert post_fetch.split_input("BEL wins ₹500 cr order") == (None, "BEL wins ₹500 cr order")


def test_x_post_via_oembed(monkeypatch):
    html = ('<blockquote class="twitter-tweet"><p lang="en">BEL bags &amp; wins ₹500 cr order 🚀</p>'
            '&mdash; Some Guy (@guy) <a href="https://x.com/guy/status/9">September 17, 2026</a></blockquote>')
    monkeypatch.setattr(post_fetch, "fetch_text", lambda url, **k: json.dumps({"html": html, "author_name": "Some Guy"}))
    p = post_fetch.fetch_post("https://x.com/guy/status/9")
    assert (p.kind, p.text, p.author, p.published) == ("x", "BEL bags & wins ₹500 cr order 🚀", "Some Guy",
                                                       "September 17, 2026")


def test_videos_are_declined_politely():
    p = post_fetch.fetch_post("https://www.youtube.com/shorts/abc")
    assert p.kind == "unsupported" and "paste the caption" in p.note
    assert post_fetch.fetch_post("").kind == "error"


# ------------------------------------------------------------------ the command
@pytest.mark.parametrize("subject, body, want", [
    ("reality check: https://x.com/a/status/1", "", "https://x.com/a/status/1"),
    ("reality_check: BEL wins order", "", "BEL wins order"),
    ("Reality Check", "https://example.com/story", "https://example.com/story"),
    ("verify: HDFC profit up 60%", "", "HDFC profit up 60%"),
    ("tip: buy XYZ", "", "buy XYZ"),
    ("tip", "", None),                      # a bare word is not a request
    ("reliance", "", None),
])
def test_reality_query(subject, body, want):
    from equity_research.bot import app
    from equity_research.reports.inbox import EmailRequest

    req = EmailRequest(uid=1, sender="me@x", subject=subject, body=body, message_id="<m>", references="")
    assert app._reality_query(req) == want


# ------------------------------------------------------------------ the judge can't invent evidence
def test_verify_downgrades_a_confirmation_without_real_evidence(monkeypatch):
    fake = json.dumps([{"claim": 1, "status": "confirmed", "evidence": "F9", "note": "yes"},
                       {"claim": 2, "status": "contradicted", "evidence": "C1", "note": "+19%, not +60%"}])
    monkeypatch.setattr(synthesize.llm, "generate", lambda *a, **k: fake)
    out = synthesize.reality_verify([{"text": "order won"}, {"text": "profit +60%"}],
                                    {"F1": "order filing", "C1": "profit +19% YoY"})
    assert out[0]["status"] == "not_found" and out[0]["evidence"] == ""     # F9 doesn't exist
    assert out[1]["status"] == "contradicted" and out[1]["evidence"] == "C1"


# ------------------------------------------------------------------ verdict rules
def _co(sym="ACME", **kw):
    c = rc.Company(sym, sym)
    for k, v in kw.items():
        setattr(c, k, v)
    return c


@pytest.mark.parametrize("claims, co, hype, label", [
    ([], _co(), [], "ℹ️ No specific stock claim"),
    ([{"symbols": ["ACME"], "status": "contradicted"}], _co(), [], "❌ Contradicted"),
    ([{"symbols": ["ACME"], "status": "partly"}], _co(), [], "🟡 Partly true"),
    ([{"symbols": ["ACME"], "status": "confirmed", "pct_rev": 12.0}], _co(moves={"ex20": 2.0}), [],
     "✅ Real and material"),
    ([{"symbols": ["ACME"], "status": "confirmed", "pct_rev": 12.0}], _co(moves={"ex20": 22.0}), [],
     "🟡 Real — but the stock has already moved"),
    ([{"symbols": ["ACME"], "status": "confirmed", "pct_rev": 1.0, "pct_mcap": 0.2}], _co(), [],
     "🟢 Real, but small"),
    ([{"symbols": ["ACME"], "status": "confirmed"}], _co(), [], "✅ Confirmed — size not stated"),
    ([{"symbols": ["ACME"], "status": "not_found"}], _co(), ["multibagger"], "🚩 Looks like hype"),
    ([{"symbols": ["ACME"], "status": "not_found"}], _co(), [], "⚠️ Unconfirmed"),
])
def test_verdict(claims, co, hype, label):
    assert rc.verdict(claims, {"ACME": co}, hype, True)[0] == label


def test_red_flags():
    co = _co(mcap_cr=400, moves={"turnover_cr": 0.3, "ex20": 40.0, "vol_x": 5.0}, pledge_pct=60.0,
             promoter_sold_cr=12.0)
    f = rc._flags(co, [])
    assert len(f) == 6 and any("Micro-cap" in x for x in f) and any("pledged" in x for x in f)
