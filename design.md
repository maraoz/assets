# Mixed Asset Tracker — MVP Design Doc (v2)

A single sortable list of the world's most valuable assets — **public companies, private companies, cryptocurrencies, commodities, fiat currencies, and miscellaneous categories (global real estate, global bonds)** — in one ranked table. Like 8marketcap.com but broader.

## Goal

Build the smallest possible working version of:
- Backend service that aggregates market cap data from ~6 sources
- Frontend page with a sortable, category-filterable, color-coded table that refreshes live

## Non-goals

- No accounts, no auth, no DB
- No charts or per-asset detail pages
- No ETFs (out of scope)
- No mobile-specific UI
- No WebSockets (HTTP polling is enough)
- No deployment automation

## Asset Categories

Six categories, each with a distinct row background color and a filter checkbox at the top of the page. All filters default to ON.

| Category | Description | Row color | Filter label |
|---|---|---|---|
| `public` | Public companies | White (default) | Public companies |
| `private` | Private companies | Light gray (`#f4f4f5`) | Private companies |
| `crypto` | Cryptocurrencies | Light lavender (`#f5f0fa`) | Crypto |
| `commodity` | Precious metals + commodities | Light yellow (`#fdf6dc`) | Commodities |
| `fiat` | Fiat currencies (M2) | Light green (`#eef7ee`) | Fiat (M2) |
| `misc` | Global aggregates (real estate, bonds) | Light blue (`#eef3fb`) | Other |

Filter behavior is purely client-side — uncheck a box, rows hide. Sort and rank are recomputed on the visible set.

## Architecture

```
                          ┌──────────────────────────┐
  CoinGecko API ───────►  │                          │
  Yahoo Finance API ────► │   FastAPI Backend        │  ◄── HTTP polling
  commodities.json ─────► │   (in-memory cache,      │     every 30s
  fiat_m2.json ─────────► │    bg refresh task)      │
  private_companies.json► │                          │
  misc_assets.json ─────► └──────────────────────────┘
```

**Why polling over WebSockets/SSE.** Half the data (private, fiat, misc) doesn't move between manual updates. Public stocks only move during market hours. Only crypto and commodity prices need sub-minute freshness. `setInterval(fetch, 30000)` is enough, has zero deps, and avoids connection state.

## Tech Stack

**Backend:** Python 3.11+, `fastapi`, `uvicorn`, `httpx` (3 deps total)
**Frontend:** Single `index.html`, vanilla JS, vanilla CSS, no build step

## Data Sources

### 1. Crypto — CoinGecko (`crypto`)

`GET https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd&order=market_cap_desc&per_page=100&page=1&price_change_percentage=24h,7d,30d,1y`

One call, top 100 coins, all 4 % windows. Free tier, no token. Refresh every 30s.

Required attribution footer: "Powered by CoinGecko".

### 2. Public companies — Yahoo Finance unofficial (`public`)

Tickers hardcoded in `tickers.py` — top ~100 by market cap including non-US (TSM, ASML, BABA, TCEHY, NVO, NESN.SW, etc.). Updated manually quarterly.

- **Live (every 30s):** `query1.finance.yahoo.com/v7/finance/quote?symbols=...` — batched comma-separated, returns `marketCap` + `regularMarketChangePercent` (24h)
- **Historical (every 1 hour):** `query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=1y` — one call per ticker, daily closes used to compute 7d/30d/1y % changes
- Requires `User-Agent` header. Handle 429 by serving stale data, not crashing.

### 3. Private companies — Static JSON (`private`)

File: `private_companies.json`. Hand-maintained (~25 companies).

```json
[
  {
    "name": "OpenAI",
    "ticker": "OPENAI",
    "valuation_usd": 500000000000,
    "as_of": "2025-10-02",
    "source": "secondary share sale"
  }
]
```

Update cadence: manual, when notable rounds happen. Quarterly Claude Code refresh task: re-research valuations, write back to JSON.

### 4. Commodities — Yahoo futures + static supply (`commodity`)

This category mixes two methodologies. The frontend shows a small `[stock]` or `[flow]` tag in the row to make this honest.

**Tier A — Store-of-value metals (above-ground stock × spot price):**

| Commodity | Yahoo ticker | Above-ground stock |
|---|---|---|
| Gold | `GC=F` | ~216,265 tonnes |
| Silver | `SI=F` | ~1.74M tonnes |
| Platinum | `PL=F` | ~10,000 tonnes |
| Palladium | `PA=F` | ~3,500 tonnes |

**Tier B — Flow commodities (annual global production × spot price):**

Energy: Crude (`CL=F`), Brent (`BZ=F`), Natural Gas (`NG=F`), Heating Oil (`HO=F`)
Industrial metals: Copper (`HG=F`), plus Aluminum/Nickel/Zinc/Lead/Tin (LME-based — may need alternate source)
Agricultural: Wheat (`ZW=F`), Corn (`ZC=F`), Soybeans (`ZS=F`), Coffee (`KC=F`), Sugar (`SB=F`), Cocoa (`CC=F`), Cotton (`CT=F`)
Other: Lumber (`LBR=F`)

**File:** `commodities.json` — static supply/production constants per commodity. Prices fetched live from Yahoo futures.

```json
[
  {
    "name": "Gold",
    "ticker": "GOLD",
    "yahoo_symbol": "GC=F",
    "method": "stock",
    "stock_or_production_tonnes": 216265,
    "unit_per_contract": "troy oz"
  },
  {
    "name": "Crude Oil",
    "ticker": "CRUDE",
    "yahoo_symbol": "CL=F",
    "method": "flow",
    "stock_or_production_tonnes": 4400000000,
    "unit_per_contract": "barrel"
  }
]
```

Backend converts Yahoo futures price (per ounce / per barrel / per bushel) into total USD value using the conversion factors in the JSON.

**Realistic target: ~20 commodities at MVP.** Hitting 50 requires fragile sources for low-liquidity items (lithium, uranium, cobalt, rare earths). Add later or use ETF proxies (LIT, URA).

Refresh cadence: prices every 30s. Stock/production constants updated manually annually.

### 5. Fiat currencies — Static JSON (`fiat`)

File: `fiat_m2.json`. Top ~20-50 currencies by M2 in USD-equivalent. Hand-maintained quarterly.

```json
[
  {
    "name": "US Dollar",
    "ticker": "USD",
    "m2_usd": 22000000000000,
    "as_of": "2026-01-31",
    "source": "Federal Reserve H.6"
  },
  {
    "name": "Chinese Yuan",
    "ticker": "CNY",
    "m2_usd": 45000000000000,
    "as_of": "2026-01-31",
    "source": "PBoC, converted at FX 7.20"
  }
]
```

**Decision: lock USD-equivalent at update time.** No live FX. Simpler, slightly stale between quarterly refreshes. Acceptable tradeoff.

**Seed list (rough order, top ~20):** CNY, USD, EUR (Eurozone aggregate), JPY, GBP, INR, KRW, CHF, CAD, HKD, AUD, BRL, RUB, TWD, MXN, SGD, THB, ZAR, IDR, TRY, ARS, UYU.

% change fields are null for fiat M2 (would require historical M2 series — not worth the complexity for v1).

Refresh cadence: manual quarterly.

### 6. Misc — Static JSON (`misc`)

File: `misc_assets.json`. Two entries.

```json
[
  {
    "name": "Global Real Estate",
    "ticker": "GLOBAL_RE",
    "value_usd": 393300000000000,
    "as_of": "2025-01-01",
    "source": "Savills World Research 2025"
  },
  {
    "name": "Global Bonds & Fixed Income",
    "ticker": "GLOBAL_BONDS",
    "value_usd": 145100000000000,
    "as_of": "2024-12-31",
    "source": "SIFMA 2025 Capital Markets Fact Book"
  }
]
```

Refresh cadence: annual. Add more entries here over time without splitting into a new category.

## Unified Asset Schema (Backend → Frontend)

```json
{
  "assets": [
    {
      "category": "public",
      "name": "Bitcoin",
      "ticker": "BTC",
      "market_cap_usd": 1300000000000,
      "price_usd": 65000.00,
      "change_24h": 1.23,
      "change_7d": -2.5,
      "change_30d": 8.1,
      "change_1y": 45.6,
      "as_of": "2026-04-15",
      "source": "Federal Reserve H.6",
      "method": null
    }
  ],
  "last_updated": 1714600000
}
```

Fields are null where not applicable: `price_usd` is null for fiat M2 and misc; all `change_*` are null for fiat, misc, and private; `as_of` and `source` are present on static categories; `method` is `"stock"` or `"flow"` for commodities only.

## Backend Spec

Single `main.py`. Background async task on a 30s tick:

```python
async def refresh_loop():
    last_historical = 0
    last_static_load = 0
    while True:
        try:
            crypto = await fetch_coingecko()
            stocks_live = await fetch_yahoo_quotes(TICKERS)
            commodities = await fetch_commodities(COMMODITY_DEFS)
            
            if time.time() - last_historical > 3600:
                cache["historical"] = await fetch_yahoo_history(TICKERS)
                last_historical = time.time()
            
            if time.time() - last_static_load > 300:  # 5min static reload
                cache["private"] = load_json("private_companies.json")
                cache["fiat"] = load_json("fiat_m2.json")
                cache["misc"] = load_json("misc_assets.json")
                cache["commodity_constants"] = load_json("commodities.json")
                last_static_load = time.time()
            
            stocks = merge_stocks(stocks_live, cache["historical"])
            cache["assets"] = unify(
                crypto, stocks, commodities,
                cache["private"], cache["fiat"], cache["misc"]
            )
            cache["last_updated"] = time.time()
        except Exception as e:
            print(f"refresh error: {e}")
        await asyncio.sleep(30)
```

Endpoints:
- `GET /api/assets` — returns the cached unified list
- `GET /` — serves `index.html`

## Frontend Spec

Single `index.html`, no framework, no build step.

**Top bar:** 6 filter checkboxes (one per category), all checked by default.

**Table columns:**
1. Rank (computed from current sort, on visible rows only)
2. Name + ticker + tiny category label (and `[stock]`/`[flow]` tag for commodities)
3. Market cap (formatted: $32.16T, $4.82T, $456B)
4. Price (null shown as "—")
5. 24h %
6. 7d %
7. 30d %
8. 1y %

**Row styling:** apply CSS class `category-{category}` to each `<tr>`. Background color from the category table above.

**Sort:**
- Default: market cap descending
- Click header to sort
- Second click reverses
- **Null handling:** rows with null in the active sort column always sink to the bottom regardless of direction

**Live indicator:** "Updated Xs ago" footer driven by `last_updated`.

**Polling:** `setInterval(() => fetch('/api/assets').then(render), 30000)`.

**Footer attribution:** "Powered by CoinGecko · Yahoo Finance · Savills · SIFMA".

## File Layout

```
/
├── main.py                    # FastAPI app + refresh loop
├── tickers.py                 # ~100 public ticker symbols
├── private_companies.json     # ~25 private company valuations
├── commodities.json           # ~20 commodity definitions
├── fiat_m2.json               # ~20-50 fiat currencies by M2
├── misc_assets.json           # 2 entries: global real estate, global bonds
├── index.html                 # single-page frontend
├── requirements.txt           # fastapi, uvicorn, httpx
└── README.md                  # how to run + how to refresh static data
```

Nine files.

## Update Cadences Summary

| Category | Live (30s) | Slow refresh | Manual |
|---|---|---|---|
| Crypto | ✅ all data | — | — |
| Public | ✅ price, mcap, 24h | ✅ 7d/30d/1y (1h) | ✅ ticker list (quarterly) |
| Commodity | ✅ price | — | ✅ supply constants (annual) |
| Private | — | — | ✅ valuations (quarterly) |
| Fiat | — | — | ✅ M2 in USD (quarterly) |
| Misc | — | — | ✅ values (annual) |

## Run

```bash
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

## Build Sequence for Claude Code

1. `requirements.txt` — fastapi, uvicorn, httpx
2. `tickers.py` — top ~100 public tickers (research current top by market cap, mix of US + international)
3. `private_companies.json` — seed with SpaceX, OpenAI, Anthropic, ByteDance, xAI, Stripe, Databricks, Tether, Revolut, Canva, Anduril, Epic Games, Discord, Fanatics, Shein, Kraken, Ripple, Rippling, Chime, Cargill, Mars, Koch Industries, IKEA, Bloomberg LP, Thinking Machines Lab
4. `commodities.json` — seed ~20 commodities listed above; research current production/above-ground stock figures from World Gold Council, USGS, EIA, USDA
5. `fiat_m2.json` — seed ~20 currencies above; research latest M2 from each central bank's official statistics and convert at current FX rates
6. `misc_assets.json` — global real estate ($393.3T, Savills 2025), global bonds (~$145.1T, SIFMA 2024)
7. `main.py` — fetchers, merge, refresh loop, endpoint
8. `index.html` — table, filters, sort, polling
9. Smoke test: confirm all 6 categories appear, filter checkboxes hide/show, sort works, 30s refresh runs without 429s

## Decisions Already Resolved (Confirm Before Build)

1. **Polling over WebSockets** — confirmed
2. **Mixed commodity methodology with `[stock]`/`[flow]` tag** — confirmed
3. **Fiat M2 in USD locked at update time, no live FX** — confirmed
4. **Top ~20 commodities at MVP, not 50** — confirmed; expand later
5. **Filters default ON, client-side only** — confirmed
6. **Real estate + bonds grouped as "misc", not own categories** — confirmed

## Static Data Refresh Workflow

For manually-maintained data (private companies, fiat M2, misc, commodity constants), the workflow:

> "Claude Code, please research and update `fiat_m2.json` with the latest M2 figures from each country's central bank, converted to USD at current FX rates. Update the `as_of` date and `source` for each entry."

Claude Code does the research, edits the JSON in place. The running app picks up the changes within 5 minutes (static-file reload tick).
