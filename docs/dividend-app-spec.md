# Dividend Investment Tracking App — Technical Specification & Implementation Plan

Status: design spec, no code yet. Target repo for implementation: `rwhalen0406/dividendapp` (currently
empty — no existing code, schema, or conventions to preserve). This document was written in the
`hairbiz` workspace per this session's branch assignment; the stack recommended below deliberately
mirrors the sibling `hairbiz` app's proven pattern (Flask + sqlite3 + requests/pandas, no frontend
build step) since that is the most relevant local precedent available.

**Verification caveat:** this environment's egress proxy blocks `stockanalysis.com`, so the exact
HTML/DOM structure of the target pages could not be inspected live while writing this spec. Section 5
gives the scraping strategy and a best-effort field list from general knowledge of the site, but flags
a mandatory **Phase 0** task to confirm real selectors/JSON shape against the live site before writing
the parser.

---

## 1. System Architecture

Single-user, local-first app. One Python process serves both the API and the server-rendered UI;
SQLite is the only datastore; the only outbound network calls are to `stockanalysis.com`.

```
┌─────────────────────────────────────────────────────────────────────────┐
│                          Browser (localhost)                            │
│  Jinja2-rendered pages + vanilla JS/fetch() for AJAX actions             │
│  Chart.js for price-history & projection charts                         │
│                                                                           │
│  Pages: /            (dashboard)                                        │
│         /research/<ticker>                                              │
│         /history/<ticker>                                               │
│         /analysis                                                       │
│         /portfolio                                                      │
└───────────────┬─────────────────────────────────────┬───────────────────┘
                │ HTML (GET)                           │ JSON (fetch, AJAX)
                ▼                                       ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                        Flask app  (app.py)                              │
│  Route handlers — thin: parse request → call module → render/return     │
├───────────────┬───────────────────┬───────────────────┬─────────────────┤
│  db.py         │  stockanalysis_   │  analysis.py       │  sync.py        │
│  SQLite conn,  │  client.py        │  appreciation,     │  orchestrates   │
│  schema, CRUD  │  fetch + parse    │  dividend growth,  │  "refresh all"  │
│                │  dividend/history │  projections,      │  across every   │
│                │  pages            │  DRIP simulation   │  tracked ticker │
└───────┬────────┴─────────┬─────────┴─────────────────────┴───────┬───────┘
        │                  │ requests.get()                        │
        ▼                  ▼                                       │
┌──────────────────┐  ┌─────────────────────────────────┐          │
│  SQLite file      │  │  stockanalysis.com               │◄─────────┘
│  data/dividend    │  │  /etf/{ticker}/dividend/         │
│  app.db           │  │  /etf/{ticker}/history/          │
└──────────────────┘  └─────────────────────────────────┘
```

**Data flow, end to end (e.g. clicking "Refresh" on the dashboard):**

1. Browser → `POST /api/refresh_all`.
2. `app.py` calls `sync.refresh_all(conn)`.
3. `sync.py` reads every distinct ticker referenced by a `holdings` row (plus any ticker with a
   `research`/`analysis` page visited recently — see §3 caching rules), and for each:
   a. `stockanalysis_client.fetch_dividend_page(ticker)` → parsed dict → `db.upsert_dividend_research(...)`.
   b. `stockanalysis_client.fetch_history_page(ticker)` → list of daily rows → `db.upsert_price_history(...)`.
4. Each ticker's outcome (ok/error) is appended to a `refresh_log` row.
5. Response JSON summarizes: tickers refreshed, tickers failed (with reason), timestamp.
6. Browser updates the dashboard's "last refreshed" banner and re-renders affected holdings' current
   price/value without a full page reload.

Analysis and portfolio-projection calculations are **never scraped live on request** — they always
read from the local SQLite cache (populated by the last refresh) so the Analysis and Portfolio pages
stay fast and don't hammer the external site on every keystroke/reload. If a ticker has no cached data
yet, the app fetches it once, synchronously, on first request (see §3).

---

## 2. Database Schema (SQLite)

Following `hairbiz`'s pattern: plain `sqlite3` via a `db.py` module (no ORM), schema as a `CREATE TABLE
IF NOT EXISTS` string run on startup, one file at `data/dividendapp.db` (gitignored, like
`data/hairbiz.db`).

```sql
-- One row per ticker the app has ever pulled data for. Acts as the ticker registry
-- and holds the "latest snapshot" fields that don't need full history (current price,
-- payout frequency) so other tables can join on it cheaply.
CREATE TABLE IF NOT EXISTS tickers (
    ticker              TEXT PRIMARY KEY,          -- e.g. 'SCHD', normalized upper-case
    name                TEXT,                      -- fund/company name, from research page
    last_price          REAL,                      -- most recent close from price_history
    last_price_date     TEXT,                      -- ISO date of last_price
    payout_frequency    TEXT,                      -- 'monthly' | 'quarterly' | 'semi-annual' | 'annual'
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Snapshot of the /dividend/ page, one row per successful pull (append-only history of
-- the summary stats, not just "latest"), so trend-of-trend questions are possible later.
CREATE TABLE IF NOT EXISTS dividend_research (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker                TEXT NOT NULL REFERENCES tickers(ticker),
    pulled_at             TEXT NOT NULL DEFAULT (datetime('now')),
    dividend_yield_pct    REAL,      -- trailing yield, e.g. 3.42
    annual_dividend       REAL,      -- $ per share, trailing 12mo
    payout_frequency      TEXT,
    ex_dividend_date      TEXT,
    declaration_date      TEXT,
    record_date           TEXT,
    payment_date          TEXT,
    dividend_growth_1y_pct  REAL,
    dividend_growth_3y_pct  REAL,
    dividend_growth_5y_pct  REAL,
    payout_ratio_pct      REAL,      -- null for many ETFs; present for stocks/some funds
    source_url            TEXT NOT NULL,
    raw_json              TEXT       -- full parsed payload, for fields not modeled above yet
);
CREATE INDEX IF NOT EXISTS idx_dividend_research_ticker_time
    ON dividend_research(ticker, pulled_at DESC);

-- Per-payout dividend history rows, parsed from the table on the /dividend/ page.
CREATE TABLE IF NOT EXISTS dividend_payments (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker            TEXT NOT NULL REFERENCES tickers(ticker),
    ex_dividend_date  TEXT NOT NULL,
    cash_amount       REAL NOT NULL,
    declaration_date  TEXT,
    record_date       TEXT,
    payment_date      TEXT,
    pulled_at         TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(ticker, ex_dividend_date)
);
CREATE INDEX IF NOT EXISTS idx_dividend_payments_ticker_date
    ON dividend_payments(ticker, ex_dividend_date);

-- Daily OHLCV rows parsed from the /history/ page.
CREATE TABLE IF NOT EXISTS price_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker      TEXT NOT NULL REFERENCES tickers(ticker),
    date        TEXT NOT NULL,        -- ISO 'YYYY-MM-DD'
    open        REAL,
    high        REAL,
    low         REAL,
    close       REAL NOT NULL,
    volume      INTEGER,
    pulled_at   TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(ticker, date)
);
CREATE INDEX IF NOT EXISTS idx_price_history_ticker_date
    ON price_history(ticker, date);

-- User-defined brokerage/retirement accounts.
CREATE TABLE IF NOT EXISTS accounts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,          -- e.g. "Fidelity Roth IRA"
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- A position: one ticker within one account.
CREATE TABLE IF NOT EXISTS holdings (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id            INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    ticker                TEXT NOT NULL REFERENCES tickers(ticker),
    shares                REAL NOT NULL DEFAULT 0,      -- fractional shares supported
    dividend_reinvest     INTEGER NOT NULL DEFAULT 1,   -- 0/1 boolean (DRIP on/off)
    continue_buying       INTEGER NOT NULL DEFAULT 0,   -- 0/1 boolean
    monthly_buy_amount    REAL NOT NULL DEFAULT 0,      -- $ added per month if continue_buying
    created_at            TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at            TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(account_id, ticker)
);

-- Audit trail for the "Refresh" action (dashboard + per-ticker), surfaced in the UI as
-- "last refreshed" and used to decide whether a ticker's data is stale.
CREATE TABLE IF NOT EXISTS refresh_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker        TEXT,                 -- NULL for a whole-app "refresh all" summary row
    kind          TEXT NOT NULL,        -- 'research' | 'history' | 'refresh_all'
    status        TEXT NOT NULL,        -- 'ok' | 'error'
    error_message TEXT,
    started_at    TEXT NOT NULL,
    finished_at   TEXT NOT NULL
);
```

Notes:
- `dividend_research` is append-only (small row count — one pull per ticker per refresh, refreshes are
  manual/daily at most) so historical yield/growth trends are queryable later without a migration.
  `tickers.payout_frequency`/`last_price` are the denormalized "current" view everything else joins on.
- `price_history` and `dividend_payments` use `UNIQUE` constraints so refresh is a plain
  `INSERT ... ON CONFLICT DO UPDATE` (idempotent upsert), never duplicate rows on re-pull.
- No `analysis_results` or `projection` table — those are pure functions of cached data, computed
  on request in `analysis.py` (cheap: a handful of years of daily rows, no need to persist).

---

## 3. API Endpoint Specification

Flask routes. HTML routes render Jinja2 templates; `/api/*` routes return JSON and are called via
`fetch()` from page JS for actions that shouldn't trigger a full page reload (refresh, add/edit
holding, projection).

### Pages (HTML)

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | Dashboard: accounts summary, total portfolio value, "Refresh" button, last-refresh timestamp |
| GET | `/research/<ticker>` | Research page for one ticker (Feature 1) |
| GET | `/history/<ticker>` | Price history page/chart for one ticker (Feature 2) |
| GET | `/analysis` | Analysis form + results (Feature 3) |
| GET | `/portfolio` | Accounts & holdings management (Feature 4) |

### Research & History data (Features 1, 2, 5)

| Method | Path | Body / Query | Response |
|---|---|---|---|
| GET | `/api/research/<ticker>` | — | Latest cached `dividend_research` row + `dividend_payments` list as JSON. `404` if never pulled. |
| POST | `/api/research/<ticker>/refresh` | — | Fetches `/etf/<ticker>/dividend/` now, upserts, returns the fresh JSON. `502` with error detail on scrape/parse failure (site unreachable or HTML shape changed). |
| GET | `/api/history/<ticker>?range=1y` | `range`: `1m,6m,ytd,1y,5y,max` (default `5y`) | Cached `price_history` rows in range, ascending by date. |
| POST | `/api/history/<ticker>/refresh` | — | Fetches `/etf/<ticker>/history/` now, upserts rows, returns count added/updated. |
| POST | `/api/refresh_all` | — | Runs `sync.refresh_all()`: pulls research + history for every ticker referenced by any holding. Returns `{refreshed: [...], failed: [{ticker, error}], started_at, finished_at}`. This backs the Feature 5 "Refresh" button. |

Staleness rule used by page loads (not just the button): `/research/<ticker>` and `/history/<ticker>`
trigger an automatic single-ticker refresh **only if** there is no cached row at all for that ticker
(first-ever view) or the most recent `dividend_research`/`price_history` row is more than 24h old *and*
the user hasn't already refreshed in this session — otherwise they serve cached data instantly and rely
on the explicit Refresh button. This avoids surprise multi-second scrape latency on every navigation.

### Analysis (Feature 3)

| Method | Path | Body | Response |
|---|---|---|---|
| POST | `/api/analysis` | `{ "ticker": "SCHD", "amount": 10000 }` | See shape below |

Response:
```json
{
  "ticker": "SCHD",
  "current_price": 27.84,
  "shares_purchased": 359.194,
  "avg_annual_appreciation_pct": 6.8,
  "avg_dividend_pct": 3.4,
  "avg_dividend_dollar": 0.9486,
  "history_window_years": 5,
  "projections": [
    { "years": 5,  "projected_price": 38.72, "projected_annual_dividend_per_share": 1.24, "projected_monthly_dividend": 37.13 },
    { "years": 10, "projected_price": 53.87, "projected_annual_dividend_per_share": 1.62, "projected_monthly_dividend": 48.49 },
    { "years": 15, "projected_price": 74.94, "projected_annual_dividend_per_share": 2.12, "projected_monthly_dividend": 63.44 },
    { "years": 20, "projected_price": 104.28, "projected_annual_dividend_per_share": 2.77, "projected_monthly_dividend": 82.90 }
  ]
}
```
If `ticker` has no cached history, the handler fetches it synchronously first (same as the staleness
rule above) before computing — this is the one path where a POST may take a few seconds.

### Portfolio (Feature 4)

| Method | Path | Body | Response |
|---|---|---|---|
| GET | `/api/accounts` | — | List of accounts with holdings summary (shares, current value) |
| POST | `/api/accounts` | `{ "name": "Fidelity Roth IRA" }` | Created account |
| PUT | `/api/accounts/<id>` | `{ "name": "..." }` | Updated account |
| DELETE | `/api/accounts/<id>` | — | Deletes account and its holdings (cascade) |
| POST | `/api/accounts/<id>/holdings` | `{ "ticker": "SCHD", "shares": 100, "dividend_reinvest": true, "continue_buying": true, "monthly_buy_amount": 200 }` | Created holding. Ticker is fetched (research+history) if never seen before. |
| PUT | `/api/holdings/<id>` | any subset of the same fields | Updated holding |
| DELETE | `/api/holdings/<id>` | — | Removes holding |
| POST | `/api/portfolio/project` | `{ "as_of_date": "2036-08-08" }` | Projects every holding to that date — see shape below |

`/api/portfolio/project` response:
```json
{
  "as_of_date": "2036-08-08",
  "holdings": [
    {
      "holding_id": 12,
      "account_id": 1,
      "ticker": "SCHD",
      "current_shares": 100,
      "current_value": 2784.00,
      "projected_shares": 168.42,
      "projected_price": 53.87,
      "projected_value": 9072.14,
      "projected_annual_dividend_income": 273.05,
      "projected_monthly_dividend_income": 22.75
    }
  ],
  "totals": {
    "current_value": 2784.00,
    "projected_value": 9072.14,
    "projected_annual_dividend_income": 273.05,
    "projected_monthly_dividend_income": 22.75
  }
}
```

All `POST/PUT` bodies are JSON; validation errors return `400` with `{"error": "..."}`. This mirrors
`hairbiz`'s existing `flash()`-based error surfacing for the HTML routes, and plain JSON errors for the
`/api/*` routes.

---

## 4. Frontend Page Structure

Server-rendered Jinja2 (`templates/`) + `static/app.js` (fetch-based AJAX) + `static/style.css`,
matching `hairbiz`'s "no build step" approach. Chart.js loaded from a vendored local copy (avoid a CDN
dependency for a local-first app) for the two chart types needed: price-history line chart and
projection bar/line chart.

**1. Dashboard (`/`)**
- Header: app name, last-refreshed timestamp, **Refresh** button (Feature 5) — disabled + spinner while
  `/api/refresh_all` is in flight, then re-renders account cards in place.
- Per-account summary cards: account name, total current value, holding count, link into `/portfolio`.
- Aggregate stat row: total portfolio value, total annual dividend income (sum of latest TTM dividend ×
  shares across all holdings).
- Empty state: "Add your first account" CTA into `/portfolio` when no accounts exist.

**2. Research page (`/research/<ticker>`)**
- Ticker header (name, current price, last-pulled timestamp) + per-ticker **Refresh** button.
- Summary stat grid: dividend yield %, annual dividend $, payout frequency, ex-dividend date,
  1y/3y/5y dividend growth %, payout ratio (blank/hidden if not applicable).
- Dividend payment history table (ex-date, amount, payment date), paginated/scrollable.
- Ticker search box at top to jump to any other ticker's research page.

**3. History page (`/history/<ticker>`)**
- Range selector (1M/6M/YTD/1Y/5Y/MAX) driving `GET /api/history/<ticker>?range=`.
- Chart.js line chart of close price over the selected range.
- Data table below the chart (date, O/H/L/C, volume), most recent first.
- Per-ticker **Refresh** button, same staleness/last-pulled indicator as Research.

**4. Analysis page (`/analysis`)**
- Form: ticker input (autocomplete against known tickers from `tickers` table, but free-text entry
  allowed for a new ticker), investment amount input, **Analyze** button.
- Results panel (populated via `POST /api/analysis`, no page reload):
  - Stat row: current price, shares purchased, avg annual appreciation %, avg dividend $ and %.
  - Projection table: rows for 5/10/15/20 years — projected price, projected annual dividend/share,
    projected monthly dividend (on the original share count — this page is deliberately a static
    buy-and-hold view; reinvestment compounding lives in Portfolio, §4.5, to keep this page's mental
    model simple: "if I bought and never touched it").
  - Small chart: projected price and projected monthly dividend across the 4 horizons.

**5. Portfolio page (`/portfolio`)**
- Account list, each expandable: **Add account** form (name only) at the top.
- Per-account holdings table: ticker, shares, current price, current value, DRIP toggle, "continue
  buying" toggle + monthly $ field (shown when toggle on), edit/delete row actions.
- **Add holding** inline form per account: ticker, starting shares, DRIP toggle, continue-buying toggle
  + amount.
- Projection controls: a date picker (defaults to +10 years) and **Project** button firing
  `POST /api/portfolio/project`; results render as an additional "Projected" column set on the same
  holdings table (projected shares/value/income) plus a totals footer row, so the user can compare
  today vs. the projected date side by side without leaving the page.

---

## 5. Data Parsing & Scraping Strategy for stockanalysis.com

**Constraint acknowledged up front:** this spec was written without live access to `stockanalysis.com`
(blocked by this environment's network policy). Everything below reflects general knowledge of how
stockanalysis.com's ETF pages are typically structured, not a verified-today inspection. Treat field
names/table shapes as a strong starting hypothesis, not a contract.

**Recommended approach, in priority order:**

1. **Phase 0 (must happen before writing the parser): manually inspect the live pages.** Open
   `https://stockanalysis.com/etf/schd/dividend/` and `.../history/` in a browser, view source and the
   Network tab. stockanalysis.com is a Next.js site; many Next.js data sites hydrate their tables from
   either (a) an embedded `<script id="__NEXT_DATA__" type="application/json">` blob already containing
   the full page's data server-side-rendered, or (b) an internal JSON API route the page's JS calls
   client-side (commonly something like `/api/symbol/...`) to populate the interactive table/chart. If
   either exists, **prefer it over HTML scraping** — it's a stable, structured contract instead of a
   CSS-selector-dependent scrape that breaks on any redesign. Record what's found as a short
   `docs/scraping-notes.md` in the implementation repo so the choice is documented.
2. **Fallback: HTML table scraping**, if no JSON hydration source is available or usable:
   - `requests.get(url, headers={"User-Agent": "<app-name>/1.0 (personal use; contact <email>)"})`
     — identify the app honestly; don't spoof a browser UA to evade blocking.
   - Check `https://stockanalysis.com/robots.txt` for disallowed paths before scraping anything, and
     re-check periodically — this gates whether scraping `/etf/*/dividend/` and `/etf/*/history/` is
     permitted at all. If disallowed, stop and use the paid `stockanalysis.com` API product instead
     (they sell a data API — worth pricing out before building a scraper the ToS forbids).
   - Parse with `pandas.read_html(response.text)` to pull `<table>` elements directly into DataFrames
     (fast, robust to minor markup changes), falling back to `BeautifulSoup` for the non-tabular summary
     stat boxes (dividend yield, payout frequency, etc. — typically rendered as labeled stat tiles, not
     a table).
   - Wrap every field extraction in a helper that returns `None` on a missing/renamed field rather than
     raising, and log which fields were missing on that pull — so a partial site redesign degrades
     gracefully (missing one stat) instead of breaking the whole refresh.
3. **Politeness & reliability, regardless of which method wins:**
   - Cache aggressively (§3 staleness rule) — never refetch a ticker more than once per explicit refresh
     or once per 24h automatically. This is a personal tracking tool for a handful of tickers, not a
     crawler; there's no reason to hit the site more than a few times a day even with several holdings.
   - Rate-limit sequential requests within `sync.refresh_all()` (e.g. 1–2s between tickers) and add
     retry-with-backoff on `429`/`5xx`, matching the pattern already used in `hairbiz/square_client.py`.
   - Treat `403`/repeated failures as "scraping is being blocked" and surface that clearly in the UI
     (`refresh_log.status='error'`) rather than silently showing stale data as if it were fresh.
   - Isolate all site-specific parsing in one module (`stockanalysis_client.py`) with pure functions
     `parse_dividend_page(html) -> dict` and `parse_history_page(html) -> list[dict]`, unit-tested
     against saved HTML fixtures (`tests/fixtures/*.html` captured during Phase 0) — this is what makes
     the inevitable future site-redesign a one-file fix instead of a hunt through the whole codebase.

**Expected fields to extract** (to be confirmed in Phase 0):
- From `/dividend/`: dividend yield %, annual dividend $/share, payout frequency, ex-dividend/
  declaration/record/payment dates, dividend growth % (1y/3y/5y or whatever horizons the site shows),
  payout ratio (may be absent for many ETFs), and the full per-payout dividend history table.
- From `/history/`: daily rows of date/open/high/low/close/volume, plus whatever range selector the
  page exposes (used to decide how far back to paginate/request).

---

## 6. Calculation Formulas

All calculations live in `analysis.py`, operate purely on cached SQLite data (`price_history`,
`dividend_payments`, `tickers`), and take a DB connection + ticker in, returning plain dicts — no
network calls inside this module.

### 6.1 Average stock appreciation (%)

Use CAGR over the available cached history window (default lookback: 5 years, or the full available
range if shorter):

```
years = (last_date - first_date).days / 365.25
appreciation_rate = (last_close / first_close) ** (1 / years) - 1
```

`history_window_years` is reported alongside the rate so the UI can show "based on N years of data"
(important since a newly-listed ETF may only have 1–2 years of history).

### 6.2 Average dividend payout ($ and %)

```
ttm_dividend = sum(cash_amount for each dividend_payments row
                    in the trailing 12 months from the latest ex-dividend date)
avg_dividend_pct = ttm_dividend / current_price * 100

dividend_growth_rate = CAGR of annual total dividend per share across the
                        available payment history (same CAGR formula as 6.1,
                        applied to summed-per-calendar-year dividend totals)
```

`dividend_growth_rate` (not just the current yield) is what drives the projections in 6.3 — a static
yield alone can't project a growing payout.

### 6.3 Stock Analysis page projections (Feature 3 — static buy-and-hold, no reinvestment)

For each horizon `N` in `{5, 10, 15, 20}` years:

```
shares_purchased = investment_amount / current_price

projected_price[N]              = current_price * (1 + appreciation_rate) ** N
projected_annual_div_per_share[N] = current_annual_dividend_per_share * (1 + dividend_growth_rate) ** N
projected_monthly_dividend[N]     = shares_purchased * projected_annual_div_per_share[N] / 12
```

Share count is fixed at `shares_purchased` for every horizon — this page intentionally answers "what
if I buy once and hold," matching the feature description (no mention of reinvestment there).

### 6.4 Portfolio reinvestment (DRIP) simulation (Feature 4)

Closed-form compounding gets awkward once quarterly-vs-monthly payout frequency and optional monthly
contributions are both in play, so this runs as a **monthly time-stepped simulation** — simple to
implement, test, and reason about, and cheap enough (at most a few hundred iterations per holding) that
performance is a non-issue at this app's scale:

```
monthly_price_growth   = (1 + appreciation_rate) ** (1/12) - 1
monthly_div_growth     = (1 + dividend_growth_rate) ** (1/12) - 1
payout_months          = set of calendar months in which this ticker actually pays,
                          derived from payout_frequency (all 12 if monthly; every 3rd if
                          quarterly; every 6th if semi-annual; December only if annual)

price          = current_price
div_per_share  = current_annual_dividend_per_share / payouts_per_year   # per-payout amount
shares         = holding.shares
cash_dividends_received = 0   # tracked separately when DRIP is off

for each month m from 1 to N_months (today -> as_of_date):
    price *= (1 + monthly_price_growth)

    if m is a payout_month:
        div_per_share *= (1 + monthly_div_growth) ** months_since_last_payout
        dividend_cash = shares * div_per_share
        if holding.dividend_reinvest:
            shares += dividend_cash / price
        else:
            cash_dividends_received += dividend_cash

    if holding.continue_buying:
        shares += holding.monthly_buy_amount / price

projected_shares = shares
projected_price  = price
projected_value  = shares * price
projected_annual_dividend_income  = shares * div_per_share * payouts_per_year
projected_monthly_dividend_income = projected_annual_dividend_income / 12
```

Notes:
- `appreciation_rate` and `dividend_growth_rate` per ticker come straight from §6.1/6.2 (computed once
  from cached history, reused for both the Analysis page and every holding of that ticker in Portfolio
  projections).
- When `dividend_reinvest` is off, `cash_dividends_received` is exposed in the API response too (not
  shown in the response shape in §3 for brevity) so a user can see "how much cash income you'd have
  collected instead of reinvesting" — useful for comparing DRIP on vs. off on the same holding.
- Portfolio totals (§3 response) are a plain sum of each holding's projected value/income — no
  cross-holding interaction (e.g. no rebalancing modeled).

---

## 7. Technology Stack Recommendation

| Layer | Choice | Why |
|---|---|---|
| Backend framework | **Flask** | Matches `hairbiz`'s existing pattern in this workspace; minimal, no unneeded abstraction for a single-user local app. |
| HTTP client (scraping) | **requests** | Same library `hairbiz/square_client.py` already uses; simple retry/backoff wrapper is a known-good pattern to copy. |
| HTML parsing | **pandas.read_html** + **BeautifulSoup4** (fallback for non-table stat tiles) | `pandas` is already a `hairbiz` dependency; reusing it for table extraction avoids adding a new heavy dependency just for scraping. |
| Calculations | **pandas / numpy** | Already proven in `hairbiz/analysis.py` for the equivalent role; CAGR/DRIP simulation is straightforward with plain Python floats too if the dependency isn't wanted. |
| Database | **SQLite via the `sqlite3` stdlib module** | Matches `hairbiz/db.py` exactly — no ORM, no server process, single-file, trivially backed up/inspected. Fully sufficient for one user and a few hundred tickers/holdings. |
| Frontend | **Jinja2 server-rendered templates + vanilla JS (`fetch`)** | Matches `hairbiz`'s "no frontend build step" philosophy; keeps the app genuinely "light" per the brief. A SPA framework (React/Vue) is unnecessary complexity for this scope and would be the first thing to reconsider if the app grows real-time/multi-user requirements later. |
| Charts | **Chart.js**, vendored locally (no CDN) | Small, no build step, sufficient for line/bar projection charts; local vendoring keeps the app fully offline-capable except for the scrape itself. |
| Config/secrets | **python-dotenv + `config.py`** | Same as `hairbiz/config.py` — `.env` (gitignored) for any tunables (refresh rate limits, DB path), no secrets needed here since stockanalysis.com requires no API key for scraping. |

This is a near-exact copy of `hairbiz`'s stack, which is the right call here: it's a proven pattern
already working in this workspace, keeps the two apps maintainable by the same person with one set of
idioms, and nothing in the five features needs more than this stack provides (no auth, no multi-user
concurrency, no real-time push).

---

## 8. Implementation Priority & Phased Rollout

**Phase 0 — Groundwork & verification (prerequisite for everything else)**
- Inspect live `stockanalysis.com` dividend/history pages (Network tab + view-source); decide
  HTML-scrape vs. hidden-JSON-endpoint approach; save fixture HTML/JSON for tests.
- Check `robots.txt` / ToS for scraping permissibility; if blocked, evaluate the paid stockanalysis.com
  API as the actual data source before writing any scraper.
- Scaffold repo: `app.py`, `config.py`, `db.py` (schema from §2), `requirements.txt`, `.env.example`,
  `.gitignore` (mirroring `hairbiz`'s), empty `templates/`/`static/`.

**Phase 1 — Data pull (Features 1, 2, 5 backend only)**
- `stockanalysis_client.py`: `fetch_dividend_page`, `fetch_history_page`, and their `parse_*` pure
  functions, unit-tested against Phase 0 fixtures.
- `db.py`: upsert functions for `tickers`, `dividend_research`, `dividend_payments`, `price_history`,
  `refresh_log`.
- `sync.py`: `refresh_all()` orchestration with rate limiting/backoff.
- `/api/research/<ticker>`, `/api/research/<ticker>/refresh`, `/api/history/<ticker>`,
  `/api/history/<ticker>/refresh`, `/api/refresh_all` routes, exercised with `curl`/Postman before any
  UI exists.

**Phase 2 — Research & History pages (Features 1, 2, 5 UI)**
- Research page template + stat grid + payment table.
- History page template + Chart.js line chart + range selector.
- Dashboard skeleton with the Refresh button wired to `/api/refresh_all`.

**Phase 3 — Analysis page (Feature 3)**
- `analysis.py`: CAGR (§6.1), dividend growth (§6.2), projection table (§6.3).
- `/api/analysis` route + Analysis page template with form and results rendering.
- This is the first feature that exercises the full calculation layer end-to-end and is a good
  checkpoint to validate formulas against a couple of known real ETFs by hand before trusting them.

**Phase 4 — Portfolio & reinvestment modeling (Feature 4)**
- `accounts`/`holdings` CRUD routes + Portfolio page (accounts, holdings table, add/edit forms).
- DRIP simulation (§6.4) in `analysis.py`, `/api/portfolio/project` route, projection UI on the
  holdings table.
- Wire dashboard's aggregate stats (total value, total dividend income) once holdings exist.

**Phase 5 — Polish & hardening**
- Error/empty states across all pages (no accounts yet, ticker never pulled, scrape failure banner).
- Refresh staleness indicators (§3) and per-ticker "last pulled" timestamps everywhere data is shown.
- `refresh_log` surfaced somewhere (even just a simple `/refresh-history` debug page) for diagnosing a
  failed scrape without reading server logs — same spirit as `hairbiz`'s diagnostics pages.
- Basic security posture pass matching `hairbiz`'s README section: bind to `127.0.0.1` by default,
  Flask debug off, no secrets committed, `pip-audit` before release.

Each phase produces a usable increment (Phase 1 alone is already useful via raw API calls; Phase 2
makes it usable as an app; Phases 3–4 are the two features the user actually asked for by name).
