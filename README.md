# Mixed Asset Tracker

Single sortable list of the world's most valuable assets — public companies, private companies, cryptocurrencies, commodities, fiat currencies (M2), and miscellaneous (global real estate, global bonds).

## Run

```bash
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

Then open http://localhost:8000.

The backend caches data in memory and refreshes every 30s. First page load may briefly show "Loading…" until the first refresh completes.

## Data sources

| Category | Source | Refresh |
|---|---|---|
| Crypto | CoinGecko (`/coins/markets`) | 30s |
| Public companies | Yahoo Finance (`/v8/finance/chart`) × hardcoded shares outstanding | 30 min |
| Commodities | Yahoo Finance futures × static stock/production constants | 60s |
| Private companies | `private_companies.json` | 5min static reload |
| Fiat M2 | `fiat_m2.json` | 5min static reload |
| Misc (RE, bonds) | `misc_assets.json` | 5min static reload |

Stocks are throttled to 30 min because Yahoo rate-limits ~100 chart requests per 30s. The unofficial `/v7/finance/quote` endpoint (which would have given live market cap in one batched call) now requires a crumb/cookie auth flow that's increasingly unreliable. We fall back to per-ticker chart calls and compute `market_cap = price × shares_outstanding` using values in `tickers.py`.

## Refreshing static data

Edit the JSON files directly. The running app picks up changes on the next 5-minute static reload tick.

For periodic refreshes, ask Claude Code to research and update each file (e.g. "update `fiat_m2.json` with the latest M2 figures from each central bank, converted to USD").

## Files

- `main.py` — FastAPI app + background refresh loop
- `tickers.py` — top public ticker symbols
- `private_companies.json`, `commodities.json`, `fiat_m2.json`, `misc_assets.json` — static data
- `index.html` — single-page frontend (no build step)
