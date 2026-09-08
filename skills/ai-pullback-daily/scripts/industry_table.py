#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""产业质量参考表读取器 —— 日更技能与周更技能之间的桥。

它做什么
--------
日更 routine 的「第一步·产业质量参考表」不再内联维护一份表格，而是直接读姊妹技能
`ai-industry-weekly` 每周滚动覆写的 `assets/baseline.md`。本脚本负责：
  1. 定位姊妹技能目录；
  2. 拿到表格与元数据（数据日期 / 更新时间 / 标的数）；
  3. **检查基准表是否陈旧**——超过 STALE_DAYS 天就醒目告警；数据日期若在未来则单独告警
     （那种情况下陈旧闸门本身失效，见 future_alert 的注释）；
  4. **与 assets/universe.json 交叉校验**——基准表行数 ≠ 清单标的数就醒目告警，
     那说明周更改了标的清单却还没重跑 `baseline.py write`（见 universe_alert 的注释）；
  5. 供日更报告按标的查「评级·层级·瓶颈」。

三类告警**都不阻断**（exit 仍为 0）：数据本身解析得出来，日更照常出报告；
坏的是周更那一侧，日更对周更只读，只能提示。
三者在 JSON 里的等价物是顶层 `alerts[]`（每条带条件、两侧数字、成因与处置原文），
外加 `degraded` / `degraded_reasons[]`——横幅只落在人类分支/stderr 上，
等于「窄读 JSON 的调用方看不见评级可能已过时」，那正是这些横幅存在的意义所在。

为什么解析要走 subprocess
-------------------------
表格的格式、列名、校验口径由周更技能的 `scripts/baseline.py` **单点维护**。
本脚本首选 `baseline.py show` / `baseline.py meta` 取数，而不是自己再实现一遍
markdown 解析——两边各写一份解析，早晚会在某次列名调整后悄悄漂移，
而漂移的表现是「日更报告里的评级与周更基准表对不上」，极难发现。
只有在 subprocess 不可用时（例如姊妹技能被裁剪、python 调不起来）才退回直接解析
baseline.md，并在 stderr 注明走了回退路径。

为什么要陈旧告警
----------------
周更技能的定位是**每周**跑一次。日更每天引用它的评级，却完全不知道它上次跑在什么时候。
如果周更两周没跑，日更会拿着两周前的评级照常输出「🟢 重点买入」，而这正是分桶规则里
优先级最高的质量闸门。这是日更引用周更的最大风险，必须显式暴露，不能静默使用。

用法
----
    industry_table.py                 打印全表（代码 / 评级 / 层级 / 瓶颈 / 主题）
    industry_table.py --json          打印完整 JSON（含论点、估值性格与元数据）
    industry_table.py --ticker NVDA   单只摘要，评级·层级·瓶颈 形如 🟢·L1·🔥①②③
    industry_table.py --check         只验证能否定位并解析，exit 0/1（SKILL.md 第零步前置检查）
    industry_table.py --check --json  同上，但输出机读 JSON（checks[] / alerts[] / degraded）

**任何** --json 子命令的退出码语义都与其文本分支完全一致，且阻断性失败一律给一份
`ok:false` / `blocking:true` / `result_produced:false` 的 JSON——stdout 上永远不会
「什么都没有」。不加 --json 的纯文本分支在失败时仍逐字保持 stdout 静默。

姊妹技能定位顺序由共享模块 `_weekly.py` 统一（三个脚本同一套顺序、同一套探针、
同一套环境变量语义），见该模块的文档字符串。

纯标准库实现，无第三方依赖；所有路径以 __file__ 为锚，任意 cwd 下均可运行。
对外打印的路径一律相对化/折叠家目录——输出会贴进日更报告并推送 Slack。
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import date, datetime
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
# 以 `python3 /abs/path/scripts/industry_table.py` 方式调用时同目录 import 本来就成立，
# 这里再显式把脚本目录加进 sys.path，保证 -P / PYTHONSAFEPATH 等场景下也稳。
_SCRIPTS_DIR = str(Path(__file__).resolve().parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from _weekly import (  # noqa: E402  （必须在上面的 sys.path 之后）
    NEED_BASELINE,
    WEEKLY_DIRNAME as WEEKLY_SKILL_NAME,
    WEEKLY_ENV as WEEKLY_DIR_ENV,
    WeeklySkillError as WeeklySkillNotFound,
    locate_weekly_skill,
    rel_display,
)

# ---------------------------------------------------------------- 常量

# 基准表「数据日期」距今超过这么多天 → 醒目告警。
# 周更是每周一次，10 天 = 一周 + 3 天缓冲；再超就说明周更那一轮压根没跑。
STALE_DAYS = 10

# 表里必须存在的列（列名以 baseline.md 表头为准，靠名字取下标，不靠位置）
COL_CODE = "代码"
COL_THEME = "主题"
COL_LAYER = "8层"
COL_BOTTLENECK = "瓶颈"
COL_RATING = "评级"
COL_THESIS = "论点"
COL_VALUATION = "估值性格"
REQUIRED_COLUMNS = (
    COL_CODE, COL_THEME, COL_LAYER, COL_BOTTLENECK, COL_RATING, COL_THESIS, COL_VALUATION,
)

SEPARATOR_CELL_RE = re.compile(r"^:?-{2,}:?$")
PIPE_SPLIT_RE = re.compile(r"(?<!\\)\|")
DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})")

SUBPROCESS_TIMEOUT = 60


# ---------------------------------------------------------------- 基础工具


def _configure_streams() -> None:
    """强制 stdout/stderr 用 UTF-8，避免管道场景下 emoji 评级写不出去。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass


def err(msg: str = "") -> None:
    print(msg, file=sys.stderr)


def read_text(path: Path) -> str:
    """UTF-8 读文件（容忍 BOM 与 CRLF）。"""
    return path.read_text(encoding="utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")


def split_cells(line: str) -> list[str]:
    """把一条 markdown 表格行拆成单元格（尊重 \\| 转义），并逐个 strip。"""
    body = line.strip()
    if body.startswith("|"):
        body = body[1:]
    stripped = body.rstrip()
    if stripped.endswith("|") and not stripped.endswith("\\|"):
        body = stripped[:-1]
    return [cell.strip() for cell in PIPE_SPLIT_RE.split(body)]


def is_separator_cells(cells: list[str]) -> bool:
    return bool(cells) and all(SEPARATOR_CELL_RE.match(c) for c in cells)


# ---------------------------------------------------------------- 姊妹技能定位
#
# 定位逻辑（候选顺序 / 探针 / 环境变量语义）全部收敛在 _weekly.locate_weekly_skill，
# 三个脚本共用同一实现——早先各写一套，会导致「第一步读 A 份安装、第二步读 B 份安装」
# 的脑裂日报。本脚本用 require=NEED_BASELINE 声明自己要的是 assets/baseline.md，
# 于是「装了但缺 baseline.md」能报出精确错误，而不是误导性的「找不到姊妹技能」。


# ---------------------------------------------------------------- 取数：subprocess 优先


# 「baseline.py 不存在」对 meta 与 show 会各触发一次，提示只打一次即可。
_MISSING_SCRIPT_NOTICED = False


def _run_baseline(weekly_dir: Path, subcommand: str) -> str | None:
    """跑 `baseline.py <subcommand>`，成功返回 stdout，失败返回 None（调用方回退直接解析）。"""
    global _MISSING_SCRIPT_NOTICED
    script = weekly_dir / "scripts" / "baseline.py"
    if not script.is_file():
        # 其余回退分支都有「提示：」，这一条早先是完全静默的——
        # 于是「姊妹技能被裁剪过」这件事会悄悄发生，没人知道走了次优解析路径。
        if not _MISSING_SCRIPT_NOTICED:
            _MISSING_SCRIPT_NOTICED = True
            err(f"提示：{rel_display(script)} 不存在（周更技能可能被裁剪），"
                f"改为直接解析 baseline.md。")
        return None
    try:
        proc = subprocess.run(
            [sys.executable, str(script), subcommand],
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT,
        )
    except Exception as exc:  # noqa: BLE001 - 任何失败都只是触发回退，不该中断日更
        err(f"提示：调用 {rel_display(script)} {subcommand} 失败（{scrub(exc)}），改为直接解析 baseline.md。")
        return None
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        head = detail[0] if detail else f"exit {proc.returncode}"
        err(f"提示：{rel_display(script)} {subcommand} 返回非零，改为直接解析 baseline.md。"
            f"（子进程错误：{scrub(head)}）" if head and "Traceback" not in head
            else f"提示：{rel_display(script)} {subcommand} 返回非零，改为直接解析 baseline.md。")
        return None
    return proc.stdout


def parse_meta_line(text: str) -> dict[str, str]:
    """从 baseline.md 表格之前的部分抓三项元数据。仅回退路径使用。"""
    meta: dict[str, str] = {}
    pattern = re.compile(r"^(数据日期|更新时间|标的数)\s*[:：]\s*(.*)$")
    for line in text.splitlines():
        if line.strip().startswith("|"):
            break
        m = pattern.match(line.strip())
        if m and m.group(1) not in meta:
            meta[m.group(1)] = m.group(2).strip()
    return meta


def load_meta(weekly_dir: Path, baseline_path: Path) -> tuple[dict, str]:
    """返回 (meta, source)：source ∈ {"baseline.py meta", "直接解析 baseline.md"}。"""
    out = _run_baseline(weekly_dir, "meta")
    if out:
        try:
            data = json.loads(out)
            if isinstance(data, dict):
                return {
                    "date": data.get("date"),
                    "updated_at": data.get("updated_at"),
                    "count": data.get("count"),
                }, "baseline.py meta"
        except json.JSONDecodeError:
            err("提示：baseline.py meta 的输出不是合法 JSON，改为直接解析 baseline.md。")

    raw = parse_meta_line(read_text(baseline_path))
    count: int | None
    try:
        count = int(raw.get("标的数", "").strip())
    except (TypeError, ValueError):
        count = None
    return {
        "date": raw.get("数据日期") or None,
        "updated_at": raw.get("更新时间") or None,
        "count": count,
    }, "直接解析 baseline.md"


def load_table_lines(weekly_dir: Path, baseline_path: Path) -> tuple[list[str], str]:
    """返回 (以 `|` 开头的表格行, source)。首选 baseline.py show。"""
    out = _run_baseline(weekly_dir, "show")
    if out:
        lines = [ln.strip() for ln in out.splitlines() if ln.strip().startswith("|")]
        if lines:
            return lines, "baseline.py show"
        err("提示：baseline.py show 没有输出任何表格行，改为直接解析 baseline.md。")
    text = read_text(baseline_path)
    lines = [ln.strip() for ln in text.splitlines() if ln.strip().startswith("|")]
    return lines, "直接解析 baseline.md"


# ---------------------------------------------------------------- 数据模型


class ParseError(Exception):
    """表格结构不可用时抛出。"""


def parse_rows(lines: list[str]) -> tuple[list[str], list[dict[str, str]]]:
    """把表格行解析成 (表头, 行字典列表)。列按**名字**取，不按位置，避免列序调整后错位。"""
    if not lines:
        raise ParseError(
            "基准表里没有任何以 `|` 开头的表格行。\n"
            "周更技能可能尚未跑过第一轮，请先运行 ai-industry-weekly 建立基准表。"
        )
    header = split_cells(lines[0])
    missing = [c for c in REQUIRED_COLUMNS if c not in header]
    if missing:
        raise ParseError(
            f"基准表表头缺少必需列：{'、'.join(missing)}\n"
            f"实际表头：| {' | '.join(header)} |\n"
            "日更依赖「代码/主题/8层/瓶颈/评级/论点/估值性格」这几列；"
            "若周更技能改过列名，请同步更新本脚本的 REQUIRED_COLUMNS。"
        )
    idx = {name: header.index(name) for name in REQUIRED_COLUMNS}

    rows: list[dict[str, str]] = []
    for line in lines[1:]:
        cells = split_cells(line)
        if is_separator_cells(cells):
            continue
        if len(cells) != len(header):
            code = cells[idx[COL_CODE]] if idx[COL_CODE] < len(cells) else "?"
            raise ParseError(
                f"数据行「{code}」有 {len(cells)} 个单元格，应为 {len(header)} 个。\n"
                "列错位会把论点读成评级，日更的质量闸门会整个错掉，因此直接拒绝。\n"
                "请在周更技能里重跑 `baseline.py validate` 修复基准表。"
            )
        rows.append({
            "ticker": cells[idx[COL_CODE]],
            "theme": cells[idx[COL_THEME]],
            "layer": cells[idx[COL_LAYER]],
            "bottleneck": cells[idx[COL_BOTTLENECK]],
            "rating": cells[idx[COL_RATING]],
            "thesis": cells[idx[COL_THESIS]],
            "valuation": cells[idx[COL_VALUATION]],
        })
    if not rows:
        raise ParseError("基准表只有表头、没有任何数据行；请先在周更技能里跑一轮 write 建立基准。")
    return header, rows


def rating_layer_bottleneck(row: dict[str, str]) -> str:
    """「评级·层级·瓶颈」列的写法，如 🟢·L1·🔥①②③（见 references/output-format.md）。"""
    return "·".join(
        (row.get("rating") or "—", row.get("layer") or "—", row.get("bottleneck") or "—")
    )


# ---------------------------------------------------------------- 陈旧检查


def staleness(meta_date: str | None, today: date | None = None) -> dict:
    """基准表「数据日期」距今多少天，是否超过 STALE_DAYS。"""
    today = today or date.today()
    info = {"date": meta_date, "age_days": None, "stale": False, "future": False,
            "threshold_days": STALE_DAYS}
    if not meta_date:
        info["stale"] = True
        info["reason"] = "基准表没有「数据日期」元数据，无法判断新鲜度"
        return info
    m = DATE_RE.match(meta_date.strip())
    if not m:
        info["stale"] = True
        info["reason"] = f"「数据日期」无法解析：{meta_date}"
        return info
    try:
        parsed = datetime.strptime(m.group(1), "%Y-%m-%d").date()
    except ValueError:
        info["stale"] = True
        info["reason"] = f"「数据日期」不是合法日期：{meta_date}"
        return info
    age = (today - parsed).days
    info["age_days"] = age
    # 未来日期先判：负的 age 永远不可能 > STALE_DAYS，早先只往 info["reason"] 塞一句话
    # 却没有任何标志位，于是 stale_alert() 返回 None、--check 照打「OK：在新鲜期内」，
    # 同一屏里同时出现「距今 -120 天」和「OK」——那句 reason 是纯死代码。
    if age < 0:
        info["future"] = True
        info["reason"] = f"基准表数据日期 {m.group(1)} 在未来（距今 {age} 天）"
    elif age > STALE_DAYS:
        info["stale"] = True
        info["reason"] = f"基准表数据日期 {m.group(1)}，距今 {age} 天（阈值 {STALE_DAYS} 天）"
    return info


def stale_alert(info: dict) -> dict | None:
    """陈旧告警记录。返回 None 表示无需告警。

    横幅文字与 JSON 字段出自**同一条**记录：`lines` 就是文本分支逐字打印的内容，
    其余键是同一段话的结构化形态（条件、两侧数字、成因、后果、处置）。
    两边各写一份，早晚会出现「JSON 说没事、横幅说已过期」——而窄读 JSON 的调用方
    只会看见前者。
    """
    if not info.get("stale"):
        return None
    reason = info.get("reason") or "基准表已过期"
    cause = f"周更技能 {WEEKLY_SKILL_NAME} 的定位是每周跑一次；超过 {STALE_DAYS} 天说明这一轮没跑。"
    consequence = "评级（🟢/🔵/🟡/🔴）是分桶规则里优先级最高的质量闸门，过期评级会直接影响买入桶。"
    remedy = "先跑一轮周更技能刷新 assets/baseline.md，或在日更报告里显式注明本表已过期。"
    return {
        "id": "baseline_stale",
        "headline": "基本面参考表已陈旧 —— 本日评级可能过时",
        "condition": f"基准表「数据日期」距今 > {STALE_DAYS} 天，或该日期缺失/无法解析",
        "blocking": False,
        "summary": reason,
        "baseline_date": info.get("date"),
        "age_days": info.get("age_days"),
        "threshold_days": STALE_DAYS,
        "cause": cause,
        "consequence": consequence,
        "remedy": remedy,
        "lines": [
            "=" * 68,
            "⚠️⚠️  基本面参考表已陈旧 —— 本日评级可能过时  ⚠️⚠️",
            f"  {reason}",
            f"  {cause}",
            f"  {consequence}",
            f"  处理：{remedy}",
            "=" * 68,
        ],
    }


def future_alert(info: dict) -> dict | None:
    """基准表日期在未来时的告警记录。与陈旧告警互斥，但同样不阻断。

    未来日期意味着「距今 N 天」这个量本身没意义，陈旧闸门跟着一起失效：
    表可能其实很旧，只是日期被写成了未来，于是永远不会触发陈旧告警。
    """
    if not info.get("future"):
        return None
    reason = info.get("reason") or "基准表数据日期在未来"
    cause = "多半是 baseline.md 的「数据日期」被手改错了，或本机系统日期不对。"
    # 后果原文在横幅里折成两行，这里保留同样的两段，拼起来即 JSON 里的整句，
    # 不另写一份措辞——重写一遍就是给同一件事留两个版本。
    conseq_a = f"{STALE_DAYS} 天陈旧闸门在此情形下永远不会触发——表可能其实很旧，"
    conseq_b = "只是日期写成了未来，于是「新鲜」这个结论完全不可信，本日评级是否最新无法判断。"
    remedy = "先核对本机日期；若确是表里日期写错，回到周更技能重跑 `baseline.py write` 覆写。"
    return {
        "id": "baseline_date_in_future",
        "headline": "基准表数据日期在未来 —— 新鲜度判定整体失效",
        "condition": "基准表「数据日期」晚于本机今天（age_days < 0）",
        "blocking": False,
        "summary": reason,
        "baseline_date": info.get("date"),
        "age_days": info.get("age_days"),
        "threshold_days": STALE_DAYS,
        "cause": cause,
        "consequence": conseq_a + conseq_b,
        "remedy": remedy,
        "lines": [
            "=" * 68,
            "⚠️⚠️  基准表数据日期在未来 —— 新鲜度判定整体失效  ⚠️⚠️",
            f"  {reason}，即比本机今天还晚。",
            f"  {cause}",
            f"  后果：{conseq_a}",
            f"  {conseq_b}",
            f"  处理：{remedy}",
            "=" * 68,
        ],
    }


def universe_alert(data: dict) -> dict | None:
    """基准表行数 ≠ universe.json 标的数 时的告警记录（跨文件交叉校验）。

    为什么需要这一条：--check 早先只比对「表头里自称的标的数」与「实际数据行数」，
    两个数来自 baseline.md **同一个文件**，周更自己写的时候就是配平的，几乎不会分歧。
    真正会分歧的是**跨文件**：universe.json 是标的清单的唯一权威（日更第二步的
    technicals.py / perp_quotes.py 都读它），baseline.md 则是 `baseline.py write`
    按当时的 universe.json 生成的快照。在周更里改了 universe.json 却还没重跑 write
    的那段时间里，两者就会对不上，而日更的第一步读前者、第二步读后者——
    早先三个脚本各说各话、全部 exit 0、零告警，直接产出「第一步 46 档、第二步 3 档」
    的自相矛盾日报。
    """
    uni = data.get("universe") or {}
    count = uni.get("count")
    rows = (data.get("meta") or {}).get("row_count")
    if not isinstance(count, int) or not isinstance(rows, int) or count == rows:
        return None
    uni_path = uni.get("path") or "assets/universe.json"
    base_path = data.get("baseline_path") or "assets/baseline.md"
    cause_a = "这说明周更技能改过 universe.json（增删了标的），但还没重跑 `baseline.py write`"
    cause_b = "把基准表重新生成——两个文件停在了不同的标的清单上。"
    conseq_a = f"日更第一步（产业质量表，读 baseline.md）报 {rows} 档，"
    conseq_b = (f"第二步（technicals.py / perp_quotes.py，读 universe.json）报 {count} 档，"
                "同一份日报对不上。")
    remedy_a = f"日更对周更只读、无法自行修复：请先到 {WEEKLY_SKILL_NAME} 跑"
    remedy_b = "`python3 scripts/baseline.py write` 让两个文件对齐，再跑日更。"
    return {
        "id": "universe_row_count_mismatch",
        "headline": "基准表与标的清单对不上 —— 日报第一步与第二步会自相矛盾",
        "condition": "基准表数据行数 ≠ universe.json 的 tickers 条数",
        "blocking": False,
        "summary": f"标的清单 {count} 个标的 ≠ 基准表 {rows} 行数据",
        "universe_path": uni_path,
        "universe_count": count,
        "baseline_path": base_path,
        "baseline_row_count": rows,
        # cause / remedy 在横幅里断在反引号处，拼成整句时补一个空格；
        # consequence 断在中文逗号后，补空格反而是错的。
        "cause": f"{cause_a} {cause_b}",
        "consequence": conseq_a + conseq_b,
        "remedy": f"{remedy_a} {remedy_b}",
        "lines": [
            "=" * 68,
            "⚠️⚠️  基准表与标的清单对不上 —— 日报第一步与第二步会自相矛盾  ⚠️⚠️",
            f"  标的清单 {uni_path}：{count} 个标的",
            f"  基准表   {base_path}：{rows} 行数据",
            f"  {cause_a}",
            f"  {cause_b}",
            f"  后果：{conseq_a}",
            f"  {conseq_b}",
            f"  {remedy_a}",
            f"  {remedy_b}",
            "=" * 68,
        ],
    }


def alerts(data: dict) -> list[dict]:
    """本次运行触发的全部告警记录（未来日期 / 陈旧 / 清单不一致），顺序即打印顺序。

    三者**都不阻断**（每条记录的 `blocking` 恒为 False，--check 仍 exit 0）：
    数据本身解析得出来，日更照常出报告；但它们指向的都是「周更那边出了事」，
    日更对周更只读，只能提示，所以必须同时出现在人类看得见的地方（横幅）
    与机器看得见的地方（JSON 的 alerts[]）。
    """
    fresh = data.get("freshness") or {}
    recs = (future_alert(fresh), stale_alert(fresh), universe_alert(data))
    return [rec for rec in recs if rec]


def banner_lines(recs: list[dict]) -> list[str]:
    """把告警记录渲染成醒目横幅，空行分隔。文本分支的字全部取自记录的 `lines`。"""
    out: list[str] = []
    for rec in recs:
        if out:
            out.append("")
        out.extend(rec["lines"])
    return out


def alert_banners(data: dict) -> list[str]:
    """本次运行要打的全部醒目横幅（未来日期 / 陈旧 / 清单不一致），空行分隔。"""
    return banner_lines(alerts(data))


def degraded_reasons(data: dict, recs: list[dict]) -> list[str]:
    """本次运行的全部降级原因（短句）。空列表 == 未降级。

    口径与仓库其它脚本一致：自检未通过、回退档触发、源不可达、
    或任何字段因缺数记 N/A，都算降级。三类告警各自也是一条降级原因——
    它们不阻断，但「本日评级可能过时」「两个文件停在不同的标的清单上」正是
    调用方在动笔写报告之前必须先看到的东西，只落在 stderr 上等于没说。
    """
    reasons = [rec["summary"] for rec in recs]

    src = data.get("source") or {}
    if src.get("table") != "baseline.py show":
        reasons.append(f"表格取数回退档：{src.get('table')}（baseline.py show 不可用）")
    if src.get("meta") != "baseline.py meta":
        reasons.append(f"元数据取数回退档：{src.get('meta')}（baseline.py meta 不可用）")

    meta = data.get("meta") or {}
    declared = meta.get("declared_count")
    if isinstance(declared, int) and declared != meta.get("row_count"):
        reasons.append(f"元数据声明「标的数: {declared}」，实际数据行 {meta.get('row_count')} 行，"
                       "两者不一致（同一文件内）")

    uni = data.get("universe") or {}
    if uni.get("error"):
        # 交叉校验做不成 → universe.count 记 N/A（None），这属于「因缺数记 N/A」。
        reasons.append(f"无法与标的清单交叉校验——{uni.get('path')} {uni['error']}，标的数记 N/A")
    return reasons


# ---------------------------------------------------------------- 加载入口


def load_universe(weekly_dir: Path) -> dict:
    """读周更技能的 assets/universe.json，只取 tickers 的条数，用于与基准表行数交叉校验。

    读不到不是错误：universe.json 不是本脚本的依赖（locate 时只 require baseline.md），
    缺了只是「这次校验不了」，记下原因即可，绝不因此让日更失败。
    """
    path = weekly_dir / "assets" / "universe.json"
    info = {"path": rel_display(path), "count": None, "error": None}
    if not path.is_file():
        info["error"] = "文件不存在"
        return info
    try:
        data = json.loads(read_text(path))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        info["error"] = f"读取/解析失败：{scrub(exc)}"
        return info
    tickers = data.get("tickers") if isinstance(data, dict) else None
    if not isinstance(tickers, list):
        info["error"] = "里没有 tickers 数组"
        return info
    info["count"] = len(tickers)
    return info


def load_all() -> dict:
    """定位 + 取数 + 解析 + 新鲜度判定，返回一个自洽的数据包。"""
    # require=NEED_BASELINE：命中安装却缺 baseline.md 时，由共享模块抛出
    # 「找到了周更技能但缺 assets/baseline.md」的精确错误（不会误报成「没装」，
    # 也不会为了凑齐文件而偷偷换到另一份安装）。
    weekly_dir = locate_weekly_skill(require=NEED_BASELINE)
    baseline_path = weekly_dir / "assets" / "baseline.md"
    lines, table_source = load_table_lines(weekly_dir, baseline_path)
    header, rows = parse_rows(lines)
    meta, meta_source = load_meta(weekly_dir, baseline_path)
    fresh = staleness(meta.get("date"))
    # 交叉校验用：universe.json 是标的清单的唯一权威，baseline.md 只是按它生成的快照。
    # 只多读一个小 JSON，换掉「第一步 46 档、第二步 3 档」这种静默自相矛盾。
    universe = load_universe(weekly_dir)
    return {
        "weekly_dir": rel_display(weekly_dir),
        "baseline_path": rel_display(baseline_path),
        "source": {"table": table_source, "meta": meta_source},
        "meta": {
            "date": meta.get("date"),
            "updated_at": meta.get("updated_at"),
            "declared_count": meta.get("count"),
            "row_count": len(rows),
        },
        "freshness": fresh,
        "universe": universe,
        "columns": header,
        "rows": rows,
    }


def find_row(rows: list[dict[str, str]], ticker: str) -> dict[str, str] | None:
    """大小写不敏感精确匹配；再退一步允许省略 .HK/.KS 后缀的前缀匹配。"""
    want = ticker.strip().upper()
    for row in rows:
        if row["ticker"].strip().upper() == want:
            return row
    prefix_hits = [r for r in rows if r["ticker"].strip().upper().split(".")[0] == want]
    return prefix_hits[0] if len(prefix_hits) == 1 else None


# ---------------------------------------------------------------- 输出


def print_meta_header(data: dict) -> None:
    meta = data["meta"]
    print(f"产业质量参考表（来自姊妹技能 {WEEKLY_SKILL_NAME}）")
    print(f"  来源：{data['baseline_path']}（{data['source']['table']}）")
    print(f"  基本面表最近更新：{meta['date'] or '未知'}"
          + (f"（距今 {data['freshness']['age_days']} 天）"
             if data["freshness"].get("age_days") is not None else ""))
    print(f"  标的数：{meta['row_count']}")


def cmd_table(data: dict) -> int:
    banner = alert_banners(data)
    for line in banner:
        print(line)
    if banner:
        print()
    print_meta_header(data)
    print()

    # 以 markdown 紧凑表格输出而非空格对齐：表里混着中文、emoji（🟢🔥）与带圈数字（①②③），
    # 带圈数字属于 East Asian Ambiguous，不同终端宽度不同，空格对齐必然在某些终端上歪掉；
    # 而这段输出本来就要贴进日更报告（markdown）与 Slack，管道格式在哪儿都成立。
    cols = [
        ("代码", lambda r: r["ticker"]),
        ("评级", lambda r: r["rating"]),
        ("层级", lambda r: r["layer"]),
        ("瓶颈", lambda r: r["bottleneck"]),
        ("主题", lambda r: r["theme"]),
    ]
    print("| " + " | ".join(name for name, _ in cols) + " |")
    print("| " + " | ".join([":---"] * len(cols)) + " |")
    for r in data["rows"]:
        print("| " + " | ".join(get(r) or "—" for _, get in cols) + " |")
    return 0


def cmd_json(data: dict) -> int:
    recs = alerts(data)
    for line in banner_lines(recs):
        err(line)
    reasons = degraded_reasons(data, recs)
    # 成功路径也要带信封：`ok` / `command` / `result_produced` 若只出现在失败载荷里，
    # 「缺这个键」就等于「成功」，窄读的调用方写 `if not d.get("ok")` 会把每一次正常
    # 运行读成失败——那正是这次要修的歧义，只是挪到了下一层。`ok` 由 reasons 推出，
    # 不是写死的 true（口径同 check_payload）。
    payload = {
        "command": "table",
        "ok": not reasons,
        "blocking": False,
        "exit_code": 0,
        "result_produced": True,
        "error": None,
    }
    payload.update(data)
    payload["rows"] = [
        dict(r, summary=rating_layer_bottleneck(r)) for r in data["rows"]
    ]
    # 横幅同时进 JSON：stderr 是冗余通道，不能是唯一通道——窄读 stdout 的调用方
    # 拿到的必须与人类看到的是同一批告警。
    payload["alerts"] = recs
    payload["alerts_reason"] = None
    payload["degraded"] = bool(reasons)
    payload["degraded_reasons"] = reasons
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def cmd_ticker(data: dict, ticker: str, as_json: bool) -> int:
    row = find_row(data["rows"], ticker)
    if row is None:
        # stderr 这三行逐字不变（人类分支一个字节不动），只是先攒起来，好让
        # --json 分支把同一批字送进 error.message——stderr 是冗余通道，不是唯一通道。
        codes = [r["ticker"] for r in data["rows"]]
        head = ticker.strip().upper()[:2]
        near = [c for c in codes if c.upper().startswith(head)] if head else []
        lines = [f"错误：基准表里没有标的「{ticker}」。"]
        if near:
            lines.append(f"相近的代码：{'、'.join(near)}")
        lines.append(
            f"表内共 {len(codes)} 个标的；标的清单由 {WEEKLY_SKILL_NAME}/assets/universe.json 维护，"
            "新增标的请改那里并重跑周更技能。")
        for line in lines:
            err(line)
        if as_json:
            # 这条路径基准表**读出来了**，三类告警是可判定的：照常算、照常带走，
            # 不沿用 failure_payload 里那份 null（那是「读都没读出来」的口径）。
            # 横幅同样照常上 stderr，与本函数成功分支一致——否则「基准表已陈旧」
            # 只在 JSON 里有，盯着终端的人什么都看不到。
            recs = alerts(data)
            for line in banner_lines(recs):
                err(line)
            # ticker 是用户输入，整段可能就是个绝对路径。failure_payload() 只 scrub
            # 它收到的 message；下面两处若用原串，同一份载荷里就会出现 error.message
            # 已折叠成 ~ 而 degraded_reasons / ticker 仍是完整家目录路径——
            # 而 degraded_reasons[] 正是要被抄进日报正文的那个字段（公开仓库红线）。
            safe = scrub(ticker)
            payload = failure_payload("ticker", "ticker_not_found", "\n".join(lines))
            payload["ticker"] = safe
            payload["known_ticker_count"] = len(codes)
            payload["near_matches"] = near
            payload["alerts"] = recs
            payload["alerts_reason"] = None
            payload["degraded_reasons"] = ([f"基准表里没有标的「{safe}」"]
                                           + degraded_reasons(data, recs))
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 1

    if as_json:
        recs = alerts(data)
        for line in banner_lines(recs):
            err(line)
        reasons = degraded_reasons(data, recs)
        # 行字段照旧摊在顶层（既有调用方按 payload["rating"] 取，不能改），信封另加一层。
        payload = {
            "command": "ticker",
            "ok": not reasons,
            "blocking": False,
            "exit_code": 0,
            "result_produced": True,
            "error": None,
        }
        payload.update(row)
        payload["summary"] = rating_layer_bottleneck(row)
        payload["alerts"] = recs
        payload["alerts_reason"] = None
        payload["degraded"] = bool(reasons)
        payload["degraded_reasons"] = reasons
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    banner = alert_banners(data)
    for line in banner:
        print(line)
    if banner:
        print()
    # 第一行即摘要，格式贴合 output-format.md 的「评级·层级·瓶颈」列（如 🟢·L1·🔥①②③）
    print(f"{row['ticker']}  {rating_layer_bottleneck(row)}  {row['theme']}")
    print(f"  论点：{row['thesis']}")
    print(f"  估值性格：{row['valuation']}")
    print(f"  基本面表最近更新：{data['meta']['date'] or '未知'}")
    return 0


def check_items(data: dict) -> list[dict]:
    """--check 的逐条自检结论。文本分支逐行打 `message`，--json 原样带走同一批记录。

    `ok=False` 的条目是「查了、没过」，但**都不阻断**：真正 exit 1 的三类
    （定位不到姊妹技能 / 基准表解析不了 / 基准表不是合法 UTF-8）在 load_all()
    就抛出来了，根本走不到这里。
    """
    meta = data["meta"]
    items: list[dict] = [
        {"id": "locate_weekly_skill", "ok": True,
         "message": f"OK：已定位姊妹技能 {data['weekly_dir']}"},
        {"id": "parse_baseline", "ok": True,
         "message": f"OK：已解析 {data['baseline_path']}（{data['source']['table']}），"
                    f"{meta['row_count']} 个标的、{len(data['columns'])} 列"},
    ]
    declared = meta.get("declared_count")
    if isinstance(declared, int) and declared != meta["row_count"]:
        # 表内自洽性：两个数都来自 baseline.md，周更写表时本来就配平，很少分歧。
        # 真正的分歧在下面那条跨文件校验里。
        items.append({
            "id": "declared_vs_rows", "ok": False,
            "message": f"注意：元数据声明「标的数: {declared}」，实际数据行 {meta['row_count']} 行，两者不一致（同一文件内）。",
        })

    uni = data.get("universe") or {}
    if isinstance(uni.get("count"), int):
        if uni["count"] == meta["row_count"]:
            items.append({
                "id": "universe_cross_check", "ok": True,
                "message": f"OK：标的清单 {uni['path']} 的 {uni['count']} 个标的与基准表 {meta['row_count']} 行一致。",
            })
        # 不一致由横幅（alerts 里的 universe_row_count_mismatch）统一报，这里不重复
    elif uni.get("error"):
        items.append({
            "id": "universe_cross_check", "ok": False,
            "message": f"注意：无法与标的清单交叉校验——{uni.get('path')} {uni['error']}。"
                       "本次只能确认基准表自身可解析，无法确认它与第二步用的标的清单是同一份。",
        })

    fresh = data["freshness"]
    items.append({
        "id": "baseline_date", "ok": True,
        "message": f"OK：基本面表最近更新 {meta['date'] or '未知'}"
                   + (f"（距今 {fresh['age_days']} 天）"
                      if fresh.get("age_days") is not None else ""),
    })
    if not fresh.get("stale") and not fresh.get("future"):
        items.append({
            "id": "freshness", "ok": True,
            "message": f"OK：基准表在 {STALE_DAYS} 天新鲜期内。",
        })
    return items


def check_payload(data: dict, items: list[dict], recs: list[dict]) -> dict:
    """--check --json 的载荷。exit 码语义与文本分支**完全一致**：这里恒为 0。

    `ok` 描述的是「所有自检是否全过」（含三类告警），**不是**「要不要停线」——
    后者看 `blocking` / `exit_code`。两者刻意分开：三类告警都指向周更那一侧，
    日更只读、修不了，所以照常出报告；但把 ok 写死成 true 就成了「查过了、没事」，
    正是这次要修的那种谎。
    """
    reasons = degraded_reasons(data, recs)
    return {
        "command": "check",
        "ok": all(item["ok"] for item in items) and not recs,
        "blocking": False,
        "exit_code": 0,
        "error": None,
        "weekly_dir": data["weekly_dir"],
        "baseline_path": data["baseline_path"],
        "source": data["source"],
        "meta": data["meta"],
        "freshness": data["freshness"],
        "universe": data["universe"],
        "checks": items,
        "alerts": recs,
        "degraded": bool(reasons),
        "degraded_reasons": reasons,
    }


# 各 --json 子命令在拒绝路径上的「结果位」，恒为空值。口径抄周更技能 baseline.py
# 的 `diff --json`：拒绝时给 result=null / summary_produced=false，让 JSON 里压根
# 不存在一个「长得像正常结果」的对象——窄读 stdout 的调用方因此不可能把
# 「拒绝了」读成「跑完了、表里就是没有」。
# ⚠️ ticker **没有**结果位：成功时行字段是摊在顶层的（payload["rating"] 等），
# 并不存在一个 row 对象。凭空塞 "row": null 的话，成功路径上 payload.get("row")
# 同样是 None——「查到了」与「没查到」再次不可区分。拒绝与否一律看 result_produced。
_EMPTY_RESULT: dict[str, dict] = {
    # checks 记 None 而非 []：连基准表都没读出来，逐条自检一条都没跑，
    # 而 [] 的语义是「跑了、都过了」。理由同下面 alerts 那条。
    "check": {"checks": None,
              "checks_reason": "基准表未读出，逐条自检一条都没跑"},
    "table": {"rows": None},
}


def failure_payload(command: str, error_id: str, message: str) -> dict:
    """任一 --json 子命令在阻断性失败时的载荷（exit 仍为 1，stderr 上的中文说明一字不改）。

    没有这一份，唯一能出 JSON 的路径就只剩「解析成功」那条，`ok` 便退化成写死的 true；
    对 `--json` 与 `--ticker X --json` 更糟——stdout 上一个字节都没有，
    「拒绝了」与「跑完了、什么都没有」在窄读下完全无法区分。

    `alerts` 记 null 而非 []：这些路径连基准表都没读出来，三类告警**无从判定**，
    而 [] 的语义是「查了、都没触发」——在这里那是谎话，与 ⚪️ / ❌ 的分野同理。
    """
    # 再过一次 scrub：错误文本里可能夹着别处的家目录绝对路径（例如用户把
    # AI_INDUSTRY_WEEKLY_DIR 指到了另一个用户名下），而 JSON 一样会被贴进日报。
    # 正常情形下这层是恒等变换，stderr 上那份保持原样、逐字不变。
    text = scrub(message)
    head = (text.splitlines() or [""])[0]
    payload: dict = {
        "command": command,
        "ok": False,
        "blocking": True,
        "exit_code": 1,
        "result_produced": False,
        "error": {"id": error_id, "message": text},
    }
    payload.update(_EMPTY_RESULT.get(command, {}))
    payload["alerts"] = None
    payload["alerts_reason"] = "基准表未读出，陈旧 / 未来日期 / 行数不一致三类告警均无从判定"
    payload["degraded"] = True
    payload["degraded_reasons"] = [head or f"前置检查失败：{error_id}"]
    return payload


def cmd_check(data: dict, as_json: bool = False) -> int:
    """只验证「能否定位 + 能否解析」。陈旧只告警、不算失败（数据本身是可用的）。"""
    items = check_items(data)
    recs = alerts(data)

    if as_json:
        # 早先 --check 分支在 --json 之前 return，于是 `--check --json` 静默打人类文本：
        # 三条横幅（含「先跑一轮周更技能」那句处置）没有任何机读等价物。
        print(json.dumps(check_payload(data, items, recs), ensure_ascii=False, indent=2))
        return 0

    for item in items:
        print(item["message"])

    # 三类横幅都只告警不阻断：数据能解析，日更照常跑；但坏的是周更那边，日更只能提示。
    banner = banner_lines(recs)
    if banner:
        print()
        for line in banner:
            print(line)
    return 0


# ---------------------------------------------------------------- CLI


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="industry_table.py",
        description=f"读取姊妹技能 {WEEKLY_SKILL_NAME} 维护的产业质量参考表"
                    "（assets/baseline.md），供日更「第一步」引用。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"日更技能不自己维护产业表；标的清单与评级口径全部以 {WEEKLY_SKILL_NAME} 为准。\n"
               f"周更超过 {STALE_DAYS} 天未跑会打印陈旧告警；基准表日期在未来、\n"
               f"或基准表行数与 assets/universe.json 的标的数对不上，同样各打一条横幅（均不阻断）。\n"
               f"姊妹技能装在别处时用环境变量 {WEEKLY_DIR_ENV} 指定其根目录。",
    )
    parser.add_argument("--json", action="store_true", help="以 JSON 输出（配合 --ticker 时只出该标的）")
    parser.add_argument("--ticker", metavar="CODE", help="只输出单个标的的摘要，如 NVDA / 0700.HK")
    parser.add_argument("--check", action="store_true",
                        help="只验证能否定位并解析姊妹技能的基准表，exit 0/1")
    return parser


def json_command(args) -> str:
    """这次跑的是哪个子命令。**main() 的分派也走它**——两处各写一套 if 的话，
    重排其一就会出现「JSON 里 command 说 table、实际跑的是 ticker」，
    而两边照常 exit 0/1，谁都不会发现。"""
    if args.check:
        return "check"
    if args.ticker:
        return "ticker"
    return "table"


def fail(args, error_id: str, message: str) -> int:
    """阻断性失败的统一出口：stderr 原样打（人类分支逐字不变），exit 恒为 1。

    只有这几类才阻断（定位不到姊妹技能 / 基准表解析不了 / 基准表不是合法 UTF-8 /
    读文件本身失败）；三类横幅告警一律不阻断，见 alerts()。
    **任何** --json 子命令都额外给一份 ok:false 的 JSON，否则窄读 stdout 的调用方
    只会看到「什么都没有」，与「一切正常」无法区分。早先这里的条件是
    `args.check and args.json`，于是同样一次阻断失败下 `--json` 与
    `--ticker X --json` 在 stdout 上一个字节都不吐，而 `--check --json` 有对象
    ——同一个 fail() 两种口径。

    不加 --json 的纯文本分支**逐字不变**：stdout 依旧一个字节不吐。这与周更技能
    baseline.py 一致（`diff` 的纯文本分支保持静默，只有 --json 分支拿错误对象）。
    """
    err(message)
    if args.json:
        print(json.dumps(failure_payload(json_command(args), error_id, message),
                         ensure_ascii=False, indent=2))
    return 1


def main(argv: list[str] | None = None) -> int:
    _configure_streams()
    args = build_parser().parse_args(argv)

    try:
        data = load_all()
    except WeeklySkillNotFound as exc:
        return fail(args, "weekly_skill_not_found", str(exc))
    except ParseError as exc:
        return fail(args, "baseline_unparseable", f"错误：产业质量参考表解析失败。\n{scrub(exc)}")
    except UnicodeDecodeError as exc:
        # UnicodeDecodeError 是 ValueError 的子类、**不是** OSError：早先只捕 OSError，
        # baseline.md 含非法 UTF-8 字节时会一路落到顶层兜底，打成
        # 「✗ 执行失败：UnicodeDecodeError: ...」这种没有处置建议的英文类型名。
        return fail(args, "baseline_not_utf8",
                    "错误：产业质量参考表不是合法的 UTF-8 文本，无法读取。\n"
                    f"  文件：{WEEKLY_SKILL_NAME}/{NEED_BASELINE}\n"
                    f"  解码失败：{scrub(exc)}\n"
                    "  基准表由 `baseline.py write` 以 UTF-8 覆写，出现非法字节多半是被别的工具"
                    "以非 UTF-8 编码手改过、或文件损坏。\n"
                    "  处理：回到周更技能重跑一轮 `baseline.py write` 覆写，或 "
                    "`git checkout -- assets/baseline.md` 回滚。")
    except OSError as exc:
        return fail(args, "baseline_read_failed", f"错误：读取产业质量参考表失败：{scrub(exc)}")

    # 子命令只判定一次（见 json_command 的注解）。
    cmd = json_command(args)
    if cmd == "check":
        return cmd_check(data, args.json)
    if cmd == "ticker":
        return cmd_ticker(data, args.ticker, args.json)
    if args.json:
        return cmd_json(data)
    return cmd_table(data)


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
