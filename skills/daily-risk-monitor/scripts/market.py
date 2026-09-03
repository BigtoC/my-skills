#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""抗情绪层 + 趋势 + 周一附加的行情取数（信号 19–22、26、33–34 · yfinance 本地计算）。

口径逐字沿用 references/signals-d-antiemotion.md、references/signals-e-cycle-valuation.md、
references/signals-f-monday.md 与 references/data-cadence.md 的「yfinance 通用取数」骨架，
不做任何自创改动：

    信号 19  σ倍数    单日1σ = 20日已实现波动率(年化) ÷ √252
                     σ倍数 = |当日涨跌%| ÷ 单日1σ
                     <2σ 正常 ｜ 2–3σ 留意 ｜ ≥3σ 真异常
                     **SPX 与 BTC 分开算**（波动率差 2–3 倍，不能共用标尺）
    信号 20  VRP     = VIX − 20日已实现波动率(RV20)
                     <0 转负 = 恐慌是真的 ｜ >15 = 恐慌过头
                     **VIX 首选 FRED VIXCLS**（subprocess 调同目录 fred.sh），
                     与信号 4 期限结构、7 项硬阈值第 1 项「VIX >25 连续 3 日」同源同日；
                     FRED 取不到才回退 yfinance `^VIX`，且回退必在 warnings 里写明不同源。
    信号 21  跨资产   仅在 SPY 单日跌 >1% 时判定，否则「不适用」
                     🟢 股跌、债金涨 ｜ 🔴 股债金同跌（计为 Tier 1 触发）
    信号 22  广度     RSP 当日% − SPY 当日%；|差| <0.5pt = 广度一致
    信号 26  趋势     价 vs 200DMA 偏离%；200DMA 20日斜率 = MA200(今)/MA200(20日前) − 1
                     🟢 价在上方 **且** 斜率向上 ｜ 🔴 价在下方 **且** 斜率向下 ｜ 🟡 只成立一个
                     另输出 above_200dma / slope_positive 两个**当日**布尔（snapshot.py 仍在用）。
                     战术层恢复要两个条件：「连续 5 个交易日站稳 200DMA」+「200DMA 斜率转正」。
                     前者是**纯历史计算**（每根收盘 vs 该根**当日**的 200DMA），本脚本已直接
                     从日线历史算出 above_200dma_streak / streak_meets_5，不需要状态档累积；
                     后者的跨日确认仍看 snapshot.py（本脚本只给当日的 slope_positive）。
    信号 33  DXY     yfinance DX-Y.NYB
    信号 34  Gold/SPX yfinance GC=F ÷ ^GSPC

硬约束（弄错会直接改变判定，改代码前先读 references/known-traps.md）：
  * **必须用 `requests.Session` + UA**——urllib 走 yfinance 会 SSL 验证失败。
    注意这条与 FRED **相反**：FRED 必须用 `curl`，python `requests` 在该环境会超时。
    所以本脚本只负责 yfinance 那一半，FRED 那一半在 scripts/fred.sh。
  * **不要用 yfinance 取 `^VIX3M` / `^VIX9D` / `^VIX6M`**——三个序列全部停更，
    而 `^VIX` 是当日的，拿它们算期限结构会静默地用几周前的远月值比今天的近月值，
    **而信号 4 是 Tier 1**。信号 4 的期限结构改由 `scripts/fred.sh VXVCLS` 提供，
    本脚本一律不抓，也不代算（`--signals 4` 会明确告诉你去哪里拿）。
  * 信号 33 的备援 FRED `DTWEXBGS` 是广义美元指数，**量级不同不可混用**；
    DX-Y.NYB 取不到时本脚本只标 N/A，绝不换源顶替。
  * **缺失一律 N/A，不估算。** 一个看起来合理的数字比一个明显的空格危险得多。
  * 单位/量级自检见 MAGNITUDE_CHECKS；算出来量级不对先怀疑单位，不要直接报出来。
    （净流动性 5–7兆｜HY OAS 2–10%｜Sahm −1–2｜Buffett 50–250%｜CAPE 5–50
     这五项不在本脚本，归 fred.sh 与长期估值取数。）

分档取值全部来自 references，唯一一处由实测基线推出的是信号 20 的 🟡 带：
原文只给了 <0 与 >15 两个硬阈值，而实测基线 +0.5 被标为 ⚠️「已经贴着零轴」，
故本脚本把 0 ≤ VRP ≤ 2 渲染为 🟡「贴近零轴」。阈值本身没有被改动。

用法:
    python3 scripts/market.py                       # 全部（19,20,21,22,26,33,34）
    python3 scripts/market.py --signals 19,20,21    # 只跑子集
    python3 scripts/market.py --json out.json       # 写 JSON 文件
    python3 scripts/market.py --json                # JSON 打到 stdout
    python3 scripts/market.py --period 3y           # 拉长历史（默认 2y）

依赖: python3 + yfinance + pandas + numpy + requests，需要外网。
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_NAME = Path(__file__).name
SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_ROOT = Path(__file__).resolve().parent.parent

# 信号 20 的 VIX 首选 FRED VIXCLS，与信号 4／硬阈值 1 同源同日（见 calc_20 的档头注释）。
FRED_SH = SCRIPT_DIR / "fred.sh"
FRED_TIMEOUT = 60
# 两个 VIX 源读数差多少就要在 warnings 里标出来（点）。这不是判定阈值，
# 只是「同一份报告里两个 VIX 不一致」的提示线，调它不影响任何分档。
VIX_SOURCE_DIFF_TOL = 0.5

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

_HOMEISH_RE = re.compile(r"(?:/Users|/home|/var/folders)/[^/\s\"']+")

pd = None  # 延迟导入，见 load_deps()
np = None

# yfinance 自己 logger 吐的英文噪声先收进这里，不直接进 stderr（会混进日报正文）；
# 但取数失败时必须把原因原样报出来——「拿不到」和「为什么拿不到」是两件事，
# 后者决定报告里该写「网络问题重试」还是「被限流，本次标 ⚪️ 数据暂缺」。
_YF_LOG: list[str] = []


# ---------------------------------------------------------------- 通用小工具


def _configure_streams() -> None:
    """强制 stdout/stderr 用 UTF-8，避免管道场景下 emoji 档位写不出去。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass


def scrub(text) -> str:
    """把文本里的家目录绝对路径折叠掉，用于错误信息与异常讯息。

    本仓库是 public repo，脚本输出会被贴进日报正文并推 Slack，
    所以任何 f"...{scrub(exc)}" 都必须先过这里。
    """
    s = str(text)
    try:
        home = str(Path.home())
    except Exception:
        home = ""
    if home:
        s = s.replace(home, "~")
    return _HOMEISH_RE.sub("~", s)


def rel_display(path: Path) -> str:
    """技能自身的路径一律相对技能根目录展示；技能外的路径只取文件名。"""
    try:
        return str(Path(path).resolve().relative_to(SKILL_ROOT))
    except Exception:
        return Path(path).name


def err(msg: str = "") -> None:
    print(msg, file=sys.stderr)


def fmt(x, nd: int = 2, suffix: str = "") -> str:
    """数值渲染；None / NaN 一律 N/A（绝不用 0 或估算值顶替）。"""
    if x is None:
        return "N/A"
    try:
        v = float(x)
    except (TypeError, ValueError):
        return "N/A"
    if math.isnan(v) or math.isinf(v):
        return "N/A"
    return f"{v:.{nd}f}{suffix}"


def signed(x, nd: int = 2, suffix: str = "") -> str:
    """带正负号渲染（涨跌幅、偏离、斜率专用）。"""
    if x is None:
        return "N/A"
    try:
        v = float(x)
    except (TypeError, ValueError):
        return "N/A"
    if math.isnan(v) or math.isinf(v):
        return "N/A"
    return f"{v:+.{nd}f}{suffix}"


def arrow(x) -> str:
    """↑ / ↓ / → （对比上次扫描那一列的方向标记）。"""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return "→"
    if math.isnan(v):
        return "→"
    if v > 0:
        return "↑"
    if v < 0:
        return "↓"
    return "→"


# ---------------------------------------------------------------- 信号登记表

# 本脚本负责的信号：编号 → (名称, 需要的代码, 所属板块)
SIGNAL_SPECS = {
    19: ("波动率归一化跌幅（σ 倍数）", ["^GSPC", "BTC-USD"], "抗情绪"),
    20: ("VRP（恐慌溢价）", ["^VIX", "^GSPC"], "抗情绪"),
    21: ("跨资产同步性", ["SPY", "TLT", "GLD", "UUP"], "抗情绪"),
    22: ("下跌广度（等权 vs 市值权）", ["RSP", "SPY"], "抗情绪"),
    26: ("趋势过滤器（SPX vs 200DMA + 斜率）", ["^GSPC"], "周期趋势"),
    33: ("美元指数 DXY", ["DX-Y.NYB"], "周一附加"),
    34: ("Gold / S&P 500 比值", ["GC=F", "^GSPC"], "周一附加"),
}

# 不计入 30 个信号、不参与任何触发计数，因此不进 JSON 的 signals 块
MONDAY_EXTRA = (33, 34)

# 本脚本不负责、但最容易被误当成「yfinance 能拿」的信号 → 明确改道
ELSEWHERE = {
    4: "信号 4 VIX 期限结构：^VIX3M/^VIX9D/^VIX6M 已停更，改由 scripts/fred.sh VXVCLS 提供，本脚本不抓也不代算。",
}

# 量级自检（算出来落在区间外 → 标 ⚠️ 量级可疑，先怀疑单位，不要直接报出来）
MAGNITUDE_CHECKS = {
    "VIX": (5.0, 100.0),
    "RV20(%)": (1.0, 200.0),
    "σ倍数": (0.0, 20.0),
    "200DMA 偏离(%)": (-60.0, 60.0),
    "DXY": (50.0, 200.0),
    "Gold/SPX": (0.05, 5.0),
}

STATE_ORDER = {"🟢": 0, "🟡": 1, "🔴": 2, "⚪️": 3}
TRADING_DAYS = 252.0


def worse(a: str, b: str) -> str:
    """两个档位取较严重的一个（⚪️ 视为最严重：不知道不能当安全）。"""
    return a if STATE_ORDER.get(a, 0) >= STATE_ORDER.get(b, 0) else b


# ---------------------------------------------------------------- 依赖与取数


def load_deps():
    """延迟导入重依赖，让 --help 在没装 yfinance 的环境下也能跑。"""
    global pd, np
    try:
        import numpy as _np
        import pandas as _pd
        import requests  # noqa: F401  # 只做存在性检查，make_session 里再用
        import yfinance as _yf
    except ImportError as exc:
        missing = getattr(exc, "name", None) or "yfinance / pandas / numpy / requests 之一"
        err(f"错误：缺少依赖 {missing}（{scrub(exc)}）。"
            f"请先 `pip install yfinance pandas numpy requests`。")
        sys.exit(1)
    try:
        import logging

        # yfinance 取不到代码时会往 stderr 吐英文噪声（"1 Failed download: ... possibly
        # delisted"、"YFRateLimitError"），这些会混进日报正文。这里改成「收进 _YF_LOG，
        # 不进 stderr」：噪声不出，但失败原因不丢——被限流和网络断线要写成不同的报告。
        class _Collector(logging.Handler):
            def emit(self, record):
                try:
                    _YF_LOG.append(record.getMessage())
                except Exception:
                    pass

        lg = logging.getLogger("yfinance")
        lg.handlers = [_Collector()]
        lg.propagate = False
        lg.setLevel(logging.WARNING)
    except Exception:
        pass
    pd, np = _pd, _np
    return _yf


def make_session():
    """必须是 requests.Session + UA。

    references/known-traps.md：`yfinance via urllib` → SSL 验证失败 →
    必须用 `requests.Session` + UA。这条与 FRED（必须用 curl）方向相反，最容易搞混。
    """
    import requests

    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    return s


def download_closes(tickers: list[str], period: str):
    """拉收盘价矩阵。auto_adjust=False：本脚本的标的都是指数/ETF/期货，
    不需要复权，也避免复权把历史价改掉影响 200DMA 与 RV20。"""
    yf = load_deps()

    def _dl(use_session):
        kw = {"tickers": tickers, "period": period, "interval": "1d",
              "progress": False, "auto_adjust": False}
        if use_session:
            kw["session"] = make_session()
        return yf.download(**kw)

    # 先走 requests.Session（源文档的硬约束：urllib 会 SSL 验证失败）。
    # 但 Yahoo 限流时，裸 requests.Session 比 yfinance 默认的 curl_cffi 更容易被挡
    # ——后者伪装浏览器 TLS 指纹。实测在限流窗口内 session 路径回空表、默认引擎正常。
    # 所以空表不等于「没有数据」，必须换引擎再试一次，否则会把限流误报成取数失败。
    raw = None
    try:
        raw = _dl(True)
    except TypeError:
        err("⚠ 当前 yfinance 不支持 download(session=...)，改用默认引擎。")
    except Exception as exc:
        err(f"⚠ requests.Session 路径取数失败（{type(exc).__name__}），改用默认引擎重试。")

    if raw is None or len(raw) == 0:
        try:
            raw2 = _dl(False)
        except Exception as exc:
            err(f"✗ 两种引擎均取数失败：{type(exc).__name__}: {scrub(exc)}")
            return None
        if raw2 is not None and len(raw2) > 0:
            err("⚠ requests.Session 路径回空表、默认引擎(curl_cffi)取到数据 —— "
                "多半是 Yahoo 限流挡了裸 Session。本次采用默认引擎结果。")
            raw = raw2

    if raw is None or len(raw) == 0:
        return None
    try:
        closes = raw["Close"]
    except Exception:
        return None
    if not hasattr(closes, "columns"):          # 单代码时可能退化成 Series
        closes = closes.to_frame(name=tickers[0])
    return closes


def yf_reasons() -> list[str]:
    """yfinance 收集到的失败原因（去重、scrub、每条截断）。"""
    out: list[str] = []
    for line in _YF_LOG:
        s = " ".join(scrub(line).split())[:300]
        if s and s not in out:
            out.append(s)
    return out


def series_of(closes, sym):
    """取单代码的干净序列（去 NaN）。拿不到返回 None，绝不用别的代码顶替。"""
    if closes is None or sym not in getattr(closes, "columns", []):
        return None
    s = closes[sym].dropna()
    return s if len(s) else None


def as_of(s) -> str | None:
    if s is None or not len(s):
        return None
    try:
        return s.index[-1].strftime("%Y-%m-%d")
    except Exception:
        return str(s.index[-1])[:10]


def last_pct(s):
    """最新一根相对前一根的涨跌%。"""
    if s is None or len(s) < 2:
        return None
    return float(s.iloc[-1] / s.iloc[-2] - 1.0) * 100.0


def pct_change_on(s, date_str: str):
    """指定日期那一根的涨跌%（跨资产比较必须同日，不能各取各的末行）。"""
    if s is None or len(s) < 2 or not date_str:
        return None
    try:
        dates = [d.strftime("%Y-%m-%d") for d in s.index]
    except Exception:
        dates = [str(d)[:10] for d in s.index]
    if date_str not in dates:
        return None
    i = dates.index(date_str)
    if i == 0:
        return None
    return float(s.iloc[i] / s.iloc[i - 1] - 1.0) * 100.0


def pct_change_back(s, bars: int):
    """相对 N 根之前的变化%（趋势方向用，不参与任何阈值判定）。"""
    if s is None or len(s) <= bars:
        return None
    return float(s.iloc[-1] / s.iloc[-1 - bars] - 1.0) * 100.0


def rv20(s):
    """20日已实现波动率（年化 %）。口径逐字照 data-cadence.md 的 vol_block。"""
    if s is None or len(s) < 21:
        return None
    r = s.pct_change()
    v = r.rolling(20).std() * math.sqrt(TRADING_DAYS) * 100.0
    v = v.dropna()
    if not len(v):
        return None
    return float(v.iloc[-1])


def magnitude_flag(name: str, value) -> str | None:
    """量级自检：落在区间外返回一句中文告警，否则 None。"""
    rng = MAGNITUDE_CHECKS.get(name)
    if rng is None or value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(v):
        return None
    lo, hi = rng
    if v < lo or v > hi:
        return f"⚠️ 量级可疑：{name} = {v:.4g}，正常应在 {lo}–{hi}；先怀疑单位，不要直接报出来"
    return None


# -------------------------------------------------- FRED VIXCLS（信号 20 的 VIX 首选源）

# fred.sh 的退出码语义（见 scripts/fred.sh 档头）
_FRED_EXIT_MEANING = {
    1: "参数错误",
    2: "依赖缺失（curl / awk / sed）",
    3: "取数失败（数据暂缺）",
    4: "量级自检不通过",
}


def fred_vixcls() -> tuple[float | None, str | None, str | None]:
    """经 subprocess 调**同目录**的 fred.sh 取 FRED VIXCLS，回 (值, 数据日期, 失败原因)。

    为什么必须绕 fred.sh 而不在本脚本里直接打 FRED：references/known-traps.md 实测
    「FRED 必须用 curl、且**不能加自订 UA**」，python requests 打 FRED 在本环境会超时。
    本脚本只管 yfinance 那一半，FRED 一律走 fred.sh，不另开第二条 FRED 取数路径。

    任何失败（脚本不存在 / 不可执行 / 超时 / 非 0 退出 / JSON 解析不了）都回
    (None, None, 原因) 而**不抛例外**——信号 20 必须能优雅回退到 yfinance ^VIX。
    """
    if not FRED_SH.exists():
        return None, None, f"{rel_display(FRED_SH)} 不存在"
    cmd = [str(FRED_SH), "VIXCLS", "--json"]
    if not os.access(FRED_SH, os.X_OK):
        # 少了执行位时用 bash 兜一手，而不是直接判死
        cmd = ["bash", *cmd]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=FRED_TIMEOUT)
    except subprocess.TimeoutExpired:
        return None, None, f"{rel_display(FRED_SH)} 超时（>{FRED_TIMEOUT}s）"
    except FileNotFoundError:
        return None, None, f"{rel_display(FRED_SH)} 不可执行（bash 也找不到）"
    except Exception as exc:  # noqa: BLE001 - 回退路径不得抛
        return None, None, (f"{rel_display(FRED_SH)} 调用失败："
                            f"{type(exc).__name__}: {scrub(exc)}")

    if proc.returncode != 0:
        why = _FRED_EXIT_MEANING.get(proc.returncode, f"未知退出码 {proc.returncode}")
        tail = " ".join(scrub(proc.stderr or "").split())[:200]
        return None, None, (f"{rel_display(FRED_SH)} 退出码 {proc.returncode}（{why}）"
                            + (f"：{tail}" if tail else ""))

    try:
        data = json.loads(proc.stdout)
        block = next((x for x in (data.get("series") or []) if x.get("id") == "VIXCLS"), None)
        latest = (block or {}).get("latest") or {}
        value, date = latest.get("value"), latest.get("date")
        if value is None or date is None:
            return None, None, f"{rel_display(FRED_SH)} 回传里没有 VIXCLS 的最新有值观测"
        return float(value), str(date), None
    except Exception as exc:  # noqa: BLE001 - 回退路径不得抛
        return None, None, (f"{rel_display(FRED_SH)} 输出解析失败："
                            f"{type(exc).__name__}: {scrub(exc)}")


# ---------------------------------------------------------------- 各信号计算


def sigma_block(s, label: str) -> dict:
    """信号 19 的单个标的块：当日涨跌% / RV20 / 单日1σ / σ倍数。"""
    v = rv20(s)
    chg = last_pct(s)
    one_sigma = (v / math.sqrt(TRADING_DAYS)) if v is not None else None
    mult = None
    if chg is not None and one_sigma:
        mult = abs(chg) / one_sigma
    if mult is None:
        state, verdict = "⚪️", "数据暂缺"
    elif mult >= 3.0:
        state, verdict = "🔴", "≥3σ 真异常，值得查原因"
    elif mult >= 2.0:
        state, verdict = "🟡", "2–3σ 留意"
    else:
        state, verdict = "🟢", "<2σ 正常波动"
    warns = [w for w in (magnitude_flag("RV20(%)", v), magnitude_flag("σ倍数", mult)) if w]
    return {
        "标的": label,
        "当日涨跌%": chg,
        "RV20年化%": v,
        "单日1σ%": one_sigma,
        "σ倍数": mult,
        "state": state,
        "判定": verdict,
        "as_of": as_of(s),
        "warnings": warns,
    }


def calc_19(closes) -> dict:
    spx = sigma_block(series_of(closes, "^GSPC"), "SPX (^GSPC)")
    btc = sigma_block(series_of(closes, "BTC-USD"), "BTC (BTC-USD)")
    state = worse(spx["state"], btc["state"])
    return {
        "id": 19, "name": SIGNAL_SPECS[19][0], "group": SIGNAL_SPECS[19][2],
        "state": state,
        "detail": {"SPX": spx, "BTC": btc},
        "as_of": spx["as_of"] or btc["as_of"],
        "note": "SPX 与 BTC 分开算（波动率差 2–3 倍，不能共用标尺）；"
                "本信号档位取两者较严重的一档。σ 是相对标尺不是概率，市场肥尾，"
                "3σ 出现频率远高于常态分布。",
        "warnings": spx["warnings"] + btc["warnings"],
        "强制归因": (
            "触发（≥2.5σ）" if any(
                (b["σ倍数"] is not None and b["σ倍数"] >= 2.5) for b in (spx, btc)
            ) else "未触发（<2.5σ）"
        ),
    }


def calc_20(closes) -> dict:
    """信号 20 VRP。

    VIX **首选 FRED VIXCLS**，与信号 4（VIX 期限结构）和 7 项硬阈值第 1 项
    「VIX >25 且连续 3 个交易日」同源同日。同一份报告里出现两个 VIX，
    不只是数字不一致：两者数据日期常常不同（yfinance 当日 vs FRED 滞后一个交易日），
    VRP = VIX(T) − RV20(T−1) 这种跨日相减会静默发生，而信号 4 是 Tier 1。
    所以：FRED 取不到才回退 yfinance ^VIX，且回退与两源不一致都必须写进 warnings。
    """
    spx_s = series_of(closes, "^GSPC")
    yf_vix_s = series_of(closes, "^VIX")
    yf_vix = float(yf_vix_s.iloc[-1]) if yf_vix_s is not None else None
    yf_vix_asof = as_of(yf_vix_s)

    fred_vix, fred_asof, fred_why = fred_vixcls()

    warns: list[str] = []
    if fred_vix is not None:
        vix, vix_as_of, vix_source = fred_vix, fred_asof, "FRED VIXCLS"
    else:
        vix, vix_as_of = yf_vix, yf_vix_asof
        vix_source = "yfinance ^VIX" if yf_vix is not None else None
        warns.append(
            "⚠️ VIX 源已回退至 yfinance ^VIX，与信号 4／硬阈值 1 的 FRED VIXCLS 不同源，"
            "两者读数与数据日期可能不一致，报告须注明"
            + (f"（FRED 取数失败：{fred_why}）" if fred_why else "")
        )

    # 两个源都取到时必须比对——这正是本 bug 要暴露的东西，修好了也不能藏起来。
    if fred_vix is not None and yf_vix is not None:
        diff = fred_vix - yf_vix
        if abs(diff) > VIX_SOURCE_DIFF_TOL:
            warns.append(
                f"⚠️ 两个 VIX 源读数不一致：FRED VIXCLS {fred_vix:.2f}"
                f"（{fred_asof or 'N/A'}）vs yfinance ^VIX {yf_vix:.2f}"
                f"（{yf_vix_asof or 'N/A'}），差 {diff:+.2f} 点（提示线 ±{VIX_SOURCE_DIFF_TOL}）；注意两者数据日期不同时这个差多半只是日期错位、并非源本身分歧；本信号采用 FRED，报告须注明"
            )
        if fred_asof and yf_vix_asof and fred_asof != yf_vix_asof:
            warns.append(
                f"⚠️ 两个 VIX 源数据日期不同：FRED VIXCLS {fred_asof} vs "
                f"yfinance ^VIX {yf_vix_asof}（FRED 通常滞后一个交易日）；"
                f"本信号采用 FRED {fred_asof}，与信号 4／硬阈值 1 同日"
            )

    # VRP 两条腿必须同日。VIX 走 FRED 后通常滞后 1–2 个交易日，而 SPX 是当日，
    # 直接相减就是跨日 —— 实测偏差 0.2 点以上，而阈值是 VRP<0，零轴附近足以翻转档位。
    # 所以把 SPX 截断到 VIX 的数据日期再算 RV20：VIX 与信号 4／硬阈值 1 同源同日，
    # 同时 VRP 自己的两条腿也同日，两个对齐同时成立。
    spx_leg = spx_s
    if spx_s is not None and vix_as_of:
        try:
            import pandas as _pd
            truncated = spx_s[spx_s.index <= _pd.Timestamp(vix_as_of)]
            if len(truncated) >= 21:          # RV20 至少要 21 根才有意义
                spx_leg = truncated
            else:
                warns.append(
                    f"⚠️ SPX 截断到 VIX 数据日期 {vix_as_of} 后仅剩 {len(truncated)} 根日线，"
                    f"不足以算 RV20；改用未截断序列，VRP 为跨日相减，不可直接对阈值下硬结论"
                )
        except Exception as exc:              # 截断失败不该让整个信号挂掉
            warns.append(f"⚠️ SPX 日期对齐失败（{type(exc).__name__}），VRP 可能为跨日相减")
    v = rv20(spx_leg)
    rv20_as_of = as_of(spx_leg)
    if (vix is not None and v is not None and vix_as_of and rv20_as_of
            and vix_as_of != rv20_as_of):
        # 对齐后仍不同日 = 截断没生效（如 VIX 日在 SPX 序列里没有对应交易日）。
        warns.append(
            f"⚠️ VRP 两条腿数据日期仍不同：VIX（{vix_source}）{vix_as_of} vs "
            f"RV20(^GSPC) {rv20_as_of} —— 这是跨日相减，数值不可直接对阈值下硬结论，"
            f"报告须注明两条腿各自的日期"
        )
    vrp = (vix - v) if (vix is not None and v is not None) else None
    if vrp is None:
        state, verdict = "⚪️", "数据暂缺"
    elif vrp < 0:
        state, verdict = "🔴", "VRP 转负 = 恐慌是真的（实际波动已超过期权定价）"
    elif vrp > 15:
        state, verdict = "🟡", ">15 恐慌过头（保险费远贵于实际风险）"
    elif vrp <= 2:
        # 原文只给 <0 与 >15 两个硬阈值；0–2 的 🟡「贴近零轴」取自实测基线
        # +0.5 被标 ⚠️「已经贴着零轴，缓冲垫几乎没有了」。阈值本身未改动。
        state, verdict = "🟡", "贴近零轴，缓冲垫所剩无几（阈值仍是 <0 才算转负）"
    else:
        state, verdict = "🟢", "恐慌溢价正常"
    return {
        "id": 20, "name": SIGNAL_SPECS[20][0], "group": SIGNAL_SPECS[20][2],
        "state": state,
        "VIX": vix, "RV20年化%": v, "VRP": vrp, "判定": verdict,
        "vix_source": vix_source,
        "vix_as_of": vix_as_of,
        "rv20_as_of": rv20_as_of,
        "vix_fred_vixcls": fred_vix,
        "vix_fred_as_of": fred_asof,
        "vix_yfinance": yf_vix,
        "vix_yfinance_as_of": yf_vix_asof,
        "as_of": vix_as_of or rv20_as_of,
        "note": "VRP = VIX − 20日已实现波动率。触发：<0 转负 = 恐慌是真的｜>15 = 恐慌过头。"
                "VIX 首选 FRED VIXCLS，与信号 4 期限结构、硬阈值第 1 项「VIX >25 连续 3 日」"
                "同源同日；FRED 取不到才回退 yfinance ^VIX，回退必在 warnings 明示。",
        "warnings": warns + [w for w in (magnitude_flag("VIX", vix),
                                         magnitude_flag("RV20(%)", v)) if w],
    }


def calc_21(closes) -> dict:
    spy_s = series_of(closes, "SPY")
    anchor = as_of(spy_s)
    legs = {}
    for sym, label in (("SPY", "股 SPY"), ("TLT", "债 TLT"), ("GLD", "金 GLD"), ("UUP", "美元 UUP")):
        s = series_of(closes, sym)
        legs[sym] = {
            "标的": label,
            "当日涨跌%": last_pct(s) if sym == "SPY" else pct_change_on(s, anchor),
            "as_of": as_of(s),
        }
    spy = legs["SPY"]["当日涨跌%"]
    tlt, gld = legs["TLT"]["当日涨跌%"], legs["GLD"]["当日涨跌%"]

    applicable = spy is not None and spy < -1.0
    tier1 = False
    if spy is None:
        state, verdict = "⚪️", "数据暂缺：SPY 当日涨跌无法计算"
    elif not applicable:
        # 「不适用」不是数据缺失：查过了，条件没成立。故记 🟢（未触发），
        # 而不是 ⚪️（不知道）——把两者混为一谈正是本技能最忌讳的事。
        state = "🟢"
        verdict = f"不适用（SPY {signed(spy, 2, '%')}，未跌逾 1%，本信号仅在跌 >1% 时判定）"
    elif tlt is None or gld is None:
        state, verdict = "⚪️", "SPY 已跌逾 1%，但 TLT / GLD 当日数据缺失，无法判定"
    elif tlt < 0 and gld < 0:
        state = "🔴"
        verdict = "股债金同跌 = 无差别抛售换现金（流动性事件）→ 计为 Tier 1 触发"
        tier1 = True
    elif tlt > 0 and gld > 0:
        state, verdict = "🟢", "股跌、债金涨 = 正常 risk-off 轮动，市场功能正常"
    else:
        state = "🟡"
        rise = "、".join(
            n for n, x in (("债 TLT", tlt), ("金 GLD", gld)) if x is not None and x > 0
        ) or "无"
        verdict = (f"混合：仅 {rise} 上涨——非原文定义的两档（🟢 债金同涨 / 🔴 股债金同跌）之一，"
                   f"记 🟡 待观察，不作 Tier 1 触发")
    return {
        "id": 21, "name": SIGNAL_SPECS[21][0], "group": SIGNAL_SPECS[21][2],
        "state": state, "适用": applicable, "判定": verdict,
        "Tier1触发": tier1,
        "腿": legs, "as_of": anchor,
        "note": "仅在 SPY 单日跌 >1% 时判定；🔴 股债金同跌计为 Tier 1 触发，"
                "且属于「任一极端触发」→ 风险等级直接 🔴。UUP 只作背景，不参与判定。",
        "warnings": [],
    }


def calc_22(closes) -> dict:
    spy_s, rsp_s = series_of(closes, "SPY"), series_of(closes, "RSP")
    anchor = as_of(spy_s)
    spy = last_pct(spy_s)
    rsp = pct_change_on(rsp_s, anchor)
    diff = (rsp - spy) if (spy is not None and rsp is not None) else None
    if diff is None:
        state, verdict = "⚪️", "数据暂缺"
    elif abs(diff) < 0.5:
        state, verdict = "🟢", "广度一致（普跌或普涨）"
    elif diff < 0:
        state = "🟡"
        verdict = ("RSP 明显更差 = 真·普跌" if (spy is not None and spy < 0)
                   else "RSP 明显更差 = 涨势集中在巨头，底下没跟上")
    else:
        state = "🟡"
        verdict = ("RSP 明显更好 = 只是巨头在拖" if (spy is not None and spy < 0)
                   else "RSP 明显更好 = 底下的公司涨得更整齐")
    return {
        "id": 22, "name": SIGNAL_SPECS[22][0], "group": SIGNAL_SPECS[22][2],
        "state": state,
        "SPY当日%": spy, "RSP当日%": rsp, "RSP−SPY(pt)": diff, "判定": verdict,
        "as_of": anchor,
        "note": "上下文信号，不报警：只解释「是 500 家一起跌，还是几只巨头在跌」，"
                "所以最重档只到 🟡。",
        "warnings": [],
    }


MA200_WINDOW = 200
STREAK_NEED = 5


def above_200dma_streak(s, window: int = MA200_WINDOW, need: int = STREAK_NEED):
    """从最新一根往回数，连续有多少根收盘 > **该根当日**的 200DMA。回 (天数, 说明)。

    这是一个**纯历史计算**，不需要任何跨日状态档：yfinance 的 2 年日线里
    每一根都能算出它自己当日的 MA200。靠 last_run.json 逐日累积的老做法，
    只要漏跑一天 / 状态档损坏 / 首次运行，就永远答不出这个条件。

    关键：每根必须用**它自己当日**的 MA200 比较。拿今天的 MA200 去比历史收盘是错的
    ——上升趋势里今天的均线更高，会把当年其实已站上均线的日子误判成没站上（反之亦然）。

    历史不足 window + need 根时回 (None, 原因)：**绝不默认成 0，也绝不视同已满足**。
    """
    n = 0 if s is None else len(s)
    if s is None or n < window + need:
        return None, (f"历史不足：判定「连续 {need} 个交易日站稳 {window}DMA」"
                      f"至少需要 {window + need} 根日线，实际 {n} 根 → 无法判定"
                      f"（不填 0，也不视同已满足；需要更长历史请加 --period）")
    m = s.rolling(window).mean()
    streak = 0
    truncated = False               # 是否数到历史尽头才停（而不是遇到跌破才停）
    for i in range(len(s) - 1, -1, -1):
        try:
            ma = float(m.iloc[i])
        except (TypeError, ValueError):
            truncated = True
            break
        if math.isnan(ma):          # 前 window−1 根没有 MA200，数到这里就停
            truncated = True
            break
        if float(s.iloc[i]) > ma:
            streak += 1
        else:
            break                   # 真的跌破 → streak 是确定值
    else:
        truncated = True            # 一路数到序列开头都没跌破
    if truncated:
        # 数到 MA200 暖机边界/序列开头才停 —— streak 是**下界**不是确定值。
        # 把下界说成事实，与本技能「绝不编数字」同属一类错误。
        note = (f"至少连续 {streak} 个交易日站稳（需 ≥{need}）"
                f"——已数到可得历史的尽头，真实天数 ≥{streak}；加 --period 可回溯更久")
    elif streak >= need:
        note = f"已连续 {streak} 个交易日站稳（需 ≥{need}）"
    else:
        note = f"已连续 {streak} 个交易日站稳，距 {need} 日还差 {need - streak} 个"
    return streak, note


def calc_26(closes) -> dict:
    s = series_of(closes, "^GSPC")
    price = ma200 = dev = slope = None
    if s is not None and len(s) >= 200:
        m = s.rolling(200).mean().dropna()
        if len(m):
            price = float(s.iloc[-1])
            ma200 = float(m.iloc[-1])
            dev = (price / ma200 - 1.0) * 100.0
            if len(m) > 20:
                slope = (float(m.iloc[-1]) / float(m.iloc[-1 - 20]) - 1.0) * 100.0
    above = (dev is not None and dev > 0)
    up = (slope is not None and slope > 0)
    # 「连续 5 个交易日站稳 200DMA」直接从日线历史算出来，不靠状态档累积（见函数档头）。
    streak, streak_note = above_200dma_streak(s)
    if dev is None or slope is None:
        state, regime = "⚪️", "数据暂缺（200DMA 或 20日斜率算不出，历史不足）"
    elif above and up:
        state, regime = "🟢", "多头机制（价在 200DMA 上方 且 斜率向上）"
    elif (not above) and (not up):
        state, regime = "🔴", "空头机制（价在下方 且 斜率向下）"
    else:
        state, regime = "🟡", "过渡（价与斜率两个条件只成立一个）"
    return {
        "id": 26, "name": SIGNAL_SPECS[26][0], "group": SIGNAL_SPECS[26][2],
        "state": state,
        "SPX": price, "MA200": ma200, "偏离%": dev, "MA200_20日斜率%": slope,
        "价在200DMA上方": above if dev is not None else None,
        "斜率向上": up if slope is not None else None,
        # 同样两个布尔，另给一组 ASCII 键名（snapshot.py 仍在读，别改名别删）。
        # 取不到（历史不足）时为 null，snapshot.py 必须把 null 当「今天不算站稳」，
        # 不得跳过、也不得视同 true（那会凭空补满 5 天）。
        "above_200dma": above if dev is not None else None,
        "slope_positive": up if slope is not None else None,
        # 「连续 5 个交易日站稳 200DMA」的**完整答案**，直接从日线历史算出：
        # 每根收盘 vs 该根**当日**的 200DMA，从最新一根往回数。不需要 last_run.json，
        # 漏跑一天 / 状态档损坏 / 首次运行都不影响。历史不足时为 null + streak_note 说明，
        # 绝不填 0、也绝不视同已满足。
        "above_200dma_streak": streak,
        "streak_meets_5": (None if streak is None else streak >= STREAK_NEED),
        "streak_note": streak_note,
        "机制": regime,
        "as_of": as_of(s),
        "note": "双条件设计：只看价格跌破会被反复洗，加「均线斜率也转负」做二次确认。"
                "斜率是战术层 🔴 组合 A 的第二个条件，不可省。"
                "战术层 🔴/🟡 → 🟢 的恢复需要两个条件：「连续 5 个交易日站稳 200DMA」"
                "+「200DMA 斜率转正」。前者本脚本给出完整答案"
                "（above_200dma_streak / streak_meets_5，纯历史计算，不依赖状态档）；"
                "后者本脚本只给当日值 slope_positive，其跨日确认仍看 snapshot.py。",
        "warnings": [w for w in (magnitude_flag("200DMA 偏离(%)", dev),) if w],
    }


def calc_33(closes) -> dict:
    s = series_of(closes, "DX-Y.NYB")
    v = float(s.iloc[-1]) if s is not None else None
    return {
        "id": 33, "name": SIGNAL_SPECS[33][0], "group": SIGNAL_SPECS[33][2],
        "DXY": v, "当日%": last_pct(s),
        "5日%": pct_change_back(s, 5), "20日%": pct_change_back(s, 20),
        "as_of": as_of(s),
        "note": "周一附加，不计入 30 个信号、不参与任何触发计数，因此无档位。"
                "备援 FRED DTWEXBGS 是广义美元指数，量级不同不可混用——"
                "本项取不到就标 N/A，不换源顶替。",
        "warnings": [w for w in (magnitude_flag("DXY", v),) if w],
    }


def calc_34(closes) -> dict:
    g, spx = series_of(closes, "GC=F"), series_of(closes, "^GSPC")
    anchor = as_of(spx)
    ratio = gold = idx = None
    if g is not None and spx is not None:
        gold, idx = float(g.iloc[-1]), float(spx.iloc[-1])
        if idx:
            ratio = gold / idx
    r20 = r60 = None
    if g is not None and spx is not None:
        try:
            rs = (g / spx).dropna()
            r20 = pct_change_back(rs, 20)
            r60 = pct_change_back(rs, 60)
        except Exception:
            r20 = r60 = None
    return {
        "id": 34, "name": SIGNAL_SPECS[34][0], "group": SIGNAL_SPECS[34][2],
        "Gold(GC=F)": gold, "SPX(^GSPC)": idx, "Gold/SPX": ratio,
        "比值20日%": r20, "比值60日%": r60,
        "as_of": anchor,
        "note": "周一附加，不计入 30 个信号、不参与任何触发计数，因此无档位。"
                "它非常慢，看的是季度以上的方向，不是当天。",
        "warnings": [w for w in (magnitude_flag("Gold/SPX", ratio),) if w],
    }


CALCULATORS = {19: calc_19, 20: calc_20, 21: calc_21, 22: calc_22,
               26: calc_26, 33: calc_33, 34: calc_34}


# ---------------------------------------------------------------- 报告渲染


def print_report(result: dict) -> None:
    meta = result["meta"]
    print(f"# 行情取数（yfinance）· {meta['generated_at']}")
    print(f"信号：{'、'.join(str(i) for i in meta['signals'])}"
          f"｜历史窗口 {meta['period']}｜代码 {'、'.join(meta['tickers'])}")
    if meta.get("missing_tickers"):
        print(f"⚠️ yfinance 未返回数据的代码（记 N/A，不估算）：{'、'.join(meta['missing_tickers'])}")
        for line in meta.get("fetch_errors") or []:
            print(f"   yfinance 报告：{line}")
    print("ℹ️ " + ELSEWHERE[4])
    if meta.get("stale_note"):
        print("ℹ️ " + meta["stale_note"])
    print()

    blocks = dict(result["signals"])
    blocks.update(result.get("monday_extra", {}))
    for sid in meta["signals"]:
        d = blocks.get(str(sid))
        if d is None:
            continue
        head = f"## 信号 {sid} · {d['name']}"
        if sid not in MONDAY_EXTRA:
            head += f"　{d['state']}"
        print(head)
        if sid == 19:
            for key in ("SPX", "BTC"):
                b = d["detail"][key]
                print(f"- {b['标的']}：当日 {signed(b['当日涨跌%'], 2, '%')}｜"
                      f"RV20 {fmt(b['RV20年化%'], 1, '%')}｜单日1σ {fmt(b['单日1σ%'], 2, '%')}｜"
                      f"σ倍数 {fmt(b['σ倍数'], 2, 'σ')} → {b['state']} {b['判定']}"
                      f"（as of {b['as_of'] or 'N/A'}）")
            print(f"- 强制归因（≥2.5σ）：{d['强制归因']}")
        elif sid == 20:
            print(f"- VIX {fmt(d['VIX'], 2)} − RV20 {fmt(d['RV20年化%'], 1)} = "
                  f"VRP {signed(d['VRP'], 2)} → {d['判定']}（as of {d['as_of'] or 'N/A'}）")
            print(f"- VIX 源：{d['vix_source'] or 'N/A'}"
                  f"（as of {d['vix_as_of'] or 'N/A'}）｜"
                  f"RV20(^GSPC) as of {d['rv20_as_of'] or 'N/A'}")
            print(f"- 两源对照：FRED VIXCLS {fmt(d['vix_fred_vixcls'], 2)}"
                  f"（{d['vix_fred_as_of'] or 'N/A'}）｜"
                  f"yfinance ^VIX {fmt(d['vix_yfinance'], 2)}"
                  f"（{d['vix_yfinance_as_of'] or 'N/A'}）")
        elif sid == 21:
            for leg in d["腿"].values():
                print(f"- {leg['标的']}：{signed(leg['当日涨跌%'], 2, '%')}"
                      f"（as of {leg['as_of'] or 'N/A'}）")
            print(f"- 判定：{d['判定']}")
            if d["Tier1触发"]:
                print("- 🔴 计为 Tier 1 触发，且属「任一极端触发」→ 风险等级直接 🔴")
        elif sid == 22:
            print(f"- RSP {signed(d['RSP当日%'], 2, '%')} − SPY {signed(d['SPY当日%'], 2, '%')} = "
                  f"{signed(d['RSP−SPY(pt)'], 2, 'pt')} → {d['判定']}（as of {d['as_of'] or 'N/A'}）")
        elif sid == 26:
            print(f"- SPX {fmt(d['SPX'], 2)}｜200DMA {fmt(d['MA200'], 2)}｜"
                  f"偏离 {signed(d['偏离%'], 2, '%')}｜20日斜率 {signed(d['MA200_20日斜率%'], 2, '%')}"
                  f" {arrow(d['MA200_20日斜率%'])}")
            print(f"- 机制：{d['机制']}（as of {d['as_of'] or 'N/A'}）")
            print(f"- 恢复条件用的当日布尔：above_200dma={d['above_200dma']}｜"
                  f"slope_positive={d['slope_positive']}")
            print(f"- 连续站稳 200DMA：{d['above_200dma_streak'] if d['above_200dma_streak'] is not None else 'N/A'}"
                  f" 个交易日｜满足 ≥5：{d['streak_meets_5']}　{d['streak_note']}")
            print("- 战术层恢复要「连续 5 个交易日站稳 200DMA」+「200DMA 斜率转正」两个条件："
                  "前者本脚本已从日线历史直接算出完整答案（不依赖状态档）；"
                  "后者本脚本只给当日值，跨日确认仍看 snapshot.py。")
        elif sid == 33:
            print(f"- DXY {fmt(d['DXY'], 2)}｜当日 {signed(d['当日%'], 2, '%')} {arrow(d['当日%'])}｜"
                  f"5日 {signed(d['5日%'], 2, '%')}｜20日 {signed(d['20日%'], 2, '%')}"
                  f"（as of {d['as_of'] or 'N/A'}）")
        elif sid == 34:
            print(f"- Gold {fmt(d['Gold(GC=F)'], 2)} ÷ SPX {fmt(d['SPX(^GSPC)'], 2)} = "
                  f"{fmt(d['Gold/SPX'], 4)}｜20日 {signed(d['比值20日%'], 2, '%')}｜"
                  f"60日 {signed(d['比值60日%'], 2, '%')}（as of {d['as_of'] or 'N/A'}）")
        for w in d.get("warnings") or []:
            print(f"- {w}")
        print(f"- 说明：{d['note']}")
        print()

    graded = [d for k, d in result["signals"].items()]
    if graded:
        line = "｜".join(f"{d['id']} {d['state']}" for d in sorted(graded, key=lambda x: x["id"]))
        print(f"档位小结（抄进 today.json 的 signals 块；snapshot.py 要求 1–30 齐全）：{line}")
    unknown = [d["id"] for d in graded if d["state"] == "⚪️"]
    if unknown:
        print(f"⚪️ 数据暂缺：信号 {'、'.join(str(i) for i in unknown)}"
              f"——报告里必须写明尝试过的来源与滞后周数，不得填成「未触发」。")


# ---------------------------------------------------------------- 主流程


def parse_signals(raw: str | None) -> list[int]:
    if raw is None:
        return sorted(SIGNAL_SPECS)
    if not raw.strip():
        # 显式给了空的 --signals 就报错，不要静默当成「全部」：
        # 那会让一次本想只跑子集的调用悄悄跑满，多打十次 Yahoo 请求。
        err("错误：--signals 为空。省略该参数才是「跑全部」。")
        sys.exit(2)
    out: list[int] = []
    for chunk in re.split(r"[,\s，、]+", raw.strip()):
        if not chunk:
            continue
        if not chunk.isdigit():
            err(f"错误：--signals 只接受信号编号，实际「{chunk}」。"
                f"本脚本支持：{','.join(str(i) for i in sorted(SIGNAL_SPECS))}。")
            sys.exit(2)
        n = int(chunk)
        if n in ELSEWHERE:
            err(f"错误：{ELSEWHERE[n]}")
            sys.exit(2)
        if n not in SIGNAL_SPECS:
            err(f"错误：信号 {n} 不由本脚本负责。"
                f"本脚本支持：{','.join(str(i) for i in sorted(SIGNAL_SPECS))}"
                f"（其余信号见 scripts/fred.sh 等取数脚本与 references/）。")
            sys.exit(2)
        if n not in out:
            out.append(n)
    if not out:
        err("错误：--signals 为空。")
        sys.exit(2)
    return sorted(out)


def run(signals: list[int], period: str) -> dict:
    tickers: list[str] = []
    for sid in signals:
        for t in SIGNAL_SPECS[sid][1]:
            if t not in tickers:
                tickers.append(t)

    closes = download_closes(tickers, period)
    if closes is None:
        err("错误：yfinance 未返回任何数据。"
            "本次不产出任何数值——宁可整片 N/A，也不用记忆或估算值填充。")
        for line in yf_reasons():
            err(f"  yfinance 报告：{line}")
        err("  若是 Too Many Requests（限流），等几分钟再跑；"
            "仍失败则这几项在报告里写「⚪️ 数据暂缺」+ 尝试过的来源 + 滞后周数。")
        sys.exit(1)

    missing = [t for t in tickers if series_of(closes, t) is None]

    graded: dict[str, dict] = {}
    monday: dict[str, dict] = {}
    for sid in signals:
        block = CALCULATORS[sid](closes)
        (monday if sid in MONDAY_EXTRA else graded)[str(sid)] = block

    # 周末 / 美股假期提示：末行日期不是今天就明说数据是上一交易日
    stale_note = None
    spx_asof = as_of(series_of(closes, "^GSPC")) or as_of(series_of(closes, "SPY"))
    today = datetime.now().strftime("%Y-%m-%d")
    if spx_asof and spx_asof != today:
        stale_note = (f"TradFi 最新交易日为 {spx_asof}（今天 {today}）："
                      f"200DMA / σ倍数 / VRP / 广度 均为上一交易日数据，报告须注明。")

    return {
        "meta": {
            "generated_at": datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S%z"),
            "source": "yfinance (requests.Session + UA)",
            "period": period,
            "signals": signals,
            "tickers": tickers,
            "missing_tickers": missing,
            "fetch_errors": yf_reasons() if missing else [],
            "stale_note": stale_note,
            "excluded": ELSEWHERE[4],
        },
        "signals": graded,
        "monday_extra": monday,
    }


def build_parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(
        prog=SCRIPT_NAME,
        description="抗情绪层 + 趋势 + 周一附加行情取数（信号 19–22、26、33–34 · yfinance）。"
                    "缺失一律 N/A，不估算。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "信号分工：\n"
            "  19 σ倍数 / 20 VRP / 21 跨资产 / 22 广度 / 26 趋势 → 本脚本（yfinance）\n"
            "  33 DXY / 34 Gold-SPX → 本脚本，周一附加，不计入 30 信号\n"
            "  4  VIX 期限结构 → scripts/fred.sh VXVCLS（^VIX3M 已停更，本脚本不抓）\n\n"
            "注意：被 Yahoo 限流（Too Many Requests）时 yfinance 会自己退避重试，\n"
            "单次可能卡上几分钟。卡住通常就是限流，不是网络断——等几分钟再跑，\n"
            "别在同一分钟内连开好几次，那只会把限流拖更久。\n\n"
            "例：\n"
            "  python3 scripts/market.py\n"
            "  python3 scripts/market.py --signals 19,20,21\n"
            "  python3 scripts/market.py --json out.json\n"
        ),
    )


def main(argv: list[str] | None = None) -> int:
    _configure_streams()
    ap = build_parser()
    ap.add_argument("--signals", metavar="19,20,21",
                    help="只跑这些信号（逗号分隔）；默认全部")
    ap.add_argument("--json", nargs="?", const="-", metavar="OUT.json",
                    help="输出 JSON；带文件名则写文件，不带则打到 stdout")
    ap.add_argument("--period", default="2y", metavar="2y",
                    help="yfinance 历史窗口（默认 2y；200DMA + 20日斜率至少需要 220 根）")
    args = ap.parse_args(argv)

    signals = parse_signals(args.signals)
    result = run(signals, args.period)

    if args.json:
        text = json.dumps(result, ensure_ascii=False, indent=2, default=str)
        if args.json == "-":
            print(text)
            return 0
        out = Path(args.json).expanduser()
        # 写盘发生在整轮取数之后：这里抛异常就白跑一趟。写不进也只告警，
        # 完整 JSON 改打 stdout，退出码保持 0，用户可自行重定向。
        try:
            if out.parent and str(out.parent) not in ("", "."):
                out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(text + "\n", encoding="utf-8")
        except OSError as exc:
            err(f"警告：写入 {rel_display(out)} 失败：{scrub(exc)}；"
                f"结果不丢弃，完整 JSON 已改打到 stdout。")
            print(text)
        else:
            print(f"已写入 {rel_display(out)}（{len(text):,} 字节）")
        return 0

    print_report(result)
    return 0


if __name__ == "__main__":
    # 顶层兜底：裸 traceback 会把 ~/... 的完整绝对路径吐进 stderr，而脚本输出会被
    # 贴进日报正文并推 Slack。任何未预期异常一律折叠成一行中文错误。
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        err("✗ 已中断。")
        sys.exit(130)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - 顶层兜底，刻意兜住一切
        print(f"✗ {SCRIPT_NAME} 执行失败：{type(exc).__name__}: {scrub(exc)}",
              file=sys.stderr)
        sys.exit(1)
