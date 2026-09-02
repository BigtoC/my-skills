#!/usr/bin/env python3
"""ai-pullback-daily 三个脚本共用的姊妹技能（ai-industry-weekly）定位与路径展示。

为什么要有这个模块
------------------
日更的三步（产业评级表 / 技术面 / 永续隐含跳空）分别由 industry_table.py、
technicals.py、perp_quotes.py 产出，但三者读的是**同一份**周更安装：
评级表来自 assets/baseline.md，标的清单来自 assets/universe.json。
早先三个脚本各写了一套定位逻辑，探针不同（baseline.md vs universe.json）、
候选顺序不同、环境变量语义也不同（静默回退 / 警告后回退 / 直接报错），
于是可能出现「第一步读 A 份安装、第二步读 B 份安装」且两边都 exit 0 的脑裂，
产出一份内部自相矛盾的日报。本模块把定位收敛成唯一实现。

统一规则
--------
1. 候选顺序（三个脚本完全一致）：
     环境变量 AI_INDUSTRY_WEEKLY_DIR
     → <本技能>/../ai-industry-weekly
     → ~/.claude/skills/ai-industry-weekly
     → ~/.config/claude/skills/ai-industry-weekly
2. 统一探针：目录名为 ai-industry-weekly 且其下有 assets/ 目录，
   即认定「这是一份周更技能安装」。**不**用某个具体文件当探针——
   否则「装了但缺 universe.json」会被误报成「没装」，还会让缺文件的那份被跳过、
   悄悄落到另一份安装上。
3. 调用方用 require= 声明自己需要哪个文件（assets/baseline.md 或
   assets/universe.json），命中安装却缺该文件时给出精确错误，而不是误导性的
   「找不到姊妹技能」。
4. 环境变量语义统一为**严格**：AI_INDUSTRY_WEEKLY_DIR 设了但不合格 →
   一律报错退出，绝不静默回退到默认目录。显式覆写却拿到别处的数据是最难查的故障。

对外打印的路径一律相对化 / 折叠家目录：这些脚本的输出会被贴进日更报告并推 Slack，
绝不能带出含本机用户名的绝对路径。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

WEEKLY_ENV = "AI_INDUSTRY_WEEKLY_DIR"
WEEKLY_DIRNAME = "ai-industry-weekly"

SKILL_ROOT = Path(__file__).resolve().parent.parent   # <...>/ai-pullback-daily
SKILLS_DIR = SKILL_ROOT.parent                        # 两个技能的共同父目录

# 调用方声明依赖时用的相对路径
NEED_BASELINE = "assets/baseline.md"
NEED_UNIVERSE = "assets/universe.json"

_INSTALL_HINT = (
    f"两个技能须装在同一层目录下（repo 的 skills/、~/.claude/skills/、"
    f"~/.config/claude/skills/ 均可），即与本技能同级出现 {WEEKLY_DIRNAME}/。\n"
    f"若装在别处，用环境变量指过去：export {WEEKLY_ENV}=/path/to/{WEEKLY_DIRNAME}"
)


class WeeklySkillError(Exception):
    """定位不到姊妹技能、或命中的安装缺必需文件。

    str(exc) 即可直接打给用户看的多行中文说明（已相对化，无绝对家目录路径）。
    """


def err(msg: str = "") -> None:
    print(msg, file=sys.stderr)


def tilde(path: Path | str) -> str:
    """把家目录折叠成 ~，用于展示用户自己传入的路径。"""
    text = str(path)
    home = str(Path.home())
    if home and (text == home or text.startswith(home + os.sep)):
        return "~" + text[len(home):]
    return text


def rel_display(path: Path | str) -> str:
    """把路径相对化后展示，三个脚本共用同一套口径。

    依次尝试：相对本技能根（scripts/technicals.py）、相对技能安装目录
    （ai-industry-weekly/assets/universe.json）、相对 repo 根
    （skills/ai-industry-weekly/...）；都不成立就折叠家目录；
    再不成立只保留末几段，并剔除与本机用户名同名的那一段。
    """
    p = Path(path)
    try:
        p = p.resolve()
    except OSError:
        pass
    for base in (SKILL_ROOT, SKILLS_DIR, SKILLS_DIR.parent):
        try:
            return str(p.relative_to(base))
        except ValueError:
            continue
    folded = tilde(p)
    if folded.startswith("~"):
        return folded
    username = Path.home().name
    parts = [seg for seg in p.parts[-3:] if seg not in (os.sep, "/", "\\", username)]
    return ".../" + "/".join(parts) if parts else p.name


def _expand(raw: str) -> Path:
    """展开 ~ 并保证 .name 可用（`x/..`、`.`、末尾斜杠之类）。"""
    p = Path(raw).expanduser()
    if p.name in ("", ".", ".."):
        try:
            p = p.resolve()
        except OSError:
            pass
    return p


def looks_like_weekly_skill(path: Path) -> bool:
    """统一探针：这是不是一份 ai-industry-weekly 安装。

    只看「是不是这份技能」（目录名 + assets/ 存在），不看缺哪个文件——
    缺文件由 locate_weekly_skill(require=...) 单独报，两类故障必须分开。
    """
    return path.name == WEEKLY_DIRNAME and (path / "assets").is_dir()


def default_candidates() -> list[tuple[Path, str]]:
    """不含环境变量的默认候选，按优先级返回 (目录, 来源说明)，已去重。"""
    raw: list[tuple[Path, str]] = [(SKILLS_DIR / WEEKLY_DIRNAME, "与本技能同级目录")]
    for root in (
        Path.home() / ".claude" / "skills",
        Path.home() / ".config" / "claude" / "skills",
    ):
        raw.append((root / WEEKLY_DIRNAME, "常见安装位置"))
    seen: set[str] = set()
    out: list[tuple[Path, str]] = []
    for path, why in raw:
        key = str(path)
        if key not in seen:
            seen.add(key)
            out.append((path, why))
    return out


def _env_reject_message(raw: str, path: Path) -> str:
    if not path.exists():
        why = "该路径不存在"
    elif not path.is_dir():
        why = "该路径不是目录"
    elif path.name != WEEKLY_DIRNAME:
        why = f"目录名是「{path.name}」，不是「{WEEKLY_DIRNAME}」"
    else:
        why = "其下没有 assets/ 目录"
    return "\n".join([
        f"错误：环境变量 {WEEKLY_ENV} 指向的目录不是一份有效的 {WEEKLY_DIRNAME} 安装。",
        f"  {WEEKLY_ENV}={tilde(raw)}",
        f"  原因：{why}",
        "",
        f"{WEEKLY_ENV} 是显式覆写：设了却指不对就直接报错，不会静默回退到默认目录",
        "（否则「我明明指向了另一份安装」却拿到默认那份的数据，最难查；",
        "  更糟的是日报三步可能各读一份安装，产出自相矛盾的内容）。",
        f"请把它指向 {WEEKLY_DIRNAME} 技能根目录（其下应有 assets/），或取消该变量走默认查找。",
    ])


def _missing_file_message(weekly: Path, require: str, source: str) -> str:
    return "\n".join([
        f"错误：找到了周更技能 {WEEKLY_DIRNAME}，但它缺少 {require}。",
        f"  安装位置：{rel_display(weekly)}（来源：{source}）",
        "",
        f"这不是「没装」，而是「装了但这一份不完整」：请到 {WEEKLY_DIRNAME} 里跑一轮，",
        f"生成 {require} 后再跑日更。",
    ])


def _not_found_message(tried: list[tuple[Path, str]]) -> str:
    lines = [
        f"错误：找不到姊妹技能「{WEEKLY_DIRNAME}」。",
        "",
        "日更技能（ai-pullback-daily）的产业评级表与标的清单都不自己维护，",
        f"硬依赖周更技能「{WEEKLY_DIRNAME}」滚动维护的 assets/。请先安装周更技能：",
        "",
        "    npx skills add BigtoC/my-skills/ai-industry-weekly",
        "",
        _INSTALL_HINT,
        "",
        "已按顺序尝试过的位置：",
    ]
    for path, why in tried:
        if not path.exists():
            mark = "不存在"
        elif not path.is_dir():
            mark = "不是目录"
        else:
            mark = "目录存在但其下没有 assets/"
        lines.append(f"  · {tilde(path)}  （{why}；{mark}）")
    return "\n".join(lines)


def locate_weekly_skill(require: str | None = None) -> Path:
    """定位姊妹技能 ai-industry-weekly 的根目录。

    require: 调用方需要的相对路径，如 NEED_BASELINE / NEED_UNIVERSE。
             命中安装却缺这个文件时报「装了但缺 X」，而不是「找不到姊妹技能」。
    返回已 resolve 的绝对 Path；任何失败都抛 WeeklySkillError（消息可直接打给用户）。
    """
    env_raw = os.environ.get(WEEKLY_ENV, "").strip()
    if env_raw:
        env_path = _expand(env_raw)
        if not looks_like_weekly_skill(env_path):
            raise WeeklySkillError(_env_reject_message(env_raw, env_path))
        if require and not (env_path / require).is_file():
            raise WeeklySkillError(
                _missing_file_message(env_path, require, f"环境变量 {WEEKLY_ENV}")
            )
        return env_path.resolve()

    candidates = default_candidates()
    for path, why in candidates:
        if not looks_like_weekly_skill(path):
            continue
        # 第一个命中的安装即最终答案：绝不因为它缺某个文件就顺延到下一个候选，
        # 否则同一次日更的三步会落到不同安装上（脑裂）。
        if require and not (path / require).is_file():
            raise WeeklySkillError(_missing_file_message(path, require, why))
        return path.resolve()

    raise WeeklySkillError(_not_found_message(candidates))


def locate_weekly_skill_or_exit(require: str | None = None) -> Path:
    """locate_weekly_skill 的 sys.exit(1) 版本，给不做异常处理的脚本用。"""
    try:
        return locate_weekly_skill(require=require)
    except WeeklySkillError as exc:
        err(str(exc))
        sys.exit(1)
