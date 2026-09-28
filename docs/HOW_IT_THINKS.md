# 🧠 How it thinks

Most "AI stock analysts" ask a language model about a company and print what it says. This one works the
other way round: **Python computes every number from primary filings, and the model is only allowed to
read, triage and write — with every claim it makes checked against something real.** This page shows the
three places that matters most.

← back to the [README](../README.md) · every formula: [METHODOLOGY.md](METHODOLOGY.md) · every command: [COMMANDS.md](COMMANDS.md)

## 1. A deep report: numbers first, words last

```mermaid
flowchart LR
    A["You: <b>adani power</b>"] --> R{"Name → NSE symbol<br/><i>exact / renamed symbol,<br/>unique company name</i>"}
    R -- "one match" --> F
    R -- "several<br/>(adani, hdfc, tata…)" --> P["Numbered list<br/>→ you pick"] --> F
    R -- "no clean match" --> L["LLM guess<br/>→ checked against NSE's<br/>official symbol-change list"] --> F
    F["Fetch primary data<br/>XBRL financials · shareholding ·<br/>insider trades · prices · filings"] --> Q
    subgraph Q ["Deterministic Python — no LLM"]
        direction TB
        Q1["Shape the business<br/><i>industrial · bank · life insurer · general insurer</i>"] --> Q2["Statements, ratios, forensics<br/>Altman · Beneish · Piotroski · accruals"]
        Q2 --> Q3["Valuation vs its own history<br/>reverse-DCF · sector-appropriate lens"]
        Q3 --> Q4["Technicals · smart-money cost zones ·<br/>ownership diff · employee sentiment"]
    end
    Q --> W["LLM reads the brief + the filing PDFs<br/>→ writes the thesis and verdict"]
    W --> O["Report + PDF<br/>web UI · terminal · email"]
```

- **The model never supplies a number.** The brief it reads is already computed; it explains, it doesn't
  calculate. With no LLM configured at all, the report still builds with every number — only the prose is missing.
- **Ambiguity is shown, not guessed.** "hdfc" is four listed companies, so you get a list. An early version
  guessed HDFC Bank; that was the first bug a real user hit.
- **The shape follows the business.** A bank has no EBITDA and an insurer has no working capital, so they get
  NIM / NPAs / CET1 and APE / persistency / combined ratio / solvency instead of a page of "n/a".

## 2. 💨 Tailwind — four agents, each checking the one before

A supply shock somewhere in the world (an export ban, a quota, a tariff) forces buyers to find another
supplier. Tailwind looks for the Indian listed companies that *are* that other supplier. Nobody types a
commodity — it scans roughly 30 chokepoints (metals, agri, pharma inputs, chemicals, fertiliser, energy)
and also takes open-ended news.

```mermaid
flowchart TB
    S["① <b>Scout</b><br/>Google News across ~30 chokepoints<br/>+ US Federal Register<br/>(incl. proposed rules)"]
    -- "raw signals,<br/>numbered" --> A["② <b>Analyst</b> (LLM)<br/>keeps genuine disruptions;<br/>must cite a signal <b>by number</b>"]
    -- "disruptions" --> M["③ <b>Mapper</b> (LLM + web search)<br/>who in India makes this?<br/>existing producers only"]
    -- "candidate names" --> U["④ <b>Auditor</b> (Python)<br/>real NSE symbol? name matches?<br/>plausible industry? intent-only?"]
    --> O["Verified names,<br/>smallest first,<br/>each with its source link"]
    A -. "no valid signal number" .-> X1["✗ dropped"]
    U -. "not listed / name mismatch /<br/>implausible industry" .-> X2["✗ dropped"]
```

The guard-rails are the design:

- **The Analyst can't invent a source.** It returns the *number* of the signal that evidences each
  disruption; Python maps that number back to the URL it actually fetched. No valid number, no disruption.
- **The Auditor doesn't trust the Mapper.** Every name must resolve to a real NSE symbol whose company name
  matches, in an industry that could plausibly make the thing; "plans to", "MoU", "bid" are flagged as
  intent-only and ranked below real producers.
- **"n/a" and "nothing" are allowed answers.** Revenue share and market share are left blank when they
  aren't known, and a shock with no clean listed beneficiary says so rather than forcing a name.

## 3. ⛏️ Pickaxe — the same idea, on the demand side

In a gold rush, sell pickaxes. When demand for something surges in India, the obvious producers are
crowded and cyclical; the supplier of feed, vaccines, packaging or equipment to *them* often isn't.

```mermaid
flowchart TB
    S["① <b>Scout</b><br/>Google Trends rising 'buy' searches<br/>+ demand-surge news"]
    --> A["② <b>Analyst</b> (LLM)<br/>durable, specific themes —<br/>not fads; cites signals by number"]
    --> M["③ <b>Mapper</b> (LLM + web search)<br/>direct plays <b>and</b><br/>the indirect 'pickaxe' layer"]
    --> U["④ <b>Auditor</b> (Python)<br/>verify vs NSE master ·<br/>smart-money · cyclicality"]
    --> E["⑤ <b>Enrich</b><br/>price · P/E vs sector · support/resistance<br/>(our engines) + revenue-share projection<br/>read from filings, with sources"]
    --> C["⑥ Trends chart<br/>per theme, in the PDF"]
```

Example from a live run: a pet-care demand theme mapped past the obvious poultry producer to an
animal-vaccine maker and an animal-API maker — the less cyclical "pickaxe" layer.

## 4. A real case, told straight

On **Saturday 19-Sep-2026** the weekly Tailwind digest carried a low-severity catalyst: the **Philippines
restricting exports of ube (purple yam)**. There is no "purple-yam stock", so the Mapper went one layer out
— natural colours and specialty food ingredients — and the Auditor kept three small-caps. What they did
next, from the exchange's own closing prices (Friday 18-Sep close = 0):

| Name | Mon 21 | **Tue 22** | Wed 23 | Mon 28 |
|---|---:|---:|---:|---:|
| AVT Natural Products (AVTNPL) | +5.1% | **+14.7%** | +13.0% | +1.1% |
| Vidhi Specialty Food Ingredients (VIDHIING) | +2.9% | +4.5% | +4.4% | +2.7% |
| Dynemic Products (DYNPRO) | 0.0% | +0.7% | +2.0% | −2.3% |
| *Nifty 50, for context* | | *−0.1%* | | *−2.4%* |

What this does and doesn't show:

- **One of three ran, and it gave most of it back within a week.** This is an idea generator, not a
  trading signal — every report says so, and a single case is an anecdote, not a backtest.
- **It found a name no screen would have** — a small colours-and-extracts company, reached from a food
  export ban on another continent, while the index was flat.
- **It was late, and that changed the code.** The mid-week "urgent" alert only fired for *high*-severity
  shocks, so this one waited for Saturday's digest. The urgent check now runs three times each trading day
  (08:30 / 12:30 / 18:00) and also breaks in for a fresh shock of **any severity that carries a small- or
  mid-cap beneficiary** — the kind that moves on news the market hasn't priced — while already-seen shocks
  are skipped before the expensive mapping step.

## 5. One process, three front doors

```mermaid
flowchart TB
    subgraph IN ["Ask"]
        B["Browser<br/>localhost:8765"]
        T["Terminal<br/><code>eqr …</code>"]
        E["Email<br/>subject line"]
    end
    B -- "HTTP + live progress (SSE)" --> S
    T -- "forwarded if a server runs<br/>(else runs directly)" --> S
    E -- "IMAP poll" --> S
    subgraph S ["The server — the only process that writes the database"]
        H["One command handler<br/>(same resolution, lists, follow-ups everywhere)"] --> D[("DuckDB")]
        K["Background: digests · weekly pushes ·<br/>urgent Tailwind · filing alerts · concall scoring"] --> D
    end
    H --> O["Report: Markdown · HTML · PDF"]
    K --> O
```

DuckDB allows one writer, so everything runs inside one process and the CLI forwards to it. That is also
why the three front doors behave identically: there is only one handler behind them.
