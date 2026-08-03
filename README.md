# HairBiz Revenue Insights

A small Flask app that connects to your Square account (read-only) and turns
your transaction, catalog, appointment, and customer data into an actionable
revenue report for a hair styling business.

**This app is read-only.** It only calls Square's `GET` and `search` (POST
read) endpoints -- Locations, Catalog search, Orders search, Payments list,
Bookings list, Customers list. There is no code path anywhere in this repo
that creates, updates, or deletes anything in your Square account. See
`square_client.py` for the full (and only) set of API calls made.

## What it does

1. **Sync**: pulls your locations, service catalog (items/prices), completed
   orders + line items, payments, appointment bookings, and customers from
   Square, and caches them locally in a SQLite file (`data/hairbiz.db`).
2. **Analyze**: for each service, computes total revenue, times sold, average
   ticket, and a growth/flat/decline trend; finds services frequently booked
   together; flags top performers and underperformers; and computes client
   repeat-visit / retention metrics.
3. **Recommend**: turns the analysis into plain-English recommendations --
   services to promote, services to reconsider or discontinue, bundle/upsell
   ideas, low return-on-time services, and client retention opportunities.

### Important caveat on "profitability"

Square's public API does not expose cost-of-goods, labor cost, or margin
data. Every "profitability" or "underperformer" signal in this report is a
**proxy built from revenue and volume** (and, where appointment duration data
is available, revenue-per-minute of chair time as a time-efficiency proxy) --
**not true profit margin**. Treat the rankings as directional starting
points for your own judgment, not a precise P&L.

## Setup

1. **Get a Square access token.** In the [Square Developer
   Dashboard](https://developer.squareup.com/apps), create/open an
   application and copy a **Sandbox** or **Production** access token from the
   Credentials page. (If you'd rather use OAuth for a multi-merchant setup,
   this app currently expects a static access token -- ask if you want OAuth
   added.)

2. **Configure environment variables:**
   ```bash
   cp .env.example .env
   # then edit .env and set SQUARE_ACCESS_TOKEN (and SQUARE_ENVIRONMENT)
   ```
   Credentials are only ever read from environment variables -- nothing is
   hardcoded.

3. **Install dependencies:**
   ```bash
   python3 -m venv venv
   ./venv/bin/pip install -r requirements.txt
   ```

4. **Run the app:**
   ```bash
   ./venv/bin/python app.py
   ```
   Then open http://localhost:5000

5. Click **"Sync data from Square now"** on the home page, then **"View
   report"**.

## Which Square products this uses

- **Catalog API** -- your service menu and prices.
- **Orders API** -- completed order line items, the primary source of
  per-service revenue.
- **Payments API** -- transaction-level amounts, dates, and payment method.
- **Bookings API** -- appointment history (which service, when, which
  team member), used for booking-volume and chair-time analysis. If your
  account doesn't use Square Appointments, sync will note this as a warning
  and the rest of the report still works from Orders/Payments data alone.
- **Customers API** -- used to identify repeat clients and retention
  patterns.

## Notes on pagination & rate limits

All list/search calls in `square_client.py` follow Square's `cursor`-based
pagination until exhausted. Requests that hit a `429` (rate limited) or
`5xx` are retried with exponential backoff (up to 5 attempts, honoring
`Retry-After` when present). `401`/`403` responses are treated as an
auth/token problem and surfaced clearly in the UI rather than retried.

## Data window

By default each sync pulls the last `SYNC_LOOKBACK_DAYS` (730 / ~2 years) of
orders, payments, and bookings, so trend analysis has enough history.
Adjust this in `.env`.

## Tech stack

Python 3 / Flask, `requests` for the Square REST API, `pandas`/`numpy` for
analysis, SQLite (via the standard library) as a local cache. No frontend
build step -- server-rendered templates.
