#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ETF 持仓取数（三级回退：Alpha Vantage → yfinance → 官网）——替代人工扒发行商官网这一步。

标的清单读 assets/universe.json 里 `etf: true` 的行，脚本内不内联任何 ticker、
不硬编码档数；增减 ETF 只改 universe.json，本脚本无需改动。

三级回退与「不留缓存」的理由（先读这一段）
------------------------------------------
每档独立走 **①Alpha Vantage ETF_PROFILE → ②yfinance top_holdings → ③官网提示**，
任一级成功即停，来源记进 `source` 字段并随每档输出打印。

**本脚本不保存任何持仓快照，也不从快照回退。** 每次都现取。理由：ETF 持仓会变
（三档 Roundhill 是主动管理、季度调仓），**陈旧快照比没有更危险**——它长得跟新鲜数据
一模一样，读者无从分辨，一份上个季度的持仓表会被当成现状引用。回退到一个**新鲜的次选源**
（并如实标注其口径），好过回退到一个**过期的首选源**。`--json` 是用户自传的落盘路径，
是导出不是缓存，脚本自己从不读回来。

口径红线（务必先读）
--------------------
**三级的口径不同，三级的数字绝不可拼进同一张表。**
- ① Alpha Vantage = **全量**持仓（实测 LYTE 23 笔 / NCLD 22 笔 / DRAM 26 笔）。
- ② yfinance = **只有 top-N**（实测 LYTE 0 笔、NCLD 0 笔——这两档太新 yfinance 没有；
  DRAM 5 笔；SMH 10 笔；SOXX 10 笔）。**不是全量**，故派生量只有一部分算得出来，
  见下方「top-N 口径下哪些派生量不可算」。
- ③ 官网 = **权威**（rating-rules.md 正文要求的就是它）。

一次运行里不同档可能落在不同级，汇总区按来源**分组列出并各自标注**，不混排成一张表。

top-N 口径下哪些派生量不可算（这是本脚本最容易被"优化"坏的地方）
----------------------------------------------------------------
- **前三大合计**：N≥3 时可算。它是「top-N 内的前三大」，而前三大本来就在 top-N 里，
  与全量口径同义 → **可用**。N<3 记 N/A。
- **前十大合计**：**N≥10 才可算**；DRAM 在 yfinance 只有 5 笔 → **记 N/A**，不得拿 5 笔充数。
- **swap 部位合计 / 非美股（无代码）成分合计 / 现金·国库券类合计**：**全部不可算**，
  一律 N/A 并注明「需全量持仓，top-N 无法计算」。rating-rules.md 里
  「行销页把 36% 国库券当成持仓、前十大≈82.9% 是误导」那个陷阱，**正是靠全量持仓才识破的**；
  拿 top-N 去算一个「现金类合计」只会原样复现该陷阱（top-N 里通常一笔现金都没有，
  算出来会是 0.00%，比 N/A 危险得多）。
- **权重合计**：top-N 口径下它是「前 N 大权重之和」，**不是基金权重总和**，按前者措辞打印。

AV 与官网的关系（不因本脚本存在而放宽）
----------------------------------------
**Alpha Vantage 不是官方持仓表。** 它是发行商官网持仓表的*替代取数*，不是它的等价物：
同一档 ETF，AV 的行项目拆分方式与官网可能不同（实测 LYTE：官网列 T-Bill 36.0% 抵押品，
AV 只给一笔货币基金 2.93% 外加一笔 CASH OFFSET −35.03%，两者是同一件事的两种记法）。
所以每档输出结尾固定打一行数据源声明，且 references/rating-rules.md 里
「持仓一律以官方持仓表为准」的规矩不因本脚本存在而放宽——本脚本只是把「抓不到就记 N/A」
里的「抓不到」变少，不改变「与官方冲突时以官方为准」。

数据缺失一律记 N/A，绝不估算：AV 的 net_assets/portfolio_turnover 等字段常是字符串
"n/a"，一律记 N/A，不得转成 0；某档全部 key 都限流取不到，记 N/A 并明说原因，
绝不留空当成「该档没有持仓」。

用法
----
    etf_holdings.py                       # universe.json 里 etf=true 的全部
    etf_holdings.py --tickers LYTE,NCLD   # 只取部分（调试/重跑用）
    etf_holdings.py --json out.json       # 额外把结构化结果写一份
    etf_holdings.py --check               # 只验 key 可用性与配额，不取全部
    etf_holdings.py --sleep 2.0           # 调每档之间的主动限速（默认 1.2 秒）

API key
-------
从环境变量 `AV_API_KEYS` 读，多个 key 用英文逗号分隔，例如：

    export AV_API_KEYS=k1,k2,k3

本仓库是 public repo：脚本内**不存在**任何硬编码 key，也不读配置文件，
**绝不回退到任何内置 key**。所有对外输出（含错误信息、异常文本、URL 回显）里的 key
一律遮罩成 `key#N(前4****后2)`，短 key 全遮。

变量没设 ≠ 报错退出：那只是「AV 取不到」的一种，**第一级整个跳过、直接从第二级
yfinance 起跑**（并在 stderr 大声说明降级后只有 top-N）。唯独 `--check` 例外——
它本来就只验 key，没 key 就退出 1。

限流处置（按实测行为实现，勿"优化"成看 HTTP 码）
------------------------------------------------
1. **限流回的是 HTTP 200**，body 是 `{"Information": "..."}`。按状态码判断会把限流
   当成数据写进持仓表。判定只看 body：dict 且含 `Information` / `Note` 键 → 不是数据。
2. 瞬时限流（实测文案）：
     "Thank you for using Alpha Vantage! Please consider spreading out your free API
      requests more sparingly (1 request per second). ... to lift the free key rate
      limit (25 requests per day), raise the per-second burst limit, ..."
   **注意这段瞬时文案里同时含 "per day" 与 "rate limit"**，所以关键词匹配必须
   *先判瞬时、后判日限*，顺序反了会把一次每秒限流误判成日配额耗尽、把好 key 全烧掉。
3. 瞬时 → 同一个 key 指数退避重试（默认最多 3 次），仍失败才换下一个 key。
   日配额耗尽 → 本次运行内直接标记该 key 已耗尽，不再使用，立刻换下一个。
   文案两者都不匹配（如 demo key 的提示）→ **保守当瞬时处理**，并把原文完整记进 warnings。
4. `Error Message` 键 = 参数/代码错误（symbol 拼错等），**不是限流**，不换 key。
5. 空 dict `{}` = 该 symbol 不是 ETF 或不存在（实测 NVDA / 不存在的代码都返回 `{}`），
   **不是限流**，不换 key，该档记 N/A。
6. 每档之间主动限速（实测连打第 2 次立即触发瞬时限流），默认间隔 1.2 秒。

第二级 yfinance（AV 全部 key 耗尽/取不到时才走）
------------------------------------------------
`yf.Ticker(sym, session=s).funds_data.top_holdings` → DataFrame，index 是代码，
列为 `Name` / `Holding Percent`（小数）。必须传 requests.Session + UA（与
fetch_fundamentals.py 同款理由：yfinance 默认 curl_cffi 引擎在本环境 TLS 失败，
标准库 urllib 则 CERTIFICATE_VERIFY_FAILED）。空 DataFrame / None / 抛异常一律视为
该级失败，落到第三级；**绝不把空结果当成「该档没有持仓」**。

第三级 官网（前两级都失败）
---------------------------
不静默记 N/A 了事：打印该档发行商持仓页入口、要看什么（全量持仓+权重、
以及是否有现金/国库券/swap 抵押品行项目），并明写「本次未取到，需人工或 WebSearch 补，
在补上之前该档持仓记 N/A，绝不估算」。

依赖: requests（必需，AV 与 yfinance 共用同一个 Session）；yfinance（仅第二级需要，
两者都是 lazy import，缺库时 --help 仍可用并给安装提示，不抛 traceback）。
所有路径以 __file__ 为锚，任意 cwd 下均可运行。

退出码: 请求的标的**一档都没取到**（三级全落空）时 1，其余 0
（部分失败在结尾按来源分组列出，便于按 --tickers 重跑）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
import unicodedata
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------- 路径与常量
# 脚本随技能分发，一律以 __file__ 相对定位，从任意 cwd 都能跑。
HERE = Path(__file__).resolve().parent
SKILL_ROOT = HERE.parent
UNIVERSE_PATH = SKILL_ROOT / "assets" / "universe.json"
# 刻意没有缓存文件：ETF 持仓会变，陈旧快照比没有更危险（详见文件头注释）。

API_BASE = "https://www.alphavantage.co/query"
API_FUNCTION = "ETF_PROFILE"

# 与 fetch_fundamentals.py 同一串 Chrome UA。
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

ENV_VAR = "AV_API_KEYS"
HTTP_TIMEOUT = 30
DEFAULT_SLEEP = 1.2          # 每档之间的主动限速（秒）；实测 1 req/sec 就会触发瞬时限流
DEFAULT_RETRIES = 3          # 同一个 key 上的最大尝试次数（含首次）
BACKOFF_BASE = 1.5           # 指数退避起点：1.5s -> 3.0s -> 6.0s

# 三级来源标识。落进 --json 的 `source` 字段，也决定每档结尾打哪一行数据源声明。
SRC_AV = "alphavantage"
SRC_YF = "yfinance"          # 展示时带上实际 N：yfinance(top-10)
SRC_NONE = "unavailable"

SOURCE_NOTE_AV = (
    "数据源：Alpha Vantage ETF_PROFILE（全量持仓，非官方持仓表）。"
    "与发行商官方持仓表冲突时以官方为准。"
)


def source_note_yf(n) -> str:
    n_txt = str(n) if n else "N"
    return (
        f"数据源：yfinance top_holdings（**仅前 {n_txt} 大，非全量**）。"
        "集中度以外的派生量不可算（见上方 N/A 原因）；与官方持仓表冲突时以官方为准。"
    )


SOURCE_NOTE_NONE = (
    "数据源：本次三级全部落空（Alpha Vantage 与 yfinance 均未取到）。"
    "持仓一律记 N/A，须由官方持仓表补齐，绝不估算。"
)

# 第三级的官网线索。**这不是标的清单**——清单永远是 universe.json；这里只是几档已知
# 发行商的入口，未列入的档走通用线索，新增 ETF 不必改本脚本。
# universe.json 的行里若带 `issuer_page` 字段，优先用那个（比本表更权威、可随时更新）。
ISSUER_PAGES = {
    "LYTE": ("Roundhill", "https://www.roundhillinvestments.com/etf/lyte"),
    "NCLD": ("Roundhill", "https://www.roundhillinvestments.com/etf/ncld"),
    "DRAM": ("Roundhill", "https://www.roundhillinvestments.com/etf/dram"),
    "SMH": ("VanEck", "https://www.vaneck.com/us/en/investments/semiconductor-etf-smh/"),
    "SOXX": ("iShares / BlackRock", "https://www.ishares.com/us/products/239705/"),
}

# 限流文案关键词。**先判瞬时、后判日限**：瞬时那段文案里同时含 "per day" 与
# "rate limit"（见文件头注释 2），顺序反了会把每秒限流误判成日配额耗尽。
TRANSIENT_MARKERS = (
    "spreading out",
    "per second",
    "burst limit",
    "call frequency",
    "higher api call volume",
)
DAILY_MARKERS = (
    "per day",
    "daily rate limit",
    "daily limit",
    "requests per day",
)

# 现金 / 国库券 / 货币类成分的识别词。rating-rules.md 明确警告过
# 「行销页把 36% 国库券当成持仓」这个陷阱，所以这一类必须单独拎出来报。
CASH_MARKERS = (
    "TREASURY", "T-BILL", "TBILL", "T BILL", "BILLS",
    "GOVERNMENT OBLIG", "MONEY MARKET", "MONEY MKT",
    "CASH", "REPO", "DEPOSIT",
    "US DOLLAR", "U.S. DOLLAR", "NEW TAIWAN DOLLAR", "HONG KONG DOLLAR",
    "SOUTH KOREA WON", "KOREAN WON", "CHINESE YUAN", "JAPANESE YEN", "EURO ",
)
# 抵押品冲销 / 其他资产负债：权重为负，与正向现金类混在一起加总会得出误导性的净数，
# 所以单独一栏。
OFFSET_MARKERS = ("CASH OFFSET", "OTHER ASSETS AND LIABILITIES", "OTHER ASSETS & LIABILITIES")

SWAP_MARKERS = ("SWAP", "TOTAL RETURN SWAP", "-TRS", " TRS")

NA = "N/A"

# 归并发行人时要剥掉的后缀词（同一标的的 swap 与实物是两行，归并后才对得上
# rating-rules 里「DRAM 前三大≈73%」这类人工核过的数字）。
_ISSUER_STRIP_PHRASES = (
    "-SWAP-GOLD-L", "SWAP-GOLD-L", "-SWAP-GOLD", "SWAP NM", "-SWAP", " SWAP",
    "ORDINARY SHARE", "PARTICIPATING PREFERRED", "PREFERRED SHARE",
    "CLASS A", "CLASS B", "CLASS C", "CLASS-A-", "CLASS-B-",
)
_ISSUER_STRIP_TOKENS = {
    "INC", "INCORPORATED", "CORP", "CORPORATION", "CO", "COLTD", "LTD", "LIMITED",
    "PLC", "NV", "SA", "AG", "SE", "GROUP", "HOLDING", "HOLDINGS", "COMPANY",
    "ADR", "GDR", "SHS", "SHARE", "SHARES", "CLASS", "NEW", "THE", "AND", "&",
}

_HOMEISH_RE = re.compile(r"(?:/Users|/home|/var/folders)/[^/\s\"']+")


# ---------------------------------------------------------------- 基础工具
def _configure_streams() -> None:
    """强制 stdout/stderr 用 UTF-8，避免管道场景下中文/符号写不出去。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass


def err(msg: str = "") -> None:
    print(msg, file=sys.stderr)


def rel_path(path) -> str:
    """技能自身的路径一律相对技能根目录展示。

    取数输出会被贴进运行结果正文、再推到 Slack，绝不能把含本机用户名的
    绝对路径带出去。（用户自己传进来的 --json 落盘路径不走这里。）
    """
    p = Path(path)
    try:
        return str(p.resolve().relative_to(SKILL_ROOT))
    except (ValueError, OSError):
        return p.name


def scrub_paths(text) -> str:
    """把文本里的家目录绝对路径折叠掉，绝不把本机用户名带出去。"""
    s = str(text)
    try:
        home = str(Path.home())
    except Exception:
        home = ""
    if home:
        s = s.replace(home, "~")
    return _HOMEISH_RE.sub("~", s)


# ---------------------------------------------------------------- API key
class KeyRing:
    """AV API key 的持有者。对外只暴露遮罩后的标识，原文永不出现在任何输出里。"""

    def __init__(self, keys: list[str]):
        self.keys = keys
        self.masks = [self._mask(k, i) for i, k in enumerate(keys)]
        # 本次运行内已判定为「日配额耗尽」的 key 下标，不再使用。
        self.exhausted: set[int] = set()

    @staticmethod
    def _mask(key: str, idx: int) -> str:
        """`key#N(前4****后2)`；key 太短则整体遮掉，宁可少给线索也不泄露。"""
        if len(key) >= 12:
            body = f"{key[:4]}****{key[-2:]}"
        else:
            body = "****"
        return f"key#{idx + 1}({body})"

    def mask_of(self, idx: int) -> str:
        return self.masks[idx] if 0 <= idx < len(self.masks) else "key#?"

    def redact(self, text) -> str:
        """把任何文本里出现的 key 原文换成遮罩标识。

        错误信息、异常文本、requests 回显的 URL 都会经过这里。仓库是 public repo，
        这条是硬要求，改动本文件时务必保持「所有 print/err 都走 safe()」。
        """
        s = str(text)
        for key, mask in zip(self.keys, self.masks):
            if key:
                s = s.replace(key, mask)
        return s

    def safe(self, text) -> str:
        """对外输出的唯一出口：先去 key、再去家目录路径。"""
        return scrub_paths(self.redact(text))

    def live_indices(self) -> list[int]:
        return [i for i in range(len(self.keys)) if i not in self.exhausted]


def load_keys(required: bool = False) -> KeyRing:
    """读 AV key。

    `required=True`（--check 用）：没 key 就退出——那条子命令本来就只验 key。
    `required=False`（取数用）：**没 key 不退出**，返回空 KeyRing，第一级整个跳过、
    直接从第二级 yfinance 起跑。这与三级回退的前提一致：没 key 也是「AV 取不到」的一种，
    没理由因此连次选源都不试。但必须**大声说出**降级了，否则读者会把 top-N 当全量。
    """
    raw = os.environ.get(ENV_VAR)
    if raw is None or not raw.strip():
        if not required:
            err(f"⚠ 未设置环境变量 {ENV_VAR}：**跳过第一级 Alpha Vantage（全量持仓）**，"
                f"本次直接从第二级 yfinance 起跑。")
            err(f"  第二级**只有 top-N**：前十大／swap／非美股／现金类合计一律记 {NA}；"
                f"新上市的档（实测 LYTE / NCLD）yfinance 一笔都没有，会落到第三级官网提示。")
            err(f"  要拿全量持仓请设置（多个 key 用英文逗号分隔，脚本会在限流时自动轮换）：")
            err(f"    export {ENV_VAR}=k1,k2")
            err("  免费 key 申请：https://www.alphavantage.co/support/#api-key")
            return KeyRing([])
        err(f"错误：环境变量 {ENV_VAR} 未设置，--check 无 key 可验。")
        err("请先设置（多个 key 用英文逗号分隔，脚本会在限流时自动轮换）：")
        err(f"    export {ENV_VAR}=k1,k2")
        err("免费 key 申请：https://www.alphavantage.co/support/#api-key")
        err("本脚本不含任何内置 key，也不读配置文件，不会回退到默认 key。")
        sys.exit(1)
    seen: set[str] = set()
    keys: list[str] = []
    for part in raw.split(","):
        k = part.strip()
        if not k or k in seen:
            continue
        seen.add(k)
        keys.append(k)
    if not keys:
        err(f"错误：{ENV_VAR} 里没有解析出任何有效 key（逗号分隔、去空白后为空）。")
        err(f"    export {ENV_VAR}=k1,k2")
        sys.exit(1)
    return KeyRing(keys)


# ---------------------------------------------------------------- HTTP
def make_session():
    """requests.Session + UA。

    本执行环境的 urllib 走 HTTPS 会 CERTIFICATE_VERIFY_FAILED（实测），必须用
    requests；与 fetch_fundamentals.py 的 yfinance 要求同源，别"统一"回标准库。
    """
    try:
        import requests
    except ImportError:
        err("缺少 requests。请先执行：")
        err("    pip install -q requests")
        err("（PEP668 报错时加 --break-system-packages）")
        sys.exit(1)
    sess = requests.Session()
    sess.headers["User-Agent"] = USER_AGENT
    return sess


def classify(payload) -> tuple[str, str]:
    """把一次响应 body 归类。返回 (kind, detail)。

    kind ∈ {ok, transient, daily, api_error, empty, malformed}
    **不看 HTTP 状态码**：限流回的是 200，按状态码判断会把限流当成数据。
    """
    if not isinstance(payload, dict):
        return "malformed", f"响应不是 JSON 对象（实际 {type(payload).__name__}）"

    note = payload.get("Information") or payload.get("Note")
    if note:
        text = str(note)
        low = text.lower()
        # 顺序不可调换：瞬时文案里同时含 "per day" 与 "rate limit"。
        if any(m in low for m in TRANSIENT_MARKERS):
            return "transient", text
        if any(m in low for m in DAILY_MARKERS):
            # ⚠️ 实测（2026-09-03）：**无效 key 与配额耗尽的 key 返回完全相同的消息**——
            #   随便编一个 ZZZZINVALIDKEY99 也回
            #   "We have detected your API key as ZZZZINVALIDKEY99 and our standard
            #    API rate limit is 25 requests per day."
            #   所以这里判出的 "daily" 有两种可能：真的用完了，或者这个 key 根本是错的
            #   （打字打错、复制漏字符）。响应里没有任何字段能区分。
            #   处置上两者都该换下一个 key，但**报告措辞必须把歧义说出来**，
            #   否则一个手滑打错的 key 会被静默记成「今日配额已用尽」。
            return "daily", text + "｜⚠️ 注意：AV 对无效 key 与配额耗尽返回相同消息，无法区分，请顺带核对该 key 是否拼写正确"
        # 两者都不匹配（如 demo key 的提示）→ 保守当瞬时，原文完整带出去。
        return "transient", text

    if payload.get("Error Message"):
        # 参数/代码错误，不是限流，不换 key。
        return "api_error", str(payload["Error Message"])

    if not payload:
        # 实测：不存在的代码、以及非 ETF 的个股（NVDA）都返回 {}。
        return "empty", "响应为空对象 {}（该代码可能不是 ETF 或不存在）"

    if "holdings" not in payload:
        return "malformed", "响应里没有 holdings 字段"

    return "ok", ""


def fetch_one(sess, symbol: str, ring: KeyRing, retries: int, warnings: list[str]):
    """按 key 轮换取一档 ETF。返回 (payload | None, notes)。

    单个 key 上：瞬时限流指数退避重试至多 `retries` 次，仍失败才换下一个 key；
    日配额耗尽则立刻标记该 key 本次运行作废并换下一个。
    """
    notes: list[str] = []
    live = ring.live_indices()
    if not live:
        if not ring.keys:
            notes.append(f"未设置 {ENV_VAR}，第一级 Alpha Vantage 整个跳过（直接走第二级 yfinance）")
        else:
            notes.append("所有 key 均已限流/耗尽（本次运行内）")
        return None, notes

    for idx in live:
        mask = ring.mask_of(idx)
        for attempt in range(1, retries + 1):
            try:
                resp = sess.get(
                    API_BASE,
                    params={
                        "function": API_FUNCTION,
                        "symbol": symbol,
                        "apikey": ring.keys[idx],
                    },
                    timeout=HTTP_TIMEOUT,
                )
                payload = resp.json()
            except Exception as exc:
                # 异常文本可能带上完整 URL（含 apikey），必须先脱敏再落地。
                detail = ring.safe(f"{type(exc).__name__}: {exc}")
                notes.append(f"{mask} 第 {attempt} 次请求异常：{detail}")
                if attempt < retries:
                    time.sleep(BACKOFF_BASE * (2 ** (attempt - 1)))
                    continue
                break

            kind, detail = classify(payload)
            detail = ring.safe(detail)

            if kind == "ok":
                if idx != live[0]:
                    notes.append(f"已轮换到 {mask} 取数成功")
                return payload, notes

            if kind == "api_error":
                # 参数错误：换 key 也没用，直接判这档失败。
                notes.append(f"{mask} 返回 API 参数错误（非限流，不换 key）：{detail}")
                return None, notes

            if kind in ("empty", "malformed"):
                notes.append(f"{mask} 返回无效数据（非限流，不换 key）：{detail}")
                return None, notes

            if kind == "daily":
                ring.exhausted.add(idx)
                msg = f"{mask} 日配额已耗尽，本次运行不再使用：{detail}"
                notes.append(msg)
                warnings.append(f"[{symbol}] {msg}")
                break  # 换下一个 key

            # transient：同 key 指数退避重试
            warnings.append(f"[{symbol}] {mask} 第 {attempt} 次遇限流：{detail}")
            if attempt < retries:
                wait = BACKOFF_BASE * (2 ** (attempt - 1))
                notes.append(f"{mask} 第 {attempt} 次遇瞬时限流，退避 {wait:.1f}s 后重试")
                time.sleep(wait)
            else:
                notes.append(f"{mask} 连续 {retries} 次遇瞬时限流，换下一个 key")

    notes.append("所有 key 均已限流/耗尽，本档取不到数据")
    return None, notes


# ---------------------------------------------------------------- 第二级 yfinance
def _yf_import(notes: list[str]):
    """lazy import：缺 yfinance 时 --help 仍可用，给安装提示而不是 traceback。"""
    try:
        import yfinance as yf  # noqa: PLC0415 —— 刻意延后导入
    except ImportError:
        notes.append("第二级 yfinance 不可用：未安装 yfinance"
                     "（pip install -q yfinance requests；PEP668 报错时加 --break-system-packages）")
        return None
    return yf


def fetch_yfinance_one(sess, symbol: str, notes: list[str]) -> list[dict] | None:
    """第二级：yfinance `funds_data.top_holdings`。

    返回持仓列表（**只有 top-N，不是全量**）或 None（该级失败，落到第三级）。
    实测（2026-09-03）：LYTE 0 笔、NCLD 0 笔（两档太新，yfinance 没有）、DRAM 5 笔、
    SMH 10 笔、SOXX 10 笔。DRAM 在这边 symbol 是对的（005930.KQ = 三星），
    而 AV 那边全是 n/a —— 两家弱点互补，所以第二级不是聊胜于无。

    session 必须是 requests.Session + UA（见 make_session 的注释）；
    空 DataFrame / None / 任何异常都当该级失败，**绝不当成「该档没有持仓」**。
    """
    yf = _yf_import(notes)
    if yf is None:
        return None
    try:
        tk = yf.Ticker(symbol, session=sess)
        df = tk.funds_data.top_holdings
    except Exception as exc:
        notes.append(f"第二级 yfinance 取数异常：{scrub_paths(f'{type(exc).__name__}: {exc}')}")
        return None

    if df is None:
        notes.append("第二级 yfinance 未返回 top_holdings（None）")
        return None
    try:
        if getattr(df, "empty", False) or len(df.index) == 0:
            notes.append(f"第二级 yfinance 返回空 top_holdings（{symbol} 可能太新，yfinance 尚无持仓）")
            return None
        cols = [str(c) for c in df.columns]
    except Exception as exc:
        notes.append(f"第二级 yfinance 返回值形状异常：{scrub_paths(f'{type(exc).__name__}: {exc}')}")
        return None

    # 列名以实测为准（`Name` / `Holding Percent`），但按语义匹配，免得列名微调就整级失效。
    name_col = next((c for c in cols if "name" in c.lower()), None)
    pct_col = next((c for c in cols if "percent" in c.lower() or "weight" in c.lower()), None)
    if pct_col is None:
        notes.append(f"第二级 yfinance 的 top_holdings 里找不到权重列（实际列：{', '.join(cols)}）")
        return None

    out: list[dict] = []
    try:
        for idx, row in df.iterrows():
            sym = as_text(idx)
            desc = as_text(row.get(name_col)) if name_col else None
            weight = as_num(row.get(pct_col))     # 实测已是小数：0.0723 = 7.23%
            desc = desc or (sym or "")
            upper = (desc or "").upper()
            out.append({
                "symbol": sym,
                "description": desc,
                "weight": weight,
                # 逐笔标签仅供阅读，**不参与任何合计**：top-N 口径下 swap/现金类合计不可算。
                "is_swap": any(m in upper for m in SWAP_MARKERS),
                "is_offset": any(m in upper for m in OFFSET_MARKERS),
                "is_cash": (any(m in upper for m in CASH_MARKERS)
                            or any(m in upper for m in OFFSET_MARKERS)),
                "issuer": issuer_key(desc) or (sym or ""),
            })
    except Exception as exc:
        notes.append(f"第二级 yfinance 解析失败：{scrub_paths(f'{type(exc).__name__}: {exc}')}")
        return None

    if not out:
        notes.append("第二级 yfinance 解析后为 0 笔")
        return None
    notes.append(f"第一级 Alpha Vantage 未取到，已回退到第二级 yfinance，拿到 {len(out)} 笔"
                 f"（**top-{len(out)}，不是全量**）")
    return out


# ---------------------------------------------------------------- 第三级 官网
def official_lines(row: dict, partial_n: int | None = None) -> list[str]:
    """第三级的可操作提示（不是静默 N/A）。

    `partial_n=None`：前两级都失败，整档记 N/A。
    `partial_n=N`：第二级只拿到 top-N，**全量部分仍未取到**，同样要去官网补。
    """
    sym = row.get("ticker") or "?"
    page = as_text(row.get("issuer_page"))       # universe.json 里若有就用它
    issuer = as_text(row.get("issuer"))
    if not page:
        known = ISSUER_PAGES.get(str(sym).upper())
        if known:
            issuer, page = known[0], known[1]
    if partial_n is None:
        lines = [f"↓ 第三级：请人工或用 WebSearch 到发行商官网取 {sym} 的**官方持仓表**。"]
    else:
        lines = [f"↓ 全量持仓仍缺：请人工或用 WebSearch 到发行商官网取 {sym} 的**官方持仓表**"
                 f"（本次只有 yfinance 的 top-{partial_n}）。"]
    if page:
        lines.append(f"  · 发行商持仓页：{issuer or '发行商'} — {page}"
                     f"（链接若失效，改用 WebSearch 搜「{sym} ETF holdings」并认准发行商域名）")
    else:
        lines.append(f"  · 本脚本没有 {sym} 的发行商入口线索。查法：WebSearch 「{sym} ETF holdings」，"
                     "只认发行商自家域名（如 Roundhill / VanEck / iShares 官网）的持仓页；"
                     f"也可在 universe.json 的该行加 `issuer_page` 字段，之后本脚本会直接打印它。")
    lines += [
        "  · 要看什么：① **全量**持仓明细与逐笔权重（不是行销页的「前十大」）；"
        "② 是否有现金 / 国库券（T-Bill）/ 货币基金行项目；③ 是否有 swap 及其抵押品行项目；"
        "④ 持仓表的**截止日**（引用时必须连日期一起写）。",
        "  · ⚠ 行销页的「前十大合计」常把国库券抵押品算进持仓（rating-rules.md 记过 LYTE 的"
        "「82.9%」就是这么来的），只取官方持仓表本身，不取行销页摘要。",
    ]
    if partial_n is None:
        lines.append(f"  · **本次未取到，需人工或 WebSearch 补；在补上之前该档持仓记 {NA}，绝不估算**，"
                     "也不得当成「该档没有持仓」。")
    else:
        lines.append(f"  · **本次只取到 top-{partial_n}；全量持仓与 profile 字段未取到，需人工或 "
                     f"WebSearch 补，在补上之前那些字段记 {NA}，绝不估算**——"
                     f"更不得拿 top-{partial_n} 去顶全量口径。")
    return lines


# ---------------------------------------------------------------- 解析
def as_num(value):
    """AV 的数值字段常是字符串 "n/a" —— 一律记 N/A，绝不转成 0。"""
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    s = str(value).strip()
    if not s or s.lower() in ("n/a", "na", "none", "-", "--"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def as_text(value):
    if value is None:
        return None
    s = str(value).strip()
    if not s or s.lower() in ("n/a", "na", "none"):
        return None
    return s


def _normalize_issuer(description: str, fold_plural: bool) -> str:
    s = (description or "").upper()
    s = s.replace("&", " AND ")
    for phrase in _ISSUER_STRIP_PHRASES:
        s = s.replace(phrase.replace("&", " AND "), " ")
    s = re.sub(r"[^A-Z0-9 ]+", " ", s)
    tokens = []
    for tok in s.split():
        if tok in _ISSUER_STRIP_TOKENS:
            continue
        # 简单去复数，吸收 COMMUNICATION / COMMUNICATIONS 这类两行不一致的写法。
        if fold_plural and len(tok) >= 5 and tok.endswith("S"):
            tok = tok[:-1]
        if tok in _ISSUER_STRIP_TOKENS:
            continue
        tokens.append(tok)
    return " ".join(tokens).strip()


def issuer_key(description: str) -> str:
    """把 description 归一化成发行人**分组键**，让同一标的的 swap 与实物两行合得起来。

    这是**聚合**不是估算：只做大小写、标点、法人后缀与 swap 后缀的剥离，
    外加一步去复数（AV 同一标的两行会写成 COMMUNICATION / COMMUNICATIONS）。
    实测校验：DRAM 归并后前三大 = 三星 25.23% + 美光 25.21% + SK海力士 22.54%
    ≈ 73.0%，与 references/rating-rules.md 里人工核过的「前三大≈73%」对得上；
    LYTE 归并后新易盛 13.94% / 中际旭创 12.84% / 天孚通信 9.48% / 源杰 6.49%
    亦与该文件里人工扒官网的 13.7 / 12.7 / 9.5 / 6.6 对得上。
    截断名（如 "T AND S COMMUNICAT"）归并不上，属已知限制，不硬凑。
    """
    return _normalize_issuer(description, fold_plural=True)


def issuer_label(description: str) -> str:
    """展示用名称：与 issuer_key 同一套剥离，但**不去复数**，免得印出
    "ACCELINK TECHNOLOGIE" 这种缺字母的名字误导读者。分组仍以 issuer_key 为准。"""
    return _normalize_issuer(description, fold_plural=False)


def parse_holdings(payload: dict) -> tuple[list[dict], list[str]]:
    """解析 holdings。

    `symbol` 为 "n/a" 的**必须保留**：非美股成分（韩股/台股/A 股）与 swap 合成部位
    都是这个形状，丢掉就等于丢掉 LYTE 一半的 A 股曝险、DRAM 的全部韩股。
    """
    raw = payload.get("holdings")
    notes: list[str] = []
    if not isinstance(raw, list):
        return [], ["holdings 字段不是列表，本档持仓记 N/A"]

    out: list[dict] = []
    bad_weight = 0
    for item in raw:
        if not isinstance(item, dict):
            continue
        sym = as_text(item.get("symbol"))          # "n/a" -> None，但整笔保留
        desc = as_text(item.get("description")) or ""
        weight = as_num(item.get("weight"))
        if weight is None:
            bad_weight += 1
        upper = desc.upper()
        out.append({
            "symbol": sym,                          # None 表示 AV 没给代码
            "description": desc,
            "weight": weight,                       # 小数口径，0.1555 = 15.55%
            "is_swap": any(m in upper for m in SWAP_MARKERS),
            "is_offset": any(m in upper for m in OFFSET_MARKERS),
            "is_cash": (any(m in upper for m in CASH_MARKERS)
                        or any(m in upper for m in OFFSET_MARKERS)),
            "issuer": issuer_key(desc) or (sym or ""),
        })
    if bad_weight:
        notes.append(f"{bad_weight} 笔持仓的 weight 无法解析，已记 N/A 并排除出各项合计")
    if not out:
        notes.append("holdings 为空列表，本档持仓记 N/A")
    return out, notes


def _w(h) -> float:
    return h.get("weight") or 0.0


def derive(holdings: list[dict], full: bool = True) -> dict:
    """算 rating-rules 真正用得上的几个派生量。全部以小数口径存，展示时 ×100。

    `full` 说明入参是不是**全量持仓**：
    - `True`（Alpha Vantage）：所有派生量都算得出来。
    - `False`（yfinance top-N）：**只有集中度算得出来**，其余一律置 None 并把原因写进
      返回值的 `na_reasons`。这不是保守，是口径问题：top-N 里根本没有现金/国库券/swap
      那几行，硬算会得到 0.00% 这种「看起来是数据、其实是缺失」的读数——
      rating-rules.md 里「行销页把 36% 国库券当成持仓」的陷阱就会被原样复现。

    字段一律用 .get 取（yfinance 那级构造的行少几个派生字段也不会炸）。
    """
    usable = [h for h in holdings if h.get("weight") is not None]
    ranked = sorted(usable, key=_w, reverse=True)

    # 归并发行人：同一标的的 swap 与实物两行合并（DRAM「前三大≈73%」就是这个口径）。
    merged: dict[str, dict] = {}
    for h in sorted(usable, key=_w, reverse=True):
        k = h.get("issuer") or h.get("description") or (h.get("symbol") or "?")
        # 展示名取组内权重最大那笔（usable 已按权重降序遍历，故只在建组时定）。
        slot = merged.setdefault(k, {
            "issuer": issuer_label(h.get("description")) or k,
            "key": k, "weight": 0.0, "lines": 0, "symbols": [],
        })
        slot["weight"] += _w(h)
        slot["lines"] += 1
        if h.get("symbol") and h.get("symbol") not in slot["symbols"]:
            slot["symbols"].append(h.get("symbol"))
    merged_ranked = sorted(merged.values(), key=lambda m: m["weight"], reverse=True)

    def head(seq, n, getter=_w):
        return sum(getter(x) for x in seq[:n]) if seq else None

    swap_w = sum(_w(h) for h in usable if h.get("is_swap"))
    phys_w = sum(_w(h) for h in usable if not h.get("is_swap") and not h.get("is_cash"))
    # 「无代码成分」有两个口径，差别很大、不能只报一个：
    # 含现金类的话，权重 −35% 的 CASH OFFSET（symbol 也是 n/a）会把数字压掉一大截，
    # 实测 LYTE 含现金 16.01% / 不含现金 51.08%——后者才是 rating-rules 关心的
    # 「A 股 48.7% + 台股 2.3% ≈ 51%」曝险，前者会严重低估。
    nosym_w = sum(_w(h) for h in usable if not h.get("symbol"))
    nosym_ex_cash_w = sum(_w(h) for h in usable if not h.get("symbol") and not h.get("is_cash"))
    cash_pos = sum(_w(h) for h in usable if h.get("is_cash") and not h.get("is_offset"))
    offset_w = sum(_w(h) for h in usable if h.get("is_offset"))
    pos_w = sum(_w(h) for h in usable if _w(h) > 0)
    neg_w = sum(_w(h) for h in usable if _w(h) < 0)

    out = {
        "full_coverage": bool(full),
        "count": len(holdings),
        "count_weighted": len(usable),
        "weight_total": sum(_w(h) for h in usable) if usable else None,
        "weight_positive": pos_w if usable else None,
        "weight_negative": neg_w if usable else None,
        "top3": head(ranked, 3),
        # 前三大里若混进现金/货币基金行项目，它与 rating-rules 里人工核过的
        # 「前三大＝三大**股票**」不是同一口径（实测 DRAM 走 yfinance top-5 时
        # 第 3 名是货币基金 FGXXX 14.66%，算出 51.31%，而正文写的是 ≈73%，差 22pt）。
        # 这个事实必须同时进 stdout 与 --json —— 只印在屏幕上的话，
        # 下游读 JSON 建表就会拿一个撞号的裸数字去比正文的既有数字。
        "top3_cash_items": [
            {"symbol": h.get("symbol"), "description": h.get("description"), "weight": _w(h)}
            for h in ranked[:3] if h.get("is_cash")
        ],
        "top10": head(ranked, 10),
        "top3_merged": head(merged_ranked, 3, lambda m: m["weight"]),
        "top10_merged": head(merged_ranked, 10, lambda m: m["weight"]),
        "swap": swap_w if usable else None,
        "physical": phys_w if usable else None,
        "no_symbol": nosym_w if usable else None,
        "no_symbol_ex_cash": nosym_ex_cash_w if usable else None,
        "cash_like": (cash_pos + offset_w) if usable else None,
        "cash_positive": cash_pos if usable else None,
        "cash_offset": offset_w if usable else None,
        "na_reasons": {},
        "ranked": ranked,
        "merged_ranked": merged_ranked,
    }
    if not full:
        n = len(holdings)
        need_full = ("swap", "physical", "no_symbol", "no_symbol_ex_cash",
                     "cash_like", "cash_positive", "cash_offset")
        for k in need_full:
            out[k] = None
            out["na_reasons"][k] = f"需全量持仓，yfinance 仅提供 top-{n}，无法计算"
        # 前三大：top-N 内的前三大即全量口径的前三大（前三大必在 top-N 内）→ N≥3 可算。
        if n < 3:
            for k in ("top3", "top3_merged"):
                out[k] = None
                out["na_reasons"][k] = f"yfinance 仅提供 top-{n}，不足 3 笔"
        # 前十大：N≥10 才算得出，N=5（实测 DRAM）时**不可算**，不得拿 5 笔充数。
        if n < 10:
            for k in ("top10", "top10_merged"):
                out[k] = None
                out["na_reasons"][k] = f"需前十大，yfinance 仅提供 top-{n}"
        for k in ("weight_total", "weight_positive", "weight_negative"):
            out["na_reasons"][k] = f"这是前 {n} 大之和，不是基金权重总和"
    return out


# ---------------------------------------------------------------- 展示
def disp_w(s: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in str(s))


def pad(s: str, width: int) -> str:
    s = str(s)
    return s + " " * max(0, width - disp_w(s))


def pct(value, signed: bool = False) -> str:
    """小数 -> 百分比字符串。None 一律 N/A，绝不填 0。"""
    if value is None:
        return NA
    if signed:
        return f"{value * 100:+.2f}%"
    return f"{value * 100:.2f}%"


def money(value) -> str:
    if value is None:
        return NA
    v = float(value)
    sign = "-" if v < 0 else ""
    a = abs(v)
    if a >= 1e12:
        return f"{sign}{a / 1e12:.2f}T"
    if a >= 1e9:
        return f"{sign}{a / 1e9:.2f}B"
    if a >= 1e6:
        return f"{sign}{a / 1e6:.2f}M"
    return f"{sign}{a:,.0f}"


def na_reason(d: dict, key: str) -> str:
    """派生量记 N/A 时的原因串——**不可算的必须说出为什么不可算**，
    否则读者会以为是「这档恰好没有」而不是「这个口径算不出来」。"""
    r = (d.get("na_reasons") or {}).get(key)
    return f"   ← {NA}：{r}" if r else ""


def print_etf(rec: dict) -> None:
    meta, prof, d = rec["meta"], rec["profile"], rec["derived"]
    sym = meta["ticker"]
    src = meta.get("source") or SRC_NONE
    label = meta.get("source_label") or src

    print()
    print("━" * 78)
    head = f"{sym}   （universe.json 第 {meta.get('order')} 行 · 主题 {meta.get('theme') or NA}）"
    print(head)
    print(f"来源：{label}")

    # ---- 第三级：三级全落空。不静默记 N/A，给可操作的官网提示。
    if src == SRC_NONE:
        print(f"✗ 本档 Alpha Vantage 与 yfinance 都没取到，持仓相关字段全部记 {NA}"
              f"（绝不估算，也不得当成「该档没有持仓」）。")
        for n in meta.get("notes", []):
            print(f"  · {n}")
        for line in official_lines(meta.get("row") or {"ticker": sym}):
            print(line)
        print(SOURCE_NOTE_NONE)
        return

    partial = not d.get("full_coverage", True)
    n = d["count"]

    if partial:
        print(f"⚠ 第一级 Alpha Vantage 未取到，本档回退到 **yfinance top-{n}**："
              f"只有前 {n} 大，**不是全量持仓**。")
        print(f"  下面凡标 {NA} 的派生量，都是 top-{n} 口径下**算不出来**的——"
              f"它不是 0、不是「这档没有」，更不得估算。")
        print(f"  profile 字段（费率 / 净资产 / 成立日 / 换手 / 股息率 / 杠杆标记）本级不提供，"
              f"全部记 {NA}；那几项须取自官方或 Alpha Vantage。")
    else:
        print(
            f"成立日 {prof.get('inception_date') or NA}"
            f" | 费率 {pct(prof.get('net_expense_ratio'))}"
            f" | 净资产 {money(prof.get('net_assets'))}"
            f" | 换手 {pct(prof.get('portfolio_turnover'))}"
            f" | 股息率 {pct(prof.get('dividend_yield'))}"
            f" | 杠杆标记 {prof.get('leveraged') or NA}"
        )

    if partial:
        print(f"持仓 top-{n} 笔（其中 {d['count_weighted']} 笔权重可解析）"
              f" | 前 {n} 大权重之和 {pct(d['weight_total'])}"
              f"   ← 这不是基金权重总和；全量档数 yfinance 不提供，记 {NA}")
    else:
        print(f"持仓 {d['count']} 笔（其中 {d['count_weighted']} 笔权重可解析）"
              f" | 权重合计 {pct(d['weight_total'])}"
              f"（正 {pct(d['weight_positive'], True)} / 负 {pct(d['weight_negative'], True)}）")

    print("派生量（权重口径：原字段是小数 0.1555，此处已 ×100）")
    top3_tail = na_reason(d, "top3") or (
        f"   ← top-{n} 内的前三大即全量口径的前三大（前三大必在 top-N 内）" if partial else "")
    print(f"  前三大合计   行项目 {pct(d['top3']):>9}   归并发行人 {pct(d['top3_merged']):>9}"
          f"{top3_tail}")
    print(f"  前十大合计   行项目 {pct(d['top10']):>9}   归并发行人 {pct(d['top10_merged']):>9}"
          f"{na_reason(d, 'top10')}")
    # 「前三大」里混进现金/货币基金行项目时必须点名：rating-rules 里人工核过的
    # 「DRAM 前三大(三星/SK海力士/美光)≈73%」指的是**三大股票**，两者不是同一口径。
    # 实测 DRAM 走 yfinance 时第 3 名就是 FGXXX 货币基金——不提示就会被直接引用。
    cash_top3 = [h for h in d["ranked"][:3] if h.get("is_cash")]
    if cash_top3:
        names = "、".join(f"{h.get('symbol') or 'n/a'} {pct(h.get('weight'))}" for h in cash_top3)
        print(f"  ⚠ 上面的「前三大」里含现金/货币基金类行项目（{names}），"
              f"与 rating-rules.md 里人工核过的「前三大＝三大**股票**」不是同一口径；"
              f"引用前须改口径或剔除，不得直接对比。")
    print(f"  swap 合成部位合计 {pct(d['swap']):>9}   实物（非 swap 非现金）合计 {pct(d['physical']):>9}"
          f"{na_reason(d, 'swap')}")
    print(f"  无代码成分（不含现金类）{pct(d['no_symbol_ex_cash']):>9}"
          f"   含现金类 {pct(d['no_symbol'])}"
          f"{na_reason(d, 'no_symbol_ex_cash') or '   ← AV 未给 symbol：非美股成分与 swap 部位'}")
    print(f"  现金/国库券/货币类 净 {pct(d['cash_like']):>9}"
          f"   = 正向 {pct(d['cash_positive'])} + 抵押品冲销 {pct(d['cash_offset'], True)}"
          f"{na_reason(d, 'cash_like')}")
    print("  ⚠ 「行项目」口径下同一标的的 swap 与实物分列两行，前 N 大合计小于「归并发行人」口径；")
    print("    引用「前三大集中度」时须写明用的是哪一种，两者不可混用。")
    if partial:
        print(f"  ⚠ 上面几项 {NA} 的成因是**口径**不是缺数：rating-rules.md 记过的"
              f"「行销页把 36% 国库券当成持仓」正是靠全量持仓才识破的，"
              f"拿 top-{n} 去算「现金类合计」只会复现该陷阱。")

    rows = d["ranked"]
    na_rows = [h for h in rec["holdings"] if h.get("weight") is None]
    scope = f"top-{n}（**非全量**）" if partial else f"全部 {len(rec['holdings'])} 笔"
    print(f"持仓明细（按权重降序，{scope}；symbol 为 n/a 的整笔保留）")
    wsym = max([4] + [disp_w(h.get("symbol") or "n/a") for h in rec["holdings"]])
    for i, h in enumerate(rows, 1):
        tag = "swap" if h.get("is_swap") else ("cash" if h.get("is_cash") else "    ")
        print(f"  {i:>2}. {pad(h['symbol'] or 'n/a', wsym)}  {pct(h.get('weight'), True):>9}"
              f"  {tag}  {h['description']}")
    for h in na_rows:
        print(f"   —. {pad(h['symbol'] or 'n/a', wsym)}  {NA:>9}        {h['description']}")

    mr = d["merged_ranked"][:10]
    if mr:
        title = ("归并发行人（同一标的的 swap 与实物合并）"
                 + (f"（限 top-{n} 之内）" if partial else "前 10"))
        print(title)
        wi = max([6] + [disp_w(m["issuer"]) for m in mr])
        for i, m in enumerate(mr, 1):
            syms = "/".join(m["symbols"]) if m["symbols"] else "n/a"
            print(f"  {i:>2}. {pad(m['issuer'], wi)}  {pct(m['weight'], True):>9}"
                  f"  {m['lines']} 笔  [{syms}]")

    for note in meta.get("notes", []):
        print(f"  · {note}")
    print(source_note_yf(n) if partial else SOURCE_NOTE_AV)
    if partial:
        for line in official_lines(meta.get("row") or {"ticker": sym}, partial_n=n):
            print(line)

# ---------------------------------------------------------------- universe
def load_universe() -> tuple[dict, list[dict]]:
    where = rel_path(UNIVERSE_PATH)
    if not UNIVERSE_PATH.exists():
        err(f"错误：找不到标的清单 {where}")
        sys.exit(1)
    try:
        data = json.loads(UNIVERSE_PATH.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        err(f"错误：{where} 不是合法 JSON：{exc}")
        sys.exit(1)
    if not isinstance(data, dict):
        err(f"错误：{where} 顶层应是一个 JSON 对象。")
        sys.exit(1)
    rows = data.get("tickers") or []
    if not rows:
        err(f"错误：标的清单为空：{where}")
        sys.exit(1)
    # 行序以 order 为准（universe.json 是权威行序），order 缺失时退回文件顺序。
    rows = sorted(rows, key=lambda r: r.get("order", 10 ** 6))
    # 档数从 etf=true 推导，脚本内不硬编码数量。
    etfs = [r for r in rows if r.get("etf") and r.get("ticker")]
    if not etfs:
        err(f"错误：{where} 里没有任何 etf=true 的标的。")
        sys.exit(1)
    return data, etfs


# ---------------------------------------------------------------- 落盘
# 这里只有 --json 一条落盘路径，且**只写不读**：它是用户自传的导出路径，不是缓存。
# 本脚本刻意不保存、也不回退到任何持仓快照——理由见文件头「三级回退与『不留缓存』」。
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
        os.replace(tmp_path, path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------- 子流程
def cmd_check(sess, ring: KeyRing, probe: str, gap: float) -> int:
    """只验 key 可用性与配额：每个 key 打一次，不取全部标的。"""
    print(f"AV_API_KEYS 解析出 {len(ring.keys)} 个 key（已去重、去空白）。"
          f"探针标的：{probe}")
    usable = 0
    for idx in range(len(ring.keys)):
        mask = ring.mask_of(idx)
        try:
            resp = sess.get(
                API_BASE,
                params={"function": API_FUNCTION, "symbol": probe, "apikey": ring.keys[idx]},
                timeout=HTTP_TIMEOUT,
            )
            payload = resp.json()
        except Exception as exc:
            print(f"  {mask}  ✗ 请求异常：{ring.safe(f'{type(exc).__name__}: {exc}')}")
            continue
        kind, detail = classify(payload)
        detail = ring.safe(detail)
        if kind == "ok":
            n = len(payload.get("holdings") or [])
            print(f"  {mask}  ✓ 可用（{probe} 返回 {n} 笔持仓）")
            usable += 1
        elif kind == "transient":
            print(f"  {mask}  ⚠ 瞬时限流/不可用（重试或加大 --sleep 后再试）：{detail}")
        elif kind == "daily":
            print(f"  {mask}  ✗ 日配额已耗尽：{detail}")
        elif kind == "api_error":
            print(f"  {mask}  ✗ API 参数错误（非限流）：{detail}")
        else:
            print(f"  {mask}  ✗ 无效响应：{detail}")
        if idx < len(ring.keys) - 1:
            time.sleep(gap)
    print()
    if usable:
        print(f"结论：{usable}/{len(ring.keys)} 个 key 当前可用。")
        return 0
    print(f"结论：0/{len(ring.keys)} 个 key 可用——现在取数会全部记 {NA}。"
          f"请稍后重试、或在 {ENV_VAR} 里补充新的 key。")
    return 1


def build_record(meta: dict, payload: dict) -> dict:
    prof = {
        "net_assets": as_num(payload.get("net_assets")),
        "net_expense_ratio": as_num(payload.get("net_expense_ratio")),
        "portfolio_turnover": as_num(payload.get("portfolio_turnover")),
        "dividend_yield": as_num(payload.get("dividend_yield")),
        "inception_date": as_text(payload.get("inception_date")),
        "leveraged": as_text(payload.get("leveraged")),
        "sectors": payload.get("sectors") if isinstance(payload.get("sectors"), list) else [],
    }
    holdings, notes = parse_holdings(payload)
    meta = dict(meta)
    meta["notes"] = list(meta.get("notes", [])) + notes
    return {"meta": meta, "profile": prof, "holdings": holdings,
            "derived": derive(holdings, full=True)}


def serialize(rec: dict) -> dict:
    """--json 落盘用的形态：ranked/merged_ranked 是展示中间物，不落盘。

    `source` / `source_label` / `coverage` 三个字段是**口径标签**，缺一不可：
    下游若把 alphavantage（全量）与 yfinance（top-N）的数字拼进同一张表，就会造出一份
    加总对不上的「持仓表」——这正是 rating-rules.md 顶部编者注禁止的事。
    """
    d = {k: v for k, v in rec["derived"].items() if k not in ("ranked", "merged_ranked")}
    meta = rec["meta"]
    src = meta.get("source") or SRC_NONE
    n = rec["derived"].get("count") or 0
    if src == SRC_AV:
        note, coverage = SOURCE_NOTE_AV, "full"
    elif src == SRC_YF:
        note, coverage = source_note_yf(n), f"top-{n}"
    else:
        note, coverage = SOURCE_NOTE_NONE, "none"
    return {
        "ticker": meta["ticker"],
        "order": meta.get("order"),
        "theme": meta.get("theme"),
        "status": meta["status"],
        "source": src,                                  # alphavantage / yfinance / unavailable
        "source_label": meta.get("source_label") or src,  # 展示串，yfinance 带上 top-N
        "coverage": coverage,                           # full / top-N / none
        "fetched_at": meta.get("fetched_at"),
        "notes": meta.get("notes", []),
        "official_holdings_note": note,
        "official_followup": ([] if src == SRC_AV else official_lines(
            meta.get("row") or {"ticker": meta["ticker"]},
            partial_n=(n if src == SRC_YF else None))),
        "profile": rec["profile"],
        "derived": d,
        "holdings": rec["holdings"],
    }


# ---------------------------------------------------------------- main
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="etf_holdings.py",
        description="按 assets/universe.json 里 etf=true 的标的抓 ETF 持仓，"
                    "三级回退：Alpha Vantage ETF_PROFILE（全量）→ yfinance top_holdings"
                    "（**仅 top-N**）→ 官网提示。不保存也不回退任何持仓快照。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"API key 从环境变量 {ENV_VAR} 读，多个 key 用逗号分隔，限流时自动轮换。\n"
               f"脚本内无任何内置 key；输出中的 key 一律遮罩。\n"
               f"{SOURCE_NOTE_AV}\n"
               "回退到 yfinance 时只有 top-N：前十大／swap／非美股／现金类合计一律 N/A；\n"
               "三级的数字口径不同，不得拼进同一张表。持仓最终以官方持仓表为准。",
    )
    ap.add_argument("--tickers", help="只取这些 ETF（逗号分隔，如 LYTE,NCLD）")
    ap.add_argument("--json", metavar="PATH", help="额外把结构化结果写成 JSON")
    ap.add_argument("--check", action="store_true",
                    help=f"只验 {ENV_VAR} 里每个 key 的可用性与配额，不取全部标的")
    ap.add_argument("--sleep", type=float, default=DEFAULT_SLEEP, metavar="SEC",
                    help=f"每档之间的主动限速秒数（默认 {DEFAULT_SLEEP}；实测 1 req/sec 会触发限流）")
    ap.add_argument("--retries", type=int, default=DEFAULT_RETRIES, metavar="N",
                    help=f"同一个 key 遇瞬时限流的最大尝试次数（默认 {DEFAULT_RETRIES}）")
    ap.add_argument("--quiet", action="store_true", help="只写 --json，不做人类可读打印")
    args = ap.parse_args(argv)

    if args.sleep < 0:
        err("错误：--sleep 不能为负。")
        return 2
    retries = max(1, args.retries)

    uni, etf_rows = load_universe()
    # --check 只验 key，没 key 就没意义 -> required；取数则允许无 key 降级到第二级。
    ring = load_keys(required=bool(args.check))
    sess = make_session()

    if args.check:
        return cmd_check(sess, ring, etf_rows[0]["ticker"], max(args.sleep, 1.0))

    if args.tickers:
        want = [t.strip() for t in args.tickers.split(",") if t.strip()]
        idx = {r["ticker"].upper(): r for r in etf_rows}
        picked, unknown = [], []
        for t in want:
            r = idx.get(t.upper())
            (picked.append(r) if r else unknown.append(t))
        if unknown:
            err(f"错误：以下代码不在 {rel_path(UNIVERSE_PATH)} 的 etf=true 清单里："
                f"{', '.join(unknown)}")
            err(f"当前 etf=true 的标的：{', '.join(r['ticker'] for r in etf_rows)}")
            return 1
        etf_rows = picked

    warnings: list[str] = []
    records: list[dict] = []
    got_av, got_yf, failed = [], [], []
    now_iso = datetime.now().astimezone().isoformat(timespec="seconds")

    for i, row in enumerate(etf_rows):
        sym = row["ticker"]
        meta = {
            "ticker": sym,
            "order": row.get("order"),
            "theme": row.get("theme"),
            "layer": row.get("layer"),
            "row": row,                 # 第三级要用它找 issuer_page
            "status": "failed",
            "source": SRC_NONE,
            "source_label": SRC_NONE,
            "notes": [],
            "fetched_at": None,
        }
        # ---- 第一级：Alpha Vantage（全量持仓）
        payload, notes = fetch_one(sess, sym, ring, retries, warnings)
        meta["notes"] = notes

        if payload is not None:
            meta["status"] = "ok"
            meta["source"] = SRC_AV
            meta["source_label"] = f"{SRC_AV}（Alpha Vantage ETF_PROFILE · 全量持仓）"
            meta["fetched_at"] = now_iso
            rec = build_record(meta, payload)
            if not rec["holdings"]:
                # holdings 空：有 profile 无持仓，不当成"没有持仓"，明说记 N/A。
                rec["meta"]["notes"].append(f"AV 未返回任何持仓明细，持仓相关字段记 {NA}")
            got_av.append(sym)
            records.append(rec)
        else:
            # ---- 第二级：yfinance（**只有 top-N**）
            yf_rows = fetch_yfinance_one(sess, sym, meta["notes"])
            if yf_rows:
                n = len(yf_rows)
                meta["status"] = "ok"
                meta["source"] = SRC_YF
                meta["source_label"] = f"{SRC_YF}(top-{n})"
                meta["fetched_at"] = now_iso
                got_yf.append(f"{sym}[top-{n}]")
                records.append({
                    "meta": meta,
                    "profile": {},                       # 本级不提供 profile 字段
                    "holdings": yf_rows,
                    "derived": derive(yf_rows, full=False),
                })
                warnings.append(f"[{sym}] 第一级 AV 未取到，已回退 yfinance top-{n}"
                                f"（**非全量**，集中度以外的派生量记 {NA}）")
            else:
                # ---- 第三级：官网（不静默记 N/A，打印可操作提示）
                failed.append(sym)
                meta["notes"].append(f"三级中前两级都未取到，本档全部字段记 {NA}（绝不估算）")
                records.append({"meta": meta, "profile": {}, "holdings": [],
                                "derived": derive([], full=True)})
                warnings.append(f"[{sym}] AV 与 yfinance 均未取到，记 {NA}，须到官方持仓页人工补")

        if i < len(etf_rows) - 1 and args.sleep:
            time.sleep(args.sleep)

    if args.json:
        payload = {
            "fetched_at": now_iso,
            "sources": {
                SRC_AV: "Alpha Vantage ETF_PROFILE（全量持仓）",
                SRC_YF: "yfinance funds_data.top_holdings（**仅 top-N，非全量**）",
                SRC_NONE: "本次未取到，须到发行商官方持仓页人工补，持仓记 N/A",
            },
            "caliber_warning": "三级口径不同：alphavantage=全量、yfinance=top-N、"
                               "unavailable=无。**不同来源的数字不得拼进同一张表**，"
                               "引用时必须连 source 与取数日一起写。",
            "no_cache_note": "本脚本不保存也不回退持仓快照：ETF 持仓会变，"
                             "陈旧快照比没有更危险。本文件是导出，不是缓存，脚本从不读回。",
            "universe": rel_path(UNIVERSE_PATH),
            "universe_etf_total": len([r for r in (uni.get("tickers") or []) if r.get("etf")]),
            "requested": [r["ticker"] for r in etf_rows],
            "by_source": {
                SRC_AV: got_av,
                SRC_YF: got_yf,
                SRC_NONE: failed,
            },
            "keys_total": len(ring.keys),
            "keys_exhausted": [ring.mask_of(i) for i in sorted(ring.exhausted)],
            "warnings": [ring.safe(w) for w in warnings],
            "etfs": [serialize(r) for r in records],
        }
        out = Path(args.json).expanduser()
        try:
            out.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(out, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
            # 用户自传的路径同样含用户名，而本脚本输出会被贴进报告并推 Slack。
            # 折叠家目录（~/...）而不是只留文件名：既不泄漏，又还能让人找到文件。
            err(f"JSON 已写入 {scrub_paths(out)}")
        except Exception as exc:
            err(f"⚠ JSON 写入失败（不影响本次输出）：{type(exc).__name__}: {ring.safe(exc)}")

    if not args.quiet:
        print(f"ETF 持仓取数 · {now_iso}")
        key_txt = (f"key {len(ring.keys)} 个，已耗尽 {len(ring.exhausted)} 个。"
                   if ring.keys else
                   f"未设置 {ENV_VAR} → 第一级已整个跳过，本次自第二级 yfinance 起跑（**仅 top-N**）。")
        print(f"清单 {rel_path(UNIVERSE_PATH)}（etf=true）；本次请求 {len(etf_rows)} 档；" + key_txt)
        print("三级回退：① Alpha Vantage（全量） → ② yfinance（**仅 top-N**） → ③ 官网提示；"
              "每档标注实际来源，不保存也不回退任何快照。")
        for rec in records:
            print_etf(rec)

        print()
        print("━" * 78)
        # 汇总**按来源分组**，绝不混排成一张表：三级口径不同（全量 / top-N / 无），
        # 混排会造出一份加总对不上的「持仓表」，正是 rating-rules.md 顶部编者注禁止的事。
        print(f"汇总（按来源分组，**三级数字不可混进同一张表**）：本次请求 {len(etf_rows)} 档")
        print(f"  ① {SRC_AV}（全量持仓）        {len(got_av)} 档"
              + (f"：{', '.join(got_av)}" if got_av else "：—"))
        print(f"  ② yfinance(top-N)（**非全量**） {len(got_yf)} 档"
              + (f"：{', '.join(got_yf)}" if got_yf else "：—")
              + ("　← 这些档只有集中度可用，swap／非美股／现金类合计一律 N/A" if got_yf else ""))
        print(f"  ③ {SRC_NONE}（记 {NA}，待官网补） {len(failed)} 档"
              + (f"：{', '.join(failed)}" if failed else "：—"))
        if got_av and got_yf:
            print("  ⚠ 本次运行同时出现 ① 与 ②：两组数字口径不同，"
                  "写进报告时必须分组、各自标注来源与取数日，不得并成一张表。")
        if ring.exhausted:
            print("本次耗尽的 key：" + "、".join(ring.mask_of(i) for i in sorted(ring.exhausted)))
        if warnings:
            print("告警：")
            for w in warnings:
                print(f"  · {ring.safe(w)}")
        print("持仓一律以发行商官方持仓表为准；本脚本不保存快照，每次现取。")

    # 三级全落空（一档都没取到）-> 1；否则 0（部分失败已在汇总里分组列出，便于按 --tickers 重跑）。
    return 1 if (etf_rows and not got_av and not got_yf) else 0


if __name__ == "__main__":
    _configure_streams()
    # 最后一道闸：任何未预期的例外都不得吐出裸 traceback
    #（traceback 会印出 /Users/<用户名>/... 的脚本路径，且可能带上含 apikey 的 URL；
    #  本脚本输出会被贴进报告正文并推 Slack）。
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        err("✗ 已中断。")
        sys.exit(130)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 —— 刻意兜底
        err(f"✗ ETF 持仓取数失败：{type(exc).__name__}: {scrub_paths(exc)}")
        sys.exit(1)
