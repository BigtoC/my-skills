#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# crypto.py —— 加密永续与市场（信号 14–17）
#
# 本档是信号 14–17 取数的**唯一实现**。它原本是 crypto.sh 的 Python 版，行为
# 以那支 shell 版为规格逐位元组对齐；**`crypto.sh` 已於 2026-09-07 切换後删除**
# （回滚靠 git，档案仍在历史里）。下文所有提到 `crypto.sh` 的地方都是**与那支
# 已删除前身的差别记录**，不是还存在的另一份实作——刻意保留档名，因为每一条差别
# 都是当时对着它量出来的，改掉档名就读不出这些决定是相对什么做的。
#
# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ ⚠️ 口径统一（references/signals-c-crypto.md「最容易搞错的一步」，硬约束）║
# ║                                                                          ║
# ║ 阈值 0.05% / 0.1% 都是 **8 小时口径**。各家原始费率的结算周期不同，       ║
# ║ **必须先换算到 8h 再比阈值**：                                           ║
# ║   来源                    原始周期    换算成 8h                          ║
# ║   Binance（默认）         8 小时      直接用                             ║
# ║   Binance（部分币种）     4 小时      × 2                                ║
# ║   Hyperliquid             1 小时      × 8                                ║
# ║                                                                          ║
# ║ 所以每次都必须先打 /fapi/v1/fundingInfo 查 fundingIntervalHours——        ║
# ║ Binance 会不定期调整个别币种。**未出现在结果中的币种按默认 8 小时处理。**║
# ║ 反例：SOL 若已改 4 小时结算，看到的 0.05% 其实等于 8h 的 0.1%，          ║
# ║ 从「偏热」直接跳到「极端触发」。不换算就会漏掉真正危险的信号。            ║
# ║                                                                          ║
# ║ 年化换算固定为：年化% = 8h费率 × 3 × 365。报告同时给 8h 费率与年化。      ║
# ╚══════════════════════════════════════════════════════════════════════════╝
#
# ┌─ 其它踩坑 ───────────────────────────────────────────────────────────────┐
# │ · 兜底顺序（references/signals-c-crypto.md）：                            │
# │     Binance → Hyperliquid → web_search coinglass → 标「数据暂缺」        │
# │   用了备援源**必须在输出里注明来源**：不同交易所费率可以差一倍，          │
# │   来源不同**不能直接跨日比较**。本脚本每一行都带 source 栏。             │
# │ · 触发条件要求「持续 ≥24 小时」→ 必须查历史结算，不能只看当下一个数。    │
# │ · 清算数据（信号 15）没有免费公开 API：Coinglass v4 需 API key。         │
# │   本脚本会**实际探测并回报每个来源的 HTTP 码**，然后标「数据暂缺」，     │
# │   绝不用推断值填补（行为准则第 1 条）。两个探测都要打，其中一个是        │
# │   **已知会失败**的端点——证据要的是「两个来源各自回了什么」，            │
# │   少打一个不是省时间，是把证据砍掉一半。                                │
# │ · BTC Dominance 的 CoinGecko 与 CoinPaprika **口径不同**（分母不同，     │
# │   实测同日 59.1% vs 56.9%）→ 换源当日必须注明，不可跨日直接比较。        │
# │ · 信号 16 的「7d 跌幅 >3%」这条腿曾经**结构性永久不可判定**（固定印     │
# │   「需 CoinGecko Pro」），害得加密信号触发计数系统性偏低。              │
# │   关键认识：缺的是「全市场市值**历史序列**」，不是「**当日** dominance」│
# │   ——当日值每天都免费取得到。换第三方源只会引入第三套分母口径，所以      │
# │   正解是**自己按天累积同源历史**（assets/dominance_history.jsonl），     │
# │   7 天后这条腿就能自己回答，且分母口径天然一致。见 dom_history_append。 │
# └──────────────────────────────────────────────────────────────────────────┘
#
# 依赖：python3（3.8+）与 requests。**不再需要 curl / jq / awk。**
#   stdlib urllib 在本机对多个来源 TLS 验证失败，requests 自带 certifi，
#   所以这支埠用 requests；requests 只在真正要连网时才 import，
#   `--help` 与 `--history` 没装 requests 也能跑。
# 退出码：0 正常｜1 参数错误｜2 依赖缺失｜3 数据暂缺（全部来源失败或本无免费源）
#
# ⚠️ 本脚本是本技能里**唯一**会读写技能目录内档案的一支
#    （assets/dominance_history.jsonl）。因此它由 __file__ 反推技能根目录，
#    **与 cwd 无关**；历史档写不进去只告警、取数照常输出并 exit 0。
#
# ── 与 crypto.sh 的行为差别（完整清单；以前这里写「只有四条」，是错的）──────
# 每一条都是刻意的，而且都往「不知道就说不知道」的方向走。清单不完整比没有
# 清单更糟：下一个读的人会以为「除了这四条以外逐位元组相同」，然后拿一个
# 没列出来的差别当成回归去「修」。oracle（130 案）钉住的是文字与 JSON 的
# 位元组，下列各条要嘛不在 oracle 覆盖范围内，要嘛就是 oracle 里那 6 个
# 已知差异案（c-help / c-nocmd / c-unknown-opt / c-nocurl / c-noawk / c-nojq）。
#
#  1. `PROG` 取自 argv[0]，所以 usage 与错误讯息印的是 `crypto.py`，不是
#     `crypto.sh`。埠不该冒充另一个档名——照抄进报告的排查指令必须真的能跑。
#     （oracle: c-help / c-nocmd / c-unknown-opt）
#  2. 不再检查 curl / jq / awk 是否存在：它们已经不是这支埠的依赖，
#     检查一个自己根本不呼叫的程式，等于把「不知道」记成「查过、没事」。
#     exit 2「依赖缺失」改由 requests 缺席时触发，语意不变。
#     （oracle: c-nocurl / c-noawk / c-nojq）
#  3. 参数解析是手写的左到右循环，不是 argparse：argparse 的 error() 一律
#     以 **exit 2** 结束，而本仓库 2 是保留给「依赖缺失」的；而且这些错误
#     讯息会被照抄进报告，措辞不能交给 argparse 生成。`--help` 仍然完全
#     离线、不需要 requests。
#  4. 7d 腿多一个状态 `unparseable`：crypto.sh 用 jq strptime/mktime 算日期，
#     该函式在部分平台不存在，jq 整段失败会被兜底成「历史不足」——把一次
#     移植性破损伪装成良性状态。这支埠把「日期解析不了」独立成一个 ⚪️ 状态，
#     绝不与「历史不足」混为一谈。四个原有状态
#     （ok / source_mismatch / gap / insufficient）一字未动。
#  5. **`all` 的四个区块平行跑**（crypto.sh 是依序跑）。四个区块打四个不同的
#     host、彼此没有共享状态（只有 dominance 会写历史档），归约（OKCOUNT /
#     MISSING）与输出一律按**固定的区块顺序** funding → liquidations →
#     dominance → stablecoins 收敛，每个区块的 stdout / stderr 各自缓冲后
#     依序吐出。**每个流的位元组完全相同**，差别有两个：耗时，以及
#     stdout 与 stderr 之间的即时交错顺序（shell 是边跑边印，这支埠是
#     整块缓冲后依序吐）。oracle 把两个流分开存，所以验不到交错顺序。
#  6. **数字栏位一律先过 `as_num()` 验证**（见下面那一节）。crypto.sh 把
#     上游字串直接丢给 awk／jq，非数字会被 awk 静默当成 0（`$0.00 B`、
#     `0.0000%`）或让 jq 中途炸掉留下半截档。这支埠一律记 ⚪️／N/A 并附证据。
#     oracle 的 fixture 里没有这种坏栏位，所以 130 案不受影响。
#  7. **任何未预期例外都不得以 traceback 逃到使用者面前**：每个区块各自被
#     围栏（一个区块炸掉不会连累另外三个已经取到数的区块），最外层再有一道
#     总围栏，一律折成 ⚪️ 数据暂缺 + **exit 3**（取数问题），讯息经 `scrub()`
#     去掉家目录绝对路径。crypto.sh 在 `set -e` 下的对应行为是「印半截、
#     回 0」或「回 1」，两者都不合本仓库的退出码约定。
#  8. `RISK_FIXTURE_NOW` 不是 epoch 秒时，这支埠以专属讯息 exit 1；
#     crypto.sh 走的是 bash 算术展开的错误。只影响离线回放，不影响正式执行。
#  9. stdout / stderr 一律钉成 UTF-8（`reconfigure`），不看 locale。
#     输出含大量中日文与 ⚪️/✅/❌ 且会被照抄进报告，locale 不是 UTF-8 时
#     不能让它变成 UnicodeEncodeError 或一堆 `?`。
# 10. crypto.sh 开头会 `mktemp -d`，失败则 exit 2；这支埠不需要工作目录
#     （所有中间结果都在记忆体里），所以那条分支不存在。
# 11. **被要求的币种一个都不许从输出里消失**（信号 14）。crypto.sh 的
#     `funding_hyperliquid` 用 `// empty` + `continue`：主池没有的币种整列
#     丢掉，于是底下那句「N/M 个币种当下高于阈值」的**分母悄悄变小**，一次
#     取不到数被印成一个看起来完全正常、实际更宽松的判定。这支埠一律留一列
#     ⚪️，写出两层各自回了什么，并把该信号降为 partial、进「数据暂缺项」。
#     「三者**同时**」与「**任一**」在集合不完整时的裁决不同：前者一律 ⚪️
#     （否定结论要求集合完整），后者只有在**没有**任何一个超阈值时才 ⚪️
#     （肯定结论不受缺口影响）。oracle 没有覆盖这条（README §6.7）。
# 12. **24h 持续性是三态，不是两态**（信号 14）。crypto.sh 的
#     `persist` 只在 `$9=="YES"` 处被读，于是 `UNKNOWN` 与 `NO` 在判定上
#     完全等价——`/fundingRate` 回 HTTP 500 会被印成「当下三者皆 >阈值，但
#     未满足持续 ≥24h」这个**否定结论**，`--json` 里则是 `false`。这支埠把
#     「评不出来」独立出来：文字走 ⚪️ 无法判定並附 HTTP 证据，JSON 的
#     `leverage_overheated` 走 `null` 並多一个 `triggers_unknown_reason`，
#     每列另有 `persist_24h_reason`。三个 UNKNOWN 成因（备援源不查历史／
#     取数失败／24h 内无结算）不再共用同一句话。
# 13. **读不到的历史档绝不覆写**（信号 16）。crypto.sh 与埠的旧版把
#     「档案不存在」与「档案在、但读不到」都折成一个空清单：报告上讲成
#     「尚无历史，明天就好」，而 `dom_history_append` 紧接着把那个读不到的
#     档**整个盖掉**（读到空 = 本来就没有历史），几个月的累积一次归零。
#     这支埠新增 7d 腿状态 `unreadable`、`--history` 专属 ⚪️ stanza，
#     并在读档失败时**放弃写入**。
# 14. 稳定币净流入/出读不出来时**整格**换成 ⚪️（信号 17）。crypto.sh 只把
#     数字换成「—」，外层照样包上 `$` 与 ` B`，印出来是 `$— B（基准日 —）`
#     ——一个带货币符号与单位的空洞，扫过去很像「零净流入」。
# 15. dominance 两个来源**回 HTTP 200 却给不出可用读数**时会各自留一行
#     stderr 证据（回应不可解析 / 缺栏位）。crypto.sh 这两条路径一个字都不印，
#     只留下一句「回 HTTP 200，改用备援源」，读起来像「取到数了却莫名换源」。
#     ⚠️ 尚未闭合：两个来源都回**非 200** 时，备援源那一层的 HTTP 码仍然
#     哪里都没有（stdout 的 ⚪️ stanza 也没有「失败细节」行）。补上会改动
#     oracle 的 c-dom-missing / c-dom-connfail（含 -json）四个黄金案，
#     与「逐位元组等价」的冻结契约冲突，故留待重录黄金时一并处理。
#
# ── 上游数字的唯一入口：as_num() ───────────────────────────────────────────
# 「HTTP 200 但栏位型别变了」是最常见的一种上游变更（Binance 现在**每个数字
# 都是字串**），也是这支埠唯一会被打爆的地方。规矩只有一条：
# **所有来自上游的数字栏位都要先过 `as_num()`**，验不过回哨兵 `_NUM_BAD`，
# 由呼叫端接进既有的 ⚪️／missing 机制——不 raise、不补 0、不拿邻近栏位推。
#
# ── 状态档（assets/dominance_history.jsonl）的格式 ─────────────────────────
# 这支埠写出来的档**不保证与 jq 逐位元组相同**，语义则完全相同。具体差别：
#   · 上游原样带来的数字保留输入字面量（0.00000396 不会变成 3.96e-06）；
#     **指数写法例外** —— jq 会正规化成 `1E+2` / `1E-7`，这支埠原样保留
#     `1e2` / `1e-7`。
#   · 计算出来的数字走 Python 的最短往返表示（jq 对计算结果也是这样做，
#     但两边的最短表示偶有差异，例如 jq `29544.160` vs Python `29544.16`）。
#   · `00`、`+5`、`.5`、`5.` 这类 jq 收、JSON 规格不收的字面量：整份文件
#     会被判为非法 JSON（`00`），或改写成合法写法（`+5` → `5`）。
# 这个档**只有本脚本自己读**（`dom_history_load`，纯 JSON 解析，不看排版）；
# 仓库里没有第二个读它的程式（已 grep 全库确认：README / SKILL.md /
# references 只是提到它，没有解析它）。所以格式定为「Python 正规化」，
# 不追 jq 的位元组。

import concurrent.futures
import datetime
import json
import os
import re
import sys
import tempfile
import threading

PROG = os.path.basename(sys.argv[0])
TIMEOUT = 25

# ── 技能目录锚定（只有信号 16 的本地历史档需要）──────────────────────────
# 由 __file__ 反推，不看 cwd：SKILL.md 一律用绝对路径调用，但排查时常从别的
# cwd 跑。印在输出里的一律是 DOM_HISTORY_REL（相对路径）——绝对路径含家目录
# 形状，而本技能的输出会进报告并推 Slack（行为准则第 4 条）。
SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
SKILL_ROOT = os.path.dirname(SCRIPT_DIR)
DOM_HISTORY_REL = "assets/dominance_history.jsonl"
DOM_HISTORY = os.path.join(SKILL_ROOT, "assets", "dominance_history.jsonl")
DOM_HISTORY_MAX = 90    # 保留上限，超出丢最旧的；约 3 个月，档案不会无限增长

BINANCE = "https://fapi.binance.com/fapi/v1"
HYPERLIQUID = "https://api.hyperliquid.xyz/info"
COINGECKO = "https://api.coingecko.com/api/v3"
COINPAPRIKA = "https://api.coinpaprika.com/v1"
LLAMA_STABLE = "https://stablecoins.llama.fi"

# 阈值（8h 口径，百分比）—— 行为准则第 3 条：阈值永远不因市场情绪动态调整
# 字面量（"0.10" 而不是 0.1）是刻意保留的：这些数字同时出现在人类输出与
# --json 里，报告会照抄，位元组必须稳定。
FUND_HOT_8H = "0.05"        # 三者同时 >0.05%/8h 且持续 ≥24h = 多头杠杆过热
FUND_EXTREME_8H = "0.10"    # 任一 >0.1%/8h = 急迫反转风险
DOM_DROP_24H = "2.0"        # BTC Dominance 24h 跌幅 >2%
DOM_DROP_7D = "3.0"         # BTC Dominance 7d 跌幅 >3%
# 7d 基准的可用年龄窗口（天）。下限 6 是为了不拿三四天前的读数冒充 7 日变动；
# **上限 10 同样是硬约束**：漏跑几天后最接近的一笔可能是 31 天前，
# 拿它算出来的数字是「31 日变动」，贴上「7d 变动」的标签就是编数字。
# 窗口外一律 ⚪️ 断层，绝不将就。
DOM_7D_MIN_AGE = 6
DOM_7D_MAX_AGE = 10
STABLE_DAY_OUT_USD = "1000000000"   # 单日净流出 >$1B 必须标注

DEFAULT_SYMBOLS = ["BTC", "ETH", "SOL"]


# ══════════════════════════ 数字字面量保存 ═══════════════════════════════
# jq 会**原样保留**输入里的数字字面量（0.00000396 不会变成 3.96e-06，
# 79662.30000000 不会变成 79662.3，0.10 不会变成 0.1）。报告会照抄这些
# 位元组，所以埠也必须保留。做法：解析 JSON 时把数字包成带字面量的
# float/int 子类；一旦拿去做算术，结果自然退化成普通 float，
# 印出来就走「最短往返表示」——那也正是 jq 对计算结果的做法。
class FLit(float):
    def __new__(cls, lit):
        o = float.__new__(cls, lit)
        o.lit = lit
        return o


class ILit(int):
    def __new__(cls, lit):
        o = int.__new__(cls, lit)
        o.lit = lit
        return o


_BAD = object()          # 「解析失败」哨兵，不是 None（null 是合法 JSON 值）


def _reject_const(name):
    """NaN / Infinity / -Infinity 不是合法 JSON，jq 会直接判 parse error
    （`echo '{"a":NaN}' | jq .` → Invalid numeric literal）。Python 的
    json 模组预设**收**它们，收下来之后一路飘到 `%.2f`（印成 `nan`）或
    序列化（丢例外）。这里跟 jq 对齐：整份文件判为非法 JSON。"""
    raise ValueError("非法 JSON 数值字面量：%s" % name)


def jparse(raw):
    """解析 JSON（保留数字字面量）。解析不了回 _BAD。"""
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            return _BAD
    try:
        return json.loads(raw, parse_float=FLit, parse_int=ILit,
                          parse_constant=_reject_const)
    except Exception:
        return _BAD


def ok_json(v):
    """等价 `jq -e . file`：解析得出、且最后输出不是 false / null。"""
    return v is not _BAD and v is not None and v is not False


def _jstr(s):
    out = ['"']
    for ch in s:
        o = ord(ch)
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\b":
            out.append("\\b")
        elif ch == "\f":
            out.append("\\f")
        elif o < 0x20 or o == 0x7F:
            out.append("\\u%04x" % o)
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _jnum(v):
    if isinstance(v, (FLit, ILit)):
        return v.lit
    if isinstance(v, int):
        return str(v)
    f = float(v)
    if f != f or f in (float("inf"), float("-inf")):
        # jq 把 nan/inf 印成 null / 极大值；这里一律拒绝，绝不悄悄写个数字。
        raise ValueError("不可序列化的数值：%r" % (v,))
    if f.is_integer() and abs(f) < 1e17:
        return str(int(f))
    return repr(f)


def jdumps(v, ind=0):
    """jq 预设的 pretty 输出（缩排 2）。"""
    sp = " " * ind
    if v is None:
        return "null"
    if v is True:
        return "true"
    if v is False:
        return "false"
    if isinstance(v, str):
        return _jstr(v)
    if isinstance(v, (int, float)):
        return _jnum(v)
    if isinstance(v, (list, tuple)):
        if not v:
            return "[]"
        inner = ",\n".join(sp + "  " + jdumps(x, ind + 2) for x in v)
        return "[\n" + inner + "\n" + sp + "]"
    if isinstance(v, dict):
        if not v:
            return "{}"
        inner = ",\n".join(
            sp + "  " + _jstr(k) + ": " + jdumps(x, ind + 2) for k, x in v.items())
        return "{\n" + inner + "\n" + sp + "}"
    raise TypeError("不可序列化：%r" % (v,))


def jdumpc(v):
    """jq -c 的紧凑输出。"""
    if v is None:
        return "null"
    if v is True:
        return "true"
    if v is False:
        return "false"
    if isinstance(v, str):
        return _jstr(v)
    if isinstance(v, (int, float)):
        return _jnum(v)
    if isinstance(v, (list, tuple)):
        return "[" + ",".join(jdumpc(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{" + ",".join(_jstr(k) + ":" + jdumpc(x) for k, x in v.items()) + "}"
    raise TypeError("不可序列化：%r" % (v,))


def jq_r(v):
    """等价 `jq -r`：字串原样，其余走 JSON 表示。"""
    if isinstance(v, str):
        return v
    return jdumpc(v)


def jq_r_or_empty(v):
    """等价 `jq -r '. // empty'`：null / false → 空字串（= shell 里的未取到）。"""
    if v is None or v is False:
        return ""
    return jq_r(v)


# ══════════════════════════ 输出缓冲 ═════════════════════════════════════
# 四个区块平行跑，所以每个区块把自己的 stdout / stderr 各自收进一个缓冲区，
# 再由主执行绪按固定顺序吐出。单一子命令时只有一个缓冲区，行为完全一样。
class Out(object):
    def __init__(self):
        self.o = []
        self.e = []

    def p(self, s=""):
        self.o.append(s)

    def w(self, s):
        self.e.append(s)

    def drop_stdout(self):
        """区块内部出错时丢掉半截的 stdout。半截报告里没有任何一行说明它是
        半截的——照抄进报告就成了一份看起来完整的错误报告。stderr 上的告警
        一律保留：那是证据。"""
        self.o = []

    def flush(self):
        if self.o:
            sys.stdout.write("".join(x + "\n" for x in self.o))
        if self.e:
            sys.stderr.write("".join(x + "\n" for x in self.e))


def die(msg, code=1):
    sys.stderr.write("错误：%s\n" % msg)
    sys.stderr.flush()
    raise SystemExit(code)


def warn(msg):
    sys.stderr.write("%s\n" % msg)


# ── 家目录路径折叠 ─────────────────────────────────────────────────────────
# 例外物件的 str() 几乎总带绝对路径（OSError 尤甚），而 traceback 一定会印出
# 脚本自己的绝对路径——正式环境那是 /Users/<使用者名>/…。本技能的输出会进
# 报告并推 Slack（行为准则第 4 条），所以**任何**含例外讯息的输出都要先过
# 这里。写法与仓库里其它四份 `scrub()` 相同（grep `_HOMEISH_RE` 找得到全部）。
_HOMEISH_RE = re.compile(r"(?:/Users|/home|/var/folders)/[^/\s\"']+")


def scrub(text):
    s = str(text)
    try:
        home = os.path.expanduser("~")
    except Exception:
        home = ""
    if home and home != "~":
        s = s.replace(home, "~")
    return _HOMEISH_RE.sub("~", s)


def _internal_note(exc):
    """把一个未预期例外折成一句可以照抄进报告的证据。
    **一律经 scrub**，而且只取型别与讯息、不带 traceback：traceback 的每一行
    都是绝对路径，而报告读者要的是「哪里坏了」，不是行号。"""
    return "脚本内部错误：%s: %s" % (type(exc).__name__, scrub(exc))


# ══════════════════════ fixture 回放钩子（离线迁移验证用）══════════════════
# 正常执行时这段等于不存在：**只有** RISK_FIXTURE_DIR 指到一个存在的目录才启用，
# 变数没设 = 一律走网络。指到不存在的目录一律当参数错误当场停（exit 1），
# 绝不「悄悄退回连网」——那会让一次以为在离线比对的跑法偷偷打了真上游。
#
# 目录布局：一个请求两个档
#   <slug>.body   原始回应内容（bytes 照抄）
#   <slug>.code   HTTP 码；档案不存在时视为 200
# slug 规则（与 crypto.sh 逐字一致）：
#   GET  : 去掉 scheme，再把 [^A-Za-z0-9._-] 全部换成 "_"
#   POST : "POST_" + 上述规则处理过的 url + "_" + 上述规则处理过的 body
# 找不到 fixture 时**大声**回放成连线失败（000）并印一行 stderr，绝不静默。
#
# RISK_FIXTURE_NOW（epoch 秒）同样只在 fixture 模式下生效，用来冻结「今天」，
# 让含日期／年龄的输出可以逐字比对。单独设它而不设 RISK_FIXTURE_DIR 无效。
FIXTURE_DIR = os.environ.get("RISK_FIXTURE_DIR", "")

_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")
_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]")


def fx_slug(s):
    return _SLUG_RE.sub("_", _SCHEME_RE.sub("", s))


def fx_serve(slug, out):
    """→ (http_code, body_bytes)"""
    body_path = os.path.join(FIXTURE_DIR, slug + ".body")
    if os.path.isfile(body_path):
        with open(body_path, "rb") as f:
            body = f.read()
        code_path = os.path.join(FIXTURE_DIR, slug + ".code")
        if os.path.isfile(code_path):
            with open(code_path, "r", encoding="utf-8") as f:
                code = f.read()
        else:
            code = "200"
        return code, body
    out.w("⚠️ RISK_FIXTURE_DIR 里没有 fixture「%s」，本次请求以连线失败（000）回放。" % slug)
    return "000", b""


def fx_epoch():
    now = os.environ.get("RISK_FIXTURE_NOW", "")
    if FIXTURE_DIR and now:
        try:
            return int(now)
        except ValueError:
            die("RISK_FIXTURE_NOW 不是 epoch 秒：%s" % now, 1)
    import time
    return int(time.time())


def fx_date_u(fmt):
    dt = datetime.datetime.fromtimestamp(fx_epoch(), datetime.timezone.utc)
    return dt.strftime(fmt)


NOW_MS = None   # main() 里设定（fx_epoch 会读环境变数，要等参数检查过再算）


# ── HTTP 工具：回传 (http_code, body_bytes)；连线层失败回 000 ──
# Session 是 **thread-local** 的：`all` 会用四条执行绪同时打四个 host，而
# requests.Session 不保证执行绪安全（连线池与 cookie jar 会被并发改写）。
# 一个 host 一条执行绪各用各的 Session，成本可以忽略。
#
# 容器**在汇入时就建好**，不是第一次呼叫时才 lazy 建立：`if _TLS is None:
# _TLS = threading.local()` 没有锁，四条 worker 同时进来时可以各建一个，
# 后建的会盖掉先建的 —— 先建的那条执行绪存进去的 Session 就此失联，
# 之后每次 `_session()` 都重开一个新 Session（连线池全失效），最坏情况下
# 两条执行绪共用同一个 Session 物件。threading 是标准库，无条件汇入不会
# 影响 `--help` / `--history` 的免依赖执行（那两条路径本来就不碰 requests）。
_TLS = threading.local()


def _session():
    """requests 只在真的要连网时才 import：--help / --history 不需要它。"""
    import requests
    s = getattr(_TLS, "s", None)
    if s is None:
        s = requests.Session()
        # 刻意**不设**自订 User-Agent —— crypto.sh 用的是 curl 的预设 UA，
        # 这六个 host 都不要求浏览器 UA（要求浏览器 UA 的是 CNN / Yahoo，
        # 不在本档；FRED 则是带了浏览器 UA 会挂到超时）。
        _TLS.s = s
    return s


def require_requests():
    try:
        import requests  # noqa: F401
    except ImportError:
        die("找不到 Python 套件 requests（pip3 install requests）。"
            "本脚本的四个数据源全部走 HTTPS，标准库 urllib 在本机对多个来源 "
            "TLS 验证失败，必须用自带 certifi 的 requests。", 2)


def http_get(url, out):
    if FIXTURE_DIR:
        return fx_serve(fx_slug(url), out)
    try:
        # allow_redirects=False 是刻意的：crypto.sh 的 curl 没带 -L，所以 3xx
        # 会**如实回报成 3xx**、触发兜底换源。让 requests 默默跟着跳转，等于
        # 把「这个端点搬走了」悄悄记成「取到数了」。
        r = _session().get(url, timeout=TIMEOUT, allow_redirects=False)
        return str(r.status_code), r.content
    except Exception:
        return "000", b""


def http_post_json(url, body, out):
    if FIXTURE_DIR:
        return fx_serve("POST_" + fx_slug(url) + "_" + fx_slug(body), out)
    try:
        r = _session().post(url, data=body.encode("utf-8"), timeout=TIMEOUT,
                            allow_redirects=False,
                            headers={"Content-Type": "application/json"})
        return str(r.status_code), r.content
    except Exception:
        return "000", b""


# ── 数字自检：拿去比大小之前，先确认它真的是个数 ────────────────────────────
# crypto.sh 里这一段是**最容易致命的一处**（空字串比大小在 bash 里不是 false，
# 而是 test 报错 → if 走 else → 取数失败被记成成功）。Python 的比较不会这样
# 静默出错，但「不知道」与「未触发」必须分开这条规矩不变：所有统计函式
# 拿不到数就回 None，呼叫端一律走 ⚪️，绝不落进 else。
_IS_NUM = re.compile(r"^[+-]?([0-9]+\.?[0-9]*|\.[0-9]+)([eE][+-]?[0-9]+)?$")
_IS_POS = re.compile(r"^\+?([0-9]+\.?[0-9]*|\.[0-9]+)$")


def is_uint(s):
    return isinstance(s, str) and s != "" and s.isdigit()


def is_num(s):
    return isinstance(s, str) and _IS_NUM.match(s) is not None


def is_pos_num(s):
    if not isinstance(s, str) or _IS_POS.match(s) is None:
        return False
    try:
        return float(s) > 0
    except ValueError:
        return False


# ══════════════════ 上游数字栏位的唯一入口（行为契约的核心）══════════════════
# 「HTTP 200 + 栏位型别变了」是最常见的一种上游变更：Binance 现在每个数字都是
# 字串，CoinGecko 哪天把 59.1 改成 "59.1" 也不会通知任何人。这种输入有两种
# 错误的处理方式，本档两种都不做：
#   (a) crypto.sh 的老毛病：丢给 awk，非数字静默当 0 → 报告上出现 `$0.00 B`
#       这种看起来完全正常、实际凭空生出来的数字，而且 exit 0。
#   (b) 埠的新毛病：直接 `float()` / 算术 → TypeError/ValueError → traceback、
#       零 stdout、exit 1（1 是「参数错误」，取数问题必须是 3），而且 traceback
#       会印出脚本的绝对路径（正式环境在 $HOME 底下，违反公开仓库规则）。
# 正确的是第三种：**验不过就回哨兵**，由呼叫端接进既有的 ⚪️／missing 机制。
_NUM_BAD = object()
_INF = float("inf")

# 合法 JSON 数字写法。字面量只有长这样才能原样写回 JSON：
# `+5` / `.5` / `5.` / `007` 是 awk、jq tonumber、Python float 都收，
# 但 JSON 规格不收的写法——原样写回去会产出「看起来像 JSON 的坏字串」。
_JSON_NUM_RE = re.compile(r"^-?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][+-]?[0-9]+)?$")


def _finite(x):
    """算术结果的自检：inf / nan 一律不算数字。
    `%.4f % inf` 会印出 `inf`，序列化更会丢例外——两者都要在源头挡掉。"""
    try:
        f = float(x)
    except (TypeError, ValueError, OverflowError):
        return False
    return f == f and f != _INF and f != -_INF


def _present(v):
    """等价 jq 的 `// empty` 判定：null / false 视为「栏位不存在」。"""
    return v is not None and v is not False


def as_num(v):
    """上游数字栏位的唯一入口。

    回传一个既能直接拿去算术、也能直接序列化的数值；**验不过一律回
    `_NUM_BAD`**。绝不 raise、绝不回 0、绝不用邻近栏位推——这三件事每一件
    都是把「不知道」记成「查过、没事」。

    收：JSON 数字（含保留字面量的 FLit / ILit）、以及**整个字串都长得像数字**
        的字串（Binance 现在每个数字都是字串）。
    拒：null、布林、物件、阵列、空字串、非数字字串、非有限值（inf/nan）、
        以及大到 float 装不下的整数。
    """
    if v is None or isinstance(v, bool):
        return _NUM_BAD
    if isinstance(v, (int, float)):
        return v if _finite(v) else _NUM_BAD
    if isinstance(v, str):
        if not is_num(v) or not _finite(v):
            return _NUM_BAD
        return _lit(v)
    return _NUM_BAD


def _num_or_null(v):
    """as_num 的 JSON 版：验不过记 `null`（= N/A），不是 0。"""
    n = as_num(v)
    return None if n is _NUM_BAD else n


def _brief(v, cap=60):
    """把一个坏栏位压成一句可以放进报告的证据。**只印型别与截断后的值**，
    不印整包 payload：证据要的是「哪个栏位、是什么型别、长什么样」。"""
    t = ("null" if v is None else
         "boolean" if isinstance(v, bool) else
         "number" if isinstance(v, (int, float)) else
         "string" if isinstance(v, str) else
         "array" if isinstance(v, (list, tuple)) else
         "object" if isinstance(v, dict) else type(v).__name__)
    try:
        s = jdumpc(v)
    except Exception:
        s = repr(v)
    s = " ".join(s.split())
    if len(s) > cap:
        s = s[:cap] + "…"
    return "%s %s" % (t, s)


def num(v, f=2):
    return "%.*f" % (f, float(v))


def num_na(s, f=2):
    """`num` 的守门版：读不出数字就印 ⚪️ N/A，绝不印一个凭空生出来的 0.00。"""
    return num(s, f) if is_num(s if isinstance(s, str) else jq_r(s)) else "⚪️ N/A"


def snum(v, f=2):
    """带正负号的格式化。**变动量一律走这个**：印在「跌幅」这类带方向的标签
    底下时，无号的 "1.50" 会被读成「跌了 1.5%」，实际却是涨了 1.5%——
    方向读反比读不到更危险。"""
    return "%+.*f" % (f, float(v))


def snum_na(s, f=2):
    """`snum` 的守门版。`snum("")` 会印出 `+0.00`——一个看起来完全正常、
    实际凭空生出来的数字，而且带方向语义（`+` 会被读成「涨了」）。"""
    return snum(s, f) if is_num(s if isinstance(s, str) else jq_r(s)) else "⚪️ N/A"


def bwidth(s):
    return len(s.encode("utf-8"))


def pad(s, w, left=False):
    """printf 的 %-Ns / %Ns 数的是**位元组**，中文字一个 3 位元组、显示宽度
    却是 2 —— 表头与表格的对齐全靠这个语意，用字元数会整排歪掉。"""
    n = w - bwidth(s)
    if n <= 0:
        return s
    return s + " " * n if left else " " * n + s


def usage(out):
    out.p("""%(prog)s —— 加密永续与市场（信号 14–17）

用法:
  %(prog)s <子命令> [选项]

子命令:
  funding       信号 14：BTC/ETH/SOL 永续资金费率（自动做 8h 口径换算 + 24h 持续性检查）
  liquidations  信号 15：过去 24h 清算（无免费公开源，会实测各来源并回报 HTTP 码）
  dominance     信号 16：BTC Dominance
  stablecoins   信号 17：USDT + USDC 总供应与净流入/流出
  all           依序跑上面四项

选项:
  --json        以 JSON 输出
  --symbols A,B 只查指定币种（默认 BTC,ETH,SOL；仅对 funding 有效）
  --history     只印本地累积的 dominance 历史（仅对 dominance 有效，不连网）
  -h, --help    显示本说明

例子:
  %(prog)s funding
  %(prog)s funding --symbols BTC,ETH
  %(prog)s dominance
  %(prog)s dominance --history          # 排查 7d 腿：看已累积几天、来源是否一致
  %(prog)s stablecoins --json
  %(prog)s all --json

信号 16 的 7d 腿:
  「7d 跌幅 >3%%」不再是永久不可判定。dominance 每次成功取数会往
  %(hist)s 追加一笔（同日重跑覆盖，最多留 %(cap)d 笔），
  累积满 7 天后本脚本自己算 7d 变动。**7 日前那笔的 source 必须与今日相同**，
  否则一律拒绝比较（分母口径不同）。历史不足 7 天一律 ⚪️，绝不当成未触发。

退出码:
  all 只要有任一区块取到数就回 0，并在结尾列出「本次数据暂缺项」；
  四个区块全部失败才回 3。单一子命令取不到数一律回 3。""" % {
        "prog": PROG, "hist": DOM_HISTORY_REL, "cap": DOM_HISTORY_MAX})


# ════════════════════════════ 信号 14 · 资金费率 ════════════════════════════
# 产出 rows（等价 crypto.sh 的 funding.tsv），每列栏位一律是**字串**：
#   symbol source raw_rate interval_h rate8h_pct annual_pct mark next_ms persist n24h
# 保持字串是为了保住数字字面量（见 FLit/ILit 那段注解）。
# 第 11 栏 persist_why 是埠自己加的：`persist == "UNKNOWN"` 有好几种成因
# （备援源本来就不查历史／`/fundingRate` 取数失败／历史资料解析不了／24h 内
# 没有任何结算），把它们印成同一句话，等于把一次**取数失败**讲成一句
# 「查过、没有历史」。这一栏就是那句话的证据。
_F_SYM, _F_SRC, _F_RAW, _F_IV, _F_R8, _F_ANN, _F_MARK, _F_NXT, _F_PER, _F_N24, \
    _F_PWHY = range(11)

# persist == "UNKNOWN" 且没有专属原因时的默认说法（Hyperliquid 备援源本来
# 就不查历史，这是它的正常状态，不是失败）。
_PWHY_DEFAULT = "备援源无历史"


class Funding(object):
    def __init__(self, symbols):
        self.symbols = symbols
        self.rows = []
        self.source = ""
        self.note = ""
        # 取不到数的币种：**绝不让它从输出里消失**。少一列会让「三者同时
        # >阈值」的分母悄悄从 3 变成 2，而报告上那句「0/2 个币种」看起来
        # 完全正常——一次取数失败被写成一个更宽松的判定。
        self.gaps = []

    def _gap(self, sym, attempted, reason):
        self.gaps.append(_od(symbol=sym, attempted=list(attempted),
                             reason=reason))

    # ── 主源 ──
    def binance(self, out):
        rows = []
        code, body = http_get(BINANCE + "/fundingInfo", out)
        info = jparse(body)
        if code != "200" or not ok_json(info):
            self.note = "Binance /fundingInfo 回 HTTP %s" % code
            return False

        for sym in self.symbols:
            # 结算周期：查不到该币种 → 按 Binance 默认 8 小时（reference 明订）
            iv = self._interval(info, sym)
            # 「查不到该币种 → 默认 8」只适用于**栏位不存在**。若栏位在、值却
            # 不是正数（API 改了型别），**不能也退回 8**：r8 = raw × 8 ÷ iv，
            # 真实周期若是 4h 而按 8h 算，0.1% 会被读成 0.05%，正好漏掉档头
            # 警告的那种危险信号；iv=0 更会算出 inf。一律当取数失败。
            if not is_pos_num(iv):
                self.note = ("Binance /fundingInfo 给出无法解析的 %sUSDT 结算周期「%s」，"
                             "无法换算 8h 口径" % (sym, iv))
                return False

            code, body = http_get(BINANCE + "/premiumIndex?symbol=%sUSDT" % sym, out)
            pi = jparse(body)
            if code != "200" or not ok_json(pi):
                self.note = "Binance /premiumIndex(%sUSDT) 回 HTTP %s" % (sym, code)
                return False
            f_raw = _get(pi, "lastFundingRate")
            f_mark = _get(pi, "markPrice")
            if not _present(f_raw) or not _present(f_mark):
                self.note = ("Binance /premiumIndex(%sUSDT) 缺 lastFundingRate 或 "
                             "markPrice 栏位" % sym)
                return False
            # 栏位在、值却不是数字（Binance 已经把每个数字都发成字串了，
            # 下一次改型别只是时间问题）：**不得当成 0**——awk 会静默把
            # "abc" 当 0，产出 0.0000%/8h 这种「查过、没事」的假读数。
            v_raw = as_num(f_raw)
            v_mark = as_num(f_mark)
            if v_raw is _NUM_BAD or v_mark is _NUM_BAD:
                bad = []
                if v_raw is _NUM_BAD:
                    bad.append("lastFundingRate=%s" % _brief(f_raw))
                if v_mark is _NUM_BAD:
                    bad.append("markPrice=%s" % _brief(f_mark))
                self.note = ("Binance /premiumIndex(%sUSDT) 的数字栏位不是可用数值"
                             "（%s），不得当成 0 使用" % (sym, "；".join(bad)))
                return False
            raw = jq_r(v_raw)
            mark = jq_r(v_mark)

            # nextFundingTime 只是附注栏，缺了不该让整个主源失效：
            # 栏位不存在 → 与 crypto.sh 的 `// 0` 一致记 0；
            # 栏位在但不是数字 → 记 N/A（null），并在注解里点名，不猜、不退回 0。
            f_nxt = _get(pi, "nextFundingTime")
            if not _present(f_nxt):
                nxt = "0"
            else:
                v_nxt = as_num(f_nxt)
                if v_nxt is _NUM_BAD:
                    nxt = None
                    self.note = _join_note(
                        self.note,
                        "Binance /premiumIndex(%sUSDT) 的 nextFundingTime 不是可用数值"
                        "（%s），该栏记 N/A" % (sym, _brief(f_nxt)))
                else:
                    nxt = jq_r(v_nxt)

            # 步骤 2：24h 持续性。limit=12 足以覆盖 8h 与 4h 两种周期的 24 小时。
            code, body = http_get(
                BINANCE + "/fundingRate?symbol=%sUSDT&limit=12" % sym, out)
            hist = jparse(body)
            # 三态，不是两态：**持续（YES）／未持续（NO）／评不出来（UNKNOWN）**。
            # 把第三种折进第二种，就是把一次「/fundingRate 取不到数」记成
            # 「查过了，没有持续 24 小时」——一个**否定结论**，而它会直接
            # 决定「多头杠杆过热」那条腿要不要触发（行为准则第 1 条）。
            # pwhy 是这个 UNKNOWN 的证据，会一路带到人类输出与 --json。
            n24, persist, pwhy = "0", "UNKNOWN", ""
            if code == "200" and ok_json(hist):
                got = self._persist(hist, iv)
                if got is None:
                    # 历史算不出来 → 退回既有的「无法判定」表示法：
                    # 不知道就是不知道，不是没触发（行为准则第 1 条）。
                    n24, persist = "0", "UNKNOWN"
                    pwhy = ("Binance /fundingRate(%sUSDT) 的历史结算资料无法解析"
                            "（fundingTime／fundingRate 栏位不是可用数值）" % sym)
                else:
                    n24, persist = got
                    if persist == "UNKNOWN":
                        pwhy = ("Binance /fundingRate(%sUSDT) 近 24h 内没有任何"
                                "结算纪录可比对" % sym)
            else:
                pwhy = "Binance /fundingRate(%sUSDT) 回 HTTP %s" % (sym, code)
            if not is_uint(n24):
                n24 = "0"
            if persist not in ("YES", "NO", "UNKNOWN"):
                persist = "UNKNOWN"
                pwhy = ("Binance /fundingRate(%sUSDT) 的 24h 持续性统计未回传"
                        "可用结果" % sym)
            if persist != "UNKNOWN":
                pwhy = ""

            r8 = float(raw) * 8 / float(iv) * 100     # → 8h 口径百分比
            ann = r8 * 3 * 365                        # 年化%
            if not _finite(r8) or not _finite(ann):
                # 上游给了个大到换算后溢出的值。`%.4f % inf` 会印出 `inf`，
                # 一路飘进报告与 JSON —— 一律当取数失败。
                self.note = ("Binance /premiumIndex(%sUSDT) 的 lastFundingRate"
                             "「%s」换算成 8h 口径（周期 %s）后溢出，无法产生"
                             "可用数字" % (sym, raw, iv))
                return False
            rows.append([sym, "Binance", raw, iv, "%.4f" % r8, "%.2f" % ann,
                         mark, nxt, persist, n24, pwhy])

        if not rows:
            return False
        self.rows = rows
        return True

    @staticmethod
    def _interval(info, sym):
        """等价 jq '[.[] | select(.symbol==$s) | .fundingIntervalHours] | first // 8'
        后面那两句 shell 兜底（空 / "null" → 8）。"""
        want = sym + "USDT"
        try:
            vals = [e.get("fundingIntervalHours")
                    for e in info if isinstance(e, dict) and e.get("symbol") == want]
        except (AttributeError, TypeError):
            return "8"
        first = vals[0] if vals else None
        if first is None or first is False:
            return "8"
        iv = jq_r(first)
        if iv == "" or iv == "null":
            return "8"
        return iv

    def _persist(self, hist, iv):
        """→ (n24, persist) 或 None（统计失败 = 无法判定，不得当成未触发）。
        每一笔历史结算都要各自换算成 8h 口径再比阈值。"""
        hot = float(FUND_HOT_8H)
        if not isinstance(hist, list) or NOW_MS is None:
            return None
        cutoff = NOW_MS - 86400000
        rates = []
        for e in hist:
            if not isinstance(e, dict):
                return None
            ft = e.get("fundingTime")
            t = as_num(0 if not _present(ft) else ft)   # 缺栏位 → 0（jq 的 `// 0`）
            if t is _NUM_BAD:
                return None                            # 时间戳坏掉 = 无法判定
            if float(t) < cutoff:
                continue
            fr = as_num(e.get("fundingRate"))
            if fr is _NUM_BAD:
                return None                            # 单笔坏掉就不敢说 YES/NO
            r = float(fr) * 8 / float(iv) * 100
            if not _finite(r):
                return None
            rates.append(r)
        if not rates:
            return "0", "UNKNOWN"
        every = all(r > hot for r in rates)
        return str(len(rates)), ("YES" if every else "NO")

    # ── 备援源 ──
    def hyperliquid(self, out):
        # Hyperliquid 的 funding 是**每小时**费率 → × 8 才是 8h 口径。
        # 主池（不带 dex）拿 BTC / ETH / SOL；本函数不查历史，
        # 故 24h 持续性标 UNKNOWN。
        code, body = http_post_json(HYPERLIQUID, '{"type":"metaAndAssetCtxs"}', out)
        hl = jparse(body)
        if code != "200" or not ok_json(hl):
            self.note = _join_note(self.note, "Hyperliquid 回 HTTP %s" % code)
            return False
        # 主源已经失败过一次，它的说法就是这一层的第一条证据。
        tier1 = self.note or "Binance 取数失败（原因未回报）"
        rows = []
        for sym in self.symbols:
            ctx = _hl_ctx(hl, sym)
            f_raw = _get(ctx, "funding")
            f_mark = _get(ctx, "markPx")
            if not _present(f_raw):
                # 主池里没有这个币种。crypto.sh 的 `// empty` 在这里**整列丢掉**，
                # 于是被要求的币种从输出里凭空消失，而底下那句「N/M 个币种当下
                # 高于阈值」的分母跟着悄悄变小——一次取不到数被印成一个看起来
                # 完全正常、实际上更宽松的判定。这支埠一律留一列 ⚪️。
                self._gap(sym, [tier1, "Hyperliquid：主池（不带 dex）无此永续市场"],
                          "Hyperliquid 主池没有 %s 永续市场（备援源覆盖不到）" % sym)
                out.w("⚠️ Hyperliquid 主池没有 %s 永续市场，本币种记为 ⚪️ 取不到数；"
                      "「三者同时 >阈值」的分母因此不完整，不得当成未触发。" % sym)
                continue
            v_raw = as_num(f_raw)
            v_mark = as_num(f_mark)
            iv = 1                            # Hyperliquid 每小时结算
            r8 = (float(v_raw) * 8 / iv * 100) if v_raw is not _NUM_BAD else None
            if v_raw is _NUM_BAD or v_mark is _NUM_BAD or not _finite(r8):
                # 栏位在、值不可用 → **大声**跳过这个币种，绝不静默：
                # 静默少一列会让「三者同时 >阈值」的分母悄悄从 3 变成 2。
                self._gap(sym, [tier1,
                                "Hyperliquid：funding=%s；markPx=%s（不是可用数值）"
                                % (_brief(f_raw), _brief(f_mark))],
                          "Hyperliquid %s 的 funding／markPx 不是可用数值，"
                          "不得当成 0" % sym)
                out.w("⚠️ Hyperliquid %s 的 funding／markPx 不是可用数值"
                      "（funding=%s；markPx=%s），本币种记为取不到数，不得当成 0。"
                      % (sym, _brief(f_raw), _brief(f_mark)))
                continue
            ann = r8 * 3 * 365
            if not _finite(ann):
                self._gap(sym, [tier1,
                                "Hyperliquid：funding=%s（年化后溢出）"
                                % _brief(f_raw)],
                          "Hyperliquid %s 的 funding 年化后溢出，无法产生可用数字"
                          % sym)
                out.w("⚠️ Hyperliquid %s 的 funding（%s）年化后溢出，本币种记为"
                      "取不到数。" % (sym, _brief(f_raw)))
                continue
            raw = jq_r(v_raw)
            mark = jq_r(v_mark)
            rows.append([sym, "Hyperliquid", raw, "1", "%.4f" % r8, "%.2f" % ann,
                         mark, "0", "UNKNOWN", "0", ""])
        if not rows:
            # 一列真数据都没有 = 这一层也失败了。此时整个信号走 ⚪️ 数据暂缺
            # （exit 3），gaps 里的逐币种证据并进 note 一起印出来。
            self.note = _join_note(
                self.note,
                "Hyperliquid 回 HTTP 200，但被查询的币种一个都取不到（%s）"
                % "；".join("%s：%s" % (g["symbol"], g["reason"])
                            for g in self.gaps))
            return False
        self.rows = rows
        return True

    def run(self, out):
        if self.binance(out):
            self.source = "Binance"
            return True
        out.w("⚠️ Binance 取数失败（%s），依兜底顺序改用 Hyperliquid。"
              % (self.note or "原因未回报"))
        if self.hyperliquid(out):
            self.source = "Hyperliquid"
            self.note = _join_note(
                self.note,
                "已改用备援源 Hyperliquid，**不同交易所费率可差一倍，"
                "不可与前日 Binance 读数直接比较**")
            return True
        self.source = ""
        return False

    # ── 统计（拿不到就回 None → 呼叫端走 ⚪️）──
    def _count(self, pred):
        try:
            return sum(1 for r in self.rows if pred(r))
        except (TypeError, ValueError):
            return None

    def stats(self):
        n_all = len(self.rows) if self.rows is not None else None
        hot, ext = float(FUND_HOT_8H), float(FUND_EXTREME_8H)
        n_hot = self._count(lambda r: float(r[_F_R8]) > hot)
        n_persist = self._count(
            lambda r: float(r[_F_R8]) > hot and r[_F_PER] == "YES")
        n_extreme = self._count(lambda r: float(r[_F_R8]) > ext)
        return n_all, n_hot, n_persist, n_extreme

    # ── 输出 ──
    def render_text(self, out):
        # 标题用实际查询的币种，不要写死 BTC/ETH/SOL —— --symbols 可以只查子集，
        # 标题与表格内容不符会让人误以为漏了币种。
        out.p("【信号 14】%s 永续资金费率　来源：%s" % ("/".join(self.symbols), self.source))
        out.p("口径：原始费率已按结算周期换算成 **8h 口径**；年化 = 8h × 3 × 365。")
        out.p()
        out.p("  %s %s %s %s %s %s %s %s" % (
            pad("币种", 5, True), pad("来源", 12, True), pad("原始费率", 10),
            pad("周期h", 6), pad("8h费率%", 12), pad("年化%", 10),
            pad("标记价", 14), "24h持续>阈值"))
        for r in self.rows:
            if r[_F_PER] == "YES":
                p = "是(%s笔全部)" % r[_F_N24]
            elif r[_F_PER] == "NO":
                p = "否(%s笔)" % r[_F_N24]
            else:
                # UNKNOWN 的成因要说出来：「备援源本来就不查历史」与
                # 「/fundingRate 回 HTTP 500」是完全不同的两件事，
                # 印成同一句就是把取数失败讲成一个正常状态。
                p = "无法判定(%s)" % (r[_F_PWHY] or _PWHY_DEFAULT)
            out.p("  %s %s %s %s %s %s %s %s" % (
                pad(r[_F_SYM], 5, True), pad(r[_F_SRC], 12, True),
                pad(r[_F_RAW], 10), pad(r[_F_IV], 6),
                pad("%.4f" % float(r[_F_R8]), 12),
                pad("%.2f" % float(r[_F_ANN]), 10),
                pad(r[_F_MARK], 14), p))
        # 取不到数的币种照样占一列：**被要求的币种一个都不许从输出里消失**。
        for g in self.gaps:
            out.p("  %s ⚪️ 数据暂缺　已尝试：%s"
                  % (pad(g["symbol"], 5, True), " → ".join(g["attempted"])))
        out.p()
        out.p("阈值判定（8h 口径，严格比对，不加软化语言）：")
        n_all, n_hot, n_persist, n_extreme = self.stats()
        hot = float(FUND_HOT_8H)
        # 「当下 >阈值、但持续性评不出来」的币种数。这几个币种把「多头杠杆
        # 过热」推向**无法判定**，绝不是推向「未触发」。
        n_unk = self._count(
            lambda r: float(r[_F_R8]) > hot and r[_F_PER] == "UNKNOWN")
        n_gap = len(self.gaps)
        gap_syms = "、".join(g["symbol"] for g in self.gaps)
        unk_list = "；".join(
            "%s：%s" % (r[_F_SYM], r[_F_PWHY] or _PWHY_DEFAULT)
            for r in self.rows
            if float(r[_F_R8]) > hot and r[_F_PER] == "UNKNOWN") if n_unk else ""

        # 笔数本身都没统计出来 → 只能 ⚪️。把「不知道」记成「查过、没事」
        # 是本仓库最贵的一种错。
        if n_all is None or n_hot is None or n_persist is None or n_unk is None:
            out.p("  多头杠杆过热（三者同时 >%s%%/8h 持续 ≥24h） ... "
                  "⚪️ 无法判定（币种笔数统计失败，不得当成未触发）" % FUND_HOT_8H)
        elif n_gap > 0 and n_hot < n_all:
            # 缺口在，但**已经取到的**币种里就有低于阈值的 —— 一个合取式只要
            # 有一项为假就为假，缺的那几个再高也翻不了案。这里 ❌ 是有资格下的
            # 结论，不该退成 ⚪️（无谓的 ⚪️ 同样会扭曲触发计数）。分母则必须
            # 说实话：只写「%d/%d」会让人以为总共就查了这么多个币种。
            out.p("  多头杠杆过热（三者同时 >%s%%/8h 持续 ≥24h） ... "
                  "❌ 未触发（已取到的 %d/%d 个币种当下高于阈值；另有 %d 个取不到数："
                  "%s——但已取到的就有未过阈值者，足以否定「同时」）"
                  % (FUND_HOT_8H, n_hot, n_all, n_gap, gap_syms))
        elif n_gap > 0:
            # 已取到的币种全部 >阈值，胜负全押在取不到数的那几个身上：
            # 「三者**同时**」是对整个被要求集合的判定，集合不完整时
            # 「未触发」是一个没有资格下的结论。
            out.p("  多头杠杆过热（三者同时 >%s%%/8h 持续 ≥24h） ... "
                  "⚪️ 无法判定（%d/%d 个币种取不到数：%s；「同时」的分母不完整，"
                  "不得当成未触发）"
                  % (FUND_HOT_8H, n_gap, n_gap + n_all, gap_syms))
        elif n_hot == n_all and n_all > 0 and n_unk > 0:
            out.p("  多头杠杆过热（三者同时 >%s%%/8h 持续 ≥24h） ... "
                  "⚪️ 无法判定（当下 %d/%d 个币种皆 >阈值，但 %d 个币种的 24h "
                  "持续性评不出来：%s；不得当成未触发）"
                  % (FUND_HOT_8H, n_hot, n_all, n_unk, unk_list))
        elif n_persist == n_all and n_all > 0:
            out.p("  多头杠杆过热（三者同时 >%s%%/8h 持续 ≥24h） ... ✅ 触发" % FUND_HOT_8H)
        elif n_hot == n_all and n_all > 0:
            out.p("  多头杠杆过热（三者同时 >%s%%/8h 持续 ≥24h） ... "
                  "❌ 未触发（当下三者皆 >阈值，但未满足持续 ≥24h）" % FUND_HOT_8H)
        else:
            out.p("  多头杠杆过热（三者同时 >%s%%/8h 持续 ≥24h） ... "
                  "❌ 未触发（%s/%s 个币种当下高于阈值）" % (FUND_HOT_8H, n_hot, n_all))

        if n_extreme is None:
            out.p("  急迫反转风险（任一 >%s%%/8h） .............. "
                  "⚪️ 无法判定（币种笔数统计失败，不得当成未触发）" % FUND_EXTREME_8H)
        elif n_extreme > 0:
            # 「任一」只要有一个够就成立 —— 取不到数的币种不影响这个**肯定**结论。
            ext = float(FUND_EXTREME_8H)
            lst = "".join("%s(%.4f%%) " % (r[_F_SYM], float(r[_F_R8]))
                          for r in self.rows if float(r[_F_R8]) > ext)
            out.p("  急迫反转风险（任一 >%s%%/8h） .............. ✅ 触发：%s"
                  % (FUND_EXTREME_8H, lst))
        elif n_gap > 0:
            # 反过来，「一个都没有」这个**否定**结论要求集合完整。
            out.p("  急迫反转风险（任一 >%s%%/8h） .............. "
                  "⚪️ 无法判定（%d 个币种取不到数：%s；已取到的都未超阈值，"
                  "但「任一」不能在不完整的集合上判否）"
                  % (FUND_EXTREME_8H, n_gap, gap_syms))
        else:
            out.p("  急迫反转风险（任一 >%s%%/8h） .............. ❌ 未触发" % FUND_EXTREME_8H)

        if self.note:
            out.p("  注：%s" % self.note)

    def to_json(self):
        rows = []
        for r in self.rows:
            o = _od(
                symbol=r[_F_SYM], source=r[_F_SRC],
                raw_rate=_lit(r[_F_RAW]), interval_hours=_lit(r[_F_IV]),
                rate_8h_pct=_lit(r[_F_R8]), annualized_pct=_lit(r[_F_ANN]),
                mark=_lit(r[_F_MARK]),
                # None = 上游给了不可用的 nextFundingTime → N/A，不是 0。
                next_funding_ms=(None if r[_F_NXT] is None else _lit(r[_F_NXT])),
                persist_24h=r[_F_PER], settlements_24h=_lit(r[_F_N24]))
            # 只有真的有专属原因才多这一栏：备援源本来就不查历史是它的正常
            # 状态（`source` 已经说明了），不需要额外一句话；
            # 「/fundingRate 回 HTTP 500」则非说不可。
            if r[_F_PER] == "UNKNOWN" and r[_F_PWHY]:
                o["persist_24h_reason"] = r[_F_PWHY]
            rows.append(o)
        hot, ext = float(FUND_HOT_8H), float(FUND_EXTREME_8H)

        def _gt(r, key, bound):
            v = r[key]
            return v is not None and v > bound

        hot_rows = [r for r in rows if _gt(r, "rate_8h_pct", hot)]
        unk_rows = [r for r in hot_rows if r["persist_24h"] == "UNKNOWN"]
        ext_rows = [r for r in rows if _gt(r, "rate_8h_pct", ext)]

        # 三态，与文字分支同一套判据：**null = 无法判定**，不是 false。
        # 机读侧把「评不出来」写成 false，下游的触发计数会系统性偏低，
        # 而那正是这条腿存在的意义（行为准则第 1 条）。反过来，能下的结论
        # 就要下：把一个确定的 false 退成 null 同样会扭曲计数。
        why = []
        gap_syms = "、".join(g["symbol"] for g in self.gaps)

        # ── leverage_overheated：合取式（全部币种同时 >hot 且持续 ≥24h）──
        if len(rows) == 0:
            lev = None
            why.append("leverage_overheated：一列可用读数都没有")
        elif len(hot_rows) < len(rows):
            # 已取到的币种里就有未过阈值者 → 合取式确定为假，缺的几个翻不了案。
            lev = False
        elif self.gaps:
            lev = None
            why.append("leverage_overheated：已取到的币种全部 >阈值，但被要求的"
                       "币种有 %d 个取不到数（%s），「同时」的判定集合不完整"
                       % (len(self.gaps), gap_syms))
        elif unk_rows:
            lev = None
            why.append("leverage_overheated：当下全部币种 >阈值，但 %d 个币种的 "
                       "24h 持续性评不出来（%s）"
                       % (len(unk_rows),
                          "；".join("%s：%s" % (r["symbol"],
                                               r.get("persist_24h_reason")
                                               or _PWHY_DEFAULT)
                                    for r in unk_rows)))
        else:
            lev = all(r["persist_24h"] == "YES" for r in rows)

        # ── imminent_reversal：存在量词（任一 >extreme）──
        # 找到一个就成立，缺口不影响这个**肯定**结论；一个都没找到时，
        # 「没有」这个**否定**结论才要求集合完整。
        if ext_rows:
            imm = True
        elif self.gaps:
            imm = None
            why.append("imminent_reversal：已取到的都未超极端阈值，但有 %d 个"
                       "币种取不到数（%s），「任一」不能在不完整的集合上判否"
                       % (len(self.gaps), gap_syms))
        else:
            imm = False

        doc = _od(
            signal=14, name="永续资金费率", status="ok", source=self.source,
            note=self.note,
            caliber="所有费率已换算成 8h 口径；年化 = 8h × 3 × 365",
            thresholds=_od(hot_8h_pct=_lit(FUND_HOT_8H),
                           extreme_8h_pct=_lit(FUND_EXTREME_8H)),
            rows=rows,
            triggers=_od(leverage_overheated=lev, imminent_reversal=imm))
        # 以下栏位只在真的有缺口时才出现：正常那一天的 JSON 位元组不变。
        if self.gaps:
            doc["status"] = "partial"
            doc["requested_symbols"] = list(self.symbols)
            doc["missing_symbols"] = list(self.gaps)
        if why:
            doc["triggers_unknown_reason"] = "；".join(why)
        return doc

    def missing_json(self):
        o = _od(
            signal=14, name="永续资金费率", status="missing",
            attempted=["Binance fapi", "Hyperliquid info"], note=self.note,
            next_step="web_search coinglass funding rate；仍无则标 ⚪️ 数据暂缺 + 报滞后周数")
        if self.gaps:
            o["requested_symbols"] = list(self.symbols)
            o["missing_symbols"] = list(self.gaps)
        return o

    def missing_text(self, out):
        out.p("【信号 14】永续资金费率　⚪️ 数据暂缺")
        out.p("  已尝试来源：Binance fapi.binance.com → Hyperliquid api.hyperliquid.xyz")
        out.p("  失败细节：%s" % self.note)
        out.p("  下一步：web_search coinglass funding rate；仍取不到就标 ⚪️ + "
              "写出上次已知读数与滞后周数。")


def _join_note(a, b):
    return (a + "；" + b) if a else b


def _get(obj, key):
    if isinstance(obj, dict):
        return obj.get(key)
    return None


def _hl_ctx(hl, sym):
    """等价 jq '[.[0].universe, .[1]] | transpose | map(select(.[0].name==$s)) | .[0][1]'"""
    try:
        uni = hl[0]["universe"]
        ctxs = hl[1]
    except (TypeError, KeyError, IndexError):
        return None
    for i in range(min(len(uni), len(ctxs))):
        u = uni[i]
        if isinstance(u, dict) and u.get("name") == sym:
            return ctxs[i]
    return None


def _lit(s):
    """把「长得像数字的字串」还原成带字面量的数字，等价 jq 的 tonumber
    （jq 会保留字面量，所以 "0.00000396" 不会变成 3.96e-06）。

    **不 raise**：呼叫端一律先过 `as_num()`／`is_num()`，这里再多一道防线，
    因为一个会丢例外的序列化辅助函式，代价是整份 --json 文件消失。
    字面量若不是合法的 JSON 数字写法（`+5` / `.5` / `5.` / `007`），
    改用 Python 的最短往返表示——原样写回去会产出坏 JSON。"""
    if not isinstance(s, str) or not is_num(s) or not _finite(s):
        return None
    if _JSON_NUM_RE.match(s):
        return FLit(s) if ("." in s or "e" in s or "E" in s) else ILit(s)
    f = float(s)
    if f.is_integer() and abs(f) < 1e17:
        return ILit(repr(int(f)))
    return FLit(repr(f))


def _od(**kw):
    """Python 3.7+ 的 dict 保序，等价 jq 物件字面量的键序。"""
    return dict(kw)


def _block_failed(out, exc, signal, name):
    """区块内部出了未预期的错：丢掉半截 stdout、在 stderr 上留下证据，
    再让呼叫端走**该区块自己的** ⚪️ 数据暂缺输出。
    每个区块各自围栏，是为了「一个坏栏位毁掉三个已经取到数的区块」这件事
    不会发生 —— `all` 底下最贵的一种失败就是它。"""
    note = _internal_note(exc)
    out.drop_stdout()
    out.w("⚠️ 【信号 %d】%s 区块发生未预期错误（%s），本区块降级为 ⚪️ 数据暂缺；"
          "其余区块不受影响。" % (signal, name, note))
    return note


def do_funding(json_mode, symbols):
    out = Out()
    f = Funding(symbols)
    try:
        if f.run(out):
            # 取到数、但有币种缺席 → 这一区块是 partial，缺席的币种必须进
            # 「本次数据暂缺项」清单，否则 `all` 的结尾会印「本次无数据暂缺项」，
            # 把一次部分失败讲成一次干净的成功。
            gap = (["信号14 资金费率（%s 取不到数）"
                    % "、".join(g["symbol"] for g in f.gaps)] if f.gaps else [])
            if json_mode:
                return BlockResult(out, True, gap, f.to_json())
            f.render_text(out)
            return BlockResult(out, True, gap, None)
    except Exception as exc:
        f.note = _join_note(f.note, _block_failed(out, exc, 14, "永续资金费率"))
    if json_mode:
        return BlockResult(out, False, ["信号14 资金费率"], f.missing_json())
    f.missing_text(out)
    return BlockResult(out, False, ["信号14 资金费率"], None)


# ════════════════════════════ 信号 15 · 清算数据 ════════════════════════════
# 没有免费公开源。这里**实际探测**并回报每个来源的 HTTP 码与判读，
# 让「数据暂缺」带得出证据，而不是一句笼统的「取数失败」。
# 两个探测都要打：其中一个是已知会失败的端点，它回什么正是证据本身。
CG_LIQ_1 = ("https://open-api-v4.coinglass.com/api/futures/liquidation/history"
            "?symbol=BTCUSDT&interval=1d")
CG_LIQ_2 = ("https://fapi.coinglass.com/api/futures/liquidation/info"
            "?symbol=all&timeType=4")


def run_liquidations(out):
    c1, b1 = http_get(CG_LIQ_1, out)
    v1 = _liq_msg(b1)
    c2, b2 = http_get(CG_LIQ_2, out)
    v2 = _liq_has_data(b2)
    return [
        ("open-api-v4.coinglass.com/api/futures/liquidation/history｜HTTP %s" % c1, v1),
        ("fapi.coinglass.com/api/futures/liquidation/info｜HTTP %s" % c2, v2),
    ]


def _liq_msg(body):
    """等价 jq -r '(.msg // .message // "无 msg 栏位")'，jq 失败 → 「回应非 JSON」，
    空输入 → 空字串（jq 对空输入不报错也不印东西）。

    ⚠️ **JSON 的 `null` 在 jq 里是可索引的**：`null | .msg` 回 null（不报错），
    所以 body 是 `null` 时 crypto.sh 印的是「无 msg 栏位」，不是「回应非 JSON」。
    实测（jq 1.7）：`null`/`{}` → 无 msg 栏位；`[]`/`"x"`/`5`/`true`/`false`
    → jq 报错 → 回应非 JSON。这行字是信号 15「数据暂缺」的**全部证据**，
    措辞讲错等于把「对方回了个空壳」记成「对方回的不是 JSON」。"""
    if not body.strip():
        return ""
    v = jparse(body)
    if v is _BAD:
        return "回应非 JSON"
    if v is None:
        return "无 msg 栏位"          # null 可索引：null.msg → null → 兜底字串
    if not isinstance(v, dict):
        return "回应非 JSON"          # 阵列／字串／数字／布林：jq 索引会报错
    m = v.get("msg")
    if m is None or m is False:
        m = v.get("message")
    if m is None or m is False:
        return "无 msg 栏位"
    return jq_r(m)


def _liq_has_data(body):
    """等价 jq -r 'if has("data") …'。同上：`null | has("data")` 在 jq 里
    **不报错**，回 false → 「回 success 但无 data 栏位」（实测 jq 1.7）。"""
    if not body.strip():
        return ""
    v = jparse(body)
    if v is _BAD:
        return "回应非 JSON"
    if v is None:
        return "回 success 但无 data 栏位"
    if not isinstance(v, dict):
        return "回应非 JSON"
    return "有 data 栏位" if "data" in v else "回 success 但无 data 栏位"


def render_liquidations_text(out, attempts, note=""):
    out.p("【信号 15】过去 24h 清算　⚪️ 数据暂缺")
    out.p("  已尝试来源（实测结果）：")
    for src, res in attempts:
        out.p("    · %s → %s" % (src, res))
    if note:
        out.p("  失败细节：%s" % note)
    out.p("  结论：Coinglass v4 需 API key，公开端点不回明细；本项**无免费公开 API**。")
    out.p("  下一步（由上层 agent 执行，不由本脚本假造）：web_search \"coinglass liquidations 24h\"。")
    out.p("  报告要求：24h 总清算 >$500M = 杠杆洗盘｜>$1B = 重大事件；")
    out.p("            **必须注明多头与空头哪一方被清算更多**，并写出上次已知读数与滞后周数。")


def liquidations_json(attempts):
    return _od(
        signal=15, name="24h 清算", status="missing",
        reason="无免费公开 API（Coinglass v4 需 API key）",
        attempted=[_od(source=s, result=r) for s, r in attempts],
        next_step='web_search "coinglass liquidations 24h"',
        report_requirements=["总额 >$500M = 杠杆洗盘；>$1B = 重大事件",
                             "必须注明多头 vs 空头哪一方被清算更多",
                             "必须写出上次已知读数与滞后周数"])


def do_liquidations(json_mode):
    out = Out()
    attempts = []
    note = ""
    try:
        attempts = run_liquidations(out)
    except Exception as exc:
        note = _block_failed(out, exc, 15, "24h 清算")
    # 目前没有任何免费源能回出数字 → 一律 missing（OKCOUNT 不加）。
    if json_mode:
        doc = liquidations_json(attempts)
        if note:
            doc["note"] = note
        return BlockResult(out, False, ["信号15 清算"], doc)
    render_liquidations_text(out, attempts, note)
    return BlockResult(out, False, ["信号15 清算"], None)


# ═══════════════════════════ 信号 16 · BTC Dominance ═══════════════════════
#
# ── 本地 dominance 历史：7d 腿为什么自己累积，而不是换源 ─────────────────
# 免费层拿不到的是「全市场市值**历史序列**」（/global/market_cap_chart 实测
# HTTP 401，需 Pro）；「**当日** dominance」每天都免费取得到。两者是两回事。
# 若改用第三方历史源，就会引入**第三套分母口径**——档头已写明 CoinGecko 与
# CoinPaprika 实测同日 59.1% vs 56.9%，差约 2pt，而 7d 阈值只有 3%：跨源相减
# 出来的「跌幅」多半是分母差，不是真实资金流动。所以正解是按天累积同源历史，
# 7 天后这条腿自己回答，分母口径天然一致（行为准则第 2 条）。
#
# 硬规矩：7 日前那笔的 source 必须与今日相同，否则**拒绝比较**并印 ⚪️；
#         历史不足 7 天也印 ⚪️，**绝不因为其余条件都正常就推断这条腿安全**。
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_EPOCH_DAY0 = datetime.date(1970, 1, 1)


def epochday(s):
    """→ 自 1970-01-01 起的天数；解析不了回 None。
    crypto.sh 这里用 jq 的 strptime/mktime——那两个函式在部分平台缺席，
    整段 jq 失败会被兜底成「历史不足」，把移植性破损伪装成良性状态。
    这支埠把 None（解析不了）与「窗口内没有基准」彻底分开。"""
    if not isinstance(s, str) or not _DATE_RE.match(s):
        return None
    try:
        d = datetime.date(int(s[0:4]), int(s[5:7]), int(s[8:10]))
    except ValueError:
        return None
    return (d - _EPOCH_DAY0).days


def dom_history_load():
    """→ (合法纪录, 读档失败原因或 None)。

    **「档案不存在」与「档案在、但读不到」是两件事，绝不能折成同一个空清单。**
    折在一起的代价有两层，而且第二层会毁资料：
      · 报告上，一个权限错误／坏掉的档会被讲成「尚无历史，明天就好了」——
        把「不知道」记成「查过、没事」（行为准则第 1 条）。
      · 更糟的是 `dom_history_append`：它先读旧纪录、合并、再整档覆写。
        读到空清单就等于「本来就没有历史」，于是**把读不到的那个档整个覆盖掉**，
        几个月的累积一次归零，而且当天的输出看起来完全正常。
    所以读不到一律回报原因，由呼叫端走 ⚪️ 并**放弃写入**。

    逐行校验而不是一口气吃整个档：一行坏掉就整档解析失败的话，会把已经累积好
    的历史整批当成不存在——那比少一行严重得多。"""
    recs = []
    if not os.path.isfile(DOM_HISTORY):
        # 真的没有档 = 第一次跑。这不是错误，也不该当成错误。
        return recs, None
    try:
        with open(DOM_HISTORY, "r", encoding="utf-8", errors="replace") as f:
            lines = f.read().split("\n")
    except OSError as exc:
        # 只取型别与 strerror，不带路径：OSError 的 str() 一定含绝对路径，
        # 而这句会进报告（行为准则第 4 条）。
        reason = getattr(exc, "strerror", None) or scrub(exc)
        return recs, "%s：%s" % (type(exc).__name__, scrub(reason))
    for line in lines:
        if not line:
            continue
        v = jparse(line)
        if v is _BAD or not isinstance(v, dict):
            continue
        if not isinstance(v.get("date"), str):
            continue
        p = v.get("dominance_pct")
        # 与 crypto.sh 的 `(.dominance_pct? | type) == "number"` 一致：数字型别
        # 才收，**数字字串不收**（c-hist-corrupt 钉的就是这一条）。再多一道
        # `_finite`：`1e400` 是合法 JSON 数字但会变成 inf，拿去算 7d 变动会
        # 产出 nan，最后在序列化时丢例外——一行坏纪录不该炸掉整份报告。
        if isinstance(p, bool) or not isinstance(p, (int, float)) or not _finite(p):
            continue
        if not isinstance(v.get("source"), str):
            continue
        recs.append(v)
    return recs, None


_DOM_WRITE_FAIL = ("⚠️ 本地 dominance 历史写入失败（" + DOM_HISTORY_REL +
                   "）：%s。本次取数照常输出，但 7d 腿的基准点未累积。")


def dom_history_append(out, date, dom_lit, source, fetched_at):
    """一天一笔：同日重跑**覆盖**当日那笔（不追加第二笔），再按日期排序、
    只留最新 DOM_HISTORY_MAX 笔。写入走「同目录暂存档 → rename」原子替换，
    中途失败不会留下半截档把既有历史毁掉。
    写失败一律**只告警不中断**：取数照常输出、照常 exit 0（行为准则第 5 条）。"""
    d = os.path.dirname(DOM_HISTORY)
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        out.w(_DOM_WRITE_FAIL % "无法建立所在目录")
        return False

    p = jparse(dom_lit)
    if p is _BAD or isinstance(p, bool) or not isinstance(p, (int, float)):
        out.w(_DOM_WRITE_FAIL % ("无法组出纪录（dominance=%s）" % dom_lit))
        return False
    new = _od(date=date, dominance_pct=p, source=source, fetched_at=fetched_at)

    # **先读、读得成才写。** 读不到就一个字都不写：这个函式的写法是
    # 「读旧纪录 → 合并今日 → 整档覆写」，读到空清单跟「本来就没有历史」
    # 长得一模一样，于是一个权限错误／坏掉的档会被今天这一笔**整个盖掉**，
    # 几个月的累积一次归零。读不到的档一律留在原地（行为准则第 1 条）。
    old_recs, rerr = dom_history_load()
    if rerr is not None:
        out.w("⚠️ 本地 dominance 历史读取失败（%s）：%s。"
              "**本次不写入**——读不到内容就覆写会毁掉既有累积；"
              "7d 腿本次记 ⚪️，不得当成未触发。" % (DOM_HISTORY_REL, rerr))
        return False

    try:
        fd, tmp = tempfile.mkstemp(prefix=".dominance_history.", dir=d)
    except OSError:
        out.w(_DOM_WRITE_FAIL % "所在目录不可写")
        return False

    try:
        recs = [r for r in old_recs if r.get("date") != date]
        recs.append(new)
        recs.sort(key=lambda r: r["date"])
        if len(recs) > DOM_HISTORY_MAX:
            recs = recs[len(recs) - DOM_HISTORY_MAX:]
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for r in recs:
                f.write(jdumpc(r) + "\n")
        os.replace(tmp, DOM_HISTORY)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        out.w(_DOM_WRITE_FAIL % "档案不可写")
        return False
    try:
        # mkstemp 给的是 0600；这个档会被 commit 进仓库，跟其它状态档保持一致。
        os.chmod(DOM_HISTORY, 0o644)
    except OSError:
        pass
    return True


def dom_seven_day(today, dom_lit, src):
    """基准取「年龄落在 [6,10] 天、且最接近 7 天」的那一笔。
    下限 6：不拿三四天前的读数冒充 7 日变动。
    上限 10：漏跑几天后最接近的一笔可能是 31 天前 —— 那是「31 日变动」，
             贴上「7d 变动」的标签就是编数字。窗口外一律 ⚪️ 断层。
    排序键 |age-7| 优先、其次取较新的一笔，纯粹为了结果可重现。"""
    amin, amax = DOM_7D_MIN_AGE, DOM_7D_MAX_AGE
    allrec, rerr = dom_history_load()
    if rerr is not None:
        # 「档案在、但读不到」既不是「历史不足」也不是「断层」：那两个都在说
        # 「累积还不够，再跑几天就好」，而这里要修的是档案本身。讲错了，没有人
        # 会去修它，而 7d 腿会永远 ⚪️ 下去还以为快好了。
        return _od(history_days=None, history_since=None, history_until=None,
                   today_source=src, base_age_window_days=[amin, amax],
                   status="unreadable",
                   reason=("本地历史档存在但读不到（%s：%s），7d 变动无法判定"
                           "（不得视为未触发，也不是「历史不足」）"
                           % (DOM_HISTORY_REL, rerr)),
                   history_error=rerr)
    days = sorted(set(r["date"] for r in allrec))
    base = _od(history_days=len(days),
               history_since=(days[0] if days else None),
               history_until=(days[-1] if days else None),
               today_source=src,
               base_age_window_days=[amin, amax])

    t0 = epochday(today)
    bad = [d for d in days if epochday(d) is None]
    if t0 is None or bad:
        # 「日期解析不了」**不是**「历史不足」。crypto.sh 会把这两者混成同一个
        # 状态，那正是它掩盖 jq strptime 移植性破损的方式。
        base["status"] = "unparseable"
        base["reason"] = ("本地历史含无法解析的日期（%s），7d 变动无法判定"
                          "（不得视为未触发，也不是「历史不足」）"
                          % ("、".join(bad) if bad else today))
        base["unparseable_dates"] = bad if bad else [today]
        return base

    now_v = as_num(dom_lit)
    if now_v is _NUM_BAD:
        # 呼叫端已经验过一次；这里是第二道防线，而且它必须有自己的状态：
        # 掉进「历史不足」会把「今天这个读数不能用」讲成「历史累积不够」。
        base["status"] = "unparseable"
        base["reason"] = ("今日 dominance 读数不是可用数值（%s），7d 变动无法判定"
                          "（不得视为未触发，也不是「历史不足」）" % _brief(dom_lit))
        base["unparseable_dates"] = []
        return base
    now = float(now_v)
    prev = []
    for r in allrec:
        if r["date"] == today:
            continue
        rr = dict(r)
        rr["age"] = t0 - epochday(r["date"])
        prev.append(rr)
    inwin = [r for r in prev if amin <= r["age"] <= amax]
    # 同源优先：先在窗口内找与今日同源的，找不到才退回任意源（那时必然报
    # source_mismatch）。若只按年龄挑，窗口内「7天前·异源」会盖掉
    # 「8天前·同源」，明明有可用的同源基准却被判成不可比——白白丢掉一次
    # 本可成立的判定。
    same = sorted([r for r in inwin if r.get("source") == src],
                  key=lambda r: (abs(r["age"] - 7), r["age"]))
    anyw = sorted(inwin, key=lambda r: (abs(r["age"] - 7), r["age"]))
    b = same[0] if same else (anyw[0] if anyw else None)
    near = sorted([r for r in prev if r["age"] >= amin], key=lambda r: r["age"])
    nearest = near[0] if near else None

    if b is not None and b.get("source") != src:
        base["status"] = "source_mismatch"
        base["reason"] = "历史记录来自不同数据源，分母口径不同不可比较"
        base["base_date"] = b["date"]
        base["base_age_days"] = b["age"]
        base["base_source"] = b["source"]
        base["base_dominance_pct"] = b["dominance_pct"]
    elif b is not None:
        bd = float(b["dominance_pct"])
        dpt = now - bd
        # 基准 dominance 是 0（档案被写坏）→ 相对变动没有定义。记 N/A，
        # 不记 0：`triggered:false` 会把一个不可判定的腿写成「查过、没事」。
        drel = (dpt / bd * 100) if bd != 0 else None
        if drel is not None and not _finite(drel):
            drel = None
        base["status"] = "ok"
        base["base_date"] = b["date"]
        base["base_age_days"] = b["age"]
        base["base_source"] = b["source"]
        base["base_dominance_pct"] = b["dominance_pct"]
        base["delta_pt"] = dpt if _finite(dpt) else None
        base["delta_rel_pct"] = drel
        base["triggered"] = (drel < (0 - float(DOM_DROP_7D))
                             if drel is not None else None)
    elif nearest is not None:
        base["status"] = "gap"
        base["reason"] = ("本地历史有断层：最接近的一笔落在 %d–%d 天的可用窗口之外"
                          % (amin, amax))
        base["nearest_date"] = nearest["date"]
        base["nearest_age_days"] = nearest["age"]
        base["nearest_source"] = nearest["source"]
    else:
        base["status"] = "insufficient"
        base["reason"] = "本地同源历史不足 7 天，7d 变动无法判定（不得视为未触发）"
    return base


class Dominance(object):
    def __init__(self):
        self.status = "pending"
        self.data = None
        self.seven = _od(status="insufficient", history_days=0, history_since=None)

    def run(self, out):
        code, body = http_get(COINGECKO + "/global", out)
        g = jparse(body)
        raw_btc = None
        btc = _NUM_BAD
        if code == "200" and ok_json(g):
            raw_btc = _dig(g, "data", "market_cap_percentage", "btc")
            btc = as_num(raw_btc)
        if btc is not _NUM_BAD:
            code2, body2 = http_get(
                COINGECKO + "/coins/markets?vs_currency=usd&ids=bitcoin", out)
            b = jparse(body2)
            if code2 == "200" and ok_json(b):
                # 四个输入都要先验数字。**任何一个不是数字就不回推**——
                # 回推公式里塞一个 0（awk 对非数字的做法）会算出一个看起来
                # 完全正常的 24h 变动，而它是凭空生出来的。
                tot = as_num(_dig(g, "data", "total_market_cap", "usd"))
                tchg = as_num(_dig(g, "data", "market_cap_change_percentage_24h_usd"))
                bmc = as_num(_idx(b, 0, "market_cap"))
                bchg = as_num(_idx(b, 0, "market_cap_change_percentage_24h"))
                prev = None
                if _NUM_BAD not in (tot, tchg, bmc, bchg):
                    try:
                        # 24h 前的 dominance 由「市值」变动率回推（两个变动率
                        # 都是市值口径，不是价格口径）：
                        #   dom_24h_ago = [btc_mc/(1+b)] / [total_mc/(1+t)] × 100
                        # 这是从真实数据推导，不是估计；供应量 24h 变动
                        # <0.01%，可忽略。
                        prev = ((float(bmc) / (1 + float(bchg) / 100.0))
                                / (float(tot) / (1 + float(tchg) / 100.0)) * 100)
                    except (TypeError, ValueError, ZeroDivisionError,
                            OverflowError):
                        prev = None
                    if prev is not None and (not _finite(prev) or prev == 0):
                        prev = None     # 回推出 0 / inf → 分母不可用，记 N/A
                if prev is not None:
                    dpt = float(btc) - prev
                    drel = dpt / prev * 100
                    self.data = _od(
                        source="CoinGecko /global + /coins/markets",
                        source_key="coingecko",
                        dominance_pct=btc, dominance_24h_ago_pct=prev,
                        delta_pt=(dpt if _finite(dpt) else None),
                        delta_rel_pct=(drel if _finite(drel) else None),
                        total_mcap_usd=tot, btc_mcap_usd=bmc,
                        total_mcap_change_24h_pct=tchg,
                        btc_mcap_change_24h_pct=bchg)
                    self.status = "ok"
                    return True
                out.w("⚠️ CoinGecko 回推 24h 前 dominance 所需的市值/变动率栏位"
                      "不可用（total_market_cap.usd=%s；"
                      "market_cap_change_percentage_24h_usd=%s；market_cap=%s；"
                      "market_cap_change_percentage_24h=%s），24h 腿记 ⚪️。"
                      % (_brief(_dig(g, "data", "total_market_cap", "usd")),
                         _brief(_dig(g, "data",
                                     "market_cap_change_percentage_24h_usd")),
                         _brief(_idx(b, 0, "market_cap")),
                         _brief(_idx(b, 0, "market_cap_change_percentage_24h"))))
            self.data = _od(
                source="CoinGecko /global", source_key="coingecko",
                dominance_pct=btc, dominance_24h_ago_pct=None,
                delta_pt=None, delta_rel_pct=None,
                total_mcap_usd=_num_or_null(
                    _dig(g, "data", "total_market_cap", "usd")),
                btc_mcap_usd=None,
                total_mcap_change_24h_pct=_num_or_null(_dig(
                    g, "data", "market_cap_change_percentage_24h_usd")),
                btc_mcap_change_24h_pct=None)
            self.status = "partial"
            return True

        if _present(raw_btc):
            # HTTP 200、栏位也在，但值不是数字。这是**换源的理由**，而且必须
            # 说出来：底下那句「回 HTTP 200」单看会读成「取到数了却还是换源」。
            out.w("⚠️ CoinGecko /global 的 data.market_cap_percentage.btc 不是"
                  "可用数值（%s），本次不采用该读数。" % _brief(raw_btc))
        elif code == "200":
            # 200 + 用不了的 payload 是这一层**最安静**的一种失败：底下那句
            # 「回 HTTP 200，改用备援源」单看像是「取到数了却莫名换源」，
            # 缺陷本身一个字都没有。缺陷要跟来源一起写出来（THE ONE RULE）。
            out.w("⚠️ CoinGecko /global 回 HTTP 200，但%s，本次不采用该读数。"
                  % ("回应不是可解析的 JSON" if not ok_json(g)
                     else "回应缺 data.market_cap_percentage.btc"
                          "（栏位不存在或为 null）"))
        out.w("⚠️ CoinGecko /global 回 HTTP %s，改用备援源 CoinPaprika。" % code)
        code, body = http_get(COINPAPRIKA + "/global", out)
        p = jparse(body)
        raw_pd = None
        pd = _NUM_BAD
        if code == "200" and ok_json(p):
            raw_pd = _dig(p, "bitcoin_dominance_percentage")
            pd = as_num(raw_pd)
        if pd is not _NUM_BAD:
            self.data = _od(
                source="CoinPaprika /global（⚠️ 与 CoinGecko 口径不同，分母不同，"
                       "实测同日可差 2pt 以上——不可与前日 CoinGecko 读数比较）",
                source_key="coinpaprika",
                dominance_pct=pd, dominance_24h_ago_pct=None,
                delta_pt=None, delta_rel_pct=None,
                total_mcap_usd=_num_or_null(_dig(p, "market_cap_usd")),
                btc_mcap_usd=None,
                total_mcap_change_24h_pct=_num_or_null(
                    _dig(p, "market_cap_change_24h")),
                btc_mcap_change_24h_pct=None)
            self.status = "partial"
            return True
        if _present(raw_pd):
            out.w("⚠️ CoinPaprika /global 的 bitcoin_dominance_percentage 不是"
                  "可用数值（%s），本次不采用该读数。" % _brief(raw_pd))
        elif code == "200":
            # 备援源回 200 却给不出可用读数时，**这一层原本一个字都不印**：
            # 两源皆失败的 ⚪️ 底下只看得到 CoinGecko 的 HTTP 码，
            # CoinPaprika 那一层像是没跑过。至少把 200 + 坏 payload 说清楚。
            out.w("⚠️ CoinPaprika /global 回 HTTP 200，但%s，本次不采用该读数；"
                  "两个来源皆未取到可用 dominance。"
                  % ("回应不是可解析的 JSON" if not ok_json(p)
                     else "回应缺 bitcoin_dominance_percentage"
                          "（栏位不存在或为 null）"))
        self.status = "missing"
        return False

    def record_and_seven(self, out):
        """取数成功后：先落一笔历史，再据历史算 7d。日期一律 UTC，与纪录里的一致。"""
        today = fx_date_u("%Y-%m-%d")
        ts = fx_date_u("%Y-%m-%dT%H:%M:%SZ")
        dom = jq_r_or_empty(self.data.get("dominance_pct"))
        src = jq_r_or_empty(self.data.get("source_key"))
        if dom == "" or src == "":
            out.w("⚠️ 本次 dominance 读数缺 dominance_pct / source_key，跳过历史累积。")
            return
        dom_history_append(out, today, dom, src, ts)
        try:
            self.seven = dom_seven_day(today, dom, src)
        except Exception as e:
            # status 必须与 reason 说同一件事：读不动 ≠ 历史不足。
            # 记 insufficient 会把「档案坏了/权限不对」伪装成良性的「再攒几天就好」，
            # 而後者不需要人管、前者需要——这正是本埠新增 `unreadable` 的原因。
            self.seven = _od(status="unreadable", history_days=None,
                             history_since=None,
                             reason="历史档读取失败：%s" % scrub(e))

    def render_text(self, out):
        d = self.data
        src = jq_r(d["source"])
        dom = jq_r(d["dominance_pct"])
        prev = d["dominance_24h_ago_pct"]
        dpt = d["delta_pt"]
        drel = d["delta_rel_pct"]
        prev_s = "null" if prev is None or prev is False else jq_r(prev)
        dpt_s = "null" if dpt is None or dpt is False else jq_r(dpt)
        drel_s = "null" if drel is None or drel is False else jq_r(drel)

        out.p("【信号 16】BTC Dominance　来源：%s" % src)
        out.p("  当前 dominance     %s%%" % num_na(dom, 2))
        if prev_s == "null" or not is_num(prev_s):
            out.p("  24h 前             ⚪️ 数据暂缺（备援源未提供可回推的市值变动率）")
            out.p("  24h 变动           ⚪️ 无法判定 → 本项不得填数字")
        else:
            out.p("  24h 前（回推）     %s%%" % num_na(prev_s, 2))
            out.p("  24h 变动           %s pt（相对 %s%%）　正=上升／负=下降"
                  % (snum_na(dpt_s, 2), snum_na(drel_s, 2)))

        # 7d 变动：由 assets/dominance_history.jsonl 里**同源**的历史自答。
        sev = self.seven
        st7 = jq_r(sev.get("status"))
        hd = jq_r(sev.get("history_days"))
        hs = sev.get("history_since")
        hs = "—" if hs is None or hs is False else jq_r(hs)
        bdate = ""
        d7rel = None
        d7pt = None
        if st7 == "ok":
            bdate = jq_r(sev["base_date"])
            bage = jq_r(sev["base_age_days"])
            bsrc = jq_r(sev["base_source"])
            bdom = jq_r(sev["base_dominance_pct"])
            d7pt = jq_r(sev["delta_pt"])
            d7rel = jq_r(sev["delta_rel_pct"])
            out.p("  7d 前（%s，%s 天前）  %s%%　来源 %s（与今日同源）"
                  % (bdate, bage, num_na(bdom, 2), bsrc))
            out.p("  7d 变动            %s pt（相对 %s%%）　正=上升／负=下降"
                  % (snum_na(d7pt, 2), snum_na(d7rel, 2)))
            out.p("     基准：本地历史 %s（自 %s 起累积，已 %s 天）"
                  % (DOM_HISTORY_REL, hs, hd))
        elif st7 == "source_mismatch":
            bdate = jq_r(sev["base_date"])
            bsrc = jq_r(sev["base_source"])
            out.p("  7d 变动            ⚪️ 无法判定 → 本项不得填数字")
            out.p("     原因：历史记录来自不同数据源（今日 %s vs 7日前 %s @ %s），"
                  "分母口径不同不可比较。" % (jq_r(d["source_key"]), bsrc, bdate))
            out.p("           口径红线：不同源的 dominance 分母不同"
                  "（CoinGecko 与 CoinPaprika 实测同日可差 2pt 以上，")
            out.p("           而 7d 阈值只有 %s%%），跨源相减出来的「跌幅」"
                  "多半是分母差，不是真实资金流动。" % DOM_DROP_7D)
            out.p("           等 %s 累积满 7 天同源读数后，本项自动恢复。" % DOM_HISTORY_REL)
        elif st7 == "gap":
            ndate = jq_r(sev["nearest_date"])
            nage = jq_r(sev["nearest_age_days"])
            out.p("  7d 变动            ⚪️ 无法判定 → 本项不得填数字")
            out.p("     原因：本地历史有断层——最接近的一笔是 %s（%s 天前），"
                  "落在 %d–%d 天的可用窗口之外。"
                  % (ndate, nage, DOM_7D_MIN_AGE, DOM_7D_MAX_AGE))
            out.p("           拿 %s 天前的读数算出来的是「%s 日变动」，"
                  "贴上「7d 变动」的标签就是编数字。" % (nage, nage))
            out.p("           连跑几天补上 %s 就会自动恢复。" % DOM_HISTORY_REL)
        elif st7 == "unreadable":
            out.p("  7d 变动            ⚪️ 无法判定 → 本项不得填数字")
            out.p("     原因：%s 存在但读不到（%s）——这是**档案／权限损坏**，"
                  "不是「历史不足」。"
                  % (DOM_HISTORY_REL, jq_r(sev.get("history_error"))))
            out.p("           本次**没有写入**该档（读不到内容就覆写会毁掉既有累积）；")
            out.p("           修好读取权限后本项自动恢复。")
        elif st7 == "unparseable":
            # crypto.sh 没有这个状态：它会把日期解析失败伪装成「历史不足」。
            bad = "、".join(sev.get("unparseable_dates") or [])
            out.p("  7d 变动            ⚪️ 无法判定 → 本项不得填数字")
            out.p("     原因：本地历史里有无法解析的日期（%s）——这是**档案损坏**，"
                  "不是「历史不足」。" % bad)
            out.p("           修好 %s 里那几行（或删掉它们）后本项自动恢复。"
                  % DOM_HISTORY_REL)
        else:
            out.p("  7d 变动            ⚪️ 历史不足（已累积 %s 天，7d 判定需 ≥7 天）" % hd)
            out.p("     本地历史自 %s 起累积（%s）；每天跑一次 dominance 就会自己补齐。"
                  % (hs, DOM_HISTORY_REL))
            out.p("     注：缺的只是「历史序列」，当日 dominance 本身取得到——所以这里不换源")
            out.p("         （换源会引入第三套分母口径），而是等同源历史累积够。")

        out.p()
        out.p("阈值判定（信号 16：24h 跌幅 >2% 或 7d 跌幅 >3% = 山寨狂热期）：")
        # 非数字也一律 ⚪️：先验数字再比大小，绝不凭空造一个警报出来。
        if drel_s == "null" or not is_num(drel_s):
            out.p("  24h 跌幅 >%s%% ... ⚪️ 无法判定" % DOM_DROP_24H)
        else:
            # 这里印的是**带号的变动量**（正=上升、负=下降），不是「跌幅」的
            # 绝对值。在「跌幅」标签底下印无号数字，涨 1.5% 会被读成跌 1.5%，
            # 方向刚好相反。
            if float(drel_s) < -float(DOM_DROP_24H):
                out.p("  24h 跌幅 >%s%% ... ✅ 触发（24h 变动 相对 %s%%）"
                      % (DOM_DROP_24H, snum_na(drel_s, 2)))
            else:
                out.p("  24h 跌幅 >%s%% ... ❌ 未触发（24h 变动 相对 %s%%，正=上升／负=下降）"
                      % (DOM_DROP_24H, snum_na(drel_s, 2)))
            out.p("     ⚠️ 口径提示：上面按**相对百分比**判定。若报告采用**百分点**口径，")
            out.p("        请改用 Δpt = %s pt 自行判定——两种口径结论可能不同，别混用。"
                  % snum_na(dpt_s, 2))

        if st7 == "ok":
            # 与 24h 同一套规则：阈值按**相对百分比**判定，Δpt 另印一份供
            # 百分点口径使用。
            if not is_num(d7rel):
                out.p("  7d 跌幅 >%s%% .... ⚪️ 无法判定（7d 变动读数解析失败）" % DOM_DROP_7D)
                out.p("     **不得因为其余条件都正常就把这条腿写成「未触发」**"
                      "——那会让加密信号触发计数偏低。")
            elif float(d7rel) < -float(DOM_DROP_7D):
                out.p("  7d 跌幅 >%s%% .... ✅ 触发（7d 变动 相对 %s%%，基准日 %s）"
                      % (DOM_DROP_7D, snum_na(d7rel, 2), bdate))
            else:
                out.p("  7d 跌幅 >%s%% .... ❌ 未触发（7d 变动 相对 %s%%，正=上升／负=下降）"
                      % (DOM_DROP_7D, snum_na(d7rel, 2)))
            # Δpt 也一样：读不出来就别印。snum("") 会印出 "+0.00"，
            # 那是个看起来完全正常、实际上凭空生出来的数字。
            if is_num(d7pt):
                out.p("     ⚠️ 口径提示：上面按**相对百分比**判定。若报告采用**百分点**口径，")
                out.p("        请改用 Δpt = %s pt 自行判定——两种口径结论可能不同，别混用。"
                      % snum_na(d7pt, 2))
        elif st7 == "source_mismatch":
            out.p("  7d 跌幅 >%s%% .... ⚪️ 无法判定（7 日前那笔与今日不同源，"
                  "分母口径不可比较）" % DOM_DROP_7D)
        elif st7 == "gap":
            out.p("  7d 跌幅 >%s%% .... ⚪️ 无法判定（本地历史断层，%d–%d 天窗口内没有基准）"
                  % (DOM_DROP_7D, DOM_7D_MIN_AGE, DOM_7D_MAX_AGE))
            out.p("     **不得因为其余条件都正常就把这条腿写成「未触发」**"
                  "——那会让加密信号触发计数偏低。")
        elif st7 == "unreadable":
            out.p("  7d 跌幅 >%s%% .... ⚪️ 无法判定（本地历史档读不到，档案／权限损坏）"
                  % DOM_DROP_7D)
            out.p("     **不得因为其余条件都正常就把这条腿写成「未触发」**"
                  "——那会让加密信号触发计数偏低。")
        elif st7 == "unparseable":
            out.p("  7d 跌幅 >%s%% .... ⚪️ 无法判定（本地历史日期无法解析，档案损坏）"
                  % DOM_DROP_7D)
            out.p("     **不得因为其余条件都正常就把这条腿写成「未触发」**"
                  "——那会让加密信号触发计数偏低。")
        else:
            out.p("  7d 跌幅 >%s%% .... ⚪️ 无法判定（本地历史仅 %s 天，未满 7 天）"
                  % (DOM_DROP_7D, hd))
            out.p("     **不得因为其余条件都正常就把这条腿写成「未触发」**"
                  "——那会让加密信号触发计数偏低。")

    def to_json(self):
        o = dict(self.data)
        o["signal"] = 16
        o["name"] = "BTC Dominance"
        o["status"] = self.status
        o["thresholds"] = _od(drop_24h_rel_pct=_lit(DOM_DROP_24H),
                              drop_7d_rel_pct=_lit(DOM_DROP_7D))
        sev = dict(self.seven)
        sev["history_file"] = DOM_HISTORY_REL
        sev["history_retention_max"] = DOM_HISTORY_MAX
        o["seven_day"] = sev
        o["caliber_note"] = ("24h 与 7d 判定都用相对百分比口径；delta_pt 为百分点口径，"
                             "两者不可混用。7d 基准取自本地同源历史，源不同一律拒绝比较")
        return o

    @staticmethod
    def missing_json(note=""):
        o = _od(signal=16, name="BTC Dominance", status="missing",
                attempted=["CoinGecko /global", "CoinPaprika /global"],
                next_step="web_fetch coingecko / tradingview BTC.D；标 ⚪️ + 报滞后周数")
        if note:                    # 只有真的有细节才多这个栏位
            o["note"] = note
        return o

    @staticmethod
    def missing_text(out, note=""):
        out.p("【信号 16】BTC Dominance　⚪️ 数据暂缺")
        out.p("  已尝试来源：CoinGecko /global → CoinPaprika /global（皆失败）")
        if note:
            out.p("  失败细节：%s" % note)
        out.p("  下一步：web_fetch coingecko / tradingview 的 BTC.D；标 ⚪️ + "
              "写出上次已知读数与滞后周数。")


def _dig(o, *keys):
    for k in keys:
        if not isinstance(o, dict):
            return None
        o = o.get(k)
    return o


def _idx(o, i, key):
    if not isinstance(o, list) or len(o) <= i or not isinstance(o[i], dict):
        return None
    return o[i].get(key)


def do_dominance(json_mode):
    out = Out()
    d = Dominance()
    note = ""
    try:
        if d.run(out):
            d.record_and_seven(out)
            missing = []
            if d.status != "ok":
                missing.append("信号16 dominance 24h 变动")
            if jq_r(d.seven.get("status")) != "ok":
                missing.append("信号16 dominance 7d 变动")
            if json_mode:
                return BlockResult(out, True, missing, d.to_json())
            d.render_text(out)
            return BlockResult(out, True, missing, None)
    except Exception as exc:
        note = _block_failed(out, exc, 16, "BTC Dominance")
    if json_mode:
        return BlockResult(out, False, ["信号16 BTC Dominance"],
                           d.missing_json(note))
    d.missing_text(out, note)
    return BlockResult(out, False, ["信号16 BTC Dominance"], None)


def render_dom_history(out, json_mode):
    loaded, rerr = dom_history_load()
    if json_mode:
        if rerr is not None:
            # 形状与正常那份一致，只是 count / records 换成 null 并多两个栏位：
            # 「读不到」绝不可写成 count 0 / records []（那是「档案是空的」）。
            out.p(jdumps(_od(signal=16, name="BTC Dominance 本地历史",
                             file=DOM_HISTORY_REL,
                             retention_max=DOM_HISTORY_MAX,
                             count=None, records=None, status="unreadable",
                             error=rerr,
                             note=("本地历史档存在但读不到；这不是「尚无历史」，"
                                   "7d 腿一律 ⚪️，且本脚本不会覆写它"))))
            return
        recs = sorted(loaded, key=lambda r: r["date"])
        out.p(jdumps(_od(signal=16, name="BTC Dominance 本地历史",
                         file=DOM_HISTORY_REL, retention_max=DOM_HISTORY_MAX,
                         count=len(recs), records=recs)))
        return
    out.p("【信号 16】BTC Dominance 本地历史　档案：%s（保留上限 %d 笔，日期为 UTC）"
          % (DOM_HISTORY_REL, DOM_HISTORY_MAX))
    if not os.path.isfile(DOM_HISTORY):
        out.p("  ⚪️ 尚无本地历史——「dominance」第一次成功取数后才会建立（这不是安装缺档）。")
        out.p("  在那之前 7d 腿一律 ⚪️「历史不足」，**不得当成未触发**。")
        return
    if rerr is not None:
        # 档案在、读不到。**绝不印一张空表加「合计 0 天」** —— 那跟「尚无历史」
        # 长得一模一样，读的人会以为再跑几天就好，而真正该做的是修权限／修档。
        out.p("  ⚪️ 档案存在但读不到（%s）——这不是「尚无历史」，是**档案／权限损坏**。" % rerr)
        out.p("  已累积几天、来源分布一律**无法判定**；7d 腿一律 ⚪️，不得当成未触发。")
        out.p("  在修好之前，dominance 取数仍会照常输出，但**不会写入本档**"
              "（读不到内容就覆写会毁掉既有累积）。")
        return
    # 表头用字面量对齐：printf 的 %-12s 数的是**位元组**，中文字一个 3 位元组、
    # 显示宽度却是 2 —— 拿 %-12s 排中文表头，栏位一定歪。
    out.p("  日期(UTC)     dominance%  来源           取数时间(UTC)")
    recs = sorted(loaded, key=lambda r: r["date"])
    for r in recs:
        fa = r.get("fetched_at")
        fa = "—" if fa is None or fa is False else jq_r(fa)
        out.p("  %s %s  %s %s" % (pad(r["date"], 12, True),
                                  pad("%.2f" % float(r["dominance_pct"]), 11),
                                  pad(jq_r(r["source"]), 14, True), fa))
    days = sorted(set(r["date"] for r in recs))
    groups = {}
    for r in recs:
        groups.setdefault(jq_r(r["source"]), 0)
        groups[jq_r(r["source"])] += 1
    dist = "、".join("%s×%d" % (k, groups[k]) for k in sorted(groups))
    out.p("  合计 %d 天（%s … %s）；来源分布：%s"
          % (len(days), days[0] if days else "—", days[-1] if days else "—", dist))

    today = fx_date_u("%Y-%m-%d")
    t0 = epochday(today)
    bad = [d for d in days if epochday(d) is None]
    if t0 is None or bad:
        # 与 7d 腿同一条规矩：「日期解析不了」不得伪装成「历史不足」。
        out.p("  7d 腿：⚪️ 无法判定——本地历史含无法解析的日期（%s），"
              "这是档案损坏，不是历史不足；不得当成未触发。"
              % ("、".join(bad) if bad else today))
        return
    win = [t0 - epochday(r["date"]) for r in recs if r["date"] != today]
    win = [a for a in win if DOM_7D_MIN_AGE <= a <= DOM_7D_MAX_AGE]
    if win:
        out.p("  7d 腿：以今日（%s）为准已具备判定条件（仍须该笔与今日同源，否则拒绝比较）。"
              % today)
    elif len(days) >= 7:
        out.p("  7d 腿：⚪️ 以今日（%s）为准，%d–%d 天窗口内没有基准（历史断层）；"
              "不得当成未触发。" % (today, DOM_7D_MIN_AGE, DOM_7D_MAX_AGE))
    else:
        out.p("  7d 腿：⚪️ 历史不足（已累积 %d 天，7d 判定需 ≥7 天）；不得当成未触发。"
              % len(days))


# ═══════════════════════ 信号 17 · 稳定币总供应（USDT+USDC）══════════════════
class Stablecoins(object):
    def __init__(self):
        self.series = []
        self.note = ""

    def run(self, out):
        code, body = http_get(LLAMA_STABLE + "/stablecoins", out)
        if code != "200" or not ok_json(jparse(body)):
            self.note = "DeFiLlama /stablecoins 回 HTTP %s" % code
            return False
        c1, b1 = http_get(LLAMA_STABLE + "/stablecoincharts/all?stablecoin=1", out)
        c2, b2 = http_get(LLAMA_STABLE + "/stablecoincharts/all?stablecoin=2", out)
        t, c = jparse(b1), jparse(b2)
        if c1 != "200" or c2 != "200" or not ok_json(t) or not ok_json(c):
            self.note = ("DeFiLlama /stablecoincharts 回 HTTP %s/%s（USDT/USDC）"
                         % (c1, c2))
            return False

        # 两条序列按 date 对齐后相加（日期不一致时只取交集，避免拿两个不同日的
        # 数相加）。对齐失败**一律当取数失败**：一次全灭的取数被记成成功，
        # 报告上会印出 USDT $0.00 B 这种凭空生出来的数字。
        T = _llama_pairs(t)
        C = _llama_pairs(c)
        if T is None or C is None:
            self.note = ("DeFiLlama 两条序列对齐失败（回应结构非预期，"
                         "无法取得对齐后天数）")
            return False
        cm = dict((k, v) for k, _d, v in C)      # 值可能是 None（该日 N/A）
        series = []
        for k, d, v in T:
            if k not in cm:
                continue                          # 日期无交集 —— 与 crypto.sh 一致
            u = cm[k]
            try:
                day = datetime.datetime.fromtimestamp(
                    float(d), datetime.timezone.utc).strftime("%Y-%m-%d")
            except (ValueError, OverflowError, OSError):
                self.note = ("DeFiLlama 两条序列对齐失败（date 栏位 %s 不是可用的"
                             "秒级时间戳）" % _brief(d))
                return False
            # **两边都有数字才相加**。crypto.sh（与埠的旧版）用 jq 的 `+`
            # 语义，null 是加法单位元 —— 于是 null + 1000 = 1000，一个缺失值
            # 被悄悄换成「另一边那个数」，正是「用邻近栏位推」这条红线。
            tot = (v + u) if (v is not None and u is not None) else None
            series.append(_od(date=day, usdt=v, usdc=u, total=tot))
        series.sort(key=lambda r: r["date"])

        n = len(series)
        if n < 2:
            self.note = "USDT / USDC 两条序列的日期无交集（只对齐到 %d 天）" % n
            return False
        na = [r["date"] for r in series if r["total"] is None]
        if na:
            # 大声降级：少了几天的数字不该悄悄变成 $0.00 B，也不该让整条序列
            # 消失（基准日会跟着悄悄换掉）。那几天记 N/A，并在这里点名。
            out.w("⚠️ DeFiLlama 序列中有 %d 天的 totalCirculating.peggedUSD 不是"
                  "可用数值（最早 %s，最新 %s），那几天的 USDT/USDC/合计一律记 "
                  "N/A，不得当成 0。" % (len(na), na[0], na[-1]))
            self.note = ("序列中有 %d 天的 totalCirculating.peggedUSD 不可用"
                         "（最早 %s，最新 %s），该几日记 N/A" % (len(na), na[0], na[-1]))
        self.series = series
        return True

    def metrics(self):
        s = self.series
        n = len(s)
        last = s[-1]
        d1 = s[-2] if n >= 2 else None
        d7 = s[-8] if n >= 8 else None
        d14 = s[-15] if n >= 15 else None
        dayout = float(STABLE_DAY_OUT_USD)
        # 每个差值都可能是 None：基准日不存在（序列太短），或对齐日的
        # peggedUSD 是 N/A。两种情况都是「无法判定」，不是「未触发」。
        c1 = _sub(last["total"], d1["total"]) if d1 else None
        c7 = _sub(last["total"], d7["total"]) if d7 else None
        c14 = _sub(last["total"], d14["total"]) if d14 else None
        cout = _sub(d1["total"], last["total"]) if d1 else None
        return _od(
            asof=last["date"], usdt=last["usdt"], usdc=last["usdc"],
            total=last["total"],
            change_1d=c1, change_7d=c7, change_14d=c14,
            base_1d=(d1["date"] if d1 else None),
            base_7d=(d7["date"] if d7 else None),
            base_14d=(d14["date"] if d14 else None),
            triggers=_od(
                net_outflow_7d=((c7 < 0) if c7 is not None else None),
                midterm_flat_or_shrink_14d=((c14 <= 0) if c14 is not None else None),
                daily_outflow_gt_1b=((cout > dayout) if cout is not None else None)))

    def render_text(self, out):
        m = self.metrics()

        def flow(v, base, short_reason):
            """净流入/出这一格：算得出来才带货币符号与单位。
            读不出来时**整格**换成 ⚪️ 标记 —— 旧版只把数字换成「—」、
            外面照样包上 `$` 与 ` B`，印出来是 `$— B（基准日 —）`：一个
            带货币符号与单位的空洞，扫过去很像「零净流入」。缺口要长得像
            缺口（行为准则第 1 条）。"""
            if v is None:
                if not base:
                    return "⚪️ 无法判定（%s）" % short_reason
                return ("⚪️ 无法判定（基准日 %s 的 peggedUSD 为 N/A，"
                        "不得当成 0）" % base)
            return "$%+.2f B（基准日 %s）" % (float(v) / 1e9, base)

        def bb(v, field):
            """N/A 一律印 ⚪️ 并点名坏在哪个栏位。
            `$0.00 B` 是这支脚本历史上最贵的一个假数字：它看起来完全正常。"""
            if v is None:
                return "⚪️ 数据暂缺（DeFiLlama %s 非数值，不得当成 0）" % field
            return "$%.2f B" % (float(v) / 1e9)

        out.p("【信号 17】稳定币总供应（USDT + USDC）　来源：DeFiLlama stablecoins.llama.fi")
        out.p("  as of              %s" % jq_r(m["asof"]))
        out.p("  USDT               %s" % bb(m["usdt"], "USDT totalCirculating.peggedUSD"))
        out.p("  USDC               %s" % bb(m["usdc"], "USDC totalCirculating.peggedUSD"))
        out.p("  合计               %s" % bb(m["total"], "USDT/USDC totalCirculating.peggedUSD"))
        out.p()
        out.p("  1 日净流入/出      %s"
              % flow(m["change_1d"], m["base_1d"], "序列不足 2 天"))
        out.p("  7 日净流入/出      %s"
              % flow(m["change_7d"], m["base_7d"], "序列不足 8 天"))
        out.p("  14 日净流入/出     %s"
              % flow(m["change_14d"], m["base_14d"], "序列不足 15 天"))
        out.p()
        out.p("阈值判定：")
        tg = m["triggers"]
        # ⚪️ 的理由要分清楚：序列太短（没有基准日）与「基准日在、但那天的
        # peggedUSD 是 N/A」是两回事，写成同一句会让人以为再多跑几天就好了。
        na_reason = "⚪️ 无法判定（对齐日的 peggedUSD 为 N/A，不得当成 0）"
        out.p("  7 日净流出（流动性撤出） ...... " + _tri(
            tg["net_outflow_7d"],
            na_reason if m["base_7d"] else "⚪️ 无法判定（序列不足 8 天）",
            "✅ 触发", "❌ 未触发"))
        out.p("  中期确认：近 2 周持平或萎缩 ... " + _tri(
            tg["midterm_flat_or_shrink_14d"],
            na_reason if m["base_14d"] else "⚪️ 无法判定（序列不足 15 天）",
            "✅ 触发", "❌ 未触发"))
        out.p("  单日净流出 >$1B（须标注） ..... " + _tri(
            tg["daily_outflow_gt_1b"],
            na_reason if m["base_1d"] else "⚪️ 无法判定",
            "✅ 触发，必须在报告中标注", "❌ 未触发"))
        if self.note:
            out.p("  注：%s" % self.note)

    def to_json(self):
        m = self.metrics()
        m["signal"] = 17
        m["name"] = "稳定币总供应（USDT+USDC）"
        m["status"] = "ok"
        m["source"] = ("DeFiLlama stablecoins.llama.fi"
                       "（/stablecoincharts/all?stablecoin=1|2，按日期对齐后相加）")
        # 只有真的有缺口时才多这个栏位：正常那一天的 JSON 位元组不变。
        if self.note:
            m["note"] = self.note
            m["data_gaps"] = [r["date"] for r in self.series if r["total"] is None]
        return m

    def missing_json(self):
        return _od(
            signal=17, name="稳定币总供应（USDT+USDC）", status="missing",
            attempted=["DeFiLlama /stablecoins",
                       "DeFiLlama /stablecoincharts/all?stablecoin=1|2"],
            note=self.note,
            next_step="DeFiLlama MCP get_stablecoins；标 ⚪️ + 报滞后周数")

    def missing_text(self, out):
        out.p("【信号 17】稳定币总供应　⚪️ 数据暂缺")
        out.p("  已尝试来源：DeFiLlama /stablecoins、/stablecoincharts/all?stablecoin=1|2")
        out.p("  失败细节：%s" % (self.note if self.note else "未知"))
        out.p("  下一步：改走 DeFiLlama MCP 的 get_stablecoins；标 ⚪️ + "
              "写出上次已知读数与滞后周数。")


def _llama_pairs(seq):
    """DeFiLlama 序列 → [(日期键, 日期数, peggedUSD 或 None)]；
    **结构不对**（不是阵列／元素不是物件／date 不是数字）回 None = 取数失败。

    只有 `peggedUSD` 那一格坏掉时才走 N/A：日期本身仍保留，那一天的值记
    None。丢掉整天会让 1d/7d/14d 的基准日**悄悄换成别的一天**，而报告里
    印出来的基准日看起来仍然完全正常——那比缺一格严重得多。"""
    if not isinstance(seq, list):
        return None
    pairs = []
    for e in seq:
        if not isinstance(e, dict):
            return None
        d = as_num(e.get("date"))
        if d is _NUM_BAD:
            return None
        f = float(d)
        key = repr(int(f)) if f.is_integer() else repr(f)
        v = as_num(_dig(e, "totalCirculating", "peggedUSD"))
        pairs.append((key, d, None if v is _NUM_BAD else v))
    return pairs


def _sub(a, b):
    """差值：任一端 N/A 就没有差值。绝不把 None 当 0。"""
    if a is None or b is None:
        return None
    return a - b


def _tri(v, unknown, yes, no):
    """三态：None（⚪️ 无法判定）绝不折成 false。"""
    if v is None:
        return unknown
    return yes if v else no


def do_stablecoins(json_mode):
    out = Out()
    s = Stablecoins()
    try:
        if s.run(out):
            if json_mode:
                return BlockResult(out, True, [], s.to_json())
            s.render_text(out)
            return BlockResult(out, True, [], None)
    except Exception as exc:
        s.note = _join_note(s.note, _block_failed(out, exc, 17, "稳定币总供应"))
    if json_mode:
        return BlockResult(out, False, ["信号17 稳定币供应"], s.missing_json())
    s.missing_text(out)
    return BlockResult(out, False, ["信号17 稳定币供应"], None)


# ═══════════════════════════════ 分派 ═══════════════════════════════
class BlockResult(object):
    """一个区块的完整产出。平行跑，但归约与输出一律按固定的区块顺序。"""

    def __init__(self, out, ok, missing, payload):
        self.out = out
        self.ok = ok
        self.missing = missing
        self.payload = payload


_BLOCKS = (
    (14, "永续资金费率", "信号14 资金费率"),
    (15, "24h 清算", "信号15 清算"),
    (16, "BTC Dominance", "信号16 BTC Dominance"),
    (17, "稳定币总供应（USDT+USDC）", "信号17 稳定币供应"),
)


def _block_fallback(i, json_mode, exc):
    """连区块自己的围栏都没接住（工作绪被杀、MemoryError…）时的最后一层。
    仍然给出**该区块的**完整 ⚪️ 输出，绝不让这个位置变成 null 或消失：
    `all` 的 JSON 有四个固定键，少一个下游就再也对不齐。"""
    signal, name, tag = _BLOCKS[i]
    out = Out()
    note = _internal_note(exc)
    out.w("⚠️ 【信号 %d】%s 区块未能回传结果（%s），本区块记为 ⚪️ 数据暂缺；"
          "其余区块不受影响。" % (signal, name, note))
    if json_mode:
        payload = _od(signal=signal, name=name, status="missing", note=note,
                      next_step="人工复核该区块来源；标 ⚪️ + 写出上次已知读数与滞后周数")
        return BlockResult(out, False, [tag], payload)
    out.p("【信号 %d】%s　⚪️ 数据暂缺" % (signal, name))
    out.p("  失败细节：%s" % note)
    out.p("  下一步：人工复核该区块来源；标 ⚪️ + 写出上次已知读数与滞后周数。")
    return BlockResult(out, False, [tag], None)


def parse_args(argv, out):
    """手写左到右循环，与 crypto.sh 的参数处理逐条对应。
    不用 argparse：argparse.error() 一律 exit 2，而 2 在本仓库是保留给
    「依赖缺失」的；这些错误讯息也会被照抄进报告，措辞不能交给它生成。"""
    json_mode = False
    cmd = ""
    history_only = False
    symbols = list(DEFAULT_SYMBOLS)
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("-h", "--help"):
            usage(out)
            out.flush()
            raise SystemExit(0)
        elif a == "--json":
            json_mode = True
            i += 1
        elif a == "--history":
            history_only = True
            i += 1
        elif a == "--symbols":
            if i + 1 >= len(argv):
                die("--symbols 需要一个以逗号分隔的清单，例如 BTC,ETH。", 1)
            v = argv[i + 1]
            if not re.match(r"^[A-Za-z0-9]+(,[A-Za-z0-9]+)*$", v):
                die("--symbols 格式错误，收到「%s」。" % v, 1)
            symbols = [x.upper() for x in v.split(",")]
            i += 2
        elif a in ("funding", "liquidations", "dominance", "stablecoins", "all"):
            if cmd:
                die("只能给一个子命令（已收到「%s」又收到「%s」）。" % (cmd, a), 1)
            cmd = a
            i += 1
        elif a.startswith("-"):
            die("未知选项「%s」。用 %s --help 看用法。" % (a, PROG), 1)
        else:
            die("未知子命令「%s」。可用：funding / liquidations / dominance / "
                "stablecoins / all。" % a, 1)

    # --history 单独给也算数：它只对 dominance 有意义，直接补上子命令，
    # 不要为了「子命令必给」的规矩逼使用者多打一个字。
    if history_only and not cmd:
        cmd = "dominance"
    if not cmd:
        usage(out)
        out.flush()
        raise SystemExit(1)
    if history_only and cmd != "dominance":
        die("--history 只对 dominance 子命令有效（收到「%s」）。" % cmd, 1)
    return cmd, json_mode, history_only, symbols


def main():
    global NOW_MS, JSON_MODE, CMD_NAME
    # 输出含大量中日文与 ⚪️/✅/❌，而且会被照抄进报告：locale 不是 UTF-8 时
    # 不能让它变成 UnicodeEncodeError 或一堆 ?，一律钉死 UTF-8。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    out = Out()
    cmd, json_mode, history_only, symbols = parse_args(sys.argv[1:], out)
    # 总围栏要知道该吐 JSON 还是文字、以及该吐哪一种文件形状
    JSON_MODE = json_mode
    CMD_NAME = cmd

    # fixture 回放钩子的第一道保险：指到不存在的目录一律当参数错误当场停，
    # 绝不「悄悄退回连网」。
    if FIXTURE_DIR and not os.path.isdir(FIXTURE_DIR):
        # 路径要 scrub：这是本档唯一会把使用者给的**绝对路径**原样印出来的
        # 地方，而 RISK_FIXTURE_DIR 十之八九落在 $HOME 底下。本技能的输出会
        # 进报告并推 Slack（行为准则第 4 条），家目录形状不得外流。
        die("RISK_FIXTURE_DIR 指向的目录不存在：%s" % scrub(FIXTURE_DIR), 1)

    NOW_MS = fx_epoch() * 1000

    # --history：只读本地历史，不连网、不取数（排查 7d 腿用）。
    if history_only:
        render_dom_history(out, json_mode)
        out.flush()
        raise SystemExit(0)

    if not FIXTURE_DIR:
        require_requests()

    if cmd == "all":
        # 四个区块打四个不同的 host，彼此没有共享状态 → 平行跑。
        # 归约与输出一律按固定顺序 funding → liquidations → dominance →
        # stablecoins，所以平行只影响耗时，不影响任何一个位元组。
        jobs = [
            (do_funding, (json_mode, symbols)),
            (do_liquidations, (json_mode,)),
            (do_dominance, (json_mode,)),
            (do_stablecoins, (json_mode,)),
        ]
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
            futs = [ex.submit(fn, *args) for fn, args in jobs]
            # 逐个取结果，**每个各自围栏**。`f.result()` 会把工作绪里的例外
            # 重新丢出来；只要在这里让它逃掉，三个已经成功的区块的输出就全被
            # 丢弃了（旧版正是这样：一个坏栏位毁掉三个好区块）。
            res = []
            for i, fu in enumerate(futs):
                try:
                    res.append(fu.result())         # 提交顺序 = 固定区块顺序
                except BaseException as exc:        # noqa: BLE001（含 SystemExit）
                    res.append(_block_fallback(i, json_mode, exc))

        okcount = 0
        missing = []
        for r in res:
            if r.ok:
                okcount += 1
            missing.extend(r.missing)

        if json_mode:
            for r in res:
                r.out.flush()
            # ok 不是字面量：与下面 exit 3 同一个判据（四个 block 全灭才算失败）。
            # missing 非空 ⇒ degraded：暂缺项必须标 ⚪️ 并报滞后周数，这条要求
            # 以前只存在于文本分支，机读侧看不到，故一并落成字段（行为准则第 1 条）。
            doc = _od(ok=(okcount > 0), funding=res[0].payload,
                      liquidations=res[1].payload, dominance=res[2].payload,
                      stablecoins=res[3].payload, missing=list(missing))
            doc["degraded"] = (len(missing) > 0) or (not doc["ok"])
            reasons = []
            if missing:
                reasons.append(
                    "本次数据暂缺项：" + "、".join(missing) +
                    "。每一项都必须在报告中标 ⚪️、列出已尝试来源、并写出"
                    "「上次已知读数 X @ YYYY-MM-DD，已滞后 N 周」；"
                    "没有滞后周数的「数据暂缺」是不合格输出（行为准则第 1 条）。")
            if not doc["ok"]:
                reasons.append("四个 block 全部取数失败（OKCOUNT=0）")
            doc["degraded_reasons"] = reasons
            _emit_json(doc)
        else:
            for i, r in enumerate(res):
                r.out.flush()
                if i < 3:
                    sys.stdout.write("\n")
            sys.stdout.write("\n")
            if missing:
                sys.stdout.write("本次数据暂缺项：%s\n" % "、".join(missing))
                sys.stdout.write("  → 每一项都必须在报告中标 ⚪️、列出已尝试来源、"
                                 "并写出「上次已知读数 X @ YYYY-MM-DD，已滞后 N 周」。\n")
                sys.stdout.write("  → 没有滞后周数的「数据暂缺」是不合格输出"
                                 "（行为准则第 1 条）。\n")
            else:
                sys.stdout.write("本次无数据暂缺项。\n")
        raise SystemExit(0 if okcount > 0 else 3)

    if cmd == "funding":
        r = do_funding(json_mode, symbols)
    elif cmd == "liquidations":
        r = do_liquidations(json_mode)
    elif cmd == "dominance":
        r = do_dominance(json_mode)
    else:
        r = do_stablecoins(json_mode)

    r.out.flush()
    if json_mode:
        _emit_json(r.payload)
    if cmd == "liquidations":
        raise SystemExit(3)
    raise SystemExit(0 if r.ok else 3)


# ══════════════════════════ 总围栏（最后一道）════════════════════════════
# 契约：**任何未预期例外都不得以 traceback 逃到使用者面前**。
#   · traceback 会印出脚本的绝对路径（正式环境在 $HOME 底下）→ 违反公开仓库规则。
#   · traceback 走的是 exit 1，而 1 在本仓库是「参数错误」；取数／payload 问题
#     一律是 **exit 3**。
#   · 而且 stdout 一个字都没有 —— 「什么都没印」是最糟的一种失败：
#     没有人能从一份空输出里看出哪一项该标 ⚪️。
JSON_MODE = False
CMD_NAME = ""
_JSON_WRITTEN = False

_CMD_BLOCK = {"funding": 0, "liquidations": 1, "dominance": 2, "stablecoins": 3}


def _fatal_doc(note):
    """总围栏用的 JSON 文件。**形状要跟正常那一份一样**：`all` 就是四个固定
    键都在（每个都是该区块的 ⚪️ missing），单一子命令就是那一个区块的
    missing。少一个键、或换成一份不同形状的文件，下游就再也对不齐了。"""
    reasons = ["脚本发生未预期错误，本次相关信号一律记 ⚪️ 数据暂缺；每一项都必须"
               "在报告中标 ⚪️、列出已尝试来源、并写出「上次已知读数 X @ "
               "YYYY-MM-DD，已滞后 N 周」；没有滞后周数的「数据暂缺」是不合格"
               "输出（行为准则第 1 条）。", note]

    def blk(i):
        signal, name, _tag = _BLOCKS[i]
        return _od(signal=signal, name=name, status="missing", note=note,
                   next_step="人工复核该区块来源；标 ⚪️ + 写出上次已知读数与滞后周数")

    if CMD_NAME == "all":
        return _od(ok=False, funding=blk(0), liquidations=blk(1),
                   dominance=blk(2), stablecoins=blk(3),
                   missing=[b[2] for b in _BLOCKS],
                   degraded=True, degraded_reasons=reasons)
    if CMD_NAME in _CMD_BLOCK:
        o = blk(_CMD_BLOCK[CMD_NAME])
        o["degraded"] = True
        o["degraded_reasons"] = reasons
        return o
    # 连子命令都还没解析出来（参数处理阶段就炸了）
    return _od(ok=False, status="missing", note=note, degraded=True,
               degraded_reasons=reasons)


def _emit_json(doc):
    """把整份 JSON **先组成字串再一次写出**：组的过程若出错，stdout 上不会
    留下半截文件（`--json` 必须永远可解析，这是硬要求）。"""
    global _JSON_WRITTEN
    s = jdumps(doc) + "\n"
    sys.stdout.write(s)
    _JSON_WRITTEN = True


def _fatal(exc):
    """把一个逃到最外层的例外折成合格的降级输出，回传应有的退出码（3）。"""
    note = _internal_note(exc)
    try:
        if JSON_MODE:
            if not _JSON_WRITTEN:
                _emit_json(_fatal_doc(note))
        else:
            for i in ([0, 1, 2, 3] if CMD_NAME == "all"
                      else ([_CMD_BLOCK[CMD_NAME]] if CMD_NAME in _CMD_BLOCK
                            else [])):
                signal, name, _tag = _BLOCKS[i]
                sys.stdout.write("【信号 %d】%s　⚪️ 数据暂缺\n" % (signal, name))
                sys.stdout.write("  失败细节：%s\n" % note)
                sys.stdout.write("  下一步：人工复核该区块来源；标 ⚪️ + "
                                 "写出上次已知读数与滞后周数。\n\n")
            sys.stdout.write("⚪️ 数据暂缺：本次执行发生未预期错误，"
                             "没有任何一项读数可以采信。\n")
            sys.stdout.write("  失败细节：%s\n" % note)
            sys.stdout.write("  → 上列各项一律标 ⚪️，并写出"
                             "「上次已知读数 X @ YYYY-MM-DD，已滞后 N 周」。\n")
        sys.stdout.flush()
    except Exception:
        pass        # stdout 都写不出去了，至少还要把 stderr 那行印出来
    try:
        sys.stderr.write("错误：%s\n" % note)
        sys.stderr.flush()
    except Exception:
        pass
    return 3


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise                       # 正常收尾与 die() 都走这条，原样放行
    except KeyboardInterrupt:
        warn("错误：已中断。")
        raise SystemExit(3)
    except BaseException as _exc:   # noqa: BLE001（这就是「任何例外」的意思）
        raise SystemExit(_fatal(_exc))
