"""下载 CFTC Disaggregated 报告（期货+期权合并），生成 docs/data/cot.json（仅黄金、白银）。

分组对应关系（按视频博主的数值量级核对过）：
  商业套保 comm  = Producer/Merchant/Processor/User
  大户     large = Managed Money
  散户     specs = Other Reportables

增量更新：已有数据按日期合并，每次只重新下载最近两年的 zip；
首次运行（或加 --full）会下载 START_YEAR 起的全部历史。
"""
import csv, io, json, re, sys, zipfile, urllib.request
from datetime import date, datetime, timedelta, timezone
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


PRICE_SYMS = {"088691": "GC=F", "084691": "SI=F"}  # 黄金用连续合约 GC=F；白银 SI=F 仅用于较早的历史，近期改用主力合约（见 silver_weekly）
REBUILD_SILVER = "--rebuild-silver" in sys.argv
MONTH_CODES = "FGHJKMNQUVXZ"   # 1~12 月的期货月份代码
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
    rows.sort()
    # Yahoo 周线末尾常多出一行"当天日期"的重复周（与前一行同属一周）：丢掉，保留以周一为日期的那根
    mon = lambda d: (date.fromisoformat(d) - timedelta(days=date.fromisoformat(d).weekday()))
    while len(rows) >= 2 and mon(rows[-1][0]) == mon(rows[-2][0]):
        rows.pop()
    return rows


def fetch_daily(sym, rng, with_volume=False):
    """Yahoo 日线 -> [[日期, 开, 高, 低, 收(, 量)], ...]；同一天只保留第一条。"""
    last = None
    for host in ("query1.finance.yahoo.com", "query2.finance.yahoo.com"):
        url = f"https://{host}/v8/finance/chart/{sym}?range={rng}&interval=1d"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                res = json.loads(r.read().decode("utf-8"))["chart"]["result"]
            if not res:
                raise ValueError("Yahoo 没有这个代码的数据")
            res = res[0]
            break
        except Exception as e:
            last = e
    else:
        raise RuntimeError(last)
    off, q = res["meta"]["gmtoffset"], res["indicators"]["quote"][0]
    seen = {}
    for i, t in enumerate(res["timestamp"]):
        if None in (q["open"][i], q["high"][i], q["low"][i], q["close"][i]):
            continue
        d = datetime.fromtimestamp(t + off, timezone.utc).date().isoformat()
        row = [d] + [round(q[k][i], 2) for k in ("open", "high", "low", "close")]
        if with_volume:
            row.append(q["volume"][i] or 0)
        seen.setdefault(d, row)
    return sorted(seen.values())


def pick_silver_contract():
    """在未来 14 个月的白银合约里，挑最近 10 个交易日成交量最大的一个。"""
    today = datetime.now(timezone.utc).date()
    best, best_vol = None, -1
    for k in range(0, 14):
        y, m = divmod(today.year * 12 + today.month - 1 + k, 12)
        sym = f"SI{MONTH_CODES[m]}{str(y)[-2:]}.CMX"
        try:
            rows = fetch_daily(sym, "1mo", with_volume=True)
        except Exception:
            continue
        vol = sum(r[5] for r in rows[-10:])
        if vol > best_vol:
            best, best_vol = sym, vol
    if not best or best_vol <= 0:
        raise RuntimeError("没有找到有成交量的白银合约")
    print(f"白银主力合约：{best}（最近 10 日成交量 {best_vol}）")
    return best


def to_weekly(daily):
    """日线按周一分组聚合成周线：开=首日开，高=最高，低=最低，收=末日收。"""
    weeks = {}
    for d, o, h, l, c in daily:
        mon = (date.fromisoformat(d) - timedelta(days=date.fromisoformat(d).weekday())).isoformat()
        w = weeks.get(mon)
        if w is None:
            weeks[mon] = [mon, o, h, l, c]
        else:
            w[2] = max(w[2], h); w[3] = min(w[3], l); w[4] = c
    return [weeks[k] for k in sorted(weeks)]


def silver_weekly(old, rebuild):
    """白银周线：Yahoo 的 SI=F 在非主力月份有大量一字线，所以近期改用成交量最大的主力合约的日线聚合成周线。
    每次只覆盖最近 3 周并补新周，已存历史不改（主力换月当周有小的价差，不做复权）。
    首次迁移用 --rebuild-silver：主力合约可用数据之前的周保留 SI=F 周线。"""
    sym = pick_silver_contract()
    raw = fetch_daily(sym, "2y", with_volume=True)
    last_flat = max((i for i, r in enumerate(raw) if r[1] == r[2] == r[3] == r[4]), default=-1)
    daily = fix_latest_rows([r[:5] for r in raw[last_flat + 1:]], sym)
    if len(daily) < 100:
        raise ValueError(f"{sym} 可用数据只有 {len(daily)} 行")
    new = to_weekly(daily)[1:]          # 起始那一周不完整，丢掉，沿用原来的整周
    if rebuild or not old:
        base = fetch_weekly("SI=F")
        return [r for r in base if r[0] < new[0][0]] + new
    cutoff = (datetime.now(timezone.utc).date() - timedelta(days=21)).isoformat()
    have = {r[0]: r for r in old}
    for r in new:
        if r[0] >= cutoff or r[0] not in have:
            have[r[0]] = r
    return [have[k] for k in sorted(have)]


def yahoo_sessions(sym, rng="7d"):
    """用 Yahoo 小时线合成"交易日"：COMEX 期货每个交易日从前一天美东 18:00 到当天 17:00。
    返回按日期排序的 [(日期, [开, 高, 低, 收], 小时线根数)]。"""
    last = None
    for host in ("query1.finance.yahoo.com", "query2.finance.yahoo.com"):
        url = f"https://{host}/v8/finance/chart/{sym}?range={rng}&interval=1h"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                res = json.loads(r.read().decode("utf-8"))["chart"]["result"]
            if not res:
                raise ValueError("Yahoo 没有这个代码的小时线")
            res = res[0]
            break
        except Exception as e:
            last = e
    else:
        raise RuntimeError(last)
    off, q = res["meta"]["gmtoffset"], res["indicators"]["quote"][0]
    sess = {}
    for i, t in enumerate(res["timestamp"]):
        if None in (q["open"][i], q["high"][i], q["low"][i], q["close"][i]):
            continue
        et = datetime.fromtimestamp(t + off, timezone.utc)                # 交易所本地时间
        day = (et + timedelta(hours=6)).date()                            # 18:00 起算下一个交易日
        if day.weekday() >= 5:
            continue                                                      # 周五 18:00 之后到周日 18:00 休市
        r = sess.get(day.isoformat())
        if r is None:
            sess[day.isoformat()] = [q["open"][i], q["high"][i], q["low"][i], q["close"][i], 1]
        else:
            r[1] = max(r[1], q["high"][i]); r[2] = min(r[2], q["low"][i]); r[3] = q["close"][i]; r[4] += 1
    return [(d, [round(x, 2) for x in v[:4]], v[4]) for d, v in sorted(sess.items())]


def fix_latest_rows(rows, sym):
    """Yahoo 日线有个毛病：美东 18:00 新交易日开盘之后到午夜之前，最后一根日线是"新交易日刚开盘的几个小时"，
    却贴着刚结束那一天的日期，把那天完整的日线顶替掉了（北京时间约 06:00~12:00 抓取会撞上）。
    用小时线合成的交易日识别并修复：某天的日线若开盘价等于"下一个交易日"的开盘价（而不是自己的开盘价），就判定被污染，
    改用该日自己的小时线合成值；日线里缺失的完整交易日也用小时线补上。小时线抓不到时原样返回。"""
    try:
        sess = yahoo_sessions(sym)
    except Exception as e:
        print(f"warn: {sym} 小时线抓取失败，最后一根日线可能不准：{e}", file=sys.stderr)
        return rows
    by = {r[0]: list(r[:5]) for r in rows}
    fixed = []
    for i, (d, ohlc, n) in enumerate(sess):
        row = by.get(d)
        nxt = sess[i + 1][1] if i + 1 < len(sess) else None
        corrupted = bool(row and nxt and abs(row[1] - nxt[0]) <= 0.011 and abs(row[1] - ohlc[0]) > 0.011)
        if (corrupted or row is None) and n >= 20:
            by[d] = [d] + ohlc
            fixed.append(d)
        elif corrupted:
            del by[d]
    if fixed:
        print(f"{sym}: 用小时线修复了 {len(fixed)} 根被污染/缺失的日线 {fixed}")
    return [by[k] for k in sorted(by)]


def recent_weeks_from_daily(sym):
    """用修复过的日线聚合最近几周的周线（第一周在 1 个月窗口里可能不完整，丢掉）。"""
    return to_weekly(fix_latest_rows(fetch_daily(sym, "1mo"), sym))[1:]


def embed_prices(page):
    """抓周线并写进 index.html 的 price-data；任何品种失败都保留旧数据。"""
    html = page.read_text(encoding="utf-8")
    m = re.search(r'<script id="price-data">window\.PRICE_DATA=(.*?);</script>', html, re.S)
    prices = json.loads(m.group(1)) if m else {}
    for code, sym in PRICE_SYMS.items():
        try:
            if code == "084691":
                rows = silver_weekly(prices.get(code), REBUILD_SILVER)
            else:
                rows = fetch_weekly(sym)
                try:      # 最近几周：Yahoo 周线末尾可能被新交易日的开盘部分污染，用修复后的日线重新聚合
                    fresh = {r[0]: r for r in recent_weeks_from_daily(sym)}
                    rows = sorted([r for r in rows if r[0] not in fresh] + list(fresh.values()))
                except Exception as e:
                    print(f"warn: {sym} 最近几周周线修复失败，沿用 Yahoo 周线：{e}", file=sys.stderr)
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
