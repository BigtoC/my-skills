#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""stock_perp.py —— 美股 24/7 永续（信号 18）

╔══════════════════════════════════════════════════════════════════════════╗
║ 🚨 必须带 "dex":"xyz" —— 这是本脚本最重要的一行                          ║
║                                                                          ║
║ Hyperliquid **主池**的 `SPX` 是 SPX6900 **迷因币**（约 $0.34），         ║
║ 不是标普 500。不指定 dex 就会取到迷因币，数字完全错误却看起来像有数。     ║
║ 本脚本只接受 xyz 池，且只认 `xyz:` 前缀的市场名。                        ║
╚══════════════════════════════════════════════════════════════════════════╝

⚠️ 两个必须保留的检查（references/signals-c-crypto.md 信号 18，逐条实现）：
  1. **流动性门槛**：名义 OI = markPx × openInterest，**< $5M 就标「不适用」**。
     历史基线 xyz:SP500 ≈ $483M OI／$129M 日成交；xyz:XYZ100 ≈ $282M／$177M。
     骤降到千万以下 = 池子在迁移，报价不可信。
  2. **代码撞名**：标记价与真实指数收盘差 **>10% 就判定取错市场**，
     该市场整笔作废、不得用于报告。

口径（reference 明订，不可改）：
  xyz:SP500  ↔ ^GSPC   指数点位 1:1（≈7,7xx，不是 SPY 的 ≈77x）
  xyz:XYZ100 ↔ ^NDX    指数点位 1:1（≈29,xxx，不是 QQQ 的 ≈7xx）
  资金费率：Hyperliquid 是**每小时**结算 → 8h 费率 = hourly × 8；
           年化% = 8h费率 × 3 × 365（等价于 hourly × 24 × 365）。
           基线 hourly 0.00000625 = 8h 0.005% = 年化 +5.48%
           → 读到这个数就是**「无方向信号，纯粹是利率」**。

上一美股收盘从哪来：
  · Yahoo chart 端点本机实测稳定回 **HTTP 429**（带 UA、带 cookie jar 都一样），
    stooq CSV 端点已下线 —— 两者都不可靠，本脚本不用。
  · `--from-fred` 走 FRED 的 SP500 / NASDAQ100 日收盘序列（免 API key，稳定），
    但它**滞后 1 个交易日**，所以每次都会把观测日期与滞后天数印出来。
    拿滞后的收盘当「上一收盘」算出来的隐含跳空是错的，必须让人看得见滞后。
  · 最准的还是呼叫方直接传 --spx / --ndx。绝不自己编一个收盘价。

⚠️ 收盘价必须是**正数**：隐含跳空是 markPx ÷ 收盘价，0 会让计算炸掉，负数则
   算得出一个看起来正常、实际毫无意义的跳空。两者一律在进计算前挡掉，绝不带着
   坏值往下跑。三个来源（--spx/--ndx、--closes、--from-fred）都要各自把关。

依赖：python3 + requests（stdlib urllib 在本机对 FRED 一律 CERTIFICATE_VERIFY_FAILED）。
退出码：0 正常｜1 参数错误｜2 依赖缺失｜3 取数失败（数据暂缺）｜4 判定取错市场

—— 本档是信号 18 取数的**唯一实现**。它原本是 stock_perp.sh 的 Python 版，行为
   以该 shell 版为规格，逐位元组比对由 oracle（RISK_FIXTURE_DIR 回放）把关；
   **`stock_perp.sh` 已於 2026-09-07 切换後删除**（回滚靠 git）。下文提到 shell 版
   的地方都是与那支已删除前身的差别记录，不是还存在的另一份实作。
   ⚠️ oracle 只走 fixture 路径，**证不出真网传输层**：迁移期间 `http_request` 成功时
   回 `False`（契约是「fail_reason 非 None 即失败」），fixture 路径回 None 所以 oracle
   119/130 全绿，真网却 100% 失败。改传输层後必须跑一次真网比对，不能只看 oracle。

════════════════════ 三个刻意的决定，改埠前先读完 ════════════════════════

【决定一】`--spx=7757.64`／`--sp`／`--nd`／`--n` 这类 GNU 长选项写法一律
  **拒绝**，讯息与退出码与 shell 版逐字相同（`错误：未知参数「--spx=7757.64」。`
  exit 1）。本档不用 argparse：argparse 会把 `--opt=值` 与前缀缩写正规化掉，
  于是「CLI 表面收下了值」与「程式实际拿到值」变成两件事——曾经因此把一个
  **有传**的收盘价静默丢掉，跑出 exit 0 的 ⚪️ 报告，等于把强制检查 ②
  （>10% 撞名／取错市场）悄悄关掉，还在 --json 里写成「未提供」倒打呼叫方一耙。
  参数扫描改成逐字对照 shell 的 while 回圈，多一种写法都不收。
  要放宽成 shell 的超集，请连同 oracle 案例一起加，不要只改这里。

【决定二】上游读数**逐市场**降级，不是逐 payload 作废。任一栏位坏掉（缺栏位、
  null、空字串、非数字字串、布林、NaN／Infinity、阵列长度不足）只让**该市场**
  变成 ⚪️ 数据暂缺，另一个市场照常输出。每个预期市场一定会出现：不是带资料，
  就是带 ⚪️ 与一句「哪个来源、哪里坏了」。**绝不允许一个市场无声消失。**
  两个市场都没读数才整支停在 exit 3；此时若两个都是「名字不在 universe 里」，
  沿用 shell 版那三行「可能是改了市场名」的措辞，否则照实说是哪个栏位坏了——
  把栏位坏掉讲成「市场被改名」会让人去追一个根本没发生的改名。

【决定三】退出码优先序：4（撞名）> 3（有市场 ⚪️ 数据暂缺）> 0。
  payload 形状／栏位问题是**取数问题**，一律 3，绝不 1，也绝不 2。
  另外本档有一个 top-level 兜底：任何没预期到的例外都折成
  「⚪️ 数据暂缺 + exit 3」，讯息经 scrub() 折掉家目录路径，
  --json 分支照样吐一份**完整**（不截断）的 JSON。绝不让 traceback 外泄。

  ⚠️ 兜底自己也必须是全函式（_last_resort）。它以前会炸在三个地方：
  `str(exc)`（例外的 __str__ 本来就可以 raise）、`sys.stdout.write`、
  `sys.stderr.write`（`>&-` / `2>&-` 会把这两个物件变成 **None**）。
  炸掉的结果正是这段要挡的东西：带绝对路径的 traceback、exit 1、空的 --json。
  现在 scrub()／clip()／warn()／die()／_emit() 全部保证不 raise，
  最后一段则只是把 import 时就编码好的 _LAST_RESORT_JSON_BYTES 写到 fd 1。

【决定四】**派生量**（乘除出来的数）要再验一次 isfinite，不是只验输入栏位。
  canon_number() 保证每个上游栏位是有限数，但 1e200 × 1e200 = inf，而 inf
  通得过之后每一个比较——於是印出「年化 +inf% > +15% → 真实多头拥挤」这种
  exit 0 的**捏造触发**。任一派生量非有限，该市场整笔降级成 ⚪️ 数据暂缺。
  见 NonFiniteDerivation / finite_or_fail。
"""

import json
import math
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from urllib.parse import urlsplit

PROG = os.path.basename(sys.argv[0]) or "stock_perp.py"
API = "https://api.hyperliquid.xyz/info"
DEX = "xyz"                      # 绝不可省，见档头
TIMEOUT = 25

OI_MIN_USD = "5000000"           # 名义 OI 门槛：< $5M 标「不适用」
WRONG_MARKET_PCT = "10.0"        # 与现货收盘差 >10% → 判定取错市场
GAP_TIER2_PCT = "2.0"            # |隐含跳空| ≥2% → 计 1 个 Tier 2 触发
GAP_MENTION_PCT = "1.0"          # ≥1% 即使不触发也必须在解读里点名
RELSTR_PCT = "1.5"               # 科技 vs 大盘差距 ≥1.5pt 要点名
FUND_HOT_ANNUAL = "15.0"         # 年化 >+15% = 真实多头拥挤
FUND_COLD_ANNUAL = "-10.0"       # 年化 <−10% = 强烈对冲需求

# 阈值同时是「印出来的字面」与「拿来比大小的数」。字面保留成字串（--help、
# 人类输出、JSON 数字字面量三处都要照抄），比较时才 float()，两边不会各自漂。
OI_MIN_USD_F = float(OI_MIN_USD)
WRONG_MARKET_PCT_F = float(WRONG_MARKET_PCT)
GAP_TIER2_PCT_F = float(GAP_TIER2_PCT)
GAP_MENTION_PCT_F = float(GAP_MENTION_PCT)
RELSTR_PCT_F = float(RELSTR_PCT)
FUND_HOT_ANNUAL_F = float(FUND_HOT_ANNUAL)
FUND_COLD_ANNUAL_F = float(FUND_COLD_ANNUAL)

MARKETS = (
    # (Hyperliquid 市场名, 对照现货指数, --closes 的等价代码)
    ("xyz:SP500", "^GSPC", ("^GSPC", "GSPC", "SPX", "SP500", "标普500")),
    ("xyz:XYZ100", "^NDX", ("^NDX", "NDX", "NASDAQ100", "NDX100", "纳斯达克100")),
)
INDEX_OF = {m: idx for m, idx, _ in MARKETS}

# 每来源一组 header，写死在同一张表里。两条实测事实（2026-09-05）方向相反，
# 「统一成一套 header」会静默弄坏其中一边：
#   · FRED **绝不可带浏览器 User-Agent**（带了 ~25–30s ReadTimeout；
#     requests 预设 UA 或 curl-like UA 才会 200）。
#   · Yahoo **必须带**浏览器 UA（裸 session 稳定回 429）。
# 本脚本目前不打 Yahoo（档头写明理由），Yahoo 那条留着是为了下一个人加请求时
# 不必重新踩一次；不要把它「简化」成一条共用 header。
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def headers_for(url):
    host = (urlsplit(url).hostname or "").lower()
    if host.endswith("fred.stlouisfed.org"):
        return {}                                   # ← 绝不加 UA
    if host.endswith("finance.yahoo.com"):
        return {"User-Agent": BROWSER_UA}           # ← 必须加 UA
    return {}


USAGE = """{prog} —— 美股 24/7 永续 xyz:SP500 / xyz:XYZ100（信号 18）

用法:
  {prog} [--spx <上一美股收盘>] [--ndx <上一美股收盘>] [选项]
  {prog} --closes <file.json> [选项]

选项:
  --spx N          ^GSPC 上一美股收盘价（指数点位，例如 7757.64）
  --ndx N          ^NDX  上一美股收盘价（指数点位，例如 29800.12）
  --from-fred      自动从 FRED 抓 SP500 / NASDAQ100 的最近收盘（免 API key）。
                   ⚠️ FRED 的日收盘序列**会滞后 1 个交易日**，本脚本一定会把
                      观测日期与滞后天数印出来；滞后 >1 天时另外告警。
                      --spx / --ndx 明确给的值永远优先于 FRED。
  --closes FILE    从 JSON 档读收盘价；下列任一形状都认得：
                     {{"indices":{{"^GSPC":{{"close":7757.64}},"^NDX":{{"close":29800.1}}}}}}
                     {{"^GSPC":7757.64,"^NDX":29800.1}}
                   （即姊妹技能 ai-pullback-daily 的 technicals.py --json 输出形状）
  --json           以 JSON 输出
  -h, --help       显示本说明

不给收盘价也能跑：会输出 perp 标记价、prevDayPx、名义 OI、资金费率与流动性判定，
但**隐含跳空一律标 ⚪️ 无法判定**，绝不用 prevDayPx 冒充美股收盘
（prevDayPx 是 perp 自己 24 小时前的价，不是现货收盘）。

例子:
  {prog}
  {prog} --from-fred                      # 隐含跳空对照 FRED 的最近收盘（会标滞后）
  {prog} --spx 7757.64 --ndx 29800.12
  {prog} --closes tech.json --json

判定规则:
  隐含跳空 = perp markPx ÷ 上一美股收盘 − 1
    |偏离| ≥ {g2}%  → 计 1 个 Tier 2 触发
    |偏离| ≥ {g1}%  → 即使不触发也必须在解读里点名
  科技 vs 大盘相对强弱 = XYZ100 偏离% − SP500 偏离%，差距 ≥{rel}pt 时点名
  资金费率（弱信号）：年化 >+{hot}% 或 <{cold}%
""".format(prog="{prog}", g2=GAP_TIER2_PCT, g1=GAP_MENTION_PCT, rel=RELSTR_PCT,
           hot=FUND_HOT_ANNUAL, cold=FUND_COLD_ANNUAL)


def usage_text():
    return USAGE.replace("{prog}", PROG)


# ══════════════════════ 路径脱敏（公开仓库规矩）══════════════════════════
# 本仓库是公开的，脚本输出会被照抄进报告正文并推 Slack，所以任何**可能**带绝对
# 路径的字串（尤其例外物件的 str()，OSError 尤甚）都要先过 scrub()。
# 同一份实作在 technicals.py / perp_quotes.py / neocloud_credit_*.py 里各有一份
# 复本（CLAUDE.md 有记）——grep `_HOMEISH_RE` 会找到全部，改一处要改全部。
_HOMEISH_RE = re.compile(r"(?:/Users|/home|/var/folders)/[^/\s\"']+")


# 连「把这个物件变成字串」都失败时用的常数。绝不回传未经脱敏的原字串。
_UNPRINTABLE = "⟪无法转成字串的物件⟫"


def _safe_text(obj):
    """obj → str，**绝不 raise**。

    `str(exc)` 自己会 raise 不是理论问题：任何自订例外的 __str__ 都可能炸，
    而 top-level 兜底处理器正是拿例外物件来组讯息的地方——它一炸，整条
    「不准让 traceback 外泄」的保证就跟着炸，使用者看到的是一份带
    /Users/<名字>/… 绝对路径的 traceback，退出码还是 1（本仓库的「参数错误」）。
    所以 str → repr → 常数，三段都包起来。
    """
    if isinstance(obj, str):
        return obj
    try:
        return str(obj)
    except BaseException:                       # noqa: BLE001（理由见上）
        pass
    try:
        return repr(obj)
    except BaseException:                       # noqa: BLE001
        pass
    try:
        return "%s（%s）" % (_UNPRINTABLE, type(obj).__name__)
    except BaseException:                       # noqa: BLE001
        return _UNPRINTABLE


def scrub(text):
    """折掉家目录绝对路径。**全函式**：任何输入都回一个字串，绝不 raise。"""
    s = _safe_text(text)
    try:
        home = os.path.expanduser("~")
    except BaseException:                       # noqa: BLE001
        home = ""
    try:
        if home and home.startswith("/"):
            s = s.replace(home, "~")
        return _HOMEISH_RE.sub("~", s)
    except BaseException:                       # noqa: BLE001
        # 折不掉就整段丢掉：宁可少一句诊断，也不把绝对路径放出去（公开仓库规矩）。
        return _UNPRINTABLE


def clip(text, limit=60):
    """把要塞进讯息的**上游值**截短并脱敏：坏 payload 可能是一整个 MB。

    与 scrub() 一样是**全函式**——兜底处理器唯一的讯息来源就是它。
    """
    try:
        s = scrub(text).replace("\n", "\\n").replace("\t", "\\t")
        return s if len(s) <= limit else s[:limit] + "…"
    except BaseException:                       # noqa: BLE001
        return _UNPRINTABLE


# ══════════════════════ 两个输出流：写不进去也不准炸 ══════════════════════
# `>&-` / `2>&-`（把 fd 关掉）时 CPython 会把 sys.stdout ／ sys.stderr 设成
# **None**，於是 `sys.stderr.write(...)` 是 AttributeError；`| head -1` 则是
# BrokenPipeError。两者以前都会一路逃到最外层：印一份带绝对路径的 traceback，
# 退出码变成 1，把一次取数失败（3）或撞名判定（4）谎报成呼叫方打错字。
# 写不出去是**输出通道**的问题，不是判定的问题——判定该是 3／4 就还是 3／4。
def _emit_bytes(fd, data):
    """把 bytes 直接写到 fd（1=stdout、2=stderr）。回传是否成功，绝不 raise。"""
    try:
        os.write(fd, data)
        return True
    except BaseException:                       # noqa: BLE001
        return False


def _emit(fd, text):
    """把 text 写到 stdout(1)／stderr(2)。回传是否成功，**绝不 raise**。

    先走 Python 的 stream（正常路径的位元组一字不变），失败再退到 os.write
    （stream 物件坏掉、但 fd 还活着时救得回来）。两段都失败就安静放弃：
    此时使用者根本收不到任何东西，再抛例外只是往同一个坏掉的 fd 多印一份
    traceback。
    """
    try:
        stream = sys.stdout if fd == 1 else sys.stderr
    except BaseException:                       # noqa: BLE001
        stream = None
    if stream is not None:
        try:
            stream.write(text)
            try:
                stream.flush()
            except BaseException:               # noqa: BLE001
                pass
            return True
        except BaseException:                   # noqa: BLE001
            pass
    try:
        data = text.encode("utf-8", "replace")
    except BaseException:                       # noqa: BLE001
        return False
    return _emit_bytes(fd, data)


def warn(msg):
    try:
        line = "%s\n" % _safe_text(msg)
    except BaseException:                       # noqa: BLE001
        line = "%s\n" % _UNPRINTABLE
    _emit(2, line)


def die(msg, code=1):
    try:
        line = "错误：%s\n" % _safe_text(msg)
    except BaseException:                       # noqa: BLE001
        line = "错误：%s\n" % _UNPRINTABLE
    # ⚠️ stderr 写不进去（`2>&-`）**不准**改掉退出码：以前 `sys.stderr.write`
    # 会丢 AttributeError，一路逃到最外层，於是每一条 die() 都变成 exit 1——
    # 取数失败（3）与撞名（4）全被谎报成参数错误。
    _emit(2, line)
    sys.exit(code)


# ══════════════════════ 数字字面量：一路保留原样 ══════════════════════════
# 上游给的 markPx / funding / FRED 收盘价、以及本脚本 sprintf 出来的 %.4f/%.2f，
# 都以**字串**在程式里流动，只有要比大小时才 float()。理由有两个：
#   · 人类输出与 JSON 都得逐字照抄同一个字面（29544.160 不可以变成 29544.16）；
#   · 「显示用的字面」与「计算用的数」分开，就不会有某一处偷偷多做一次四舍五入。
class Lit(object):
    """JSON 里要原样输出的数字字面量。"""
    __slots__ = ("text",)

    def __init__(self, text):
        self.text = text


class RawFloat(float):
    """json.loads 解出来的浮点数，附带原始字面。"""

    def __new__(cls, s):
        o = float.__new__(cls, float(s))
        o.lit = s
        return o


class RawInt(int):
    """json.loads 解出来的整数，附带原始字面。"""

    def __new__(cls, s):
        o = int.__new__(cls, int(s))
        o.lit = s
        return o


class NonFinite(object):
    """json.loads 碰到 NaN／Infinity／-Infinity 时换上的哨兵。

    Python 的 json 预设**接受**这三个非标准字面量并解成真的 float('nan')／inf，
    之后 `float(v)` 当然过关、`math.isfinite` 之外没有任何东西挡得住它们。
    换成一个不是 int／float 的物件，让所有数字检查一律不认得它，
    再由 canon_number()／read_close() 各自给出「不是有限数字」的具体讯息。
    """
    __slots__ = ("text",)

    def __init__(self, text):
        self.text = text


# JSON 数字字面量的完整文法。用途有两个，缺一不可：
#   · 判定上游给的字串是不是**真的**是个数字——`float()` 收 "NaN"、"inf"、
#     " 1.5 "、"1_000"（Python 的底线分隔！），全都不是合法 JSON 数字，
#     照抄进 --json 会吐出解不开的文件；
#   · 保证凡是被包成 Lit() 原样输出的字面，写进 JSON 一定合法。
JSON_NUM_RE = re.compile(r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][-+]?[0-9]+)?$")


def canon_number(v):
    """上游栏位 → (字面:str, 数值:float)；不合格回 (None, 中文原因)。

    **唯一**把上游值变成本程式内部数字的入口。以前的写法是「用 float(v) 验、
    却把原始 v 存下来」，於是：
      · Hyperliquid 把 markPx 从字串改成 JSON 数字 → padl()／Lit() 拿到 float
        → `'float' object has no attribute 'encode'` traceback，退出码还是 1；
      · markPx = "NaN" → float() 过关 → 印出「❌ 未触发（+nan%）」与
        「名义 OI $nanM < $5M 门槛」，一份 exit 0 的**捏造未触发**报告。
    这里一次把两件事定死：型别正规化成 (字面, 有限 float)，不合格就说清楚哪里坏。
    """
    if v is None:
        return None, "栏位是 null"
    if isinstance(v, bool):
        # 必须排在 int 之前：Python 里 True 是 int，float(True) = 1.0。
        return None, "栏位是布林值（%s）" % ("true" if v else "false")
    if isinstance(v, NonFinite):
        return None, "栏位不是有限数字（收到「%s」）" % clip(v.text)
    if isinstance(v, str):
        lit = v.strip()                          # 只去头尾空白，不改内容
        if lit == "":
            return None, "栏位是空字串"
    elif isinstance(v, (int, float)):
        lit = getattr(v, "lit", None)            # RawFloat／RawInt 带原始字面
        if lit is None:
            lit = repr(v)
    else:
        return None, "栏位型别不是数字（%s）" % type(v).__name__
    if not JSON_NUM_RE.match(lit):
        return None, "栏位不是合法的数字字面量（收到「%s」）" % clip(lit)
    try:
        f = float(lit)
    except (TypeError, ValueError):              # 理论上不可达，兜着
        return None, "栏位无法解析成数字（收到「%s」）" % clip(lit)
    if not math.isfinite(f):
        # 例如 "1e400"：字面合法，值却溢位成 inf。
        return None, "栏位不是有限数字（收到「%s」）" % clip(lit)
    return lit, f


class NonFiniteDerivation(Exception):
    """**派生量**（乘出来／除出来的数）溢位成 inf／nan。

    canon_number() 只保证每个**输入**栏位是有限数——但有限数相乘相除照样会
    溢位：markPx 1e200 × openInterest 1e200 = inf，funding 1e306 × 8 × 100 也是。
    inf 通得过之后每一个比较（`inf > 15.0` 为真、`inf >= 5e6` 为真），於是：
      · 文字分支印出「资金费率 ✅ 触发：年化 +inf% > +15% → 真实多头拥挤」与
        「名义 OI $infM ≥ $5M 门槛 → 可用」—— 一份 exit 0 的**捏造触发**报告；
      · --json 那边 Lit() 挡下不合法的字面量，於是 notional_oi_usd 变成一句
        中文（schema 当场坏掉），triggers.funding_crowded_long 却照样把这个
        市场列进去，ok 还是 true。
    所以每一个派生量都要再验一次 isfinite。任一个不是有限数，就让**该市场**
    整笔降级成 ⚪️ 数据暂缺——不是只把那一栏填 None：流动性、资金费率、隐含
    跳空三个判定全建立在同一批乘除上，留下任何一个都还是在报一个算坏了的市场。
    """
    __slots__ = ("reason",)

    def __init__(self, reason):
        Exception.__init__(self, reason)
        self.reason = reason


def finite_or_fail(value, what):
    """派生量守门员：不是有限 float 就抛 NonFiniteDerivation（附带哪一步坏了）。"""
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise NonFiniteDerivation(
        "%s算出来不是有限数字（%s）—— 上游数量级异常，本市场整笔不采用"
        % (what, clip(repr(value), 40)))


def _jstr(s):
    return json.dumps(s, ensure_ascii=False)


def jq_dumps(obj, indent=0):
    """照 jq 的排版输出：2 空格缩排、空容器写成 []／{}、非 ASCII 不转义。

    ⚠️ 这个函式必须是**全函式**：任何输入都要吐出合法 JSON，绝不 raise。
    半路 raise 会让 `sys.stdout.write(jq_dumps(...))` 变成「什么都没写」或
    （若改成边算边写）写出**截断的非 JSON**，那是本埠最不能出现的结果——
    机器读到半份文件比读到一份 ⚪️ 文件危险得多。
    未知型别一律折成一个会自曝的字串，而不是猜一个数字。
    """
    pad = "  " * indent
    pad2 = "  " * (indent + 1)
    if isinstance(obj, Lit):
        # Lit 是「原样输出的数字字面量」：只有通过 canon_number() 的字面才会
        # 走到这里。万一有人塞了别的东西进来，宁可输出一个合法的 JSON 字串，
        # 也不要吐出会让整份文件解不开的裸 token。
        if isinstance(obj.text, str) and JSON_NUM_RE.match(obj.text):
            return obj.text
        return _jstr("⚪️ 无法序列化的数字字面量（%s）" % clip(repr(obj.text)))
    if obj is None:
        return "null"
    if obj is True:
        return "true"
    if obj is False:
        return "false"
    if isinstance(obj, str):
        return _jstr(obj)
    if isinstance(obj, int):                    # bool 已在上面拦掉
        return str(obj)
    if isinstance(obj, float):
        # 只有 lag_days 之类的整数会走这里；非有限值不可能写进合法 JSON。
        return repr(obj) if math.isfinite(obj) else _jstr("⚪️ 非有限数字")
    if isinstance(obj, list):
        if not obj:
            return "[]"
        return ("[\n" + ",\n".join(pad2 + jq_dumps(v, indent + 1) for v in obj)
                + "\n" + pad + "]")
    if isinstance(obj, dict):
        if not obj:
            return "{}"
        return ("{\n" + ",\n".join(pad2 + _jstr(str(k)) + ": " + jq_dumps(v, indent + 1)
                                   for k, v in obj.items())
                + "\n" + pad + "}")
    return _jstr("⚪️ 无法序列化的值（型别 %s）" % type(obj).__name__)


# ══════════════════════ 栏宽：照 bash/awk 的 **位元组** 补齐 ══════════════
# ⚠️ 这是刻意的，不是疏忽。bash 3.2 与 awk 的 printf 都按 UTF-8 **位元组**补宽，
#    Python 的 %-12s 按**码位**补，zsh 的 printf 又跟 Python 一致——所以在 zsh
#    里互动测试会看不出差别。人类输出是要照抄进报告的（本仓库行为准则第 4 条），
#    表头与资料列的相对位置必须与 shell 版一模一样，因此这里按位元组补。
#    （附带后果：CJK 表头在等宽终端里本来就没对齐在资料上方——那是 shell 版
#     既有的排版缺陷，此处刻意原样保留，修它属于另一次「刻意且要报备」的改动。）
#    ⚠️ 这三个也必须是**全函式**（见 jq_dumps 上面那段）：文字分支只在最后
#    write 一次，但半路 raise 就等于「什么都没印」——一份报告静默消失，
#    比一份带 ⚪️ 的报告糟得多。非字串一律先 str()，绝不 AttributeError。
def _blen(s):
    if not isinstance(s, str):
        s = str(s)
    return len(s.encode("utf-8", "replace"))


def padr(s, w):
    """printf '%-Ns'"""
    if not isinstance(s, str):
        s = str(s)
    return s + " " * max(0, w - _blen(s))


def padl(s, w):
    """printf '%Ns'"""
    if not isinstance(s, str):
        s = str(s)
    return " " * max(0, w - _blen(s)) + s


# ══════════════════════ fixture 回放钩子（离线迁移验证用）══════════════════
# 正常执行时这段等于不存在：**只有** RISK_FIXTURE_DIR 指到一个存在的目录才启用，
# 变数没设 = 一律走网络。指到不存在的目录一律当参数错误当场停（exit 1），
# 绝不「悄悄退回连网」——那会让一次以为在离线比对的跑法偷偷打了真上游。
#
# 目录布局：一个请求两个档
#   <slug>.body   原始回应内容（bytes 照抄）
#   <slug>.code   HTTP 码；档案不存在时视为 200
# slug 规则（与 shell 版逐字一致）：
#   GET  : 去掉 scheme，再把 [^A-Za-z0-9._-] 全部换成 "_"
#   POST : "POST_" + 上述规则处理过的 url + "_" + 上述规则处理过的 body
# 找不到 fixture 时**大声**回放成连线失败（000）并印一行 stderr，绝不静默。
#
# RISK_FIXTURE_NOW（epoch 秒）同样只在 fixture 模式下生效，用来冻结「今天」，
# 让含日期／年龄的输出可以逐字比对。单独设它而不设 RISK_FIXTURE_DIR 无效。
FIXTURE_DIR = os.environ.get("RISK_FIXTURE_DIR", "")


def fx_slug(s):
    return re.sub(r"[^A-Za-z0-9._-]", "_",
                  re.sub(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", "", s))


def fx_serve(slug):
    """→ (http_code:str, body:bytes, warnings:[str])"""
    body_path = os.path.join(FIXTURE_DIR, slug + ".body")
    code_path = os.path.join(FIXTURE_DIR, slug + ".code")
    if os.path.isfile(body_path):
        with open(body_path, "rb") as f:
            body = f.read()
        code = "200"
        if os.path.isfile(code_path):
            with open(code_path, "r", encoding="utf-8", errors="replace") as f:
                code = f.read().strip()   # ⚠ 必须 strip：`echo 200 > x.code`
                # 会写进结尾换行，而所有消费端比的是 `code != "200"`，
                # 於是一个健康的回放被读成 HTTP 失败、整条回退链被触发。
                # fixture 是这两支埠**唯一**的离线验证手段，坏在这里等於验证白做。
        return code, body, []
    return "000", b"", [
        "⚠️ RISK_FIXTURE_DIR 里没有 fixture「%s」，本次请求以连线失败（000）回放。" % slug]


def fx_epoch():
    if FIXTURE_DIR and os.environ.get("RISK_FIXTURE_NOW"):
        return int(os.environ["RISK_FIXTURE_NOW"])
    return int(time.time())


# ══════════════════════ 依赖 ══════════════════════════════════════════════
_REQUESTS = None


def ensure_requests():
    """唯一的依赖检查，在**主线程**、在开工前跑一次。

    与 shell 版一样是无条件的前置检查（shell 版在最上面就 command -v curl/jq/awk），
    只有 --help 例外——说明书不该因为少装套件就看不到。
    刻意不在工作线程里做：线程里 sys.exit() 只会变成 future 的例外，
    讯息会随线程数印很多次，退出码还得靠 re-raise 才对，两者都不可靠。

    只抓 ImportError 是不够的：**装坏一半**的 requests（相依的 urllib3／
    charset_normalizer 版本对不上、编译出来的 .so 载不进来）会丢 AttributeError／
    OSError／ValueError 而不是 ImportError，一路逃到最外层变成 traceback。
    结论一样是「这台机器上 requests 不能用」＝依赖缺失＝exit 2，
    所以这里一律接住，另外把原因（已脱敏）多印一行好让人知道要修什么。
    """
    global _REQUESTS
    if _REQUESTS is not None:
        return _REQUESTS
    try:
        import requests
    except Exception as exc:                    # noqa: BLE001（理由见上）
        warn("错误：找不到 Python 套件 requests。Hyperliquid 与 FRED 都要走 HTTPS，")
        warn("     而 stdlib urllib 在本机对 FRED / multpl.com / CNN 一律")
        warn("     CERTIFICATE_VERIFY_FAILED（requests 自带 certifi，所以这支用它）。")
        warn("     安装：python3 -m pip install requests")
        if not isinstance(exc, ImportError):
            warn("     （import requests 抛的不是 ImportError，多半是装坏一半：%s: %s）"
                 % (type(exc).__name__, clip(exc, 160)))
        sys.exit(2)
    _REQUESTS = requests
    return requests


# ══════════════════════ HTTP 出口（GET / POST 各一个）═══════════════════════
# 所有网络出口只有这两个函式，fixture 钩子与 per-host header 都只在这里生效。
# 回传 (code, body, warnings, fail_reason)：
#   · fail_reason 非 None 代表连线层就没成功（相当于 curl 非 0 退出码），
#     字串本身是已脱敏的原因，印在 ⚪️ 那行之後；
#     HTTP 码不对／结构不对是另一条路，两者的讯息**不同**，不可合并。
def http_request(method, url, body=None):
    if FIXTURE_DIR:
        slug = fx_slug(url) if method == "GET" else \
            "POST_%s_%s" % (fx_slug(url), fx_slug(body or ""))
        code, raw, warns = fx_serve(slug)
        return code, raw, warns, None

    requests = ensure_requests()
    headers = dict(headers_for(url))
    if method == "POST":
        headers["Content-Type"] = "application/json"
    last = None
    for _ in range(2):                        # curl --retry 1 = 首次 + 1 次重试
        try:
            if method == "GET":
                r = requests.get(url, headers=headers, timeout=TIMEOUT)
            else:
                r = requests.post(url, headers=headers,
                                  data=(body or "").encode("utf-8"), timeout=TIMEOUT)
            # fail_reason 的契约是「**非 None 即失败**」（见本函式上方注解）。
            # 这里回 False 会让 `fail_reason is not None` 成立 → 每一次**成功**的
            # 请求都被读成传输层失败，并把 False 当成失败原因印出来。
            # fixture 路径回的是 None，所以 oracle 全绿而真网 100% 坏 ——
            # 回放模式根本不走这一行。成功一律 None。
            return str(r.status_code), r.content, [], None
        except Exception as exc:              # 传输层任何失败（DNS／TLS／逾时／断线）
            # 以前这里是 `pass`，於是「为什么连不上」被整个吞掉，操作者只看得到
            # 一句「连线失败」，得自己重打一次 curl 才知道是逾时还是 TLS。
            # 原因一定要带出来，但**必须先过 scrub()**：requests 的例外讯息常带
            # certifi 的绝对路径（/Users/<名字>/… 或 ~/…/site-packages），
            # 那正是公开仓库禁止外泄的东西。
            last = "%s: %s" % (type(exc).__name__, clip(exc, 200))
    return "000", b"", [], (last or "未知（requests 未回报例外）")


def http_get(url):
    return http_request("GET", url)


def http_post(url, body):
    return http_request("POST", url, body)


# ══════════════════════ 参数 ══════════════════════════════════════════════
NUM_RE = re.compile(r"^-?[0-9]+(\.[0-9]+)?$")


def is_num(s):
    return bool(NUM_RE.match(s))


def is_pos(s):
    """只查正负、不查格式：给 --closes 用。

    ⚠️ `float()` 收 "NaN"／"Infinity"／"inf"／"1e400"，而 NaN 的所有比较都是
    False、inf 则 `> 0` 为真——後者会一路走进除法，算出 −100.00% 的「隐含跳空」
    并触发撞名判定，凭空造出一个看起来完全正常的结论。一律先 isfinite。
    """
    try:
        f = float(s)
    except (TypeError, ValueError):
        return False
    return math.isfinite(f) and f > 0


def is_pos_num(s):
    """格式与正负都查：给 --spx / --ndx / FRED 用。"""
    return is_num(s) and is_pos(s)


class Opts(object):
    """扫描 argv 的结果——具名，不是一包 dict。"""
    __slots__ = ("json", "from_fred", "spx", "ndx", "closes")

    def __init__(self):
        self.json = False
        self.from_fred = False
        self.spx = None
        self.ndx = None
        self.closes = None


class HelpRequested(Exception):
    pass


# 逐字对照 shell 版的 `while [ $# -gt 0 ]; do case "$1" in … esac; done`。
# **刻意不用 argparse**（决定一，见档头）：argparse 会把 `--spx=7757.64` 与
# `--sp` 这类写法正规化掉，於是「CLI 表面收下了值」跟「程式真的拿到值」变成
# 两件事。本埠只认这七个 token，一字不差：
#   -h  --help  --json  --from-fred  --spx <N>  --ndx <N>  --closes <FILE>
# 其余（含 `--opt=值`、前缀缩写、任何位置参数）一律「未知参数」exit 1，
# 措辞与 shell 版逐字相同。
def parse_argv(argv):
    o = Opts()
    i = 0
    n = len(argv)
    while i < n:
        a = argv[i]
        if a in ("-h", "--help"):
            raise HelpRequested()
        elif a == "--json":
            o.json = True
            i += 1
        elif a == "--from-fred":
            o.from_fred = True
            i += 1
        elif a in ("--spx", "--ndx"):
            name = a
            if i + 1 >= n:
                die("%s 需要一个数字。" % name, 1)
            v = argv[i + 1]
            # 验证与赋值绑在一起：值只有通过检查才会被记下来，
            # 也**只**从这里记下来。曾经因为「argparse 收值、另一段扫 argv
            # 决定要不要用」而把有传的收盘价整个丢掉。
            if not is_num(v):
                die("%s 必须是数字，收到「%s」。" % (name, v), 1)
            if not is_pos(v):
                die("%s 必须大于 0（隐含跳空要拿标记价除以它），收到「%s」。" % (name, v), 1)
            if name == "--spx":
                o.spx = v
            else:
                o.ndx = v
            i += 2
        elif a == "--closes":
            if i + 1 >= n:
                die("--closes 需要一个 JSON 档路径。", 1)
            v = argv[i + 1]
            # 档案存在与否在这里就查——与 --spx/--ndx 同一个位置、同一个时机。
            # 以前 `--closes=<path>` 绕过这道检查，於是「档案不存在」被报成
            # 「不是合法 JSON」，把人指向错误的方向。
            if not os.path.isfile(v):
                die("--closes 指定的档案不存在。", 1)
            o.closes = v
            i += 2
        else:
            die("未知参数「%s」。用 %s --help 看用法。" % (a, PROG), 1)
    return o


# ══════════════════════ --closes：容错读取 ════════════════════════════════
def _flatten(value, cap=10000):
    """jq 的 `flatten`（无深度上限，阵列一路摊平；非阵列原样保留）。

    用显式堆叠而不是递归：这是在读**呼叫方给的档案**，一份深巢状阵列不该
    变成 RecursionError；cap 再挡住「一百万个元素」把时间与讯息吃光。
    """
    out = []
    stack = [value]
    while stack:
        v = stack.pop()
        if isinstance(v, list):
            for x in reversed(v):
                stack.append(x)
            continue
        out.append(v)
        if len(out) >= cap:
            break
    return out


def read_close(doc, keys):
    """容器键只展开一层，与姊妹技能 perp_quotes.py 同语义。

    候选物件 = [根物件] + [根物件里每个是 object 的值]；
    对每个候选，取键落在 keys 里的项，值是 object 就拿 .close // .price，
    否则拿值本身；**扁平化**（jq 的 flatten，阵列一路摊平）后只留数字，取第一个。
    「扁平化」不是修辞：`{"^GSPC":[7757.64]}` 与 `{"^GSPC":{"close":[7757.64]}}`
    都是 shell 版收得下的形状。

    → (字面:str｜None, 非有限值的字面:str｜None)
    第二个回传值是给「档案里写了 NaN／Infinity」用的。jq 会把 NaN 读成 null
    （於是静默变成「未提供」）、把 Infinity 夹成 1.797e308（於是隐含跳空算出
    −100.00%、撞名判定 🔴、exit 4——一个凭空长出来的结论）。两种都不接受：
    找不到别的合法数字时，由呼叫端当场以参数错误停下来，并把读到的东西讲出来。
    """
    if not isinstance(doc, dict):
        return None, None
    candidates = [doc]
    for v in doc.values():
        if isinstance(v, dict):
            candidates.append(v)
    out = []
    for cand in candidates:
        for k, v in cand.items():
            if k not in keys:
                continue
            if isinstance(v, dict):
                for sub in ("close", "price"):
                    sv = v.get(sub)
                    if sv is not None and sv is not False:
                        out.append(sv)
                        break
            else:
                out.append(v)
    # jq 的最后三步是 `| flatten | map(select(type=="number")) | first`：值是
    # **阵列**时要先摊平再找第一个数字。以前这里少了 flatten，於是
    # `{"^GSPC":[7757.64]}` 在 shell 版算得出隐含跳空、在本埠却读成「未提供」——
    # 同一份档案两个答案，而且是安静地把强制检查 ②（>10% 撞名）关掉的那一边。
    for v in _flatten(out):
        if isinstance(v, bool):
            continue
        if isinstance(v, NonFinite):
            # **当场停**，不继续找下一个候选。档案里同一个指数既写了 NaN 又写了
            # 一个正常数字，代表这份档案本身有问题；默默挑其中一个用，等於在两个
            # 互相矛盾的读数里替呼叫方做决定——本仓库所有口径规则要挡的就是这个。
            # shell 版在这里也是 exit 1（它读到 jq 把 NaN 转成的 null），
            # 退出码一致，只是本埠讲得出「读到的是 NaN」而不是「读到 null」。
            return None, v.text
        if isinstance(v, (int, float)):
            lit = getattr(v, "lit", None)
            return (lit if lit is not None else repr(v)), None
    return None, None


# ══════════════════════ FRED ══════════════════════════════════════════════
def fred_last(series_id):
    """→ (值:str, 日期:str, warnings:[str])；取不到回 (None, None, warnings)。

    这段刻意与 fred.sh 重复一小块取数逻辑，好让 stock_perp 可以单独发布、单独
    执行，不依赖同目录还有没有 fred.sh。两处的规则必须一致：
      · **不要加自订 User-Agent**（带浏览器 UA 会挂住到超时）
      · FRED 用 `.` 表示缺值 → 跳过，取最近一个有值的点，并回报该点日期
    """
    url = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=%s" % series_id
    code, raw, warns, fail_reason = http_get(url)
    if fail_reason is not None:
        # 以前连线失败的原因被 `except Exception: pass` 整个吞掉，
        # 於是「FRED 没取到」永远看不出是逾时、DNS 还是 TLS。原因已脱敏。
        warns = list(warns) + ["⚠️ FRED %s 连线失败：%s" % (series_id, fail_reason)]
        return None, None, warns
    if code != "200":
        return None, None, warns
    text = raw.decode("utf-8", "replace")
    value = date = None
    for line in text.splitlines()[1:]:          # NR>1：跳过表头
        line = line[:-1] if line.endswith("\r") else line
        parts = line.split(",")
        if len(parts) < 2:
            continue
        if parts[1] != "" and parts[1] != ".":
            value, date = parts[1], parts[0]
    return value, date, warns


# ══════════════════════ 收盘价滞后天数 ════════════════════════════════════
def lag_days(date_str):
    """YYYY-MM-DD → 距今天数；无日期／解不了回 -1。

    与 shell 版一样用**本地时区**的午夜（oracle 把 TZ 钉在 UTC），
    且用截断除法（bash $(( )) 的 C 语意），不是 Python 的地板除。
    """
    if not date_str:
        return -1
    try:
        t = datetime.strptime(date_str, "%Y-%m-%d")
        epoch = int(time.mktime(t.timetuple()))
    except (ValueError, OverflowError):
        return -1
    return int((fx_epoch() - epoch) / 86400)


# ══════════════════════ 单一市场的完整读数 ════════════════════════════════
class Row(object):
    """一个市场的全部栏位——**具名**，不是位置。

    shell 版把这 16 个栏位当成位置式 TSV，同一份资料被 awk（$13/$14）、
    jq（.[12]/.[13]）与 shell（16 个位置变数）三种互不相容的方式定址，
    任一个栏位为空还会被 IFS=tab 悄悄往左折叠。这里一律具名。
    """
    __slots__ = ("market", "index", "close", "mark", "prev", "funding",
                 "f8", "fann", "oi", "notional", "vlm", "liquid",
                 "gap", "wrong", "close_date", "close_src")

    available = True                       # 与 MissingMarket 的判别栏位

    def __init__(self, market, index, close, mark, prev, funding, oi, vlm,
                 close_date, close_src):
        """mark/prev/funding/oi/vlm 一律是 canon_number() 出来的 **(字面, float)**。

        绝不再收原始上游值：型别正规化必须发生在**抽取**那一步，
        不能留到 padl()／Lit() 才炸。
        """
        self.market = market
        self.index = index
        self.close = close                     # 原始字面 or None
        self.mark = mark[0]
        self.prev = prev[0]
        self.funding = funding[0]
        self.oi = oi[0]
        self.close_date = close_date
        self.close_src = close_src

        f_mark = mark[1]
        f_oi = oi[1]
        f_fund = funding[1]
        # ⚠️ 每一个乘除出来的值都要再验 isfinite：输入有限**不代表**结果有限，
        #    而 inf 会一路走完所有比较，印成一个捏造的 ✅ 触发（见 NonFiniteDerivation）。
        f8 = finite_or_fail(f_fund * 8 * 100,          # 每小时 → 8h 口径百分比
                            "8h 资金费率（funding × 8 × 100）")
        fann = finite_or_fail(f8 * 3 * 365,            # 年化%
                              "年化资金费率（8h 费率 × 3 × 365）")
        notional = finite_or_fail(f_mark * f_oi,
                                  "名义 OI（markPx × openInterest）")

        self.f8 = "%.4f" % f8
        self.fann = "%.2f" % fann
        self.notional = "%.0f" % notional
        self.vlm = "%.0f" % vlm[1]
        self.liquid = notional >= OI_MIN_USD_F

        # 本档唯一一条「除数是变数」的除法就在下面，任何情况下都不许除以零。
        # close 为 None＝未提供；<= 0 与非有限值上游三个来源都已各自挡掉
        # （is_pos 连 NaN/inf 一起挡），这里再兜一次。
        fc = None
        if close is not None:
            try:
                fc = float(close)
            except (TypeError, ValueError):
                fc = None
            if fc is not None and not math.isfinite(fc):
                fc = None
        if fc is None or fc <= 0:
            self.gap = None
            self.wrong = None
        else:
            # 收盘价可以小到 5e-324（denormal）且仍然「> 0」，除出来就是 inf；
            # 不挡的话会算出一个 +inf% 的隐含跳空，凭空判成「取错市场」→ exit 4。
            g = finite_or_fail((f_mark / fc - 1) * 100,
                               "隐含跳空（markPx ÷ 上一收盘 − 1）")
            self.gap = "%.4f" % g
            self.wrong = (g > WRONG_MARKET_PCT_F) or (g < -WRONG_MARKET_PCT_F)

    # —— 供比较用的数值视图（显示一律用上面的字面）——
    @property
    def fann_f(self):
        return float(self.fann)

    @property
    def gap_f(self):
        return None if self.gap is None else float(self.gap)

    @property
    def notional_f(self):
        return float(self.notional)

    @property
    def vlm_f(self):
        return float(self.vlm)

    @property
    def lag(self):
        return lag_days(self.close_date)

    @property
    def stale(self):
        if self.close_date is None:
            return None
        d = self.lag
        return None if d < 0 else d > 1


# ══════════════════════ 取数 ══════════════════════════════════════════════
def fetch_hyperliquid():
    body = '{"type":"metaAndAssetCtxs","dex":"%s"}' % DEX
    code, raw, warns, failed = http_post(API, body)
    return code, raw, warns, failed


class MissingMarket(object):
    """一个**预期存在、但这次没取到可用读数**的市场。

    存在的唯一理由：让它在输出里占住位置。以前 `zip(universe, ctxs)` 长度不齐
    就直接少跑一圈、任一栏位坏掉就 `return None` 丢掉整份 payload——两种情况下
    市场都会**无声消失**：没有 ⚪️ 列、没有原因、ok:true、degraded:false、
    triggers 全空、exit 0。读的人分不出「查过、没事」与「根本没评估」，
    那正是本仓库所有 ⚪️ 记帐规则要挡的事。
    """
    __slots__ = ("market", "index", "close", "close_date", "close_src", "reason")
    available = False

    def __init__(self, market, index, close, close_date, close_src, reason):
        self.market = market
        self.index = index
        self.close = close
        self.close_date = close_date
        self.close_src = close_src
        self.reason = reason                   # 「哪里坏了」，一句话讲完

    @property
    def lag(self):
        return lag_days(self.close_date)


NEEDED_FIELDS = ("markPx", "prevDayPx", "funding", "openInterest", "dayNtlVlm")


def extract_units(payload, closes):
    """payload → (units, shape_error)。

    units 依 universe 顺序排列，**每个预期市场都会出现一次**：拿得到读数的是
    Row，拿不到的是 MissingMarket（带原因）。universe 里没出现过的预期市场，
    照 MARKETS 的顺序补在后面。
    shape_error 非 None 代表连 payload 的外壳都不对，整份没得救。
    """
    if not isinstance(payload, list):
        return None, "回传不是阵列"
    if len(payload) < 2:
        return None, "回传阵列只有 %d 个元素（预期 [meta, ctxs] 两个）" % len(payload)
    meta, ctxs = payload[0], payload[1]
    if not isinstance(meta, dict):
        return None, "回传的第 1 个元素不是物件（是 %s）" % type(meta).__name__
    if not isinstance(ctxs, list):
        return None, "回传的第 2 个元素不是阵列（是 %s）" % type(ctxs).__name__
    universe = meta.get("universe")
    if not isinstance(universe, list):
        return None, "meta.universe 不是阵列（是 %s）" % type(universe).__name__

    units = []
    seen = set()
    for i, u in enumerate(universe):
        if not isinstance(u, dict):
            continue
        name = u.get("name")
        if name not in INDEX_OF or name in seen:
            continue
        seen.add(name)
        close, cdate, csrc = closes[name]

        def missing(reason):
            return MissingMarket(name, INDEX_OF[name], close, cdate, csrc, reason)

        # ⚠️ 这里刻意不用 zip(universe, ctxs)：zip 遇到较短的 ctxs 会**静默**
        #    少跑几圈，市场就这样消失了。长度不齐要当成取数失败讲出来。
        if i >= len(ctxs):
            units.append(missing(
                "assetCtxs 只有 %d 笔、universe 有 %d 笔，对不上这个市场的第 %d 个位置"
                % (len(ctxs), len(universe), i + 1)))
            continue
        ctx = ctxs[i]
        if not isinstance(ctx, dict):
            units.append(missing("对应的 assetCtx 不是物件（是 %s）" % type(ctx).__name__))
            continue

        fields = {}
        bad = None
        for key in NEEDED_FIELDS:
            lit, val = canon_number(ctx.get(key))
            if lit is None:
                # 逐**市场**降级，不是逐 payload 作废：另一个市场是好的就照常输出。
                # 原因要指名道姓讲是哪个栏位怎么坏了——讲成「找不到市场／可能改名」
                # 会把人送去追一个根本没发生的改名（见档头决定二）。
                bad = "%s %s" % (key, val)
                break
            fields[key] = (lit, val)
        if bad is not None:
            units.append(missing(bad))
            continue

        try:
            units.append(Row(market=name, index=INDEX_OF[name],
                             close=close, mark=fields["markPx"],
                             prev=fields["prevDayPx"], funding=fields["funding"],
                             oi=fields["openInterest"], vlm=fields["dayNtlVlm"],
                             close_date=cdate, close_src=csrc))
        except NonFiniteDerivation as exc:
            # 栏位本身都是合法有限数，却在乘除那一步溢位 → 与「栏位坏掉」同一种
            # 降级：这个市场 ⚪️ 数据暂缺，另一个照常输出。绝不留一个带 inf 的 Row。
            units.append(missing(exc.reason))

    for name, idx, _keys in MARKETS:
        if name in seen:
            continue
        close, cdate, csrc = closes[name]
        units.append(MissingMarket(
            name, idx, close, cdate, csrc,
            "未出现在 xyz 池的 universe 里（可能是 Hyperliquid 改了市场名，或 dex 参数失效）"))
    return units, None


def market_absent(unit):
    """这个 ⚪️ 是「整个市场不在 universe 里」还是「读数坏掉」？"""
    return (not unit.available) and unit.reason.startswith("未出现在 xyz 池")


# ══════════════════════ 输出 ══════════════════════════════════════════════
# ⚪️ 栏位在表格与判定行里的统一写法。文字输出会被照抄进报告，
# 所以「没取到」必须长得跟「取到了」明显不同，且绝不可以是 0 或空白。
NA_CELL = "⚪️暂缺"

# 相对强弱为 null 时必须配一句理由，否则读的人分不清「没算」与「算出 0」。
# 三种理由的措辞在文字分支与 --json 分支共用同一份常数，不会各自漂。
RELSTR_NA_WRONG = "有市场被撞名检查判定取错，两边偏离不可比"
RELSTR_NA_MISSING = "有市场本次未取到读数 ⚪️ 数据暂缺，两边偏离不可比"
RELSTR_NA_NOCLOSE = "两个指数收盘价须都提供"
# 两边跳空各自有限、相减却溢位（|跳空| 逼近 1e308 的极端上游）。
# 印一个 ±inf 出来会变成一句「科技/AI 领涨」的捏造结论，所以一律 ⚪️。
RELSTR_NA_NONFINITE = "两边偏离相减后不是有限数字（上游数量级异常），不可比"


def render_text(units, relstr, wrong_seen, spx_close, ndx_close,
                relstr_nonfinite=False):
    rows = [u for u in units if u.available]
    gone = [u for u in units if not u.available]
    out = []
    out.append("【信号 18】美股 24/7 永续　来源：%s（dex=%s）" % (API, DEX))
    out.append("口径：指数点位 1:1｜资金费率为每小时结算，已换算成 8h 与年化")
    out.append("")
    out.append("  " + padr("市场", 12) + " " + padr("对照", 7) + " "
               + padl("标记价", 12) + " " + padl("上一收盘", 12) + " "
               + padl("隐含跳空%", 12) + " " + padl("年化费率%", 10) + " "
               + padl("名义OI($M)", 12) + " " + padl("日成交($M)", 14))
    for u in units:
        cl = "—" if u.close is None else u.close
        if not u.available:
            # 没读数的市场**照样占一列**：整列 ⚪️，绝不从表格里消失。
            out.append("  " + padr(u.market, 12) + " " + padr(u.index, 7) + " "
                       + padl(NA_CELL, 12) + " " + padl(cl, 12) + " "
                       + padl(NA_CELL, 12) + " " + padl(NA_CELL, 10) + " "
                       + padl(NA_CELL, 12) + " " + padl(NA_CELL, 14))
            continue
        r = u
        gap = NA_CELL if r.gap is None else "%+.2f" % r.gap_f
        out.append("  " + padr(r.market, 12) + " " + padr(r.index, 7) + " "
                   + padl(r.mark, 12) + " " + padl(cl, 12) + " " + padl(gap, 12)
                   + " " + padl("%.2f" % r.fann_f, 10) + " "
                   + padl("%.1f" % (r.notional_f / 1e6), 12) + " "
                   + padl("%.1f" % (r.vlm_f / 1e6), 14))
    if gone:
        out.append("")
        out.append("⚪️ 本次未取到读数的市场　来源：%s（dex=%s）" % (API, DEX))
        for u in gone:
            out.append("  " + padr(u.market, 12) + " ⚪️ 数据暂缺 —— " + u.reason)
        out.append("     → 名义 OI／隐含跳空／撞名检查／资金费率全部无法判定，"
                   "不计入任何触发，也不得写进报告。")
        out.append("     ⚠️ 不要因此退回主池：主池的 SPX 是 SPX6900 迷因币，取到的数完全无关。")
    out.append("")
    out.append("上一美股收盘来源：")
    for r in units:
        if r.close is None:
            out.append("  " + padr(r.market, 12) + " %s：未提供 → 隐含跳空无法判定" % r.index)
        else:
            out.append("  " + padr(r.market, 12) + " %s = %s（来源：%s，观测日 %s）"
                       % (r.index, r.close, r.close_src,
                          "NA" if r.close_date is None else r.close_date))
    for r in rows:
        if r.close_date is None:
            continue
        d = r.lag
        if d > 1:
            out.append("  ⚠️ %s 的收盘价观测日 %s 已滞后 %d 天——隐含跳空是拿 perp 现价对**旧收盘**算的，"
                       % (r.market, r.close_date, d))
            out.append("     不是「对上一收盘」。报告里必须写明这个滞后，或改传当日实际收盘。")
    out.append("")

    out.append("两个必须保留的检查：")
    for r in units:
        if not r.available:
            out.append("  ① 流动性  " + padr(r.market, 12)
                       + " ⚪️ 无法判定 —— 本次未取到读数（" + r.reason + "）")
        elif r.liquid:
            out.append("  ① 流动性  " + padr(r.market, 12)
                       + " 名义 OI $%.1fM ≥ $%.0fM 门槛 → 可用"
                       % (r.notional_f / 1e6, OI_MIN_USD_F / 1e6))
        else:
            out.append("  ① 流动性  " + padr(r.market, 12)
                       + " 名义 OI $%.1fM < $%.0fM 门槛 → **标「不适用」**，池子可能在迁移，报价不可信"
                       % (r.notional_f / 1e6, OI_MIN_USD_F / 1e6))
    for r in units:
        if not r.available:
            out.append("  ② 撞名    " + padr(r.market, 12)
                       + " ⚪️ 无法判定 —— 本次未取到读数（已强制 dex=xyz，未落主池）")
        elif r.wrong is None:
            out.append("  ② 撞名    " + padr(r.market, 12)
                       + " ⚪️ 未提供 %s 收盘价，无法做 >%.0f%% 撞名检查（已强制 dex=xyz，未落主池）"
                       % (r.index, WRONG_MARKET_PCT_F))
        elif r.wrong:
            out.append("  ② 撞名    " + padr(r.market, 12)
                       + " 🔴 与 %s 收盘差 %+.2f%%（>%.0f%%）→ **判定取错市场，本市场整笔作废**"
                       % (r.index, r.gap_f, WRONG_MARKET_PCT_F))
        else:
            out.append("  ② 撞名    " + padr(r.market, 12)
                       + " 与 %s 收盘差 %+.2f%%，在 ±%.0f%% 内 → 市场正确"
                       % (r.index, r.gap_f, WRONG_MARKET_PCT_F))
    out.append("")

    out.append("阈值判定：")
    for r in units:
        if not r.available:
            out.append("  隐含跳空  " + padr(r.market, 12)
                       + " ⚪️ 无法判定 —— 本次未取到读数")
        elif r.wrong:
            # 取错市场 → 整笔作废，**不得计任何触发**（否则一个错市场会凭空造出 Tier 2 触发）
            out.append("  隐含跳空  " + padr(r.market, 12)
                       + " 🔴 作废（撞名检查判定取错市场）→ 不计触发")
        elif r.gap is None:
            out.append("  隐含跳空  " + padr(r.market, 12)
                       + " ⚪️ 无法判定 —— 未提供上一美股收盘价")
        else:
            a = abs(r.gap_f)
            if a >= GAP_TIER2_PCT_F:
                out.append("  隐含跳空  " + padr(r.market, 12)
                           + " ✅ 触发（%+.2f%%，|偏离| ≥ %.0f%%）→ 计 1 个 Tier 2 触发"
                           % (r.gap_f, GAP_TIER2_PCT_F))
            elif a >= GAP_MENTION_PCT_F:
                out.append("  隐含跳空  " + padr(r.market, 12)
                           + " ❌ 未触发（%+.2f%%），但 ≥%.0f%% → **解读里必须点名**"
                           % (r.gap_f, GAP_MENTION_PCT_F))
            else:
                out.append("  隐含跳空  " + padr(r.market, 12)
                           + " ❌ 未触发（%+.2f%%）" % r.gap_f)

    if relstr is None:
        if wrong_seen > 0:
            out.append("  相对强弱  🔴 无法判定（有市场被撞名检查判定取错，两边偏离不可比）")
        elif relstr_nonfinite:
            out.append("  相对强弱  ⚪️ 无法判定（%s）" % RELSTR_NA_NONFINITE)
        elif gone:
            out.append("  相对强弱  ⚪️ 无法判定（%s）" % RELSTR_NA_MISSING)
        else:
            out.append("  相对强弱  ⚪️ 无法判定（两个指数收盘价须都提供）")
    else:
        r = float(relstr)
        if abs(r) >= RELSTR_PCT_F:
            out.append("  相对强弱  XYZ100 − SP500 = %+.2f pt（≥%.1f pt）→ **必须点名**：%s"
                       % (r, RELSTR_PCT_F, "科技/AI 领涨" if r > 0 else "科技在拖累大盘"))
        else:
            out.append("  相对强弱  XYZ100 − SP500 = %+.2f pt（<%.1f pt，无须点名）"
                       % (r, RELSTR_PCT_F))

    for r in units:
        if not r.available:
            out.append("  资金费率  " + padr(r.market, 12)
                       + " ⚪️ 无法判定 —— 本次未取到读数")
        elif r.fann_f > FUND_HOT_ANNUAL_F:
            out.append("  资金费率  " + padr(r.market, 12)
                       + " ✅ 触发：年化 %+.2f%% > +%.0f%% → 真实多头拥挤"
                       % (r.fann_f, FUND_HOT_ANNUAL_F))
        elif r.fann_f < FUND_COLD_ANNUAL_F:
            out.append("  资金费率  " + padr(r.market, 12)
                       + " ✅ 触发：年化 %+.2f%% < %.0f%% → 强烈对冲需求"
                       % (r.fann_f, FUND_COLD_ANNUAL_F))
        else:
            out.append("  资金费率  " + padr(r.market, 12)
                       + " ❌ 未触发：年化 %+.2f%%（基线 hourly 0.00000625 = 年化 +5.48%%，纯粹是利率，无方向信号）"
                       % r.fann_f)

    if not spx_close or not ndx_close:
        out.append("")
        out.append("提示：隐含跳空要有「上一美股收盘」才算得出来。请传 --spx / --ndx，或用 --closes 读")
        out.append("      姊妹技能 ai-pullback-daily 的 technicals.py --json 输出。")
        out.append("      **不要拿 prevDayPx 当收盘价**——那是 perp 自己 24 小时前的报价，不是现货收盘。")
    return "\n".join(out) + "\n"


def degraded_reasons(units, fred_failed):
    """降级原因：措辞照抄文字分支，一行一条，且一定点名是哪个市场。"""
    reasons = []
    for r in units:
        if not r.available:
            # ⚪️ 的市场也必须在这里留下一条：--json 的读者只看得到这个阵列，
            # 少一条就等於「查过、没事」。来源与坏在哪都要写进同一句。
            reasons.append("%s ⚪️ 数据暂缺 —— 来源 %s（dex=%s）：%s → 本市场不计入任何触发，不得写进报告"
                           % (r.market, API, DEX, r.reason))
            continue
        if r.wrong:
            reasons.append("%s 🔴 与 %s 收盘差 %+.2f%%（>%.0f%%）→ 判定取错市场，本市场整笔作废，不得写进报告"
                           % (r.market, r.index, r.gap_f, WRONG_MARKET_PCT_F))
        if not r.liquid:
            reasons.append("%s 名义 OI $%.1fM < $%.0fM 门槛 → 标「不适用」，池子可能在迁移，报价不可信"
                           % (r.market, r.notional_f / 1e6, OI_MIN_USD_F / 1e6))
        if r.gap is None:
            reasons.append("%s ⚪️ 未提供 %s 收盘价 → 隐含跳空无法判定，>%.0f%% 撞名检查也做不了"
                           % (r.market, r.index, WRONG_MARKET_PCT_F))
    for r in units:
        if r.available and r.close_date is not None and r.lag > 1:
            reasons.append("%s 的收盘价观测日 %s 已滞后 %d 天——隐含跳空是拿 perp 现价对旧收盘算的，"
                           "不是「对上一收盘」；报告里必须写明这个滞后，或改传当日实际收盘"
                           % (r.market, r.close_date, r.lag))
    if fred_failed:
        reasons.append("--from-fred 两个序列都没取到（FRED SP500 / NASDAQ100）；隐含跳空标 ⚪️ 无法判定")
    return reasons


def render_json(units, relstr, wrong_seen, wrong_any, fred_failed,
                relstr_nonfinite=False):
    """--json 不等于「不报警」：文字分支印的每一条告警与禁令，这里都要有一个栏位。"""
    reasons = degraded_reasons(units, fred_failed)
    rows = [u for u in units if u.available]
    gone = [u for u in units if not u.available]

    market_objs = []
    for r in units:
        lag = r.lag
        if not r.available:
            # ⚪️ 市场也留在 markets[] 里，**绝不从阵列里消失**。
            # 用 available=false + unavailable_reason 两个只有它才有的键当判别栏位：
            # 正常列一个键都没多，既有消费端逐位元组不变；
            # 想过滤的人用 `.available == false`，没改过的人也不会把 null 当数字用。
            market_objs.append({
                "market": r.market,
                "index": r.index,
                "available": False,
                "unavailable_reason": r.reason,
                "cash_close": None if r.close is None else Lit(r.close),
                "mark": None,
                "prev_day_px": None,
                "funding_hourly": None,
                "funding_8h_pct": None,
                "funding_annual_pct": None,
                "open_interest": None,
                "notional_oi_usd": None,
                "day_ntl_vlm_usd": None,
                "liquid": None,
                "implied_gap_pct": None,
                "wrong_market_suspected": None,
                "cash_close_date": r.close_date,
                "cash_close_source": r.close_src,
                "cash_close_lag_days": None if r.close_date is None or lag < 0 else lag,
                "cash_close_stale": None,
            })
            continue
        market_objs.append({
            "market": r.market,
            "index": r.index,
            "cash_close": None if r.close is None else Lit(r.close),
            "mark": Lit(r.mark),
            "prev_day_px": Lit(r.prev),
            "funding_hourly": Lit(r.funding),
            "funding_8h_pct": Lit(r.f8),
            "funding_annual_pct": Lit(r.fann),
            "open_interest": Lit(r.oi),
            "notional_oi_usd": Lit(r.notional),
            "day_ntl_vlm_usd": Lit(r.vlm),
            "liquid": r.liquid,
            "implied_gap_pct": None if r.gap is None else Lit(r.gap),
            "wrong_market_suspected": None if r.wrong is None else bool(r.wrong),
            "cash_close_date": r.close_date,
            "cash_close_source": r.close_src,
            "cash_close_lag_days": None if r.close_date is None or lag < 0 else lag,
            "cash_close_stale": r.stale,
        })

    # 相对强弱为 null 时必须配一句理由，否则读的人分不清「没算」与「算出 0」。
    relna = None
    if relstr is None:
        if wrong_seen > 0:
            relna = RELSTR_NA_WRONG
        elif relstr_nonfinite:
            relna = RELSTR_NA_NONFINITE
        elif gone:
            relna = RELSTR_NA_MISSING
        else:
            relna = RELSTR_NA_NOCLOSE

    doc = {
        # 有市场没取到读数时 ok 一定是 false：ok:true + 少一个市场
        # 正是「静默缩水」最难被发现的形态。
        "ok": wrong_any == 0 and not gone,
        "signal": 18,
        "name": "美股 24/7 永续",
        "source": API + " dex=" + DEX,
        "degraded": len(reasons) > 0 or wrong_any != 0,
        "degraded_reasons": reasons,
        "thresholds": {
            "notional_oi_min_usd": Lit(OI_MIN_USD),
            "gap_tier2_pct": Lit(GAP_TIER2_PCT),
            "gap_mention_pct": Lit(GAP_MENTION_PCT),
            "relative_strength_pt": Lit(RELSTR_PCT),
            "funding_annual_hot_pct": Lit(FUND_HOT_ANNUAL),
            "funding_annual_cold_pct": Lit(FUND_COLD_ANNUAL),
            "wrong_market_pct": Lit(WRONG_MARKET_PCT),
        },
        "markets": market_objs,
        "relative_strength_pt": None if relstr is None else Lit(relstr),
        "relative_strength_na_reason": relna,
        "triggers": {
            "gap_tier2": [r.market for r in rows
                          if r.wrong is not True and r.gap is not None
                          and abs(r.gap_f) >= GAP_TIER2_PCT_F],
            "gap_mention": [r.market for r in rows
                            if r.wrong is not True and r.gap is not None
                            and abs(r.gap_f) >= GAP_MENTION_PCT_F],
            "gap_not_evaluated": [
                {"market": r.market,
                 "reason": ("⚪️ 数据暂缺 —— 本次未取到 perp 读数（%s）" % r.reason
                            if not r.available
                            else "🔴 作废（撞名检查判定取错市场）→ 不计触发" if r.wrong is True
                            else "⚪️ 无法判定 —— 未提供上一美股收盘价")}
                for r in units if (not r.available) or r.wrong is True or r.gap is None],
            "funding_crowded_long": [r.market for r in rows
                                     if r.fann_f > FUND_HOT_ANNUAL_F],
            "funding_hedging_demand": [r.market for r in rows
                                       if r.fann_f < FUND_COLD_ANNUAL_F],
            "illiquid": [r.market for r in rows if r.liquid is False],
            "stale_cash_close": [r.market for r in rows if r.stale is True],
            "wrong_market": [r.market for r in rows if r.wrong is True],
        },
        "notes": {
            "cash_close_fetch": "Yahoo chart 端点本机实测稳定回 HTTP 429（带 UA、带 cookie jar 都一样），stooq CSV 端点已下线 —— 两者都不可靠，本脚本不用。最准的还是呼叫方直接传 --spx / --ndx；--from-fred 免 API key 但滞后 1 个交易日。绝不自己编一个收盘价。",
            "prev_day_px": "**不要拿 prevDayPx 当收盘价**——那是 perp 自己 24 小时前的报价，不是现货收盘。",
            "wrong_market": "被判定取错市场的市场，数字整笔作废，不得写进报告。先确认传入的收盘价口径是不是指数点位（^GSPC ≈7,7xx 而非 SPY ≈77x；^NDX ≈29,xxx 而非 QQQ ≈7xx）。",
            "dex": "必须带 \"dex\":\"xyz\"：Hyperliquid 主池的 SPX 是 SPX6900 迷因币，不是标普 500。",
        },
    }
    return jq_dumps(doc) + "\n"


# ══════════════════════ main ══════════════════════════════════════════════
def _report_lost(rc):
    """报告一个字都没写到 stdout（`>&-`、管线被下游关掉）时的处理。

    静默 return 0 是「消失」的另一种写法：呼叫方拿到一个成功的退出码，却什么
    都没收到。写不出去就是没交出读数 → 至少 3；撞名的 4 优先序更高，保留。
    """
    warn("⚪️ 信号 18 报告一个字都没写到 stdout（输出通道不可用）—— 视同未取得读数。")
    return rc if rc >= 3 else 3


def main(argv):
    # --help 一律先答：说明书不该因为少装套件就看不到（与 fetch_fundamentals.py 同规矩）。
    # 这里**只**用来决定要不要跳过依赖检查；真正处理 -h/--help 的是 parse_argv()，
    # 它照 shell 版的顺序逐一扫 argv，所以 `--nope --help` 仍旧先死在 --nope 上。
    help_wanted = ("-h" in argv) or ("--help" in argv)

    # 依赖检查在**主线程**、开工前跑一次（shell 版同样把 command -v 放在最上面）。
    # fixture 回放模式下整支程式不碰传输层，requests 当下就不是依赖，因此跳过——
    # 这不会让任何失败被藏起来：回放模式下根本没有网络请求可失败。
    if not FIXTURE_DIR and not help_wanted:
        ensure_requests()

    # 扫描 argv：验证与赋值在 parse_argv() 里绑在一起（见档头决定一）。
    # 这里不再有第二段「拿 argv 决定要不要采用 args.spx」的逻辑——
    # 那正是把一个有传的收盘价整个丢掉的地方。
    args = parse_argv(argv)

    spx_close = args.spx
    ndx_close = args.ndx
    spx_date = ndx_date = None
    spx_src = "--spx 参数" if spx_close is not None else "未提供"
    ndx_src = "--ndx 参数" if ndx_close is not None else "未提供"
    fred_failed = False               # --from-fred 两个序列都没取到 → 降级原因之一

    if FIXTURE_DIR and not os.path.isdir(FIXTURE_DIR):
        die("RISK_FIXTURE_DIR 指向的目录不存在：%s" % scrub(FIXTURE_DIR), 1)

    # ── --closes：容错读取（容器键只展开一层）──
    if args.closes:
        try:
            with open(args.closes, "r", encoding="utf-8") as f:
                doc = json.load(f, parse_float=RawFloat, parse_int=RawInt,
                                parse_constant=NonFinite)
        except Exception:                       # noqa: BLE001
            # 档案存在与否已经在 parse_argv() 里查过了，所以走到这里一定是
            # 内容问题（解不开／读不了），措辞不会再指错方向。
            die("--closes 指定的档案不是合法 JSON。", 1)
        # 档案里读到 0／负数一样是参数错误：宁可当场喊停，也不要带着坏值去除。
        for who, (idx_name, keys, cur) in (
                ("spx", ("^GSPC", MARKETS[0][2], spx_close)),
                ("ndx", ("^NDX", MARKETS[1][2], ndx_close))):
            if cur is not None:
                continue
            v, bad = read_close(doc, keys)
            if v is None:
                if bad is not None:
                    # NaN／Infinity：绝不当成「未提供」静默略过，也绝不拿去当除数。
                    die("--closes 档案里的 %s 收盘价不是有限数字，读到「%s」。"
                        % (idx_name, clip(bad)), 1)
                continue
            if not is_pos(v):
                die("--closes 档案里的 %s 收盘价必须大于 0，读到「%s」。"
                    % (idx_name, clip(v)), 1)
            if who == "spx":
                spx_close, spx_src = v, "--closes 档案"
            else:
                ndx_close, ndx_src = v, "--closes 档案"

    # ── 取数：三个互不相依的请求同时发，回来后**按固定顺序**处理 ──
    # 平行只影响等待时间，不影响任何输出：每个请求各自把 stderr 讯息装进自己的
    # buffer，主线程再依 FRED SP500 → FRED NASDAQ100 → Hyperliquid 的顺序倾倒，
    # 与 shell 版的先后完全一致。谁先回来完全不影响结果。
    with ThreadPoolExecutor(max_workers=3) as pool:
        f_spx = pool.submit(fred_last, "SP500") \
            if (args.from_fred and spx_close is None) else None
        f_ndx = pool.submit(fred_last, "NASDAQ100") \
            if (args.from_fred and ndx_close is None) else None
        f_hl = pool.submit(fetch_hyperliquid)

        r_spx = f_spx.result() if f_spx else None
        r_ndx = f_ndx.result() if f_ndx else None
        hl_code, hl_raw, hl_warns, hl_fail_reason = f_hl.result()

    if args.from_fred:
        # FRED 理论上不会回 0／负数，但真回了就是取数出问题，不是参数问题：
        # 一律当成「没取到」走 ⚪️，绝不拿去当除数。
        if r_spx is not None:
            v, d, warns = r_spx
            for w in warns:
                warn(w)
            if v is not None:
                if is_pos_num(v):
                    spx_close, spx_date, spx_src = v, d, "FRED SP500"
                else:
                    warn("⚠️ FRED SP500 回的收盘价「%s」不是正数，不采用（隐含跳空将标 ⚪️ 无法判定）。" % v)
        if r_ndx is not None:
            v, d, warns = r_ndx
            for w in warns:
                warn(w)
            if v is not None:
                if is_pos_num(v):
                    ndx_close, ndx_date, ndx_src = v, d, "FRED NASDAQ100"
                else:
                    warn("⚠️ FRED NASDAQ100 回的收盘价「%s」不是正数，不采用（隐含跳空将标 ⚪️ 无法判定）。" % v)
        if spx_close is None and ndx_close is None:
            fred_failed = True
            warn("⚠️ --from-fred 两个序列都没取到（FRED SP500 / NASDAQ100）。隐含跳空将标 ⚪️ 无法判定。")

    for w in hl_warns:
        warn(w)

    # 传输层例外与「HTTP 码不对／结构不对」是**两条不同的讯息**，不可合并：
    # 前者代表根本没连上，后者代表连上了但回来的东西不能用。
    if hl_fail_reason is not None:
        warn("⚪️ 信号 18 数据暂缺 —— 已尝试来源：%s（dex=%s）" % (API, DEX))
        warn("   （传输层最后一次失败：%s）" % hl_fail_reason)
        die("Hyperliquid 连线失败（传输层例外）。不得以记忆或推断填补。", 3)

    payload = None
    if hl_code == "200":
        try:
            payload = json.loads(hl_raw.decode("utf-8"),
                                 parse_float=RawFloat, parse_int=RawInt,
                                 parse_constant=NonFinite)
        except Exception:                       # noqa: BLE001
            payload = None
    if hl_code != "200" or not isinstance(payload, list):
        warn("⚪️ 信号 18 数据暂缺 —— 已尝试来源：%s（dex=%s），回 HTTP %s" % (API, DEX, hl_code))
        die("Hyperliquid 回传不是预期的阵列结构。不得以记忆或推断填补。", 3)

    closes = {
        "xyz:SP500": (spx_close, spx_date, spx_src),
        "xyz:XYZ100": (ndx_close, ndx_date, ndx_src),
    }
    units, shape_err = extract_units(payload, closes)
    if units is None:
        warn("⚪️ 信号 18 数据暂缺 —— 已尝试来源：%s（dex=%s），回 HTTP %s" % (API, DEX, hl_code))
        warn("   阵列外壳不符预期：%s。" % shape_err)
        die("Hyperliquid 回传不是预期的阵列结构。不得以记忆或推断填补。", 3)

    rows = [u for u in units if u.available]
    gone = [u for u in units if not u.available]

    if not rows:
        # 一个市场都没取到 → 没有报告可印（与 shell 版一致：stdout 空、exit 3）。
        # 但**诊断要说实话**：全部都是「名字不在 universe 里」才沿用
        # shell 版那三行「可能改了市场名」；只要有一个是栏位坏掉，就照实讲，
        # 否则等於把人送去追一个根本没发生的改名（档头决定二）。
        if all(market_absent(u) for u in gone):
            warn("⚪️ 信号 18 数据暂缺 —— xyz 池里找不到 xyz:SP500 / xyz:XYZ100。")
            warn("   可能是 Hyperliquid 改了市场名，或 dex 参数失效。")
            warn("   ⚠️ 不要因此退回主池：主池的 SPX 是 SPX6900 迷因币，取到的数完全无关。")
            die("不得以记忆或推断填补。", 3)
        warn("⚪️ 信号 18 数据暂缺 —— 已尝试来源：%s（dex=%s），回 HTTP %s" % (API, DEX, hl_code))
        for u in gone:
            warn("   %s ⚪️ %s" % (u.market, u.reason))
        warn("   ⚠️ 不要因此退回主池：主池的 SPX 是 SPX6900 迷因币，取到的数完全无关。")
        die("两个市场都没取到可用读数，不得以记忆或推断填补。", 3)

    # 有市场没取到读数时**先大声讲**，再照常印另一个市场的完整报告。
    for u in gone:
        warn("⚠️ %s 本次未取到可用读数 —— 来源 %s（dex=%s）：%s"
             % (u.market, API, DEX, u.reason))
        warn("   该市场以 ⚪️ 数据暂缺 记录，不计入任何触发，也不得写进报告。")

    wrong_seen = sum(1 for r in rows if r.wrong is True)
    wrong_any = 1 if wrong_seen else 0

    # 任一市场被判定取错，相对强弱就不可信 → 一律 NA
    by_market = {r.market: r for r in rows}
    spx_gap = by_market["xyz:SP500"].gap if "xyz:SP500" in by_market else None
    ndx_gap = by_market["xyz:XYZ100"].gap if "xyz:XYZ100" in by_market else None
    relstr = None
    relstr_nonfinite = False
    if spx_gap is not None and ndx_gap is not None and wrong_seen == 0:
        # 相对强弱也是一个**派生量**：两个有限跳空相减照样能溢位。
        diff = float(ndx_gap) - float(spx_gap)
        if math.isfinite(diff):
            relstr = "%.4f" % diff
        else:
            relstr_nonfinite = True
            warn("⚠️ 相对强弱（XYZ100 − SP500）算出来不是有限数字，两边偏离不可比 → ⚪️ 无法判定。")

    # 退出码优先序：4（撞名）> 3（有市场 ⚪️ 数据暂缺）> 0。见档头决定三。
    rc = 4 if wrong_any else (3 if gone else 0)

    if args.json:
        # 先整份组好再一次写出：绝不边算边写，例外时才不会留下截断的 JSON。
        doc = render_json(units, relstr, wrong_seen, wrong_any, fred_failed,
                          relstr_nonfinite=relstr_nonfinite)
        return rc if stdout_write(doc) else _report_lost(rc)

    if not stdout_write(render_text(units, relstr, wrong_seen, spx_close, ndx_close,
                                    relstr_nonfinite=relstr_nonfinite)):
        rc = _report_lost(rc)

    if wrong_any:
        warn("")
        warn("⚠️ 有市场触发「取错市场」判定（与现货收盘差 >%s%%）。" % WRONG_MARKET_PCT)
        warn("   该市场的数字整笔作废，不得写进报告。先确认传入的收盘价口径是不是指数点位")
        warn("   （^GSPC ≈7,7xx 而非 SPY ≈77x；^NDX ≈29,xxx 而非 QQQ ≈7xx）。")
    return rc


# ══════════════════════ top-level 兜底 ════════════════════════════════════
# 契约：**任何**没预期到的例外都不准以 traceback 的形式跑到使用者面前。
#   · traceback 会印出 /Users/<名字>/… 的绝对路径 —— 公开仓库明令禁止；
#   · 退出码会变成 1（本仓库的「参数错误」），把取数／内部问题谎报成呼叫方打错字；
#   · --json 时 stdout 可能是空的或半截，机器端读到截断文件比读到 ⚪️ 危险得多。
# 所以一律折成「⚪️ 数据暂缺 + exit 3」，讯息过 scrub()，
# --json 分支补一份**完整**的 JSON（两个市场都以 ⚪️ 占位，绝不静默缩水）。
_STDOUT_DONE = False


def stdout_write(text):
    """写 stdout；回传**是否真的写出去了**。绝不 raise（见 _emit）。"""
    global _STDOUT_DONE
    ok = _emit(1, text)
    if ok:
        _STDOUT_DONE = True
    return ok


def fatal_json(reason):
    """内部错误时的完整 JSON：两个预期市场都以 ⚪️ 占位。"""
    units = [MissingMarket(name, idx, None, None, "未提供", reason)
             for name, idx, _keys in MARKETS]
    return render_json(units, None, 0, 0, False)


# 最后一道防线：**在 import 时就组好、之后一个字都不再算**的常数。
# 走到这里代表连「组一份 JSON」都失败了，所以它不能含任何格式化、任何变数、
# 任何 import——只剩把一串既有的位元组写到 fd 1。
# 内容仍旧**列出两个市场**：markets:[] 会让消费端读成「这次没有任何市场要看」，
# 那正是静默缩水；ok:false + available:false 才读得出「没评估到」。
_LAST_RESORT_JSON = """{
  "ok": false,
  "signal": 18,
  "name": "美股 24/7 永续",
  "source": "https://api.hyperliquid.xyz/info dex=xyz",
  "degraded": true,
  "degraded_reasons": [
    "本脚本内部错误 —— 连错误讯息本身都组不出来；取数未完成，不得以记忆或推断填补"
  ],
  "markets": [
    {
      "market": "xyz:SP500",
      "index": "^GSPC",
      "available": false,
      "unavailable_reason": "本脚本内部错误 —— 取数未完成，不得以记忆或推断填补"
    },
    {
      "market": "xyz:XYZ100",
      "index": "^NDX",
      "available": false,
      "unavailable_reason": "本脚本内部错误 —— 取数未完成，不得以记忆或推断填补"
    }
  ]
}
"""
_LAST_RESORT_JSON_BYTES = _LAST_RESORT_JSON.encode("utf-8")
_LAST_RESORT_STDERR_BYTES = (
    "⚪️ 信号 18 数据暂缺 —— 本脚本内部错误（连错误讯息本身都组不出来）。\n"
    "错误：不得以记忆或推断填补。\n").encode("utf-8")


def _last_resort(exc, argv):
    """兜底的兜底：**这个函式自己一行都不准 raise**。

    以前这里是一串「看起来很安全」的呼叫，每一个都会炸：
      · `clip(exc)` → `str(exc)`，而例外的 __str__ 本来就可以 raise；
      · `sys.stdout.write(...)` / `sys.stderr.write(...)`，而 `>&-` / `2>&-`
        会把这两个物件变成 **None**。
    任何一个炸掉，使用者拿到的就是**一份 traceback**：带 /Users/<名字>/… 的
    绝对路径（公开仓库明令禁止）、退出码 1（本仓库的「参数错误」，等於把内部
    错误谎报成呼叫方打错字）、--json 的 stdout 一个字都没有。
    「挡 traceback 的东西自己没被挡住」—— 本函式就是补这个洞。

    四段，每一段比上一段依赖更少：
      ① 组讯息：clip()／scrub() 已是全函式，路径在进任何讯息前就折掉了
      ② fatal_json()：完整 JSON，两个市场都以 ⚪️ 占位
      ③ json.dumps 的精简版（render_json 组不出来时）
      ④ _LAST_RESORT_JSON_BYTES：import 时就编码好的常数位元组
    退出码一律 **3**（取数未完成），绝不 1。
    """
    try:
        name = type(exc).__name__
    except BaseException:                       # noqa: BLE001
        name = "UnknownError"
    detail = "%s: %s" % (name, clip(exc, 200))          # clip 保证不 raise
    reason = "本脚本内部错误（%s）—— 取数未完成，不得以记忆或推断填补" % detail

    try:
        wants_json = "--json" in argv
    except BaseException:                       # noqa: BLE001
        wants_json = False

    if wants_json and not _STDOUT_DONE:
        doc = None
        try:
            doc = fatal_json(reason)                                    # ②
        except BaseException:                   # noqa: BLE001
            doc = None
        if doc is None:
            try:                                                        # ③
                doc = json.dumps(
                    {"ok": False, "signal": 18, "name": "美股 24/7 永续",
                     "source": API + " dex=" + DEX, "degraded": True,
                     "degraded_reasons": [reason],
                     "markets": [{"market": mname, "index": idx,
                                  "available": False,
                                  "unavailable_reason": reason}
                                 for mname, idx, _keys in MARKETS]},
                    ensure_ascii=False) + "\n"
            except BaseException:               # noqa: BLE001
                doc = None
        if not (doc is not None and _emit(1, doc)):
            _emit_bytes(1, _LAST_RESORT_JSON_BYTES)                     # ④

    # stderr 也一样分两段：组得出讯息就印讯息，组不出来就印常数。
    try:
        head = "⚪️ 信号 18 数据暂缺 —— 已尝试来源：%s（dex=%s）\n" % (API, DEX)
        body = "   本脚本内部错误：%s\n" % detail
        tail = "错误：不得以记忆或推断填补。\n"
    except BaseException:                       # noqa: BLE001
        _emit_bytes(2, _LAST_RESORT_STDERR_BYTES)
        return 3
    if not _emit(2, head):
        _emit_bytes(2, _LAST_RESORT_STDERR_BYTES)
        return 3
    _emit(2, body)
    _emit(2, tail)
    return 3


def guarded_main(argv):
    try:
        return main(argv)
    except HelpRequested:
        stdout_write(usage_text())
        return 0
    except SystemExit:
        raise                                   # die() / --help 的正常离开路径
    except KeyboardInterrupt:
        warn("")
        warn("⚪️ 信号 18 数据暂缺 —— 使用者中断（未取得任何读数）。")
        return 3
    except BaseException as exc:                # noqa: BLE001（理由见上）
        return _last_resort(exc, argv)


if __name__ == "__main__":
    # guarded_main 之外还有一层：兜底本身出事时，至少退出码要是 3（取数未完成），
    # 而不是 Python 预设的 1（本仓库的「参数错误」）。
    try:
        _RC = guarded_main(sys.argv[1:])
    except SystemExit:
        raise                                   # die() 的正常离开路径
    except BaseException:                       # noqa: BLE001
        _emit_bytes(2, _LAST_RESORT_STDERR_BYTES)
        _RC = 3
    if not isinstance(_RC, int) or isinstance(_RC, bool):
        _RC = 3
    # stdout 是坏掉的管线时，直译器结束时的自动 flush 会印一段
    # 「Exception ignored ... BrokenPipeError」并把退出码换成 120。
    # 这里先自己 flush 一次；flush 不掉就把 sys.stdout 拆掉，让结束流程别再试。
    try:
        if sys.stdout is not None:
            sys.stdout.flush()
    except BaseException:                       # noqa: BLE001
        try:
            sys.stdout = None
        except BaseException:                   # noqa: BLE001
            pass
    sys.exit(_RC)
