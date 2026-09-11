#!/usr/bin/env python3
"""
market.py 的非 Yahoo 收盘价回退档（只回收盘序列，不做任何判定）

为什么存在：
    2026-09-11 在 Claude Routines 容器实测：yfinance 在该环境**结构性不可达**。
    它的数据 host 写死在 yfinance/const.py（`_BASE_URL_ = query2.finance.yahoo.com`），
    而该容器的出站代理会把到 query2 的隧道在 ~6 秒后切断（发出约 1.8KB ClientHello、
    只收到 39 字节、code 1006；20 条中继失败全部指向 query2）。取 crumb 的 query1 通得了
    但回 HTTP 429。**换传输引擎救不了**——`requests.Session` 与 `curl_cffi` 打的是同一个
    被封的 host，所以 market.py 现有的两档引擎回退（session -> curl_cffi）两条腿同时断，
    这就是「market.py 三次全败」的成因。回退必须换**上游**。

为什么不复用 ai-pullback-daily 的 bars_fallback.py：
    `daily-risk-monitor` 是**刻意 standalone** 的（CLAUDE.md：它与 ai-pullback-daily
    不共享代码、无姊妹技能依赖，两边各自保留自己的 fetcher，不得合并）。所以这里是
    第二份实现，**与 bars_fallback.py 同族但不同文件**——改动是两处编辑，与两份 TH
    阈值字典、两份 search-contract.md 同一性质。两边的标的集也确实不同：
    那边是 46 档个股，这边是 ETF + 指数 + 期货 + 加密。

本模块只做传输，不做判断：
    只回收盘序列与出处，**不回** 🟢🟡🔴 状态、不做阈值比较、不算 200DMA / σ / VRP。
    所有指标数学留在 market.py 里。两份实现对同一个信号各说各话，是本仓库记录在案的
    最坏故障。

逐标的回退方案（顺序写死，见 CLAUDE.md「Fallback chains — the rule」）：

    标的          信号          第0档      第1档（本模块）
    SPY           21, 22        yfinance   stockanalysis.com
    RSP           22            yfinance   stockanalysis.com
    TLT           21            yfinance   stockanalysis.com
    GLD           21            yfinance   stockanalysis.com
    UUP           21            yfinance   stockanalysis.com
    ^GSPC         19,20,26,34   yfinance   腾讯 us.INX 日线（实测可取 901 根）
    BTC-USD       19            yfinance   Binance BTCUSDT 日线（**USDT 计价**，见下）
    ^VIX          20            —          **不在本模块**：market.py 早已首选
                                            FRED VIXCLS（subprocess 调 fred.sh）
    GC=F          34            yfinance   **无同口径源** -> N/A
    DX-Y.NYB      33            yfinance   **禁止换源** -> N/A

两条硬禁令（都是本仓库/本脚本既有规则，不是本模块新增的）：

  ⛔ **DX-Y.NYB 绝不换源顶替。** market.py 档头已写死：FRED `DTWEXBGS` 是广义美元指数、
     **量级不同不可混用**，取不到就只标 N/A。本模块对 DXY **主动拒绝**并给出这个理由，
     而不是"试一下失败再说"——否则后来人很容易"顺手"接一个 DTWEXBGS 上去。
     另注：腾讯 `usDX` 返回的是「德尼克斯投资」(DX.N) 这家公司，**不是美元指数**，
     是个会静默出错的同名陷阱，实测已确认，任何时候都别用。

  ⛔ **GC=F 不可用 GLD / PAXG 顶替。** GLD 是黄金 ETF（含管理费与折溢价）、
     PAXG 是代币化黄金，两者与 COMEX 黄金期货**口径不同**。信号 34 是 Gold/SPX 比值，
     换了分子就换了这个比值的定义。腾讯 `hf_GC` 只有报价、没有日线（实测 `param error`），
     故本模块对 GC=F 记 N/A。信号 34 属周一附加、不计入 30 信号，N/A 不影响主判定。

口径说明（消费端必读）：
  * stockanalysis 用 `c` 字段 == yfinance auto_adjust=False 的 Close（拆股已还原、
    股息未复权）。**绝不用 `a`**（Adj Close，股息已复权）。
  * 腾讯 us.INX 是 S&P 500 指数点位，与 ^GSPC 同一标的。实测 2026-09-10 收盘
    7591.700 vs yfinance 7591.7002，一致。
  * **BTC-USD -> Binance BTCUSDT 是 USDT 计价，不是 USD**。信号 19 用的是**收益率序列**
    （σ 倍数），计价单位在收益率上基本抵消，但这仍是一次跨口径替换：本模块在该标的上
    回 `caliber_note`，消费端必须把它写进 warnings。四源交叉验证过现货价差在 0.1% 内
    （Binance 77894.57 / CoinGecko 77893 / Coinbase 77949.28 / Kraken 77865.30）。

用法:
    python3 scripts/market_fallback.py --tickers SPY,^GSPC,BTC-USD
    python3 scripts/market_fallback.py --tickers SPY --json           # JSON 到 stdout
    python3 scripts/market_fallback.py --tickers SPY --json out.json
    python3 scripts/market_fallback.py --quiet --json                 # 不渲染表格，告警照出

退出码（沿用本仓库约定）:
    0  跑完了（**即使全部取不到**——那是结果，不是脚本失败）
    1  参数错误
    2  依赖缺失（requests）

依赖: python3 + requests。**不需要 yfinance / pandas / numpy** —— 刻意的：该环境依赖
      状态本身不稳定（实测三次运行分别缺 numpy、缺 yfinance、依赖齐全），回退档的依赖面
      越小越好。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor

SCRIPT_NAME = "market_fallback.py"

# 本仓库又一份 scrub（路径脱敏是公开仓库的防泄漏规则，改它是 N 处编辑，
# grep '_HOMEISH_RE' 能找全；数目以 grep 为准，不要相信任何文档里写死的数字）。
_HOMEISH_RE = re.compile(r"(?:/Users|/home|/var/folders)/[^/\s\"']+")


def scrub(s) -> str:
    return _HOMEISH_RE.sub("~", str(s))


def err(msg: str) -> None:
    """告警一律走 stderr。--quiet 只关表格渲染，永远不关告警。"""
    print(msg, file=sys.stderr)


TIER_SA = "stockanalysis.com(c·拆股已还原·股息未复权)"
TIER_TENCENT = "腾讯 us.INX 日线(S&P500 指数点位)"
TIER_BINANCE = "Binance BTCUSDT 日线(**USDT 计价**，非 USD)"

# 逐 host header 规则：stockanalysis 与 Binance 裸请求即可；腾讯沿用 hk_quote.py 那串 UA。
# （与 FRED 方向相反——FRED 绝不能送浏览器 UA。方向差在 header，不在语言。）
BROWSER_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

# ETF 走 stockanalysis 的 s/ 前缀（股票与 ETF 共用，实测 5/5 各 1255 根）
_SA_TICKERS = {"SPY", "RSP", "TLT", "GLD", "UUP"}

# 主动拒绝的标的：理由写死在这里，避免后来人"顺手"接一个量级不同的源上去
_REFUSED = {
    "DX-Y.NYB": ("禁止换源顶替：FRED DTWEXBGS 是广义美元指数、量级不同不可混用；"
                 "腾讯 usDX 返回的是「德尼克斯投资」(DX.N) 这家公司、是同名陷阱。"
                 "market.py 档头既有规则：DX-Y.NYB 取不到就只标 N/A。"),
    "GC=F": ("无同口径源：GLD 是含管理费与折溢价的黄金 ETF、PAXG 是代币化黄金，"
             "与 COMEX 黄金期货口径不同；换了分子就换了 Gold/SPX 比值的定义。"
             "腾讯 hf_GC 只有报价无日线（实测 param error）。信号 34 记 N/A。"),
    "^VIX": ("不在本模块：market.py 早已首选 FRED VIXCLS（subprocess 调 fred.sh），"
             "与信号 4／硬阈值 1 同源同日。本模块不重复实现。"),
}


def _http_get(url, params=None, headers=None, timeout=25):
    """返回 (text, None) 或 (None, 失败原因)。永远不抛。"""
    import requests
    try:
        r = requests.get(url, params=params, headers=headers or {}, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}: {scrub(exc)[:150]}"
    if r.status_code != 200:
        return None, f"HTTP {r.status_code}"
    body = r.text
    # 每一种失败都可能长成 HTTP 200：拦截页就是 200 + HTML
    if body[:2000].lstrip().lower().startswith(("<!doctype", "<html")):
        ct = (r.headers.get("content-type") or "未给")
        return None, f"HTTP 200 但返回 HTML（疑似机器人拦截页，content-type={ct}）"
    return body, None


def fetch_stockanalysis(ticker: str):
    """ETF/个股。返回 (points, tier, note, error)；points = [(YYYY-MM-DD, close), ...] 升序。"""
    body, e = _http_get(
        f"https://stockanalysis.com/api/symbol/s/{ticker.lower()}/history",
        params={"range": "5Y", "period": "Daily"})
    if e:
        return None, TIER_SA, None, f"{ticker}: {e}"
    try:
        payload = json.loads(body)
    except Exception as exc:  # noqa: BLE001
        return None, TIER_SA, None, f"{ticker}: JSON 解析失败（{scrub(exc)[:100]}）"
    if payload.get("status") != 200:
        return None, TIER_SA, None, f"{ticker}: body status={payload.get('status')}"
    data = payload.get("data")
    raw = data.get("data") if isinstance(data, dict) else data
    if not isinstance(raw, list) or not raw:
        return None, TIER_SA, None, f"{ticker}: data 不是非空数组"
    pts = []
    for it in raw:
        try:
            # c = Close（== yfinance auto_adjust=False）。绝不用 a（Adj Close）。
            pts.append((it["t"], float(it["c"])))
        except (KeyError, TypeError, ValueError):
            continue
    if not pts:
        return None, TIER_SA, None, f"{ticker}: 无可用收盘价"
    pts.sort()
    note = None
    if len(pts) == 252:
        # range 只有 5Y/10Y 被遵守，其余值静默回恰好 252 根
        note = f"{ticker}: 恰好 252 根，疑似 range=5Y 被静默降级"
    return pts, TIER_SA, note, None


def fetch_spx():
    """^GSPC -> 腾讯 us.INX 日线。信号 26 要 200DMA + 20日斜率，故要够深。"""
    body, e = _http_get(
        "https://web.ifzq.gtimg.cn/appstock/app/kline/kline",
        params={"param": "us.INX,day,,,900"},
        headers={"User-Agent": BROWSER_UA})
    if e:
        return None, TIER_TENCENT, None, f"^GSPC: {e}"
    try:
        d = json.loads(body)["data"]["us.INX"]
        kl = d.get("day") or d.get("qfqday") or []
        # 腾讯日线一行是 [日期, 开, 收, 高, 低, 量]——**收在第 3 位**，不是最后一位
        pts = sorted((k[0], float(k[2])) for k in kl)
    except Exception as exc:  # noqa: BLE001
        return None, TIER_TENCENT, None, f"^GSPC: 结构不符（{scrub(exc)[:100]}）"
    if not pts:
        return None, TIER_TENCENT, None, "^GSPC: 无可用收盘价"
    note = None
    if len(pts) < 220:
        # 200DMA + 20日斜率至少要 220 根，不够就必须让消费端知道
        note = f"^GSPC: 仅 {len(pts)} 根 < 220，信号 26 的 200DMA/斜率可能算不出"
    return pts, TIER_TENCENT, note, None


def fetch_btc():
    """BTC-USD -> Binance BTCUSDT。**跨口径替换**（USDT 计价），note 必须带出去。"""
    body, e = _http_get("https://api.binance.com/api/v3/klines",
                        params={"symbol": "BTCUSDT", "interval": "1d", "limit": 1000})
    if e:
        return None, TIER_BINANCE, None, f"BTC-USD: {e}"
    try:
        kl = json.loads(body)
        pts = sorted(
            (dt.datetime.fromtimestamp(k[0] / 1000, dt.UTC).date().isoformat(), float(k[4]))
            for k in kl)
    except Exception as exc:  # noqa: BLE001
        return None, TIER_BINANCE, None, f"BTC-USD: 结构不符（{scrub(exc)[:100]}）"
    if not pts:
        return None, TIER_BINANCE, None, "BTC-USD: 无可用收盘价"
    note = ("BTC-USD 由 Binance BTCUSDT 顶替：**USDT 计价、非 USD**，是一次跨口径替换。"
            "信号 19 用收益率序列（σ 倍数），计价单位在收益率上基本抵消，但该替换必须"
            "写进 warnings，不得当作同一标的。")
    return pts, TIER_BINANCE, note, None


def fetch_closes(tickers, max_workers=6):
    """返回 (results, meta)。results[ticker] = {tier, points, bars, first, last, note, error}。"""
    def one(t):
        u = t.upper()
        if u in _REFUSED:
            return t, None, None, None, f"{t}: {_REFUSED[u]}"
        if u in _SA_TICKERS:
            return (t, *fetch_stockanalysis(t))
        if u == "^GSPC":
            return (t, *fetch_spx())
        if u == "BTC-USD":
            return (t, *fetch_btc())
        return t, None, None, None, f"{t}: 本模块未覆盖该标的（无已验证的同口径替代源）"

    out, notes, errors = {}, [], []
    workers = max(1, min(max_workers, len(tickers))) if tickers else 1
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for t, pts, tier, note, error in pool.map(one, tickers):
            if note:
                notes.append(note)
            if error:
                errors.append(error)
                out[t] = {"tier": tier, "points": None, "bars": 0, "first": None,
                          "last": None, "note": note, "error": error}
                continue
            out[t] = {"tier": tier, "points": pts, "bars": len(pts),
                      "first": pts[0][0], "last": pts[-1][0], "note": note, "error": None}
    meta = {
        # ok 不是字面量：任一标的失败即 false
        "ok": bool(out) and all(v["points"] for v in out.values()),
        "requested": len(tickers),
        "fetched": sum(1 for v in out.values() if v["points"]),
        "fallback_order": {
            "SPY/RSP/TLT/GLD/UUP": ["yfinance", TIER_SA],
            "^GSPC": ["yfinance", TIER_TENCENT],
            "BTC-USD": ["yfinance", TIER_BINANCE],
            "^VIX": ["FRED VIXCLS(fred.sh)", "yfinance"],
            "GC=F": ["yfinance", "无同口径源 -> N/A"],
            "DX-Y.NYB": ["yfinance", "禁止换源 -> N/A"],
        },
        "prohibitions": [
            "DX-Y.NYB 绝不换源顶替（DTWEXBGS 量级不同；腾讯 usDX 是「德尼克斯投资」"
            "这家公司的同名陷阱）——取不到只标 N/A。",
            "GC=F 不可用 GLD / PAXG 顶替（ETF 与代币化黄金，与 COMEX 期货口径不同）。",
            "stockanalysis 用 c 不用 a（a 是股息复权价）。",
            "BTC-USD 的 Binance 替代是 USDT 计价，属跨口径替换，必须写进 warnings。",
            "本模块只回收盘序列，不回状态/阈值比较——指标数学只在 market.py 一份。",
        ],
        "degraded": bool(notes or errors),
        "degraded_reasons": list(notes) + list(errors),
    }
    return out, meta


def main() -> int:
    ap = argparse.ArgumentParser(description="market.py 的非 Yahoo 收盘价回退档")
    ap.add_argument("--tickers", required=True, help="逗号分隔，如 SPY,^GSPC,BTC-USD")
    ap.add_argument("--json", nargs="?", const="-", metavar="OUT")
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

    tickers = [t.strip() for t in args.tickers.split(",") if t.strip()]
    if not tickers:
        err("错误：参数错误——--tickers 为空")
        return 1

    results, meta = fetch_closes(tickers)

    # --json 打到 stdout 时静默表格：两者混流会让调用方 json.load 直接失败
    if not args.quiet and args.json != "-":
        print(f"📈 market 回退档收盘序列（{meta['fetched']}/{meta['requested']} 取到）")
        w = max((len(t) for t in results), default=8)
        for t in tickers:
            r = results[t]
            if r["error"]:
                print(f"  {t:<{w}}  ❌ {r['error'][:110]}")
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
