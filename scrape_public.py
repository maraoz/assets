"""Scrape companiesmarketcap.com for the global top ~1000 public companies.

Runs in two modes:
  - import as a module: `await fetch_public_snapshot()` returns the list
  - run as a script: writes public_companies.json and downloads any missing
    logos to icons/public/<TICKER>.png

Source HTML structure (per row):
    <tr>
      <td class="fav">...</td>
      <td class="rank-td" data-sort="1">1</td>
      <td class="name-td">
        <div class="logo-container">
          <img class="company-logo" src="/img/company-logos/64/AAPL.webp">
        </div>
        <div class="name-div">
          <a href="/apple/marketcap/">
            <div class="company-name">Apple</div>
            <div class="company-code">AAPL</div>
          </a>
        </div>
      </td>
      <td data-sort="4112774856704">$4.112 T</td>      market cap (USD)
      <td data-sort="19845">$198.45</td>               price * 100
      <td data-sort="-56">0.56%</td>                   24h change * 100, signed
      <td>...sparkline...</td>
      <td>🇺🇸 USA</td>                                  country
    </tr>
"""

import asyncio
import html
import io
import json
import re
from pathlib import Path

from curl_cffi.requests import AsyncSession
from PIL import Image

ROOT = Path(__file__).parent
ICONS_PUBLIC = ROOT / "icons" / "public"

BASE = "https://companiesmarketcap.com"
PAGES = 10              # 100 rows per page → top 1000
CONCURRENCY = 4

RANK_RE = re.compile(r'class="rank-td td-right" data-sort="(\d+)"')
LOGO_RE = re.compile(r'src="(/img/company-logos/64/[^"]+)"')
NAME_RE = re.compile(r'<div class="company-name">([^<]+)</div>')
CODE_RE = re.compile(r'<div class="company-code">(?:<span[^>]*></span>)?([^<]+)</div>')
NUM_RE = re.compile(r'data-sort="(-?\d+(?:\.\d+)?)"')
COUNTRY_RE = re.compile(r'<td>([^<]+)<span class="responsive-hidden">')


def _parse_row(row_html: str):
    rank = RANK_RE.search(row_html)
    code = CODE_RE.search(row_html)
    name = NAME_RE.search(row_html)
    if not (rank and code and name):
        return None
    # Numeric data-sort values, in order: rank, mcap, price*100, change*100
    nums = NUM_RE.findall(row_html)
    if len(nums) < 4:
        return None
    logo = LOGO_RE.search(row_html)
    country = COUNTRY_RE.search(row_html)
    return {
        "rank": int(rank.group(1)),
        "name": html.unescape(name.group(1)).strip(),
        "ticker": html.unescape(code.group(1)).strip(),
        "market_cap_usd": int(float(nums[1])),
        "price_usd": float(nums[2]) / 100.0,
        "change_24h": float(nums[3]) / 100.0,
        "country": html.unescape(country.group(1)).strip() if country else "",
        "logo_url": (BASE + logo.group(1)) if logo else None,
    }


async def _fetch_page(client, page: int):
    url = f"{BASE}/page/{page}/" if page > 1 else f"{BASE}/"
    r = await client.get(url, timeout=30)
    if r.status_code != 200:
        print(f"  page {page}: HTTP {r.status_code}")
        return []
    tbody = r.text
    s = tbody.find("<tbody")
    e = tbody.find("</tbody>")
    if s == -1 or e == -1:
        return []
    body = tbody[s:e]
    # split into rows
    rows = []
    parts = body.split("<tr")
    for p in parts[1:]:  # first part is before <tbody opening tag
        end = p.find("</tr>")
        if end == -1:
            continue
        row_html = "<tr" + p[:end + len("</tr>")]
        parsed = _parse_row(row_html)
        if parsed:
            rows.append(parsed)
    return rows


async def fetch_public_snapshot():
    """Fetch all pages concurrently and return one merged list."""
    async with AsyncSession(impersonate="chrome") as c:
        sem = asyncio.Semaphore(CONCURRENCY)

        async def one(p):
            async with sem:
                return await _fetch_page(c, p)

        results = await asyncio.gather(*(one(p) for p in range(1, PAGES + 1)))
    out = [r for page in results for r in page]
    out.sort(key=lambda x: x["rank"])
    return out


async def download_logos(entries):
    """Pull any missing logos. Saves as 64x64 WebP for size."""
    ICONS_PUBLIC.mkdir(parents=True, exist_ok=True)
    sem = asyncio.Semaphore(8)
    async with AsyncSession(impersonate="chrome") as c:
        async def one(e):
            ticker = e["ticker"]
            out_webp = ICONS_PUBLIC / f"{ticker}.webp"
            out_png = ICONS_PUBLIC / f"{ticker}.png"
            if out_webp.exists() or out_png.exists():
                return False
            async with sem:
                try:
                    r = await c.get(e["logo_url"], timeout=15)
                    if r.status_code != 200 or len(r.content) < 200:
                        return False
                    img = Image.open(io.BytesIO(r.content)).convert("RGBA")
                    img.thumbnail((64, 64), Image.LANCZOS)
                    if img.size != (64, 64):
                        canvas = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
                        canvas.paste(img, ((64 - img.size[0]) // 2, (64 - img.size[1]) // 2))
                        img = canvas
                    img.save(out_webp, format="WEBP", quality=82, method=6)
                    return True
                except Exception as ex:
                    print(f"  logo err {ticker}: {ex}")
                    return False

        results = await asyncio.gather(*(one(e) for e in entries))
    return sum(1 for r in results if r)


async def main():
    print("scraping companiesmarketcap.com...")
    entries = await fetch_public_snapshot()
    print(f"  got {len(entries)} entries")
    snapshot = ROOT / "public_companies.json"
    snapshot.write_text(json.dumps(entries, indent=2, ensure_ascii=False))
    print(f"  wrote {snapshot}")
    n_logos = await download_logos(entries)
    print(f"  downloaded {n_logos} new logos")


if __name__ == "__main__":
    asyncio.run(main())
