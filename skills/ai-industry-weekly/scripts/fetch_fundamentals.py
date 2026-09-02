#!/usr/bin/env python3
"""
AI 算力产业链 · 基本面批量取数（yfinance）

标的清单与行序一律读 assets/universe.json，脚本内不内联任何 ticker 列表；
增减标的只改 universe.json，本脚本无需改动。

用法:
    python3 scripts/fetch_fundamentals.py                     # 取全部标的，人类可读打印
    python3 scripts/fetch_fundamentals.py --json out.json     # 同时存一份 JSON
    python3 scripts/fetch_fundamentals.py --tickers NVDA,TSM  # 只取部分（调试/重跑用）

依赖:
    pip install -q yfinance requests
    （遇 PEP668 报错时加 --break-system-packages）
    注意：yfinance 是在 main() 里延迟导入的（不在顶部 import 块），
    目的是让缺库时 --help 仍可用、并给出友好安装提示。

硬约束（踩坑换来的，勿"优化"）:
  1. 必须用 requests.Session 而不是 yfinance 默认的 curl_cffi 引擎。
     本执行环境出站走代理，curl_cffi 会 TLS 握手失败 -> .info 全部为 null。
  2. 3 次重试，成功判定沿用：i and (currentPrice or regularMarketPrice or len(i) > 20)
  3. 字段映射 F 逐字沿用原表 23 个字段，不增不减不改名。
  4. price 回退顺序：currentPrice -> regularMarketPrice -> previousClose
  5. 数据缺失一律记 N/A，绝不估算。
  6. 港股（universe.json 里 hk_quote=true 的行）的 price / hi / fromHi%
     不得用 yfinance，改由 scripts/hk_quote.py 覆盖（见 HK_OVERRIDE 注释）。

退出码: 全部标的取数失败时 1，其余 0（部分失败在结尾汇总，便于按 --tickers 重跑）。
"""

import argparse
import datetime
import json
import subprocess
import sys
import time
import unicodedata
from pathlib import Path

import requests

# yfinance 故意**不**在这里导入，而是延迟到 main() 里（见文件末尾附近的
# `import yfinance as yf`，再作为参数传进 fetch_info()）。这样缺库时
# `--help` 仍可用，且报的是带安装指引的友好错误而不是 ImportError traceback。

# ---------------------------------------------------------------- 路径定位
# 脚本随技能分发，一律以 __file__ 相对定位，从任意 cwd 都能跑。
HERE = Path(__file__).resolve().parent
SKILL_DIR = HERE.parent
UNIVERSE_PATH = SKILL_DIR / "assets" / "universe.json"
HK_QUOTE_SCRIPT = HERE / "hk_quote.py"


def rel_path(path: Path) -> str:
    """技能自身的路径一律相对技能根目录展示。

    取数输出会被贴进运行结果正文、再推到 Slack，绝不能把含本机用户名的
    绝对路径带出去。（用户自己传进来的 --json 落盘路径不走这里。）
    """
    try:
        return str(Path(path).resolve().relative_to(SKILL_DIR))
    except ValueError:
        return Path(path).name

# ---------------------------------------------------------------- 取数配置
# 原表那串 Chrome UA，逐字沿用（换 UA 曾导致 Yahoo 限流返回空 info）。
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

RETRIES = 3
RETRY_SLEEP = 0.8

# 字段映射 F：yfinance .info 键 -> 本表字段名。逐字沿用原表的 23 个字段。
F = {
    "longName": "name",
    "marketCap": "mktcap",
    "trailingPE": "PE",
    "forwardPE": "fwdPE",
    "priceToSalesTrailing12Months": "PS",
    "priceToBook": "PB",
    "enterpriseToEbitda": "EVE",
    "trailingPegRatio": "PEG",
    "grossMargins": "gm",
    "operatingMargins": "om",
    "profitMargins": "nm",
    "returnOnEquity": "ROE",
    "returnOnAssets": "ROA",
    "revenueGrowth": "revG",
    "earningsGrowth": "epsG",
    "freeCashflow": "FCF",
    "totalCash": "cash",
    "totalDebt": "debt",
    "fiftyTwoWeekHigh": "hi",
    "52WeekChange": "chg52",
    "SandP52WeekChange": "spx",
    "targetMeanPrice": "tgt",
    "recommendationKey": "rec",
}

# 打印分组（仅影响人类可读输出的排版，不影响 JSON）
PRINT_GROUPS = [
    ["price", "fromHi%", "hi", "chg52", "spx"],
    ["mktcap", "PE", "fwdPE", "PEG", "PS", "PB", "EVE"],
    ["gm", "om", "nm", "ROE", "ROA"],
    ["revG", "epsG", "FCF", "cash", "debt"],
    ["tgt", "tgt%", "rec"],
]

PCT_LEVEL = {"gm", "om", "nm", "ROE", "ROA"}                 # 水位：不强制正号
PCT_SIGNED = {"revG", "epsG", "chg52", "spx"}                # 变动：强制正负号
PCT_FRACTION = PCT_LEVEL | PCT_SIGNED                        # 这些是分数，需 *100
PCT_ALREADY = {"fromHi%", "tgt%"}                            # 这些本来就是百分数
MONEY = {"mktcap", "FCF", "cash", "debt"}
PRICE_LIKE = {"price", "hi", "tgt"}


# ---------------------------------------------------------------- 小工具
def disp_w(s):
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in s)


def pad(s, width):
    return s + " " * max(0, width - disp_w(s))


def money(v):
    """大额金钱：同时给 B/T 与 亿（产业表正文用「亿」，减少换算出错）。"""
    if v is None:
        return "N/A"
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "N/A"
    a = abs(v)
    if a >= 1e12:
        head = f"{v / 1e12:.2f}T"
    elif a >= 1e9:
        head = f"{v / 1e9:.2f}B"
    elif a >= 1e6:
        head = f"{v / 1e6:.1f}M"
    else:
        head = f"{v:,.0f}"
    return f"{head}({v / 1e8:,.1f}亿)"


def fmt(key, v):
    if v is None or v == "":
        return "N/A"
    if key in MONEY:
        return money(v)
    if isinstance(v, str):
        return v
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    if key in PCT_LEVEL:
        return f"{f * 100:.1f}%"
    if key in PCT_SIGNED:
        return f"{f * 100:+.1f}%"
    if key in PCT_ALREADY:
        return f"{f:+.1f}%"
    if key in PRICE_LIKE:
        return f"{f:,.2f}"
    return f"{f:.2f}"


# ---------------------------------------------------------------- universe
def load_universe(path=UNIVERSE_PATH):
    if not path.exists():
        sys.exit(f"找不到标的清单: {path}")
    with open(path, encoding="utf-8") as fh:
        u = json.load(fh)
    rows = u.get("tickers") or []
    if not rows:
        sys.exit(f"标的清单为空: {path}")
    # 行序以 order 为准（universe.json 是权威行序），order 缺失时退回文件顺序。
    rows = sorted(rows, key=lambda r: r.get("order", 10**6))
    return u, rows


def hk_key(code):
    """0700.HK / 00700.HK / hk00700 一律规约成 5 位数字，用于两侧对齐。"""
    c = str(code).strip().upper().replace("HK.", "").replace(".HK", "")
    c = c[2:] if c.startswith("HK") and c[2:].isdigit() else c
    return c.zfill(5) if c.isdigit() else c


# ---------------------------------------------------------------- yfinance
_SESSION_OK = True


def make_session():
    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    return s


def fetch_info(yf, sym, sess):
    """3 次重试；成功判定沿用原表条件。取不到返回 {}。"""
    global _SESSION_OK
    info = {}
    for attempt in range(RETRIES):
        try:
            if _SESSION_OK:
                try:
                    info = yf.Ticker(sym, session=sess).info
                except TypeError:
                    # 极少数 yfinance 版本移除了 Ticker(session=...)。
                    # 退回默认引擎，但必须显式告警：代理环境下 curl_cffi 可能全 null。
                    _SESSION_OK = False
                    print(
                        "⚠ 当前 yfinance 不支持 Ticker(session=...)，已退回默认引擎；"
                        "若 .info 大面积为 null，请 pip install -U yfinance",
                        file=sys.stderr,
                    )
                    info = yf.Ticker(sym).info
            else:
                info = yf.Ticker(sym).info
            if info and (
                info.get("currentPrice")
                or info.get("regularMarketPrice")
                or len(info) > 20
            ):
                return info
        except Exception:
            info = {}
        if attempt < RETRIES - 1:
            time.sleep(RETRY_SLEEP)
    return info or {}


def build_row(info):
    """原表取数逻辑逐字沿用：price 三级回退，fromHi% = round((p/fh-1)*100, 1)。"""
    p = (
        info.get("currentPrice")
        or info.get("regularMarketPrice")
        or info.get("previousClose")
    )
    fh = info.get("fiftyTwoWeekHigh")
    frm = round((p / fh - 1) * 100, 1) if (p and fh) else None
    row = {"price": p, "fromHi%": frm}
    row.update({v: info.get(k) for k, v in F.items()})
    return row


def add_target_upside(row):
    """派生字段：目标价相对现价的缺口（正文「目标价+46%」那一栏），缺任一端记 None。"""
    p, t = row.get("price"), row.get("tgt")
    row["tgt%"] = round((t / p - 1) * 100, 1) if (p and t) else None
    return row


# ---------------------------------------------------------------- HK_OVERRIDE
def fetch_hk_quotes(codes):
    """
    调 scripts/hk_quote.py --json 取港股原始未复权行情。

    为什么必须覆盖 yfinance：
      - Yahoo 港股报价延迟 >= 15 分钟；
      - yfinance history() 默认股息复权，0700.HK 52周高被调成 675.1（原始 683.0）、
        0941.HK 被调成 86.3（原始 90.6）-> price / hi / fromHi% 随之失真。
    只覆盖 price / hi / fromHi% 三个字段；财务字段（PE/利润率/ROE/现金流等）仍留 yfinance。
    """
    if not codes:
        return {}, []
    if not HK_QUOTE_SCRIPT.exists():
        return {}, [f"找不到 {HK_QUOTE_SCRIPT}，港股价格无法覆盖"]
    cmd = [sys.executable, str(HK_QUOTE_SCRIPT), *codes, "--json"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except Exception as e:
        return {}, [f"hk_quote.py 调用失败: {e}"]
    if r.returncode != 0:
        tail = (r.stderr or "").strip().splitlines()[-3:]
        return {}, [f"hk_quote.py 退出码 {r.returncode}: {' / '.join(tail)}"]
    try:
        items = json.loads(r.stdout)
    except json.JSONDecodeError as e:
        return {}, [f"hk_quote.py 输出不是合法 JSON: {e}"]
    out, warns = {}, []
    for it in items:
        k = hk_key(it.get("code", ""))
        out[k] = it
        if it.get("error"):
            warns.append(f"{it.get('code')} hk_quote 取数失败: {it['error']}")
    return out, warns


def apply_hk_override(row, meta, quote):
    """用 hk_quote 的 last / hi52 / pct_from_hi52 覆盖 yfinance 同名字段。"""
    if not quote or quote.get("error"):
        meta["price_source"] = "yfinance(港股覆盖失败，价格可能为复权/延迟口径)"
        meta["hk_stale"] = None
        return row, [f"{meta['ticker']}: hk_quote 未返回可用报价，price/hi/fromHi% 仍是 yfinance 口径，不可当收盘价用"]
    warns = []
    if quote.get("last") is not None:
        row["price"] = quote["last"]
    if quote.get("hi52") is not None:
        row["hi"] = quote["hi52"]
    if quote.get("pct_from_hi52") is not None:
        row["fromHi%"] = quote["pct_from_hi52"]
    elif row.get("price") and row.get("hi"):
        row["fromHi%"] = round((row["price"] / row["hi"] - 1) * 100, 1)
    meta["price_source"] = "hk_quote.py(原始未复权)"
    meta["hk_market_status"] = quote.get("market_status")
    meta["hk_quote_time"] = quote.get("quote_time")
    meta["hk_age_min"] = quote.get("age_min")
    meta["hk_hi52_source"] = quote.get("hi52_source")
    meta["hk_stale"] = quote.get("stale")
    if quote.get("stale"):
        warns.append(
            f"{meta['ticker']}: hk_quote stale=true（报价时间 {quote.get('quote_time')}，"
            f"龄 {quote.get('age_min')} 分钟，状态 {quote.get('market_status')}）"
            f" -> 该价不得当最新收盘价用，重跑或记 N/A"
        )
    elif quote.get("market_status") and "已收盘" not in str(quote.get("market_status")):
        warns.append(
            f"{meta['ticker']}: market_status={quote.get('market_status')}"
            f"（非「已收盘(收盘价)」）-> 此为盘中/延迟价，写表前确认口径"
        )
    return row, warns


# ---------------------------------------------------------------- 打印
def print_rows(records, uni_meta, warnings, failed, partial):
    print("# AI 算力产业链 · 基本面取数")
    print(f"# 清单: {rel_path(UNIVERSE_PATH)}")
    print(f"# 取数时间: {datetime.datetime.now().astimezone().isoformat(timespec='seconds')}")
    print(f"# 标的数: {len(records)}（清单共 {uni_meta['total']} 个）")
    print("# 缺失一律 N/A，不估算。gm/om/nm/ROE/ROA/revG/epsG/chg52/spx 已由分数换算成 %。")
    print("")
    for rec in records:
        m, row = rec["meta"], rec["row"]
        tags = []
        if m.get("currency"):
            tags.append(m["currency"])
        if m.get("etf"):
            tags.append("ETF")
        if m.get("ratio_distorted"):
            tags.append("比率失真")
        if m.get("price_source", "").startswith("hk_quote"):
            tags.append("港股原始未复权")
        head = f"[{m['order']:>2}] {pad(m['ticker'], 11)} {m.get('theme', '-')}/{m.get('layer', '-')}"
        if tags:
            head += "  [" + " ".join(tags) + "]"
        if m.get("status") != "ok":
            head += f"  <<{m['status']}>>"
        print(head)
        name = row.get("name")
        print(f"     name={name if name else 'N/A'}")
        for group in PRINT_GROUPS:
            print("     " + "  ".join(f"{k}={fmt(k, row.get(k))}" for k in group))
        if m.get("hk_stale") is not None:
            print(
                f"     hk: status={m.get('hk_market_status')}  quote_time={m.get('hk_quote_time')}"
                f"  age_min={m.get('hk_age_min')}  stale={m.get('hk_stale')}  hi52_src={m.get('hk_hi52_source')}"
            )
        print("")

    if warnings:
        print("## 告警")
        for w in warnings:
            print(f"  ⚠ {w}")
        print("")
    if partial:
        print(f"## 取数不完整（{len(partial)}）: 有 info 但拿不到 price，相关字段记 N/A")
        print("  " + ", ".join(partial))
        print("")
    if failed:
        print(f"## 取数失败（{len(failed)}）: 重跑命令")
        print("  " + ", ".join(failed))
        print(f"  python3 {rel_path(Path(__file__))} --tickers {','.join(failed)}" + "   # 从技能目录下跑，或改用绝对路径")
        print("")
    if not failed and not partial:
        print("## 全部标的取数成功")


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(
        description="按 assets/universe.json 批量抓取基本面（yfinance + 港股 hk_quote.py 覆盖）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--tickers", help="只取这些标的（逗号分隔，如 NVDA,TSM,0700.HK）")
    ap.add_argument("--json", metavar="PATH", help="额外把结果写成 JSON")
    ap.add_argument("--quiet", action="store_true", help="只写 JSON，不做人类可读打印")
    args = ap.parse_args()

    uni, rows = load_universe()

    if args.tickers:
        want = [t.strip() for t in args.tickers.split(",") if t.strip()]
        idx = {r["ticker"].upper(): r for r in rows}
        picked, unknown = [], []
        for t in want:
            r = idx.get(t.upper())
            (picked.append(r) if r else unknown.append(t))
        if unknown:
            sys.exit(f"以下代码不在 {UNIVERSE_PATH.name} 里: {', '.join(unknown)}")
        rows = picked

    try:
        import yfinance as yf
    except ImportError:
        sys.exit(
            "缺少 yfinance。请先执行：\n"
            "  pip install -q yfinance requests\n"
            "（PEP668 报错时加 --break-system-packages）"
        )

    sess = make_session()

    # 港股先批量取一次实时行情（一次 subprocess 取全部，省得逐只起进程）
    hk_rows = [r for r in rows if r.get("hk_quote")]
    hk_quotes, warnings = fetch_hk_quotes([r["ticker"] for r in hk_rows])

    records, failed, partial = [], [], []
    for r in rows:
        sym = r["ticker"]
        info = fetch_info(yf, sym, sess)
        row = build_row(info)
        meta = {
            "order": r.get("order"),
            "ticker": sym,
            "theme": r.get("theme"),
            "layer": r.get("layer"),
            "currency": r.get("currency"),
            "etf": bool(r.get("etf")),
            "ratio_distorted": bool(r.get("ratio_distorted")),
            "hk_quote": bool(r.get("hk_quote")),
            "price_source": "yfinance",
            "info_keys": len(info),
        }
        if r.get("hk_quote"):
            row, w = apply_hk_override(row, meta, hk_quotes.get(hk_key(sym)))
            warnings.extend(w)
        add_target_upside(row)

        if not info and row.get("price") is None:
            meta["status"] = "取数失败"
            failed.append(sym)
        elif row.get("price") is None:
            meta["status"] = "取数不完整(无 price)"
            partial.append(sym)
        else:
            meta["status"] = "ok"
        records.append({"meta": meta, "row": row})

    uni_meta = {"total": len(uni.get("tickers") or []), "columns": uni.get("columns")}

    if args.json:
        payload = {
            "fetched_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
            "universe": rel_path(UNIVERSE_PATH),
            "universe_total": uni_meta["total"],
            "requested": len(records),
            "ok": len(records) - len(failed) - len(partial),
            "failed": failed,
            "partial": partial,
            "warnings": warnings,
            "field_map": F,
            "rows": [{**rec["meta"], **rec["row"]} for rec in records],
        }
        out = Path(args.json).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        print(f"JSON 已写入 {out}", file=sys.stderr)

    if not args.quiet:
        print_rows(records, uni_meta, warnings, failed, partial)

    sys.exit(1 if records and len(failed) == len(records) else 0)


if __name__ == "__main__":
    main()
