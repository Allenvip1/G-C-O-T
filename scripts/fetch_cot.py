"""下载 CFTC Disaggregated 报告（期货+期权合并），生成 docs/data/cot.json（仅黄金、白银）。

分组对应关系（按视频博主的数值量级核对过）：
  商业套保 comm  = Producer/Merchant/Processor/User
  大户     large = Managed Money
  散户     specs = Other Reportables

增量更新：已有数据按日期合并，每次只重新下载最近两年的 zip；
首次运行（或加 --full）会下载 START_YEAR 起的全部历史。
"""
import csv, io, json, re, sys, zipfile, urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

START_YEAR = 2010
URL_YEAR = "https://www.cftc.gov/files/dea/history/com_disagg_txt_{year}.zip"  # 2017 起，按年
URL_HIST = "https://www.cftc.gov/files/dea/history/com_disagg_txt_hist_2006_2016.zip"  # 2006-2016 合订
OUT = Path(__file__).resolve().parent.parent / "docs" / "data" / "cot.json"

CONTRACTS = {  # CFTC代码 -> 显示名
    "088691": "黄金 Gold (GC)",
    "084691": "白银 Silver (SI)",
}

COLS = {  # 输出字段 -> CSV 列名
    "oi": "Open_Interest_All",
    "cl": "Prod_Merc_Positions_Long_All",
    "cs": "Prod_Merc_Positions_Short_All",
    "ll": "M_Money_Positions_Long_All",
    "ls": "M_Money_Positions_Short_All",
    "sl": "Other_Rept_Positions_Long_All",
    "ss": "Other_Rept_Positions_Short_All",
}


def download(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=180) as r:
        return r.read()


def parse(blob):
    z = zipfile.ZipFile(io.BytesIO(blob))
    with z.open(z.namelist()[0]) as f:
        rd = csv.reader(io.TextIOWrapper(f, "utf-8-sig"))
        header = [h.strip() for h in next(rd)]
        idx = {k: header.index(v) for k, v in COLS.items()}
        i_date = header.index("Report_Date_as_YYYY-MM-DD")
        i_code = header.index("CFTC_Contract_Market_Code")
        for row in rd:
            code = row[i_code].strip()
            if code in CONTRACTS and int(row[i_date][:4]) >= START_YEAR:
                yield code, row[i_date].strip(), {k: int(row[i].strip()) for k, i in idx.items()}


PRICE_SYMS = {"088691": "GC=F", "084691": "SI=F"}  # COMEX 黄金、白银期货连续合约（Yahoo）
PRICE_URL = "https://{host}/v8/finance/chart/{sym}?range=20y&interval=1wk"


def fetch_weekly(sym):
    """Yahoo 周线 -> [[周一日期, 开, 高, 低, 收], ...]；失败抛异常。"""
    last = None
    for host in ("query1.finance.yahoo.com", "query2.finance.yahoo.com"):
        url = PRICE_URL.format(host=host, sym=sym)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                res = json.loads(r.read().decode("utf-8"))["chart"]["result"][0]
            break
        except Exception as e:
            last = e
    else:
        raise RuntimeError(last)
    off, q = res["meta"]["gmtoffset"], res["indicators"]["quote"][0]
    rows = []
    for i, t in enumerate(res["timestamp"]):
        if None in (q["open"][i], q["high"][i], q["low"][i], q["close"][i]):
            continue
        day = datetime.fromtimestamp(t + off, timezone.utc).date().isoformat()
        rows.append([day] + [round(q[k][i], 2) for k in ("open", "high", "low", "close")])
    return sorted(rows)


def embed_prices(page):
    """抓周线并写进 index.html 的 price-data；任何品种失败都保留旧数据。"""
    html = page.read_text(encoding="utf-8")
    m = re.search(r'<script id="price-data">window\.PRICE_DATA=(.*?);</script>', html, re.S)
    prices = json.loads(m.group(1)) if m else {}
    for code, sym in PRICE_SYMS.items():
        try:
            rows = fetch_weekly(sym)
            if len(rows) < 100:
                raise ValueError(f"只取到 {len(rows)} 行")
            prices[code] = rows
            print(f"price {sym}: {len(rows)} weeks, {rows[0][0]} ~ {rows[-1][0]}")
        except Exception as e:
            print(f"warn: {sym} 周线抓取失败，保留旧数据：{e}", file=sys.stderr)
    tag = '<script id="price-data">window.PRICE_DATA=' + json.dumps(prices, separators=(",", ":")) + ";</script>"
    if m:
        html = html[:m.start()] + tag + html[m.end():]
    else:
        html = html.replace('<script id="cot-data">', tag + "\n" + '<script id="cot-data">', 1)
    page.write_text(html, encoding="utf-8")


def main():
    full = "--full" in sys.argv
    store = {}  # code -> {date: rec}
    if OUT.exists() and not full:
        old = json.loads(OUT.read_text(encoding="utf-8"))
        for code, c in old["contracts"].items():
            store[code] = {c["dates"][i]: {k: c[k][i] for k in COLS} for i in range(len(c["dates"]))}
    this_year = date.today().year
    first = START_YEAR if (full or not store) else this_year - 1
    sources = []
    if first <= 2016:
        sources.append(("2006-2016 合订", URL_HIST))
    sources += [(str(y), URL_YEAR.format(year=y)) for y in range(max(first, 2017), this_year + 1)]
    for label, url in sources:
        try:
            blob = download(url)
        except Exception as e:
            print(f"skip {label}: {e}")
            continue
        n = 0
        for code, d, rec in parse(blob):
            store.setdefault(code, {})[d] = rec
            n += 1
        print(f"{label}: {n} rows")
    out = {"updated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), "order": [], "contracts": {}}
    for code, name in CONTRACTS.items():
        recs = store.get(code)
        if not recs:
            continue
        dates = sorted(recs)
        c = {"name": name, "dates": dates}
        for k in COLS:
            c[k] = [recs[d][k] for d in dates]
        out["contracts"][code] = c
        out["order"].append(code)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    blob = json.dumps(out, ensure_ascii=False, separators=(",", ":"))
    OUT.write_text(blob, encoding="utf-8")
    # 把数据内嵌进 index.html（单文件即可打开；预览窗格/双击打开时相对路径的 js 加载不到）
    page = OUT.parent.parent / "index.html"
    html = page.read_text(encoding="utf-8")
    html = re.sub(r'(<script id="cot-data">).*?(</script>)',
                  lambda m: m.group(1) + "window.COT_DATA=" + blob.replace("</", "<\\/") + ";" + m.group(2),
                  html, count=1, flags=re.S)
    page.write_text(html, encoding="utf-8")
    embed_prices(page)
    latest = max(c["dates"][-1] for c in out["contracts"].values())
    print(f"written {OUT}: {len(out['contracts'])} contracts, latest report date {latest}")


if __name__ == "__main__":
    main()
