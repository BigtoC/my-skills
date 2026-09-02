#!/usr/bin/env python3
"""
港股实时行情取数（权威口径：原始未复权）

为什么不用 yfinance / WebSearch 取港股价格：
- Yahoo 港股报价延迟 >= 15 分钟，WebSearch 常返回搜索引擎缓存旧页 -> 价格过时
- yfinance history() 默认股息复权：0700.HK 52周高被调成 675.1（原始 683.0）、
  0941.HK 被调成 86.3（原始 90.6）-> 52周高/T1触发价/回撤% 全部失真
- 腾讯/新浪实时行情的 52 周高字段是前复权口径，与原始日 K 混用必不一致

本脚本：腾讯实时行情（盘中实时、收盘后 = 当日最终收盘价）
        + 腾讯/东财日K线（不复权）自算 252 交易日的 52 周高/低。
纯标准库、无依赖、无 API key。

用法:
    python3 scripts/hk_quote.py 0700.HK 1810.HK 0941.HK          # 对齐表格
    python3 scripts/hk_quote.py 0700.HK 1810.HK --json           # JSON
    python3 scripts/hk_quote.py 0941.HK --csv                    # CSV

校验规则（消费者必读）：
    - quote_time 应为最近一个港股交易日的 16:08 前后（HKT）收盘快照，
      否则该价为盘中/延迟价，不得当收盘价使用。
    - stale=true 表示：当前为港股交易时段但报价已超过 5 分钟未更新，
      或今天为工作日且已过收盘、但报价日期不是今天（假期/数据未更新）。
"""

import csv
import datetime
import io
import json
import sys
import time
import unicodedata
from zoneinfo import ZoneInfo

import requests

HKT = ZoneInfo("Asia/Hong_Kong")
DISP_W = 0


def disp_w(s):
    w = 0
    for ch in s:
        w += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return w


def pad(s, width):
    return s + " " * max(0, width - disp_w(s))


def http_get(url, timeout=12, referer=None, encoding="utf-8"):
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    }
    if referer:
        headers["Referer"] = referer
    r = requests.get(url, headers=headers, timeout=timeout)
    r.raise_for_status()
    return r.content.decode(encoding or "utf-8", errors="replace")


def norm_code(c):
    c = c.strip().upper().replace("HK.", "").replace(".HK", "")
    if not c.isdigit():
        raise ValueError(f"无法识别的港股代码: {c}")
    return c.zfill(5)


def tencent_realtime(codes):
    """一次请求取全部。返回 {code: {...}}，失败个股为 None。"""
    q = ",".join(f"hk{c}" for c in codes)
    txt = http_get(f"https://qt.gtimg.cn/q={q}", encoding="gbk")
    out = {}
    for line in txt.strip().split(";"):
        line = line.strip()
        if not line or "=" not in line or '"' not in line:
            continue
        var, payload = line.split("=", 1)
        code = var.replace("v_hk", "")
        f = payload.strip('"').split("~")
        try:
            if len(f) < 38 or f[2] != code:
                out[code] = None
                continue
            out[code] = {
                "name": f[1],
                "last": float(f[3]),
                "prev_close": float(f[4]),
                "open": float(f[5]),
                "volume": float(f[6]),
                "quote_time": f[30].replace("/", "-"),
                "chg": float(f[31]),
                "chg_pct": float(f[32]),
                "high": float(f[33]),
                "low": float(f[34]),
                "turnover_hkd": float(f[37]),
                "source": "腾讯实时行情(qt.gtimg.cn)",
            }
        except (ValueError, IndexError):
            out[code] = None
    return out


def tencent_kline_raw(code):
    """不复权日K线，返回 (date,open,close,high,low,volume) 列表。"""
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=hk{code},day,,,420,"
    d = json.loads(http_get(url))["data"][f"hk{code}"]
    kl = d.get("day") or d.get("qfqday")
    rows = []
    for k in kl:
        rows.append((k[0], float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5])))
    return rows


def eastmoney_kline_raw(code):
    """东财不复权日K线（回退源），返回与 tencent_kline_raw 相同结构。"""
    end = datetime.date.today().strftime("%Y%m%d")
    beg = (datetime.date.today() - datetime.timedelta(days=400)).strftime("%Y%m%d")
    url = (
        "https://push2his.eastmoney.com/api/qt/stock/kline/get"
        f"?secid=116.{code}&fields1=f1,f2,f3,f4,f5,f6"
        "&fields2=f51,f52,f53,f54,f55,f56,f57&klt=101&fqt=0"
        f"&beg={beg}&end={end}"
    )
    d = json.loads(http_get(url))["data"]
    if not d:
        return []
    rows = []
    for k in d["klines"]:
        p = k.split(",")
        rows.append((p[0], float(p[1]), float(p[2]), float(p[3]), float(p[4]), float(p[5])))
    return rows


def hi52_lo52(code):
    """252 交易日窗口的 52 周高/低（原始未复权），返回 (hi, lo, source) 或 (None, None, src)。"""
    for fetch, src in ((tencent_kline_raw, "腾讯日K线(不复权)"), (eastmoney_kline_raw, "东财日K线(不复权)")):
        try:
            rows = fetch(code)
            if len(rows) < 30:
                continue
            w = rows[-252:]
            hi = max(r[3] for r in w)
            lo = min(r[4] for r in w)
            return hi, lo, src
        except Exception:
            continue
    return None, None, "取数失败"


def market_status(qt_dt, now_hkt):
    """港股交易时段 09:30-12:00 / 13:00-16:00 HKT，周一至周五。"""
    t = qt_dt.time()
    if qt_dt.weekday() >= 5:
        return "休市日(最近交易日收盘)"
    if datetime.time(9, 30) <= t <= datetime.time(12, 0) or datetime.time(13, 0) <= t <= datetime.time(16, 0):
        return "盘中(实时)" if (now_hkt - qt_dt) < datetime.timedelta(minutes=5) else "盘中(报价延迟>5分钟)"
    if t < datetime.time(9, 30):
        return "未开盘(竞价)"
    return "已收盘(收盘价)"


def is_hk_trading_window(now_hkt):
    if now_hkt.weekday() >= 5:
        return False
    t = now_hkt.time()
    return (datetime.time(9, 30) <= t <= datetime.time(12, 0)) or (datetime.time(13, 0) <= t <= datetime.time(16, 0))


def fetch(codes):
    now_hkt = datetime.datetime.now(HKT)
    rt = tencent_realtime(codes)
    out = []
    for c in codes:
        item = {"code": f"{c}.HK"}
        r = rt.get(c)
        if not r:
            item.update({"error": "实时行情取数失败", "stale": None})
            out.append(item)
            continue
        item.update(r)
        try:
            qt = datetime.datetime.strptime(r["quote_time"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=HKT)
        except ValueError:
            qt = now_hkt - datetime.timedelta(days=30)
        age = now_hkt - qt
        item["age_min"] = round(age.total_seconds() / 60, 1)
        item["market_status"] = market_status(qt, now_hkt)
        hi, lo, ksrc = hi52_lo52(c)
        item["hi52"] = hi
        item["lo52"] = lo
        item["hi52_source"] = ksrc
        item["pct_from_hi52"] = round((r["last"] / hi - 1) * 100, 1) if hi else None
        stale = False
        if is_hk_trading_window(now_hkt) and age > datetime.timedelta(minutes=5):
            stale = True
        if now_hkt.weekday() < 5 and now_hkt.time() > datetime.time(16, 30) and qt.date() < now_hkt.date():
            stale = True
        item["stale"] = stale
        out.append(item)
    return out


def print_table(items):
    keys = ("code", "name", "last", "chg_pct", "high", "low", "hi52", "lo52",
            "pct_from_hi52", "quote_time", "age_min", "market_status")
    headers = ("代码", "名称", "最新价", "涨跌%", "最高", "最低", "52周高", "52周低",
               "距52周高%", "报价时间(HKT)", "龄min", "状态")
    fmt = {
        "last": lambda v: f"{v:,.3f}", "chg_pct": lambda v: f"{v:+.2f}",
        "high": lambda v: f"{v:,.3f}", "low": lambda v: f"{v:,.3f}",
        "hi52": lambda v: f"{v:,.3f}" if v is not None else "N/A",
        "lo52": lambda v: f"{v:,.3f}" if v is not None else "N/A",
        "pct_from_hi52": lambda v: f"{v:+.1f}%" if v is not None else "N/A",
        "age_min": lambda v: f"{v}" if v is not None else "-",
    }
    rows = []
    for it in items:
        if "error" in it:
            rows.append([it["code"], "取数失败", *["N/A"] * 10])
            continue
        row = []
        for k in keys:
            v = it.get(k)
            row.append(fmt.get(k, lambda x: str(x) if x is not None else "-")(v))
        rows.append(row)
    widths = [max(disp_w(h), *(disp_w(r[i]) for r in rows)) for i, h in enumerate(headers)]
    print(" | ".join(pad(h, widths[i]) for i, h in enumerate(headers)))
    for r in rows:
        print(" | ".join(pad(r[i], widths[i]) for i in range(len(headers))))
    for it in items:
        if it.get("stale"):
            print(f"⚠ {it['code']} 报价可能过时（stale=true），不得当最新收盘价使用")
    print("口径：原始未复权。52周高/低 = 252交易日不复权日K线自算（来源见 --json 的 hi52_source）。")


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    mode = "json" if "--json" in sys.argv else "csv" if "--csv" in sys.argv else "table"
    if not args:
        print(__doc__)
        sys.exit(1)
    codes = [norm_code(a) for a in args]
    items = fetch(codes)
    if mode == "json":
        print(json.dumps(items, ensure_ascii=False, indent=2))
    elif mode == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["code", "name", "last", "prev_close", "open", "high", "low",
                    "chg", "chg_pct", "volume", "turnover_hkd", "hi52", "lo52",
                    "pct_from_hi52", "hi52_source", "quote_time", "age_min",
                    "market_status", "stale"])
        for it in items:
            if "error" in it:
                w.writerow([it["code"], "ERROR", *[""] * 17])
                continue
            w.writerow([it["code"], it["name"], it["last"], it["prev_close"], it["open"],
                        it["high"], it["low"], it["chg"], it["chg_pct"], it["volume"],
                        it["turnover_hkd"], it["hi52"], it["lo52"], it["pct_from_hi52"],
                        it["hi52_source"], it["quote_time"], it["age_min"],
                        it["market_status"], it["stale"]])
        print(buf.getvalue().rstrip())
    else:
        print_table(items)


if __name__ == "__main__":
    main()
