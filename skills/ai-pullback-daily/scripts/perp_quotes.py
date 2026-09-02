#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""perp_quotes.py —— 第二步之二：盘后 / 休市期间隐含变动（24/7 永续）

对应 references/perp-overnight.md，逐条实现其硬约束。

========================= 硬 性 提 醒 =========================
本节是**纯观察节点**。它 **绝不产生 T1/T2/T3 触发、绝不改变分桶结论**。
T1/T2/T3 永远只用「最近一个完整交易日收盘价」计算。
隐含变动若会让某股跨过阈值，只能写成**预告**：
    「若明日以此价开盘将触及 T2@$XXX（预告，非已触发）」
不得记为已触发，不得变更「✅ 未触发」清单。
==============================================================

取数：POST https://api.hyperliquid.xyz/info  body {"type":"metaAndAssetCtxs","dex":"xyz"}
**必须带 "dex":"xyz"**。Hyperliquid **主池**的 SPX 是 SPX6900 迷因币（$0.34），
不是标普 500；取错池会得到完全错误的数字。

纯标准库（urllib），零第三方依赖，与 neocloud_credit_lite.py 同风格。

用法：
    python3 scripts/perp_quotes.py                       # 24/7 永续隐含变动
    python3 scripts/perp_quotes.py --json out.json       # 同时落盘结构化结果
    python3 scripts/perp_quotes.py --spot tech.json      # 用 technicals.py --json 的现货收盘价
                                                         # 未提供 --spot 时只输出 perp 价与 OI 分档

标的清单读姊妹技能 ai-industry-weekly 的 assets/universe.json（不内联 ticker 列表、
不硬编码标的数）。姊妹技能的定位顺序 / 探针 / 环境变量语义由共享模块 `_weekly.py`
统一（三个脚本必须落到同一份周更安装，否则日报会自相矛盾），见该模块文档字符串。

--spot 接受 technicals.py --json 的输出，读取容错（下列任一形状均可）：
    {"tickers": {"NVDA": {"close": 217.44, "data_date": "2026-09-01"}, ...},
     "indices": {"^GSPC": {"close": 7631.47}, "^NDX": {"close": 29077.22}}}
    {"NVDA": 217.44, "^GSPC": 7631.47, ...}
    [{"ticker": "NVDA", "close": 217.44}, ...]
（^GSPC/^NDX 由 technicals.py 放在顶层 indices 键下——容器键只展开一层，
  嵌在 macro.backdrop 里是取不到的；没有这两个收盘价就算不出「科技相对强弱」。）
记录里若带 t1_price / t2_price，会据此给出「预告，非已触发」的跨阈值提示。
"""

import argparse
import json
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_NAME = Path(__file__).name

_HOMEISH_RE = re.compile(r"(?:/Users|/home|/var/folders)/[^/\s\"']+")


def scrub(text) -> str:
    """把文本里的家目录绝对路径折叠掉，用于错误信息与异常讯息。

    异常对象的 str() 几乎总带完整绝对路径（OSError 尤甚）。脚本输出会被贴进
    日报正文并推 Slack，所以任何 f"...{scrub(exc)}" 都必须先过这里。
    """
    s = str(text)
    try:
        home = str(Path.home())
    except Exception:
        home = ""
    if home:
        s = s.replace(home, "~")
    return _HOMEISH_RE.sub("~", s)


# 姊妹技能定位是三个脚本共用的逻辑，收敛在同目录的 _weekly.py 里。
# 以 `python3 /abs/path/scripts/perp_quotes.py` 方式调用时同目录 import 本来就成立，
# 这里再显式把脚本目录加进 sys.path，保证 -P / PYTHONSAFEPATH 等场景下也稳。
_SCRIPTS_DIR = str(Path(__file__).resolve().parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from _weekly import (  # noqa: E402  （必须在上面的 sys.path 之后）
    NEED_UNIVERSE,
    locate_weekly_skill_or_exit,
    rel_display,
)

API_URL = "https://api.hyperliquid.xyz/info"
DEX = "xyz"                                     # 绝不可省：主池 SPX 是迷因币
PREFIX = f"{DEX}:"

# —— 分档门槛（名义 OI = markPx × openInterest）——
# references/perp-overnight.md 里那张 2026-07-27 实测表是**基线不是定论**，
# 因此这里只保留门槛，每次运行都用当天的 OI 现场分档。
OI_MAIN = 10_000_000.0      # 主用：读数可信
OI_THIN = 3_000_000.0       # 薄盘：$3–10M，须标注「（薄盘 $X.XM，仅参考）」
                            # < $3M 过薄：直接跳过，不输出

MIN_ABS_MOVE = 2.0          # 只输出 |隐含变动| ≥ 2%
MAJOR_MOVE = 5.0            # ≥ 5% 视为重大异动，须 WebSearch 查起因
RATIO_LO, RATIO_HI = 0.9, 1.1   # 1:1 核对区间，落区间外 → 判定取错市场并跳过

# 非同名标的 → xyz 市场名。其余标的默认同名（NVDA → xyz:NVDA）。
MARKET_ALIASES = {
    "000660.KS": "SKHX",    # SK海力士。**务必 SKHX**，见 FORBIDDEN_MARKETS
    "005930.KS": "SMSN",    # 三星电子 USD 口径；是否 1:1 由下方核对把关
}
# 绝不可用作任何标的的映射：SKHY 与 SKHX 是两个口径（比值约 0.13），混用即错。
FORBIDDEN_MARKETS = {"SKHY"}

# 非美元计价标的的换算汇率市场（同池取，保证与 perp 报价同源）
FX_MARKETS = {"KRW": "KRW"}     # 1 USD = <markPx> KRW

# 大盘层：(xyz 市场, 现货代码, 中文名)
INDEX_PAIRS = [
    ("SP500", "^GSPC", "标普500"),
    ("XYZ100", "^NDX", "纳斯达克100"),
]
# 现货侧代码的等价写法（--spot 里怎么写都能认出来）
SPOT_ALIASES = {
    "^GSPC": ("^GSPC", "GSPC", "SPX", "SP500", "SPY500", "标普500"),
    "^NDX": ("^NDX", "NDX", "NASDAQ100", "NDX100", "QQQ100", "纳斯达克100"),
}

CLOSE_KEYS = ("close", "close_price", "closing_price", "last_close", "prev_close",
              "price", "last", "spot", "收盘价", "收盘")
DATE_KEYS = ("data_date", "close_date", "date", "asof", "as_of", "数据日期", "取数日期")
T1_KEYS = ("t1_price", "t1_trigger", "t1", "T1触发价")
T2_KEYS = ("t2_price", "t2_trigger", "t2", "T2触发价")
CONTAINER_KEYS = ("tickers", "data", "quotes", "stocks", "results", "rows",
                  "macro", "indices", "index", "benchmarks", "大盘", "个股")
TICKER_KEY_RE = re.compile(r"^\^?[A-Z0-9][A-Z0-9.\-]{0,11}$")


# --------------------------------------------------------------------------
# 路径与输出
# --------------------------------------------------------------------------
def err(msg: str) -> None:
    print(msg, file=sys.stderr)


def find_sibling_skill() -> Path:
    """定位姊妹技能 ai-industry-weekly（候选顺序/探针/env 语义见 _weekly.py）。

    本脚本需要的是 assets/universe.json，因此 require=NEED_UNIVERSE。
    环境变量语义与另两个脚本完全一致：设了却不合格就报错退出，绝不静默回退——
    早先这里是「warn 后回退」，而 industry_table.py 是「静默回退」，
    于是同一次日更的第一步与第二步可能读到两份不同安装，两边还都 exit 0。
    """
    return locate_weekly_skill_or_exit(require=NEED_UNIVERSE)


def load_universe():
    """返回 [(ticker, currency), ...]，保持 universe.json 的行序。"""
    sibling = find_sibling_skill()
    manifest = sibling / "assets" / "universe.json"
    # 共享的 rel_display 已保证：装在别处时回显形如 .../ai-industry-weekly/assets/universe.json，
    # 既读得懂又不带出绝对家目录路径。
    where = rel_display(manifest)
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        err(f"错误：读取 {where} 失败：{scrub(exc)}")
        sys.exit(1)
    rows = data.get("tickers") if isinstance(data, dict) else None
    if not isinstance(rows, list) or not rows:
        err(f"错误：{where} 缺少非空的 tickers 列表。")
        sys.exit(1)
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        tic = str(row.get("ticker", "")).strip()
        if tic:
            out.append((tic, str(row.get("currency") or "USD").strip().upper()))
    if not out:
        err(f"错误：{where} 里没有可用的 ticker。")
        sys.exit(1)
    return out, where


# --------------------------------------------------------------------------
# 取数
# --------------------------------------------------------------------------
def _ssl_ctx():
    try:
        import certifi                      # 可选；没有就退回系统根证书
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def _post_once(body: bytes, timeout: float):
    """先 urllib，再 curl。返回 (原始文本, None) 或 (None, 原因)。

    curl 兜底不是多余的：部分环境（无 certifi 的干净安装）Python 默认 SSL 上下文
    没有可用根证书，urllib 必然 CERTIFICATE_VERIFY_FAILED，而 curl 用系统证书能通。
    与 neocloud_credit_lite.py 的 FRED 取数保持同一套兜底策略。
    """
    req = urllib.request.Request(
        API_URL, data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx()) as resp:
            return resp.read().decode("utf-8"), None
    except urllib.error.HTTPError as exc:
        reason = f"HTTP {exc.code}"
    except urllib.error.URLError as exc:
        reason = f"网络错误 {exc.reason}"
    except Exception as exc:                                  # 超时等
        reason = f"{type(exc).__name__}: {scrub(exc)}"

    try:
        import subprocess
        proc = subprocess.run(
            ["curl", "-sS", "--max-time", str(int(max(timeout, 1))), "-X", "POST", API_URL,
             "-H", "Content-Type: application/json", "-d", body.decode("utf-8")],
            capture_output=True, text=True,
        )
    except Exception as exc:
        return None, f"{reason}；curl 兜底也失败（{scrub(exc)}）"
    if proc.returncode == 0 and proc.stdout.strip():
        return proc.stdout, None
    detail = (proc.stderr or "").strip().splitlines()
    return None, f"{reason}；curl 兜底也失败（{detail[-1] if detail else 'exit ' + str(proc.returncode)}）"


def fetch_meta_and_ctxs(timeout: float, retries: int):
    """POST /info。失败最多重试 retries 次；全失败返回 (None, 原因)。"""
    body = json.dumps({"type": "metaAndAssetCtxs", "dex": DEX}).encode("utf-8")
    last = "未知错误"
    for attempt in range(retries + 1):
        raw, last = _post_once(body, timeout)
        if raw is not None:
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                last = "返回内容不是合法 JSON"
            else:
                if (isinstance(payload, list) and len(payload) >= 2
                        and isinstance(payload[0], dict)
                        and isinstance(payload[0].get("universe"), list)
                        and isinstance(payload[1], list)):
                    return payload, None
                last = "返回结构不符（缺 universe / assetCtxs）"
        if attempt < retries:
            time.sleep(1.5 * (attempt + 1))
    return None, last


def to_float(val):
    try:
        f = float(val)
    except (TypeError, ValueError):
        return None
    return f if f == f else None      # 排除 nan


def build_markets(payload):
    """{市场短名（去 xyz: 前缀）: {...}}。"""
    universe, ctxs = payload[0]["universe"], payload[1]
    markets = {}
    for meta, ctx in zip(universe, ctxs):
        if not isinstance(meta, dict) or not isinstance(ctx, dict):
            continue
        full = str(meta.get("name", ""))
        short = full[len(PREFIX):] if full.startswith(PREFIX) else full
        mark = to_float(ctx.get("markPx"))
        if mark is None:
            mark = to_float(ctx.get("oraclePx"))
        oi = to_float(ctx.get("openInterest")) or 0.0
        markets[short] = {
            "market": full or short,
            "mark": mark,
            "prev_day": to_float(ctx.get("prevDayPx")),
            "open_interest": oi,
            "notional_oi": (mark * oi) if mark is not None else None,
            "day_ntl_vlm": to_float(ctx.get("dayNtlVlm")) or 0.0,
            "delisted": bool(meta.get("isDelisted")),
        }
    return markets


# --------------------------------------------------------------------------
# 现货基准（--spot）
# --------------------------------------------------------------------------
def _pick(rec, keys):
    for k in keys:
        if k in rec:
            return rec[k]
    low = {str(k).lower(): v for k, v in rec.items()}
    for k in keys:
        if k.lower() in low:
            return low[k.lower()]
    return None


def _absorb(container, flat):
    if isinstance(container, list):
        for item in container:
            if not isinstance(item, dict):
                continue
            tic = _pick(item, ("ticker", "symbol", "code", "代码"))
            if isinstance(tic, str) and tic.strip():
                flat.setdefault(tic.strip().upper(), item)
    elif isinstance(container, dict):
        for key, val in container.items():
            if not isinstance(key, str):
                continue
            k = key.strip().upper()
            if isinstance(val, dict):
                if _pick(val, CLOSE_KEYS) is not None:
                    flat.setdefault(k, val)
            elif isinstance(val, (int, float)) and not isinstance(val, bool):
                # 只认大写 ticker 形状的键，避免把 count / version 之类元字段吃进来
                if TICKER_KEY_RE.match(k):
                    flat.setdefault(k, {"close": float(val)})


def load_spot(path: Path):
    """容错读取 technicals.py --json 的输出 → {TICKER: record}。"""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        err(f"警告：--spot {Path(path).name} 读取失败（{scrub(exc)}），本次只输出 perp 价与 OI 分档。")
        return {}
    flat = {}
    if isinstance(data, dict):
        for key in CONTAINER_KEYS:
            val = data.get(key)
            if isinstance(val, (dict, list)):
                _absorb(val, flat)
        _absorb(data, flat)
    elif isinstance(data, list):
        _absorb(data, flat)
    if not flat:
        err(f"警告：--spot {Path(path).name} 里没解析出任何现货收盘价，本次只输出 perp 价与 OI 分档。")
    return flat


def spot_lookup(flat, ticker):
    """按 ticker 及其等价写法查现货记录。"""
    if not flat:
        return None
    for cand in (ticker,) + SPOT_ALIASES.get(ticker, ()):
        rec = flat.get(cand.upper())
        if rec is not None:
            return rec
    # 000660.KS ↔ 000660 之类的宽松匹配
    base = ticker.split(".")[0].upper()
    return flat.get(base)


# --------------------------------------------------------------------------
# 计算
# --------------------------------------------------------------------------
def tier_of(notional_oi):
    if notional_oi is None:
        return "unknown"
    if notional_oi >= OI_MAIN:
        return "main"
    if notional_oi >= OI_THIN:
        return "thin"
    return "too_thin"


def fmt_money(val):
    if val is None:
        return "N/A"
    if val >= 1e9:
        return f"${val / 1e9:.2f}B"
    return f"${val / 1e6:.1f}M"


def fmt_px(val):
    if val is None:
        return "N/A"
    if abs(val) >= 1000:
        return f"{val:,.1f}"
    if abs(val) >= 10:
        return f"{val:.2f}"
    return f"{val:.4f}"


def thin_suffix(row):
    if row["tier"] == "thin":
        return f"（薄盘 {fmt_money(row['notional_oi'])}，仅参考）"
    return ""


def forecast_note(row):
    """跨阈值只能写成预告，绝不记为已触发。"""
    perp, spot = row.get("perp_usd_equiv"), row.get("spot_close")
    if perp is None or spot is None:
        return None
    notes = []
    for label, val in (("T1", row.get("t1_price")), ("T2", row.get("t2_price"))):
        v = to_float(val)
        if v is None or v <= 0:
            continue
        if perp <= v < spot:
            notes.append(f"若明日以此价开盘将触及 {label}@${fmt_px(v)}（预告，非已触发）")
    return "；".join(notes) if notes else None


def evaluate(ticker, currency, markets, spot_flat, fx_rates, fx_notes):
    """把一个标的算成一行结果。返回 dict（status 决定它落到哪个区块）。"""
    name = MARKET_ALIASES.get(ticker, ticker if "." not in ticker else None)
    row = {"ticker": ticker, "currency": currency, "market": None, "status": "unlisted"}

    if name is None:
        return row
    if name in FORBIDDEN_MARKETS:      # 防御：永不落到这条
        row["status"] = "forbidden"
        return row
    m = markets.get(name)
    if m is None:
        return row

    row.update({
        "market": m["market"], "mark": m["mark"], "prev_day": m["prev_day"],
        "notional_oi": m["notional_oi"], "day_ntl_vlm": m["day_ntl_vlm"],
        "tier": tier_of(m["notional_oi"]),
    })
    if m["delisted"] or m["mark"] is None:
        row["status"] = "delisted"
        return row
    if row["tier"] == "too_thin":
        row["status"] = "too_thin"     # 过薄：报价不可信，直接跳过
        return row

    # 现货基准
    rec = spot_lookup(spot_flat, ticker)
    close = to_float(_pick(rec, CLOSE_KEYS)) if isinstance(rec, dict) else None
    if isinstance(rec, dict):
        row["spot_date"] = _pick(rec, DATE_KEYS)
        row["t1_price"] = to_float(_pick(rec, T1_KEYS))
        row["t2_price"] = to_float(_pick(rec, T2_KEYS))
    if close is None or close <= 0:
        row["status"] = "no_spot"      # 只报 perp 价与 OI 分档
        return row
    row["spot_close"] = close

    # 非美元计价 → 用同池汇率折成 USD 后再做 1:1 核对
    spot_usd = close
    if currency and currency != "USD":
        fx = fx_rates.get(currency)
        if fx is None:
            row["status"] = "no_fx"
            row["fx_currency"] = currency
            return row
        spot_usd = close / fx
        row["fx_currency"] = currency
        row["fx_rate"] = fx
        if fx_notes.get(currency):
            row["fx_note"] = fx_notes[currency]

    row["spot_usd_equiv"] = spot_usd
    row["perp_usd_equiv"] = m["mark"]
    ratio = m["mark"] / spot_usd
    row["ratio"] = ratio
    # 1:1 核对：落在 0.9–1.1 之外即判定取错市场，不输出一个错误的隐含变动
    if not (RATIO_LO <= ratio <= RATIO_HI):
        row["status"] = "ratio_fail"
        return row

    row["implied_pct"] = (ratio - 1.0) * 100.0
    row["major"] = abs(row["implied_pct"]) >= MAJOR_MOVE
    row["forecast"] = forecast_note(row)
    row["status"] = "ok"
    return row


# --------------------------------------------------------------------------
# 渲染
# --------------------------------------------------------------------------
TIER_CN = {"main": "主用", "thin": "薄盘·仅参考", "too_thin": "过薄", "unknown": "OI未知"}

FOOTER = [
    "",
    "— 硬约束（本节为纯观察节点）—",
    "· 本节绝不产生 T1/T2/T3 触发、绝不改变分桶结论；T1/T2/T3 永远只用最近一个完整交易日收盘价计算。",
    "· 跨阈值只能写成预告：「若明日以此价开盘将触及 T2@$XXX（预告，非已触发）」，不得计入「✅ 未触发」清单的变更。",
    "· 全程标注为「隐含」，绝不写成现货收盘价——这是另一个标的（永续合约）的真实成交价。",
    f"· 只输出 |隐含变动| ≥ {MIN_ABS_MOVE:.0f}%；≥ {MAJOR_MOVE:.0f}% 为重大异动，须 WebSearch 查起因并在风险提示单列。",
    "· 带 ⚠️EARN 且当日盘后已发布财报的标的，其隐含变动即为财报反应，须点名注明「财报已出，盘后 ±X.X%」。",
]


def render(result):
    out = []
    A = out.append
    A("== 第二步之二：盘后 / 休市期间隐含变动（24/7 永续 · Hyperliquid xyz 池）==")
    A(f"取数时间：{result['fetched_at']}（UTC） · 池：{DEX} · 市场数：{result['market_count']}"
      f"（在架 {result['market_listed']}） · 标的清单：{result['universe_path']}")
    if result.get("spot_path"):
        A(f"现货基准：{result['spot_path']}"
          + (f" · 数据日期 {result['spot_date_hint']}" if result.get("spot_date_hint") else ""))
    elif result.get("spot_given"):
        A(f"现货基准：--spot {result.get('spot_arg_name') or ''} 读取失败（文件读不出、"
          "不是合法 JSON、或里面没有任何现货收盘价）"
          " → 退化为只输出 perp 价与 OI 分档，不计算隐含变动。")
    else:
        A("现货基准：未提供 --spot → 只输出 perp 价与 OI 分档，不计算隐含变动。")
    for note in result.get("fx_lines", []):
        A(note)

    # —— 大盘层 ——
    A("")
    A("【大盘层】")
    idx = result["indices"]
    if not idx:
        A("· 未取到 SP500 / XYZ100 报价。")
    for row in idx:
        line = (f"· {row['market']:<11} {fmt_px(row['mark']):>10}"
                f" · OI {fmt_money(row['notional_oi'])} {TIER_CN[row['tier']]}")
        if row.get("implied_pct") is not None:
            line += (f" · vs {row['ticker']} {fmt_px(row['spot_close'])}"
                     + (f"（{row['spot_date']}）" if row.get("spot_date") else "")
                     + f" → 隐含 {row['implied_pct']:+.2f}%")
        elif row["status"] == "ratio_fail":
            line += f" · 疑似取错市场（perp/现货 = {row['ratio']:.3f}，超出 {RATIO_LO}–{RATIO_HI}），跳过"
        else:
            line += " · 无现货基准，仅报 perp 价"
        A(line + f"  [{row['name_cn']}]")
    rs = result.get("tech_rel_strength")
    if rs is not None:
        A(f"· 科技相对强弱 = XYZ100 隐含变动 − SP500 隐含变动 = {rs:+.2f}pp"
          f"（{'正值 = 科技领涨大盘' if rs >= 0 else '负值 = 科技弱于大盘'}）→ 写入「📊 大盘与板块背景」")
    else:
        A("· 科技相对强弱：两腿隐含变动未同时取到，本次不计算。")

    # —— 个股 ——
    ok = [r for r in result["stocks"] if r["status"] == "ok"]
    moved = [r for r in ok if abs(r["implied_pct"]) >= MIN_ABS_MOVE]
    moved.sort(key=lambda r: -abs(r["implied_pct"]))
    A("")
    A(f"【个股 · |隐含变动| ≥ {MIN_ABS_MOVE:.0f}%】（1:1 核对通过 {len(ok)} 只，达标 {len(moved)} 只）")
    if not result.get("spot_path"):
        A("· " + ("--spot 读取失败，退化为只输出 perp 价与 OI 分档，本区块留空。"
                  if result.get("spot_given") else "未提供 --spot，本区块留空。")
          + "perp 价与 OI 分档见下方【全部在架标的】。")
    elif not moved:
        A(f"· 本次无标的隐含变动达 ±{MIN_ABS_MOVE:.0f}%。")
    for r in moved:
        flag = "🚨重大异动" if r["major"] else "　"
        line = (f"· {flag} {r['ticker']:<10} perp {fmt_px(r['mark'])} vs 现货 {fmt_px(r['spot_close'])}"
                + (f"（{r['spot_date']}）" if r.get("spot_date") else "")
                + f" → 隐含 {r['implied_pct']:+.2f}%"
                + f" · OI {fmt_money(r['notional_oi'])}{thin_suffix(r)}")
        A(line)
        if r.get("fx_rate"):
            A(f"    汇率：1 USD = {fmt_px(r['fx_rate'])} {r['fx_currency']}（同池 {PREFIX}{FX_MARKETS[r['fx_currency']]}）"
              + (f" · {r['fx_note']}" if r.get("fx_note") else ""))
        if r["major"]:
            A("    ⚠️ ≥5%：须 WebSearch 查起因，并在风险提示明细单列（若为盘后财报，注明「财报已出，盘后 ±X.X%」）。")
        if r.get("forecast"):
            A(f"    {r['forecast']}")

    # —— 跨阈值预告 ——
    # 硬约束 2：只能写成预告，绝不记为已触发。与 2% 门槛无关——门槛管的是「哪些标的进
    # 隐含变动清单」，预告管的是「跨阈值怎么写」，一只 −0.5% 的股票同样可能压在阈值上。
    fc = [r for r in ok if r.get("forecast")]
    if fc:
        A("")
        A("【跨阈值预告 · 非已触发】")
        for r in fc:
            A(f"· {r['ticker']:<10} {r['forecast']}{thin_suffix(r)}")
        A("  ↑ 这些**不是**触发。T1/T2/T3 仍只用最近一个完整交易日收盘价判定，分桶不变。")

    # —— 全部在架标的（含未达 2% 的，供交叉核对）——
    listed = [r for r in result["stocks"] if r["status"] in ("ok", "no_spot")]
    A("")
    A(f"【全部在架标的 · OI 现场分档】（{len(listed)} 只）")
    for tier, label in (("main", "主用（OI ≥ $10M，读数可信）"),
                        ("thin", "薄盘·仅参考（$3–10M，须标注）")):
        group = [r for r in listed if r["tier"] == tier]
        if not group:
            continue
        A(f"· {label}：")
        for r in group:
            imp = (f"隐含 {r['implied_pct']:+.2f}%" if r.get("implied_pct") is not None
                   else "无现货基准")
            A(f"    {r['ticker']:<10} {r['market']:<12} mark {fmt_px(r['mark']):>10}"
              f"  OI {fmt_money(r['notional_oi']):>8}  {imp}")

    # —— 跳过 ——
    A("")
    A("【跳过】")
    groups = [
        ("too_thin", f"过薄·不输出（OI < {fmt_money(OI_THIN)}，池子迁移或停摆，报价不可信）"),
        ("ratio_fail", f"疑似取错市场（perp/现货 比值超出 {RATIO_LO}–{RATIO_HI}）"),
        ("delisted", "已下架 / 无报价"),
        ("no_fx", "缺同池汇率，无法折算"),
        ("unlisted", "xyz 池未上架（不要硬找替代）"),
        ("forbidden", "命中禁用市场"),
    ]
    for status, label in groups:
        group = [r for r in result["stocks"] if r["status"] == status]
        if not group:
            continue
        if status == "ratio_fail":
            A(f"· {label}：")
            for r in group:
                A(f"    {r['ticker']:<10} {r['market']:<12} perp {fmt_px(r['mark'])}"
                  f" / 现货折USD {fmt_px(r.get('spot_usd_equiv'))} = {r['ratio']:.3f} → 不输出隐含变动")
            A("    （提示：真实的隔夜 >10% 跳空也会落在这里。先 WebSearch 核实是财报/事件还是取错市场，"
              "核实前一律按取错市场处理，不得输出该标的的隐含变动。）")
        elif status == "too_thin":
            A(f"· {label}：" + "、".join(f"{r['ticker']}({fmt_money(r['notional_oi'])})" for r in group))
        else:
            A(f"· {label}：" + "、".join(r["ticker"] for r in group))

    out.extend(FOOTER)
    return "\n".join(out)


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(
        description="24/7 永续隐含变动（Hyperliquid xyz 池）· 纯观察节点，绝不改变 T1/T2/T3 与分桶",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--json", metavar="OUT", help="把结构化结果写到该文件")
    ap.add_argument("--spot", metavar="PATH",
                    help="technicals.py --json 的输出，提供各标的最近完整交易日收盘价；不给则只输出 perp 价与 OI 分档")
    ap.add_argument("--timeout", type=float, default=20.0, help="单次请求超时秒数（默认 20）")
    args = ap.parse_args()

    universe, universe_path = load_universe()

    payload, reason = fetch_meta_and_ctxs(args.timeout, retries=2)   # 最多重试 2 次
    if payload is None:
        # 硬约束 6：整节写这句，不影响其余所有部分
        print("== 第二步之二：盘后 / 休市期间隐含变动（24/7 永续）==")
        print(f"本次未取到 24/7 永续数据（{reason}；已重试 2 次）。")
        print("按规则：本节留空，不影响其余所有部分；不得因此改变任何 T1/T2/T3 判定或分桶。")
        sys.exit(0)

    markets = build_markets(payload)

    spot_flat, spot_path = {}, None
    # spot_given / spot_arg_name 与 spot_path 是三件不同的事：
    #   spot_given    = 用户给没给 --spot
    #   spot_arg_name = 给的那个文件名（只回显文件名，不带绝对路径）
    #   spot_path     = 真的读出现货价了（None = 本次没有现货基准）
    # 早先只有 spot_path，报告头据此打「未提供 --spot」——于是「给了但读取失败」
    # 会被如实性反了地写成「没给」，读者以为是自己漏了参数，实际是 tech.json 坏了/空了。
    spot_given = bool(args.spot)
    spot_arg_name = Path(args.spot).name if args.spot else None
    if args.spot:
        spot_flat = load_spot(Path(args.spot))
        if spot_flat:
            spot_path = spot_arg_name

    # 汇率（同池）
    fx_rates, fx_notes, fx_lines = {}, {}, []
    for cur in {c for _, c in universe if c and c != "USD"}:
        mname = FX_MARKETS.get(cur)
        m = markets.get(mname) if mname else None
        if m and m["mark"]:
            fx_rates[cur] = m["mark"]
            if m["delisted"] or m["notional_oi"] in (0, None) or m["day_ntl_vlm"] == 0:
                fx_notes[cur] = f"{PREFIX}{mname} 已下架/无持仓，仅 oracle 报价，折算结果仅参考"
            fx_lines.append(f"汇率：1 USD = {fmt_px(m['mark'])} {cur}（同池 {PREFIX}{mname}）"
                            + (f" · {fx_notes[cur]}" if cur in fx_notes else ""))
        elif mname:
            fx_lines.append(f"汇率：同池 {PREFIX}{mname} 未取到 → {cur} 计价标的本次跳过。")
        else:
            fx_lines.append(f"汇率：{cur} 计价标的在 {DEX} 池无对应汇率市场 → 本次跳过。")

    # 个股
    stocks = [evaluate(t, c, markets, spot_flat, fx_rates, fx_notes) for t, c in universe]

    # 大盘层
    indices = []
    for mname, spot_ticker, name_cn in INDEX_PAIRS:
        m = markets.get(mname)
        if m is None:
            continue
        rec = spot_lookup(spot_flat, spot_ticker)
        close = to_float(_pick(rec, CLOSE_KEYS)) if isinstance(rec, dict) else None
        row = {
            "ticker": spot_ticker, "name_cn": name_cn, "market": m["market"], "mark": m["mark"],
            "prev_day": m["prev_day"], "notional_oi": m["notional_oi"],
            "tier": tier_of(m["notional_oi"]), "status": "no_spot",
            "spot_close": close,
            "spot_date": _pick(rec, DATE_KEYS) if isinstance(rec, dict) else None,
        }
        if close and close > 0 and m["mark"]:
            ratio = m["mark"] / close
            row["ratio"] = ratio
            if RATIO_LO <= ratio <= RATIO_HI:
                row["implied_pct"] = (ratio - 1.0) * 100.0
                row["status"] = "ok"
            else:
                row["status"] = "ratio_fail"
        indices.append(row)

    by_market = {r["market"]: r for r in indices}
    sp = by_market.get(f"{PREFIX}SP500", {}).get("implied_pct")
    nx = by_market.get(f"{PREFIX}XYZ100", {}).get("implied_pct")
    tech_rs = (nx - sp) if (sp is not None and nx is not None) else None

    spot_dates = [r.get("spot_date") for r in stocks if r.get("spot_date")]
    result = {
        "section": "第二步之二·24/7 永续隐含变动",
        "observation_only": True,
        "note": "纯观察节点：绝不产生 T1/T2/T3 触发、绝不改变分桶；跨阈值只能写成预告。",
        "source": {"url": API_URL, "type": "metaAndAssetCtxs", "dex": DEX},
        "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "market_count": len(markets),
        "market_listed": sum(1 for m in markets.values() if not m["delisted"]),
        "universe_path": universe_path,
        "universe_count": len(universe),
        "spot_path": spot_path,
        "spot_given": spot_given,
        "spot_arg_name": spot_arg_name,
        "spot_date_hint": max(set(spot_dates), key=spot_dates.count) if spot_dates else None,
        "thresholds": {"oi_main": OI_MAIN, "oi_thin": OI_THIN, "min_abs_move_pct": MIN_ABS_MOVE,
                       "major_move_pct": MAJOR_MOVE, "ratio_band": [RATIO_LO, RATIO_HI]},
        "fx": {"rates": fx_rates, "notes": fx_notes},
        "fx_lines": fx_lines,
        "indices": indices,
        "tech_rel_strength": tech_rs,
        "stocks": stocks,
    }

    print(render(result))

    if args.json:
        out = Path(args.json)
        try:
            if out.parent and str(out.parent) not in ("", "."):
                out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as exc:
            err(f"警告：写入 {out.name} 失败：{scrub(exc)}")
        else:
            print(f"\n已写入 {rel_display(out)}")
    return 0


if __name__ == "__main__":
    # 顶层兜底：裸 traceback 会把 ~/... 的完整绝对路径吐进 stderr，而脚本输出会被
    # 贴进日报正文并推 Slack。任何未预期异常一律折叠成一行中文错误。
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - 顶层兜底，刻意兜住一切
        print(f"✗ {SCRIPT_NAME} 执行失败：{type(exc).__name__}: {scrub(exc)}",
              file=sys.stderr)
        sys.exit(1)
