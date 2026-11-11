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
美股盘前运行（tech.json 的 session.US.premarket_active 为真）时，美股档的预告改写为
    「若今日以此价开盘将触及 T2@$XXX（预告，非已触发）」
今日盘前成交价（technicals.py 的 live_kind == "premarket" 叠加层）只**分列**展示：
它与永续是两个不同标的，绝不并入隐含变动列、绝不套 |≥2%|/|≥5%| 阈值、
绝不写进任何以「收盘」为名的字段（spot_close / implied_pct 的分母仍是最近完整交易日收盘）。
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
    python3 scripts/perp_quotes.py --json out.json --quiet   # 只落盘，stdout 不打人类正文

--quiet 只关 stdout 的人类可读正文（并发调度下多个单元同时跑，正文会交织在一起），
**不是**关警告：stderr 照常喊，降级照常写进 JSON 的 degraded / degraded_reasons。

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
from zoneinfo import ZoneInfo

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
#
# ⚠️ **这两个门槛不随盘口状态重新标定，这是刻意的。** 名义 OI = markPx × openInterest
# 是一个**存量**（未平仓合约的总额），不是流量：它不在一天之内累积、也不在收盘时结算。
# 一档 OI $12M 的永续，上午十点和傍晚六点同样值得信。相较之下 |≥2%|/|≥5%| 的隐含变动
# 阈值标定在「收盘→下次开盘」那个**窗口**上，所以盘中必须记 N/A（已在 evaluate 里处理）——
# 两者性质不同，别把後者的处理方式套到这里。
# 美股盘中反而是 OI 读数**最**可信的时段：现货在交易，与永续之间的套利是活的。
OI_MAIN = 10_000_000.0      # 主用：读数可信
OI_THIN = 3_000_000.0       # 薄盘：$3–10M，须标注「（薄盘 $X.XM，仅参考）」
                            # < $3M 过薄：直接跳过，不输出

MIN_ABS_MOVE = 2.0          # 只输出 |隐含变动| ≥ 2%
MAJOR_MOVE = 5.0            # ≥ 5% 视为重大异动，须 WebSearch 查起因
RATIO_LO, RATIO_HI = 0.9, 1.1   # 1:1 核对区间，落区间外 → 判定取错市场并跳过

# 取数失败与正常两条路径都要吐同一份阈值/同一句硬约束，写成常量免得两边各说各话。
THRESHOLDS = {"oi_main": OI_MAIN, "oi_thin": OI_THIN, "min_abs_move_pct": MIN_ABS_MOVE,
              "major_move_pct": MAJOR_MOVE, "ratio_band": [RATIO_LO, RATIO_HI]}
OBSERVATION_NOTE = "纯观察节点：绝不产生 T1/T2/T3 触发、绝不改变分桶；跨阈值只能写成预告。"

# 未提供 --spot 会把整个「🌙 盘后隐含」掏空（隐含变动、科技相对强弱、跨阈值预告全没了），
# 是本脚本最大的一档降级，必须显式喊出来而不是让读者从空区块自己猜。
NO_SPOT_REASON = ("未提供 --spot：无现货基准，本节退化为只输出 perp 价与 OI 分档，"
                  "隐含变动 / 科技相对强弱 / 跨阈值预告全部 N/A")

# —— 盘口模式 ——
# 只从 tech.json 读（session.US.state / session.US.premarket_active），**绝不自己看时钟**：
# 盘前模式要的是 technicals.py 真的取到了今日盘前成交这一证据，而不是「现在几点」。
# 只看时钟会在节假日早上、周末印出「若今日以此价开盘」。
# 反过来的代价：盘前窗口内 premarket_active 不为真（1m 取数失败 / --no-live-overlay）时
# 落回 overnight，美股档写「明日」——若今日是交易日这就错了。不改判定（无节假日表），只喊出来。
MODE_NOTE_PM_WINDOW = "美股盘前时段但无盘前叠加层；US 档「明日」措辞可能应为「今日」（若今日为交易日）"

# 盘前成交价（technicals.py 的盘前叠加层）——与永续分列，口径说明写成常量，正文与 JSON 共用。
PREMARKET_THRESHOLD_REASON = (
    "盘前成交价是另一个标的（现货盘前撮合，成交稀薄、Yahoo 1m 量报 0），且非收盘："
    "|≥2%|/|≥5%| 标定在「收盘→下次开盘」的永续隐含变动上，不随标的迁移（回退链规则第 4 条），"
    "盘前涨跌与永续−盘前基差一律不套阈值")
PREMARKET_NOT_RUN_REASON = "本次非美股盘前运行（mode ≠ premarket），无盘前叠加层"
PREMARKET_NON_US_REASON = "非美股：盘前叠加层仅限美股"
PREMARKET_INDEX_REASON = "指数无盘前成交"
PREMARKET_INDEX_NO_SUBSTITUTE = "不以 SPY/QQQ/期货替代"
PREMARKET_INDEX_RULE = f"{PREMARKET_INDEX_REASON} → N/A（{PREMARKET_INDEX_NO_SUBSTITUTE}）"
PREMARKET_NO_REC_REASON = "--spot 里没有该标的的记录"
PREMARKET_NO_PRINT_REASON = "tech.json 未给出该档今日盘前成交（live_kind ≠ premarket 或 live_price 为空）"
PREMARKET_INSUFFICIENT_HISTORY_REASON = "历史不足，technicals 未附盘前叠加层"
# technicals 对日线取数失败的档同时置 source_unavailable 与 insufficient_history，并明说
# 「是取数失败，不是上市太新」——这里先判 source_unavailable，不把两者重新混成「历史不足」。
PREMARKET_SOURCE_UNAVAILABLE_REASON = "technicals 日线取数失败（不是历史不足），未附盘前叠加层"
PREMARKET_NO_BASE_REASON = "无最近完整交易日收盘可作分母，盘前涨跌 N/A"
PREMARKET_REFERENCE_NOTE = "盘前成交价仅供参考：照常列出并分析，但不判定触发、不改分桶"
PREMARKET_PCT_BASIS_NOTE = "分母同上：最近完整交易日收盘；盘前价不是收盘价，不判定触发、不改分桶。"

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
    """容错读取 technicals.py --json 的输出 → ({TICKER: record}, session)。

    session 取自同一份 tech.json，**不另开旗标、也不自己判时钟**：现货基准与盘口
    状态必须来自同一次取数，否则会出现「基准是 D-1 收盘、却按盘后口径渲染」这种
    两个来源各说各话的组合。
    """
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        err(f"警告：--spot {Path(path).name} 读取失败（{scrub(exc)}），本次只输出 perp 价与 OI 分档。")
        return {}, None
    session = None
    if isinstance(data, dict):
        s = (data.get("session") or {}).get("US")
        if isinstance(s, dict):
            session = s
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
    return flat, session


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


# 每个 status 的中文说明：正文【跳过】区块与 JSON 的 status_reason 共用这一份，
# 免得同一件事在正文里叫「过薄·不输出」、在 JSON 里叫别的名字。
STATUS_REASON_CN = {
    "ok": None,
    "no_spot": "无现货基准，仅报 perp 价",
    "too_thin": f"过薄·不输出（OI < {fmt_money(OI_THIN)}，池子迁移或停摆，报价不可信）",
    "ratio_fail": f"疑似取错市场（perp/现货 比值超出 {RATIO_LO}–{RATIO_HI}）",
    "delisted": "已下架 / 无报价",
    "no_fx": "缺同池汇率，无法折算",
    "unlisted": "xyz 池未上架（不要硬找替代）",
    "forbidden": "命中禁用市场",
}


def finalize_row(row, *, triggers: bool = True):
    """把「没算出来」显式写成 null + 一句中文原因，而不是让这些键干脆消失。

    约定：算不出来的判定写 null，False 只表示「算过、没触发」——这就是 ⚪️ / ❌
    在 JSON 里的形态。键缺失时下游 .get() 也拿到 None，但缺失读不出「为什么」，
    所以一律补上 status_reason。
    """
    row.setdefault("implied_pct", None)
    if triggers:
        row.setdefault("major", None)     # None = 隐含变动没算出来；False 才是「算了，没到 5%」
        row.setdefault("forecast", None)  # ok 行的 None = 没跨阈值；其余行的 None = 没算，看 status_reason
    row["status_reason"] = STATUS_REASON_CN.get(row.get("status"))
    return row


def is_us_row(ticker, currency, rec=None):
    """美股档判定。优先用 tech.json 记录里的 market 字段，缺了再按代码后缀 / 计价币种推断。"""
    mk = rec.get("market") if isinstance(rec, dict) else None
    if isinstance(mk, str) and mk.strip():
        return mk.strip().upper() == "US"
    return "." not in ticker and (currency or "USD") == "USD"


def forecast_note(row, intraday=False, premarket=False):
    """跨阈值只能写成预告，绝不记为已触发。

    时态要跟着盘口走：美股仍在交易时说「若**明日开盘**」是错的——下一个定价事件
    是**今天的收盘**，不是明天的开盘。美股盘前同理：下一个定价事件是**今天的开盘**。
    盘前时态只给美股档：04:00–09:30 ET 韩股/港股当日早已收盘，它们的下一次开盘仍是「明日」。
    """
    perp, spot = row.get("perp_usd_equiv"), row.get("spot_close")
    if perp is None or spot is None:
        return None
    # T1/T2 与 spot_close 都是本币；perp 是 USD。非美元档必须用 evaluate 里 1:1 核对那同一个
    # 同池汇率折成本币再比（ratio = mark / (close / fx) ⇔ mark × fx 对 close），否则 KRW 档的
    # T1/T2 永远「低于」一个几百美元的 perp，预告凭空成立。缺汇率就不预告（原因由 evaluate 写）。
    cur = row.get("currency") or "USD"
    if cur != "USD":
        fx = to_float(row.get("fx_rate"))
        if fx is None or fx <= 0:
            return None
        perp = perp * fx
    us = is_us_row(row.get("ticker", ""), row.get("currency"))
    # 美股盘中（约 09:30–16:00 ET）时韩股/港股当日早已收盘：它们的下一次定价仍是「明日开盘」。
    if intraday and us:
        when = "今日以此价收盘"
    elif premarket and us:
        when = "今日以此价开盘"
    else:
        when = "明日以此价开盘"
    notes = []
    for label, val in (("T1", row.get("t1_price")), ("T2", row.get("t2_price"))):
        v = to_float(val)
        if v is None or v <= 0:
            continue
        if perp <= v < spot:
            px = f"${fmt_px(v)}" if cur == "USD" else f"{fmt_px(v)} {cur}"
            notes.append(f"若{when}将触及 {label}@{px}（预告，非已触发）")
    return "；".join(notes) if notes else None


def attach_premarket(row, rec, mode, *, index=False):
    """把 tech.json 里的今日盘前成交**分列**挂到这一行上（纯附加字段）。

    分母与隐含变动完全相同（最近完整交易日收盘 spot_close）；盘前价绝不写进
    spot_close / implied_pct 或任何以「收盘」为名的字段，也不改 status。
    盘前涨跌与永续−盘前基差不套任何阈值——见 PREMARKET_THRESHOLD_REASON。
    取不到时写 null + premarket_unavailable_reason，而不是让键消失。
    """
    for k in ("premarket_px", "premarket_ts", "premarket_pct", "premarket_pct_basis",
              "premarket_base_close", "perp_premarket_basis_pct",
              "perp_premarket_basis_reason", "premarket_threshold_comparable",
              "premarket_threshold_reason", "premarket_unavailable_reason",
              "premarket_pct_reason"):
        row[k] = None

    if index:
        reason = PREMARKET_INDEX_REASON
    elif mode != "premarket":
        reason = PREMARKET_NOT_RUN_REASON
    elif not is_us_row(row["ticker"], row.get("currency"), rec):
        reason = PREMARKET_NON_US_REASON
    elif not isinstance(rec, dict):
        reason = PREMARKET_NO_REC_REASON
    else:
        px = to_float(rec.get("live_price")) if rec.get("live_kind") == "premarket" else None
        if px is None or px <= 0:
            # 原因照搬 tech.json 那一行自己的说法（跨档拒绝 / 尚无成交 / 指数期货），
            # 不在这里统一改写成「无今日盘前成交」——历史不足的档 technicals 根本不附叠加层。
            why = rec.get("live_unavailable_reason")
            if isinstance(why, str) and why.strip():
                reason = why
            elif rec.get("source_unavailable"):
                reason = PREMARKET_SOURCE_UNAVAILABLE_REASON
            elif rec.get("insufficient_history"):
                reason = PREMARKET_INSUFFICIENT_HISTORY_REASON
            else:
                reason = PREMARKET_NO_PRINT_REASON
        else:
            reason = None
    if reason is not None:
        row["premarket_unavailable_reason"] = reason
        return row

    row["premarket_px"] = px
    row["premarket_ts"] = rec.get("live_ts")
    base = to_float(row.get("spot_close"))
    if base is None:
        base = to_float(_pick(rec, CLOSE_KEYS))
    has_base = bool(base and base > 0)
    row["premarket_pct"] = (px / base - 1.0) * 100.0 if has_base else None
    # 正文「分母同上：最近完整交易日收盘」的字段——只在真算出了涨跌时才指向它
    row["premarket_pct_basis"] = "spot_close" if has_base else None
    row["premarket_pct_reason"] = None if has_base else PREMARKET_NO_BASE_REASON
    # 过薄 / 下架行的 spot_close 是 null，分母取自 tech.json 同一收盘：把数写出来，basis 才不指向空字段
    row["premarket_base_close"] = base if base and base > 0 else None
    # 基差只对「1:1 核对通过」的永续行算：过薄 / 下架 / 取错市场的报价不拿来对照。
    mark = row.get("mark")
    if row.get("status") == "ok" and mark is not None:
        row["perp_premarket_basis_pct"] = (mark / px - 1.0) * 100.0
    else:
        row["perp_premarket_basis_reason"] = (
            "xyz 池未上架该标的" if row.get("market") is None
            else f"永续行未通过（status={row.get('status')}），不对照基差")
    row["premarket_threshold_comparable"] = False
    row["premarket_threshold_reason"] = PREMARKET_THRESHOLD_REASON
    return row


def fmt_et_hhmm(ts):
    """ISO 时间戳 → 'HH:MM ET'；解析不了就原样回显（不猜）。

    带时区的先折成美东再印（+00:00 / Z 不能直接贴 ET 标签）；不带时区的按原样印、标签不变。
    """
    if not ts:
        return "时间未知"
    try:
        dt_ = datetime.fromisoformat(str(ts))
        if dt_.tzinfo is not None:
            dt_ = dt_.astimezone(ZoneInfo("America/New_York"))
        return dt_.strftime("%H:%M") + " ET"
    except Exception:       # ValueError，或本机缺 tz 数据（ZoneInfoNotFoundError）
        return str(ts)


def premarket_text(r):
    """一行盘前分列文字；只读 JSON 里已有的字段（正文 == JSON）。"""
    pct = r.get("premarket_pct")
    s = (f"盘前成交 ${fmt_px(r['premarket_px'])}"
         f"（{(f'{pct:+.2f}%' if pct is not None else '涨跌 N/A：' + (r.get('premarket_pct_reason') or '无分母'))}，"
         f"{fmt_et_hhmm(r.get('premarket_ts'))}）")
    basis = r.get("perp_premarket_basis_pct")
    s += (f"｜永续−盘前基差 {basis:+.2f}%" if basis is not None
          else f"｜永续−盘前基差 N/A（{r.get('perp_premarket_basis_reason') or '无'}）")
    return s


def evaluate(ticker, currency, markets, spot_flat, fx_rates, fx_notes, intraday=False,
             premarket=False):
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
    # |≥2%| 印出、|≥5%| 强制 WebSearch 查因 —— 这两个阈值标定在**现货收盘到下次开盘**
    # 那段隔夜窗上（perp-overnight.md:21）。美股仍在交易时，分母是 D-1 收盘而分子含
    # 了今天已经走完的大半个交易日，窗口根本不是同一个：阈值不随窗口转移
    # （回退链规则第 4 条），所以盘中记 null + 理由，不拿隔夜阈值去套。
    row["threshold_comparable"] = not intraday
    if intraday:
        row["major"] = None
        row["major_reason"] = ("盘中：隐含变动的窗口是「D-1收盘→此刻」，"
                               "而 |≥2%|/|≥5%| 标定在隔夜窗上，阈值不可跨窗比较")
    else:
        row["major"] = abs(row["implied_pct"]) >= MAJOR_MOVE
    row["forecast"] = forecast_note(row, intraday=intraday, premarket=premarket)
    if currency and currency != "USD" and not to_float(row.get("fx_rate")):
        row["forecast_reason"] = "非美元计价且缺同池汇率：无法把永续价折成本币与 T1/T2 比较，预告 N/A"
    row["status"] = "ok"
    return row


# --------------------------------------------------------------------------
# 渲染
# --------------------------------------------------------------------------
TIER_CN = {"main": "主用", "thin": "薄盘·仅参考", "too_thin": "过薄", "unknown": "OI未知"}

MAJOR_ACTION = ("⚠️ ≥5%：须 WebSearch 查起因，并在风险提示明细单列"
                "（若为盘后财报，注明「财报已出，盘后 ±X.X%」）。")
FORECAST_NOT_TRIGGER = "这些**不是**触发。T1/T2/T3 仍只用最近一个完整交易日收盘价判定，分桶不变。"
RATIO_FAIL_HINT = ("真实的隔夜 >10% 跳空也会落在这里。先 WebSearch 核实是财报/事件还是取错市场，"
                   "核实前一律按取错市场处理，不得输出该标的的隐含变动。")

FOOTER = [
    "",
    "— 硬约束（本节为纯观察节点）—",
    "· 本节绝不产生 T1/T2/T3 触发、绝不改变分桶结论；T1/T2/T3 永远只用最近一个完整交易日收盘价计算。",
    "· 跨阈值只能写成预告：「若明日以此价开盘将触及 T2@$XXX（预告，非已触发）」，不得计入「✅ 未触发」清单的变更。",
    "· 全程标注为「隐含」，绝不写成现货收盘价——这是另一个标的（永续合约）的真实成交价。",
    f"· 只输出 |隐含变动| ≥ {MIN_ABS_MOVE:.0f}%；≥ {MAJOR_MOVE:.0f}% 为重大异动，须 WebSearch 查起因并在风险提示单列。",
    "· 带 ⚠️EARN 且当日盘后已发布财报的标的，其隐含变动即为财报反应，须点名注明「财报已出，盘后 ±X.X%」。",
]

# 美股盘前运行的页脚：预告时态改为「今日开盘」（仅美股档），并加上盘前成交价的分列硬约束。
# overnight / intraday 两种模式沿用上面的 FOOTER，一字不改。
FOOTER_PREMARKET = [
    "",
    "— 硬约束（本节为纯观察节点）—",
    "· 本节绝不产生 T1/T2/T3 触发、绝不改变分桶结论；T1/T2/T3 永远只用最近一个完整交易日收盘价计算。",
    "· 跨阈值只能写成预告：美股档「若今日以此价开盘将触及 T2@$XXX（预告，非已触发）」"
    "（韩股/港股档仍为「若明日以此价开盘」），不得计入「✅ 未触发」清单的变更。",
    "· 全程标注为「隐含」，绝不写成现货收盘价——这是另一个标的（永续合约）的真实成交价。",
    f"· 只输出 |隐含变动| ≥ {MIN_ABS_MOVE:.0f}%；≥ {MAJOR_MOVE:.0f}% 为重大异动，须 WebSearch 查起因并在风险提示单列"
    "（盘前窗口仍是「最近完整交易日收盘→此刻」，阈值照常有效）。",
    "· 带 ⚠️EARN 且当日盘后已发布财报的标的，其隐含变动即为财报反应，须点名注明「财报已出，盘后 ±X.X%」。",
    "· 今日盘前成交价与永续是两个不同标的：分列展示，绝不并入隐含变动列、绝不合成一个「隐含」数字；"
    "盘前涨跌与永续−盘前基差不套 |≥2%|/|≥5%| 阈值。",
    "· 盘前价不得写入任何以「收盘」为名的字段、不得据此判定新触发或改变分桶；须今日开盘后以收盘确认。",
]


def footer_lines(mode):
    return FOOTER_PREMARKET if mode == "premarket" else FOOTER


def section_title(mode):
    """节标题随模式走；失败路径与正常路径共用，免得两边各说各话。"""
    if mode == "intraday":
        return "⏳ 盘中 vs 前收 隐含变动"
    if mode == "premarket":
        return "🌙 盘前隐含变动（24/7 永续 vs 最近完整交易日收盘）"
    return "盘后 / 休市期间隐含变动"


def derive_mode(session):
    """只读 tech.json 的 session.US，绝不看时钟。返回 (intraday, premarket, mode)。"""
    s = session if isinstance(session, dict) else {}
    intraday = bool(s.get("state") == "intraday")
    # `is True`：字符串 "false" 之类的脏值不能被 bool() 读成真。
    premarket = s.get("premarket_active") is True and not intraday
    mode = "intraday" if intraday else ("premarket" if premarket else "overnight")
    return intraday, premarket, mode


def derive_mode_note(session, mode):
    """盘前窗口内却落到 overnight（premarket_active 为 False / None）→ 返回提示句，否则 None。"""
    s = session if isinstance(session, dict) else {}
    if mode == "overnight" and s.get("premarket_window") is True:
        return MODE_NOTE_PM_WINDOW
    return None


def prohibitions_block(mode="overnight"):
    """正文里全部禁止性文字的结构化副本。

    --quiet 下正文根本不打，JSON 就是这次运行唯一的载体；硬约束不能只活在
    人类可读分支里，否则并发调度跑出来的那份结果读不到「这不是触发」。
    措辞一律引用上面的常量，不另起炉灶。
    """
    block = {
        "observation_only": OBSERVATION_NOTE,
        "footer": [ln for ln in footer_lines(mode) if ln.startswith("·")],
        "major_move": MAJOR_ACTION,
        "forecast_not_triggered": FORECAST_NOT_TRIGGER,
        "ratio_fail_handling": RATIO_FAIL_HINT,
    }
    if mode == "premarket":
        # 【盘前成交】区块里的两句禁止性文字（指数不拿 SPY/QQQ/期货顶替；分母口径）。
        block["premarket"] = {"index": PREMARKET_INDEX_RULE, "pct_basis": PREMARKET_PCT_BASIS_NOTE,
                              "reference_note": PREMARKET_REFERENCE_NOTE}
    return block


def render(result):
    out = []
    A = out.append
    _iu = bool(result.get("intraday"))
    mode = result.get("mode") or ("intraday" if _iu else "overnight")
    _pm = mode == "premarket"
    A("== 第二步之二：" + section_title(mode)
      + ("· Hyperliquid xyz 池 ==" if _pm else "（24/7 永续 · Hyperliquid xyz 池）=="))
    if _iu:
        A("⚠ 盘中口径：分母为最近完整交易日收盘、分子含今日已走完的盘中时段——"
          "**与隔夜窗不是同一个窗口**。|≥2%| / |≥5%| 阈值标定在隔夜窗上，本次记 N/A 不套用。")
    if _pm:
        A("⏳ 美股盘前口径：隐含变动仍是「最近完整交易日收盘→此刻」的隔夜窗，|≥2%| / |≥5%| 阈值照常套用；"
          "美股档预告时态为「今日开盘」。今日盘前成交价另列于【盘前成交】区块——与永续是两个标的，"
          "分列不合并、不套阈值。")
    if result.get("mode_note"):
        A(f"⚠ {result['mode_note']}")
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
            line += f" · {STATUS_REASON_CN['no_spot']}"
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
        if _pm and r.get("premarket_px") is not None:
            A(f"    {premarket_text(r)}")
        if r["major"]:
            A(f"    {MAJOR_ACTION}")
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
        A(f"  ↑ {FORECAST_NOT_TRIGGER}")

    # —— 盘前成交（仅美股盘前运行）——
    # 与永续分列：两个不同标的，绝不并入上面的隐含变动列，也不套 2%/5% 阈值。
    if _pm:
        with_pm = [r for r in result["stocks"] if r.get("premarket_px") is not None]
        A("")
        A(f"【盘前成交 · 与永续分列（仅供参考·未确认、不套阈值）】（{len(with_pm)} 只取到今日盘前成交）")
        A(f"  {PREMARKET_PCT_BASIS_NOTE}")
        for r in with_pm:
            A(f"· {r['ticker']:<10} {premarket_text(r)}")
        # 按每行自己的 premarket_unavailable_reason 分组（同 technicals 的 by_reason），不统一说「无今日盘前成交」。
        for reason, tks in _premarket_missing_by_reason(result["stocks"]).items():
            A(f"· 有永续报价、无盘前价（{reason}；基差 N/A）：" + "、".join(tks))
        idx_na = [r["ticker"] for r in result.get("indices", [])
                  if r.get("premarket_unavailable_reason") == PREMARKET_INDEX_REASON]
        if idx_na:
            A(f"· {'、'.join(idx_na)}：{PREMARKET_INDEX_RULE}")
        A(f"  ↑ {PREMARKET_THRESHOLD_REASON}")

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
    groups = [(st, STATUS_REASON_CN[st]) for st in
              ("too_thin", "ratio_fail", "delisted", "no_fx", "unlisted", "forbidden")]
    for status, label in groups:
        group = [r for r in result["stocks"] if r["status"] == status]
        if not group:
            continue
        if status == "ratio_fail":
            A(f"· {label}：")
            for r in group:
                A(f"    {r['ticker']:<10} {r['market']:<12} perp {fmt_px(r['mark'])}"
                  f" / 现货折USD {fmt_px(r.get('spot_usd_equiv'))} = {r['ratio']:.3f} → 不输出隐含变动")
            A(f"    （提示：{RATIO_FAIL_HINT}）")
        elif status == "too_thin":
            A(f"· {label}：" + "、".join(f"{r['ticker']}({fmt_money(r['notional_oi'])})" for r in group))
        else:
            A(f"· {label}：" + "、".join(r["ticker"] for r in group))

    out.extend(footer_lines(mode))
    return "\n".join(out)


def _perp_without_premarket(r):
    """盘前模式下：美股档、有可用永续报价，却没有盘前价（原因看 premarket_unavailable_reason）。

    --spot 里根本没有记录的档已按 no_spot 报过，这里不再重复。
    """
    return (r.get("premarket_px") is None
            and r.get("mark") is not None
            and r.get("status") in ("ok", "ratio_fail", "no_spot")
            and r.get("premarket_unavailable_reason") not in (
                PREMARKET_NON_US_REASON, PREMARKET_NOT_RUN_REASON, PREMARKET_INDEX_REASON,
                PREMARKET_NO_REC_REASON))


def _premarket_missing_by_reason(stocks):
    """{premarket_unavailable_reason: [ticker, ...]}——正文与 degraded_reasons 共用这一份分组。"""
    by_reason = {}
    for r in stocks or []:
        if _perp_without_premarket(r):
            by_reason.setdefault(r.get("premarket_unavailable_reason") or PREMARKET_NO_PRINT_REASON,
                                 []).append(r["ticker"])
    return by_reason


# --------------------------------------------------------------------------
# 降级汇总
# --------------------------------------------------------------------------
def compute_degraded(result):
    """把本次运行的所有降级收敛成 (degraded, reasons)。

    降级 = 自检没过 / 走了兜底档 / 数据源取不到 / 任何字段因缺数据记成 N/A。
    正文里这些话本来就有，但 --quiet 时正文不打，所以必须同时是结构化字段。
    「xyz 池未上架」不算降级：池子本来就只上一部分标的，不是这次运行坏了。
    """
    reasons = []

    if not result.get("spot_given"):
        reasons.append(NO_SPOT_REASON)
    elif not result.get("spot_path"):
        reasons.append(f"--spot {result.get('spot_arg_name') or ''} 读取失败（文件读不出、"
                       "不是合法 JSON、或里面没有任何现货收盘价）：退化为只输出 perp 价与 OI 分档")
    elif not result.get("spot_date_hint"):
        reasons.append("现货基准没带数据日期：无法核对它是不是最近一个完整交易日的收盘价")

    if not result.get("indices"):
        reasons.append("未取到 SP500 / XYZ100 报价：大盘层留空")
    for row in result.get("indices", []):
        if row.get("status") == "ratio_fail":
            reasons.append(f"大盘 {row['market']} 1:1 核对失败（perp/现货 = {row['ratio']:.3f}，"
                           f"超出 {RATIO_LO}–{RATIO_HI}）：疑似取错市场，不输出隐含变动")
    if result.get("tech_rel_strength") is None:
        reasons.append("科技相对强弱 N/A：SP500 / XYZ100 两腿隐含变动未同时取到")

    for cur, note in sorted((result.get("fx") or {}).get("notes", {}).items()):
        reasons.append(f"汇率 {cur}：{note}")

    stocks = result.get("stocks") or []
    for status in ("ratio_fail", "too_thin", "delisted", "no_fx", "forbidden"):
        group = [r["ticker"] for r in stocks if r.get("status") == status]
        if group:
            reasons.append(f"{STATUS_REASON_CN[status]}：" + "、".join(group))
    if result.get("spot_path"):
        # --spot 读到了，但这些标的在里面没有收盘价——逐个都是 N/A，不能算「查过没事」
        group = [r["ticker"] for r in stocks if r.get("status") == "no_spot"]
        if group:
            reasons.append(f"{STATUS_REASON_CN['no_spot']}（--spot 里没有这些标的的收盘价）："
                           + "、".join(group))

    if result.get("mode") == "premarket":
        for reason, group in _premarket_missing_by_reason(stocks).items():
            reasons.append(f"美股盘前：以下有永续报价的美股档无盘前价（{reason}），"
                           "盘前涨跌 / 永续−盘前基差 N/A：" + "、".join(group))
    if result.get("mode_note"):
        reasons.append(result["mode_note"])

    return bool(reasons), reasons


def write_json(result, dest, quiet):
    """落盘结构化结果。写失败只警告不中断——正文/落盘互不绑架。"""
    out = Path(dest)
    try:
        if out.parent and str(out.parent) not in ("", "."):
            out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        err(f"警告：写入 {out.name} 失败：{scrub(exc)}")
    else:
        if not quiet:
            print(f"\n已写入 {rel_display(out)}")


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(
        description="24/7 永续隐含变动（Hyperliquid xyz 池）· 纯观察节点，绝不改变 T1/T2/T3 与分桶",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--json", metavar="OUT", help="把结构化结果写到该文件")
    ap.add_argument("--quiet", action="store_true",
                    help="stdout 不打人类可读正文（并发调度下多个单元同时跑，正文会交织）；"
                         "**不关警告**：stderr 照常喊，降级照常进 JSON 的 degraded/degraded_reasons")
    ap.add_argument("--spot", metavar="PATH",
                    help="technicals.py --json 的输出，提供各标的最近完整交易日收盘价；不给则只输出 perp 价与 OI 分档")
    ap.add_argument("--timeout", type=float, default=20.0, help="单次请求超时秒数（默认 20）")
    args = ap.parse_args()

    universe, universe_path = load_universe()

    # spot_given / spot_arg_name 与 spot_path 是三件不同的事：
    #   spot_given    = 用户给没给 --spot
    #   spot_arg_name = 给的那个文件名（只回显文件名，不带绝对路径）
    #   spot_path     = 真的读出现货价了（None = 本次没有现货基准）
    # 早先只有 spot_path，报告头据此打「未提供 --spot」——于是「给了但读取失败」
    # 会被如实性反了地写成「没给」，读者以为是自己漏了参数，实际是 tech.json 坏了/空了。
    # 前两个在取数之前就算好：取数失败那条路径也要如实报告 --spot 给没给。
    spot_given = bool(args.spot)
    spot_arg_name = Path(args.spot).name if args.spot else None

    # --spot 与盘口模式必须在取数**之前**读：取数失败那条路径也要写 mode / intraday / session。
    # （早先放在取数之后，失败路径引用未赋值的 intraday → UnboundLocalError、exit 1、不落 JSON。）
    spot_flat, spot_path, session = {}, None, None
    if args.spot:
        spot_flat, session = load_spot(Path(args.spot))
        if spot_flat:
            spot_path = spot_arg_name
    intraday, premarket, mode = derive_mode(session)
    mode_note = derive_mode_note(session, mode)

    payload, reason = fetch_meta_and_ctxs(args.timeout, retries=2)   # 最多重试 2 次
    if payload is None:
        # 硬约束 6：整节写这句，不影响其余所有部分
        lines = [
            # 标题随模式走（section_title）；overnight 的整句与改动前逐字相同。
            "== 第二步之二：" + section_title(mode)
            + ("" if mode == "premarket" else "（24/7 永续）") + "==",
            f"本次未取到 24/7 永续数据（{reason}；已重试 2 次）。",
            "按规则：本节留空，不影响其余所有部分；不得因此改变任何 T1/T2/T3 判定或分桶。",
        ]
        if mode_note:
            lines.append(f"⚠ {mode_note}")
        # --quiet 只关 stdout 的正文，绝不关警告：这三句改走 stderr。
        # 否则并发调度下这次运行会一声不吭地 exit 0，读者以为「查过，没事」。
        for line in lines:
            print(line, file=sys.stderr if args.quiet else sys.stdout)
        if args.json:
            # 取数失败也要落盘：--json 指定了却没有文件，下游只会读到上一次的陈旧结果。
            reasons = [f"未取到 24/7 永续数据（{reason}；已重试 2 次）：本节留空"]
            if not spot_given:
                reasons.append(NO_SPOT_REASON)
            if mode_note:
                reasons.append(mode_note)
            write_json({
                "section": "第二步之二·24/7 永续隐含变动",
                "observation_only": True,
                "mode": mode,
                "mode_note": mode_note,
                "intraday": intraday,
                "session": session,
                "note": OBSERVATION_NOTE,
                "degraded": True,
                "degraded_reasons": reasons,
                "prohibitions": prohibitions_block(mode),
                "source": {"url": API_URL, "type": "metaAndAssetCtxs", "dex": DEX},
                "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "fetch_error": reason,
                "market_count": 0,
                "market_listed": 0,
                "universe_path": universe_path,
                "universe_count": len(universe),
                "spot_path": spot_path,          # --spot 在取数前已读，读到了就如实回显
                "spot_given": spot_given,
                "spot_arg_name": spot_arg_name,
                "spot_date_hint": None,
                "thresholds": THRESHOLDS,
                "fx": {"rates": {}, "notes": {}},
                "fx_lines": [],
                "indices": [],
                "tech_rel_strength": None,       # null = 没算成，不是 0
                "stocks": [],
            }, args.json, args.quiet)
        sys.exit(0)

    markets = build_markets(payload)

    # 汇率（同池）
    fx_rates, fx_notes, fx_lines = {}, {}, []
    for cur in {c for _, c in universe if c and c != "USD"}:
        mname = FX_MARKETS.get(cur)
        m = markets.get(mname) if mname else None
        if m and m["mark"]:
            fx_rates[cur] = m["mark"]
            # 「已下架」与「有市场但没人持仓」是两回事，分开写。
            # 实测 2026-09-15（xyz 池）：xyz:KRW **确实带 isDelisted=true**，OI 与
            # dayNtlVlm 皆 0，仅剩 mark=1360.7 这个 oracle 报价；xyz:DXY(97.15)、
            # xyz:VIX(20.0) 同型。下架市场的 oracle **有可能**停更，所以实测过：
            # 2026-09-15 xyz:KRW = 1360.9 对 yfinance `KRW=X` 1360.95，偏离 −0.00%，
            # 且两次查询之间 1360.7→1360.9 有跳动 → **这条 oracle 是活的**，韩股折算无碍。
            # 但它仍是韩股两档 USD 折算的单点依赖、且已下架，所以每次都要标注；
            # 若哪天偏离拉开，先查这里（拿 xyz:KRW 的 markPx 对照 yfinance KRW=X 即可）。
            # （同池 xyz:VIX 的 20.0 是整数且两次查询未变，可疑；本技能不用它。CLAUDE.md
            #  已记录腾讯 .VIX 冻结七个月的同类陷阱：HTTP 200 + 合理数字 ≠ 活数据。）
            if m["delisted"]:
                fx_notes[cur] = f"{PREFIX}{mname} 已下架，折算结果仅参考"
            elif m["notional_oi"] in (0, None) or m["day_ntl_vlm"] == 0:
                fx_notes[cur] = (f"{PREFIX}{mname} 无持仓/无成交（本池 FX 为 oracle 报价、不撮合，"
                                 f"零 OI 属常态），汇率可用，折算结果标注来源即可")
            fx_lines.append(f"汇率：1 USD = {fmt_px(m['mark'])} {cur}（同池 {PREFIX}{mname}）"
                            + (f" · {fx_notes[cur]}" if cur in fx_notes else ""))
        elif mname:
            fx_lines.append(f"汇率：同池 {PREFIX}{mname} 未取到 → {cur} 计价标的本次跳过。")
        else:
            fx_lines.append(f"汇率：{cur} 计价标的在 {DEX} 池无对应汇率市场 → 本次跳过。")

    # 个股
    stocks = [attach_premarket(
                  finalize_row(evaluate(t, c, markets, spot_flat, fx_rates, fx_notes, intraday,
                                        premarket)),
                  spot_lookup(spot_flat, t), mode)
              for t, c in universe]

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
        indices.append(attach_premarket(finalize_row(row, triggers=False), rec, mode, index=True))

    by_market = {r["market"]: r for r in indices}
    sp = by_market.get(f"{PREFIX}SP500", {}).get("implied_pct")
    nx = by_market.get(f"{PREFIX}XYZ100", {}).get("implied_pct")
    tech_rs = (nx - sp) if (sp is not None and nx is not None) else None

    spot_dates = [r.get("spot_date") for r in stocks if r.get("spot_date")]
    result = {
        "section": "第二步之二·24/7 永续隐含变动",
        "observation_only": True,
        # overnight / premarket / intraday —— 只由 tech.json 的 session.US 推出（derive_mode），不看时钟。
        "mode": mode,
        # 非 null = 盘前窗口内却落到 overnight，美股档「明日」措辞可能有误（见 MODE_NOTE_PM_WINDOW）。
        "mode_note": mode_note,
        "intraday": intraday,
        "session": session,
        "note": OBSERVATION_NOTE,
        # 降级汇总在下面回填（要先有完整 result 才算得出来），放在这里是为了排在头部显眼处。
        "degraded": None,
        "degraded_reasons": [],
        "prohibitions": prohibitions_block(mode),
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
        "thresholds": THRESHOLDS,
        "fx": {"rates": fx_rates, "notes": fx_notes},
        "fx_lines": fx_lines,
        "indices": indices,
        "tech_rel_strength": tech_rs,
        "stocks": stocks,
    }

    result["degraded"], result["degraded_reasons"] = compute_degraded(result)

    if args.quiet:
        # 正文关了，降级就没人念了——逐条补到 stderr。不 quiet 时正文本身已经写清楚，
        # 再往 stderr 抄一遍只会把同一件事说两遍。
        for line in result["degraded_reasons"]:
            err(f"警告：降级 · {line}")
    else:
        print(render(result))

    if args.json:
        write_json(result, args.json, args.quiet)
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
