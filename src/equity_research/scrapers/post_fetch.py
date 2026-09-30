"""Fetch the text of a post someone wants reality-checked: a news article, an X/Twitter post, a Reddit
post — or text pasted straight in. Returns plain text for the claim extractor; never interprets it.

* **X / Twitter** — X's public oEmbed endpoint (``publish.twitter.com/oembed``, no login) returns the
  post's text and date.
* **Reddit** — plain requests are refused, so the post page is loaded in the headless browser
  (scrapling ``StealthyFetcher``) and its text extracted.
* **Articles** — plain HTTP + ``trafilatura`` (main-text extraction), with the headless browser as a
  fallback for pages that only render in one.
* **YouTube / Instagram** — not read yet (video; Instagram needs a login): the reply asks for the
  caption or claim to be pasted instead.
"""

from __future__ import annotations

import html as _html
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import quote, urlparse

from equity_research.common.http import fetch_text

log = logging.getLogger(__name__)

_URL = re.compile(r"https?://\S+", re.I)
_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"}


@dataclass
class Post:
    kind: str                  # article · x · reddit · text · unsupported · error
    text: str
    url: str = ""
    title: str = ""
    author: str = ""
    published: str = ""        # as given by the source (free text); "" when unknown
    note: str = ""             # why it couldn't be read, for the reply


class Unsupported(Exception):
    pass


def split_input(raw: str) -> tuple[str | None, str]:
    """(the first URL in the input or None, the input with that URL removed)."""
    m = _URL.search(raw or "")
    if not m:
        return None, (raw or "").strip()
    url = m.group(0).rstrip(").,>'\"")
    return url, (raw[:m.start()] + raw[m.end():]).strip()


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.").removeprefix("m.")


def _x(url: str) -> Post:
    data = json.loads(fetch_text("https://publish.twitter.com/oembed?omit_script=1&url=" + quote(url, safe=""),
                                 headers=_UA))
    raw = data.get("html", "")
    body = re.search(r"<p[^>]*>(.*?)</p>", raw, re.S)
    text = _html.unescape(re.sub(r"<[^>]+>", " ", body.group(1) if body else raw))
    dates = re.findall(r">([A-Z][a-z]+ \d{1,2}, \d{4})</a>", raw)
    return Post("x", " ".join(text.split()), url=url, author=data.get("author_name", ""),
                published=dates[-1] if dates else "")


def _extract(page_html: str, url: str) -> tuple[str, str, str]:
    """(title, text, date) from an HTML page via trafilatura."""
    import trafilatura

    doc = trafilatura.bare_extraction(page_html, url=url, with_metadata=True, include_comments=False)
    if not doc:
        return "", "", ""
    get = doc.get if isinstance(doc, dict) else (lambda k, d=None: getattr(doc, k, d))
    return get("title") or "", get("text") or "", get("date") or ""


def _browser_html(url: str) -> str:
    from scrapling.fetchers import StealthyFetcher

    page = StealthyFetcher.fetch(url, headless=True, network_idle=True, timeout=60_000)
    if page.status != 200:
        raise RuntimeError(f"page returned HTTP {page.status}")
    return page.html_content


def _reddit(url: str) -> Post:
    title, text, date = _extract(_browser_html(url), url)
    if not text:
        raise RuntimeError("no post text found on the page")
    return Post("reddit", text, url=url, title=title, published=date)


def _article(url: str) -> Post:
    html = ""
    try:
        html = fetch_text(url, headers=_UA)
    except Exception as e:  # noqa: BLE001 — some sites refuse plain requests; try a real browser
        log.info("reality check: plain fetch of %s failed (%s) — trying the browser", url, e)
    title, text, date = _extract(html, url) if html else ("", "", "")
    if len(text) < 200:                                  # blocked, paywalled or script-rendered
        try:
            title2, text2, date2 = _extract(_browser_html(url), url)
            if len(text2) > len(text):
                title, text, date = title2 or title, text2, date2 or date
        except Exception:  # noqa: BLE001
            log.info("reality check: browser fetch of %s failed too", url)
    if not text:
        raise RuntimeError("couldn't read the article text (paywalled or blocked?)")
    return Post("article", text, url=url, title=title, published=date)


def fetch_post(raw: str) -> Post:
    """Read what's to be checked. ``raw`` is a link (with or without extra words) or pasted text.
    Never raises: failures come back as kind ``error`` / ``unsupported`` with a note."""
    url, rest = split_input(raw)
    if not url:
        return Post("text", rest) if rest else Post("error", "", note="nothing to check")
    host = _host(url)
    try:
        if host in ("x.com", "twitter.com", "mobile.twitter.com") and "/status/" in url:
            post = _x(url)
        elif host.endswith("reddit.com") or host == "redd.it":
            post = _reddit(url)
        elif host.endswith(("youtube.com", "youtu.be", "instagram.com", "facebook.com", "fb.watch")):
            raise Unsupported(host)
        else:
            post = _article(url)
    except Unsupported:
        return Post("unsupported", rest, url=url,
                    note="videos and reels can't be read yet — paste the caption or the claim as text")
    except Exception as e:  # noqa: BLE001
        log.info("reality check: couldn't read %s: %s", url, e)
        return Post("error", rest, url=url, note=f"couldn't read that link ({e})")
    if rest:                                             # the user's own words ride along
        post.text = f"{post.text}\n\n[The person asking added: {rest}]"
    return post


def parse_date(s: str) -> datetime | None:
    for fmt in ("%Y-%m-%d", "%B %d, %Y", "%b %d, %Y", "%d %B %Y", "%d %b %Y"):
        try:
            return datetime.strptime((s or "").strip()[:20], fmt)
        except ValueError:
            continue
    return None
