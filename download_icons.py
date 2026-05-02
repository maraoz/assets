"""One-shot icon downloader. Run after adding/changing assets.

Sources:
- crypto: CoinGecko `image` field
- public/private: clearbit logo API by domain
- fiat: flagcdn country flags
- commodity/misc: Twemoji PNGs by codepoint

Idempotent — re-running only fetches missing files.
"""

import json
import sys
from pathlib import Path

import httpx
from curl_cffi import requests as crequests

ROOT = Path(__file__).parent
ICONS = ROOT / "icons"

CG_URL = "https://api.coingecko.com/api/v3/coins/markets"
GOOGLE_FAVICON = "https://www.google.com/s2/favicons?domain={}&sz=128"
DDG_FAVICON = "https://icons.duckduckgo.com/ip3/{}.ico"
FLAGCDN = "https://flagcdn.com/w160/{}.png"
TWEMOJI = "https://cdn.jsdelivr.net/gh/twitter/twemoji@latest/assets/72x72/{}.png"


def save(path: Path, content: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def get(url: str, *, impersonate: bool = False, timeout: int = 20) -> bytes | None:
    try:
        if impersonate:
            r = crequests.get(url, impersonate="chrome", timeout=timeout)
        else:
            r = httpx.get(url, timeout=timeout, follow_redirects=True,
                          headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code == 200 and len(r.content) > 80:
            return r.content
        print(f"  skip ({r.status_code}, {len(r.content)}B): {url}")
    except Exception as e:
        print(f"  err: {url}: {e}")
    return None


def download_crypto():
    print("=== crypto ===")
    out_dir = ICONS / "crypto"
    r = httpx.get(CG_URL, params={
        "vs_currency": "usd", "order": "market_cap_desc",
        "per_page": 100, "page": 1,
    }, timeout=30)
    coins = r.json()
    n_ok = n_skip = 0
    for c in coins:
        ticker = (c.get("symbol") or "").upper()
        path = out_dir / f"{ticker}.png"
        if path.exists():
            n_skip += 1
            continue
        img = c.get("image")
        if not img:
            continue
        data = get(img)
        if data:
            save(path, data)
            n_ok += 1
    print(f"  downloaded {n_ok}, skipped existing {n_skip}")


def fetch_favicon(domain):
    """Try Google first, then DuckDuckGo. Google returns ~330B placeholder for
    unknown domains, so check the size."""
    data = get(GOOGLE_FAVICON.format(domain))
    if data and len(data) > 500:
        return data
    data = get(DDG_FAVICON.format(domain))
    if data and len(data) > 500:
        return data
    return None


def download_public():
    print("=== public ===")
    from tickers import DOMAINS
    out_dir = ICONS / "public"
    n_ok = n_skip = n_fail = 0
    for ticker, domain in DOMAINS.items():
        path = out_dir / f"{ticker}.png"
        if path.exists():
            n_skip += 1
            continue
        data = fetch_favicon(domain)
        if data:
            save(path, data)
            n_ok += 1
        else:
            n_fail += 1
            print(f"  no icon for {ticker} ({domain})")
    print(f"  downloaded {n_ok}, skipped {n_skip}, failed {n_fail}")


def download_private():
    print("=== private ===")
    out_dir = ICONS / "private"
    entries = json.loads((ROOT / "private_companies.json").read_text())
    n_ok = n_skip = n_fail = 0
    for e in entries:
        path = out_dir / f"{e['ticker']}.png"
        if path.exists():
            n_skip += 1
            continue
        domain = e.get("domain")
        if not domain:
            continue
        data = fetch_favicon(domain)
        if data:
            save(path, data)
            n_ok += 1
        else:
            n_fail += 1
            print(f"  no icon for {e['ticker']} ({domain})")
    print(f"  downloaded {n_ok}, skipped {n_skip}, failed {n_fail}")


def download_fiat():
    print("=== fiat ===")
    out_dir = ICONS / "fiat"
    entries = json.loads((ROOT / "fiat_m2.json").read_text())
    n_ok = n_skip = 0
    for e in entries:
        path = out_dir / f"{e['ticker']}.png"
        if path.exists():
            n_skip += 1
            continue
        cc = e.get("country_code")
        if not cc:
            continue
        data = get(FLAGCDN.format(cc))
        if data:
            save(path, data)
            n_ok += 1
    print(f"  downloaded {n_ok}, skipped {n_skip}")


def download_emoji_set(category: str, json_file: str):
    print(f"=== {category} ===")
    out_dir = ICONS / category
    entries = json.loads((ROOT / json_file).read_text())
    n_ok = n_skip = 0
    for e in entries:
        path = out_dir / f"{e['ticker']}.png"
        if path.exists():
            n_skip += 1
            continue
        hex_code = e.get("emoji_hex")
        if not hex_code:
            continue
        data = get(TWEMOJI.format(hex_code))
        if data:
            save(path, data)
            n_ok += 1
    print(f"  downloaded {n_ok}, skipped {n_skip}")


if __name__ == "__main__":
    only = sys.argv[1] if len(sys.argv) > 1 else None
    steps = {
        "crypto": download_crypto,
        "public": download_public,
        "private": download_private,
        "fiat": download_fiat,
        "commodity": lambda: download_emoji_set("commodity", "commodities.json"),
        "misc": lambda: download_emoji_set("misc", "misc_assets.json"),
    }
    targets = [only] if only else list(steps.keys())
    for t in targets:
        steps[t]()
    print("done")
