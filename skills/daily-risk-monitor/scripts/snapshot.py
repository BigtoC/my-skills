#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""昨日对照的滚动状态（第 0 步 · assets/last_run.json 的读取、对比与覆写）。

为什么存在
----------
原文第 0 步靠翻 Slack 历史贴文找昨日基准。那让「能不能做档位对照」取决于
Slack 连接器当天通不通，也取决于标题有没有被改过。本脚本把基准落到本机：
每次运行后把**各信号档位 + 两条轨道档位 + 7 项硬阈值状态**写进
assets/last_run.json，下次直接读它。Slack 历史仅作**回退**（本地文件缺失时）。

只比档位，不比数值
------------------
**对照只看状态档位（🟢🟡🔴⚪️），不看具体数值。** 这是原文的硬要求：
数值天天动，档位才是信号。所以 `diff` 输出的是「30 个信号中 X 个状态改变」，
而不是一堆涨跌幅——全绿那天要的正是那句「**30 个信号中 0 个状态改变，变的只有价格。**」

写入立场：宁可拒写，也不写坏
----------------------------
覆写是自动的，写坏一次会把错误基准带进后续每一天，而且要人肉翻 git 才看得出来。
所以 `write` 必定先跑完整校验，任一条不过就 exit 1、**完全不碰** last_run.json，
通过后走「临时文件 + os.replace」原子写入。没有 `--force`，也不要加。
`diff` 同样先校验今日文件：结构不对的文件对出来的「X 个状态改变」一定是错的，
而这句话会直接进报告第 1 部分。

首次运行不是错误
----------------
状态文件不存在时，`show` / `diff` 明确输出「无昨日基准，本次为首次建立」并 **exit 0**。
原文：读不到就标注首次建立，不影响其余部分。

子命令
------
    snapshot.py show                              打印上次运行的各信号档位 + 两条轨道档位 + 7 项硬阈值
                                                  + 回补 / 恢复条件的连续天数进度
    snapshot.py show --json                       同上，输出原始 JSON
    snapshot.py diff <today.json>                 逐信号对照：哪些档位变了、哪些没变
    snapshot.py write <today.json> --date YYYY-MM-DD   先校验，通过才原子覆写

<today.json> 的最小形状（**白名单写入**：只有下列字段会被写进状态档，见文末）：

    {
      "signals": {
        "1": {"state": "🟢", "name": "HY 信用利差", "value": "271bp", "as_of": "2026-08-06"},
        ...  "1"–"30" 一个都不能少，state 只能是 🟢 / 🟡 / 🔴 / ⚪️
      },
      "hard_thresholds": {
        "1": {"symbol": "❌", "name": "VIX 突破并站稳", "as_of": "2026-08-06"},
        ...  "1"–"7"，symbol 只能是 ❌ / ⚠️ / ✅ / ⚪️
      },
      "tracks": {
        "strategic": {"baseline_pct": 100, "trigger_count": 0, "valuation_env": "🔴"},
        "tactical":  {"state": "🟢", "factor": 1.00, "above_200dma": true}
      }
    }

state / symbol 也可以直接写成字符串（"1": "🟢"），脚本一律归一化处理。

`tracks.tactical.above_200dma`（布尔）不是必填，但**不填就判不了战术层恢复条件**
（「SPX 重新站上 200DMA 且连续 5 个交易日站稳」），show / diff 只会如实回答
「历史缺 above_200dma 记录，无法判定」，绝不会替你猜成已站稳。

history：让「维持满 2 周」可验证
-------------------------------
恢复条件是**跨日**规则：战略层要「7 项硬阈值触发数回落至 ≤1 **且维持满 2 周**」，
战术层要「站上 200DMA **连续 5 个交易日**」。只留一次运行的状态档回答不了
「这是第 1 天还是第 10 天」，而报告又必须每天写「距离恢复还差什么」——
没有历史就只能凭印象编，那正是本技能第 1 条禁止的事。

所以状态档带一个 `history` 数组，每个交易日一笔、最多保留 14 笔：

    {"date": "2026-08-06", "trigger_count": 1, "unknown_count": 0,
     "strategic_baseline_pct": 100, "tactical_state": "🟢", "above_200dma": true}

`write` 时按日期去重后追加（同日重跑覆盖当日那笔，不追加第二笔），超出 14 笔丢最旧的。
`history` **由脚本维护**，today.json 里写了也会被丢弃。
数到 history 头部还没被截断时，一律输出「历史不足，无法判定是否满 2 周」，
**绝不默认成已满足**——缺历史只会让人提前回补，方向上是单边失效。

白名单写入：只写列出的字段
--------------------------
状态档是 public repo 里每次运行都被覆写并提交的文件。今日文件里多塞一个字段
（中间计算、原始响应、持仓上下文……）若被原样带出去，就会被静默提交。
所以 `write` 只写出 `ALLOWED_*` 里明确列出的字段，其余一律丢弃，并在 stderr
点名「已丢弃 N 个未知字段：…」。要加字段就改那几个常量，不要改回 pass-through。

纯标准库实现，无第三方依赖；所有路径以 __file__ 为锚，任意 cwd 下均可运行。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_NAME = Path(__file__).name
SKILL_ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = SKILL_ROOT / "assets" / "last_run.json"

_HOMEISH_RE = re.compile(r"(?:/Users|/home|/var/folders)/[^/\s\"']+")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

SIGNAL_IDS = [str(i) for i in range(1, 31)]      # 30 个信号，编号固定 1–30
HARD_IDS = [str(i) for i in range(1, 8)]         # 7 项硬阈值

# 信号档位词汇（output-format.md 第 2 部分「状态」列）
SIGNAL_STATES = ("🟢", "🟡", "🔴", "⚪️")
STATE_MEANING = {"🟢": "正常", "🟡": "接近阈值", "🔴": "已触发", "⚪️": "数据暂缺"}

# 硬阈值符号（decision-framework.md 7 项硬阈值表）
HARD_SYMBOLS = ("❌", "⚠️", "✅", "⚪️")
HARD_MEANING = {"❌": "未触发", "⚠️": "距阈值 <10% 或部分条件成立",
                "✅": "已触发", "⚪️": "数据暂缺，无法判定"}

# 7 项硬阈值的名称（供 show 打印；顺序即编号，逐字取自 decision-framework.md）
HARD_NAMES = {
    "1": "VIX 突破并站稳（>25 且连续 3 个交易日）",
    "2": "Margin Debt 月减（连续 3 个月）",
    "3": "HY Spread 扩张（>4.5%）",
    "4": "Fear & Greed 从高位回落（>75 跌回 <50）",
    "5": "A/D Line 顶背离（SPX 新高但 A/D 未创新高）",
    "6": "BofA Bull & Bear（>8.0）",
    "7": "Insider Buy/Sell（<0.17）",
}

# 战略层目标仓位基准的合法档位（decision-framework.md 轨道一那张表的全部取值）
STRATEGIC_LEVELS = (100, 85, 70, 50)
# 战术层档位与系数（轨道二）
TACTICAL_STATES = ("🟢", "🟡", "🔴")
TACTICAL_MEANING = {"🟢": "持有 ×1.00", "🟡": "减速 ×1.00（禁止加仓）", "🔴": "降曝险 ×0.50"}
VALUATION_ENVS = ("🟢", "🟡", "🔴")

FIRST_RUN_MSG = "无昨日基准，本次为首次建立（不是错误，其余部分照常输出）。"

# 恢复条件的天数门槛（decision-framework.md「恢复条件」一节，逐字对应）
#   战略层：7 项硬阈值触发数回落至 ≤1 且**维持满 2 周** → 10 个交易日
#   战术层：SPX 重新站上 200DMA 且**连续 5 个交易日**站稳
STRATEGIC_RECOVERY_DAYS = 10
TACTICAL_RECOVERY_DAYS = 5
# history 至少要覆盖「满 2 周」再多留几笔，才能看出连续段是不是被截断
HISTORY_MAX_DAYS = 14

# ---- 写入白名单 ----------------------------------------------------------
# 状态档每次运行都会被覆写并提交进 public repo。写入一律走白名单：
# 只有下面列出的键会被写出去，其余丢弃并在 stderr 点名。
# 要新增字段就在这里显式加一项，**不要**改回「多余字段原样保留」。
ALLOWED_TODAY_TOP = ("date", "updated_at", "written_by",
                     "signals", "hard_thresholds", "tracks")
ALLOWED_SIGNAL = ("state", "name", "value", "as_of", "notes")
ALLOWED_HARD = ("symbol", "name", "value", "as_of", "notes")
ALLOWED_TRACKS = ("strategic", "tactical")
ALLOWED_STRATEGIC = ("baseline_pct", "trigger_count", "valuation_env", "notes")
ALLOWED_TACTICAL = ("state", "factor", "above_200dma", "dma200_slope_positive", "notes")
ALLOWED_HISTORY = ("date", "trigger_count", "unknown_count",
                   "strategic_baseline_pct", "tactical_state", "above_200dma")
# 这些键的内容已被 unwrap / label_of / as_map 归一化到白名单字段里，
# 丢掉它们不是「发现了未知字段」，别拿去吓人。
SILENT_DROP_KEYS = ("state", "symbol", "档位", "状态",
                    "name", "名称", "signal", "信号", "id", "编号")

# emoji 变体选择符：⚪️ 与 ⚪ 是同一个档位的两种写法，比对前统一
_VS16 = "️"


# ---------------------------------------------------------------- 通用小工具


def _configure_streams() -> None:
    """强制 stdout/stderr 用 UTF-8，避免管道场景下 emoji 档位写不出去。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass


def scrub(text) -> str:
    """把文本里的家目录绝对路径折叠掉。public repo：输出会进报告并推 Slack。"""
    s = str(text)
    try:
        home = str(Path.home())
    except Exception:
        home = ""
    if home:
        s = s.replace(home, "~")
    return _HOMEISH_RE.sub("~", s)


def rel_display(path: Path) -> str:
    """技能自身的路径相对技能根目录展示；技能外的路径只取文件名。"""
    try:
        return str(Path(path).resolve().relative_to(SKILL_ROOT))
    except Exception:
        return Path(path).name


def err(msg: str = "") -> None:
    print(msg, file=sys.stderr)


def norm_state(v) -> str:
    """归一化档位：去空白、补/去变体选择符，统一成词汇表里的写法。"""
    s = str(v).strip().replace(_VS16, "")
    for canon in (*SIGNAL_STATES, *HARD_SYMBOLS):
        if canon.replace(_VS16, "") == s:
            return canon
    return s


def unwrap(entry, key: str) -> str:
    """条目可以是裸字符串，也可以是带 state / symbol 的对象。"""
    if isinstance(entry, dict):
        for k in (key, "state", "symbol", "档位", "状态"):
            if k in entry and entry[k] is not None:
                return norm_state(entry[k])
        return ""
    if entry is None:
        return ""
    return norm_state(entry)


def label_of(entry, fallback: str = "") -> str:
    if isinstance(entry, dict):
        for k in ("name", "名称", "signal", "信号"):
            if entry.get(k):
                return str(entry[k])
    return fallback


def as_map(obj) -> dict:
    """把 signals / hard_thresholds 归一化成 {"1": entry} 形状。

    允许 list（按顺序当成 1..N）是为了容忍 agent 手写时的常见形态，
    但键最终一律是字符串数字，比对只认这个。
    """
    if isinstance(obj, dict):
        return {str(k).strip(): v for k, v in obj.items()}
    if isinstance(obj, list):
        out = {}
        for i, item in enumerate(obj, start=1):
            if isinstance(item, dict):
                key = str(item.get("id") or item.get("编号") or i).strip()
            else:
                key = str(i)
            out[key] = item
        return out
    return {}


# ---------------------------------------------------------------- 读盘与校验


def read_json(path: Path, what: str) -> dict:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        err(f"错误：找不到{what} {rel_display(path)}。")
        sys.exit(1)
    except OSError as exc:
        err(f"错误：读取{what} {rel_display(path)} 失败：{scrub(exc)}")
        sys.exit(1)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        err(f"错误：{what} {rel_display(path)} 不是合法 JSON：{scrub(exc)}")
        sys.exit(1)
    if not isinstance(data, dict):
        err(f"错误：{what} {rel_display(path)} 的顶层必须是 JSON 对象。")
        sys.exit(1)
    return data


def validate(data: dict, source: str = "") -> list[str]:
    """完整校验今日状态。返回问题清单（空 = 通过；清单里只有真问题，不含出处注记）。

    校验的三件事，正对应第 0 步真正要用的三样东西：
      1. 30 个信号编号齐全、档位取值合法 —— 少一个，「X 个状态改变」的分母就是错的；
      2. 7 项硬阈值状态齐全、符号合法 —— ⚪️ 计数规则与战略基准全靠它；
      3. 两条轨道档位存在且合法 —— 报告第 1 部分的决策层那一行全靠它。
    """
    problems: list[str] = []

    signals = as_map(data.get("signals"))
    if not signals:
        problems.append("缺少 signals（应是 \"1\"–\"30\" 的对象，每项带 state）")
    else:
        missing = [i for i in SIGNAL_IDS if i not in signals]
        if missing:
            problems.append(f"signals 缺少信号编号：{'、'.join(missing)}（必须 1–30 齐全）")
        extra = [k for k in signals if k not in SIGNAL_IDS]
        if extra:
            problems.append(
                f"signals 出现非 1–30 的编号：{'、'.join(sorted(extra))}"
                f"（31–34 是周一附加，不计入 30 个信号，不要放进 signals）")
        for i in SIGNAL_IDS:
            if i not in signals:
                continue
            st = unwrap(signals[i], "state")
            if st not in SIGNAL_STATES:
                shown = st if st else "（空）"
                problems.append(
                    f"信号 {i} 的档位「{shown}」不合法，只能是 "
                    f"{' / '.join(SIGNAL_STATES)}")

    hard = as_map(data.get("hard_thresholds"))
    if not hard:
        problems.append("缺少 hard_thresholds（7 项硬阈值状态，⚪️ 计数规则与战略基准都靠它）")
    else:
        hmissing = [i for i in HARD_IDS if i not in hard]
        if hmissing:
            problems.append(f"hard_thresholds 缺少第 {'、'.join(hmissing)} 项（必须 1–7 齐全）")
        for i in HARD_IDS:
            if i not in hard:
                continue
            sym = unwrap(hard[i], "symbol")
            if sym not in HARD_SYMBOLS:
                shown = sym if sym else "（空）"
                problems.append(
                    f"硬阈值第 {i} 项的状态「{shown}」不合法，只能是 "
                    f"{' / '.join(HARD_SYMBOLS)}")

    tracks = data.get("tracks")
    if not isinstance(tracks, dict):
        problems.append("缺少 tracks（两条轨道档位：strategic.baseline_pct 与 tactical.state）")
    else:
        strat = tracks.get("strategic")
        if not isinstance(strat, dict) or strat.get("baseline_pct") is None:
            problems.append("缺少 tracks.strategic.baseline_pct（战略层目标仓位基准）")
        else:
            try:
                pct = float(strat["baseline_pct"])
            except (TypeError, ValueError):
                pct = None
                problems.append(
                    f"tracks.strategic.baseline_pct「{strat['baseline_pct']}」不是数字")
            if pct is not None and (pct != int(pct) or int(pct) not in STRATEGIC_LEVELS):
                problems.append(
                    f"tracks.strategic.baseline_pct = {strat['baseline_pct']}"
                    f" 不是决策框架轨道一表里的档位（只能是 "
                    f"{' / '.join(str(x) + '%' for x in STRATEGIC_LEVELS)}）")
            env = strat.get("valuation_env")
            if env is not None and norm_state(env) not in VALUATION_ENVS:
                problems.append(
                    f"tracks.strategic.valuation_env「{env}」不合法，只能是 "
                    f"{' / '.join(VALUATION_ENVS)}")
            tc = strat.get("trigger_count")
            if tc is not None:
                try:
                    tci = int(tc)
                except (TypeError, ValueError):
                    problems.append(f"tracks.strategic.trigger_count「{tc}」不是整数")
                else:
                    if not 0 <= tci <= 7:
                        problems.append(
                            f"tracks.strategic.trigger_count = {tci} 超出 0–7")

        tac = tracks.get("tactical")
        if not isinstance(tac, dict) or tac.get("state") is None:
            problems.append("缺少 tracks.tactical.state（战术层档位 🟢 / 🟡 / 🔴）")
        else:
            st = norm_state(tac["state"])
            if st not in TACTICAL_STATES:
                problems.append(
                    f"tracks.tactical.state「{tac['state']}」不合法，只能是 "
                    f"{' / '.join(TACTICAL_STATES)}"
                    f"（⚪️ 不是战术档位——档位算不出来时先补齐输入，不要写成暂缺）")
            for key in ("above_200dma", "dma200_slope_positive"):
                v = tac.get(key)
                if v is not None and not isinstance(v, bool):
                    problems.append(
                        f"tracks.tactical.{key}「{v}」必须是布尔 true / false"
                        f"（判不了就整个字段别写——脚本会如实说「无法判定」，"
                        f"写成字符串反而会被当成有效记录）")

    # 注意：出处注记单独返回，不塞进 problems——塞进去会让「共 N 条问题」永远多算 1 条。
    return problems


def load_validated(path_str: str, what: str) -> dict:
    """读今日文件并强制校验；不过就 exit 1，且 stdout 一个字节都不输出。"""
    path = Path(path_str).expanduser()
    data = read_json(path, what)
    problems = validate(data, path.name)
    if problems:
        err(f"校验未通过：{path.name}")
        err(f"共 {len(problems)} 条问题（均出自 {path.name}）：")
        for i, p in enumerate(problems, start=1):
            err(f"  {i}. {p}")
        err("")
        err(f"状态文件 {rel_display(STATE_PATH)} 未被修改（一个字节都没动）。")
        sys.exit(1)
    return data


# ---------------------------------------------------------------- 摘要与统计


def hard_summary(data: dict) -> dict:
    """7 项硬阈值的触发计数（严格照 ⚪️ 计数规则：暂缺不计入分子，也不计入分母）。"""
    hard = as_map(data.get("hard_thresholds"))
    syms = {i: unwrap(hard.get(i), "symbol") for i in HARD_IDS}
    fired = [i for i in HARD_IDS if syms.get(i) == "✅"]
    unknown = [i for i in HARD_IDS if syms.get(i) == "⚪️"]
    n = len(HARD_IDS) - len(unknown)
    return {
        "symbols": syms,
        "fired": fired,
        "unknown": unknown,
        "count": len(fired),
        "denominator": n,
        "worst_case": len(fired) + len(unknown),
        "low_confidence": len(unknown) >= 3,
    }


def print_hard(data: dict, indent: str = "", label: str = "今日") -> None:
    h = hard_summary(data)
    print(f"{indent}7 项硬阈值：")
    for i in HARD_IDS:
        sym = h["symbols"].get(i) or "（缺）"
        meaning = HARD_MEANING.get(sym, "")
        print(f"{indent}  {i}. {sym} {meaning} ｜ {HARD_NAMES[i]}")
    print(f"{indent}→ {label}共 {h['count']} / {h['denominator']} 项触发"
          f"（{len(h['unknown'])} 项数据暂缺）")
    if h["unknown"]:
        print(f"{indent}→ 若暂缺的 {len(h['unknown'])} 项全部触发，计数将达 {h['worst_case']}"
              f"（≥2 即触发警戒升级）")
    if h["low_confidence"]:
        print(f"{indent}→ ⚠️ 暂缺 ≥3 项：本日触发计数可信度低（仅 {h['denominator']} 项可判定），"
              f"战略基准维持昨日档位不变，不因计数下降而回补仓位。")


def track_line(data: dict) -> tuple[str, str]:
    """两条轨道的档位（战略= 目标仓位基准%，战术= 🟢/🟡/🔴）。"""
    tracks = data.get("tracks") or {}
    strat = tracks.get("strategic") or {}
    tac = tracks.get("tactical") or {}
    pct = strat.get("baseline_pct")
    try:
        s_disp = f"{int(float(pct))}%"
    except (TypeError, ValueError):
        s_disp = "N/A"
    t_disp = norm_state(tac.get("state")) if tac.get("state") is not None else "N/A"
    return s_disp, t_disp


def print_tracks(data: dict, indent: str = "") -> None:
    s_disp, t_disp = track_line(data)
    tracks = data.get("tracks") or {}
    strat = tracks.get("strategic") or {}
    tac = tracks.get("tactical") or {}
    env = norm_state(strat.get("valuation_env")) if strat.get("valuation_env") else "—"
    tc = strat.get("trigger_count")
    tc_disp = f"{tc}" if tc is not None else "—"
    factor = tac.get("factor")
    f_disp = f"×{float(factor):.2f}" if isinstance(factor, (int, float)) else "—"
    print(f"{indent}轨道一 战略层：目标仓位基准 {s_disp}"
          f"（硬阈值触发 {tc_disp} 项，估值环境 {env}）")
    print(f"{indent}轨道二 战术层：{t_disp} {TACTICAL_MEANING.get(t_disp, '')} 系数 {f_disp}")


def staleness_banner(prev_date: str | None, today_date: str | None) -> list[str]:
    """基准新旧提示（横幅，不是失败）。

    本例程每个日历日都跑，所以基准正常只会比今天早 1 天。差 ≥3 天意味着中间漏跑过，
    这时「对比昨日」这四个字就是错的——报告必须改写成「对比 N 天前」，
    否则会把一段时间累积的档位变化说成一天之内发生的。
    """
    out: list[str] = []
    if not prev_date or not DATE_RE.match(prev_date):
        return out
    ref = today_date if (today_date and DATE_RE.match(today_date)) \
        else datetime.now().strftime("%Y-%m-%d")
    try:
        gap = (datetime.strptime(ref, "%Y-%m-%d") - datetime.strptime(prev_date, "%Y-%m-%d")).days
    except ValueError:
        return out
    if gap < 0:
        out.append(f"⚠️ 基准日期 {prev_date} 晚于本次 {ref}：状态文件比今天还新，"
                   f"很可能日期写错或跑串了，请先核对再采信对照结果。")
    elif gap >= 3:
        out.append(f"⚠️ 基准是 {gap} 天前（{prev_date}）的，中间漏跑过。"
                   f"报告里请写「对比 {gap} 天前」，不要写成「对比昨日」——"
                   f"否则会把几天累积的档位变化说成一天内发生的。")
    return out


def state_counts(data: dict) -> dict:
    signals = as_map(data.get("signals"))
    counts = {s: 0 for s in SIGNAL_STATES}
    for i in SIGNAL_IDS:
        st = unwrap(signals.get(i), "state")
        if st in counts:
            counts[st] += 1
    return counts


# ------------------------------------------------- history 与恢复条件的连续天数


def history_of(data: dict) -> list[dict]:
    """取出状态档里的 history：清洗、按日期去重、升序。缺失或坏掉一律当空。

    坏掉当空是刻意的：history 只用来回答「连续几天」，读不出来时下游会输出
    「历史不足，无法判定」，而不是拿半截数据凑一个看起来像答案的天数。
    """
    raw = data.get("history")
    if not isinstance(raw, list):
        return []
    by_date: dict[str, dict] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        d = str(item.get("date") or "").strip()
        if not DATE_RE.match(d):
            continue
        entry = {"date": d}
        for k in ALLOWED_HISTORY:
            if k != "date" and k in item:
                entry[k] = item[k]
        by_date[d] = entry          # 同日多笔以最后一笔为准
    return [by_date[d] for d in sorted(by_date)]


def history_entry(data: dict, date: str) -> dict:
    """从（已归一化的）当日状态生成一笔 history。

    触发数与暂缺数取自 hard_summary 的实算结果，不取 tracks.strategic.trigger_count——
    后者是模型手写的，实算的那份才是 7 个符号本身说的话。
    """
    h = hard_summary(data)
    tracks = data.get("tracks") or {}
    strat = tracks.get("strategic") or {}
    tac = tracks.get("tactical") or {}
    try:
        pct = int(float(strat.get("baseline_pct")))
    except (TypeError, ValueError):
        pct = None
    above = tac.get("above_200dma")
    state = tac.get("state")
    return {
        "date": date,
        "trigger_count": h["count"],
        "unknown_count": len(h["unknown"]),
        "strategic_baseline_pct": pct,
        "tactical_state": norm_state(state) if state is not None else None,
        "above_200dma": above if isinstance(above, bool) else None,
    }


def merge_history(hist: list[dict], entry: dict) -> list[dict]:
    """按日期去重后追加当日那笔，只保留最近 HISTORY_MAX_DAYS 笔。

    同日重跑覆盖当日那笔（不追加第二笔），否则一天跑两次就能把「连续天数」灌出来。
    """
    kept = [e for e in hist if e.get("date") != entry.get("date")]
    kept.append(dict(entry))
    kept.sort(key=lambda e: e.get("date") or "")
    return kept[-HISTORY_MAX_DAYS:]


def _tc_ok(entry: dict):
    """这一笔的硬阈值触发数是否 ≤1。读不出来返回 None（= 判不了，不是通过）。"""
    tc = entry.get("trigger_count")
    if isinstance(tc, bool) or not isinstance(tc, int):
        return None
    return tc <= 1


def _dma_ok(entry: dict):
    """这一笔 SPX 是否站上 200DMA。没记录返回 None（= 判不了，不是站稳）。"""
    v = entry.get("above_200dma")
    return v if isinstance(v, bool) else None


def streak_of(hist: list[dict], predicate, needed: int) -> tuple[int, str]:
    """从最新一笔往回数连续满足 predicate 的笔数。

    返回 (连续笔数, 状态)：
      met     —— 已数满 needed 笔，条件成立；
      not_met —— 被一笔**明确不满足**的记录截断，连续数是确定的；
      unknown —— 数到 history 头部还没被截断，或撞上一笔判不了的记录 →
                 真实连续天数无从得知。**这一档绝不能当成 met**：
                 缺历史只会让人提前回补，方向上是单边失效。
    """
    streak = 0
    for entry in reversed(hist):
        ok = predicate(entry)
        if ok is True:
            streak += 1
            if streak >= needed:
                return streak, "met"
        elif ok is False:
            return streak, "not_met"
        else:
            return streak, "unknown"
    return streak, "unknown"


def recovery_lines(hist: list[dict], projected: bool = False) -> list[str]:
    """回补 / 恢复条件的进度：连续几个交易日了、还差几个。

    非满仓状态下每天都要回答「距离恢复还差什么」（decision-framework.md 恢复条件一节），
    这些行就是那句话的数据来源——判不了就明说判不了，不给模型留凭印象编的空间。
    """
    n = len(hist)
    tail = "（含今日这笔，尚未写入状态档）" if projected else ""
    out = [f"回补 / 恢复条件进度{tail}：",
           f"  history {n} 笔" + (
               f"（{hist[0]['date']} → {hist[-1]['date']}，最多保留 {HISTORY_MAX_DAYS} 笔；"
               f"每个交易日跑一次时即为交易日数）" if n else "（尚无记录）")]

    s_streak, s_status = streak_of(hist, _tc_ok, STRATEGIC_RECOVERY_DAYS)
    out.append(f"  轨道一 战略层回补条件：7 项硬阈值触发数回落至 ≤1 "
               f"且维持满 2 周（{STRATEGIC_RECOVERY_DAYS} 个交易日）")
    if s_status == "met":
        out.append(f"    → 触发数 ≤1 已连续 {s_streak} 个交易日"
                   f"（≥{STRATEGIC_RECOVERY_DAYS}）：已满足「维持满 2 周」，"
                   f"可逐档往上回补**一级**（不可一次补满）。")
    elif s_status == "not_met" and s_streak == 0:
        last = hist[-1] if hist else {}
        tc = last.get("trigger_count")
        out.append(f"    → 最近一笔（{last.get('date', '？')}）触发数 {tc} 项 >1："
                   f"回补计时尚未开始，需先回落至 ≤1 再连续 {STRATEGIC_RECOVERY_DAYS} 个交易日。"
                   f"尚不满足回补条件。")
    elif s_status == "not_met":
        left = STRATEGIC_RECOVERY_DAYS - s_streak
        out.append(f"    → 触发数 ≤1 已连续 {s_streak} 个交易日，"
                   f"距「满 2 周」还差 {left} 个交易日："
                   f"**尚不满足回补条件，还差 {left} 个交易日**。")
    else:
        why = (f"已知最近 {s_streak} 个交易日触发数 ≤1，但更早无记录"
               if s_streak >= n else
               f"已知最近 {s_streak} 个交易日触发数 ≤1，再往前那笔读不出触发数")
        if n == 0:
            why = "尚无任何记录"
        out.append(f"    → **历史不足，无法判定是否满 2 周**（history 仅 {n} 笔，{why}）："
                   f"在记录补满 {STRATEGIC_RECOVERY_DAYS} 个交易日之前一律按"
                   f"「尚不满足」处理，不得回补。")

    t_streak, t_status = streak_of(hist, _dma_ok, TACTICAL_RECOVERY_DAYS)
    out.append(f"  轨道二 战术层恢复条件：SPX 重新站上 200DMA 且连续 "
               f"{TACTICAL_RECOVERY_DAYS} 个交易日站稳，且 200DMA 斜率转正")
    if t_status == "met":
        out.append(f"    → 站上 200DMA 已连续 {t_streak} 个交易日"
                   f"（≥{TACTICAL_RECOVERY_DAYS}）：站稳天数这一半已满足；"
                   f"斜率转正需另行确认，本脚本不判定斜率。")
    elif t_status == "not_met" and t_streak == 0:
        last = hist[-1] if hist else {}
        out.append(f"    → 最近一笔（{last.get('date', '？')}）SPX 未站上 200DMA："
                   f"连续站稳天数归零，需重新连续 {TACTICAL_RECOVERY_DAYS} 个交易日。")
    elif t_status == "not_met":
        left = TACTICAL_RECOVERY_DAYS - t_streak
        out.append(f"    → 站上 200DMA 已连续 {t_streak} 个交易日，"
                   f"还差 {left} 个交易日（另需 200DMA 斜率转正）。")
    else:
        if n == 0:
            why = "尚无任何记录"
        elif t_streak >= n:
            why = (f"已知最近 {t_streak} 个交易日站上 200DMA，但更早无记录"
                   if t_streak else "最近一笔没有 above_200dma 记录")
        else:
            why = (f"已知最近 {t_streak} 个交易日站上 200DMA，"
                   f"再往前那笔没有 above_200dma 记录")
        out.append(f"    → **历史不足，无法判定「连续 {TACTICAL_RECOVERY_DAYS} "
                   f"个交易日站稳」**（history 仅 {n} 笔，{why}）："
                   f"每天在 today.json 的 tracks.tactical.above_200dma 写入 true / false "
                   f"才能判定；在此之前一律按「尚不满足」处理。")
    return out


def pick(entry: dict, allowed, path: str, dropped: list[str]) -> dict:
    """白名单挑字段：只留 allowed 里的键，其余记进 dropped（别名不吵）。"""
    out = {}
    for k, v in entry.items():
        if k in allowed:
            out[k] = v
        elif k not in SILENT_DROP_KEYS:
            dropped.append(f"{path}.{k}" if path else str(k))
    return out


# ---------------------------------------------------------------- 子命令


def cmd_show(args: argparse.Namespace) -> int:
    if not STATE_PATH.exists():
        print(FIRST_RUN_MSG)
        print(f"（状态文件 {rel_display(STATE_PATH)} 尚不存在；"
              f"本次跑完用 `snapshot.py write <today.json> --date YYYY-MM-DD` 建立。"
              f"回退方案：翻 Slack 历史里标题含「每日风险监控」的上一贴。）")
        return 0

    data = read_json(STATE_PATH, "状态文件")
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0

    date = data.get("date") or "（未记录）"
    updated = data.get("updated_at") or "（未记录）"
    print(f"# 上次运行基准 · 数据日期 {date}（写入于 {updated}）")
    print(f"来源：{rel_display(STATE_PATH)}")
    for line in staleness_banner(data.get("date"), None):
        print(line)
    print()

    signals = as_map(data.get("signals"))
    print("30 个信号档位：")
    for i in SIGNAL_IDS:
        entry = signals.get(i)
        st = unwrap(entry, "state") or "（缺）"
        name = label_of(entry, "")
        value = entry.get("value") if isinstance(entry, dict) else None
        asof = entry.get("as_of") if isinstance(entry, dict) else None
        tail = ""
        if value not in (None, ""):
            tail += f"　{value}"
        if asof:
            tail += f"（as of {asof}）"
        print(f"  {i:>2}. {st} {name}{tail}")
    c = state_counts(data)
    print(f"→ 🟢 {c['🟢']}｜🟡 {c['🟡']}｜🔴 {c['🔴']}｜⚪️ {c['⚪️']}"
          f"（共 {len(SIGNAL_IDS)} 项）")
    print()
    print_tracks(data)
    print()
    print_hard(data, label="上次运行")
    print()
    for line in recovery_lines(history_of(data)):
        print(line)
    return 0


def cmd_diff(args: argparse.Namespace) -> int:
    # 先校验今日文件：结构不对的文件对出来的「X 个状态改变」一定是错的，
    # 而那句话会直接进报告第 1 部分。校验不过就 exit 1，stdout 零字节。
    today = load_validated(args.today, "今日状态文件")
    # 今日这笔先并进 history 再算连续天数：diff 跑在 write 之前，
    # 但报告里「距离恢复还差什么」问的是今天的进度，不是昨天的。
    t_date = today.get("date")
    if not (isinstance(t_date, str) and DATE_RE.match(t_date)):
        t_date = datetime.now().strftime("%Y-%m-%d")

    if not STATE_PATH.exists():
        print(FIRST_RUN_MSG)
        print("📋 对比昨日：无昨日基准，本次为首次建立，本项不参与判定。")
        print()
        c = state_counts(today)
        print(f"今日档位分布：🟢 {c['🟢']}｜🟡 {c['🟡']}｜🔴 {c['🔴']}｜⚪️ {c['⚪️']}"
              f"（共 {len(SIGNAL_IDS)} 项）")
        print_tracks(today)
        print_hard(today)
        print()
        for line in recovery_lines(merge_history([], history_entry(today, t_date)),
                                   projected=True):
            print(line)
        return 0

    prev = read_json(STATE_PATH, "状态文件")
    prev_signals, cur_signals = as_map(prev.get("signals")), as_map(today.get("signals"))
    prev_date = prev.get("date") or "（未记录）"

    changed: list[tuple[str, str, str, str]] = []
    unchanged = 0
    no_baseline: list[str] = []
    for i in SIGNAL_IDS:
        cur = unwrap(cur_signals.get(i), "state")
        old = unwrap(prev_signals.get(i), "state")
        name = label_of(cur_signals.get(i), label_of(prev_signals.get(i), ""))
        if old not in SIGNAL_STATES:
            no_baseline.append(i)
            continue
        if old == cur:
            unchanged += 1
        else:
            changed.append((i, old, cur, name))

    if len(no_baseline) == len(SIGNAL_IDS):
        # 基准文件在，但一个档位都读不出来（手改坏了 / 被别的东西覆盖了）。
        # 这时绝不能打印「0 个状态改变」——那是「比过了，都没变」的意思，
        # 而真实情况是「根本没比成」。按首次建立处理。
        print(FIRST_RUN_MSG)
        print(f"📋 对比昨日：基准文件 {rel_display(STATE_PATH)} 存在但读不出任何信号档位"
              f"（基准日期 {prev_date}），本次按首次建立处理，本项不参与判定。")
        print_tracks(today)
        print_hard(today)
        print()
        for line in recovery_lines(
                merge_history(history_of(prev), history_entry(today, t_date)),
                projected=True):
            print(line)
        return 0

    total = len(SIGNAL_IDS)
    print(f"# 昨日对照 · 基准 {prev_date} → 今日 {today.get('date') or '（未标注）'}")
    print("（只比状态档位，不比具体数值——数值天天动，档位才是信号）")
    for line in staleness_banner(prev.get("date"), today.get("date")):
        print(line)
    print()
    if not changed:
        print(f"📋 对比昨日：**{total} 个信号中 0 个状态改变，变的只有价格。**")
    else:
        print(f"📋 对比昨日：**{total} 个信号中 {len(changed)} 个状态改变**")
        for i, old, cur, name in changed:
            label = f" {name}" if name else ""
            print(f"  - 信号 {i}{label}：{old} {STATE_MEANING.get(old, '')}"
                  f" → {cur} {STATE_MEANING.get(cur, '')}")
    if no_baseline:
        print(f"  ⚠️ 昨日基准缺这几项、无法对照：信号 {'、'.join(no_baseline)}"
              f"（不计入改变数，也不计入未变数）")
    print(f"  未改变 {unchanged} 项。")
    print()

    # 数据暂缺的进出：⚪️ 是「不知道」，不是「安全」，进出都要点名
    prev_unknown = {i for i in SIGNAL_IDS if unwrap(prev_signals.get(i), "state") == "⚪️"}
    cur_unknown = {i for i in SIGNAL_IDS if unwrap(cur_signals.get(i), "state") == "⚪️"}
    new_unknown = sorted(cur_unknown - prev_unknown, key=int)
    recovered = sorted(prev_unknown - cur_unknown, key=int)
    if new_unknown:
        print(f"⚪️ 今日新增数据暂缺：信号 {'、'.join(new_unknown)}"
              f"——报告须写明尝试过的来源与滞后周数。")
    if recovered:
        print(f"✅ 昨日暂缺、今日已取到：信号 {'、'.join(recovered)}")
    if new_unknown or recovered:
        print()

    # 两条轨道档位
    ps, pt = track_line(prev)
    cs, ct = track_line(today)
    print("两条轨道：")
    print(f"  轨道一 战略基准：{ps} → {cs}　{'（改变）' if ps != cs else '（未变）'}")
    print(f"  轨道二 战术档位：{pt} → {ct}　{'（改变）' if pt != ct else '（未变）'}")
    if ps != cs or pt != ct:
        print("  ⚠️ 任一轨道档位发生变化 → Slack 消息最开头须加 @ 提示并注明具体原因。")
    print()

    # 7 项硬阈值
    ph, ch = hard_summary(prev), hard_summary(today)
    hard_changed = [i for i in HARD_IDS
                    if ph["symbols"].get(i) != ch["symbols"].get(i)]
    print(f"7 项硬阈值：{ph['count']} / {ph['denominator']} → "
          f"{ch['count']} / {ch['denominator']} 触发"
          f"（暂缺 {len(ph['unknown'])} → {len(ch['unknown'])} 项）")
    if hard_changed:
        for i in hard_changed:
            print(f"  - 第 {i} 项 {HARD_NAMES[i]}："
                  f"{ph['symbols'].get(i) or '（缺）'} → {ch['symbols'].get(i) or '（缺）'}")
    else:
        print("  7 项状态与昨日完全一致。")
    if ch["count"] >= 2:
        print(f"  🚨 触发 ≥2 项：报告最开头必须加 🚨 标记并写明「警戒升级」。")
    if ch["low_confidence"]:
        print(f"  ⚠️ 暂缺 {len(ch['unknown'])} 项（≥3）：决策层那一行须加注"
              f"「本日触发计数可信度低（仅 {ch['denominator']} 项可判定）」，"
              f"战略基准维持昨日档位 {ps} 不变。")
        if cs != ps:
            print(f"  ⚠️ 冲突：今日文件声明的战略基准是 {cs}，但上一条规则要求维持 {ps}。"
                  f"回补仓位尤其不允许——绝不能因为计数下降就当成安全。请先复核再发报告。")
    print()

    # 回补 / 恢复条件：连续几个交易日了、还差几个（判不了就明说判不了）
    for line in recovery_lines(
            merge_history(history_of(prev), history_entry(today, t_date)),
            projected=True):
        print(line)
    return 0


def atomic_write(path: Path, content: str) -> None:
    """临时文件 + os.replace，保证要么完整替换、要么原文件不动。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        # mkstemp 给 0600；状态文件是普通可读文本，统一成 0644，
        # 免得每次覆写都把权限悄悄收紧一次。
        try:
            os.chmod(tmp_path, 0o644)
        except OSError:
            pass
        os.replace(tmp_path, path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def cmd_write(args: argparse.Namespace) -> int:
    date = args.date.strip()
    if not DATE_RE.match(date):
        err(f"错误：--date 必须是 YYYY-MM-DD 格式，实际「{args.date}」。状态文件未被修改。")
        return 1
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        err(f"错误：--date「{date}」不是合法日期。状态文件未被修改。")
        return 1

    data = load_validated(args.today, "今日状态文件")

    prev_state: dict = {}
    if STATE_PATH.exists():
        try:
            loaded = json.loads(STATE_PATH.read_text(encoding="utf-8-sig"))
            if isinstance(loaded, dict):
                prev_state = loaded
        except Exception:
            prev_state = {}
    old_date = prev_state.get("date")

    # 归一化 + 白名单写入：档位统一写法、编号统一字符串，
    # **只写下面明确列出的字段**，其余一律丢弃（今日文件里多塞的中间计算、
    # 原始响应、持仓上下文……都不会被静默提交进 public repo）。
    # 为什么要归一化：⚪️ 与 ⚪ 混用会让下一天的比对凭空多出「状态改变」。
    dropped: list[str] = []
    for k in data:
        if k not in ALLOWED_TODAY_TOP:
            dropped.append(k)

    out: dict = {}
    signals = as_map(data.get("signals"))
    out["signals"] = {}
    for i in SIGNAL_IDS:
        entry = signals[i]
        e = pick(entry, ALLOWED_SIGNAL, f"signals.{i}", dropped) \
            if isinstance(entry, dict) else {}
        e["state"] = unwrap(entry, "state")
        name = label_of(entry, "")
        if name:
            e["name"] = name
        out["signals"][i] = e
    hard = as_map(data.get("hard_thresholds"))
    out["hard_thresholds"] = {}
    for i in HARD_IDS:
        entry = hard[i]
        e = pick(entry, ALLOWED_HARD, f"hard_thresholds.{i}", dropped) \
            if isinstance(entry, dict) else {}
        e["symbol"] = unwrap(entry, "symbol")
        e.setdefault("name", label_of(entry, HARD_NAMES[i]))
        out["hard_thresholds"][i] = e
    tracks_in = data.get("tracks") or {}
    pick(tracks_in, ALLOWED_TRACKS, "tracks", dropped)   # 只为点名 tracks 下的未知键
    strat_in = tracks_in.get("strategic") or {}
    strat = pick(strat_in, ALLOWED_STRATEGIC, "tracks.strategic", dropped)
    strat["baseline_pct"] = int(float(strat_in["baseline_pct"]))
    if strat.get("valuation_env") is not None:
        strat["valuation_env"] = norm_state(strat["valuation_env"])
    tac_in = tracks_in.get("tactical") or {}
    tac = pick(tac_in, ALLOWED_TACTICAL, "tracks.tactical", dropped)
    tac["state"] = norm_state(tac_in.get("state"))
    out["tracks"] = {"strategic": strat, "tactical": tac}

    out["date"] = date
    out["updated_at"] = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S%z")
    out["written_by"] = SCRIPT_NAME
    # history 由脚本维护：同日重跑覆盖当日那笔，超出 HISTORY_MAX_DAYS 笔丢最旧的。
    # 没有它，「触发数 ≤1 维持满 2 周」和「站上 200DMA 连续 5 日」就只能靠印象编。
    history = merge_history(history_of(prev_state), history_entry(out, date))
    out["history"] = history

    atomic_write(STATE_PATH, json.dumps(out, ensure_ascii=False, indent=2) + "\n")

    if dropped:
        shown = dropped[:20]
        more = f"…（另有 {len(dropped) - len(shown)} 个未列出）" if len(dropped) > len(shown) else ""
        err(f"提示：已丢弃 {len(dropped)} 个未知字段（不在写入白名单内，未写进状态档）："
            f"{'、'.join(shown)}{more}")
        err("　　　状态档会被提交进 public repo，只写白名单里的字段是刻意的；"
            "确实需要保留的字段请在脚本的 ALLOWED_* 常量里显式加一项。")

    c = state_counts(out)
    h = hard_summary(out)
    s_disp, t_disp = track_line(out)
    print(f"已写入 {rel_display(STATE_PATH)}：数据日期 {old_date or '（无，首次建立）'} → {date}")
    print(f"  30 个信号：🟢 {c['🟢']}｜🟡 {c['🟡']}｜🔴 {c['🔴']}｜⚪️ {c['⚪️']}")
    print(f"  两条轨道：战略基准 {s_disp}｜战术档位 {t_disp}")
    print(f"  7 项硬阈值：{h['count']} / {h['denominator']} 项触发"
          f"（{len(h['unknown'])} 项数据暂缺）")
    print(f"  history：{len(history)} 笔"
          f"（{history[0]['date']} → {history[-1]['date']}，最多保留 {HISTORY_MAX_DAYS} 笔）")
    print()
    for line in recovery_lines(history):
        print(line)
    return 0


# ---------------------------------------------------------------- CLI


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=SCRIPT_NAME,
        description="昨日对照的滚动状态（assets/last_run.json）：只比档位，不比数值。"
                    "写入前必定完整校验，任一条不过就拒写且不碰状态文件。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "典型一天：\n"
            "  python3 scripts/snapshot.py show                    # 昨日基准（首次运行会明说）\n"
            "  python3 scripts/snapshot.py diff today.json         # 「30 个信号中 X 个状态改变」\n"
            "  python3 scripts/snapshot.py write today.json --date 2026-08-10\n\n"
            "today.json 的最小形状见脚本顶部 docstring；signals 必须 1–30 齐全，\n"
            "档位只能是 🟢 / 🟡 / 🔴 / ⚪️，hard_thresholds 必须 1–7 齐全，\n"
            "tracks 必须同时有 strategic.baseline_pct 与 tactical.state。\n"
            "写入走白名单：只有 docstring 列出的字段会进状态档，其余丢弃并在 stderr 点名。\n"
            "状态档另存 history（最多 14 笔），show / diff 据此回答\n"
            "「触发数 ≤1 已连续几个交易日 / 距『满 2 周』还差几天」——判不了就明说判不了。\n"
        ),
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_show = sub.add_parser(
        "show", help="打印上次运行的各信号档位 + 两条轨道档位 + 7 项硬阈值 + 回补条件进度")
    p_show.add_argument("--json", action="store_true", help="输出原始 JSON")
    p_show.set_defaults(func=cmd_show)

    p_diff = sub.add_parser("diff", help="逐信号对照：哪些档位变了、哪些没变")
    p_diff.add_argument("today", metavar="today.json", help="今日状态 JSON")
    p_diff.set_defaults(func=cmd_diff)

    p_write = sub.add_parser("write", help="先校验再原子覆写 assets/last_run.json")
    p_write.add_argument("today", metavar="today.json", help="今日状态 JSON")
    p_write.add_argument("--date", required=True, metavar="YYYY-MM-DD", help="本次数据日期")
    p_write.set_defaults(func=cmd_write)

    return parser


def main(argv: list[str] | None = None) -> int:
    _configure_streams()
    args = build_parser().parse_args(argv)
    return args.func(args)


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
