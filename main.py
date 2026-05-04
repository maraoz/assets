import asyncio
import hashlib
import json
import time
from pathlib import Path

import httpx
from curl_cffi.requests import AsyncSession as CurlAsyncSession
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from scrape_public import fetch_public_snapshot, download_logos

ROOT = Path(__file__).parent
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Yahoo fingerprints clients at the TLS/HTTP2 layer; curl_cffi impersonating
# Chrome is the only Python option that gets past the 429 wall reliably.
YAHOO_HEADERS = {
    "Accept": "application/json,*/*",
    "Origin": "https://finance.yahoo.com",
    "Referer": "https://finance.yahoo.com/",
}

# Convert 1 tonne -> the per-contract unit Yahoo prices in.
UNITS_PER_TONNE = {
    "troy oz": 32150.7,
    "barrel": 7.33,
    "mmbtu": 52.0,
    "gallon": 294.0,
    "pound": 2204.62,
    "bushel": 36.74,
    "tonne": 1.0,
    "1000 boardft": 0.7,
}

CHART_CONCURRENCY = 4
CHART_DELAY_S = 0.15
YAHOO_BACKOFF_S = 1800  # pause all Yahoo calls for 30 min after a 429

# Mutable shared state for Yahoo backoff
_yahoo = {"blocked_until": 0.0}

cache = {
    "assets": [],
    "last_updated": 0,
    "private": [],
    "fiat": [],
    "misc": [],
    "commodity_constants": [],
    "public_snapshot": [],   # list of dicts from scrape_public
    "crypto_raw": [],
    "commodity_prices": {},
    "fx_rates": {},  # ticker -> units of that currency per 1 USD
    "api_body": b"{}",  # pre-serialized JSON bytes for /api/assets
    "api_etag": '"0"',
}

app = FastAPI()

# Static icon files; downloaded by download_icons.py
ICONS_DIR = ROOT / "icons"
ICONS_DIR.mkdir(exist_ok=True)
app.mount("/icons", StaticFiles(directory=str(ICONS_DIR)), name="icons")

# Self-hosted webfonts (IBM Plex Sans/Mono, latin + latin-ext woff2 subsets)
FONTS_DIR = ROOT / "fonts"
if FONTS_DIR.exists():
    app.mount("/fonts", StaticFiles(directory=str(FONTS_DIR)), name="fonts")


@app.middleware("http")
async def cache_static(request, call_next):
    """Long-lived caching for /icons/* and /fonts/*. Icons carry a ?v=<mtime>
    cache-buster; font filenames are stable per IBM Plex version."""
    response = await call_next(request)
    path = request.url.path
    if path.startswith("/icons/") or path.startswith("/fonts/"):
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return response

# Per-asset icon path with mtime-based cache buster.
# Prefer .webp; fall back to .png for any stragglers; None hides the <img>.
def _icon_url(category: str, ticker: str) -> str | None:
    cat_dir = ICONS_DIR / category
    for ext in ("webp", "png"):
        p = cat_dir / f"{ticker}.{ext}"
        if p.exists():
            return f"/icons/{category}/{ticker}.{ext}?v={int(p.stat().st_mtime)}"
    return None


def load_json(name):
    return json.loads((ROOT / name).read_text())


async def fetch_fx_rates():
    url = "https://open.er-api.com/v6/latest/USD"
    async with httpx.AsyncClient(timeout=20.0) as c:
        r = await c.get(url, headers={"User-Agent": UA})
        r.raise_for_status()
        return r.json().get("rates") or {}


async def fetch_coingecko():
    url = "https://api.coingecko.com/api/v3/coins/markets"
    params = {
        "vs_currency": "usd",
        "order": "market_cap_desc",
        "per_page": 100,
        "page": 1,
        "price_change_percentage": "24h,7d,30d,1y",
    }
    async with httpx.AsyncClient(timeout=20.0) as c:
        r = await c.get(url, params=params, headers={"User-Agent": UA})
        r.raise_for_status()
        return r.json()


async def _chart(client, sym, sem):
    if time.time() < _yahoo["blocked_until"]:
        return None
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
    async with sem:
        await asyncio.sleep(CHART_DELAY_S)
        r = await client.get(url, params={"interval": "1d", "range": "1y"}, timeout=20)
    if r.status_code == 429:
        _yahoo["blocked_until"] = time.time() + YAHOO_BACKOFF_S
        print(f"yahoo 429 on {sym} — backing off {YAHOO_BACKOFF_S}s")
        return None
    if r.status_code != 200:
        return None
    data = r.json().get("chart", {}).get("result")
    if not data:
        return None
    return data[0]


def _summarize_chart(data):
    """Extract latest price + 24h/7d/30d/1y % change from a chart result."""
    meta = data.get("meta", {})
    closes = data.get("indicators", {}).get("quote", [{}])[0].get("close", []) or []
    latest = meta.get("regularMarketPrice")
    if latest is None:
        latest = next((x for x in reversed(closes) if x is not None), None)
    if latest is None:
        return None

    # 24h change: compare latest vs the previous trading day's close from the
    # array. NOTE: meta.chartPreviousClose is the close BEFORE the chart range
    # starts (~1y ago for range=1y), NOT yesterday's close.
    non_null = [x for x in closes if x is not None]
    prev_close = non_null[-2] if len(non_null) >= 2 else None
    ch24 = ((latest - prev_close) / prev_close * 100.0) if prev_close else None

    n = len(closes)

    def pct(idx):
        if idx < 0 or idx >= n:
            return None
        v = closes[idx]
        return (latest - v) / v * 100.0 if v else None

    return {
        "price": latest,
        "name": meta.get("longName") or meta.get("shortName"),
        "change_24h": ch24,
        "change_7d": pct(n - 8),
        "change_30d": pct(n - 31),
        "change_1y": pct(0),
    }


async def fetch_commodities(defs):
    sem = asyncio.Semaphore(CHART_CONCURRENCY)
    out = {}
    async with CurlAsyncSession(headers=YAHOO_HEADERS, impersonate="chrome") as c:
        async def one(d):
            sym = d["yahoo_symbol"]
            try:
                data = await _chart(c, sym, sem)
                if not data:
                    return
                s = _summarize_chart(data)
                if s:
                    out[sym] = s
            except Exception:
                pass

        await asyncio.gather(*(one(d) for d in defs))
    return out


def unify(crypto, public_snap, commodity_prices, commodity_defs, private, fiat, misc, fx_rates):
    assets = []

    for c in crypto:
        # Skip long-tail coins under $1B mcap to keep the list curated.
        mcap = c.get("market_cap")
        if mcap is None or mcap < 1_000_000_000:
            continue
        ticker = (c.get("symbol") or "").upper()
        assets.append({
            "category": "crypto",
            "name": c.get("name"),
            "ticker": ticker,
            "icon_url": _icon_url("crypto", ticker),
            "market_cap_usd": mcap,
            "price_usd": c.get("current_price"),
            "change_24h": c.get("price_change_percentage_24h_in_currency"),
            "change_7d": c.get("price_change_percentage_7d_in_currency"),
            "change_30d": c.get("price_change_percentage_30d_in_currency"),
            "change_1y": c.get("price_change_percentage_1y_in_currency"),
            "as_of": None,
            "source": "CoinGecko",
            "method": None,
        })

    for e in public_snap:
        ticker = e["ticker"]
        assets.append({
            "category": "public",
            "name": e["name"],
            "ticker": ticker,
            "icon_url": _icon_url("public", ticker),
            "market_cap_usd": e["market_cap_usd"],
            "price_usd": e["price_usd"],
            "change_24h": e.get("change_24h"),
            "change_7d": None,
            "change_30d": None,
            "change_1y": None,
            "as_of": None,
            "source": "companiesmarketcap.com",
            "method": None,
        })

    for d in commodity_defs:
        sym = d["yahoo_symbol"]
        p = commodity_prices.get(sym)
        if not p or p.get("price") is None:
            continue
        upton = UNITS_PER_TONNE.get(d.get("unit_per_contract", ""), 1.0)
        total_units = d["stock_or_production_tonnes"] * upton
        # Yahoo quotes grains and softs in cents per unit, not USD.
        price_usd = p["price"] * d.get("price_multiplier", 1.0)
        assets.append({
            "category": "commodity",
            "name": d["name"],
            "ticker": d["ticker"],
            "icon_url": _icon_url("commodity", d["ticker"]),
            "market_cap_usd": price_usd * total_units,
            "price_usd": price_usd,
            "change_24h": p.get("change_24h"),
            "change_7d": p.get("change_7d"),
            "change_30d": p.get("change_30d"),
            "change_1y": p.get("change_1y"),
            "as_of": None,
            "source": "Yahoo Finance futures",
            "method": d.get("method"),
        })

    for p in private:
        assets.append({
            "category": "private",
            "name": p["name"],
            "ticker": p["ticker"],
            "icon_url": _icon_url("private", p["ticker"]),
            "market_cap_usd": p["valuation_usd"],
            "price_usd": None,
            "change_24h": None,
            "change_7d": None,
            "change_30d": None,
            "change_1y": None,
            "as_of": p.get("as_of"),
            "source": p.get("source"),
            "method": None,
        })

    for f in fiat:
        rate = fx_rates.get(f["ticker"])
        if f["ticker"] == "USD":
            price_usd = 1.0
        elif rate and rate > 0:
            price_usd = 1.0 / rate
        else:
            price_usd = None
        assets.append({
            "category": "fiat",
            "name": f["name"],
            "ticker": f["ticker"],
            "icon_url": _icon_url("fiat", f["ticker"]),
            "market_cap_usd": f["m2_usd"],
            "price_usd": price_usd,
            "change_24h": None,
            "change_7d": None,
            "change_30d": None,
            "change_1y": None,
            "as_of": f.get("as_of"),
            "source": f.get("source"),
            "method": None,
        })

    for m in misc:
        assets.append({
            "category": "misc",
            "name": m["name"],
            "ticker": m["ticker"],
            "icon_url": _icon_url("misc", m["ticker"]),
            "market_cap_usd": m["value_usd"],
            "price_usd": None,
            "change_24h": None,
            "change_7d": None,
            "change_30d": None,
            "change_1y": None,
            "as_of": m.get("as_of"),
            "source": m.get("source"),
            "method": None,
        })

    return assets


async def refresh_loop():
    last_public = 0.0
    last_commodities = 0.0
    last_static_load = 0.0
    last_fx = 0.0
    while True:
        try:
            now = time.time()

            # Static JSON every 5 min
            if now - last_static_load > 300:
                cache["private"] = load_json("private_companies.json")
                cache["fiat"] = load_json("fiat_m2.json")
                cache["misc"] = load_json("misc_assets.json")
                cache["commodity_constants"] = load_json("commodities.json")
                last_static_load = now

            # Crypto every tick (30s)
            try:
                cache["crypto_raw"] = await fetch_coingecko()
            except Exception as e:
                print(f"coingecko error: {e}")

            # FX rates every 5 min (open.er-api.com is cheap but not for spam)
            if now - last_fx > 300:
                try:
                    cache["fx_rates"] = await fetch_fx_rates()
                    last_fx = now
                except Exception as e:
                    print(f"fx error: {e}")

            # Commodities every 30s (~17 calls)
            if now - last_commodities > 30:
                cm = await fetch_commodities(cache["commodity_constants"])
                if cm:
                    cache["commodity_prices"] = cm
                    last_commodities = now

            # Top 1000 public companies snapshot every 5 min
            if now - last_public > 300:
                try:
                    snap = await fetch_public_snapshot()
                    if snap:
                        cache["public_snapshot"] = snap
                        asyncio.create_task(download_logos(snap))
                    last_public = now
                except Exception as e:
                    print(f"public snapshot error: {e}")

            cache["assets"] = unify(
                cache["crypto_raw"],
                cache["public_snapshot"],
                cache["commodity_prices"],
                cache["commodity_constants"],
                cache["private"],
                cache["fiat"],
                cache["misc"],
                cache["fx_rates"],
            )
            cache["last_updated"] = time.time()

            # Pre-serialize the API body once per refresh, dropping null fields
            # so 1000+ rows × ~6 mostly-null fields don't waste bytes on every poll.
            slim = []
            for a in cache["assets"]:
                slim.append({k: v for k, v in a.items() if v is not None})
            body = json.dumps(
                {"assets": slim, "last_updated": cache["last_updated"]},
                separators=(",", ":"),  # no whitespace
                ensure_ascii=False,
            ).encode("utf-8")
            cache["api_body"] = body
            cache["api_etag"] = '"' + hashlib.md5(body).hexdigest()[:16] + '"'
        except Exception as e:
            print(f"refresh error: {e}")
        await asyncio.sleep(30)


@app.on_event("startup")
async def startup():
    asyncio.create_task(refresh_loop())


@app.get("/api/assets")
async def get_assets(request: Request):
    etag = cache["api_etag"]
    # 304 fast-path: when the client already has the current snapshot
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={
            "etag": etag,
            "cache-control": "public, max-age=25, must-revalidate",
        })
    return Response(
        content=cache["api_body"],
        media_type="application/json",
        headers={
            "etag": etag,
            # Cloudflare can serve from edge for 25s (just under our 30s poll)
            "cache-control": "public, max-age=25, must-revalidate",
        },
    )


@app.get("/")
async def index():
    return FileResponse(ROOT / "index.html")


@app.get("/robots.txt")
async def robots():
    # Permissive — explicitly allow all crawlers. Overrides Cloudflare's
    # managed robots.txt so AI-crawler blocking isn't auto-injected at the edge.
    return Response(
        content="User-agent: *\nAllow: /\n",
        media_type="text/plain",
        headers={"cache-control": "public, max-age=3600"},
    )


@app.get("/favicon.ico")
async def favicon():
    return FileResponse(ROOT / "favicon.ico", media_type="image/x-icon")


@app.get("/favicon-180.png")
async def apple_touch_icon():
    return FileResponse(ROOT / "favicon-180.png", media_type="image/png")
