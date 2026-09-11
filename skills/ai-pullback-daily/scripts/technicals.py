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

MARKETS = {
    "US": {"tz": "America/New_York", "close": dt.time(16, 0), "label": "美股"},
    "HK": {"tz": "Asia/Hong_Kong", "close": dt.time(16, 0), "label": "港股"},
    "KR": {"tz": "Asia/Seoul", "close": dt.time(15, 30), "label": "韩股"},
}

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
        if now.time() < meta["close"]:
            incomplete = True
            notes.append(
                f"{meta['label']}运行时点（{now:%H:%M} {meta['tz']}）早于收盘 "
                f"{meta['close']:%H:%M}，当日 K 线未完成，按完整交易日规则取前一日"
            )
        elif ref_frame is not None and last in ref_frame.index and "Volume" in ref_frame.columns:
            try:
                pos = ref_frame.index.get_loc(last)
            except KeyError:
                pos = None
            if pos is not None and pos >= 21:
                cur = float(ref_frame["Volume"].iloc[pos])
                base = float(ref_frame["Volume"].iloc[pos - 20:pos].mean())
                if base > 0 and cur < base * 0.5:
                    incomplete = True
                    notes.append(
                        f"{meta['label']}基准 {US_REF_TICKER if market == 'US' else '标的'} "
                        f"当日量 {cur:,.0f} < 20日均量 {base:,.0f} 的 50%，判为未完成盘中 K 线，剔除退到前一日"
                    )
    if incomplete:
        if len(union_index) < 2:
            return None, notes + ["回退后无可用交易日"]
        return union_index[-2], notes
    return last, notes


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
        "data_lag": False,
        "notes": [],
    }
    if frame is None or asof is None:
        row["notes"].append("无日线数据")
        row["insufficient_history"] = True
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
        row["asof"] = qt.split(" ")[0]
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


# ---------------------------------------------------------------- 输出


def print_macro(macro):
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


def print_report(result):
    asof = result["asof"]
    print("=" * 96)
    print("📊 个股技术面（yfinance 本地计算 · auto_adjust=False）")
    print("=" * 96)
    parts = [f"{MARKETS[m]['label']} {asof[m]}" for m in ("US", "HK", "KR") if asof.get(m)]
    print("数据日期：" + "；".join(parts) if parts else "数据日期：N/A")
    for n in result["asof_notes"]:
        print(f"  · {n}")
    print(f"标的来源：{result['sources']['universe']}（{result['counts']['universe']} 档，按 order）")
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
                note.append(f"放量 {r['vol_ratio']:.2f}x")
            rows.append([t, fnum(r.get("close"), 2), fnum(r.get("dd52_pct"), 1, "%"),
                         fnum(r.get("dd20_pct"), 1, "%"), fnum(r.get("rsi14"), 1), "；".join(note)])
        print_table(["代码", "收盘", "52w回撤%", "20D回撤%", "RSI", "备注"], rows)
    print()

    if result["insufficient_history"]:
        print("⛔ 历史不足，不参与技术面判定（不进任何桶、也不进未触发清单）")
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
    fallback = [r["ticker"] for r in rows if any("回退" in n for n in (r.get("notes") or []))]
    if fallback:
        reasons.append("港股价格回退 yfinance 未复权日线（口径次优）：" + ", ".join(fallback))
    ins = result.get("insufficient_history") or []
    if ins:
        reasons.append(f"历史不足、不参与技术面判定 {len(ins)} 档：" + ", ".join(ins))
    # 触发位为 null＝「数据不足暂不判定」，与 false＝「判过了、没触发」是两回事。
    undecided = [r["ticker"] for r in rows
                 if not r.get("insufficient_history")
                 and any(r.get(k) is None for k in ("t1", "t2", "t3"))]
    if undecided:
        reasons.append(f"T1/T2/T3 中有触发位因数据不足暂不判定（null，非 false）"
                       f" {len(undecided)} 档：" + ", ".join(undecided))

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

    # 引擎回退链（顺序写死，见 CLAUDE.md「Fallback chains — the rule」）：
    #   第1档 requests.Session + 浏览器 UA   第2档 yfinance 默认 curl_cffi 引擎
    # 两档都必须保留，因为两种故障方向相反、互为对方的解药（实测见 make_session() 上方）：
    #   代理环境   curl_cffi 握不完 TLS 隧道  -> 只有第1档能过
    #   IP 被限流  requests.Session 吃 429    -> 只有第2档能过（它复用缓存 cookie、
    #              TLS 指纹不同，限流窗口内仍取得到数）
    # ⚠ 绝不要把这里简化成「只用第1档」：2026-09-11 本脚本一度被改成硬切第1档，那在代理
    # 环境下是对的，但在限流环境下比改之前更差——原本 curl_cffi 能扛的那一轮会变成整片空。
    sess = make_session()

    def _dl(use_session):
        kw = dict(tickers=dl, period="2y", interval="1d", progress=False,
                  auto_adjust=False, group_by="ticker", threads=True)
        if use_session:
            kw["session"] = sess
        return yf.download(**kw)

    raw = None
    try:
        raw = _dl(True)
    except TypeError as exc:           # 可能是「该版本不收 session=」，也可能无关
        if "session" not in str(exc):
            # 与 session= 无关的 TypeError：不得吞掉、更不得重跑整轮下载，
            # 否则真正的 bug 会被记成一次「版本不兼容」降级。
            raise
        _session_unsupported("download")
    except Exception as exc:  # noqa: BLE001 - 第1档任何失败都只降级到第2档，不掀掉整轮
        _engine_fallback(f"第1档 requests.Session 取数抛错（{type(exc).__name__}）")

    # 空表不等于「没有数据」：限流时第1档回空而第2档仍有数，直接当取数失败会误报。
    if raw is None or len(raw) == 0:
        try:
            raw2 = _dl(False)
        except Exception as exc:  # noqa: BLE001
            err(f"✗ 两档引擎均取数失败：{type(exc).__name__}: {scrub(exc)}")
            raw2 = None
        if raw2 is not None and len(raw2) > 0:
            _engine_fallback("第1档 requests.Session 回空表、第2档默认引擎取到数据"
                             "（多半是本机 IP 被 Yahoo 限流，429 只挡得住第1档）")
            raw = raw2
    if raw is None or len(raw) == 0:
        # 取数失败 = 退出码 3（本仓库保留：1 参数错误｜2 依赖缺失｜3 取数失败｜4 量级自检未通过）。
        # 这一支的成因按实测可能性排序：① 出站代理无法完成到 Yahoo 的 TLS 隧道，
        # ② Yahoo 按 UA 限流 429，③ 断网。（2026-09-11 更正：原注释只写 ②，已被实测推翻，
        # 详见 make_session() 上方的传输层矩阵。）三者都是取数失败，不是参数错误。
        # 回 1 会让调度层（SKILL.md 第二步逐单元收 .rc）把一次限流读成「命令写错了」。
        err("错误：yfinance 未返回任何日线数据（全部标的皆空）。常见成因按可能性排序："
            "① 出站代理无法完成到 Yahoo 的 TLS 隧道（本脚本已改用 requests.Session+浏览器UA "
            "绕开 curl_cffi 指纹；若仍失败，代理可能连 requests 也拦）；"
            "② Yahoo 按 UA 限流 429；③ 断网或代码全错。"
            "区分方法：带浏览器 UA 直连 query1.finance.yahoo.com/v8/finance/chart/NVDA，"
            "拿到 200 说明 ①②皆不成立，问题在本脚本；连不上则看是握手断还是 429。")
        err("     本次不写 tech.json——下游 perp_quotes.py 的 --spot 因此拿不到现货基准，"
            "🌙 盘后隐含只能标 ⚪️，**不得拿别处价格凑数**。")
        sys.exit(3)

    frames = {t: frame_for(raw, t) for t in dl}
    missing = [t for t, f in frames.items() if f is None]

    # ---- 各市场的完整交易日（美股用 SMH 基准；港股/韩股按自己市场单独判定）
    # 传输层降级（yfinance 不收 session= -> 退回 curl_cffi）必须进 asof_notes：
    # ⚠ 前缀让 apply_degraded() 把它转成 degraded_reasons 字段，--json 才不会少于正文。
    asof, asof_notes = {}, list(_ENGINE_NOTES)
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
    if asof.get("US") is None:
        # 同上：基准标的取不到日线是**取数失败**，不是参数错误 → 3。
        err(f"错误：无法确定美股完整交易日（基准 {US_REF_TICKER} 无数据）——"
            f"取数失败。成因排序同上：代理 TLS 隧道 > UA 限流 429 > 断网，"
            f"详见 make_session() 上方的传输层矩阵。")
        sys.exit(3)
    if missing:
        asof_notes.append("yfinance 未返回数据的标的（记 N/A，不估算）：" + ", ".join(missing))

    macro = build_macro(frames, asof["US_ts"])
    if args.macro_only:
        result = {
            "generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "mode": "macro-only",
            "asof": {k: v for k, v in asof.items() if not k.endswith("_ts")},
            "asof_notes": asof_notes,
            "macro": macro,
            # 顶层 indices：perp_quotes.py --spot 只展开顶层容器键，^GSPC/^NDX 必须放这一层
            "indices": macro.get("indices", {}),
            "sources": {
                "prices": "yfinance 本地计算（auto_adjust=False）",
                "transport": ("yfinance + requests.Session(浏览器UA)" if _SESSION_OK
                              else "yfinance：部分或全部调用未能使用 requests.Session(浏览器UA)，已退回自管引擎（哪一处见 degraded_reasons）"),
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
        rows.append(compute_row(meta, frames.get(t), asof.get(mkt + "_ts"), mkt,
                                earnings_cals, want_earnings=want_earn))

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
                r["notes"].append("hk_quote 报价不可用作收盘价，本行价格回退 yfinance 未复权日线")
            else:
                r["notes"].append("hk_quote 未返回本标的，价格回退 yfinance 未复权日线")

    for r in rows:
        finalize_triggers(r)

    evaluated = [r for r in rows if not r.get("insufficient_history")]
    untriggered = [r["ticker"] for r in evaluated
                   if r.get("t1") is not True and r.get("t2") is not True and r.get("t3") is not True]
    triggered = [r["ticker"] for r in evaluated if r.get("triggered") is True]
    insufficient = [r["ticker"] for r in rows if r.get("insufficient_history")]

    result = {
        "generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "mode": "full",
        "asof": {k: v for k, v in asof.items() if not k.endswith("_ts")},
        "asof_notes": asof_notes,
        "macro": macro,
        # 顶层 indices：perp_quotes.py --spot 只展开顶层容器键，^GSPC/^NDX 必须放这一层
        "indices": macro.get("indices", {}),
        "tickers": rows,
        "earnings": earnings,
        "triggered": triggered,
        "untriggered": untriggered,
        "insufficient_history": insufficient,
        "counts": {
            "universe": len(universe),
            "selected": len(selected),
            "evaluated": len(evaluated),
            "triggered": len(triggered),
            "untriggered": len(untriggered),
            "insufficient_history": len(insufficient),
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
            "prices": "yfinance 本地计算（auto_adjust=False，原始未复权）",
            "transport": ("yfinance + requests.Session(浏览器UA)" if _SESSION_OK
                          else "yfinance：部分或全部调用未能使用 requests.Session(浏览器UA)，已退回自管引擎（哪一处见 degraded_reasons）"),
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

    print_macro(result["macro"])
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
