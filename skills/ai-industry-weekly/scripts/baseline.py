#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""滚动基准表（assets/baseline.md）的读取、校验、对比与覆写工具。

用途
----
本技能每周跑一次「AI 算力产业链产业质量参考表」。上一周的表就是这一周的基准：
先用 `diff` 对出「本周变动摘要」，再用 `write` 把新表覆写回 assets/baseline.md，
下周继续以它为基准。基准表因此是**滚动**的，没有第二份种子文件兜底，回滚手段取决于安装形态：
  * 装在 git 仓库里（推荐）：`git checkout <commit> -- assets/baseline.md`；
  * 装在 ~/.claude/skills/ 等非 git 目录：**覆写不可逆**——write 是原子替换，
    不保留上一版、不生成任何备份文件。想要版本历史请在 git 形态下使用，或自行 git init。

为什么校验要这么严
------------------
覆写是自动的，写坏一次就会把错误状态带进后续每一周，而且要人肉翻 git 才能发现。
所以本脚本的立场是**宁可拒写，也不写坏**：
  * `write` 必定先跑完整 `validate`，任一条不过就 exit 1，并且**完全不碰** baseline.md；
  * 通过后走「临时文件 + os.replace」原子写入，杜绝半截文件；
  * 标的数量与行序一律从 assets/universe.json 推导，脚本里不硬编码标的数；
  * 明令禁止「见 Slack thread」「已推送至频道」「表格见附件」「其余同前」「（略）」这类
    偷懒占位符代替整表——模型在上下文紧张时最容易这么干，一旦写进基准就永久丢失该行内容；
    但判定只抓确凿的占位（整格等值 / 带落点词的转引），不误伤「详见官方持仓表」这类正常分析。

`diff` 与 `write` 一样，先对新表跑完整 validate：结构不对的表对出来的「变动摘要」一定是错的，
宁可不出摘要也不能让错摘要进正文。

新表文件的解析规则：**宽松提取**——只取文件里以 `|` 开头的行，其余一律忽略
（`<<<产业表开始>>>` / `<<<产业表结束>>>` 标记、说明文字、空行都不影响）。
因此可以把运行结果正文的整块直接存成文件喂进来。

子命令
------
    baseline.py show                                   打印当前基准表（表头 + 分隔行 + 全部数据行）
    baseline.py meta                                   打印 JSON: {date, updated_at, count, path}（path 相对技能根目录）
    baseline.py validate <new_table.md>                只校验不写；通过 exit 0，失败 exit 1 并逐条列错
    baseline.py diff <new_table.md>                    先校验新表，通过才逐行对比输出评级变动 + 其他字段漂移（不写文件）
    baseline.py write <new_table.md> --date YYYY-MM-DD 先 validate，通过才原子覆写 baseline.md

纯标准库实现，无第三方依赖；所有路径以 __file__ 为锚，任意 cwd 下均可运行。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------- 路径与常量

SKILL_ROOT = Path(__file__).resolve().parent.parent
BASELINE_PATH = SKILL_ROOT / "assets" / "baseline.md"
UNIVERSE_PATH = SKILL_ROOT / "assets" / "universe.json"

# 表格行的元数据字段（写入时会被刷新）
META_KEYS = ("数据日期", "更新时间", "标的数")

# baseline.md 不存在时用来重建的顶部结构
DEFAULT_PREAMBLE = """<!--
  滚动基准表 — 本文件由 scripts/baseline.py write 自动覆写，请勿手改。
  每次运行：先与本表逐行对比产出「本周变动摘要」，再用新表覆写本文件。
  回滚：git 仓库内用 `git checkout <commit> -- assets/baseline.md`；
        非 git 目录下覆写不可逆（不留备份），只能用当周新表重跑 write 覆盖。
-->

# AI 算力产业链 · 产业质量参考表（滚动基准）

数据日期: -
更新时间: -
标的数: 0
"""

# ------------------------------------------------------------ 偷懒占位符黑名单
#
# 教训：早期版本用「归一化后子串包含」一刀切，粒度太粗，正常分析文字被大量误杀——
#   「持仓详见官方持仓表」命中「详见」、「后回落17.4%…」命中省略号、
#   「本周已推送新品」命中「已推送」、「论点同上季，未变」命中「同上」。
# 误杀的代价比漏杀还大：write 是流程第四步，此时正文已经交给用户，
# 为了过 lint 去改表里的分析文字，会让 baseline.md 与用户拿走的正文永久不一致，
# 下周 diff 就会报出一堆假漂移。
#
# 所以现在只抓两类**确凿**的偷懒占位：
#   A. 整格就是一个占位词（归一化并剥掉包裹的括号/句读后完全相等）——真占位一定很短；
#   B. 明确的「转引到别处」短语，必须带 Slack / 频道 / 附件 / 基准表 这类落点词才命中，
#      单独的「详见」「…」「已推送」不再构成理由。

# A. 整格等值：{归一化并剥壳后的整格文本: 人类可读说明}
PLACEHOLDER_EXACT = {
    "略": "「（略）」占位",
    "省略": "「省略」占位",
    "从略": "「从略」占位",
    "同上": "「同上」占位",
    "同前": "「同前」占位",
    "同上周": "「同上周」占位",
    "同前周": "「同前周」占位",
    "同上季": "「同上季」占位",
    "同基准表": "「同基准表」占位",
    "其余同前": "「其余同前」占位",
    "其余同上": "「其余同上」占位",
    "余同前": "「余同前」占位",
    "余同上": "「余同上」占位",
    "见上": "「见上」转引",
    "见上表": "「见上表」转引",
    "见上文": "「见上文」转引",
    "见前表": "「见前表」转引",
    "同左": "「同左」占位",
    "无变化": "「无变化」占位",
    "…": "省略号占位",
    "。。。": "省略号占位",
    "ditto": "「ditto」占位",
    "sameasabove": "「same as above」占位",
    "unchanged": "「unchanged」占位",
}

# B. 转引短语：出现在单元格任意位置即命中。每条都自带落点词，正常分析文字不会这么写。
PLACEHOLDER_ANYWHERE = [
    ("见slack", "「见 Slack thread」类转引"),
    ("slackthread", "「见 Slack thread」类转引"),
    ("推送至频道", "「已推送至频道」类转引"),
    ("推送到频道", "「已推送至频道」类转引"),
    ("发在频道", "「已发在频道」类转引"),
    ("见附件", "「表格见附件」类转引"),
    ("见基准表", "「见基准表」类转引"),
    ("同基准表", "「同基准表」占位"),
    ("其余同前", "「其余同前」占位"),
    ("其余同上", "「其余同上」占位"),
    ("余同前", "「余同前」占位"),
    ("余同上", "「余同上」占位"),
    ("全表见", "「全表见…」类转引"),
    ("整表见", "「整表见…」类转引"),
    ("完整表见", "「完整表见…」类转引"),
    ("完整表格见", "「完整表格见…」类转引"),
]

# 判定 A 之前先剥掉整格外面的括号与句读，让「（略）」「同上。」「[见上表]」同样命中
PLACEHOLDER_STRIP_CHARS = "()[]{}<>【】〔〕「」『』《》\"'“”‘’.,;:!?。，、；：！？·—–-_*~#`"

SEPARATOR_CELL_RE = re.compile(r"^:?-{2,}:?$")
PIPE_SPLIT_RE = re.compile(r"(?<!\\)\|")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
META_LINE_RE = re.compile(r"^(数据日期|更新时间|标的数)\s*[:：]\s*(.*)$")


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


def render_row(cells: list[str]) -> str:
    """把单元格渲染成紧凑表格行：`| a | b | c |`。

    baseline.md 的格式由本函数**唯一决定**，不受传入新表的排版影响，
    这样每周的 git diff 行数等于真实变动的行数（见 cmd_write 里的说明）。
    """
    return "| " + " | ".join(c.strip() for c in cells) + " |"


def rel_path(path: Path) -> str:
    """技能自身的路径一律相对技能根目录展示。

    运行结果正文会被贴进 Slack，绝不能把 ~/... 这种含本机用户名的绝对路径带出去。
    （用户自己传进来的临时文件路径不走这里；对外回显时只取 .name。）
    """
    try:
        return str(path.resolve().relative_to(SKILL_ROOT))
    except ValueError:
        return path.name


def read_text(path: Path) -> str:
    """UTF-8 读文件（容忍 BOM 与 CRLF）。"""
    return path.read_text(encoding="utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")


def normalize_for_scan(text: str) -> str:
    """占位符扫描用的归一化：去空白、转小写、全角括号转半角、省略号统一。"""
    out = []
    for ch in text:
        if ch.isspace() or ch == "　":
            continue
        out.append(ch)
    s = "".join(out).lower()
    s = s.replace("（", "(").replace("）", ")")
    s = s.replace("⋯", "…").replace("……", "…")
    s = re.sub(r"\.{3,}", "…", s)
    return s


def placeholder_hit(cell: str) -> str | None:
    """返回命中的偷懒占位说明；正常内容返回 None。规则见 PLACEHOLDER_* 上方注释。"""
    scan = normalize_for_scan(cell)
    if not scan:
        return None
    core = scan.strip(PLACEHOLDER_STRIP_CHARS)
    if core and core in PLACEHOLDER_EXACT:
        return PLACEHOLDER_EXACT[core]
    for needle, label in PLACEHOLDER_ANYWHERE:
        if needle in scan:
            return label
    return None


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


def extract_table_lines(text: str) -> list[str]:
    """宽松提取：只保留以 `|` 开头的行，其余（含 <<<产业表开始>>> 标记）忽略。"""
    return [ln.strip() for ln in text.splitlines() if ln.strip().startswith("|")]


def load_universe() -> dict:
    """读入并**校验** universe.json 自身。

    universe.json 是全套校验的唯一真值来源，它自己坏了会把 agent 逼进死循环：
    比如清单里 NVDA 重复，validate 会说「行数应为 47、缺行必须补齐」，
    agent 照做补一行 NVDA，validate 又说「重复标的 NVDA×2」——两条错误都指向产业表，
    从头到尾没人提 universe.json，改哪边都不对。所以这里先把清单本身查清楚，
    错误信息一律直接点名 assets/universe.json 的第几条、哪个代码。
    """
    where = rel_path(UNIVERSE_PATH)
    if not UNIVERSE_PATH.exists():
        err(f"错误：找不到标的清单 {where}")
        sys.exit(1)
    try:
        data = json.loads(read_text(UNIVERSE_PATH))
    except json.JSONDecodeError as exc:
        err(f"错误：{where} 不是合法 JSON：{exc}")
        sys.exit(1)
    if not isinstance(data, dict):
        err(f"错误：{where} 顶层应是一个 JSON 对象。")
        sys.exit(1)
    columns = data.get("columns")
    tickers = data.get("tickers")
    ratings = data.get("ratings")
    if not isinstance(columns, list) or not columns:
        err(f"错误：{where} 缺少 columns 列表")
        sys.exit(1)
    if not isinstance(tickers, list) or not tickers:
        err(f"错误：{where} 缺少 tickers 列表")
        sys.exit(1)
    if not isinstance(ratings, list) or not ratings:
        err(f"错误：{where} 缺少 ratings 列表")
        sys.exit(1)

    # ---- tickers 逐条体检：ticker 必填、非空、唯一；order 若写了则必须是唯一整数
    problems: list[str] = []
    seen_code: dict[str, int] = {}
    seen_order: dict[int, int] = {}
    for i, item in enumerate(tickers, start=1):
        if not isinstance(item, dict):
            problems.append(
                f'第 {i} 条不是对象（{item!r}）；每条标的必须形如 {{"order": {i}, "ticker": "XXX", ...}}'
            )
            continue
        if "ticker" not in item:
            problems.append(
                f'第 {i} 条缺少 "ticker" 字段：{json.dumps(item, ensure_ascii=False)}'
            )
            continue
        code = str(item.get("ticker", "")).strip()
        if not code:
            problems.append(f'第 {i} 条的 "ticker" 是空值：{json.dumps(item, ensure_ascii=False)}')
            continue
        if code in seen_code:
            problems.append(
                f"第 {i} 条的代码「{code}」与第 {seen_code[code]} 条重复；同一个标的只能出现一次"
            )
        else:
            seen_code[code] = i
        if "order" in item:
            order = item["order"]
            if isinstance(order, bool) or not isinstance(order, int):
                problems.append(f'第 {i} 条（{code}）的 "order" 应为整数，实际 {order!r}')
            elif order in seen_order:
                problems.append(
                    f"第 {i} 条（{code}）的 order={order} 与第 {seen_order[order]} 条重复；order 必须唯一"
                )
            else:
                seen_order[order] = i

    if problems:
        err(f"错误：标的清单 {where} 自身不合法，共 {len(problems)} 处：")
        for i, p in enumerate(problems, start=1):
            err(f"  {i}. {p}")
        err("")
        err(f"请修改 {where}（**不是**产业表）：每条标的的 ticker 必填、非空、不得重复；")
        err("order 若填写也不得重复。清单修好之前，产业表怎么改都过不了校验。")
        sys.exit(1)

    return data


def expected_tickers(universe: dict) -> list[str]:
    """按 order 升序返回代码序列；order 缺失时退回文件内顺序。"""
    items = list(enumerate(universe["tickers"]))
    items.sort(key=lambda pair: (pair[1].get("order", pair[0] + 1), pair[0]))
    return [str(item.get("ticker", "")).strip() for _, item in items]


def column_index(universe: dict, name: str, fallback: int) -> int:
    cols = universe["columns"]
    return cols.index(name) if name in cols else fallback


# ---------------------------------------------------------------- 表格解析


class Table:
    """从「宽松提取」出的 pipe 行里解析出的表格。解析本身不判对错，交给 validate。"""

    def __init__(self, source: Path, lines: list[str]):
        self.source = source
        self.raw_lines = lines
        self.header: list[str] = []
        self.separator_positions: list[int] = []  # 在 raw_lines 中的下标
        self.rows: list[tuple[int, list[str]]] = []  # (raw_lines 下标, 单元格)

        for idx, line in enumerate(lines):
            cells = split_cells(line)
            if idx == 0:
                self.header = cells
                continue
            if is_separator_cells(cells):
                self.separator_positions.append(idx)
                continue
            self.rows.append((idx, cells))

    @property
    def data_line_texts(self) -> list[str]:
        return [self.raw_lines[i] for i, _ in self.rows]

    def code_at(self, cells: list[str], code_idx: int) -> str:
        return cells[code_idx] if code_idx < len(cells) else ""


def load_table(path: Path) -> Table:
    if not path.exists():
        err(f"错误：找不到文件 {path}")
        sys.exit(1)
    lines = extract_table_lines(read_text(path))
    if not lines:
        err(f"错误：{path} 里没有任何以 `|` 开头的表格行。")
        err("提示：把运行结果正文（含 <<<产业表开始>>>/<<<产业表结束>>> 标记也无妨）整块存成文件即可，"
            "本脚本会自动忽略非表格内容。")
        sys.exit(1)
    return Table(path, lines)


# ---------------------------------------------------------------- baseline 元数据


def parse_preamble(text: str) -> tuple[list[str], dict[str, str]]:
    """把 baseline.md 拆成「表格之前的部分」与三项元数据。"""
    lines = text.splitlines()
    table_start = next(
        (i for i, ln in enumerate(lines) if ln.strip().startswith("|")), len(lines)
    )
    preamble = lines[:table_start]
    meta: dict[str, str] = {}
    for ln in preamble:
        m = META_LINE_RE.match(ln.strip())
        if m and m.group(1) not in meta:
            meta[m.group(1)] = m.group(2).strip()
    return preamble, meta


def baseline_meta() -> dict:
    """当前基准表的元数据；count 以实际数据行数为准（而非「标的数」那一行的声明值）。"""
    if not BASELINE_PATH.exists():
        return {"date": None, "updated_at": None, "count": 0, "path": rel_path(BASELINE_PATH)}
    text = read_text(BASELINE_PATH)
    _, meta = parse_preamble(text)
    table = Table(BASELINE_PATH, extract_table_lines(text))
    return {
        "date": meta.get("数据日期") or None,
        "updated_at": meta.get("更新时间") or None,
        "count": len(table.rows),
        "path": rel_path(BASELINE_PATH),
    }


# ---------------------------------------------------------------- validate


def validate_table(table: Table, universe: dict) -> list[str]:
    """返回错误列表；空列表表示通过。所有检查都跑完，方便一次性修。"""
    errors: list[str] = []
    columns: list[str] = universe["columns"]
    ratings: list[str] = universe["ratings"]
    codes_expected = expected_tickers(universe)
    ncol = len(columns)
    code_idx = column_index(universe, "代码", 0)
    rating_idx = column_index(universe, "评级", 4)

    # ---- 1. 表头逐字相符
    if len(table.header) != ncol:
        errors.append(
            f"[表头] 列数应为 {ncol}，实际 {len(table.header)}。"
            f"\n        期望：| {' | '.join(columns)} |"
            f"\n        实际：| {' | '.join(table.header)} |"
        )
    else:
        for i, (want, got) in enumerate(zip(columns, table.header), start=1):
            if want != got:
                errors.append(f"[表头] 第 {i} 列列名应为「{want}」，实际「{got}」。")

    # ---- 2. 分隔行恰好一行且紧跟表头
    if len(table.separator_positions) == 0:
        errors.append("[分隔行] 找不到分隔行（形如 `| :--- | :--- | ... |`），表头下必须有且只有一行。")
    elif len(table.separator_positions) > 1:
        positions = "、".join(str(p + 1) for p in table.separator_positions)
        errors.append(
            f"[分隔行] 分隔行有 {len(table.separator_positions)} 行（位于表格行第 {positions} 条），应恰好 1 行。"
            "\n        常见原因：文件里贴了不止一份表格（例如 Slack 副本），请只保留一份。"
        )
    elif table.separator_positions[0] != 1:
        errors.append(
            f"[分隔行] 分隔行应紧跟表头（即第 2 条表格行），实际在第 {table.separator_positions[0] + 1} 条。"
        )

    # ---- 3. 数据行数
    n_expected = len(codes_expected)
    n_actual = len(table.rows)
    if n_actual != n_expected:
        extra = (
            "\n        文件里可能贴了不止一份表格（例如 Slack 副本），请只保留一份。"
            if n_actual > n_expected
            else "\n        缺行通常是模型省略了部分标的，必须逐行补齐，不得用占位符代替。"
        )
        errors.append(
            f"[行数] 数据行数应为 {n_expected}（来自 universe.json 的 {n_expected} 个标的），实际 {n_actual}。{extra}"
        )

    # ---- 4. 每行列数
    for order, (raw_i, cells) in enumerate(table.rows, start=1):
        if len(cells) != ncol:
            code = table.code_at(cells, code_idx) or "?"
            errors.append(
                f"[列数] 第 {order} 行数据（代码 {code}）应有 {ncol} 个单元格，实际 {len(cells)}。"
                "\n        提示：论点/估值性格里若出现英文竖线 `|`，需写成 `\\|` 转义。"
            )

    # ---- 5. 代码序列与 universe.json 完全一致（缺失/多余/重复/乱序一网打尽）
    codes_actual = [table.code_at(cells, code_idx) for _, cells in table.rows]

    set_expected, set_actual = set(codes_expected), set(codes_actual)
    missing = [c for c in codes_expected if c not in set_actual]
    unknown = [c for c in codes_actual if c not in set_expected]
    if missing:
        errors.append(f"[缺失标的] universe.json 里有、表里没有：{'、'.join(missing)}")
    if unknown:
        errors.append(f"[多余标的] 表里有、universe.json 里没有：{'、'.join(dict.fromkeys(unknown))}")
    seen: dict[str, int] = {}
    for code in codes_actual:
        seen[code] = seen.get(code, 0) + 1
    dups = [c for c, n in seen.items() if n > 1 and c]
    if dups:
        errors.append(f"[重复标的] 以下代码出现多次：{'、'.join(f'{c}×{seen[c]}' for c in dups)}")

    # 缺行/多行会让后面所有行整体错位，这时只列前几条，避免刷屏淹没真正的问题
    cascading = bool(missing or unknown or dups or len(codes_actual) != len(codes_expected))
    cap = 5 if cascading else 12
    mismatches = 0
    for order in range(min(len(codes_expected), len(codes_actual))):
        want, got = codes_expected[order], codes_actual[order]
        if want != got:
            mismatches += 1
            if mismatches <= cap:
                errors.append(f"[行序] 第 {order + 1} 行代码应为「{want}」，实际「{got}」。")
    if mismatches > cap:
        tail = (
            "；多为上面的缺行/多行导致的整体错位，补齐后会自动消失"
            if cascading
            else "；请整体按 universe.json 的顺序重排"
        )
        errors.append(f"[行序] 另有 {mismatches - cap} 处行序不符，未逐条列出{tail}。")

    # ---- 6/7/8. 逐单元格：评级合法、非空、无占位符
    for order, (raw_i, cells) in enumerate(table.rows, start=1):
        code = table.code_at(cells, code_idx) or f"第{order}行"

        if rating_idx < len(cells):
            rating = cells[rating_idx]
            if rating not in ratings:
                errors.append(
                    f"[评级] {code}（第 {order} 行）评级「{rating}」不在允许集合 {'/'.join(ratings)} 内。"
                )

        for ci, cell in enumerate(cells):
            col_name = columns[ci] if ci < ncol else f"第{ci + 1}列"
            if cell == "":
                errors.append(f"[空单元格] {code}（第 {order} 行）的「{col_name}」为空；无数据请写 N/A 或 —。")
                continue
            label = placeholder_hit(cell)
            if label:
                errors.append(
                    f"[占位符] {code}（第 {order} 行）的「{col_name}」是{label}：{cell[:60]}"
                    "\n        整表必须逐行写全，禁止用任何形式的转引/省略代替内容。"
                )

    return errors


def cmd_validate(args: argparse.Namespace) -> int:
    universe = load_universe()
    table = load_table(Path(args.new_table).expanduser())
    errors = validate_table(table, universe)
    if errors:
        err(f"校验未通过：{table.source}")
        err(f"共 {len(errors)} 条问题：")
        for i, e in enumerate(errors, start=1):
            err(f"  {i}. {e}")
        err("")
        err("基准表未被修改。请修正上述问题后重跑。")
        return 1
    print(f"校验通过：{table.source}")
    print(f"表头 {len(table.header)} 列、数据 {len(table.rows)} 行，行序与 universe.json 完全一致。")
    return 0


# ---------------------------------------------------------------- diff


def structure_errors(table: Table, universe: dict) -> list[str]:
    """只查「逐行对比必须成立」的结构前提：单份表格、每行列数齐全、代码非空且不重复。

    刻意**不查**行数与行序——往 universe.json 新增一个标的的那一周，
    旧基准表比新表少一行是正常的，那正是 diff 要报出来的「新增标的」。
    用于基准表；新表走的是完整 validate_table。
    """
    errors: list[str] = []
    ncol = len(universe["columns"])
    code_idx = column_index(universe, "代码", 0)

    if len(table.separator_positions) != 1:
        errors.append(
            f"分隔行应恰好 1 行，实际 {len(table.separator_positions)} 行"
            "（文件里可能贴了不止一份表格）。"
        )
    seen: dict[str, int] = {}
    for order, (_, cells) in enumerate(table.rows, start=1):
        if len(cells) != ncol:
            errors.append(f"第 {order} 行有 {len(cells)} 个单元格，应为 {ncol} 个（列错位会把论点当评级读）。")
            continue
        code = cells[code_idx]
        if not code:
            errors.append(f"第 {order} 行的「代码」为空，无法用于逐行对比。")
        elif code in seen:
            errors.append(f"第 {order} 行的代码「{code}」与第 {seen[code]} 行重复。")
        else:
            seen[code] = order
    return errors


def _brief_change(old: str, new: str, width: int = 56) -> tuple[str, str]:
    """长文本对照：剥掉公共前后缀，只显示分歧处，两侧用 … 标示被省略的部分。"""
    i = 0
    limit = min(len(old), len(new))
    while i < limit and old[i] == new[i]:
        i += 1
    j = 0
    while j < limit - i and old[len(old) - 1 - j] == new[len(new) - 1 - j]:
        j += 1
    o_mid = old[i:len(old) - j]
    n_mid = new[i:len(new) - j]

    def wrap(mid: str) -> str:
        if mid == "":
            mid = "〔无〕"  # 纯新增/纯删除时，另一侧此处本来就没内容
        elif len(mid) > width:
            mid = mid[: width - 1] + "〔…〕"
        return ("…" if i > 0 else "") + mid + ("…" if j > 0 else "")

    return wrap(o_mid), wrap(n_mid)


def cmd_diff(args: argparse.Namespace) -> int:
    universe = load_universe()
    columns: list[str] = universe["columns"]
    code_idx = column_index(universe, "代码", 0)
    rating_idx = column_index(universe, "评级", 4)
    long_cols = {"论点", "估值性格"}

    new_table = load_table(Path(args.new_table).expanduser())

    # ---- 对比之前必须先完整校验新表。
    # 漏一行、多贴一份表、某行少一格，对出来的「变动摘要」看着完全合理其实全错
    # （漏行会报成「移除 XXX」，错位会把论点当评级报成「MU 🟢→HBM三巨头之一」），
    # 而它下一步就会被抄进运行结果正文。宁可一个字不出，也不出错摘要。
    errors = validate_table(new_table, universe)
    if errors:
        err(f"校验未通过，拒绝对比：{new_table.source}")
        err(f"共 {len(errors)} 条问题：")
        for i, e in enumerate(errors, start=1):
            err(f"  {i}. {e}")
        err("")
        err("本次**没有**产出任何变动摘要：结构不对的表对出来的结论一定是错的，不得贴进正文。")
        err("请按上面逐条修好新表后重跑 diff。")
        return 1

    if not BASELINE_PATH.exists():
        err(f"错误：基准表不存在：{rel_path(BASELINE_PATH)}")
        err("首次建立基准请直接跑 `baseline.py write <new_table.md> --date YYYY-MM-DD`。")
        return 1
    base_table = Table(BASELINE_PATH, extract_table_lines(read_text(BASELINE_PATH)))
    base_errors = structure_errors(base_table, universe)
    if base_errors:
        err(f"错误：基准表 {rel_path(BASELINE_PATH)} 结构已损坏，无法用于对比，共 {len(base_errors)} 处：")
        for i, e in enumerate(base_errors, start=1):
            err(f"  {i}. {e}")
        err("")
        err("基准表由 `baseline.py write` 自动写入，出现这种情况通常是被手工改过。")
        err("· 在 git 仓库里：git checkout <commit> -- assets/baseline.md 回滚后重跑。")
        err("· 非 git 目录：没有上一版可恢复，请用本周新表跑一次 `write` 覆盖修复。")
        return 1

    def to_map(t: Table) -> tuple[dict[str, list[str]], list[str]]:
        """两张表都已过结构校验（代码非空、不重复、每行列数齐全），可以直接建映射。"""
        mapping: dict[str, list[str]] = {}
        order: list[str] = []
        for _, cells in t.rows:
            code = cells[code_idx]
            mapping[code] = cells
            order.append(code)
        return mapping, order

    base_map, base_order = to_map(base_table)
    new_map, new_order = to_map(new_table)

    base_meta = baseline_meta()
    print(f"基准：{BASELINE_PATH.name}（数据日期 {base_meta['date'] or '未知'}，{len(base_order)} 行）")
    # diff 的 stdout 会被抄进「本周变动摘要」并推 Slack：只回显文件名，
    # 不带用户传进来的目录路径（临时文件常落在 home 或仓库下）。
    print(f"新表：{Path(new_table.source).name}（{len(new_order)} 行）")
    print()

    # ---- 标的集合变化
    added = [c for c in new_order if c not in base_map]
    removed = [c for c in base_order if c not in new_map]
    if added or removed:
        print("## 标的集合变化")
        for c in added:
            print(f"  + 新增 {c}（基准表中无此标的，本周无可比基准）")
        for c in removed:
            print(f"  - 移除 {c}（基准表中有，新表中缺失）")
        print()

    common = [c for c in new_order if c in base_map]

    # ---- 评级变动
    rating_changes: list[str] = []
    for code in common:
        old_cells, new_cells = base_map[code], new_map[code]
        # 结构校验已保证两侧都有完整列数，直接取值；不再「取不到就静默跳过」
        if old_cells[rating_idx] != new_cells[rating_idx]:
            rating_changes.append(f"  {code} {old_cells[rating_idx]}→{new_cells[rating_idx]}")

    print("## 评级变动")
    if rating_changes:
        for line in rating_changes:
            print(line)
    else:
        print("  本周评级无变动")
    print()

    # ---- 其他字段漂移
    drift_blocks: list[str] = []
    for code in common:
        old_cells, new_cells = base_map[code], new_map[code]
        changed: list[str] = []
        for ci, col in enumerate(columns):
            if ci == code_idx or ci == rating_idx:
                continue
            old_v, new_v = old_cells[ci], new_cells[ci]
            if old_v == new_v:
                continue
            if col in long_cols:
                o_brief, n_brief = _brief_change(old_v, new_v)
                changed.append(f"    {col}：已变更\n      旧 {o_brief}\n      新 {n_brief}")
            else:
                changed.append(f"    {col}：{old_v} → {new_v}")
        if changed:
            drift_blocks.append(f"  {code}\n" + "\n".join(changed))

    print("## 其他字段漂移")
    if drift_blocks:
        for block in drift_blocks:
            print(block)
    else:
        print("  无")

    if not rating_changes and not drift_blocks and not added and not removed:
        print()
        print("新表与基准表逐行完全一致。")
    return 0


# ---------------------------------------------------------------- show / meta


def cmd_show(args: argparse.Namespace) -> int:
    load_universe()  # 清单坏了就在第一步炸掉，别让 agent 走到第三步才发现（见 load_universe 注释）
    if not BASELINE_PATH.exists():
        err(f"错误：基准表不存在：{rel_path(BASELINE_PATH)}")
        return 1
    for line in extract_table_lines(read_text(BASELINE_PATH)):
        print(line)
    return 0


def cmd_meta(args: argparse.Namespace) -> int:
    load_universe()  # 同 show：清单不合法时立即报错，错误信息直接点名 universe.json
    print(json.dumps(baseline_meta(), ensure_ascii=False, indent=2))
    return 0


# ---------------------------------------------------------------- write


def build_document(preamble: list[str], table_lines: list[str], date: str, count: int) -> str:
    """刷新三项元数据后，把 preamble 与表格拼成完整文件内容。"""
    updated = datetime.now().astimezone().isoformat(timespec="seconds")
    values = {"数据日期": date, "更新时间": updated, "标的数": str(count)}

    lines = list(preamble)
    seen: set[str] = set()
    for idx, ln in enumerate(lines):
        m = META_LINE_RE.match(ln.strip())
        if m and m.group(1) not in seen:
            key = m.group(1)
            lines[idx] = f"{key}: {values[key]}"
            seen.add(key)

    missing = [k for k in META_KEYS if k not in seen]
    if missing:
        block = [f"{k}: {values[k]}" for k in missing]
        title_idx = next((i for i, ln in enumerate(lines) if ln.startswith("# ")), None)
        if title_idx is None:
            insert_at = len(lines)
            lines.extend([""] + block)
        else:
            insert_at = title_idx + 1
            lines[insert_at:insert_at] = [""] + block

    while lines and lines[-1].strip() == "":
        lines.pop()

    return "\n".join(lines + [""] + table_lines) + "\n"


def atomic_write(path: Path, content: str) -> None:
    """临时文件 + os.replace，保证要么完整替换、要么原文件不动。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def cmd_write(args: argparse.Namespace) -> int:
    date = args.date.strip()
    if not DATE_RE.match(date):
        err(f"错误：--date 必须是 YYYY-MM-DD 格式，实际「{args.date}」。基准表未被修改。")
        return 1
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        err(f"错误：--date「{date}」不是合法日期。基准表未被修改。")
        return 1

    universe = load_universe()
    table = load_table(Path(args.new_table).expanduser())

    errors = validate_table(table, universe)
    if errors:
        err(f"校验未通过，拒绝写入：{table.source}")
        err(f"共 {len(errors)} 条问题：")
        for i, e in enumerate(errors, start=1):
            err(f"  {i}. {e}")
        err("")
        err(f"基准表 {rel_path(BASELINE_PATH)} 未被修改（一个字节都没动）。")
        return 1

    old_meta = baseline_meta()
    if BASELINE_PATH.exists():
        preamble, _ = parse_preamble(read_text(BASELINE_PATH))
    else:
        preamble = DEFAULT_PREAMBLE.splitlines()

    # 写入前一律归一化成紧凑格式，不沿用 agent 当周给的排版。
    # 为什么：write 若原样写入，格式就随每周 agent 心情漂移。一旦某周产出
    # 对齐填充的表，下一周换成紧凑表就会让 46 行全部重排 —— 1 行真实数据变动
    # 产生 96 行 git diff，而本设计的回滚与审计完全依赖「git diff 看得出改了什么」。
    # 紧凑格式让 diff 行数 == 真实变动行数。
    table_lines = [render_row(table.header)]
    table_lines.append("| " + " | ".join([":---"] * len(table.header)) + " |")
    table_lines.extend(render_row(cells) for _, cells in table.rows)

    content = build_document(preamble, table_lines, date, len(table.rows))
    atomic_write(BASELINE_PATH, content)

    new_meta = baseline_meta()
    print(
        f"已写入 {rel_path(BASELINE_PATH)}：{len(table.rows)} 行数据；"
        f"数据日期 {old_meta['date'] or '（无）'} → {new_meta['date']}；"
        f"更新时间 {new_meta['updated_at']}"
    )
    caliber = next(
        (ln for ln in preamble if ln.strip().startswith("口径")), None
    )
    if caliber:
        print(f"注意：口径说明行按原样保留，如已过期请手动更新 assets/baseline.md 的这一行：\n  {caliber.strip()}")
    return 0


# ---------------------------------------------------------------- CLI


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="baseline.py",
        description="滚动基准表（assets/baseline.md）的读取、校验、对比与覆写工具。"
                    "写入前必定完整校验，任一条不过就拒写且不碰基准表。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="标的数量与行序一律以 assets/universe.json 为准；"
               "<new_table.md> 采用宽松提取，只读以 `|` 开头的行。",
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="子命令")

    p_show = sub.add_parser("show", help="打印当前基准表（表头 + 分隔行 + 全部数据行）")
    p_show.set_defaults(func=cmd_show)

    p_meta = sub.add_parser("meta", help="打印 JSON: {date, updated_at, count, path}")
    p_meta.set_defaults(func=cmd_meta)

    p_val = sub.add_parser("validate", help="只校验不写；通过 exit 0，失败 exit 1 并逐条列错")
    p_val.add_argument("new_table", metavar="new_table.md", help="待校验的新表文件")
    p_val.set_defaults(func=cmd_validate)

    p_diff = sub.add_parser("diff", help="与当前基准表逐行对比，输出评级变动与其他字段漂移（不写文件）")
    p_diff.add_argument("new_table", metavar="new_table.md", help="待对比的新表文件")
    p_diff.set_defaults(func=cmd_diff)

    p_write = sub.add_parser("write", help="先 validate，通过才原子覆写 baseline.md")
    p_write.add_argument("new_table", metavar="new_table.md", help="要写入的新表文件")
    p_write.add_argument("--date", required=True, metavar="YYYY-MM-DD", help="本次数据日期")
    p_write.set_defaults(func=cmd_write)

    return parser


def main(argv: list[str] | None = None) -> int:
    _configure_streams()
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
