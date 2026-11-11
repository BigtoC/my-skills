#!/usr/bin/env python3
"""
个股技术面 + 宏观利率取数（第二步 · yfinance 本地计算）

把 references/data-acquisition.md 「附：参考取数代码骨架」补全成可跑脚本。
计算口径逐字沿用该文件的「指标定义」，不做任何自创改动：

    RSI(14)   Wilder 平滑（EWM alpha=1/14, adjust=False, min_periods=14）
    52周高    过去 252 个交易日 High 的最大值
    20日高    过去 20 个交易日 High 的最大值
    回撤%     (收盘 - 区间高) / 区间高 * 100
    T1触发价  52周高 * 0.85     收盘 <= 此价 即 T1 成立
    T2触发价  20日高 * 0.92     收盘 <= 此价 即 T2 成立
    T3 当前值 当日 RSI14        RSI <= 35 即 T3 成立
    量比      当日成交量 / 20日均量
    均线结构  多头=MA5>MA10>MA20>MA60；空头=MA5<MA10<MA20<MA60；其余 纠缠/转折

硬约束（弄错会直接改变触发判定，改代码前先读 references/data-acquisition.md）：
  * auto_adjust=False —— 港股股息大，默认复权会把 0700.HK 的 52周高算成 675.1
    （原始 683.0）、0941.HK 算成 86.3（原始 90.6），T1触发价与回撤% 随之失真。
  * 港股的 收盘价/涨跌幅/成交量/52周高低 以姊妹技能 ai-industry-weekly 的
    scripts/hk_quote.py 为准（本脚本 subprocess 调用它，不另存一份）；yfinance
    只补 MA/RSI/20日高 等派生指标。两处数字冲突时一律以 hk_quote.py 为准。
  * 完整交易日按**各自市场**判定：港股/韩股不随美股基准回退，各标的自带 asof。
  * 缺失一律 N/A，不估算。20日高缺失 -> T2 与 T2触发价都 N/A 且「暂不判定」，
    不得强行触发。
  * 上市不足 MIN_HISTORY_BARS 根日线的标的自动标「历史不足，不参与技术面判定」，
    不进任何桶、也不进「未触发」清单（自动侦测，不硬编码代码名单）。

标的清单来自姊妹技能 ai-industry-weekly 的 assets/universe.json（按 order），
本脚本不内联任何 ticker 列表、不硬编码标的数量。

用法:
    python3 scripts/technicals.py                      # 全标的技术面（对齐表格）
    python3 scripts/technicals.py --json out.json      # 写 JSON 文件
    python3 scripts/technicals.py --json               # JSON 打到 stdout
    python3 scripts/technicals.py --tickers NVDA,TSM   # 只跑子集
    python3 scripts/technicals.py --macro-only         # 只出宏观利率/驱动源输入
    python3 scripts/technicals.py --no-earnings        # 跳过财报日（省 N 次请求）
    python3 scripts/technicals.py --no-live-overlay    # 不附盘中/盘前叠加层（也不取盘前 1m）

美股盘前（工作日 04:00–09:30 ET）运行时，另取 1m prepost=True 的今日盘前最后一笔成交，
作为 ⏳ 盘前叠加层（未确认）：T1/T2/T3 与分桶仍只用最近完整交易日收盘，绝不改写。

依赖: python3 + yfinance + pandas + numpy + requests（港股走 hk_quote.py）。
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import json
import math
import subprocess
import sys
import unicodedata
import re
from concurrent.futures import ThreadPoolExecutor
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

from zoneinfo import ZoneInfo

# 姊妹技能定位是三个脚本共用的逻辑，收敛在同目录的 _weekly.py 里。
# 以 `python3 /abs/path/scripts/technicals.py` 方式调用时同目录 import 本来就成立，
# 这里再显式把脚本目录加进 sys.path，保证 -P / PYTHONSAFEPATH 等场景下也稳。
_SCRIPTS_DIR = str(Path(__file__).resolve().parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from _weekly import (  # noqa: E402  （必须在上面的 sys.path 之后）
    NEED_UNIVERSE,
    WEEKLY_DIRNAME,
    WEEKLY_ENV,
    locate_weekly_skill_or_exit,
    rel_display as rel_path,
)

# ---------------------------------------------------------------- 常量

MIN_HISTORY_BARS = 126      # 约半年。少于此的次新标的 RSI/均线/52周高全是失真读数
RSI_N = 14
LOOKBACK_52W = 252
LOOKBACK_20D = 20
T1_RATIO = 0.85             # T1触发价 = 52周高 * 0.85
T2_RATIO = 0.92             # T2触发价 = 20日高 * 0.92
T3_RSI = 35.0               # T3: RSI14 <= 35

# 财报日（⚠EARN 标签）预取并发度。串行时它是整轮的绝对大头：46 档里 41 只非 ETF
# 各一次 get_earnings_dates() 往返 ≈ 39s / 全程 ≈ 45s。上限刻意压在个位数——再高就是
# 往 Yahoo 限流上撞，而限流的表现正是「整片查不到」，比慢危险得多。
EARNINGS_MAX_WORKERS = 6
# 无结果占比超过这条线就当限流嫌疑处理。样本 < MIN_N 时不判：--tickers 调子集时
# 「1/3 只查不到」多半只是那只票本来就没有财报日历，报限流是噪声不是信号。
#
# ⚠️ empty 与 failed **不是同一件事**，相加当讯号会稳定误报：
#   failed = 真的抛了异常。Yahoo 的 429 在 yfinance 里就是抛出来的——这才是限流的样子。
#   empty  = 呼叫成功、回了空。多数时候是「这只本来就没有财报日历」：港股 0700/1810/
#            0941、韩股 000660/005930、ADR MRAAY 常态如此，全宇宙约 6/41 长期是 empty。
# MIN_N=5 挡不住这个：`--tickers 0700.HK,1810.HK,0941.HK,000660.KS,005930.KS,MRAAY`
# 会是 6/6 empty = 100%，稳定报「疑似 Yahoo 限流」——一个永远为真的假警报。
# 但 empty 也不全然无辜：限流偶尔表现为回空而不抛。所以规则按 failed 分档（见
# earnings_block）：failed==0 时不论多少 empty 都不报限流，只如实说「没有财报日历」。
EARNINGS_THROTTLE_RATIO = 1 / 3
EARNINGS_THROTTLE_MIN_N = 5

TNX_TICKER = "^TNX"
DXY_TICKER = "DX-Y.NYB"
VIX_TICKER = "^VIX"

# 现货指数收盘：perp_quotes.py 拿它们对照 xyz:SP500 / xyz:XYZ100 算隔夜隐含跳空，
# 并据此算「科技相对强弱 = XYZ100 隐含 − SP500 隐含」
# （references/perp-overnight.md、references/output-format.md 的 📊 必出行）。
# 缺了这两个代码，那一行结构性地永远算不出来，所以它们必下、必进 --json。
SPX_TICKER = "^GSPC"        # 标普500 现货指数（对 xyz:SP500）
NDX_TICKER = "^NDX"         # 纳斯达克100 现货指数（对 xyz:XYZ100）

# 指数/大盘背景标的。SMH/SOXX 通常已在 universe 里，重复无害（下面会去重）。
INDEX_TICKERS = ["QQQ", "SMH", "SOXX", SPX_TICKER, NDX_TICKER,
                 VIX_TICKER, TNX_TICKER, DXY_TICKER]
US_REF_TICKER = "SMH"       # 美股「完整交易日」基准（骨架口径）

# 「大盘背景」表里逐行展示的标的（收盘 + 日涨跌%）
BACKDROP_TICKERS = ("QQQ", "SMH", "SOXX", SPX_TICKER, NDX_TICKER, VIX_TICKER)

# 现货指数代码 → 中文名，供 --json 的 indices 块与人读表格共用
INDEX_NAMES_CN = {SPX_TICKER: "标普500", NDX_TICKER: "纳斯达克100"}

# 10Y 合理区间（用于 ^TNX 单位侦测：yfinance 有时给 % 有时给 %x10）
TNX_MIN_PCT, TNX_MAX_PCT = 0.5, 8.0

# open 只用于**标签诚实度**（盘前 vs 盘中）与美股盘前时段判定，不参与 asof 判定：
# 两者都是「当日 K 线未完成」，asof 回退完全一致。但 RUN_MODE **不**一致——
# SKILL.md 的 RUN_MODE 一行只把 state == "intraday" 映射成 intraday，pre_market /
# pre_open 都是 postclose（权威运行：写信用历史、run_state、commit）。之所以要分标签，
# 是因为 yfinance 在开盘前就会送出一根日期为今天的日 bar（实测 06:11 ET 已有 09-15 的
# bar；2026-11-11 05:40 ET 实测这根 bar 只出现在 DX-Y.NYB / ^VIX 这类近 24h 标的上，
# 个股当日 bar 全是 NaN），此时若印「盘中」，报告就在市场还没开的时候声称自己在看盘中价。
MARKETS = {
    "US": {"tz": "America/New_York", "open": dt.time(9, 30), "close": dt.time(16, 0), "label": "美股"},
    "HK": {"tz": "Asia/Hong_Kong", "open": dt.time(9, 30), "close": dt.time(16, 0), "label": "港股"},
    "KR": {"tz": "Asia/Seoul", "open": dt.time(9, 0), "close": dt.time(15, 30), "label": "韩股"},
}

# 美股盘前时段起点（ET）。[US_PREMARKET_START, MARKETS["US"]["open"]) 即盘前窗口。
# 实测（2026-11-11 05:38 ET）yf.download(period="1d", interval="1m", prepost=True)
# 从 04:00 ET 起给 1m bar，最后一行是最近一笔盘前成交。窗口外（00:00–04:00）同一调用
# 回的是**前一交易日**的 bar，必须按「今日 ET 日期 + 窗口」双重过滤才能用。
US_PREMARKET_START = dt.time(4, 0)

# 盘前叠加层的来源说明——行级 live_source、顶层 live.source、premarket.source 共用这一份，
# 改一处等于改全部（JSON 与正文逐字同源）。
PREMARKET_SOURCE = "yfinance 1m prepost=True · 今日盘前最后一笔成交（04:00–09:30 ET）"
PREMARKET_CALIBER = "盘前成交价（未复权）；对照的 hi52/hi20/均线为日线拆股已还原·股息未复权口径"
# 盘前价的定位：只做参考，但分析照常进行。正文（标题/表头/宏观行/报告头）与 JSON
# （premarket.reference_note、live.reference_note）共用这一句。
PREMARKET_REFERENCE_NOTE = ("盘前价仅供参考：分析照常进行（现价、52w/20D回撤、距T1/T2、均线位置、盘前触及均按盘前价算），"
                            "但 T1/T2/T3 触发与分桶只认最近完整交易日收盘")
# 盘前大盘背景：只取这三只 ETF。^GSPC/^NDX/^TNX 实测**没有**盘前 bar（1m 回的是前一交易日），
# 不拿 SPY/ES/NQ 之类别的工具顶替——不同工具、比值也不是 1:1。
PREMARKET_BACKDROP = ("QQQ", "SMH", "SOXX")
PREMARKET_UNAVAILABLE = (SPX_TICKER, NDX_TICKER, TNX_TICKER)
# 选中但不在盘前请求清单里的美股行（premarket_eligible 为 False）的 N/A 理由：
# 它们根本没被请求，写「该档今日盘前尚无成交」就是把「没去要」说成「要了没有」。
PREMARKET_INELIGIBLE_REASON = "指数/期货无盘前成交"
PREMARKET_FOREIGN_REASON = ("非美国本土挂牌（代码含「.」）：不取盘前价——其 1m bar 是外盘正式盘成交，"
                            "落在 04:00–09:30 ET 也不是美股盘前")

# 折现率信号阈值（references/data-acquisition.md「折现率信号」）
RATE_UP_1D_BP, RATE_UP_5D_BP = 10.0, 25.0
RATE_DN_1D_BP, RATE_DN_5D_BP = -10.0, -25.0

pd = None  # 延迟导入，见 load_deps()
requests = None  # 延迟导入，见 load_deps()；make_session() 用它建 Yahoo 会话


# ---------------------------------------------------------------- 基础工具


def err(msg: str = "") -> None:
    print(msg, file=sys.stderr)


def disp_w(s: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in str(s))


def pad(s: str, width: int) -> str:
    return str(s) + " " * max(0, width - disp_w(str(s)))


def print_table(headers, rows) -> None:
    if not rows:
        return
    widths = [max(disp_w(h), *(disp_w(r[i]) for r in rows)) for i, h in enumerate(headers)]
    print(" | ".join(pad(h, widths[i]) for i, h in enumerate(headers)))
    print("-+-".join("-" * w for w in widths))
    for r in rows:
        print(" | ".join(pad(r[i], widths[i]) for i in range(len(headers))))


def is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and not math.isnan(v)


def fnum(v, d=2, suffix="") -> str:
    return f"{v:,.{d}f}{suffix}" if is_num(v) else "N/A"


def fsign(v, d=2, suffix="") -> str:
    return f"{v:+,.{d}f}{suffix}" if is_num(v) else "N/A"


def fbool(v) -> str:
    if v is True:
        return "✅"
    if v is False:
        return "—"
    return "N/A"


def rnd(v, d=4):
    """转成可 JSON 序列化的 float；NaN/None/inf 一律 None（= N/A，不估算）。"""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return round(f, d)


def load_deps():
    """延迟导入重依赖，让 --help 在没装 yfinance 的环境下也能跑。"""
    global pd, requests
    try:
        import numpy  # noqa: F401  # 只做依赖存在性检查（pandas 计算依赖它）
        import pandas as _pd
        import requests as _requests
        import yfinance as _yf
    except ImportError as exc:  # pragma: no cover
        # exc.name 在「库自身的传递依赖缺失」等情况下会是 None，
        # 早先直接内插会打出「缺少依赖 None」——等于什么都没说。
        missing = getattr(exc, "name", None) or "yfinance / pandas / numpy / requests 之一"
        err(f"错误：缺少依赖 {missing}（{scrub(exc)}）。"
            f"请先 `pip install yfinance pandas numpy requests`。")
        # 依赖缺失 = 退出码 2（本仓库保留：1 参数错误｜2 依赖缺失｜3 取数失败｜4 量级自检未通过）。
        # 2026-09-11 前这里回 1，与顶层「未预期异常」同码，调度层分不清「库没装」和「代码炸了」。
        # 而 2026-09-07 之前**取数失败也回 1**——三件不同的事挤在一个退出码里，
        # 结果是历史上被归因成「Yahoo 限流」的失败中，有多少其实是容器里缺依赖，已经查不回来了。
        # 2026-09-11 实测：同一个 Routines 容器三次运行分别缺 numpy、缺 yfinance、依赖齐全，
        # 依赖状态本身就是不稳定变量，所以这个码必须能单独识别。
        sys.exit(2)
    # yfinance 取不到某个代码时会往 stderr 吐几行英文（"1 Failed download: ... possibly
    # delisted"、"HTTP Error 404: {...}"），这些噪声会混进日报正文。同一件事本脚本已经用
    # 中文说了一遍——「yfinance 未返回数据的标的（记 N/A，不估算）：...」，所以这里把
    # yfinance 自己的 logger 闭掉，信息不丢、噪声不出。要看原始英文报错时临时改回
    # logging.getLogger("yfinance").setLevel(logging.WARNING) 即可。
    try:
        import logging
        logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    except Exception:  # noqa: BLE001 - 抑噪失败无关紧要，绝不能因此挡住取数
        pass
    pd = _pd
    requests = _requests
    return _yf


# ---------------------------------------------------------------- Yahoo 传输层
#
# 为什么必须显式给 yfinance 一个 requests.Session（2026-09-11 在 Routines 容器实测定案）：
#
#   引擎            UA              代理隧道        Yahoo 服务端     结果
#   curl_cffi      Chrome(冒充)     ❌ 握手断       （到不了）       yfinance 默认 -> 全灭
#   requests       python-requests/* ✅ 通          ❌ 429           裸 Session -> 限流
#   requests       浏览器 UA         ✅ 通          ✅ 200           ← 只有这一种可用
#
# ① 代理层：本执行环境出站 HTTPS 走本地代理，curl_cffi 冒充 Chrome 的 TLS 指纹
#    （ClientHello 约 1.7~1.8KB）在该代理上无法完成到 guce.yahoo.com / query2 的隧道，
#    ~6 秒后 code 1006 断开、只收到 39 字节，**根本走不到能返回 HTTP 状态码的那一层**。
#    这正是姊妹脚本 ai-industry-weekly/scripts/fetch_fundamentals.py 开头第 1 条
#    「必须用 requests.Session 而不是 curl_cffi，本执行环境出站走代理」记录的同一个故障，
#    那边早就修了，本脚本此前一直没应用同一修法。
# ② 服务端层：Yahoo 另外按 UA 分层限流，裸 requests 默认 UA 稳定回 429
#    （body 为 "Edge: Too Many Requests"）。所以只换引擎不换 UA 仍然失败。
#
# 两层独立叠加，requests.Session + 浏览器 UA 一次同时绕开。症状是 EXIT=3 且与批量大小无关
# （2026-09-11 实测：2 只与 46 只同样 exit 3，全量批次耗时 11 分钟；修复后本机实测 46 只 13 秒）。

# 逐字沿用 fetch_fundamentals.py 的那串 Chrome UA——那边记着「换 UA 曾导致 Yahoo
# 限流返回空 info」。两处必须一致，改一处就要改另一处。
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

# yfinance 版本间 session= 的支持情况不一（Routines 实测 1.7.0，本地 1.4.1 两者都收）。
# 一旦某个版本不收，退回默认引擎并**显式降级**——绝不静默，否则代理环境下会变成全灭。
_SESSION_OK = True
# 回退档（非 Yahoo 源）产生的告警，同样由 main() 汇进 asof_notes。
_FALLBACK_NOTES: list = []

# make_session / 调用点产生的传输层告警，由 main() 汇进 asof_notes。
# 必须以 ⚠ 开头：apply_degraded() 靠这个前缀把它转成 degraded_reasons 字段。
_ENGINE_NOTES: list = []


def make_session():
    """建 Yahoo 用的 requests.Session（浏览器 UA）。见上方矩阵，不要改回默认引擎。"""
    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    return s


def _engine_fallback(why: str) -> None:
    """第1档（requests.Session）失败、退到第2档（默认 curl_cffi）时的降级记录。

    与 _session_unsupported 的区别：那个是「版本不支持 session=」，这个是「支持但这一轮
    没取到」。两者都必须进 degraded_reasons——回退必须响（CLAUDE.md 回退链规则第 6 条），
    静默降级等于把「不知道」记成「查过了，没事」。
    """
    global _SESSION_OK
    _SESSION_OK = False
    note = f"⚠ yfinance 引擎回退至第2档(默认 curl_cffi)：{why}"
    if note not in _ENGINE_NOTES:
        _ENGINE_NOTES.append(note)
    err(note)


def _session_unsupported(where: str) -> None:
    """某个 yfinance 版本不收 session= 时的统一降级记录（只记一次）。"""
    global _SESSION_OK
    if _SESSION_OK:
        _SESSION_OK = False
        # 措辞不写死「已退回 curl_cffi」：YfData 是进程级单例，若 download 已成功装上
        # requests session，Ticker 这条路径退回后实际仍用着它——那句话在该路径上是假的。
        note = (f"⚠ 当前 yfinance 不支持 {where}(session=...)，该调用已退回 yfinance 自管引擎；"
                "若本环境出站走代理且实际用的是 curl_cffi，Yahoo 可能整片取不到数"
                "（见脚本内传输层矩阵）")
        _ENGINE_NOTES.append(note)
        err(note)


# 引擎档位的人读名，供 premarket.tier / sources.premarket_tier 使用（None = 两档都没取到）
ENGINE_TIER_NAMES = {1: "第1档 requests.Session(浏览器UA)", 2: "第2档 yfinance 默认引擎(curl_cffi)"}


def yf_download_tiered(yf, sess, why: str = "", errors=None, **kw):
    """yf.download 的引擎回退链（顺序写死，见 CLAUDE.md「Fallback chains — the rule」）。

    返回 (raw, tier)：tier ∈ {1, 2, None}，None = 两档都没取到（raw 为 None 或空表）。

      第1档 requests.Session + 浏览器 UA   第2档 yfinance 默认 curl_cffi 引擎
    两档都必须保留，因为两种故障方向相反、互为对方的解药（实测见 make_session() 上方）：
      代理环境   curl_cffi 握不完 TLS 隧道  -> 只有第1档能过
      IP 被限流  requests.Session 吃 429    -> 只有第2档能过（它复用缓存 cookie、
                 TLS 指纹不同，限流窗口内仍取得到数）
    ⚠ 绝不要把这里简化成「只用第1档」：2026-09-11 本脚本一度被改成硬切第1档，那在代理
    环境下是对的，但在限流环境下比改之前更差——原本 curl_cffi 能扛的那一轮会变成整片空。

    why：调用点标签，前缀进 _engine_fallback 的 note。日线调用传空串，文案与抽出前
    逐字一致；盘前 1m 调用传「盘前 1m」——否则两次回退的 note 文案相同，会被
    _engine_fallback 的去重吞成一条，「1m 那次也回退了」这件事就看不见了。

    与 session= 无关的 TypeError 照旧上抛（真 bug 不得被记成一次「版本不兼容」降级）；
    调用方若不允许失败（盘前 1m），自己兜。

    errors：可选 list。第2档也抛错（「两档引擎均取数失败」）时，把 stderr 那行的成因
    （已 scrub）追加进去，供调用方写进 JSON——stderr 只能是冗余通道（JSON 等价规则）。
    日线调用不传，行为与抽出前一致。
    """
    tag = f"{why}：" if why else ""

    def _dl(use_session):
        call = dict(kw)
        if use_session:
            call["session"] = sess
        return yf.download(**call)

    raw, tier = None, None
    try:
        raw = _dl(True)
    except TypeError as exc:           # 可能是「该版本不收 session=」，也可能无关
        if "session" not in str(exc):
            # 与 session= 无关的 TypeError：不得吞掉、更不得重跑整轮下载，
            # 否则真正的 bug 会被记成一次「版本不兼容」降级。
            raise
        _session_unsupported("download")
    except Exception as exc:  # noqa: BLE001 - 第1档任何失败都只降级到第2档，不掀掉整轮
        _engine_fallback(f"{tag}第1档 requests.Session 取数抛错（{type(exc).__name__}）")
    if raw is not None and len(raw) > 0:
        tier = 1

    # 空表不等于「没有数据」：限流时第1档回空而第2档仍有数，直接当取数失败会误报。
    if raw is None or len(raw) == 0:
        try:
            raw2 = _dl(False)
        except Exception as exc:  # noqa: BLE001
            cause = f"{type(exc).__name__}: {scrub(exc)}"
            err(f"✗ {tag}两档引擎均取数失败：{cause}")
            if errors is not None:
                errors.append(cause)
            raw2 = None
        if raw2 is not None and len(raw2) > 0:
            _engine_fallback(f"{tag}第1档 requests.Session 回空表、第2档默认引擎取到数据"
                             "（多半是本机 IP 被 Yahoo 限流，429 只挡得住第1档）")
            raw, tier = raw2, 2
    return raw, tier


# 最近一次 fetch_us_premarket 的传输层元数据（与 _ENGINE_NOTES 同一种进程级记录方式），
# 供 premarket.tier / sources.premarket_tier 使用；error = 1m 取数本身失败的成因（已 scrub），
# None = 取数调用没有失败（取到空表不算失败——那可能就是休市日）。
_PREMARKET_FETCH = {"tier": None, "error": None}


def premarket_eligible(ticker: str) -> bool:
    """能否向 Yahoo 要盘前 1m：只有美股个股/ETF。

    ^ 开头的指数/收益率实测**没有**盘前 bar；含 = 的是期货/外汇。
    港股/韩股排除不只是「它们没有美股盘前」：混批时它们的 1m bar 被换算成 ET 后
    日期恰好是今天（实测 0700.HK 03:08 ET、000660.KS 01:00 ET），只按日期过滤会把
    港韩的**正式盘**价当成美股盘前价。hk_quote 旗标由调用方另行排除。
    含「.」的一律排除：market_of() 把不带 .HK/.KS/.KQ 的都算美股，于是 ASML.AS、AZN.L
    这类外盘挂牌也会混进来——实测（2026-11-11 06:11 ET）它们回的是阿姆斯特丹/伦敦
    **正式盘** 05:56 ET 的成交，正落在 04:00–09:30 ET 窗口里。Yahoo 的美股分级股
    写作「-」（BRK-B），不受影响；DX-Y.NYB 也因此被排除（它本就没有盘前个股意义）。
    """
    t = str(ticker).strip().upper()
    return (bool(t) and market_of(t) == "US" and not t.startswith("^") and "=" not in t
            and "." not in t)


def fetch_us_premarket(yf, sess, tickers, now_et):
    """取美股**今日盘前**最后一笔成交 → ({TICKER: {...}}, notes)。

    每档：premarket_px（float）、premarket_ts（该笔 1m bar 的 ET 时间戳，不是运行时刻）、
    premarket_age_min（距 now_et 的分钟数）、premarket_bars（窗口内 bar 数）。

    过滤三道，缺一不可：
      ① 请求清单只要 premarket_eligible() 的代码；
      ② bar 的 ET 日期 == now_et 的日期（窗口外同一调用回的是前一交易日的 bar）；
      ③ US_PREMARKET_START <= ET 时刻 < 开盘 09:30（盘后/正式盘的 bar 一律不算盘前）。
    最后一行可能是还在进行中的那一分钟（实测时间戳 05:40:31，不在整分）——它仍是
    最近一笔成交价，照用，时间戳如实给出。

    ⚠ 1m 索引是 tz-aware（America/New_York），日线是 tz-naive：**绝不**与日线帧混用或
    union（见 fallback_frames 帧契约那段：union 回 object 索引，sort 抛 TypeError，被顶层
    兜底吞成 exit 1）。本函数的结果只以纯 Python 值离开，不进 frames。

    任何失败只降级（⚠ note），绝不抛、绝不改退出码：盘前层是附加观察层，
    取不到 = 本次无盘前叠加层，不是取数失败。
    """
    us_tz = ZoneInfo(MARKETS["US"]["tz"])
    now_et = now_et.astimezone(us_tz)
    req = list(dict.fromkeys(str(t).strip().upper() for t in tickers if premarket_eligible(t)))
    _PREMARKET_FETCH["tier"] = None
    _PREMARKET_FETCH["error"] = None
    if not req:
        return {}, []
    notes, dl_errors = [], []
    try:
        raw, tier = yf_download_tiered(
            yf, sess, "盘前 1m", errors=dl_errors,
            tickers=req, period="1d", interval="1m", prepost=True, progress=False,
            auto_adjust=False, group_by="ticker", threads=True)
    except Exception as exc:  # noqa: BLE001 - 1m 失败只降级，绝不掀掉整轮（含无关 TypeError）
        _PREMARKET_FETCH["error"] = f"{type(exc).__name__}: {scrub(exc)[:160]}"
        return {}, [f"⚠ 盘前 1m 取数抛错（{_PREMARKET_FETCH['error']}），"
                    f"{len(req)} 档盘前价记 N/A；本次无盘前叠加层"]
    _PREMARKET_FETCH["tier"] = tier
    if raw is None or len(raw) == 0:
        if dl_errors:
            # 两档引擎都抛错：成因原样进 JSON（stderr 那行只是冗余通道），且这不是「休市日」
            _PREMARKET_FETCH["error"] = dl_errors[-1][:300]
            notes.append(f"⚠ 盘前 1m 两档引擎均取数失败（{_PREMARKET_FETCH['error']}），"
                         f"{len(req)} 档盘前价记 N/A；本次无盘前叠加层")
        return {}, notes

    today = now_et.date()
    open_t = MARKETS["US"]["open"]
    out, naive = {}, []
    for t in req:
        # 单只请求时 yfinance 可能不给 MultiIndex：frame_for 在那种情况下回整张表，
        # 只有 req 恰为一只时才成立——所以只在 len(req)==1 时接受非 MultiIndex。
        if not isinstance(raw.columns, pd.MultiIndex) and len(req) != 1:
            continue
        f = frame_for(raw, t)
        if f is None:
            continue
        idx = f.index
        if getattr(idx, "tz", None) is None:
            # 无时区就无从判定「今天几点」：不猜（不当 ET 也不当 UTC），记 N/A。
            naive.append(t)
            continue
        et = idx.tz_convert(us_tz)
        keep = [i for i, ts in enumerate(et)
                if ts.date() == today and US_PREMARKET_START <= ts.time() < open_t]
        if not keep:
            continue
        px, ts_last = None, None
        for i in reversed(keep):
            try:
                v = float(f["Close"].iloc[i])
            except (TypeError, ValueError):
                continue
            if is_num(v) and v > 0:
                px, ts_last = v, et[i]
                break
        if px is None:
            continue
        ts_py = ts_last.to_pydatetime()
        age = (now_et - ts_py).total_seconds() / 60.0
        out[t] = {
            "premarket_px": rnd(px, 4),
            "premarket_ts": ts_py.isoformat(timespec="seconds"),
            # 进行中的那一分钟时间戳可能比 now_et 晚几秒：记 0，不记负数
            "premarket_age_min": rnd(max(0.0, age), 1),
            "premarket_bars": len(keep),
        }
    if naive:
        notes.append(f"⚠ 盘前 1m 数据索引无时区 {len(naive)} 档，无法判定是否落在盘前时段，"
                     f"记 N/A（不猜时区）：" + ", ".join(naive))
    return out, notes


# ---------------------------------------------------------------- 姊妹技能定位


def find_weekly_dir() -> Path:
    """定位姊妹技能 ai-industry-weekly（候选顺序/探针/env 语义见 _weekly.py）。

    本脚本需要的是 assets/universe.json，因此 require=NEED_UNIVERSE：
    命中一份安装却缺这个文件时报「装了但缺 universe.json」，而不是「找不到姊妹技能」，
    也绝不为了凑齐文件而顺延到另一份安装——那正是「第一步读 A、第二步读 B」的脑裂来源。
    """
    return locate_weekly_skill_or_exit(require=NEED_UNIVERSE)


def _order_sort_key(rec: dict) -> tuple:
    """order 的排序键（类型归一）。

    universe.json 是人手维护的，order 出现 None / 字符串 / 布尔混型是常态。
    直接 `key=lambda r: (r["order"] is None, r["order"])` 会在混型时抛
    `TypeError: '<' not supported between instances of 'str' and 'int'`，
    整个日更第二步当场炸掉。这里一律归一：可比的数字排前面按数值排，
    其余（None/字符串/布尔/缺失）排最后并保持文件原顺序。
    """
    o = rec.get("order")
    if isinstance(o, bool) or not isinstance(o, (int, float)):
        return (1, 0.0, rec["_seq"])
    if math.isnan(o) or math.isinf(o):
        return (1, 0.0, rec["_seq"])
    return (0, float(o), rec["_seq"])


def load_universe(weekly: Path) -> list[dict]:
    """读姊妹技能的 assets/universe.json。

    这份文件由**周更技能**维护、日更只读，所以任何格式问题都必须把用户指回
    universe.json 去改，而不是让人来改日更脚本。四类坏输入（读不了 / 顶层不是
    对象 / 条目不是对象 / order 混型）一律给一行中文错误 + exit 1，绝不放任
    PermissionError、AttributeError、TypeError 冒到顶层兜底打成英文类型名。
    """
    path = weekly / "assets" / "universe.json"
    where = rel_path(path)
    fix = f"请到周更技能 {WEEKLY_DIRNAME} 里修 {where}（日更只读这份文件，改日更脚本没用）。"
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except OSError as exc:            # 含 PermissionError / IsADirectoryError / FileNotFoundError
        err(f"错误：读取 {where} 失败：{scrub(exc)}。{fix}")
        sys.exit(1)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        err(f"错误：{where} 不是合法 JSON：{scrub(exc)}。{fix}")
        sys.exit(1)
    items = data.get("tickers") if isinstance(data, dict) else None
    if not isinstance(items, list) or not items:
        err(f"错误：{where} 缺少非空的 tickers 列表"
            f"（顶层须是对象且含非空 tickers 数组，当前顶层是 {type(data).__name__}）。{fix}")
        sys.exit(1)
    out, skipped = [], 0
    for it in items:
        if not isinstance(it, dict):
            skipped += 1
            continue
        t = str(it.get("ticker") or "").strip()
        if not t:
            skipped += 1
            continue
        out.append({
            "ticker": t,
            "order": it.get("order"),
            "theme": it.get("theme"),
            "layer": it.get("layer"),
            "etf": bool(it.get("etf")),
            "hk_quote": bool(it.get("hk_quote")),
            "currency": it.get("currency") or "USD",
            "_seq": len(out),
        })
    if not out:
        err(f"错误：{where} 里没有可用的 ticker"
            f"（tickers 的每一条都须是含非空 ticker 字段的对象）。{fix}")
        sys.exit(1)
    if skipped:
        # 静默丢票 = 日报少一档还查不出来，所以必须出声（走 stderr，不进报告正文）。
        err(f"警告：{where} 中有 {skipped} 条不是「含非空 ticker 字段的对象」，已跳过；{fix}")
    out.sort(key=_order_sort_key)
    for rec in out:
        rec.pop("_seq", None)
    return out


# ---------------------------------------------------------------- 市场与交易日


def market_of(ticker: str) -> str:
    t = ticker.upper()
    if t.endswith(".HK"):
        return "HK"
    if t.endswith(".KS") or t.endswith(".KQ"):
        return "KR"
    return "US"


def frame_for(raw, ticker):
    """从 yf.download(group_by='ticker') 的结果里取出单只票的 OHLCV，去掉空 bar。"""
    try:
        if isinstance(raw.columns, pd.MultiIndex):
            if ticker not in raw.columns.get_level_values(0):
                return None
            f = raw[ticker]
        else:
            f = raw
    except (KeyError, AttributeError):
        return None
    if f is None or "Close" not in f.columns:
        return None
    f = f.dropna(subset=["Close"])
    return f if len(f) else None


def _prices_source_label(rows) -> str:
    """按本轮各行实际的 bars_source 统计出一句 provenance，绝不写死。"""
    tally = {}
    for r in rows or []:
        src = r.get("bars_source")
        if src:
            tally[src] = tally.get(src, 0) + 1
    if not tally:
        return "N/A（本轮无任何档位产出日线）"
    parts = [f"{src} {n} 档" for src, n in sorted(tally.items(), key=lambda kv: -kv[1])]
    base = "；".join(parts)
    if len(tally) > 1:
        # 不同档不并表（回退链规则第 3 条）：并列陈述，不合成一个来源名
        base += "（**不同档口径不同，逐行看 bars_source，勿合并解读**）"
    return base + "。口径：auto_adjust=False，原始未复权"


def fallback_frames(tickers):
    """用非 Yahoo 档补 yfinance 没给出的标的。返回 (frames, tiers, notes)。

    只在 yfinance 对该标的回空时才调用（第0档优先，见 CLAUDE.md 回退链规则：
    顺序固定并写下来、标注来源、不跨级合并、阈值不随源转移、回退必须响）。

    本函数只做**帧的搬运与格式归一**，指标数学一行都不碰——bars_fallback.py 只回
    bar，RSI/均线/52周高仍由 compute_row 算。两份指标实现对同一个 T1/T2/T3 各说各话，
    是本技能族记录在案的最坏故障（见两份 TH 阈值字典）。

    ⚠ 帧契约（与 yf.download 实测一致，必须逐条对齐，否则会以奇怪的方式炸）：
      * DatetimeIndex，**tz-naive**（yfinance 实测 datetime64[ns]、tz=None）。
        这一条最要命：tz-naive.union(tz-aware) **不会抛**，它回 dtype=object，
        紧接着的 sort_values() 才抛 TypeError，被顶层兜底吞成 **exit 1（参数错误）**——
        一次取数回退会被调度层读成「命令写错了」，正好是 SKILL.md 第二步要防的误判。
      * 升序、唯一；Open/High/Low/Close/Volume 均为 float
    """
    if not tickers:
        return {}, {}, []
    try:
        import bars_fallback
    except ImportError as exc:
        return {}, {}, [f"⚠ 回退档不可用（无法导入 bars_fallback：{scrub(exc)}），"
                        f"{len(tickers)} 个标的保持无数据"]

    notes = []
    try:
        results, meta = bars_fallback.fetch_bars(tickers)
    except Exception as exc:  # noqa: BLE001 - 回退档自身失败只降级，绝不掀掉整轮
        return {}, {}, [f"⚠ 回退档取数抛错（{type(exc).__name__}: {scrub(exc)}），"
                        f"{len(tickers)} 个标的保持无数据"]

    frames, tiers = {}, {}
    for t, r in (results or {}).items():
        if not r.get("rows"):
            continue
        try:
            idx = pd.to_datetime([row[0] for row in r["rows"]])   # tz-naive，别 localize
            f = pd.DataFrame(
                {"Open":   [float(row[1]) for row in r["rows"]],
                 "High":   [float(row[2]) for row in r["rows"]],
                 "Low":    [float(row[3]) for row in r["rows"]],
                 "Close":  [float(row[4]) for row in r["rows"]],
                 "Volume": [float(row[5]) for row in r["rows"]]},
                index=idx,
            ).sort_index()
            f = f[~f.index.duplicated(keep="last")]
        except Exception as exc:  # noqa: BLE001
            notes.append(f"⚠ {t} 回退档数据无法转成日线帧（{scrub(exc)[:120]}），记 N/A")
            continue
        if len(f) == 0:
            continue
        frames[t] = f
        tiers[t] = r.get("tier") or "回退档"

    if frames:
        # 回退必须响（回退链规则第 6 条）：静默降级等于把「不知道」记成「查过了，没事」
        notes.append(
            f"⚠ yfinance 未返回、已由回退档补上 {len(frames)} 档："
            + ", ".join(f"{t}[{tiers[t]}]" for t in sorted(frames))
        )
        # 阈值不随源转移（规则第 4 条）：量口径不同，「放量 ≥1.5x」不可跨档沿用
        notes.append(
            "⚠ 回退档成交量口径与 yfinance 不同（实测 AVGO −11.1%、NVDA −4.6%），"
            "这些行的 量比 不可与 yfinance 标定的「放量 ≥1.5x」阈值比较"
        )
    still = [t for t in tickers if t not in frames]
    if still:
        notes.append("⚠ 回退档也取不到（记 N/A，不估算）：" + ", ".join(still))
    for reason in (meta or {}).get("degraded_reasons", []):
        notes.append(f"⚠ 回退档：{reason}")
    return frames, tiers, notes


def us_early_close(d):
    """美股 13:00 ET 提前收盘日（每年约 3 天）。不是该日 → None。

    为什么非有不可：收盘时间写死 16:00 时，半日市当天 14:00 ET 的一次运行会认为
    「还没收盘」而退到前一日——**丢掉一个真实完整交易日的收盘**。实测 SMH 十年里
    成交量最低的那批日子几乎全是这三种半日市（2016-11-25 比值 0.146、2025-12-24
    0.263、2019-07-03 0.307…），旧的成交量启发式也正是在这些日子误判。两个判定
    栽在同一批日子上，根因都是「当天其实 13:00 就收了」。

    三条规则（NYSE/Nasdaq 惯例）：
      · 感恩节（11 月第 4 个周四）次日
      · 12-24，落在工作日时（周末则非交易日，落在观察假日也非交易日 → 无 bar，无害）
      · 07-03，落在工作日时（同上）
    非交易日本来就没有 bar，所以「是不是交易日」不必在这里判——多判一天不会有副作用。
    """
    if d.weekday() >= 5:
        return None
    if d.month == 11:
        nov1 = dt.date(d.year, 11, 1)
        first_thu = 1 + (3 - nov1.weekday()) % 7
        if d.day == first_thu + 21 + 1:          # 第 4 个周四的次日
            return dt.time(13, 0)
    if (d.month, d.day) in ((12, 24), (7, 3)):
        return dt.time(13, 0)
    return None


def close_time_for(market: str, d):
    """该市场在 d 这一天的实际收盘时间（唯一出处，时钟判定与盘口判定共用）。"""
    meta = MARKETS[market]
    if market == "US":
        early = us_early_close(d)
        if early is not None:
            return early, True
    return meta["close"], False


def resolve_asof(ref_frame, union_index, market: str):
    """按「完整交易日判定」定该市场的数据日期。

    骨架口径（美股）：最新 bar = 今日 且 当日量 < 20日均量*0.5 -> 判为未完成的盘中
    K 线，剔除退到前一日。再叠加 references 的时点规则：运行时点尚未到该市场收盘
    时间时，今日 bar 必然不完整，同样回退。**港股/韩股用各自市场的时区与收盘时间
    单独判定，不随美股基准回退。**
    """
    if union_index is None or len(union_index) == 0:
        return None, ["无可用日线数据"]
    meta = MARKETS[market]
    now = dt.datetime.now(ZoneInfo(meta["tz"]))
    last = union_index[-1]
    notes = []
    incomplete = False
    if last.date() == now.date():
        close_t, is_early = close_time_for(market, now.date())
        if now.time() < close_t:
            incomplete = True
            # ⚠ 前缀是**必须**的：apply_degraded() 只把 ⚠ 开头的 note 转成
            # degraded_reasons，没有它这条回退就只活在 asof_notes 的中文散文里，
            # --json 的 degraded 会是 false——「当日数据被换成了前一日」这件事
            # 对只读 degraded 的下游完全不可见（回退链规则第 6 条：降级必须响亮；
            # JSON 等价规则第 4 条：stderr/正文是冗余频道，不能是唯一频道）。
            notes.append(
                f"⚠ {meta['label']}运行时点（{now:%H:%M} {meta['tz']}）早于收盘 "
                f"{close_t:%H:%M}{'（半日市提前收盘）' if is_early else ''}，"
                f"当日 K 线未完成，按完整交易日规则取前一日"
            )
        elif is_early:
            notes.append(f"{meta['label']}本日为半日市（13:00 ET 收盘），"
                         f"当日 K 线已完整，按当日取数")
    # 註：这里**曾经**还有一条成交量启发式（当日量 < 20 日均量 50% → 判为未完成盘中
    # K 线并回退）。已删除，理由是实测的：
    #   · 它挂在时钟分支的 elif 下，**收盘前永远轮不到它**，所以从来没有当过盘中检测器；
    #   · 收盘后回放 SMH 十年 2491 个完整交易日，它触发 132 次（5.30%），**全是误判**
    #     ——每一次都是一个真实的完整交易日，其中最低的那批正是半日市
    #     （2016-11-25 0.146／2020-12-24 0.246／2025-12-24 0.263／2019-07-03 0.307）；
    #   · 真正的正例次数是 0。
    # 半日市那一类现在由 us_early_close() 正面处理，不再靠成交量反推。
    # 想重新引入类似启发式前，先回放这段历史：把一个真实收盘丢掉，比晚一天拿到它贵。
    if incomplete:
        if len(union_index) < 2:
            return None, notes + ["回退后无可用交易日"]
        return union_index[-2], notes
    return last, notes


def market_session(market: str, union_index, asof_ts, now=None):
    """该市场此刻的**盘口状态**，供报告头「交易状态：<盘后/盘中/休市>」逐字使用。

    这是纯附加的观察层：它**不参与** resolve_asof 的任何判定，也不改动 asof。
    `output-format.md:13` 早就要求报告头写交易状态，但在此之前没有任何脚本产出
    这个字段——写报告的人只能自己看时钟或去 asof_notes 里认中文散文。

    state 的五个取值互斥：
      intraday   最新 bar 就是今天、且此刻已开盘未收盘 → 当日 K 线未完成
      pre_market 最新 bar 就是今天、但此刻早于开盘（yfinance 提前送出的当日 bar）
      post_close 今天有 bar 且已过收盘时间
      pre_open   今天还没有 bar、且此刻早于收盘（开盘前，或数据源尚未落地当日 bar）
      closed     今天没有 bar 且已过收盘时间（假日／该市场今日不交易），或**周末**

    周末一律 closed，不看 bar：改之前周六上午会是 pre_open（今天没有 bar、又早于
    16:00），而 SKILL.md 早就声称周末是 closed。工作日假日仍可能是 pre_open——
    本函数没有交易所日历，那一类由「盘前时段但未取到任何今日盘前成交」的 ⚠ 兜住。

    附加字段（只增不改）：
      premarket_window  仅美股：工作日且 US_PREMARKET_START <= 此刻 < 开盘。窗口内
                        label 一律「盘前」（不论 state 是 pre_market 还是 pre_open——
                        两者在盘前只差 DXY/VIX 有没有提前送当日 bar，与美股盘口无关）。
      premarket_active  None；由 run() 在窗口内取完 1m 后填 True/False
                        （是否取到任何今日盘前成交）。非美股或窗口外恒为 None。

    now 只供测试注入时钟；正式运行恒为 None（= 取此刻）。
    """
    meta = MARKETS[market]
    now = (now.astimezone(ZoneInfo(meta["tz"])) if now is not None
           else dt.datetime.now(ZoneInfo(meta["tz"])))
    last = union_index[-1] if union_index is not None and len(union_index) else None
    has_today = bool(last is not None and last.date() == now.date())
    # 与 resolve_asof 共用同一个收盘时间来源：半日市当天 14:00 ET 是**盘后**，
    # 不是盘中。两处各写各的会让 RUN_MODE 与 asof 在这三天互相矛盾。
    close_t, is_early = close_time_for(market, now.date())
    before_close = now.time() < close_t
    open_t = meta.get("open", dt.time(0, 0))
    before_open = now.time() < open_t
    weekend = now.weekday() >= 5
    if weekend:
        state = "closed"
    elif has_today:
        # 盘前也有当日 bar（yfinance 提前送出），但那不是盘中价。
        state = ("pre_market" if before_open else "intraday") if before_close else "post_close"
    else:
        state = "pre_open" if before_close else "closed"
    premarket_window = bool(market == "US" and not weekend
                            and US_PREMARKET_START <= now.time() < open_t)
    label = {"intraday": "盘中", "pre_market": "盘前", "post_close": "盘后",
             "pre_open": "开盘前", "closed": "休市"}[state]
    if premarket_window:
        label = "盘前"
    asof_date = asof_ts.date().isoformat() if asof_ts is not None else None
    return {
        "state": state,
        "label": label,
        "premarket_window": premarket_window,
        "premarket_active": None,
        "now": now.isoformat(timespec="seconds"),
        "tz": meta["tz"],
        "close_time": close_t.strftime("%H:%M"),
        "early_close": bool(is_early),
        "latest_bar": last.date().isoformat() if last is not None else None,
        # 当日 bar 是否已完结。intraday 时为 False——这正是 asof 回退的原因。
        "bar_complete": state not in ("intraday", "pre_market"),
        "asof": asof_date,
        # asof 是否因当日 bar 未完成而被推回前一日（回退链规则第 2 条：标明档位）
        "rolled_back": bool(state in ("intraday", "pre_market") and last is not None and asof_date
                            and asof_date != last.date().isoformat()),
    }


def attach_live_overlay(row, frame, asof_ts, session):
    """盘中**实时价叠加层**——双轨制的活价那一轨。

    ## 为什么是叠加而不是重算

    `compute_row` 用 `hist = frame.loc[frame.index <= asof]` 切片，把当日未完成的
    那根 bar 排除在外。一旦把 asof 往前挪一天让它进来，它会**同时**进入所有窗口
    ——MA5/10/20/50/60/200、hi20、hi52、vol_ma20、RSI——这不是「拿到实时价」，
    这是用半根 K 线重算所有慢变量。实测（2026-09-14 盘中）那样做的后果：
      · hi20 的 20 根窗口向前滚一格，AVGO 20日高 412.50 → 399.36（−3.19%），
        于是 T2触发价 379.50 → 367.41 —— **盘中把球门自己挪走了**；
      · MA200 在股价下跌 4.8% 的同时 +0.21%（因为窗口同时丢掉了最老那根）；
      · ma_position 在 48 档里翻转 19 档，ma_structure 翻转 6 档；
      · 量比恒 <1（半个交易日的累计量除以整日均量），实测 37 档美股最大 1.065、
        中位 0.504，「放量 ≥1.5x」**一次都不可能触发** —— 风险标签的静默漏报。
    而且这三类偏差里有两类同向指向「没有买点」：hi52/hi20 在盘中是各自结算值的
    **下界**，T1/T2触发价被系统性压低，`close <= t*_price` 更难成立。对一个专门
    抓回调的例程，天真重算会让盘中运行**错过买点**而外观完全正常。

    所以：窗口型的量一律冻结在最近完整交易日（hi52/hi20/T1价/T2价/各均线/量比），
    只有**到这些阈值的距离**换成活价。那一个减法就是用户要的全部。

    ## 触发用的是另一套名字

    盘中触及写进 `t1_touch_live` / `t2_touch_live` / `t3_touch_live`，**绝不**写回
    `t1`/`t2`/`t3`/`triggered`。那四个字段的口径是「最近完整交易日收盘」，
    `output-format.md:89`「绝不谎报新收盘数据或制造新触发」、`perp-overnight.md:21`
    「T1/T2/T3 永远只用最近一个完整交易日收盘价计算」、`buckets.md` 三个触发全定义
    在收盘上。活价触及是**未确认**的：实测 AVGO 的 Wilder RSI 在同一个交易日里
    在 34.0–36.0 之间来回穿越 T3 的 35.0 阈值，本次研究自己的两次探测（11:16 读
    34.4 触发、11:25 读 35.0 不触发）就互相矛盾。
    """
    row["bar_complete"] = True
    row["live_kind"] = "intraday"
    if frame is None or asof_ts is None:
        return row
    later = frame.loc[frame.index > asof_ts]
    if len(later) == 0:
        # 该档的日线来自回退档时最常见：实测 stockanalysis 在 11:20 ET 仍只到前一日，
        # 韩股档甚至按设计丢弃当日未完成 bar。没有活价就记 null + 理由，不拿别处凑。
        row["live_price"] = None
        row["live_unavailable_reason"] = (
            f"该档日线源（{row.get('bars_source') or 'yfinance'}）未提供当日未完成 bar")
        return row
    bar = later.iloc[-1]
    live = float(bar["Close"]) if is_num(bar.get("Close")) else None
    if not is_num(live):
        row["live_price"] = None
        row["live_unavailable_reason"] = "当日未完成 bar 的 Close 非数值"
        return row

    row["bar_complete"] = False
    row["live_price"] = rnd(live, 4)
    row["live_bar_date"] = later.index[-1].date().isoformat()
    row["live_ts"] = session.get("now")
    # 与历史同一个 series 对象，口径天然一致（拆股已还原、股息未复权）。
    # 这是唯一能这么说的源：换任何外部实时源都要过回退链规则第 3、4 条。
    row["live_source"] = "yfinance 当日未完成日线 bar（与历史同一 series，口径一致）"
    row["live_price_caliber"] = "拆股已还原·股息未复权（同 close/hi52/hi20）"
    _live_metrics(row, live, LIVE_TEXT["intraday"])
    return row


# 两种活价轨的措辞。数字口径完全相同（同一组 *_live 字段、同一套冻结阈值），只有
# 「盘中」「盘前」两个字与各自不可判定的成因不同——所以措辞集中在这里，行级字段、
# 顶层 live 块与人读分支都从这一份取，改一处即全改。
LIVE_TEXT = {
    "intraday": {
        "t3_reason": "RSI(14) 定义在收盘价上，盘中无收盘 → 不判定（null，非 false）",
        "vol_reason": ("盘中累计量 ÷ 20日整日均量 结构性 <1（实测中位 0.504、最大 1.065），"
                       "「放量 ≥1.5x」在盘中不可达；按已过时间折算全日属「用不完整数据推算」，禁止"),
        "threshold_reason": "T1/T2/T3 阈值标定在完整交易日收盘上，活价不得与之比较后记为已触发",
        "reclaimed_title": "↩ 盘中已收回 · 未确认",
        "reclaimed_caveat": ("最近完整交易日收盘已触发、此刻盘中活价回到触发价上方——"
                             "**收盘触发与分桶不因此撤销**，须今日收盘确认"),
        "prohibitions": [
            "盘中活价不得写入任何以「收盘」为名的字段（close/prev_close/spot_close）",
            "不得据此判定 T1/T2/T3 新触发、不得改变分桶",
            "不得改变「✅ 未触发」清单——该清单口径为完整交易日收盘",
            "hi52/hi20/T1触发价/T2触发价/各均线/量比 一律为最近完整交易日之值，盘中不重算",
        ],
    },
    "premarket": {
        "t3_reason": "RSI(14) 定义在收盘价上，盘前无收盘 → 不判定（null，非 false）",
        "vol_reason": ("盘前成交量 Yahoo 1m 报 0／不可得，且盘前非整日 → 量比不可得（null）；"
                       "按已过时间折算全日属「用不完整数据推算」，禁止"),
        "threshold_reason": "T1/T2/T3 标定在完整交易日收盘上，盘前成交稀薄且非收盘",
        "reclaimed_title": "↩ 盘前已收回 · 未确认",
        "reclaimed_caveat": ("最近完整交易日收盘已触发、此刻盘前价回到触发价上方——"
                             "**收盘触发与分桶不因此撤销**，须今日收盘确认"),
        "prohibitions": [
            "盘前价不得写入任何以「收盘」为名的字段（close/prev_close/spot_close）",
            "不得据此判定 T1/T2/T3 新触发、不得改变分桶",
            "不得改变「✅ 未触发」清单——该清单口径为完整交易日收盘",
            "hi52/hi20/T1触发价/T2触发价/各均线/量比 一律为最近完整交易日之值，盘前不重算",
        ],
    },
}


def _live_metrics(row, live, text):
    """活价轨的公共数学：活价 vs **冻结**的窗口型阈值。盘中与盘前共用这一份。

    分子换成活价，分母一律不动（hi52/hi20/T1价/T2价/均线/前收都是最近完整交易日之值）。
    两份实现各算各的，就是本技能族记录在案的最坏故障（两处规则静默分叉）。
    """
    close = row.get("close")
    # 活价 vs **冻结**的窗口型阈值 —— 分子换成活价，分母一律不动
    for name, ref in (("dd52_pct_live", row.get("hi52")), ("dd20_pct_live", row.get("hi20"))):
        row[name] = rnd(pct_from(live, ref), 3) if is_num(ref) else None
    for name, ref in (("gap_to_t1_pct_live", row.get("t1_price")),
                      ("gap_to_t2_pct_live", row.get("t2_price"))):
        row[name] = rnd(pct_from(ref, live), 2) if is_num(ref) else None
    for name, ref in (("vs_ma50_pct_live", row.get("ma50")),
                      ("vs_ma200_pct_live", row.get("ma200"))):
        row[name] = rnd(pct_from(live, ref), 2) if is_num(ref) else None
    # 「今日至今%」不是「日涨跌%」：分母是前一完整交易日收盘，但分子是半天。
    # 两者不是同一个被测量，任何按整日涨跌标定的闸门都不得拿它比。
    row["live_chg_from_close_pct"] = rnd(pct_from(live, close), 3) if is_num(close) else None

    # 双轨的活价轨：独立命名，独立计数，不进桶
    t1p, t2p = row.get("t1_price"), row.get("t2_price")
    row["t1_touch_live"] = bool(live <= t1p) if is_num(t1p) else None
    row["t2_touch_live"] = bool(live <= t2p) if is_num(t2p) else None
    # T3 在盘中/盘前**没有被测量**：RSI(14) 定义在收盘上，此刻没有收盘。
    # null 不是 false（JSON 等价规则第 2 条）。数值另以观察值给出。
    row["t3_touch_live"] = None
    row["t3_touch_live_reason"] = text["t3_reason"]
    touches = [row["t1_touch_live"], row["t2_touch_live"]]
    row["touched_live"] = True if any(v is True for v in touches) else (
        False if all(v is False for v in touches) else None)
    # t*_touch_live 只问「此刻在不在触发价下方」，不问「是不是收盘**之后**才越过的」：
    # 收盘已触发、此刻仍在下方的档它也记 True（实测 2026-11-11 盘前 NVDA 收盘已触发 T1+T2，
    # 被列进「🔶 盘前触及」，等于把已确认的触发说成新的、未确认的）；而收盘已触发、此刻
    # 回到触发价上方的档（同日 TSM：收盘 396.48 ≤ T1@414.35，盘前 417.80）它根本不标。
    # 所以另给两组：新触及 = 此刻在下方 且 收盘**未**触发；已收回 = 收盘已触发 且 此刻在上方。
    # 收盘侧读 finalize_triggers 的 t1/t2（口径＝最近完整交易日收盘），任一侧 null → null。
    # t*_touch_live/touched_live 原样保留（兼容），🔶 清单只从新触及字段取。
    for k, p in (("t1", t1p), ("t2", t2p)):
        at_close, touch = row.get(k), row[f"{k}_touch_live"]
        row[f"{k}_new_touch_live"] = (None if touch is None or at_close is None
                                      else bool(touch and at_close is not True))
        row[f"{k}_reclaimed_live"] = (None if at_close is None or not is_num(p)
                                      else bool(at_close is True and live > p))
    for agg, keys in (("new_touched_live", ("t1_new_touch_live", "t2_new_touch_live")),
                      ("reclaimed_live", ("t1_reclaimed_live", "t2_reclaimed_live"))):
        vals = [row[x] for x in keys]
        row[agg] = True if any(v is True for v in vals) else (
            False if all(v is False for v in vals) else None)

    # 量比在盘中不可得：半个交易日的累计量 ÷ 整日均量，结构性 <1；盘前 Yahoo 1m 量报 0。
    # **不得**按已过时间折算成全日预估——那是「用不完整数据推算」，回退链规则第 5 条禁止。
    row["vol_ratio_live"] = None
    row["vol_ratio_live_reason"] = text["vol_reason"]

    row["threshold_comparable"] = {"value": False, "reason": text["threshold_reason"]}
    row["counts_toward"] = {"numerator": False, "denominator": False}
    row["prohibitions"] = list(text["prohibitions"])
    return row


def attach_premarket_overlay(row, pm, session):
    """美股**盘前叠加层**——活价轨的盘前版本，口径与盘中叠加层逐字段同名。

    为什么不能复用 attach_live_overlay 的价源：它读的是日线帧里 asof 之后那根 bar，
    而盘前个股的当日日线 bar 实测全是 NaN（frame_for 丢掉），盘前根本没有那一根。
    盘前价只能来自 1m prepost=True（fetch_us_premarket）。

    与盘中层相同：T1/T2/T3、分桶、triggered/untriggered 一律不动；只有「到冻结阈值
    的距离」换成盘前价，盘前触及进 t1_touch_live/t2_touch_live（未确认）。
    不同处：live_ts 是**那笔成交的 1m bar 时间戳**（不是运行时刻）——盘前成交稀薄，
    最后一笔可能是 04:0x 的，读者必须看得到它有多旧（live_age_min）。

    跨档不混用（回退链规则第 3、4 条）：该档日线来自回退档时，盘前价来自 Yahoo、
    阈值来自 stockanalysis/Naver，两档相减就是把两个来源拼成一个读数 → N/A。
    """
    row["bar_complete"] = True
    row["live_kind"] = "premarket"
    if not premarket_eligible(row.get("ticker")):
        # 根本没进请求清单（指数/期货/外盘挂牌）：写「尚无成交」就是把「没去要」说成「要了没有」
        t = str(row.get("ticker") or "").strip().upper()
        row["live_price"] = None
        row["live_unavailable_reason"] = (
            PREMARKET_FOREIGN_REASON if "." in t and t not in INDEX_TICKERS
            else PREMARKET_INELIGIBLE_REASON)
        return row
    src = row.get("bars_source") or "yfinance"
    if src != "yfinance":
        row["live_price"] = None
        row["live_unavailable_reason"] = (
            f"该档日线来自回退档 {src}，盘前价来自 Yahoo——跨档不混用（回退链规则第 3、4 条）")
        return row
    if not pm or not is_num(pm.get("premarket_px")):
        row["live_price"] = None
        row["live_unavailable_reason"] = "该档今日盘前尚无成交"
        return row
    live = float(pm["premarket_px"])
    row["bar_complete"] = False
    row["live_price"] = rnd(live, 4)
    row["live_bar_date"] = str(pm.get("premarket_ts") or "")[:10] or None
    row["live_ts"] = pm.get("premarket_ts")
    row["live_age_min"] = pm.get("premarket_age_min")
    row["live_bars"] = pm.get("premarket_bars")
    row["live_source"] = PREMARKET_SOURCE
    row["live_price_caliber"] = PREMARKET_CALIBER
    _live_metrics(row, live, LIVE_TEXT["premarket"])
    return row


# ---------------------------------------------------------------- 指标


def wilder_rsi(close, n=RSI_N):
    """RSI(14) Wilder 平滑 —— 骨架原样保留，勿改成简单均值版。"""
    d = close.diff()
    up = d.clip(lower=0)
    dn = -d.clip(upper=0)
    ru = up.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    rd = dn.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    return 100 - 100 / (1 + ru / rd)


def sma_tail(series, n):
    s = series.tail(n)
    return float(s.mean()) if len(s) == n else None


def pct_from(close, ref):
    """回撤% = (收盘 - 区间高) / 区间高 * 100；也用于价相对均线位置%。"""
    if not is_num(close) or not is_num(ref) or ref == 0:
        return None
    return (close - ref) / ref * 100.0


def ma_structure(ma5, ma10, ma20, ma60):
    vals = [ma5, ma10, ma20, ma60]
    if not all(is_num(v) for v in vals):
        return "N/A"
    if ma5 > ma10 > ma20 > ma60:
        return "多头排列"
    if ma5 < ma10 < ma20 < ma60:
        return "空头排列"
    return "纠缠/转折"


def ma_position(close, ma50, ma200):
    parts = []
    parts.append(("N/A(200DMA)" if not is_num(ma200)
                  else (">200DMA" if close > ma200 else "<200DMA")))
    parts.append(("N/A(50DMA)" if not is_num(ma50)
                  else (">50DMA" if close > ma50 else "<50DMA")))
    return ",".join(parts)


def pick_next_earnings(ed, asof_date):
    """从已取回的财报日历里挑「该标的自己的数据日当天或之后」最近的一个。

    口径与并发化之前逐字一致：只是把「拉」和「挑」拆开——拉是网络往返（可以并发），
    挑依赖该标的**自己**的最后一根 K 线日期（只有 compute_row 里才知道），所以留在串行。
    取不到 / 没有未来日期一律 None（N/A，不估算）。
    """
    if ed is None or len(ed) == 0:
        return None
    try:
        idx = ed.index
        if getattr(idx, "tz", None) is not None:
            idx = idx.tz_localize(None)
        future = sorted(d.date() for d in idx if d.date() >= asof_date)
    except Exception:
        return None
    return future[0].isoformat() if future else None


def prefetch_earnings(yf, tickers, sess=None):
    """并发预取各标的的财报日历，返回 ({ticker: 日历或 None}, 统计 dict)。

    为什么可以并发：yfinance 的 YfData 是**进程级单例**（metaclass 上带 threading.Lock，
    crumb/cookie 由 _cookie_lock 守着，一个 session 一份 cookie 由所有线程共用），
    `_set_session(None)` 是显式 no-op，所以这里不存在 crumb 竞态。

    ⚠ 2026-09-11 修正：这条调用**原本刻意不传 session**，理由写的是「走 curl_cffi 的
    浏览器指纹 TLS，不是被 Yahoo 掐得最狠的裸 requests.Session」。该理由**已被实测推翻**
    ——它只说对了一半：裸 requests.Session 确实会被 Yahoo 按 UA 限流（429），但 curl_cffi
    在本执行环境的出站代理上**连 TLS 隧道都握不完**，比被限流更早死。正确解是
    requests.Session **加浏览器 UA**，两层一起绕开。矩阵见 make_session() 上方。
    共用一个 Session 是有意的：cookie/crumb 的写入由 yfinance 自己的 _cookie_lock 守，
    连接池由 urllib3 管，6 个线程共用一份 cookie 正是上面那段单例语义要的效果。

    并发只做财报日这一件事：港股 hk_quote.py 的 subprocess 路由绝不并发。

    单只失败只把这一只降级成 N/A（不估算），绝不掀掉整轮、也绝不把异常漏到顶层；
    但**必须计数**——线程池整片失败正是 Yahoo 限流的样子，静默吞掉就等于把「不知道」
    记成「查过了，没有」。计数结果进 degraded / degraded_reasons 与 asof_notes。
    """
    cals = {}
    stats = {"attempted": len(tickers), "ok": 0, "empty": 0, "failed": 0, "errors": []}
    if not tickers:
        return cals, stats
    workers = max(1, min(EARNINGS_MAX_WORKERS, len(tickers)))

    def one(t):
        try:
            if sess is not None and _SESSION_OK:
                try:
                    tk = yf.Ticker(t, session=sess)
                except TypeError as exc:
                    if "session" not in str(exc):
                        raise          # 无关的 TypeError 交给外层按单只失败计数
                    _session_unsupported("Ticker")
                    tk = yf.Ticker(t)
            else:
                tk = yf.Ticker(t)
            return t, tk.get_earnings_dates(), None
        except Exception as exc:  # noqa: BLE001 - 单只失败只降级这一只，异常绝不外泄
            return t, None, f"{type(exc).__name__}: {scrub(exc)}"

    with ThreadPoolExecutor(max_workers=workers) as pool:
        # pool.map 按入参顺序回吐结果；本函数只往字典里填，行序仍由 selected 决定，
        # 并发不参与任何排序。
        for t, cal, exc_text in pool.map(one, tickers):
            cals[t] = cal
            if exc_text is not None:
                stats["failed"] += 1
                stats["errors"].append(f"{t}: {exc_text}")
            elif cal is None or len(cal) == 0:
                stats["empty"] += 1
            else:
                stats["ok"] += 1
    return cals, stats


def earnings_block(stats, requested):
    """把预取统计整理成 --json 的 earnings 块 + 需要出声的告警文案。"""
    attempted = stats["attempted"]
    failed, empty = stats["failed"], stats["empty"]
    missing = failed + empty
    big_enough = attempted >= EARNINGS_THROTTLE_MIN_N
    over = lambda n: n >= attempted * EARNINGS_THROTTLE_RATIO  # noqa: E731
    # 限流嫌疑由 failed 主导（见档头 EARNINGS_THROTTLE_RATIO 处的注解）：
    #   failed 自己过线                    → 限流嫌疑
    #   failed > 0 且 failed+empty 过线    → 限流嫌疑（限流部分表现为回空而不抛）
    #   failed == 0                        → **绝不**报限流，不论多少 empty
    if not big_enough:
        throttle, throttle_reason = False, None
    elif over(failed):
        throttle = True
        throttle_reason = f"{failed}/{attempted} 只抛出异常，已达 1/3 线"
    elif failed and over(missing):
        throttle = True
        throttle_reason = (f"{failed}/{attempted} 只抛出异常，加上 {empty} 只回空"
                           f"共 {missing}/{attempted} 达 1/3 线（限流可能部分表现为回空）")
    else:
        throttle, throttle_reason = False, None
    warnings = []
    if failed:
        warnings.append(
            f"⚠ 下次财报日并发查询有 {failed}/{attempted} 只异常失败"
            f"（记 N/A，不估算）"
        )
    if throttle:
        warnings.append(
            f"⚠ 下次财报日疑似 Yahoo 限流（并发 {EARNINGS_MAX_WORKERS}）：{throttle_reason}；"
            f"这些标的的下次财报记 N/A，不估算"
        )
    elif big_enough and not failed and over(empty):
        # 以前这一支会喊限流。改成如实陈述：呼叫都成功了，只是这些标的没有财报日历
        # ——港股/韩股/ADR 长期如此。喊限流会让报告把一个常态讲成取数事故。
        warnings.append(
            f"ℹ 下次财报日有 {empty}/{attempted} 只回空但**无任何异常**：这些标的多半"
            f"本就没有财报日历（港股/韩股/ADR 常态），记 N/A，不估算；不判为限流"
        )
    block = {
        "requested": bool(requested),
        "concurrent": bool(requested and attempted),
        "max_workers": EARNINGS_MAX_WORKERS,
        "attempted": attempted,
        "ok": stats["ok"],
        "empty": stats["empty"],
        "failed": stats["failed"],
        "missing": missing,
        "errors": stats["errors"],
        "throttle_suspected": bool(throttle),
        # JSON 等价律：判据也要是栏位。未判为限流时是 null 而非空字串——
        # null 表示「没有这个结论」，空字串会被读成「有结论但没写理由」。
        "throttle_reason": throttle_reason,
        # empty 与 failed 分开出栏，呼叫端才不必再犯一次把两者相加的错。
        "empty_without_error": int(empty) if not failed else None,
        "note": ("--no-earnings：本轮跳过下次财报日查询，全部记 N/A（不估算）"
                 if not requested else
                 "下次财报日取不到即 N/A，不估算；ETF 与指数代码本就不查"),
        "warnings": warnings,
    }
    return block, warnings


# ---------------------------------------------------------------- 港股覆写


def hk_key(code: str) -> str:
    """港股代码归一化。

    universe.json 用 4 位（0700.HK），hk_quote.py 回吐 5 位零填充（00700.HK）。
    不归一化就永远匹配不上、静默退回 yfinance 复权口径 —— 这正是本脚本要避免的坑。
    """
    c = str(code).strip().upper().replace("HK.", "").replace(".HK", "")
    return c.lstrip("0") or "0"


def hk_overlay(weekly: Path, codes: list[str]) -> tuple[dict, list[str]]:
    """调姊妹技能的 hk_quote.py 取港股权威价（原始未复权、实时）。

    只取 收盘价/涨跌幅/成交量/52周高低；MA/RSI/20日高 仍由 yfinance 未复权日线补。
    """
    script = weekly / "scripts" / "hk_quote.py"
    shown = f"python3 {rel_path(script)} {' '.join(codes)} --json"
    if not script.is_file():
        return {}, [f"⚠ 找不到 {rel_path(script)}，港股价格回退 yfinance 未复权日线（口径次优）"]
    cmd = [sys.executable, str(script), *codes, "--json"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {}, [f"⚠ `{shown}` 执行失败（{scrub(exc)}），港股价格回退 yfinance 未复权日线"]
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-1:] or [""]
        return {}, [f"⚠ `{shown}` 退出码 {proc.returncode}：{tail[0]}，港股价格回退 yfinance"]
    try:
        items = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {}, [f"⚠ `{shown}` 输出不是合法 JSON，港股价格回退 yfinance"]
    out, notes = {}, []
    for it in items:
        code = (it.get("code") or "").upper()
        if not code:
            continue
        if it.get("error"):
            notes.append(f"⚠ {code} hk_quote 取数失败（{it['error']}），回退 yfinance")
            continue
        out[hk_key(code)] = it
        if it.get("stale"):
            notes.append(f"⚠ {code} hk_quote stale=true（报价过时），不作收盘价使用，回退 yfinance")
        elif not str(it.get("market_status", "")).startswith("已收盘"):
            notes.append(
                f"⚠ {code} hk_quote market_status={it.get('market_status')}（非收盘价），回退 yfinance 日线"
            )
    return out, notes


def usable_hk(item) -> bool:
    return bool(item) and not item.get("stale") and str(item.get("market_status", "")).startswith("已收盘")


# ---------------------------------------------------------------- 单标的计算


def compute_row(meta, frame, asof, market, earnings_cals, want_earnings):
    """在该标的自己的取数日期上算全部指标。缺失一律 None（= N/A）。

    earnings_cals 是 run() 里并发预取好的 {ticker: 财报日历}；本函数只负责在该标的
    自己的数据日上挑下一个财报日，不再自己发网络请求。
    """
    ticker = meta["ticker"]
    row = {
        "ticker": ticker,
        "market": market,
        "market_label": MARKETS[market]["label"],
        "theme": meta.get("theme"),
        "layer": meta.get("layer"),
        "etf": meta.get("etf", False),
        "currency": meta.get("currency", "USD"),
        "asof": None,
        "price_source": "yfinance(auto_adjust=False)",
        "insufficient_history": False,
        "source_unavailable": False,
        "data_lag": False,
        "notes": [],
    }
    if frame is None or asof is None:
        # ⚪️「取不到」与 ❌「上市太新」是两件事，绝不能塌进同一个桶：
        # 前者读起来像「等满半年就好」，真相却是「所有档位都没拿到数，需要查」。
        # insufficient_history 保持 True 以维持下游分桶行为不变，但另立一个旗标，
        # 让报告能把两者分开写（这正是本仓库 ⚪️/❌ 语义塌缩要防的事）。
        row["notes"].append("无日线数据（yfinance 与回退档均未返回——是取数失败，不是上市太新）")
        row["insufficient_history"] = True
        row["source_unavailable"] = True
        row["bars"] = 0
        return row

    hist = frame.loc[frame.index <= asof]
    if len(hist) == 0:
        row["notes"].append("该标的在市场数据日期前无任何 K 线")
        row["insufficient_history"] = True
        row["bars"] = 0
        return row

    d = hist.index[-1]
    row["asof"] = d.date().isoformat()
    row["bars"] = int(len(hist))
    if d != asof:
        row["data_lag"] = True
        row["notes"].append(f"数据滞后：市场数据日 {asof.date().isoformat()}，该标的最新 K 线 {row['asof']}")

    if len(hist) < MIN_HISTORY_BARS:
        row["insufficient_history"] = True
        row["close"] = rnd(float(hist["Close"].iloc[-1]), 4)
        row["notes"].append(
            f"历史不足（仅 {len(hist)} 根日线 < {MIN_HISTORY_BARS}），"
            "RSI/均线/52周高失真，不参与技术面判定"
        )
        return row

    close = float(hist["Close"].iloc[-1])
    prev = float(hist["Close"].iloc[-2]) if len(hist) >= 2 else None
    volume = float(hist["Volume"].iloc[-1]) if "Volume" in hist.columns else None
    vol_ma20 = sma_tail(hist["Volume"], LOOKBACK_20D) if "Volume" in hist.columns else None

    high = hist["High"]
    hi52_win = high.tail(LOOKBACK_52W)
    hi52 = float(hi52_win.max()) if len(hi52_win) else None
    hi20_win = high.tail(LOOKBACK_20D)
    hi20 = float(hi20_win.max()) if len(hi20_win) == LOOKBACK_20D else None
    if hi20 is None:
        row["notes"].append("20日高数据不足，T2/T2触发价/20D回撤% 一律 N/A，暂不判定")

    rsi_series = wilder_rsi(hist["Close"])
    rsi = float(rsi_series.iloc[-1]) if len(rsi_series) and not math.isnan(float(rsi_series.iloc[-1])) else None

    ma = {n: sma_tail(hist["Close"], n) for n in (5, 10, 20, 50, 60, 200)}

    row.update({
        "close": rnd(close, 4),
        "prev_close": rnd(prev, 4),
        "chg_pct": rnd(pct_from(close, prev), 3),
        "volume": rnd(volume, 0),
        "vol_ma20": rnd(vol_ma20, 0),
        "vol_ratio": rnd(volume / vol_ma20, 3) if is_num(volume) and is_num(vol_ma20) and vol_ma20 else None,
        "hi52": rnd(hi52, 4),
        "hi52_bars": int(len(hi52_win)),
        "hi20": rnd(hi20, 4),
        "rsi14": rnd(rsi, 2),
        "ma5": rnd(ma[5], 4), "ma10": rnd(ma[10], 4), "ma20": rnd(ma[20], 4),
        "ma50": rnd(ma[50], 4), "ma60": rnd(ma[60], 4), "ma200": rnd(ma[200], 4),
    })
    if len(hi52_win) < LOOKBACK_52W:
        row["notes"].append(f"52周高仅由 {len(hi52_win)} 根 K 线算得（不足 {LOOKBACK_52W}）")

    if want_earnings and not meta.get("etf") and not ticker.startswith("^"):
        row["next_earnings"] = pick_next_earnings((earnings_cals or {}).get(ticker), d.date())
    else:
        row["next_earnings"] = None
    return row


def finalize_triggers(row):
    """由 收盘/52周高/20日高/RSI 推 T1/T2/T3 与触发价。港股覆写后须重跑本函数。"""
    if row.get("insufficient_history"):
        row.update({"t1": None, "t2": None, "t3": None, "triggered": None})
        return row
    close = row.get("close")
    hi52, hi20, rsi = row.get("hi52"), row.get("hi20"), row.get("rsi14")

    row["t1_price"] = rnd(hi52 * T1_RATIO, 4) if is_num(hi52) else None
    row["t2_price"] = rnd(hi20 * T2_RATIO, 4) if is_num(hi20) else None
    row["dd52_pct"] = rnd(pct_from(close, hi52), 3)
    row["dd20_pct"] = rnd(pct_from(close, hi20), 3)

    row["t1"] = bool(close <= row["t1_price"]) if is_num(close) and is_num(row["t1_price"]) else None
    row["t2"] = bool(close <= row["t2_price"]) if is_num(close) and is_num(row["t2_price"]) else None
    row["t3"] = bool(rsi <= T3_RSI) if is_num(rsi) else None

    # 距触发阈值的缺口%（负值 = 还要再跌这么多才触及），供「✅ 未触发」表备注用
    row["gap_to_t1_pct"] = rnd(pct_from(row["t1_price"], close), 2) if is_num(row.get("t1_price")) else None
    row["gap_to_t2_pct"] = rnd(pct_from(row["t2_price"], close), 2) if is_num(row.get("t2_price")) else None

    fired = [row["t1"], row["t2"], row["t3"]]
    row["triggered"] = True if any(v is True for v in fired) else False
    row["ma_structure"] = ma_structure(row.get("ma5"), row.get("ma10"), row.get("ma20"), row.get("ma60"))
    row["ma_position"] = ma_position(close, row.get("ma50"), row.get("ma200")) if is_num(close) else "N/A"
    row["vs_ma50_pct"] = rnd(pct_from(close, row.get("ma50")), 2)
    row["vs_ma200_pct"] = rnd(pct_from(close, row.get("ma200")), 2)
    return row


def apply_hk(row, q):
    """hk_quote.py 的数字覆写 yfinance —— 冲突时以 hk_quote 为准（硬约束）。"""
    row["price_source"] = "hk_quote.py（腾讯实时·原始未复权）；MA/RSI/20日高 = yfinance 未复权日线"
    if is_num(q.get("last")):
        row["close"] = rnd(float(q["last"]), 4)
    if is_num(q.get("chg_pct")):
        row["chg_pct"] = rnd(float(q["chg_pct"]), 3)
    if is_num(q.get("volume")):
        row["volume"] = rnd(float(q["volume"]), 0)
        row["vol_ratio_source"] = "量比分母(20日均量)仍取 yfinance，与 hk_quote 成交量口径可能不同"
    if is_num(q.get("hi52")):
        row["hi52"] = rnd(float(q["hi52"]), 4)
        row["hi52_source"] = q.get("hi52_source")
    if is_num(q.get("lo52")):
        row["lo52"] = rnd(float(q["lo52"]), 4)
    qt = q.get("quote_time")
    if qt:
        row["quote_time"] = qt
        # 覆写前先留下**指标**那一侧的日期。港股这一行本来就是两个口径拼起来的：
        # 价格/52周高来自 hk_quote（腾讯·原始未复权），MA/RSI/20日高 仍来自 yfinance
        # 日线——两者可以落在不同日期（data-acquisition.md 的〔编者注〕已承认这点）。
        # 只留一个 asof 会让报告头与该行各说各话：2026-09-14 实测头部印「港股 09-11」
        # 而三行用的都是 09-14 的价。
        row["asof_indicators"] = row.get("asof")
        row["asof"] = qt.split(" ")[0]
        if row["asof_indicators"] and row["asof_indicators"] != row["asof"]:
            row["asof_split"] = True
            row["notes"].append(
                f"价格口径日期 {row['asof']}（hk_quote 收盘）与指标口径日期 "
                f"{row['asof_indicators']}（yfinance 日线：MA/RSI/20日高）不同日；"
                f"T1 用 52周高(hk_quote)、T2/RSI 用 yfinance —— 两者不得当成同一天的读数")
    row["market_status"] = q.get("market_status")
    if is_num(row.get("volume")) and is_num(row.get("vol_ma20")) and row["vol_ma20"]:
        row["vol_ratio"] = rnd(row["volume"] / row["vol_ma20"], 3)
    return row


# ---------------------------------------------------------------- 宏观


def tnx_unit_factor(raw_close):
    """^TNX 单位侦测：yfinance 有时给 % (4.15)、有时给 %x10 (41.5)。

    10Y 合理区间约 0.5–8%。落在区间内 -> 已是百分比；落在 8–80 -> 除以 10 后才合理。
    两者都不成立时返回 None（记 ⚪ 数据不足，不臆测）。
    """
    if not is_num(raw_close):
        return None, "N/A"
    if TNX_MIN_PCT <= raw_close <= TNX_MAX_PCT:
        return 1.0, f"原始 Close={raw_close:.4g} 已是百分比，未换算"
    if TNX_MAX_PCT < raw_close <= TNX_MAX_PCT * 10:
        return 0.1, f"原始 Close={raw_close:.4g} 为 %x10，已除以 10"
    return None, f"原始 Close={raw_close:.4g} 落在 10Y 合理区间之外，单位无法判定，记 ⚪ 不臆测"


def rate_signal(chg_bp, chg5_bp):
    if not is_num(chg_bp) and not is_num(chg5_bp):
        return "⚪利率数据不足"
    up = (is_num(chg_bp) and chg_bp >= RATE_UP_1D_BP) or (is_num(chg5_bp) and chg5_bp >= RATE_UP_5D_BP)
    dn = (is_num(chg_bp) and chg_bp <= RATE_DN_1D_BP) or (is_num(chg5_bp) and chg5_bp <= RATE_DN_5D_BP)
    if up and not dn:
        return "🔺利率上行"
    if dn and not up:
        return "🔻利率下行"
    return "➖利率平稳"


def series_at(frame, asof, col="Close"):
    if frame is None or asof is None:
        return None, None
    s = frame.loc[frame.index <= asof, col].dropna()
    if len(s) == 0:
        return None, None
    cur = float(s.iloc[-1])
    prev = float(s.iloc[-2]) if len(s) >= 2 else None
    return cur, prev


def build_macro(frames, asof_us):
    macro = {"asof": asof_us.date().isoformat() if asof_us is not None else None, "notes": []}

    # --- 10Y (^TNX)
    tnx = frames.get(TNX_TICKER)
    tnx_block = {"ticker": TNX_TICKER, "close_pct": None, "chg_bp": None, "chg5_bp": None,
                 "unit_note": "N/A", "signal": "⚪利率数据不足"}
    if tnx is not None and asof_us is not None:
        s = tnx.loc[tnx.index <= asof_us, "Close"].dropna()
        if len(s):
            factor, note = tnx_unit_factor(float(s.iloc[-1]))
            tnx_block["unit_note"] = note
            if factor is not None:
                y = s * factor
                tnx_block["close_pct"] = rnd(float(y.iloc[-1]), 4)
                if len(y) >= 2:
                    tnx_block["chg_bp"] = rnd((float(y.iloc[-1]) - float(y.iloc[-2])) * 100, 2)
                if len(y) >= 6:
                    tnx_block["chg5_bp"] = rnd((float(y.iloc[-1]) - float(y.iloc[-6])) * 100, 2)
                tnx_block["asof"] = s.index[-1].date().isoformat()
    tnx_block["signal"] = rate_signal(tnx_block["chg_bp"], tnx_block["chg5_bp"])
    # 人读分支把这条口径打在「折现率信号：」那行后面，--json 必须同样带上。
    # 负号用 U+2212（−）而不是 ASCII '-'，与人读那一行逐字一致。
    tnx_block["signal_definition"] = (
        f"日≥+{RATE_UP_1D_BP:.0f}bp 或 5日≥+{RATE_UP_5D_BP:.0f}bp → 🔺；"
        f"日≤−{abs(RATE_DN_1D_BP):.0f}bp 或 5日≤−{abs(RATE_DN_5D_BP):.0f}bp → 🔻；其余 ➖；缺数据 ⚪"
    )
    macro["us10y"] = tnx_block

    # --- DXY
    dxy_cur, dxy_prev = series_at(frames.get(DXY_TICKER), asof_us)
    macro["dxy"] = {"ticker": DXY_TICKER, "close": rnd(dxy_cur, 4),
                    "chg_pct": rnd(pct_from(dxy_cur, dxy_prev), 3),
                    "note": "美元指数（影响非美计价标的）"}

    # --- 大盘背景 + 板块相对强弱
    backdrop = {}
    for t in BACKDROP_TICKERS:
        cur, prev = series_at(frames.get(t), asof_us)
        backdrop[t] = {"close": rnd(cur, 4), "chg_pct": rnd(pct_from(cur, prev), 3)}
    macro["backdrop"] = backdrop

    # --- 现货指数收盘，单独出一层给 perp_quotes.py --spot 用
    # 键名就是 ^GSPC / ^NDX，与 perp_quotes.py 的 INDEX_PAIRS / SPOT_ALIASES 直接对齐；
    # 值里带 close 是 perp 的 CLOSE_KEYS 之一，带 asof 是它的 DATE_KEYS 之一。
    # 注意必须放在**顶层** JSON 的 indices 键下（perp 的 CONTAINER_KEYS 只展开一层），
    # 埋在 macro.backdrop 里它是找不到的 —— 见 run() 末尾把它挂到 result["indices"]。
    macro["indices"] = {
        t: {
            "ticker": t,
            "name_cn": INDEX_NAMES_CN.get(t, t),
            "close": backdrop.get(t, {}).get("close"),
            "chg_pct": backdrop.get(t, {}).get("chg_pct"),
            "asof": macro["asof"],
        }
        for t in (SPX_TICKER, NDX_TICKER)
    }

    smh_c, qqq_c = backdrop.get("SMH", {}).get("chg_pct"), backdrop.get("QQQ", {}).get("chg_pct")
    rel = rnd(smh_c - qqq_c, 3) if is_num(smh_c) and is_num(qqq_c) else None
    if rel is None:
        rel_label = "⚪数据不足"
    elif rel >= 1.5:
        rel_label = "半导体显著强于大盘"
    elif rel <= -1.5:
        rel_label = "半导体显著弱于大盘"
    else:
        rel_label = "与大盘同步"
    macro["sector_rel_strength_pt"] = rel
    macro["sector_rel_strength_label"] = rel_label
    macro["definition"] = "板块相对强弱(pt) = SMH 日涨跌幅% − QQQ 日涨跌幅%"
    macro["sector_rel_strength_bands"] = "≥+1.5pt 显著强 / ≤−1.5pt 显著弱"
    macro["disclaimer"] = "宏观利率仅供回调驱动源判定，不参与 T1/T2/T3 触发与分桶；不预测利率路径。"
    return macro


def build_premarket_block(us_sess, pm_map, requested, frames, asof_us, bars_tier,
                          skipped_reason=None):
    """顶层 premarket 块（full 与 --macro-only 都出）。

    ⚠ 刻意**不**放进 macro：perp_quotes.py 的 load_spot 会展开 macro 一层、并把任何含
    CLOSE_KEYS（close/price/last/spot/…）的子 dict 当现货收盘吸收。本块挂在顶层自己的键下，
    且整块及其子 dict 的键名一个都不落在 CLOSE_KEYS 里——盘前价绝不能被当成现货基准。
    也**不**喂进 macro.* 的驱动源/折现率信号或 indices.*：那些阈值全标定在整日涨跌上。
    """
    us_sess = us_sess or {}
    window = bool(us_sess.get("premarket_window"))
    backdrop = {}
    for t in PREMARKET_BACKDROP:
        pm = (pm_map or {}).get(t) or {}
        px = pm.get("premarket_px")
        rec = {"premarket_px": px, "premarket_ts": pm.get("premarket_ts"),
               "premarket_age_min": pm.get("premarket_age_min"), "premarket_chg_pct": None}
        if not is_num(px):
            rec["premarket_unavailable_reason"] = (
                "该档今日盘前尚无成交或取数失败" if window and skipped_reason is None
                else (skipped_reason or "非美股盘前时段"))
        elif bars_tier.get(t):
            # 分母是回退档的收盘，分子是 Yahoo 盘前价：跨档不混用（回退链规则第 3、4 条）
            rec["premarket_chg_unavailable_reason"] = (
                f"该档日线来自回退档 {bars_tier[t]}，盘前价来自 Yahoo——跨档不混用")
        else:
            ref, _ = series_at(frames.get(t), asof_us)
            rec["premarket_chg_pct"] = rnd(pct_from(px, ref), 3)
            if rec["premarket_chg_pct"] is None:
                rec["premarket_chg_unavailable_reason"] = "该档最近完整交易日收盘缺失"
        backdrop[t] = rec
    smh_c = backdrop["SMH"]["premarket_chg_pct"]
    qqq_c = backdrop["QQQ"]["premarket_chg_pct"]
    rel = rnd(smh_c - qqq_c, 3) if is_num(smh_c) and is_num(qqq_c) else None
    requested = list(requested or [])
    found = [t for t in requested if t in (pm_map or {})]
    return {
        "window": window,
        "active": us_sess.get("premarket_active"),
        "now": us_sess.get("now"),
        "skipped_reason": skipped_reason,
        "source": PREMARKET_SOURCE,
        "tier": ENGINE_TIER_NAMES.get(_PREMARKET_FETCH.get("tier")) if requested else None,
        # 1m 取数调用本身失败的成因（已 scrub）；null = 没有失败（取到空表不算失败）
        "fetch_error": _PREMARKET_FETCH.get("error") if requested else None,
        "start": US_PREMARKET_START.strftime("%H:%M"),
        "end": MARKETS["US"]["open"].strftime("%H:%M"),
        "tz": MARKETS["US"]["tz"],
        "n_requested": len(requested),
        "n_found": len(found),
        "missing": [t for t in requested if t not in (pm_map or {})],
        "reference_only": True,
        "reference_note": PREMARKET_REFERENCE_NOTE,
        "chg_basis": "盘前涨跌% = 盘前价 ÷ 该档最近完整交易日收盘 − 1（分母同 macro.backdrop 的收盘）",
        "backdrop": backdrop,
        "sector_rel_premarket_pt": rel,
        "sector_rel_premarket_definition": "SMH 盘前涨跌% − QQQ 盘前涨跌%",
        "sector_rel_premarket_note": "仅观察，不进驱动源",
        "unavailable": {t: "指数/收益率无盘前成交 → N/A" for t in PREMARKET_UNAVAILABLE},
        "threshold_comparable": {
            "value": False,
            "reason": ("驱动源 SMH−QQQ ±1.5pt、折现率信号、静默规则「大盘当日涨幅>1.5%」"
                       "均标定在整日涨跌上，盘前读数不得喂入"),
        },
        "counts_toward": {"numerator": False, "denominator": False},
        "prohibitions": [
            "盘前读数不得写入 macro.*（驱动源/折现率信号/大盘背景）或 indices.*",
            "SMH−QQQ 盘前差值不得与 ±1.5pt 驱动源阈值比较，仅观察",
            "静默规则「大盘当日涨幅>1.5%」只认完整交易日涨跌，不得用盘前涨跌判定",
            "盘前价不得写入任何以「收盘」为名的字段，不得改变 T1/T2/T3 触发与分桶",
            "指数无盘前成交时不得以 SPY/QQQ/期货 顶替（^GSPC/^NDX/^TNX 盘前一律记 N/A）",
        ],
    }


# ---------------------------------------------------------------- 输出


def print_premarket_macro(pm):
    """💵 宏观块下的 ⏳ 盘前一行：QQQ/SMH/SOXX 盘前价 + SMH−QQQ（仅观察，不进驱动源）。

    只在美股盘前窗口内打印；每个数字都出自顶层 premarket 块（JSON 等价规则）。
    """
    if not pm or not pm.get("window"):
        return
    hhmm = (pm.get("now") or "")[11:16]
    print(f"⏳ 美股盘前（{hhmm} ET · 盘前时段 {pm['start']}–{pm['end']} ET · "
          f"来源：{pm['source']}）——仅供参考，{pm['sector_rel_premarket_note']}")
    if pm.get("skipped_reason"):
        print(f"   本次未取盘前价：{pm['skipped_reason']}")
        print()
        return
    if not pm.get("active"):
        if pm.get("fetch_error"):
            print(f"   盘前 1m 取数失败：{pm['fetch_error']}（记 N/A，不估算；这是取数失败，不是休市日）")
        else:
            print("   未取到任何今日盘前成交（记 N/A，不估算）")
        print()
        return
    parts = []
    for t, b in pm["backdrop"].items():
        if not is_num(b.get("premarket_px")):
            parts.append(f"{t} N/A")
            continue
        ts = (b.get("premarket_ts") or "")[11:16]
        parts.append(f"{t} {fnum(b['premarket_px'], 2)}（{fsign(b.get('premarket_chg_pct'), 2, '%')}，"
                     f"{ts} ET）")
    print("   " + "｜".join(parts))
    print(f"   SMH−QQQ 盘前 = {fsign(pm.get('sector_rel_premarket_pt'), 2, 'pt')}"
          f"（{pm['sector_rel_premarket_definition']}；{pm['sector_rel_premarket_note']}）")
    print(f"   {pm['chg_basis']}")
    print("   " + "、".join(pm["unavailable"]) + "：" + next(iter(pm["unavailable"].values())))
    print(f"   不可比：{pm['threshold_comparable']['reason']}")
    print()


def print_macro(macro, premarket=None):
    print("=" * 96)
    print(f"💵 宏观利率与大盘背景（数据日期：{macro.get('asof') or 'N/A'}）")
    print("=" * 96)
    y = macro["us10y"]
    rows = [
        ["10Y 美债(^TNX)", fnum(y["close_pct"], 3, "%"), fsign(y["chg_bp"], 1, "bp"),
         fsign(y["chg5_bp"], 1, "bp"), y["unit_note"]],
        ["DXY(DX-Y.NYB)", fnum(macro["dxy"]["close"], 3), fsign(macro["dxy"]["chg_pct"], 2, "%"),
         "—", macro["dxy"]["note"]],
    ]
    print_table(["指标", "收盘/当前", "日变动", "近5日变动", "说明"], rows)
    # 口径文案只在 build_macro 里写一份：人读这行与 --json 的字段必须逐字同源，
    # 两处各写一份就会在改阈值时静默分叉。
    print(f"折现率信号：{y['signal']}（口径：{y['signal_definition']}）")
    print()
    b = macro["backdrop"]
    rows = [[t, fnum(b[t]["close"], 2), fsign(b[t]["chg_pct"], 2, "%")] for t in BACKDROP_TICKERS]
    print_table(["标的", "收盘", "日涨跌%"], rows)
    print(f"板块相对强弱 = SMH − QQQ = {fsign(macro['sector_rel_strength_pt'], 2, 'pt')}"
          f"（{macro['sector_rel_strength_label']}；{macro['sector_rel_strength_bands']}）")
    print(macro["disclaimer"])
    print()
    # 盘前一行刻意放在整日口径的驱动源**之后**、单独成段：两者绝不并成一张表
    # （回退链规则第 3 条的同一手法），也不出现在「收盘」列里。
    print_premarket_macro(premarket)


def print_live_section(result):
    """⏳ 盘中活价 / 盘前活价 —— 双轨的活价那一轨，**未确认**。

    与「📋 触发表」「✅ 未触发」是两套互不相干的清单：那两个的口径是最近完整交易日
    收盘，这一个的口径是盘中活价（或美股盘前最后一笔成交）。表头逐字写明两者，就是
    为了让读者不可能把活价误读成收盘（perp 节的 `| 完整交易日收盘 | 24/7永续价 | 隐含变动% |`
    是同一手法）。措辞按 live.kind 切换，每个词都出自 live 块的字段（JSON 等价规则）。
    """
    live = result.get("live") or {}
    if not live.get("enabled"):
        return
    kind = live.get("kind")
    if kind is None:
        return
    word = live["label"]                 # 盘中 / 盘前
    col = live["price_column"]           # 盘中活价 / 盘前价
    rows_all = [r for r in result["tickers"]
                if r.get("live_price") is not None and r.get("live_kind") == kind]
    print(f"{live['title']}（**不改变 T1/T2/T3 触发与分桶**）")
    if live.get("reference_note"):
        print(f"   {live['reference_note']}")
    print(f"   活价源：{live['source']}")
    print(f"   口径：{live['caliber_note']}")
    if not rows_all:
        na = [r["ticker"] for r in result["tickers"]
              if r.get("live_unavailable_reason") and r.get("live_kind") == kind]
        print(f"   本次未取到任何{col}" + (f"（{len(na)} 档记 N/A）" if na else ""))
        print()
        return
    # 🔶 只列**新**触及（收盘未触发、此刻越过）：收盘已触发且仍在下方的档已在触发表里，
    # 再列一次就是把已确认的触发说成未确认的新触发（见 _live_metrics）。
    touched = [r for r in rows_all if r.get("new_touched_live") is True]
    show_ts = kind == "premarket"
    print()
    if kind == "premarket":
        # 全表：SKILL.md 完整版「⏳ 盘前活价 · 未确认」节以本表为底稿——覆盖全部有盘前价的
        # 美股标的（含未触发者）；每个数都是行级 *_live 字段（JSON 等价规则）。
        print(f"   {live['table_title']} · {len(rows_all)} 档")
        frows = [[r["ticker"], fnum(r.get("close"), 2), fnum(r.get("live_price"), 2),
                  fsign(r.get("live_chg_from_close_pct"), 2, "%"),
                  fsign(r.get("dd52_pct_live"), 1, "%"), fsign(r.get("dd20_pct_live"), 1, "%"),
                  fsign(r.get("gap_to_t1_pct_live"), 2, "%"), fsign(r.get("gap_to_t2_pct_live"), 2, "%"),
                  fsign(r.get("vs_ma50_pct_live"), 1, "%"), fsign(r.get("vs_ma200_pct_live"), 1, "%"),
                  f"{(r.get('live_ts') or '')[11:16] or 'N/A'}·{fnum(r.get('live_age_min'), 0, '分')}"]
                 for r in rows_all]
        print_table(["代码", "完整交易日收盘", col, live["chg_column"],
                     f"52w回撤%({word})", f"20D回撤%({word})", f"距T1%({word})", f"距T2%({word})",
                     f"vs50DMA%({word})", f"vs200DMA%({word})", "成交时刻(ET)·距今"], frows)
        print()
    if touched:
        print(f"   {live['touched_title']}（{len(touched)} 档）——{live['touched_caveat']}")
        trows = []
        for r in touched:
            tr = [r["ticker"],
                  fnum(r.get("close"), 2), fnum(r.get("live_price"), 2),
                  fsign(r.get("live_chg_from_close_pct"), 2, "%"),
                  ("T1" if r.get("t1_new_touch_live") else "") +
                  ("+T2" if r.get("t1_new_touch_live") and r.get("t2_new_touch_live")
                   else ("T2" if r.get("t2_new_touch_live") else "")),
                  fnum(r.get("t1_price"), 2), fnum(r.get("t2_price"), 2),
                  fsign(r.get("dd52_pct_live"), 1, "%")]
            if show_ts:
                tr += [(r.get("live_ts") or "")[11:16] or "N/A", fnum(r.get("live_age_min"), 0, "分")]
            trows.append(tr)
        heads = ["代码", "完整交易日收盘", col, live["chg_column"],
                 f"{word}触及", "T1触发价", "T2触发价", f"{word}52w回撤%"]
        if show_ts:
            heads += ["成交时刻(ET)", "距今"]
        print_table(heads, trows)
        print(f"   逐档读法：{live['reading']}")
        print()
    reclaimed = [r for r in rows_all if r.get("reclaimed_live") is True]
    if reclaimed:
        # 收盘已触发、此刻回到触发价上方：单列一行，不进 🔶，也不撤销收盘触发
        parts = []
        for r in reclaimed:
            lv = [f"{k.upper()}@{fnum(r.get(k + '_price'), 2)}" for k in ("t1", "t2")
                  if r.get(f"{k}_reclaimed_live")]
            parts.append(f"{r['ticker']} {col} {fnum(r.get('live_price'), 2)} 回到 {'/'.join(lv)} 上方"
                         f"（收盘 {fnum(r.get('close'), 2)} 已触发）")
        print(f"   {live['reclaimed_title']}（{len(reclaimed)} 档）——{live['reclaimed_caveat']}")
        print("     " + "；".join(parts))
        print()
    near = sorted((r for r in rows_all if r.get("touched_live") is not True
                   and is_num(r.get("gap_to_t2_pct_live"))),
                  key=lambda r: abs(r["gap_to_t2_pct_live"]))[:8]
    if near:
        print(f"   📉 距触发最近（{col}口径，前 8 档）")
        nrows = []
        for r in near:
            nr = [r["ticker"], fnum(r.get("close"), 2), fnum(r.get("live_price"), 2),
                  fsign(r.get("live_chg_from_close_pct"), 2, "%"),
                  fnum(r.get("t1_price"), 2), fsign(r.get("gap_to_t1_pct_live"), 2, "%"),
                  fnum(r.get("t2_price"), 2), fsign(r.get("gap_to_t2_pct_live"), 2, "%")]
            if show_ts:
                nr += [(r.get("live_ts") or "")[11:16] or "N/A", fnum(r.get("live_age_min"), 0, "分")]
            nrows.append(nr)
        heads = ["代码", "完整交易日收盘", col, live["chg_column"],
                 "T1触发价", "距T1", "T2触发价", "距T2"]
        if show_ts:
            heads += ["成交时刻(ET)", "距今"]
        print_table(heads, nrows)
        print()
    print(f"   {word}不判定的两项：**T3/RSI** —— {live['t3_touch_live_reason']}；"
          f"**量比/放量≥1.5x** —— {live['vol_ratio_live_reason']}。")
    na = [r for r in result["tickers"]
          if r.get("live_unavailable_reason") and r.get("live_kind") == kind]
    if na:
        # 按成因分组：盘前的「尚无成交」与「回退档跨档不混用」是两件事，不能并成一句
        by_reason = {}
        for r in na:
            by_reason.setdefault(r["live_unavailable_reason"], []).append(r["ticker"])
        print(f"   ⚪ {col}取不到 {len(na)} 档（记 N/A，不拿别处价格凑）：")
        for reason, tks in by_reason.items():
            print(f"     · {reason}：" + ", ".join(tks))
    print()


def print_report(result):
    asof = result["asof"]
    print("=" * 96)
    print("📊 个股技术面（yfinance 本地计算 · auto_adjust=False）")
    print("=" * 96)
    # 报告头的日期必须与各行实际使用的日期一致。港股行的价格可能来自比 yfinance
    # 日线更新的 hk_quote 收盘（apply_hk 覆写），此时只印 union 那个日期就是谎报。
    rows_all = result.get("tickers") or []
    price_dates = {}
    for r in rows_all:
        if r.get("asof"):
            price_dates.setdefault(r["market"], set()).add(r["asof"])
    parts = []
    for m in ("US", "HK", "KR"):
        if not asof.get(m):
            continue
        pset = price_dates.get(m) or set()
        newest = max(pset) if pset else None
        if newest and newest != asof[m]:
            parts.append(f"{MARKETS[m]['label']} 价 {newest}／指标 {asof[m]}")
        else:
            parts.append(f"{MARKETS[m]['label']} {asof[m]}")
    print("数据日期：" + "；".join(parts) if parts else "数据日期：N/A")
    # 交易状态逐市场给：三个市场的盘口天天不同步，一个全局 token 说不清楚。
    sess = result.get("session") or {}
    if sess:
        sparts = []
        for m in ("US", "HK", "KR"):
            s = sess.get(m)
            if not s:
                continue
            tag = f"{MARKETS[m]['label']} {s['label']}"
            if s.get("premarket_window"):
                pm = result.get("premarket") or {}
                act = s.get("premarket_active")
                tag += (f"（{s['now'][11:16]} ET，盘前时段 {pm.get('start', '04:00')}–"
                        f"{pm.get('end', '09:30')} ET；盘前成交"
                        + ("已取到" if act else ("未取到" if act is False else "未取"))
                        + f" {pm.get('n_found', 0)}/{pm.get('n_requested', 0)} 档"
                        + ("；盘前价仅供参考" if act else "") + "）")
            elif s.get("state") in ("intraday", "pre_market"):
                tag += f"（{s['now'][11:16]} {s['tz'].split('/')[-1]}，收盘 {s['close_time']}）"
            sparts.append(tag)
        print("交易状态：" + "；".join(sparts))
    for n in result["asof_notes"]:
        print(f"  · {n}")
    print(f"标的来源：{result['sources']['universe']}（{result['counts']['universe']} 档，按 order）")
    tier_map = result["sources"].get("prices_tier_by_ticker") or {}
    fb = {t: v for t, v in tier_map.items() if v and v != "yfinance"}
    if fb:
        print(f"⚠ 日线回退档 {len(fb)} 档（yfinance 未返回）："
              + "，".join(f"{t}[{v}]" for t, v in sorted(fb.items())))
        print("  这些行的 收盘/52周高/RSI 与 yfinance 同口径；**量比不可**与"
              "「放量 ≥1.5x」阈值比较（量口径不同）。")
    if result.get("source_unavailable"):
        print(f"⛔ 取数失败 {len(result['source_unavailable'])} 档"
              f"（**不是上市太新**，需查）：" + ", ".join(result["source_unavailable"]))
    print()

    rows = []
    for r in result["tickers"]:
        if r.get("insufficient_history"):
            continue
        rows.append([
            r["ticker"], r.get("asof") or "N/A", fnum(r.get("close"), 2), fsign(r.get("chg_pct"), 2, "%"),
            fnum(r.get("vol_ratio"), 2), fnum(r.get("hi52"), 2), fnum(r.get("dd52_pct"), 1, "%"),
            fnum(r.get("t1_price"), 2), fnum(r.get("hi20"), 2), fnum(r.get("dd20_pct"), 1, "%"),
            fnum(r.get("t2_price"), 2), fnum(r.get("rsi14"), 1),
            fbool(r.get("t1")), fbool(r.get("t2")), fbool(r.get("t3")),
            r.get("ma_position", "N/A"), r.get("ma_structure", "N/A"),
            r.get("next_earnings") or "N/A",
        ])
    print_table(["代码", "数据日", "收盘", "涨跌%", "量比", "52周高", "52w回撤%", "T1触发价",
                 "20日高", "20D回撤%", "T2触发价", "RSI14", "T1", "T2", "T3",
                 "均线位置", "均线结构", "下次财报"], rows)
    print("口径：T1触发价=52周高×0.85；T2触发价=20日高×0.92；RSI14 即 T3 当前值（≤35 触发）。"
          "✅=已触发，—=未触发，N/A=数据不足暂不判定。")
    print()

    print_live_section(result)

    print("📐 补充观察 · 短中期均线（不改变 T1/T2/T3 与分桶）")
    rows = [[r["ticker"], fnum(r.get("close"), 2), fnum(r.get("ma5"), 2), fnum(r.get("ma10"), 2),
             fnum(r.get("ma20"), 2), fnum(r.get("ma60"), 2), fnum(r.get("ma200"), 2),
             fsign(r.get("vs_ma50_pct"), 1, "%"), fsign(r.get("vs_ma200_pct"), 1, "%"),
             r.get("ma_structure", "N/A")]
            for r in result["tickers"] if not r.get("insufficient_history")]
    print_table(["代码", "收盘", "MA5", "MA10", "MA20", "MA60", "MA200",
                 "vs50DMA", "vs200DMA", "均线结构"], rows)
    print()

    print("✅ 未触发个股（T1/T2/T3 全部未触发 · 不进任何桶）")
    if not result["untriggered"]:
        # 「一个标的都没被评估」和「评估了但全都触发」是完全相反的两件事，
        # 早先都打同一句「全部标的均至少触发一项」，会把空跑说成满仓触发。
        if not result.get("counts", {}).get("evaluated"):
            print("  本日没有任何标的进入技术面判定（可用标的 0 档），"
                  "触发与否无从判断——请检查标的清单与取数是否正常")
        else:
            print(f"  本日无未触发标的（参与判定的 {result['counts']['evaluated']} 档"
                  f"均至少触发一项）")
    else:
        by_t = {r["ticker"]: r for r in result["tickers"]}
        rows = []
        for t in result["untriggered"]:
            r = by_t[t]
            note = []
            if r.get("t2") is None:
                note.append("T2 数据不足，暂不判定")
            if is_num(r.get("gap_to_t1_pct")):
                note.append(f"距T1还差 {r['gap_to_t1_pct']:+.1f}%")
            if is_num(r.get("gap_to_t2_pct")):
                note.append(f"距T2还差 {r['gap_to_t2_pct']:+.1f}%")
            if r.get("next_earnings"):
                note.append(f"财报 {r['next_earnings']}")
            if is_num(r.get("vol_ratio")) and r["vol_ratio"] >= 1.5:
                # 「放量 ≥1.5x」是在 yfinance 的量口径上标定的。回退档的量系统性偏低，
                # 直接套这个阈值就是让阈值跨档旅行（回退链规则第 4 条明令禁止）。
                if r.get("vol_ratio_comparable") is False:
                    note.append(f"量比 {r['vol_ratio']:.2f}x（回退档量口径，不可比 1.5x 阈值）")
                else:
                    note.append(f"放量 {r['vol_ratio']:.2f}x")
            rows.append([t, fnum(r.get("close"), 2), fnum(r.get("dd52_pct"), 1, "%"),
                         fnum(r.get("dd20_pct"), 1, "%"), fnum(r.get("rsi14"), 1), "；".join(note)])
        print_table(["代码", "收盘", "52w回撤%", "20D回撤%", "RSI", "备注"], rows)
    print()

    if result["insufficient_history"]:
        # 标题必须同时点名两类，否则「取不到」会被整段读成「上市太新」——
        # ⚪️ 与 ❌ 的语义塌缩正是本仓库明令要防的事。每一行的成因写在备注里。
        n_bad = len(result.get("source_unavailable") or [])
        n_young = len(result.get("insufficient_history_too_young")
                      or result["insufficient_history"])
        head = f"⛔ 不参与技术面判定（不进任何桶、也不进未触发清单）：❌ 上市太新 {n_young} 档"
        if n_bad:
            head += f"　＋　⚪️ 取数失败 {n_bad} 档（**成因不同，需查，不是等满半年就好**）"
        print(head)
        rows = []
        for t in result["insufficient_history"]:
            r = next(x for x in result["tickers"] if x["ticker"] == t)
            rows.append([t, str(r.get("bars", 0)), fnum(r.get("close"), 2), "；".join(r.get("notes", []))])
        print_table(["代码", "可用日线根数", "收盘", "原因"], rows)
        print(f"判定口径：可用日线 < {MIN_HISTORY_BARS} 根（约半年）即自动排除，不硬编码代码名单。")
        print()

    print(f"统计：技术面有效 {result['counts']['evaluated']} 档；"
          f"触发 {result['counts']['triggered']} 档；未触发 {result['counts']['untriggered']} 档；"
          f"历史不足 {result['counts']['insufficient_history']} 档。")
    print("所有 yfinance 派生字段标注「yfinance 本地计算」；港股价格字段以 hk_quote.py 为准。")


# ---------------------------------------------------------------- 降级汇总


def apply_degraded(result):
    """汇总本轮的降级理由，写进顶层 degraded / degraded_reasons。

    口径与 ⚪️ 记账一致：只要有自检没过、有回退触发、有源取不到、或有字段因缺数记
    N/A，就必须 degraded=true。「不知道」绝不能被记成「查过了、没问题」——那正是
    --quiet 场景下最容易被下游读成正常的一种谎。
    """
    reasons = []

    for mkt in ("US", "HK", "KR"):
        if mkt in (result.get("asof") or {}) and result["asof"][mkt] is None:
            reasons.append(f"{MARKETS[mkt]['label']}无法确定完整交易日（记 N/A）")

    for n in result.get("asof_notes") or []:
        # ⚠ 开头的是回退/取数告警；yfinance 缺票那条是「源没给数据」，两类都算降级。
        if n.startswith("⚠") or n.startswith("yfinance 未返回数据的标的"):
            reasons.append(n)

    macro = result.get("macro") or {}
    y = macro.get("us10y") or {}
    if y.get("close_pct") is None:
        reasons.append(f"10Y 美债(^TNX) 无有效读数：{y.get('unit_note') or 'N/A'}")
    if (macro.get("dxy") or {}).get("close") is None:
        reasons.append("DXY(DX-Y.NYB) 无有效读数（记 N/A）")
    if macro.get("sector_rel_strength_pt") is None:
        reasons.append("板块相对强弱(SMH−QQQ) ⚪数据不足")
    for t, blk in (macro.get("indices") or {}).items():
        if blk.get("close") is None:
            reasons.append(f"现货指数 {t} 收盘缺失，perp_quotes.py 的隔夜隐含跳空将无从对照")

    rows = result.get("tickers") or []
    # ⚠ 这里**必须**读显式旗标，绝不能扫 note 文本找「回退」二字。
    # 2026-09-11 的教训：新增了「日线来自回退档…」与「yfinance 与回退档均未返回」两条 note
    # 之后，子串匹配把 42 档美股/韩股全打成「港股价格回退」，provenance 字段整体说谎
    # （回退链规则第 2 条：来源必须标对；第 3 条：不同档不得并成一张表）。
    split = [r["ticker"] for r in rows if r.get("asof_split")]
    if split:
        reasons.append(
            "⚠ 价格口径与指标口径不同日（价来自 hk_quote 收盘、MA/RSI/20日高 来自 yfinance 日线）："
            + "、".join(f"{r['ticker']}(价 {r.get('asof')}／指标 {r.get('asof_indicators')})"
                        for r in rows if r.get("asof_split"))
            + "；两者不得当成同一天的读数比较")
    hk_fb = [r["ticker"] for r in rows if r.get("hk_price_fallback")]
    if hk_fb:
        reasons.append("港股价格回退 yfinance 未复权日线（口径次优）：" + ", ".join(hk_fb))
    # 日线回退档（非 Yahoo 源）单独成条，按档位分组——不同档不并表
    by_tier = {}
    for r in rows:
        t = r.get("bars_source")
        if t and t != "yfinance":
            by_tier.setdefault(t, []).append(r["ticker"])
    for tier, tks in sorted(by_tier.items()):
        reasons.append(f"日线回退档 {tier}（yfinance 未返回）{len(tks)} 档：" + ", ".join(tks))
        reasons.append(f"　└ 该档成交量口径与 yfinance 不同，这些行的 量比 不可与"
                       f"「放量 ≥1.5x」阈值比较（阈值不随源转移）")
    # ⚪️ 取不到（异常，需查）与 ❌ 上市太新（正常）分两条写。
    # 混成一句会让「所有档位都没拿到数」读起来像「等满半年就好」。
    src_bad = result.get("source_unavailable") or []
    if src_bad:
        reasons.append(
            f"⚠ 取数失败 {len(src_bad)} 档（yfinance 与回退档均未返回，**不是上市太新**，需查）："
            + ", ".join(src_bad))
    young = result.get("insufficient_history_too_young")
    if young is None:
        young = [t for t in (result.get("insufficient_history") or []) if t not in src_bad]
    if young:
        reasons.append(f"历史不足、不参与技术面判定 {len(young)} 档：" + ", ".join(young))
    # 触发位为 null＝「数据不足暂不判定」，与 false＝「判过了、没触发」是两回事。
    undecided = [r["ticker"] for r in rows
                 if not r.get("insufficient_history")
                 and any(r.get(k) is None for k in ("t1", "t2", "t3"))]
    if undecided:
        reasons.append(f"T1/T2/T3 中有触发位因数据不足暂不判定（null，非 false）"
                       f" {len(undecided)} 档：" + ", ".join(undecided))

    # 盘中运行本身就是一种降级：报告里每一个技术面数字都是**前一个**完整交易日的。
    # 不把它写成 degraded_reasons，只读该字段的下游会把一份昨日快照当成今日读数。
    sess = result.get("session") or {}
    for mkt, s in sess.items():
        if s.get("premarket_window"):
            # 美股盘前时段：不论 state 是 pre_market 还是 pre_open，措辞都按「盘前」。
            # 窗口内今天还没开盘，所以技术面全是前一完整交易日之值——这是事实陈述，
            # 盘前价只出现在叠加层里（未确认）。
            reasons.append(
                f"⚠ {MARKETS[mkt]['label']}此刻为盘前（{s.get('now')}，盘前时段 "
                f"{US_PREMARKET_START:%H:%M}–{MARKETS[mkt]['open']:%H:%M} ET）："
                f"本行所有技术面字段（收盘/52周高/20日高/T1·T2触发价/RSI/均线/量比）"
                f"均为最近完整交易日 {s.get('asof')} 之值，非今日"
                # 只有真附了盘前层才这么说：premarket_active 为 false/null（没取到、取数失败、
                # --no-live-overlay）时根本没有 ⏳ 层，这半句会与「本次无盘前叠加层」自相矛盾
                + ("；盘前价只见 ⏳ 盘前叠加层（未确认）" if s.get("premarket_active") is True else ""))
        elif s.get("state") in ("intraday", "pre_market"):
            reasons.append(
                f"⚠ {MARKETS[mkt]['label']}此刻为{s.get('label') or '盘中'}"
                f"（{s.get('now')}，收盘 {s.get('close_time')}"
                f"{'·半日市' if s.get('early_close') else ''}）："
                f"本行所有技术面字段（收盘/52周高/20日高/T1·T2触发价/RSI/均线/量比）"
                f"均为最近完整交易日 {s.get('asof')} 之值，非今日")
    live = result.get("live") or {}
    rows_t = result.get("tickers") or []
    if live.get("enabled") and live.get("rows"):
        n_by_kind = {}
        for r in rows_t:
            if r.get("live_price") is not None and r.get("live_kind"):
                n_by_kind[r["live_kind"]] = n_by_kind.get(r["live_kind"], 0) + 1
        if n_by_kind.get("intraday"):
            reasons.append(
                f"⚠ 已附盘中活价叠加层 {n_by_kind['intraday']} 档：活价仅用于「现价/距T1·T2/盘中触及」，"
                f"**未确认、不改分桶、不进 triggered/untriggered**；量比与 T3 在盘中记 null")
        if n_by_kind.get("premarket"):
            reasons.append(
                f"⚠ 已附盘前叠加层 {n_by_kind['premarket']} 档：盘前价仅供参考，用于「现价/回撤/距T1·T2/盘前触及」的分析，"
                f"**未确认、不改分桶、不进 triggered/untriggered**；量比与 T3 在盘前记 null")
    # 盘前「尚无成交」的档已由 run() 的「⚠ 盘前成交取不到 N 档」那条（premarket.missing）报过，
    # 这里不再重报同一批代码；只剩其它成因（回退档跨档不混用、指数/期货、外盘挂牌）的档。
    pm_reported = set((result.get("premarket") or {}).get("missing") or [])
    for kind, word in (("intraday", "盘中活价"), ("premarket", "盘前价")):
        no_live = [r["ticker"] for r in rows_t
                   if r.get("live_unavailable_reason") and r.get("live_kind", "intraday") == kind
                   and not (kind == "premarket" and r["ticker"].upper() in pm_reported)]
        if no_live:
            reasons.append(f"{word}取不到 {len(no_live)} 档（记 N/A，不拿别处价格凑）："
                           + ", ".join(no_live))

    e = result.get("earnings") or {}
    reasons.extend(e.get("warnings") or [])

    result["degraded_reasons"] = list(dict.fromkeys(reasons))
    result["degraded"] = bool(result["degraded_reasons"])
    return result


# ---------------------------------------------------------------- 主流程


def run(args):
    # 先定位姊妹技能，再加载重依赖：两者是彼此独立的两类故障，各自给各自的错误。
    # 反过来（先 load_deps）会让「没装 yfinance」盖住「姊妹技能装错了」，
    # 用户装完 yfinance 才发现真正的问题在别处。
    weekly = find_weekly_dir()
    universe = load_universe(weekly)
    yf = load_deps()

    if args.tickers:
        want = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
        known = {u["ticker"].upper(): u for u in universe}
        selected = []
        for t in want:
            selected.append(known.get(t, {"ticker": t, "theme": None, "layer": None,
                                          "etf": False, "hk_quote": t.upper().endswith(".HK"),
                                          "currency": "USD", "order": None}))
    elif args.macro_only:
        selected = []
    else:
        selected = list(universe)

    sel_tickers = [s["ticker"] for s in selected]
    # 指数类必下（宏观/大盘背景 + 美股完整交易日基准 SMH）；与选中标的去重
    dl = list(dict.fromkeys(sel_tickers + INDEX_TICKERS))

    # 引擎回退链（第1档 requests.Session+浏览器UA → 第2档 curl_cffi）收敛在
    # yf_download_tiered()，日线与盘前 1m 共用同一条链——两份链各写各的会在下次改档位时分叉。
    # 日线这一次 why 传空串：回退 note 文案与抽出前逐字一致。
    sess = make_session()
    raw, _ = yf_download_tiered(
        yf, sess, "",
        tickers=dl, period="2y", interval="1d", progress=False,
        auto_adjust=False, group_by="ticker", threads=True)
    # yfinance 整片皆空**不再直接 exit 3**：那是 2026-09-11 之前的行为，会在回退档
    # 还没跑之前就退出——而 Routines 容器正是「yfinance 整片皆空」那一种，等于回退档
    # 永远不会被用上。现在先记下来，把判定推迟到回退档也试过之后。
    yf_dead = raw is None or len(raw) == 0
    if yf_dead:
        err("⚠ yfinance 未返回任何日线数据（全部标的皆空）。成因按实测可能性排序："
            "① 出站代理无法完成到 Yahoo 数据 host（yfinance 的 _BASE_URL_ 写死 "
            "query2.finance.yahoo.com，换传输引擎救不了）；② Yahoo 按 UA 限流 429；"
            "③ 断网。转入回退档（美股 stockanalysis / 韩股 Naver）。")
        _FALLBACK_NOTES.append(
            "⚠ yfinance 整片皆空，全部标的改由回退档取数（见各行 bars_source）")

    frames = {t: None for t in dl} if yf_dead else {t: frame_for(raw, t) for t in dl}
    missing = [t for t, f in frames.items() if f is None]

    # ---- 第1档：yfinance 没给出的标的走非 Yahoo 回退档（美股 stockanalysis / 韩股 Naver）
    # 逐标的补，不是「整轮失败才回退」：yfinance 部分缺票与整片全灭走同一条路径，
    # 少一个分支就少一处会漏测的行为。
    bars_tier = {}
    # 候选 = 没有帧的 + 帧「短到不够判定」的。后者是限流下的常见形态：yfinance 回了
    # 几根就断，直接拿去算会把标的标成「历史不足」，而回退档本可给上千根。
    short = [t for t, f in frames.items() if f is not None and len(f) < MIN_HISTORY_BARS]
    candidates = missing + [t for t in short if t not in missing]
    if candidates:
        fb_frames, fb_tiers, fb_notes = fallback_frames(candidates)
        for t, f in fb_frames.items():
            cur = frames.get(t)
            # 只在回退档**确实更长**时替换：真·次新股（LYTE 24 根）两档都短，
            # 此时保留 yfinance 原帧，不制造无谓的档位切换。
            if cur is None or len(f) > len(cur):
                frames[t] = f
                bars_tier[t] = fb_tiers.get(t, "回退档")
        _FALLBACK_NOTES.extend(fb_notes)
        missing = [t for t, f in frames.items() if f is None]

    if not any(f is not None for f in frames.values()):
        # 到这里才是真正的取数失败：**两个上游都没给出任何数据**。
        # 退出码 3（本仓库保留：1 参数错误｜2 依赖缺失｜3 取数失败｜4 量级自检未通过）；
        # 回 1 会让调度层（SKILL.md 第二步逐单元收 .rc）把一次取数失败读成「命令写错了」。
        err("错误：yfinance 与回退档均未返回任何日线数据。"
            "区分方法：带浏览器 UA 直连 query1.finance.yahoo.com/v8/finance/chart/NVDA；"
            "另跑 `python3 scripts/bars_fallback.py --tickers NVDA` 看回退档是死在哪一档。")
        err("     本次不写 tech.json——下游 perp_quotes.py 的 --spot 因此拿不到现货基准，"
            "🌙 盘后隐含只能标 ⚪️，**不得拿别处价格凑数**。")
        sys.exit(3)

    # ---- 各市场的完整交易日（美股用 SMH 基准；港股/韩股按自己市场单独判定）
    # 传输层降级（yfinance 不收 session= -> 退回 curl_cffi）必须进 asof_notes：
    # ⚠ 前缀让 apply_degraded() 把它转成 degraded_reasons 字段，--json 才不会少于正文。
    asof, asof_notes = {}, list(_ENGINE_NOTES) + list(_FALLBACK_NOTES)
    sessions = {}
    markets_needed = {market_of(t) for t in sel_tickers} | {"US"}
    for mkt in markets_needed:
        mk_frames = [frames[t] for t in dl if frames[t] is not None and market_of(t) == mkt]
        if not mk_frames:
            continue
        union = mk_frames[0].index
        for f in mk_frames[1:]:
            union = union.union(f.index)
        union = union.sort_values()
        ref = frames.get(US_REF_TICKER) if mkt == "US" else mk_frames[0]
        d, notes = resolve_asof(ref, union, mkt)
        asof[mkt] = d.date().isoformat() if d is not None else None
        asof[mkt + "_ts"] = d
        asof_notes.extend(notes)
        # 盘口状态是纯观察层，与上面的 asof 判定完全独立（它不改 asof，只描述此刻）。
        sessions[mkt] = market_session(mkt, union, d)
    if asof.get("US") is None:
        # 同上：基准标的取不到日线是**取数失败**，不是参数错误 → 3。
        err(f"错误：无法确定美股完整交易日（基准 {US_REF_TICKER} 无数据）——"
            f"取数失败。成因排序同上：代理 TLS 隧道 > UA 限流 429 > 断网，"
            f"详见 make_session() 上方的传输层矩阵。")
        sys.exit(3)
    if missing:
        asof_notes.append("yfinance 未返回数据的标的（记 N/A，不估算）：" + ", ".join(missing))

    # ---- 美股盘前层（1m prepost=True）——只在盘前窗口内取，full 与 --macro-only 共用
    # 放在这里（而不是叠加层循环里）是因为 --macro-only 在下面就 return 了，而盘前的
    # QQQ/SMH/SOXX 背景两条路径都要。--no-live-overlay 同时关掉盘中与盘前两层、且不发 1m 请求。
    want_live = not args.no_live_overlay
    us_sess = sessions.get("US") or {}
    pm_map, pm_requested, pm_skipped = {}, [], None
    if us_sess.get("premarket_window"):
        if want_live:
            # 个股：选中的美股行（排除 hk_quote 旗标与指数/期货代码）＋ 盘前大盘背景三只
            pm_requested = list(dict.fromkeys(
                [m["ticker"].upper() for m in selected
                 if not m.get("hk_quote") and premarket_eligible(m["ticker"])]
                + list(PREMARKET_BACKDROP)))
            now_et = dt.datetime.now(ZoneInfo(MARKETS["US"]["tz"]))
            pm_map, pm_notes = fetch_us_premarket(yf, sess, pm_requested, now_et)
            asof_notes.extend(pm_notes)
            us_sess["premarket_active"] = bool(pm_map)
            hhmm = (us_sess.get("now") or "")[11:16]
            if not pm_map:
                # 取数本身失败时 pm_notes 里已有带成因的 ⚠：不再另说「可能是休市日」——
                # 那会把一次取数失败读成一个可能的休市日。
                if not _PREMARKET_FETCH.get("error"):
                    asof_notes.append(
                        f"⚠ 美股盘前时段（{hhmm} ET）但未取到任何今日盘前成交——"
                        f"可能是休市日或数据源未就绪；本次无盘前叠加层")
            else:
                pm_missing = [t for t in pm_requested if t not in pm_map]
                if pm_missing:
                    asof_notes.append(
                        f"⚠ 盘前成交取不到 {len(pm_missing)} 档：" + ", ".join(pm_missing)
                        + "（该档今日盘前尚无成交或取数失败）")
        else:
            # 没去看 ≠ 看过了没有：premarket_active 保持 null，理由进 premarket.skipped_reason
            pm_skipped = "--no-live-overlay：本次关闭盘中/盘前叠加层，未取盘前 1m"
            asof_notes.append("ℹ 美股处于盘前时段，但 --no-live-overlay 关闭了盘前叠加层（未取盘前价）")
    else:
        pm_skipped = "非美股盘前时段（工作日 04:00–09:30 ET 之外），不取盘前价"
    # 1m 那次若触发引擎回退，note 在上面的快照之后才产生——补一次合并，否则 degraded
    # 报 false 而 sources.transport 报「已降级」，两个字段自相矛盾。
    asof_notes.extend(n for n in _ENGINE_NOTES if n not in asof_notes)

    macro = build_macro(frames, asof["US_ts"])
    premarket = build_premarket_block(us_sess, pm_map, pm_requested, frames, asof["US_ts"],
                                      bars_tier, skipped_reason=pm_skipped)
    pm_sources = {
        "premarket_prices": PREMARKET_SOURCE + "（仅美股个股/ETF；指数/收益率无盘前成交 → N/A）",
        "premarket_transport": ("与日线同一引擎回退链：第1档 requests.Session(浏览器UA) → "
                                "第2档 yfinance 默认引擎(curl_cffi)；1m 那次回退时，degraded_reasons 里的"
                                "引擎回退条目文中带「盘前 1m：」标签（形如「⚠ yfinance 引擎回退至第2档…：盘前 1m：…」）"),
        "premarket_tier": premarket["tier"],
    }
    if args.macro_only:
        result = {
            "generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "mode": "macro-only",
            "asof": {k: v for k, v in asof.items() if not k.endswith("_ts")},
            # macro-only 也要给盘口状态：驱动源判定用的 SMH−QQQ 与 10Y 日变动都是
            # **整日**量，盘中运行时它们仍是前一完整交易日之值。不给这个字段，
            # 读 macro-only 的那一步就无从知道自己拿到的是昨天的驱动源。
            "session": sessions,
            "asof_notes": asof_notes,
            "macro": macro,
            # 顶层 indices：perp_quotes.py --spot 只展开顶层容器键，^GSPC/^NDX 必须放这一层
            "indices": macro.get("indices", {}),
            # 盘前背景（QQQ/SMH/SOXX）：仅观察，不进 macro 驱动源（见 build_premarket_block）
            "premarket": premarket,
            "sources": {
                "prices": "yfinance 本地计算（auto_adjust=False）",
                "transport": ("yfinance + requests.Session(浏览器UA)" if _SESSION_OK
                              else "yfinance：部分或全部调用未能使用 requests.Session(浏览器UA)，已退回自管引擎（哪一处见 degraded_reasons）")
                             + "（**仅描述 Yahoo 传输层，不涵盖回退档**）",
                "prices_fallback_order": {
                    "US": ["yfinance(auto_adjust=False)", "stockanalysis.com(c·拆股已还原·股息未复权)"],
                    "KR": ["yfinance(auto_adjust=False)", "naver siseJson(拆股已还原·股息未复权)"],
                    "HK": ["yfinance(auto_adjust=False) + hk_quote.py 覆写价格"],
                    "INDEX": ["yfinance(auto_adjust=False)（无替代档，取不到即 N/A）"],
                },
                "prices_tier_note": "--macro-only 不产出个股行，故无 prices_tier_by_ticker；"
                                    "宏观/背景标的的档位见 degraded_reasons",
                **pm_sources,
            },
        }
        return apply_degraded(result), True

    # ---- 财报日预取（并发，整轮只此一处并发）
    # ⚠EARN 标签要保留，所以不是砍掉这一步，而是把 N 次串行往返整体提到逐标的循环之前
    # 并发跑：串行时 41 只非 ETF ≈ 39s，占整轮 ≈ 45s 的绝大头。
    # 命中集合只按「元数据 + 有没有日线」筛（与 compute_row 里的 etf/^ 判定同口径），
    # 「数据日是哪天」仍只在 compute_row 里算一次——绝不为了并发再写第二份日期规则。
    want_earn = not args.no_earnings
    earn_targets = []
    if want_earn:
        earn_targets = list(dict.fromkeys(
            m["ticker"] for m in selected
            if not m.get("etf") and not m["ticker"].startswith("^")
            and frames.get(m["ticker"]) is not None
        ))
    earnings_cals, earn_stats = prefetch_earnings(yf, earn_targets, sess)
    # asof_notes 在 prefetch_earnings **之前**就已快照 _ENGINE_NOTES；财报那条路径若在此处
    # 才触发 session= 降级，note 会晚于快照产生 -> degraded 报 false 而 sources.transport
    # 报「已降级」，两个字段自相矛盾，等于把「不知道」记成「查过了，没事」。补一次合并。
    asof_notes.extend(n for n in _ENGINE_NOTES if n not in asof_notes)
    earnings, earn_warnings = earnings_block(earn_stats, want_earn)
    asof_notes.extend(earn_warnings)

    # ---- 逐标的计算
    rows = []
    for meta in selected:
        t = meta["ticker"]
        mkt = market_of(t)
        row = compute_row(meta, frames.get(t), asof.get(mkt + "_ts"), mkt,
                          earnings_cals, want_earnings=want_earn)
        # 档位标注：来源不明的数字事后无法复核（回退链规则第 2 条）
        tier = bars_tier.get(t)
        if row.get("source_unavailable"):
            # 没有任何档给出数据：provenance 必须是 null，不能说谎说来自 yfinance。
            # 「查过了、来自 X」与「根本没拿到」是两回事（⚪️ / ❌ 的字段形态）。
            row["bars_source"] = None
            row["vol_ratio_comparable"] = None
        elif tier:
            row["bars_source"] = tier
            row["price_source"] = f"{tier}（yfinance 未返回，已回退）"
            # 阈值不随源转移（规则第 4 条）：量口径不同，量比不可跨档比较
            row["vol_ratio_comparable"] = False
            row["notes"].append(
                f"日线来自回退档 {tier}（yfinance 未返回）；"
                "收盘/52周高/RSI 与 yfinance 同口径（拆股已还原·股息未复权），"
                "但**量比不可**与 yfinance 标定的「放量 ≥1.5x」阈值比较"
            )
        else:
            row["bars_source"] = "yfinance"
            row["vol_ratio_comparable"] = True
        rows.append(row)

    # ---- 港股覆写（hk_quote.py 为准）
    hk_codes = [m["ticker"] for m in selected if m.get("hk_quote") or market_of(m["ticker"]) == "HK"]
    if hk_codes:
        quotes, hk_notes = hk_overlay(weekly, hk_codes)
        asof_notes.extend(hk_notes)
        for r in rows:
            if r["market"] != "HK" or r.get("insufficient_history"):
                continue
            q = quotes.get(hk_key(r["ticker"]))
            if usable_hk(q):
                apply_hk(r, q)
            elif q:
                # 显式旗标，不让 apply_degraded 再去猜 note 文本（见下方说明）
                r["hk_price_fallback"] = True
                r["notes"].append("hk_quote 报价不可用作收盘价，本行价格回退 yfinance 未复权日线")
            else:
                r["hk_price_fallback"] = True
                r["notes"].append("hk_quote 未返回本标的，价格回退 yfinance 未复权日线")

    for r in rows:
        finalize_triggers(r)

    # ---- 盘中活价叠加层（双轨的活价那一轨）
    # **必须**在 finalize_triggers 之后：它要读已冻结的 hi52/hi20/T1价/T2价/均线，
    # 只把「到这些阈值的距离」换成活价。顺序反了就会拿到空阈值。
    # 美股的价源二选一（绝不叠两层）：
    #   盘前窗口内且取到了盘前成交 → 1m 盘前层（不论 state 是 pre_market 还是 pre_open）
    #   state == intraday           → 日线未完成 bar 的盘中层
    #   其余（含窗口外的 pre_market）→ 不附加：个股当日日线 bar 盘前全是 NaN，没有合法价源
    # 港股/韩股照旧：state ∈ {intraday, pre_market} 时附日线盘中层。
    # 只有选中了可附层的美股行才算「本轮有盘前叠加层」：只取到 QQQ/SMH/SOXX 背景、而个股
    # 全是港/韩时，live.kind 若仍报 premarket，正文会印「本次未取到任何盘前价」——与报告头
    # 「盘前成交已取到 3/3 档」自相矛盾。背景读数只进顶层 premarket 块。
    has_us_rows = any(r["market"] == "US" and not r.get("insufficient_history") for r in rows)
    us_pm_on = bool(want_live and us_sess.get("premarket_window") and us_sess.get("premarket_active")
                    and has_us_rows)
    live_rows = []
    if want_live:
        for r in rows:
            if r.get("insufficient_history"):
                continue
            mkt = r["market"]
            ms = sessions.get(mkt) or {}   # 不叫 sess：那是上面的 Yahoo requests.Session
            if mkt == "US":
                if us_pm_on:
                    attach_premarket_overlay(r, pm_map.get(r["ticker"].upper()), ms)
                elif ms.get("state") == "intraday":
                    attach_live_overlay(r, frames.get(r["ticker"]), asof.get(mkt + "_ts"), ms)
                else:
                    continue
            else:
                if ms.get("state") not in ("intraday", "pre_market"):
                    continue
                attach_live_overlay(r, frames.get(r["ticker"]), asof.get(mkt + "_ts"), ms)
            if r.get("live_price") is not None:
                live_rows.append(r)
    markets_premarket = ["US"] if us_pm_on else []
    markets_intraday = sorted(m for m, s in sessions.items() if s.get("state") == "intraday")
    live_kind = ("premarket" if markets_premarket else
                 "intraday" if (markets_intraday or any(r.get("live_kind") == "intraday" for r in rows))
                 else None)
    lt = LIVE_TEXT.get(live_kind or "intraday")
    word = "盘前" if live_kind == "premarket" else "盘中"

    evaluated = [r for r in rows if not r.get("insufficient_history")]
    untriggered = [r["ticker"] for r in evaluated
                   if r.get("t1") is not True and r.get("t2") is not True and r.get("t3") is not True]
    triggered = [r["ticker"] for r in evaluated if r.get("triggered") is True]
    insufficient = [r["ticker"] for r in rows if r.get("insufficient_history")]
    # 取数失败单列，且**不与「上市太新」混在一个数字里**
    src_unavail = [r["ticker"] for r in rows if r.get("source_unavailable")]
    too_young = [t for t in insufficient if t not in src_unavail]

    result = {
        "generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "mode": "full",
        "asof": {k: v for k, v in asof.items() if not k.endswith("_ts")},
        # 报告头「交易状态：<盘后/盘中/休市>」的机读来源（output-format.md:13）。
        # 逐市场给，因为三个市场的盘口状态天天不同步：实测 2026-09-14 12:16 ET
        # 美股 intraday，而港股/韩股当时都已收盘。
        "session": sessions,
        "asof_notes": asof_notes,
        "macro": macro,
        # 顶层 indices：perp_quotes.py --spot 只展开顶层容器键，^GSPC/^NDX 必须放这一层
        "indices": macro.get("indices", {}),
        "tickers": rows,
        "earnings": earnings,
        "triggered": triggered,
        "untriggered": untriggered,
        "insufficient_history": insufficient,
        # ⚪️ 取不到（异常，需查）——与 ❌ 上市太新（正常，等满半年）分开
        "source_unavailable": src_unavail,
        "insufficient_history_too_young": too_young,
        # 双轨的活价轨。与上面 triggered/untriggered 是**两套互不相干的清单**：
        # 那两个的口径是最近完整交易日收盘，这个的口径是盘中活价、且未确认。
        # 合并两者、或拿本清单改分桶，就是 output-format.md:89 禁的那件事。
        "live": {
            "enabled": bool(want_live),
            # intraday = 日线未完成 bar 的盘中层；premarket = 美股 1m 盘前层；null = 本轮无叠加层
            "kind": live_kind,
            "rows": len(live_rows),
            # 只列 state == intraday 的市场（pre_market 不再算进来：美股盘前走 markets_premarket）
            "markets_intraday": markets_intraday,
            "markets_premarket": markets_premarket,
            # 🔶 清单：只列**新**触及（收盘未触发、此刻越过）。顶层这三个名字沿用旧键名，
            # 但取自行级 *_new_touch_live——行级 t*_touch_live 只问「此刻在不在下方」，
            # 收盘已触发的档也会记 True，拿它出 🔶 就是把已确认的触发说成新的（见 _live_metrics）。
            "touched_live": sorted(r["ticker"] for r in live_rows if r.get("new_touched_live") is True),
            "t1_touch_live": sorted(r["ticker"] for r in live_rows if r.get("t1_new_touch_live") is True),
            "t2_touch_live": sorted(r["ticker"] for r in live_rows if r.get("t2_new_touch_live") is True),
            "new_touched_live": sorted(r["ticker"] for r in live_rows if r.get("new_touched_live") is True),
            "t1_new_touch_live": sorted(r["ticker"] for r in live_rows if r.get("t1_new_touch_live") is True),
            "t2_new_touch_live": sorted(r["ticker"] for r in live_rows if r.get("t2_new_touch_live") is True),
            "touched_definition": (f"新触及 = {word}价 ≤ 触发价 且 最近完整交易日收盘**未**触发该位；"
                                   f"收盘已触发、此刻仍在下方的档不重列（已在触发表里）"),
            # 收盘已触发、此刻回到触发价上方：单列，不进 🔶，也不撤销收盘触发与分桶
            "reclaimed_live": sorted(r["ticker"] for r in live_rows if r.get("reclaimed_live") is True),
            "t1_reclaimed_live": sorted(r["ticker"] for r in live_rows if r.get("t1_reclaimed_live") is True),
            "t2_reclaimed_live": sorted(r["ticker"] for r in live_rows if r.get("t2_reclaimed_live") is True),
            "reclaimed_title": lt["reclaimed_title"],
            "reclaimed_caveat": lt["reclaimed_caveat"],
            "reclaimed_definition": (f"已收回 = 最近完整交易日收盘已触发该位 且 {word}价 > 触发价"),
            "t3_touch_live": None,
            "t3_touch_live_reason": lt["t3_reason"],
            "vol_ratio_live_reason": lt["vol_reason"],
            "source": (PREMARKET_SOURCE if live_kind == "premarket"
                       else "yfinance 当日未完成日线 bar（与历史同一 series，口径一致）"),
            "price_caliber": (PREMARKET_CALIBER if live_kind == "premarket"
                              else "拆股已还原·股息未复权（同 close/hi52/hi20）"),
            # 人读分支的标题/列名/读法逐字取自这里（JSON 等价规则：正文有的字段里都有）
            "label": word,
            "title": f"⏳ {word}活价 · 未确认" + ("（仅供参考）" if live_kind == "premarket" else ""),
            "reference_note": PREMARKET_REFERENCE_NOTE if live_kind == "premarket" else None,
            "price_column": "盘前价" if live_kind == "premarket" else "盘中活价",
            "chg_column": "较前收%" if live_kind == "premarket" else "今日至今%",
            "touched_title": f"🔶 {word}触及 · 未确认",
            # 盘前那轮的全表标题（SKILL.md 完整版 ⏳ 节的底稿）；盘中不出全表 → null
            "table_title": ("📋 全部有盘前价的美股标的（含未触发者；52w/20D回撤、距T1/T2、均线均为盘前价 vs 冻结阈值）"
                            if live_kind == "premarket" else None),
            "touched_caveat": ("开盘后可能收回，**不进买入桶、不计入触发数**" if live_kind == "premarket"
                               else "收盘前可能收回，**不进买入桶、不计入触发数**"),
            "reading": ("盘前价仅为未确认读数：须今日开盘后以收盘确认才构成触发"
                        "（若**今日以此价收盘**将触及所列触发位；盘前未确认，非已触发）"
                        if live_kind == "premarket" else
                        "若**今日以此价收盘**将触及所列触发位（盘中未确认，非已触发）。"),
            "caliber_note": (f"现价为{'今日盘前最后一笔成交（看成交时刻与距今分钟数）' if live_kind == 'premarket' else '盘中活价'}；"
                             f"52周高/20日高/T1触发价/T2触发价/均线/量比 一律为最近完整交易日之值，"
                             f"{word}不重算（避免半根 K 线把阈值本身挪走）。"),
            "threshold_comparable": {
                "value": False,
                "reason": (lt["threshold_reason"] if live_kind == "premarket"
                           else "T1/T2/T3 阈值标定在完整交易日收盘上，活价触及为未确认"),
            },
            "counts_toward": {"numerator": False, "denominator": False},
            "prohibitions": list(lt["prohibitions"]) + [
                "本清单与 triggered/untriggered 不得合并计数，也不得互相顶替",
            ],
        },
        # 美股盘前背景（QQQ/SMH/SOXX）与盘前层的取数元数据；不在窗口内时 window=false
        "premarket": premarket,
        "counts": {
            "universe": len(universe),
            "selected": len(selected),
            "evaluated": len(evaluated),
            "triggered": len(triggered),
            "untriggered": len(untriggered),
            "insufficient_history": len(insufficient),
            "source_unavailable": len(src_unavail),
            "insufficient_history_too_young": len(too_young),
        },
        "params": {
            "min_history_bars": MIN_HISTORY_BARS, "rsi_n": RSI_N,
            "lookback_52w": LOOKBACK_52W, "lookback_20d": LOOKBACK_20D,
            "t1_ratio": T1_RATIO, "t2_ratio": T2_RATIO, "t3_rsi": T3_RSI,
        },
        "sources": {
            "universe": rel_path(weekly / "assets" / "universe.json"),
            "industry_table": rel_path(weekly / "assets" / "baseline.md"),
            "hk_quote": rel_path(weekly / "scripts" / "hk_quote.py"),
            # 不是字面量：由本轮实际产出行的档位统计推导。
            # yfinance 整片不可达时仍写「yfinance 本地计算」，就是把一次全回退的运行
            # 记成一次干净的 yfinance 运行——正是 ok/provenance 不得写死的那条规则。
            "prices": _prices_source_label(rows),
            "prices_fallback_order": {
                "US": ["yfinance(auto_adjust=False)", "stockanalysis.com(c·拆股已还原·股息未复权)"],
                "KR": ["yfinance(auto_adjust=False)", "naver siseJson(拆股已还原·股息未复权)"],
                "HK": ["yfinance(auto_adjust=False) + hk_quote.py 覆写价格"],
                "INDEX": ["yfinance(auto_adjust=False)（无替代档，取不到即 N/A）"],
            },
            "prices_tier_by_ticker": {r["ticker"]: r.get("bars_source")
                                      for r in rows if r.get("bars_source")},
            "transport": ("yfinance + requests.Session(浏览器UA)" if _SESSION_OK
                          else "yfinance：部分或全部调用未能使用 requests.Session(浏览器UA)，已退回自管引擎（哪一处见 degraded_reasons）")
                         + "（**仅描述 Yahoo 传输层，不涵盖回退档；回退档见 prices/bars_source**）",
            **pm_sources,
        },
        # 人读分支打出来的每一条口径/禁令都必须同时是字段（--json 不得少于正文）
        "disclaimers": [
            "口径：T1触发价=52周高×0.85；T2触发价=20日高×0.92；RSI14 即 T3 当前值（≤35 触发）。"
            "✅=已触发，—=未触发，N/A=数据不足暂不判定。",
            "补充观察 · 短中期均线（不改变 T1/T2/T3 与分桶）",
            f"判定口径：可用日线 < {MIN_HISTORY_BARS} 根（约半年）即自动排除，不硬编码代码名单。",
            "所有 yfinance 派生字段标注「yfinance 本地计算」；港股价格字段以 hk_quote.py 为准。",
        ],
    }
    return apply_degraded(result), False


class _ArgParser(argparse.ArgumentParser):
    """argparse 默认把**用法错误**回 2，而本仓库 2 保留给「依赖缺失」。

    不覆写就会撞码：2026-09-11 起 load_deps() 缺依赖回 2，于是「旗标拼错」和
    「yfinance 没装」在调度层（SKILL.md 第二步逐单元收 .rc）读起来一模一样，
    而两者的处置完全不同——一个是改命令，一个是补装依赖。
    姊妹技能 daily-risk-monitor/scripts/market.py 有同一处未修的撞码（它只改了
    语义校验那几支的 sys.exit(1)，没覆写 argparse 自身的 error 路径）。
    """

    def error(self, message):
        self.print_usage(sys.stderr)
        err(f"错误：参数错误——{message}")
        sys.exit(1)   # 参数错误=1（2 保留给依赖缺失）


def main():
    ap = _ArgParser(
        description="个股技术面 + 宏观利率取数（T1/T2/T3 触发判定 · yfinance 本地计算）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="标的清单读自姊妹技能 ai-industry-weekly 的 assets/universe.json；"
               f"其位置可用环境变量 {WEEKLY_ENV} 覆盖。",
    )
    ap.add_argument("--json", nargs="?", const="-", metavar="OUT.json",
                    help="输出 JSON；带文件名则写文件，不带则打到 stdout")
    ap.add_argument("--tickers", metavar="A,B,C", help="只跑这些标的（逗号分隔）")
    ap.add_argument("--macro-only", action="store_true", help="只出宏观利率/大盘背景（驱动源判定输入）")
    ap.add_argument("--no-earnings", action="store_true", help="跳过下次财报日查询（省 N 次请求）")
    ap.add_argument("--no-live-overlay", action="store_true",
                    help="不附加盘中活价叠加层与美股盘前叠加层（并跳过盘前 1m 取数）；"
                         "收盘后本来就不附加，此旗标只影响盘中与美股盘前（04:00–09:30 ET）运行")
    args = ap.parse_args()

    if args.macro_only and args.tickers:
        # 参数错误 = 1。原本回 2，而 2 在本仓库保留给「依赖缺失」——
        # 旗标写冲突会被报成「yfinance 没装」。
        err("错误：--macro-only 与 --tickers 互斥。")
        sys.exit(1)

    result, macro_only = run(args)

    if args.json:
        text = json.dumps(result, ensure_ascii=False, indent=2, default=str)
        if args.json == "-":
            print(text)
        else:
            out = Path(args.json).expanduser()
            # 写盘发生在整轮 yfinance 取数**之后**：这里一旦抛异常，取到的数据就全丢了，
            # 第二步只能整轮重跑。所以先建目录，写不进也只告警——正文照常打到 stdout、
            # 退出码保持 0，用户可以直接把 stdout 重定向存下来。
            try:
                if out.parent and str(out.parent) not in ("", "."):
                    out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(text + "\n", encoding="utf-8")
            except OSError as exc:
                err(f"警告：写入 {out.name} 失败：{scrub(exc)}；"
                    f"结果不丢弃，完整 JSON 已改打到 stdout（可自行重定向保存）。")
                print(text)
            else:
                print(f"已写入 {out.name}（{len(text):,} 字节）")
        return

    print_macro(result["macro"], result.get("premarket"))
    if not macro_only:
        print_report(result)


if __name__ == "__main__":
    # 顶层兜底：裸 traceback 会把 ~/... 的完整绝对路径吐进 stderr，而脚本输出会被
    # 贴进日报正文并推 Slack。任何未预期异常一律折叠成一行中文错误。
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        # 口径与 neocloud_credit_monitor.py / neocloud_credit_lite.py 一致：
        # 先说一句「已中断」，再 exit 130。静默退出会让人以为是脚本自己崩了。
        err("✗ 已中断。")
        sys.exit(130)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - 顶层兜底，刻意兜住一切
        print(f"✗ {SCRIPT_NAME} 执行失败：{type(exc).__name__}: {scrub(exc)}",
              file=sys.stderr)
        sys.exit(1)
