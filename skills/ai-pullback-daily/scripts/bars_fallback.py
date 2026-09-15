#!/usr/bin/env python3
"""
非 Yahoo 日线回退取数（只回 bar，不算任何指标）

为什么存在：
    2026-09-11 在 Claude Routines 容器实测确认，yfinance 在该环境**结构性不可达**——
    它的数据 host 写死在 const.py 的 `_BASE_URL_ = https://query2.finance.yahoo.com`
    （base.py:179 与 scrapers/history.py:208 都用它拼 /v8/finance/chart/），而该容器的
    出站 agent 代理会把到 query2 的隧道在 ~6 秒后切断（code 1006，发出约 1.7~1.8KB
    ClientHello、只收到 39 字节）。**换传输引擎救不了**：requests.Session 与 curl_cffi
    打的是同一个被封的 host。取 crumb 用的 query1 通得了，但回 HTTP 429。
    两个独立封锁叠加，Yahoo 这条路在该环境已无传输层解法。

    所以回退必须换**上游**，不是换引擎。本模块提供第 1 档之下的替代源。

本模块只做传输，不做判断（与检索传输层同一条分界）：
    返回 bar 与出处，**不返回** 🟢🟡🔴 状态、不做阈值比较、不算 RSI/均线/52周高。
    指标数学只有一份，在 technicals.py 里（compute_row / wilder_rsi / sma_tail）。
    两份实现对同一个 T1/T2/T3 各说各话，是本技能族记录在案的最坏故障。

回退链（顺序写死，见 CLAUDE.md「Fallback chains — the rule」）：
    美股  第0档 yfinance -> 第1档 stockanalysis.com
    韩股  第0档 yfinance -> 第1档 Naver siseJson
    港股  不在本模块内：价格由姊妹技能 hk_quote.py 覆写，日线仍走 yfinance
    指数  ^GSPC/^NDX/^TNX/DX-Y.NYB **无替代档**，取不到就 N/A 并写明原因
          （实测 stockanalysis 的 i/spx、i/ndx 路径回 400；腾讯 us.INX/us.NDX 可用但
           属于另一个源族，未纳入本模块）

口径（**这是本模块最要紧的部分**）：
    stockanalysis 的 `c` 字段 == yfinance auto_adjust=False 的 Close
        —— 拆股已还原、股息未复权。跨 7 次窗口内拆股验证过（NVDA 10:1、TSLA 3:1、
        ANET 4:1 ×2、DELL 1.973:1、SMH 2:1、SOXX 3:1）。
        ⚠ **绝不要用 `a` 字段**，那是 Adj Close（股息已复权），会把 52周高/T1触发价
          按 0700.HK 683.0→675.1 那种方式算歪。
    Naver siseJson 同口径：拆股已还原、股息未复权。
    ⚠ **小数位精度不同（不是口径差，但会让两档数字不逐位相同）**：低价股上 yfinance
      保留 2 位、stockanalysis 保留 3 位。实测 NOK：hi20 = 11.16 vs 11.155，
      连带 T2触发价 10.2672 vs 10.2626（差 0.045%）、dd20% −4.839 vs −4.796。
      **不改变任何 T1/T2/T3 判定**（全 46 档实测 0 处判定差异），但换档那天这些数字
      会有末位变化，不要误读成行情变了。

    ⚠ **成交量口径不同**：stockanalysis 的量系统性低于 yfinance（实测 AVGO −11.1%、
      SMH −5.6%、NVDA −4.6%）。vol_ratio 的分子分母同源、内部自洽，但「放量 ≥1.5x」
      这个阈值是在 yfinance 的量上标定的，**不可跨档沿用**——消费端必须把回退档的
      vol_ratio 标成不可比。本模块在每个标的上回 `volume_comparable: false`。

每一种失败都长成 HTTP 200，所以每一档都必须硬校验：
    stockanalysis  range 只有 5Y/10Y 被遵守，其余值静默回**恰好 252 根**；
                   content-type 必须是 JSON；body 里的 status 必须是 200
    Naver          代码错/窗口反了/周末窗口 -> HTTP 200 + 只有表头的 ~70 字节 body；
                   body 不是合法 JSON（单引号 + 韩文表头），必须 ast.literal_eval；
                   2018 年停牌日会给 O/H/L 全 0 的行；
                   **KRX 开市期间最后一根是未完成 bar，必须丢弃**

用法:
    python3 scripts/bars_fallback.py --tickers NVDA,SMH,005930.KS
    python3 scripts/bars_fallback.py --tickers NVDA --json          # JSON 到 stdout
    python3 scripts/bars_fallback.py --tickers NVDA --json out.json
    python3 scripts/bars_fallback.py --tickers NVDA --quiet --json  # 不渲染表格，告警照出

退出码（沿用本仓库约定）:
    0  跑完了（**即使全部取不到**——那是结果，不是脚本失败）
    1  参数错误
    2  依赖缺失（requests）

依赖: python3 + requests。不需要 yfinance / pandas / numpy —— 这是刻意的：
      该环境的依赖状态本身不稳定（实测三次运行分别缺 numpy、缺 yfinance、依赖齐全），
      回退档的依赖面越小越好。
"""

from __future__ import annotations

import argparse
import ast
import datetime as dt
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from zoneinfo import ZoneInfo

SCRIPT_NAME = "bars_fallback.py"

# 本仓库第 12 份 scrub 实现（CLAUDE.md 记的是 10 份 + source_probe.py 第 11 份）。
# 路径脱敏是公开仓库的防泄漏规则，修它是 N 处编辑，grep '_HOMEISH_RE' 能找全。
_HOMEISH_RE = re.compile(r"(?:/Users|/home|/var/folders)/[^/\s\"']+")


def scrub(s) -> str:
    return _HOMEISH_RE.sub("~", str(s))


def err(msg: str) -> None:
    """告警一律走 stderr。--quiet 只关表格渲染，永远不关告警。"""
    print(msg, file=sys.stderr)


# 档位标签：出现在 --json 里，也会被 technicals.py 抄进 price_source / sources。
# 数字来源不明的读数事后无法复核，所以标签必须跟着数据走。
TIER_STOCKANALYSIS = "stockanalysis.com(c·拆股已还原·股息未复权)"
TIER_NAVER = "naver siseJson(拆股已还原·股息未复权)"

KST = ZoneInfo("Asia/Seoul")

# stockanalysis 与 Naver 都**不需要任何 header**（2026-09-11 实测，含 Routines 容器）。
# 刻意不送浏览器 UA：本仓库的逐 host 规则里，送错 UA 的代价是静默超时或限流，
# 而这两个 host 实测裸请求即可。
HEADERS: dict = {}

# range 被静默降级的特征值。请求 5Y 却恰好回这个数，就是没被遵守。
_RANGE_DEGRADED_BARS = 252


def market_of(ticker: str) -> str:
    """本模块自带市场判定，不 import technicals.py —— 它要能单独跑。

    与 technicals.py 的 market_of 是两份实现，但判定规则只有后缀这一条，
    不存在两边对同一个标的给出不同市场的空间。
    """
    t = ticker.upper()
    if t.endswith(".KS"):
        return "KR"
    if t.endswith(".HK"):
        return "HK"
    return "US"


def _http_get(url, timeout=25):
    """返回 (text, None) 或 (None, 失败原因)。永远不抛。"""
    import requests
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}: {scrub(exc)[:160]}"
    ctype = (r.headers.get("content-type") or "").lower()
    body = r.text
    if r.status_code != 200:
        return None, f"HTTP {r.status_code}"
    low = body[:2000].lstrip().lower()
    # HTTP 200 + HTML = 机器人拦截页（Cloudflare 一类），不是数据
    if low.startswith(("<!doctype", "<html")):
        return None, f"HTTP 200 但返回 HTML（疑似拦截页，content-type={ctype or '未给'}）"
    return body, None


# ---------------------------------------------------------------- 美股：stockanalysis
def fetch_stockanalysis(ticker: str):
    """返回 (rows, tier, warnings, error)。rows = [[YYYY-MM-DD, o,h,l,c,v], ...] 升序。"""
    warns = []
    sym = ticker.lower()
    url = f"https://stockanalysis.com/api/symbol/s/{sym}/history?range=5Y&period=Daily"
    body, e = _http_get(url)
    if e:
        return None, TIER_STOCKANALYSIS, warns, f"{ticker}: {e}"
    try:
        payload = json.loads(body)
    except Exception as exc:  # noqa: BLE001
        return None, TIER_STOCKANALYSIS, warns, f"{ticker}: JSON 解析失败（{scrub(exc)[:120]}）"

    # body 内的 status 与 HTTP 状态码是两回事，两个都要查
    if payload.get("status") != 200:
        return None, TIER_STOCKANALYSIS, warns, f"{ticker}: body status={payload.get('status')}"
    data = payload.get("data")
    raw = data.get("data") if isinstance(data, dict) else data
    if not isinstance(raw, list) or not raw:
        return None, TIER_STOCKANALYSIS, warns, f"{ticker}: data 不是非空数组"

    if len(raw) == _RANGE_DEGRADED_BARS:
        # 只告警不丢弃：恰好 252 也可能是真的上市满一年。但请求的是 5Y，
        # 拿到这个数就必须让消费端知道窗口可能被截断了。
        warns.append(f"{ticker}: 恰好 {_RANGE_DEGRADED_BARS} 根——疑似 range=5Y 被静默降级，"
                     "52周高窗口可能无余量")

    rows = []
    bad = 0
    for it in raw:
        try:
            # c = Close（== yfinance auto_adjust=False）。**绝不用 a（Adj Close）**。
            rows.append([it["t"], float(it["o"]), float(it["h"]),
                         float(it["l"]), float(it["c"]), float(it.get("v") or 0)])
        except (KeyError, TypeError, ValueError):
            bad += 1
    if bad:
        warns.append(f"{ticker}: {bad} 根 bar 字段不全，已剔除")
    if not rows:
        return None, TIER_STOCKANALYSIS, warns, f"{ticker}: 无可用 bar"
    rows.sort(key=lambda r: r[0])          # 该源按时间倒序返回，统一成升序
    return rows, TIER_STOCKANALYSIS, warns, None


# ---------------------------------------------------------------- 韩股：Naver
def _krx_open(now=None) -> bool:
    """KRX 常规时段 09:00–15:30 KST，周一至周五。"""
    n = now or dt.datetime.now(KST)
    if n.weekday() >= 5:
        return False
    return dt.time(9, 0) <= n.time() <= dt.time(15, 30)


def fetch_naver(ticker: str):
    code = ticker.upper().replace(".KS", "")
    warns = []
    # 用 KST 的「今天」，不是本机的今天：跑在美洲时区的机器上，本机日期比首尔早一天，
    # 下面那个「丢弃当日未完成 bar」会误删一根**已经完整**的交易日。
    now_kst = dt.datetime.now(KST)
    today = now_kst.date()
    start = today - dt.timedelta(days=1100)      # ~3 年，够 252 根窗口留足余量
    url = ("https://api.finance.naver.com/siseJson.naver"
           f"?symbol={code}&requestType=1"
           f"&startTime={start:%Y%m%d}&endTime={today:%Y%m%d}&timeframe=day")
    body, e = _http_get(url)
    if e:
        return None, TIER_NAVER, warns, f"{ticker}: {e}"
    text = (body or "").strip()
    # 代码错 / 窗口反了 / 周末窗口 / requestType≠1 -> HTTP 200 + 只有表头的 ~70 字节
    if len(text) < 120:
        return None, TIER_NAVER, warns, f"{ticker}: 只返回表头（{len(text)} 字节），代码或窗口无效"
    try:
        # 不是合法 JSON（单引号 + 韩文表头）。json.loads 会抛
        # JSONDecodeError: Expecting value: line 2 column 4，必须用 literal_eval。
        parsed = ast.literal_eval(text)
    except Exception as exc:  # noqa: BLE001
        return None, TIER_NAVER, warns, f"{ticker}: literal_eval 失败（{scrub(exc)[:120]}）"

    rows, halted = [], 0
    for r in parsed[1:]:
        if not r or not str(r[0]).isdigit():
            continue
        try:
            d, o, h, lo, c = str(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4])
            v = float(r[5]) if len(r) > 5 else 0.0
        except (TypeError, ValueError, IndexError):
            continue
        if o == 0 and h == 0 and lo == 0:        # 停牌日，Naver 给全 0
            halted += 1
            continue
        rows.append([f"{d[:4]}-{d[4:6]}-{d[6:8]}", o, h, lo, c, v])
    if halted:
        warns.append(f"{ticker}: 剔除 {halted} 根 O/H/L 全 0 的停牌 bar")
    if not rows:
        return None, TIER_NAVER, warns, f"{ticker}: 无可用 bar"
    rows.sort(key=lambda r: r[0])

    # KRX 开市期间最后一根是**实时未完成**的 bar。留着会让当日收盘价与量都失真
    # （实测 2026-09-11 12:xx KST：当日量 9,154,964 vs 前一日 22,517,075，0.41×）。
    if _krx_open(now_kst) and rows and rows[-1][0] == today.isoformat():
        rows.pop()
        warns.append(f"{ticker}: KRX 开市中，已丢弃当日未完成 bar")
    return rows, TIER_NAVER, warns, None


# ---------------------------------------------------------------- 编排
def fetch_bars(tickers, max_workers=8):
    """返回 (results, meta)。results[ticker] = {tier, rows, bars, first, last, error}。"""
    def one(t):
        mkt = market_of(t)
        if mkt == "US":
            if t.startswith("^") or "=" in t:
                # 只排真正没有替代档的两类：^ 开头的指数、含 = 的期货/外汇
                # （stockanalysis 的 i/spx、i/ndx 路径实测回 400）。
                # ⚠ 不要按连字符排除：BRK-B / DX-Y.NYB 形态不同，前者是正常美股，
                #   早先的启发式会把它误判成指数并给出错误的失败原因。
                return t, None, None, [], f"{t}: 指数/期货/外汇无替代档，本模块不覆盖"
            return (t, *fetch_stockanalysis(t))
        if mkt == "KR":
            return (t, *fetch_naver(t))
        return t, None, None, [], f"{t}: 港股日线不走本模块（价格由 hk_quote.py 覆写）"

    out, warnings, errors = {}, [], []
    workers = max(1, min(max_workers, len(tickers))) if tickers else 1
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for t, rows, tier, warns, error in pool.map(one, tickers):
            warnings.extend(warns or [])
            if error:
                errors.append(error)
                out[t] = {"tier": tier, "rows": None, "bars": 0,
                          "first": None, "last": None, "error": error,
                          # 取不到就是「无法判定」，不是 false（false = 查过了、不可比）
                          "volume_comparable": None}
                continue
            out[t] = {
                "tier": tier,
                "rows": rows,
                "bars": len(rows),
                "first": rows[0][0],
                "last": rows[-1][0],
                "error": None,
                # 量口径与 yfinance 不同，阈值不可跨档沿用（见模块 docstring）
                "volume_comparable": False,
            }
    meta = {
        "ok": bool(out) and any(v["rows"] for v in out.values()),
        "requested": len(tickers),
        "fetched": sum(1 for v in out.values() if v["rows"]),
        "fallback_order": {
            "US": ["yfinance", TIER_STOCKANALYSIS],
            "KR": ["yfinance", TIER_NAVER],
            "HK": ["yfinance + hk_quote.py 覆写（本模块不覆盖）"],
            "INDEX": ["yfinance（无替代档，取不到即 N/A）"],
        },
        "prohibitions": [
            "stockanalysis 用 c（Close）不用 a（Adj Close）——a 是股息复权价，"
            "会把 52周高/T1触发价算歪。",
            "回退档的成交量口径与 yfinance 不同（实测 AVGO −11.1%、NVDA −4.6%），"
            "「放量 ≥1.5x」阈值不可跨档沿用：每个标的回 volume_comparable=false。",
            "本模块只回 bar，不回状态/阈值比较/指标——指标数学只有 technicals.py 一份。",
        ],
        "degraded": bool(warnings or errors),
        "degraded_reasons": list(warnings) + list(errors),
    }
    return out, meta


def main() -> int:
    ap = argparse.ArgumentParser(
        description="非 Yahoo 日线回退取数（只回 bar，不算指标）")
    ap.add_argument("--tickers", required=True, help="逗号分隔，如 NVDA,SMH,005930.KS")
    ap.add_argument("--json", nargs="?", const="-", metavar="OUT",
                    help="输出 JSON；给路径则写文件，不给则打到 stdout")
    ap.add_argument("--quiet", action="store_true",
                    help="不渲染表格。**不关告警**——告警走 stderr")
    # argparse 默认用法错误回 2，而本仓库 2 保留给依赖缺失
    ap.error = lambda m: (ap.print_usage(sys.stderr), err(f"错误：参数错误——{m}"), sys.exit(1))
    args = ap.parse_args()

    try:
        import requests  # noqa: F401
    except ImportError as exc:
        err(f"错误：缺少依赖 requests（{scrub(exc)}）。请先 `pip install requests`。")
        return 2

    tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    if not tickers:
        err("错误：参数错误——--tickers 为空")
        return 1

    results, meta = fetch_bars(tickers)

    # --json 打到 stdout 时必须静默表格：两者混在一个流里会让调用方 json.load 直接失败。
    # 这不算「--quiet 才关告警」的例外——告警仍走 stderr，一条都不少。
    render = not args.quiet and args.json != "-"
    if render:
        print(f"📊 回退档日线（{meta['fetched']}/{meta['requested']} 取到）")
        w = max((len(t) for t in results), default=6)
        for t in tickers:
            r = results[t]
            if r["error"]:
                print(f"  {t:<{w}}  ❌ {r['error']}")
            else:
                print(f"  {t:<{w}}  ✅ {r['bars']:>5} 根  {r['first']} → {r['last']}  [{r['tier']}]")
        for p in meta["prohibitions"]:
            print(f"  ⛔ {p}")
    for r in meta["degraded_reasons"]:
        err(f"⚠ {r}")

    if args.json:
        text = json.dumps({"meta": meta, "results": results}, ensure_ascii=False, indent=2)
        if args.json == "-":
            print(text)
        else:
            with open(args.json, "w", encoding="utf-8") as fh:
                fh.write(text + "\n")
            err(f"[{SCRIPT_NAME}] JSON 已写入 {args.json}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        err("\n已中断。")
        sys.exit(130)
