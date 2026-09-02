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
    global pd
    try:
        import numpy  # noqa: F401  # 只做依赖存在性检查（pandas 计算依赖它）
        import pandas as _pd
        import yfinance as _yf
    except ImportError as exc:  # pragma: no cover
        # exc.name 在「库自身的传递依赖缺失」等情况下会是 None，
        # 早先直接内插会打出「缺少依赖 None」——等于什么都没说。
        missing = getattr(exc, "name", None) or "yfinance / pandas / numpy 之一"
        err(f"错误：缺少依赖 {missing}（{scrub(exc)}）。请先 `pip install yfinance pandas numpy`。")
        sys.exit(1)
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
    return _yf


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


def next_earnings(yf, ticker, asof_date):
    """下次财报日；取不到就 None（N/A，不估算）。"""
    try:
        ed = yf.Ticker(ticker).get_earnings_dates()
    except Exception:
        return None
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


def compute_row(meta, frame, asof, market, yf, want_earnings):
    """在该标的自己的取数日期上算全部指标。缺失一律 None（= N/A）。"""
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
        row["next_earnings"] = next_earnings(yf, ticker, d.date())
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
    macro["us10y"] = tnx_block

    # --- DXY
    dxy_cur, dxy_prev = series_at(frames.get(DXY_TICKER), asof_us)
    macro["dxy"] = {"ticker": DXY_TICKER, "close": rnd(dxy_cur, 4),
                    "chg_pct": rnd(pct_from(dxy_cur, dxy_prev), 3)}

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
         "—", "美元指数（影响非美计价标的）"],
    ]
    print_table(["指标", "收盘/当前", "日变动", "近5日变动", "说明"], rows)
    print(f"折现率信号：{y['signal']}"
          f"（口径：日≥+10bp 或 5日≥+25bp → 🔺；日≤−10bp 或 5日≤−25bp → 🔻；其余 ➖；缺数据 ⚪）")
    print()
    b = macro["backdrop"]
    rows = [[t, fnum(b[t]["close"], 2), fsign(b[t]["chg_pct"], 2, "%")] for t in BACKDROP_TICKERS]
    print_table(["标的", "收盘", "日涨跌%"], rows)
    print(f"板块相对强弱 = SMH − QQQ = {fsign(macro['sector_rel_strength_pt'], 2, 'pt')}"
          f"（{macro['sector_rel_strength_label']}；≥+1.5pt 显著强 / ≤−1.5pt 显著弱）")
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

    raw = yf.download(dl, period="2y", interval="1d", progress=False,
                      auto_adjust=False, group_by="ticker", threads=True)
    if raw is None or len(raw) == 0:
        err("错误：yfinance 未返回任何日线数据（检查网络或标的代码）。")
        sys.exit(1)

    frames = {t: frame_for(raw, t) for t in dl}
    missing = [t for t, f in frames.items() if f is None]

    # ---- 各市场的完整交易日（美股用 SMH 基准；港股/韩股按自己市场单独判定）
    asof, asof_notes = {}, []
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
        err(f"错误：无法确定美股完整交易日（基准 {US_REF_TICKER} 无数据）。")
        sys.exit(1)
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
            "sources": {"prices": "yfinance 本地计算（auto_adjust=False）"},
        }
        return result, True

    # ---- 逐标的计算
    rows = []
    for meta in selected:
        t = meta["ticker"]
        mkt = market_of(t)
        rows.append(compute_row(meta, frames.get(t), asof.get(mkt + "_ts"), mkt, yf,
                                want_earnings=not args.no_earnings))

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
        },
    }
    return result, False


def main():
    ap = argparse.ArgumentParser(
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
        err("错误：--macro-only 与 --tickers 互斥。")
        sys.exit(2)

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
