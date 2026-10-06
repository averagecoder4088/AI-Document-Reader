"""
fetch_docs.py -- Bonus: live-data fetcher.
Builds a research pack for a real NSE ticker in the same format as research_pack/
(front-matter header + body), so run_pipeline_v3.py runs on it unchanged.

Usage:  python fetch_docs.py RELIANCE
"""
import argparse
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date
from email.utils import parsedate_to_datetime
from pathlib import Path

import yfinance as yf

CRORE = 1e7


def header(source, url, published, doc_type):
    return f"---\nsource: {source}\nurl: {url}\npublished: {published}\ntype: {doc_type}\n---\n\n"


def clean(s):
    return re.sub(r"\s+", " ", str(s)).strip()


def num(x, d=2):
    try:
        v = float(x)
        return "n/a" if v != v else f"{v:,.{d}f}"
    except (TypeError, ValueError):
        return "n/a"


def crore(x):
    try:
        v = float(x) / CRORE
        return "n/a" if v != v else f"{v:,.0f}"
    except (TypeError, ValueError):
        return "n/a"


def slug(s):
    return re.sub(r"[^a-z0-9]+", "", s.lower())[:20] or "src"


# ---------- Document 1: market snapshot ----------
def market_doc(sym, info, today):
    name = info.get("longName") or info.get("shortName") or sym
    cur = info.get("currency", "INR")
    lines = [
        header("Yahoo Finance via yfinance (market-data aggregator, not an exchange filing)",
               f"https://finance.yahoo.com/quote/{sym}", today, "market data summary"),
        f"# {name} ({sym}): market data snapshot, fetched {today}",
        "",
        "Prices are delayed and are as of the fetch date above.",
        "",
    ]
    if info.get("sector") or info.get("industry"):
        lines.append(f"- Sector: {info.get('sector', 'n/a')}; industry: {info.get('industry', 'n/a')}")
    fields = [
        ("Current price", "currentPrice", 2), ("52-week high", "fiftyTwoWeekHigh", 2),
        ("52-week low", "fiftyTwoWeekLow", 2), ("Trailing P/E", "trailingPE", 1),
        ("Forward P/E", "forwardPE", 1), ("Price to book", "priceToBook", 2),
        ("Debt to equity (percent)", "debtToEquity", 1), ("Return on equity", "returnOnEquity", 3),
    ]
    for label, key, d in fields:
        if info.get(key) is not None:
            lines.append(f"- {label}: {num(info[key], d)}" + (f" {cur}" if label in ("Current price", "52-week high", "52-week low") else ""))
    if info.get("marketCap"):
        lines.append(f"- Market capitalisation: {crore(info['marketCap'])} crore {cur}")
    if info.get("totalDebt"):
        lines.append(f"- Total debt: {crore(info['totalDebt'])} crore {cur}")
    if info.get("totalCash"):
        lines.append(f"- Total cash: {crore(info['totalCash'])} crore {cur}")
    if info.get("longBusinessSummary"):
        lines += ["", "## Business description (as provided by Yahoo Finance)", "", clean(info["longBusinessSummary"])]
    return name, "\n".join(lines) + "\n"


# ---------- Document 2: quarterly results ----------
def financials_doc(sym, name, t, info, today):
    df = t.quarterly_income_stmt
    if df is None or df.empty:
        return None
    cols = list(df.columns)[:5]  # newest first
    rows = [("Total Revenue", "Revenue"), ("EBITDA", "EBITDA"),
            ("Operating Income", "Operating income"), ("Net Income", "Net income")]
    cur = info.get("financialCurrency", "INR")
    out = [
        header("Yahoo Finance via yfinance (aggregated company financials, not an exchange filing)",
               f"https://finance.yahoo.com/quote/{sym}/financials", today, "quarterly financials table"),
        f"# {name} ({sym}): quarterly income statement, fetched {today}",
        "",
        f"Figures in crore {cur} (1 crore = 10 million), converted from raw values by code. "
        "Column headings are quarter-end dates. Yahoo can skip quarters, so columns are not always consecutive.",
        "",
        "| crore | " + " | ".join(c.strftime("%Y-%m-%d") for c in cols) + " |",
        "|---|" + "---|" * len(cols),
    ]
    for key, label in rows:
        if key in df.index:
            out.append(f"| {label} | " + " | ".join(crore(df.loc[key, c]) for c in cols) + " |")
    for key, label in rows:
        if key not in df.index:
            continue
        prior = [c for c in cols[1:] if 350 <= (cols[0] - c).days <= 380]
        if not prior:
            continue
        try:
            a, b = float(df.loc[key, cols[0]]), float(df.loc[key, prior[0]])
        except (TypeError, ValueError):
            continue
        if a == a and b == b and b:
            out += ["", f"{label}, latest quarter ({cols[0]:%Y-%m-%d}) vs same quarter a year earlier "
                        f"({prior[0]:%Y-%m-%d}): {crore(b)} crore to {crore(a)} crore "
                        f"({(a / b - 1) * 100:+.1f}%), computed by fetch_docs.py from the table above."]
    return "\n".join(out) + "\n"


# ---------- Documents 3..n: news headlines ----------
def google_news(name, n):
    q = urllib.parse.quote(f'"{name}" stock')
    url = f"https://news.google.com/rss/search?q={q}&hl=en-IN&gl=IN&ceid=IN:en"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        root = ET.fromstring(r.read())
    items = []
    for it in root.iter("item"):
        try:
            d = parsedate_to_datetime(it.findtext("pubDate", "")).date().isoformat()
        except Exception:
            continue
        src = it.find("source")
        outlet = clean(src.text) if src is not None and src.text else "unknown outlet"
        title = clean(it.findtext("title", ""))
        title = re.sub(r"\s+-\s+" + re.escape(outlet) + r"$", "", title)
        items.append((title, it.findtext("link", ""), outlet, d))
        if len(items) >= n:
            break
    return items


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ticker", help="NSE ticker, e.g. RELIANCE")
    ap.add_argument("--out", default=None)
    ap.add_argument("--news", type=int, default=5)
    args = ap.parse_args()

    tk = args.ticker.upper()
    sym = tk if tk.endswith((".NS", ".BO")) else tk + ".NS"
    out_dir = Path(args.out or f"live_packs/{tk.split('.')[0]}")
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("*.md"):  # don't mix old and new fetches
        old.unlink()
    today = date.today().isoformat()

    t = yf.Ticker(sym)
    info = t.info or {}
    if not (info.get("longName") or info.get("shortName")):
        raise SystemExit(f"No data for {sym}. Check the ticker (or Yahoo may be rate-limiting; retry later).")

    name, text = market_doc(sym, info, today)
    (out_dir / f"yahoo_market_data_{today}.md").write_text(text, encoding="utf-8")
    written = ["market data"]

    try:
        fin = financials_doc(sym, name, t, info, today)
        if fin:
            (out_dir / f"yahoo_quarterly_financials_{today}.md").write_text(fin, encoding="utf-8")
            written.append("quarterly financials")
        else:
            print("No quarterly financials returned.")
    except Exception as e:
        print(f"Financials failed: {e}")

    try:
        for i, (title, link, outlet, d) in enumerate(google_news(name, args.news), start=1):
            body = (header(f"{outlet} (headline via Google News RSS)", link, d,
                           "news headline (title only; article text not fetched)")
                    + f"# {title}\n\nOnly the headline was fetched. The article body was not retrieved.\n")
            (out_dir / f"news_{i:02d}_{slug(outlet)}_{d}.md").write_text(body, encoding="utf-8")
            written.append(f"headline {i}")
    except Exception as e:
        print(f"News failed: {e}")

    print(f"Wrote {len(written)} documents to {out_dir}/: {', '.join(written)}")
    print(f'\nNext:\n  python run_pipeline_v3.py --pack {out_dir} --ticker {tk.split(".")[0]} '
          f'--today "{date.today():%d %B %Y}" --label live_{tk.split(".")[0].lower()} '
          f'--coverage --model openai/gpt-oss-120b')


if __name__ == "__main__":
    main()
