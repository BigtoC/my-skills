#!/usr/bin/env python3
"""ai-pullback-daily 的跨运行状态档 —— 引爆点/信用层的「上次」到底是哪一次。

## 为什么需要这个档

`references/output-format.md` 的强制完整推送闸门写的是「任一引爆点 🔴，**或较上次
新增🟡**」，`references/tripwires.md` 的 ⚪ 档写的是「沿用上次状态并注明」。
在本档存在之前，这两个「上次」**没有任何机读来源**：技能目录下没有状态档，
SKILL.md 里也没有任何读回步骤，全靠写报告的人记得住或去翻 Slack 历史。

一天只跑一次时这还能勉强撑住。改成**一天两跑**（盘中一次、盘后一次）之后它会坏，
而且坏在最贵的地方：

  · 早上 10:08 盘中那轮，引爆点 ② 由 🟢 转 🟡 → 触发「较上次新增🟡」→ 完整推送带 🚨
  · 傍晚 17:30 盘后那轮（**这一份才是权威**），「上次」变成了今天早上，② 在早上
    已经是 🟡 了 → **不算新增** → 权威那一份**不带告警横幅**

于是当天唯一会被人当真的那份报告，反而是唯一没有警示的那份。

## 基准的定义：**日期严格早于今天的最近一笔 postclose**

不是「上一次 postclose」，而是「**上一个日历日**的 postclose」。差别在三种情况下
是决定性的，三种都真实会发生：

  · **同日重跑**。SKILL.md 把 gate 与 write 放在同一个可复制的 bash 区块里，且
    write 在 Slack 推送**之前**。推送失败后重跑整块 → 第二次 gate 会读到第一次
    write 刚写下的那笔，于是「② 在基准里已经是 🟡」→ 不算新增 → 重试推出去的
    权威报告**没有横幅**。本档存在的理由被一次重试打穿。
  · **周末与美股休市日**（每年约 110 天）。那些日子 `market_session()` 两轮都回
    `closed` → RUN_MODE 两轮都是 postclose → 第二轮的基准变成第一轮，同一个 bug。
  · **乱序或写错日期的 write**。旧实现会让它直接覆盖掉更新的基准。

所以本档存 `postclose_history`（近 N 笔，新的在前），gate 取其中 `date < today`
的第一笔。同一天跑几次，取到的基准永远是同一笔。

## ⚪ 的「沿用」必须参与闸门判定

`tripwires.md` 的 ⚪ 是「本次无可靠新证据，**沿用上次状态**」——沿用之后，报告正文
印出来的就是那个被沿用的档。如果闸门只看本次传入的原始读数（⚪），就会出现：

  早上盘中查到 ② 🟡 → 傍晚盘后没查到新证据（⚪）→ 报告正文照样印 ② 🟡（沿用）
  → 但闸门看到的是 ⚪、跳过 → **没有横幅**，而且明天的基准里 ② 已经是 🟡
  → 这个 🟢→🟡 的转折**永远不会被任何一份权威报告宣告**。

所以 gate 先把 ⚪ 解析成「实际生效的状态」（沿用 `last_any` 的值），再拿解析后的
状态与基准比。报告印什么，闸门就判什么。

## 「沿用上次状态」的单位是天，不是「次」

每项除 `value` 外还记 `last_evidence_date`：真有新证据时更新，⚪ 搬运时原样保留。
一天两跑会让「沿用」的**次数**翻倍，天数不会——天数才是读者要的那个量。

## 落盘时机与 git

**必须在 Slack 推送之前写**（推送失败不该连带丢掉当日判出的状态）。本档**进 git**：
不跟踪的话换一台机器跑就永远是「首次运行」，闸门等于恒不触发。
"""

import argparse
import json
import os
import re
import sys
import tempfile
import traceback
from datetime import date, datetime
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve()
SKILL_ROOT = SCRIPT_PATH.parent.parent
ASSETS_DIR = SKILL_ROOT / "assets"
STATE_PATH = ASSETS_DIR / "last_run.json"

STATES = ("🟢", "🟡", "🔴", "⚪")
RANK = {"🟢": 0, "🟡": 1, "🔴": 2}          # ⚪ 不排序：它不是一个档，是「不知道」
TRIPWIRES = ("1", "2", "3", "4", "5")
CREDIT_KEYS = ("L1", "L2", "L3", "L4", "T4")
MODES = ("intraday", "postclose")
HISTORY_CAP = 30                            # 约一个月的 postclose，足够任何回看

_HOMEISH_RE = re.compile(r"(?:/Users|/home|/var/folders)/[^/\s\"']+")
_VS16 = "️"                            # 变体选择符：⚪️ 与 ⚪ 肉眼完全相同


def scrub(text) -> str:
    """输出会被贴进日报正文并推 Slack，绝不能带出家目录绝对路径。"""
    s = str(text)
    try:
        home = str(Path.home())
    except Exception:
        home = ""
    if home:
        s = s.replace(home, "~")
    return _HOMEISH_RE.sub("~", s)


def rel_display(path) -> str:
    p = Path(path)
    try:
        return str(p.resolve().relative_to(SKILL_ROOT))
    except (ValueError, OSError):
        return scrub(p)


def err(msg):
    print(msg, file=sys.stderr)


def norm_state(v):
    """去掉变体选择符：`⚪️`(U+26AA+FE0F) 与 `⚪`(U+26AA) 渲染完全一样。

    不规范化的话，错误讯息会是「值 '⚪️' 不在 ['🟢','🟡','🔴','⚪']」——而那两个
    字符在终端里长得一模一样，报错等于没报。
    """
    return (v or "").replace(_VS16, "").strip()


def parse_pairs(s, allowed, what):
    """'1=🟢,2=🟡' -> {'1':'🟢',...}。未列出的键**不补缺省**（缺 ≠ 🟢）。"""
    out = {}
    for chunk in (s or "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" not in chunk:
            raise ValueError(f"{what} 片段 {chunk!r} 不是 KEY=STATE 形式")
        k, v = chunk.split("=", 1)
        k, v = k.strip(), norm_state(v)
        if k not in allowed:
            raise ValueError(f"{what} 键 {k!r} 不在 {list(allowed)}")
        if v not in STATES:
            raise ValueError(f"{what} 的 {k} 值 {v!r} 不在 {list(STATES)}（注意 ⚪ 勿带变体选择符）")
        out[k] = v
    return out


def load_credit_json(path):
    """从 `neocloud_credit_monitor.py --json-also` 的产物取**权威**状态。

    `references/output-format.md:93` 规则② 写得很死：引爆点④ 的状态**只由脚本产出**，
    日报不另行人工判读、**不得与脚本结论冲突**。所以 ④ 与 L1–L4/T4 本来就不是手打项，
    它们只有一个来源。手打一遍等于给同一个事实造第二个出处——这个仓库把「两处持有
    同一事实而彼此不一致」列为本技能最贵的失败（见 CLAUDE.md 的两份 TH 字典）。

    回传 (states, err)：states = {"tripwires": {...}, "credit": {...}}。
    取不到（脚本这轮失败、档案缺失、JSON 坏掉）不是错误——SKILL.md 规定此时该节写
    「本次未取到信用层数据，引爆点④ 沿用上次状态并标⚪」，所以回 ⚪ 让沿用机制接手，
    **绝不**让它变成阻断推送的硬错误。
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        return None, (f"信用层 JSON 读取失败（{rel_display(path)}）：{type(e).__name__}；"
                      f"引爆点④ 与 L1–L4/T4 按 ⚪ 沿用上次状态处理（不手打顶替）")
    d = d or {}
    # 产物自己的自检必须先看。**判别式只能是 `is False`**：完整版 monitor 的 JSON
    # 顶层**没有** ok 这个键（实测顶层为 meta/fred/equities/bonds/eval/errors/
    # data_gaps/degraded/degraded_reasons），而 lite 变体有。写成 `not d.get("ok")`
    # 会把完整版的「没有这个键」当成「自检没过」，天天误判。
    if d.get("ok") is False:
        return None, (f"信用层 JSON 自检未过（ok=false，{rel_display(path)}）；"
                      f"引爆点④ 与 L1–L4/T4 按 ⚪ 沿用上次状态处理，不采信其读数")
    ev = d.get("eval") or {}
    t4 = ((ev.get("tripwire_4") or {}).get("state"))
    if not t4:
        return None, (f"信用层 JSON 里没有 eval.tripwire_4.state（{rel_display(path)}）；"
                      f"引爆点④ 与 L1–L4/T4 按 ⚪ 沿用上次状态处理")
    meta = d.get("meta") or {}
    out = {"tripwires": {}, "credit": {},
           # 来源标注（回退链规则第 2 条）：④ 到底来自哪一次取数，必须可复核。
           # 没有它，一份隔轮残留的 /tmp/credit.json 会毫无痕迹地冒充今天的④。
           # 只记档名，不记路径。本字段会进 --json、可能被贴进报告并推 Slack，而
           # scrub() 的 _HOMEISH_RE 只折叠字面的 /Users|/home|/var/folders——像
           # `/private/tmp/claude-503/-Users-<用户名>-Documents-...` 这种把用户名
           # 编进目录名的路径它抓不到（实测）。档名 + generated_at 已足够复核「这份
           # ④ 来自哪一次取数」，路径本身没有额外价值。
           "provenance": {"file": Path(path).name,
                          "generated_at": meta.get("generated_at"),
                          "data_date": meta.get("data_date"),
                          "degraded": d.get("degraded"),
                          "ok": d.get("ok", "（完整版无此键）")}}
    v = norm_state(t4)
    if v in STATES:
        out["tripwires"]["4"] = v
        out["credit"]["T4"] = v
    for lay in ("L1", "L2", "L3", "L4"):
        s = norm_state((ev.get(lay) or {}).get("state") or "")
        if s in STATES:
            out["credit"][lay] = s
    return out, None


def merge_authoritative(tw, cr, auth):
    """把信用层 JSON 的权威值并进手打值。**脚本赢**，冲突逐条记录。

    不阻断：一次打字冲突不该拦下当天那份权威报告。但也绝不静默——冲突进
    `credit_json_conflicts` 字段、进 degraded_reasons、进 stderr，三个频道都有。
    """
    conflicts = []
    if not auth:
        return tw, cr, conflicts
    for grp, incoming, authblk in (("引爆点", tw, auth.get("tripwires") or {}),
                                   ("信用层", cr, auth.get("credit") or {})):
        for k, v in authblk.items():
            typed = incoming.get(k)
            if typed is not None and typed != v:
                conflicts.append({"group": grp, "key": k, "typed": typed, "script": v,
                                  "resolved_to": v})
            incoming[k] = v
    return tw, cr, conflicts


def load_state():
    """读状态档。缺档 = 首次运行（不是错误）；读坏了要**响亮**且**不得当成首次**。

    把「读失败」读成「首次运行」，等于把「不知道上次是什么」记成「上次什么都没有」，
    闸门会安静地永不触发。两者回不同的东西，调用方必须分别处理。
    """
    if not STATE_PATH.exists():
        # 旁边留着上一轮搬走的坏档 = 上一次已经停过线，人还没处理。
        # 这时「档案不存在」**不是**首次运行：若当成首次，下一轮就会写出一份
        # 没有 postclose_history 的新档、exit 0、ok:true，于是那次拒绝只挡了一轮，
        # 而被搬走的历史从此无人问津。
        orphans = sorted(ASSETS_DIR.glob("last_run.corrupt-*.json"))
        if orphans:
            return None, (f"状态档缺失，但旁边有未处理的损坏备份 "
                          f"{rel_display(orphans[-1])}（共 {len(orphans)} 份）："
                          f"上一轮已因读取失败停线。请人工确认并移除/合并后再跑，"
                          f"否则会以空基准重建、丢掉整个 postclose 基准序列")
        return {}, None
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        return None, f"状态档读取失败（{rel_display(STATE_PATH)}）：{type(e).__name__}"
    if not isinstance(data, dict):
        return None, (f"状态档内容不是 JSON 物件（{rel_display(STATE_PATH)}）："
                      f"实际为 {type(data).__name__}")
    return data, None


def write_state(state):
    """临时档 + os.replace：半写的状态档会让明天的闸门对着残缺基准比。"""
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(ASSETS_DIR), prefix=".last_run.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, STATE_PATH)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def merge_evidence(prev_block, new_vals, run_date):
    """合并本次读数与上次纪录，维护每项的 last_evidence_date。

    ⚪ = 本次没有可靠新证据 → **原样搬运**上次的 value 与 last_evidence_date
    （search-contract.md:48-49「搬运 ≠ 判定」）。其余档 = 本次有实据 → 更新两者。
    **完全没传的键**与 ⚪ 不同：⚪ 是「查了、没查到」，没传是「根本没提交这一项」，
    记 `not_supplied` 以免 show/闸门把它当成一次新鲜读数。
    """
    prev = prev_block or {}
    out = {}
    for k, v in new_vals.items():
        old = prev.get(k) or {}
        if v == "⚪":
            out[k] = {
                "value": old.get("value"),
                "last_evidence_date": old.get("last_evidence_date"),
                "carried_forward": True,
                "not_supplied": False,
                "never_evidenced": old.get("value") is None,
            }
        else:
            out[k] = {"value": v, "last_evidence_date": run_date,
                      "carried_forward": False, "not_supplied": False,
                      "never_evidenced": False}
    for k, old in prev.items():
        if k not in out:
            o = dict(old)
            o["carried_forward"] = True
            o["not_supplied"] = True
            out[k] = o
    return out


def values_of(block):
    return {k: (v or {}).get("value") for k, v in (block or {}).items()}


def pick_baseline(state, today):
    """基准 = `postclose_history` 里**日期严格早于 today** 的最近一笔。

    「严格早于」是这个函数的全部意义，见模组 docstring 的三种情况。
    """
    for e in (state.get("postclose_history") or []):
        d = e.get("date")
        if not d:
            continue
        try:
            if date.fromisoformat(d) < today:
                return e
        except ValueError:
            continue
    return None


def resolve_current(raw_vals, carry_block):
    """把本次读数里的 ⚪ 解析成**实际生效**的状态（沿用 last_any 的值）。

    报告正文印的是沿用后的档，闸门必须看同一个东西，否则一个由盘中发现、盘后
    没能复核的 🟡，会既印在报告里又躲过闸门，然后悄悄变成明天的基准。
    """
    carry = values_of(carry_block)
    out, carried, unresolved = {}, [], []
    for k, v in raw_vals.items():
        if v == "⚪":
            c = carry.get(k)
            if c:
                out[k] = c
                carried.append(k)
            else:
                # ⚪ 且没有可沿用的值 = 「从来不知道」。早先这里没有 else，键就**凭空消失**，
                # 于是它既不在 effective_* 里、也不在 not_supplied_* 里（因为调用方确实传了），
                # 闸门于是对一个从未被观察过的项回报「比对过、没触发」。
                unresolved.append(k)
        else:
            out[k] = v
    return out, sorted(carried), sorted(unresolved)


def newly_amber(baseline_block, current):
    """相对基准「新增🟡」：基准是 🟢 或无纪录，本次生效状态是 🟡。"""
    base = values_of(baseline_block)
    out = []
    for k, v in sorted(current.items()):
        if v != "🟡":
            continue
        b = base.get(k)
        if b is None or RANK.get(b, -1) < RANK["🟡"]:
            out.append(k)
    return out


def escalated(baseline_block, current):
    base = values_of(baseline_block)
    out = []
    for k, v in sorted(current.items()):
        if v not in RANK:
            continue
        b = base.get(k)
        if b in RANK and RANK[v] > RANK[b]:
            out.append({"key": k, "from": b, "to": v})
        elif b is None and RANK[v] > 0:
            out.append({"key": k, "from": None, "to": v})
    return out


def compute_gate(state, tw_raw, cr_raw, today, read_err,
                 credit_notes=None, credit_info=None):
    """闸门判定。gate 与 run 共用这一份，避免两条路各判各的。"""
    baseline = pick_baseline(state or {}, today) or {}
    base_tw, base_cr = baseline.get("tripwires"), baseline.get("credit")
    carry = (state or {}).get("last_any") or {}

    tw, tw_carried, tw_unres = resolve_current(tw_raw, carry.get("tripwires"))
    cr, cr_carried, cr_unres = resolve_current(cr_raw, carry.get("credit"))

    missing_tw = sorted(set(TRIPWIRES) - set(tw_raw))
    missing_cr = sorted(set(CREDIT_KEYS) - set(cr_raw))

    red = sorted(k for k, v in tw.items() if v == "🔴")
    # 「新增」必须各自看各自的基准块：信用层用 base_cr，引爆点用 base_tw。
    # 早先版本把信用层也挂在引爆点的 has_base 上，于是「信用层没有基准」时
    # credit_escalated 记 null 而 credit_l2_l4_escalated 记 []，同一份 JSON
    # 一边说「判不了」一边说「判过了、没有」——强制推送条件 ④⑤ 就此静默消失。
    amber_new = newly_amber(base_tw, tw) if base_tw else None
    esc = escalated(base_tw, tw) if base_tw else None
    cr_esc = escalated(base_cr, cr) if base_cr else None
    # 强制推送条件 ④⑤ 问的是「L2/L4 **今天**有没有跨档」。沿用值答不了这个问题：
    # 「昨天是🟢、今天没查到」不等于「今天是🟢」，前者排除不掉一次跨档。
    # 所以 L2/L4 只要不是当轮实测到的，这一条就是 null（无从判定），不是 []（查过、没有）。
    l2l4_observed = all(k not in cr_carried and k not in cr_unres and k in cr
                        for k in ("L2", "L4"))
    l2l4 = ([e for e in cr_esc if e["key"] in ("L2", "L4") and e["to"] in ("🟡", "🔴")]
            if (cr_esc is not None and l2l4_observed) else None)

    reasons = []
    if red:
        reasons.append(f"引爆点 🔴：{'、'.join(red)}")
    if amber_new:
        reasons.append(f"较上次盘后新增🟡：{'、'.join(amber_new)}")
    for e in (l2l4 or []):
        reasons.append(f"信用层 {e['key']} 由 {e['from'] or '无纪录'} 转 {e['to']}")

    degraded = []
    if read_err:
        degraded.append("⚠ " + read_err + "；本次无法与上次盘后比较，闸门按「无基准」处理")
    if not baseline:
        degraded.append("无更早日期的 postclose 纪录（首次运行，或状态档尚未建立）："
                        "「较上次新增🟡」无从判定（null，非「没有新增」）")
    if base_tw and not base_cr:
        degraded.append("基准里没有信用层纪录：强制推送条件 ④⑤（L2/L4 跨档）无从判定（null）")
    if missing_tw:
        degraded.append(f"⚠ 未提交的引爆点：{'、'.join(missing_tw)}——"
                        f"这些项**未参与**闸门判定（不是「没触发」）")
    if missing_cr:
        degraded.append(f"⚠ 未提交的信用层键：{'、'.join(missing_cr)}——同样未参与判定")
    if tw_carried or cr_carried:
        degraded.append(f"⚪ 沿用上次状态后参与判定：引爆点 {tw_carried or '无'}、"
                        f"信用层 {cr_carried or '无'}（报告印什么，闸门就判什么）")
    if tw_unres or cr_unres:
        degraded.append(f"⚠ ⚪ 且无可沿用值、**从未被观察过**：引爆点 {tw_unres or '无'}、"
                        f"信用层 {cr_unres or '无'}——这些项未参与判定，"
                        f"**不等于「没触发」**")
    if not l2l4_observed:
        degraded.append("⚠ L2/L4 本轮非实测（沿用或缺失）→ 强制推送条件 ④⑤「L2/L4 跨档」"
                        "**无从判定**（null，非「没跨档」）")
    for n in (credit_notes or []):
        degraded.append(n)
    never = sorted(k for k, v in (base_tw or {}).items() if (v or {}).get("never_evidenced"))
    if never:
        degraded.append(f"基准里从未有过实据的引爆点：{'、'.join(never)}")

    # 三态：true=判定为是；null=该腿无从判定；false=每条腿都判过、都不成立
    #
    # **没提交的键也算「无从判定」**。SKILL.md 叫调用方照 force_full_push /
    # recommended_full_push 行动，只把漏提交写进 degraded_reasons 是不够的：
    # 一个忘了打的 🔴 会让闸门回「没有强制推送理由」而 ok:true、exit 0，
    # 矛盾只藏在另一个字段里——正是 ⚪/❌ 那条规则要防的形状。
    undetermined = ((amber_new is None) or (l2l4 is None)
                    or bool(missing_tw) or bool(missing_cr)
                    or bool(tw_unres) or bool(cr_unres))
    if reasons:
        force = True
    elif undetermined:
        force = None
    else:
        force = False

    return {
        "ok": not read_err,
        # null 表示「新增🟡/信用跨档这两条腿至少有一条无从判定」，不是「不强制推送」
        "force_full_push": force,
        "reasons": reasons,
        "gate_undetermined": bool(undetermined),
        "gate_undetermined_reason": (
            ((("缺基准：「较上次新增🟡」或信用层跨档无从判定。"
               if (amber_new is None or l2l4 is None) else "")
              + (f"未提交的键未参与判定：引爆点 {missing_tw or '无'}、信用层 {missing_cr or '无'}。"
                 if (missing_tw or missing_cr) else ""))
             + "建议保守按完整推送处理，并在报告中注明本次闸门不完整") if undetermined else None),
        # 调用方实际照做的那个布林：未判定时保守取 True
        "recommended_full_push": bool(reasons) or bool(undetermined),
        "red": red,
        "newly_amber_vs_last_postclose": amber_new,
        "escalated_vs_last_postclose": esc,
        "credit_escalated": cr_esc,
        "credit_l2_l4_escalated": l2l4,
        "effective_tripwires": tw,
        "effective_credit": cr,
        "carried_forward_tripwires": tw_carried,
        "carried_forward_credit": cr_carried,
        "not_supplied_tripwires": missing_tw,
        "not_supplied_credit": missing_cr,
        # ⚪ 且无可沿用值：调用方**有**提交（所以不在 not_supplied_*），但它从未被观察过，
        # 也就不在 effective_* 里。没有这两个字段，这类键会在两边都查不到。
        "unresolved_tripwires": tw_unres,
        "unresolved_credit": cr_unres,
        "l2_l4_observed_this_run": l2l4_observed,
        # 引爆点④/L1–L4/T4 若由 --credit-json 提供，这里记它与手打值的冲突。
        # 空清单 = 比对过、一致；null = 没给 --credit-json，根本没比对。
        # 三态，缺一不可区分：
        #   not_provided = 没给 --credit-json，④ 仍是手打值
        #   ok           = 读到了，conflicts 为清单（[] = 比对过、一致）
        #   unusable     = 给了但档案缺失/坏掉/自检未过 → ④ 已改走 ⚪ 沿用
        # 早先 conflicts=None 同时表示第一与第三种，SKILL.md 却只定义了第一种。
        "credit_json_status": (credit_info or {}).get("status", "not_provided"),
        "credit_json_conflicts": (credit_info or {}).get("conflicts"),
        "credit_json_provenance": (credit_info or {}).get("provenance"),
        "baseline": {
            "source": "postclose_history（date < today 的最近一笔）",
            "date": baseline.get("date"),
            "mode": baseline.get("mode"),
            "generated_at": baseline.get("generated_at"),
            "available": bool(baseline),
        },
        "state_path": rel_display(STATE_PATH),
        "degraded_reasons": degraded,
        "degraded": bool(degraded),
        "prohibitions": [
            "引爆点④ 与 L1–L4/T4 只由 neocloud_credit_monitor.py 产出，手打值不得与之冲突"
            "（output-format.md 规则②）；冲突时一律以脚本为准",
            "基准只能是**日期早于今天**的 postclose 运行；同日重跑不得把基准换成自己",
            "⚪ 先解析成沿用后的生效状态再判，不得直接跳过",
            "无基准时 newly_amber / credit_l2_l4_escalated 是 null，不得读成空清单「没有新增」",
            "未提交的键不参与判定，也不得被读成「没触发」",
        ],
    }


def print_gate(res):
    b = res["baseline"]
    print(f"比较基准：{'上次盘后 ' + str(b['date']) if b['available'] else '（无——首次运行）'}")
    f = res["force_full_push"]
    print("强制完整推送：" + ("是" if f is True else
                             ("否" if f is False else
                              "**未判定**（缺基准）——建议保守走完整推送，并在报告注明")))
    for r in res["reasons"]:
        print(f"  · {r}")
    for r in res["degraded_reasons"]:
        print(f"  {r if r.startswith('⚠') or r.startswith('⚪') else '· ' + r}")


def cmd_gate(args):
    state, read_err = load_state()
    if read_err:
        err("⚠ " + read_err)
    today = parse_date(args.date)
    tw, cr, notes, cinfo = resolve_inputs(args)
    for n in notes:
        err(n)
    res = compute_gate(state or {}, tw, cr, today, read_err,
                       credit_notes=notes, credit_info=cinfo)
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        print_gate(res)
    return 0 if res["ok"] else 3


def resolve_inputs(args):
    """手打值 + （可选）信用层 JSON 的权威值 → (tw, cr, notes, conflicts)。

    conflicts 为 None 表示「没给 --credit-json，没比对过」；[] 表示「比对过、一致」。
    这两者不是同一件事，字段上必须分得开。
    """
    tw = parse_pairs(args.tripwires, TRIPWIRES, "引爆点")
    cr = parse_pairs(args.credit, CREDIT_KEYS, "信用层")
    notes, conflicts, prov = [], None, None
    status = "not_provided"
    path = getattr(args, "credit_json", None)
    if path:
        auth, cerr = load_credit_json(path)
        if cerr:
            status = "unusable"
            notes.append("⚠ " + cerr)
            # 脚本没给 → 这几项记 ⚪，交给沿用机制；**不**保留手打值顶替
            for k in ("4",):
                tw[k] = "⚪"
            for k in CREDIT_KEYS:
                cr[k] = "⚪"
            conflicts = None
        else:
            status = "ok"
            prov = auth.get("provenance")
            tw, cr, conflicts = merge_authoritative(tw, cr, auth)
            for c in conflicts:
                notes.append(f"⚠ {c['group']} {c['key']} 手打 {c['typed']} 与脚本 {c['script']} 冲突，"
                             f"已以脚本为准（output-format.md 规则②：④ 不得与脚本结论冲突）")
    return tw, cr, notes, {"conflicts": conflicts, "status": status, "provenance": prov}


def parse_date(s):
    if not s:
        return date.today()
    try:
        return date.fromisoformat(s)
    except ValueError:
        raise ValueError(f"--date {s!r} 不是 YYYY-MM-DD")


def do_write(state, mode, tw, cr, run_date, pushed):
    prev_any = (state.get("last_any") or {})
    entry = {
        "date": run_date.isoformat(),
        "mode": mode,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "tripwires": merge_evidence(prev_any.get("tripwires"), tw, run_date.isoformat()),
        "credit": merge_evidence(prev_any.get("credit"), cr, run_date.isoformat()),
        "pushed": bool(pushed),
    }
    state["last_any"] = entry
    if mode == "postclose":
        hist = [e for e in (state.get("postclose_history") or [])
                if e.get("date") != entry["date"]]          # 同日重跑覆盖自己那笔
        hist.insert(0, entry)
        hist.sort(key=lambda e: e.get("date") or "", reverse=True)
        state["postclose_history"] = hist[:HISTORY_CAP]
        state["last_postclose"] = entry                      # 仅供人看，闸门不读它
    state["_readme"] = (
        "ai-pullback-daily 跨运行状态档。闸门基准 = postclose_history 里**日期早于今天**的"
        "最近一笔（同日重跑、周末两跑、乱序写入都因此不会把基准换成自己）。"
        "last_postclose 只是最近一笔 postclose 的便捷视图，闸门不读它。"
        "每项 last_evidence_date 是该项最后一次有真实证据的日期，⚪ 搬运时原样保留。"
        "由 scripts/run_state.py 维护，Slack 推送**之前**落盘。")
    return entry


def cmd_write(args, _gate_res=None, _resolved=None):
    state, read_err = load_state()
    if read_err:
        # **绝不**以空基准重建：那会让一次暂时性的 PermissionError 抹掉整个
        # postclose_history。旧档先备份留证，然后拒绝写入并回非零。
        bak = STATE_PATH.with_suffix(f".corrupt-{datetime.now():%Y%m%dT%H%M%S}.json")
        try:
            if STATE_PATH.exists():
                os.replace(STATE_PATH, bak)
                err(f"⚠ {read_err}；已保留原档为 {rel_display(bak)} 以便人工检查")
        except OSError as e:
            err(f"⚠ {read_err}；且原档无法备份：{type(e).__name__}")
        err("⛔ 拒绝以空基准重建状态档——重建会抹掉 postclose_history，"
            "而闸门基准正是从它取的。请人工确认后再跑。")
        if args.json:
            print(json.dumps({"ok": False, "written": None, "reason": read_err,
                              "state_path": rel_display(STATE_PATH),
                              "degraded": True, "degraded_reasons": ["⚠ " + read_err]},
                             ensure_ascii=False, indent=2))
        return 3

    # run 已经解析过一次就直接复用：重解析会再读一次 credit.json、把冲突讯息印两遍，
    # 而且两次之间档案可能被换掉——判的与落盘的就不是同一组状态了。
    if _resolved is not None:
        tw, cr, wnotes, wcinfo = _resolved
    else:
        tw, cr, wnotes, wcinfo = resolve_inputs(args)
        for n in wnotes:
            err(n)
    run_date = parse_date(args.date)
    entry = do_write(state, args.mode, tw, cr, run_date, args.pushed)
    if _gate_res is not None:
        entry["gate"] = {k: _gate_res[k] for k in
                         ("force_full_push", "reasons", "gate_undetermined",
                          "recommended_full_push")}
        entry["gate"]["baseline_date"] = _gate_res["baseline"]["date"]
    try:
        write_state(state)
    except OSError as e:
        err(f"⚠ 状态档写入失败（{rel_display(STATE_PATH)}）：{type(e).__name__}；"
            f"本次判出的状态不会被明天读到，明天的闸门将缺基准")
        if args.json:
            print(json.dumps({"ok": False, "written": None,
                              "reason": f"写入失败：{type(e).__name__}",
                              "degraded": True}, ensure_ascii=False, indent=2))
        return 3
    res = {"ok": True, "written": rel_display(STATE_PATH), "mode": args.mode,
           "date": run_date.isoformat(),
           "updated_postclose_baseline": args.mode == "postclose",
           "tripwires": values_of(entry["tripwires"]),
           "credit": values_of(entry["credit"]),
           "not_supplied_tripwires": sorted(set(TRIPWIRES) - set(tw)),
           "not_supplied_credit": sorted(set(CREDIT_KEYS) - set(cr)),
           # 单独跑 write --json 时，冲突此前只进 stderr——而 stderr 是冗余频道，
           # 不能是唯一频道（JSON 等价规则第 4 条）。
           "credit_json_status": (wcinfo or {}).get("status", "not_provided"),
           "credit_json_conflicts": (wcinfo or {}).get("conflicts"),
           "credit_json_provenance": (wcinfo or {}).get("provenance"),
           "degraded_reasons": [], "degraded": False}
    if (wcinfo or {}).get("conflicts"):
        res["degraded_reasons"].append(
            f"⚠ 手打值与信用层脚本冲突 {len((wcinfo or {}).get('conflicts'))} 处，已以脚本为准")
        res["degraded"] = True
    if (wcinfo or {}).get("status") == "unusable":
        res["degraded_reasons"].append("⚠ 信用层 JSON 不可用 → ④ 与 L1–L4/T4 按 ⚪ 沿用处理")
        res["degraded"] = True
    if res["not_supplied_tripwires"] or res["not_supplied_credit"]:
        res["degraded_reasons"].append(
            f"⚠ 未提交：引爆点 {res['not_supplied_tripwires'] or '无'}、"
            f"信用层 {res['not_supplied_credit'] or '无'}（沿用旧值并标 not_supplied）")
        res["degraded"] = True
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        print(f"已写入 {res['written']}（{args.mode} · {res['date']}）"
              + ("；已加入 postclose 基准序列" if res["updated_postclose_baseline"]
                 else "；未动 postclose 基准序列（盘中）"))
        for r in res["degraded_reasons"]:
            print("  " + r)
    return 0


def cmd_run(args):
    """gate + write 一次做完 —— 状态字串只输入一次。

    分成两条命令时，SKILL.md 要求调用方把同一串 emoji **手打两遍**，两遍之间没有
    任何交叉检查；打错一个字元，闸门判的和落盘的就是两组不同的状态。
    而且 write 会推进基准序列，所以两条命令的先后与重跑次数都会影响结果。
    """
    state, read_err = load_state()
    if read_err:
        err("⚠ " + read_err)
    resolved = resolve_inputs(args)
    tw, cr, notes, cinfo = resolved
    for n in notes:
        err(n)
    today = parse_date(args.date)
    gate_res = compute_gate(state or {}, tw, cr, today, read_err,
                            credit_notes=notes, credit_info=cinfo)
    rc = cmd_write(args, _gate_res=gate_res, _resolved=resolved) if not args.gate_only else 0
    if args.json:
        if not args.gate_only:
            print()          # write 的 JSON 已印，分隔后再印 gate 的
        print(json.dumps(gate_res, ensure_ascii=False, indent=2))
    else:
        print_gate(gate_res)
    return rc if rc else (0 if gate_res["ok"] else 3)


def cmd_show(args):
    state, read_err = load_state()
    if read_err:
        err("⚠ " + read_err)
        if args.json:
            print(json.dumps({"ok": False, "error": read_err,
                              "state_path": rel_display(STATE_PATH)},
                             ensure_ascii=False, indent=2))
        return 3
    if args.json:
        print(json.dumps({"ok": True, "state_path": rel_display(STATE_PATH),
                          "exists": STATE_PATH.exists(),
                          "baseline_for_today": pick_baseline(state, date.today()),
                          **state}, ensure_ascii=False, indent=2))
        return 0
    if not state:
        print(f"{rel_display(STATE_PATH)} 不存在 —— 首次运行（不是错误）")
        return 0
    today = date.today()
    base = pick_baseline(state, today)
    print(f"今日闸门基准：{base['date'] + ' · postclose' if base else '（无——首次运行）'}")
    print(f"postclose 序列：{len(state.get('postclose_history') or [])} 笔")
    for name in ("last_any",):
        e = state.get(name)
        print(f"── {name}: " + (f"{e['date']} · {e['mode']}" if e else "（无）"))
        if not e:
            continue
        for grp in ("tripwires", "credit"):
            for k, v in (e.get(grp) or {}).items():
                val = v.get("value") or "（从未有实据）"
                ev = v.get("last_evidence_date")
                age = ""
                if ev:
                    try:
                        age = f"，已 {(today - date.fromisoformat(ev)).days} 天"
                    except ValueError:
                        age = ""
                tag = "（未提交）" if v.get("not_supplied") else (
                    "（沿用）" if v.get("carried_forward") else "")
                print(f"    {grp[:2]}.{k}: {val}{tag}  最后实据 {ev or 'N/A'}{age}")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="ai-pullback-daily 跨运行状态档：推送闸门的「上次」与 ⚪ 的沿用来源")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add_states(p):
        p.add_argument("--tripwires", default="", metavar="1=🟢,2=🟡,…")
        p.add_argument("--credit", default="", metavar="L1=🟡,L2=🟢,…,T4=🟡")
        p.add_argument("--date", default=None, metavar="YYYY-MM-DD")
        p.add_argument("--credit-json", default=None, metavar="FILE",
                       help="neocloud_credit_monitor.py --json-also 的产物。"
                            "引爆点④ 与 L1–L4/T4 从这里取（权威），手打值只作交叉检查；"
                            "档案缺失/坏掉 → 该几项按 ⚪ 沿用，不阻断推送")
        p.add_argument("--json", action="store_true")

    r = sub.add_parser("run", help="判闸门并落盘（**推荐**：状态只输入一次）")
    r.add_argument("--mode", choices=MODES, required=True)
    r.add_argument("--pushed", action="store_true")
    r.add_argument("--gate-only", action="store_true", help="只判不写")
    add_states(r)

    w = sub.add_parser("write", help="只落盘（**必须在 Slack 推送之前**）")
    w.add_argument("--mode", choices=MODES, required=True)
    w.add_argument("--pushed", action="store_true")
    add_states(w)

    g = sub.add_parser("gate", help="只判是否强制完整推送（不写档）")
    add_states(g)

    s = sub.add_parser("show", help="看目前状态档")
    s.add_argument("--json", action="store_true")

    args = ap.parse_args(argv)
    try:
        return {"run": cmd_run, "write": cmd_write,
                "gate": cmd_gate, "show": cmd_show}[args.cmd](args)
    except ValueError as e:
        err(f"错误：参数错误——{scrub(e)}")
        return 1
    except Exception as e:
        # 裸 traceback 会把 /Users/<用户名>/... 印进日报正文并推 Slack。
        err(f"错误：未预期的例外 {type(e).__name__}：{scrub(e)}")
        err(scrub("".join(traceback.format_exc())))
        return 3


if __name__ == "__main__":
    sys.exit(main())
