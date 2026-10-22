#!/usr/bin/env python3
"""
AI 算力产业链 · 基本面批量取数（yfinance）

标的清单与行序一律读 assets/universe.json，脚本内不内联任何 ticker 列表；
增减标的只改 universe.json，本脚本无需改动。

用法:
    python3 scripts/fetch_fundamentals.py                     # 取全部标的，人类可读打印
    python3 scripts/fetch_fundamentals.py --json out.json     # 同时存一份 JSON
    python3 scripts/fetch_fundamentals.py --tickers NVDA,TSM  # 只取部分（调试/重跑用）

依赖:
    pip install -q yfinance requests
    （遇 PEP668 报错时加 --break-system-packages）
    注意：yfinance 是在 main() 里延迟导入的（不在顶部 import 块），
    目的是让缺库时 --help 仍可用、并给出友好安装提示。

硬约束（踩坑换来的，勿"优化"）:
  1. 必须用 requests.Session 而不是 yfinance 默认的 curl_cffi 引擎。
     本执行环境出站走代理，curl_cffi 会 TLS 握手失败 -> .info 全部为 null。
  2. 3 次重试，成功判定沿用：i and (currentPrice or regularMarketPrice or len(i) > 20)
  3. 字段映射 F 逐字沿用原表 23 个字段，不增不减不改名。
  4. price 回退顺序：currentPrice -> regularMarketPrice -> previousClose
  5. 数据缺失一律记 N/A，绝不估算。
  6. 港股（universe.json 里 hk_quote=true 的行）的 price / hi / fromHi%
     不得用 yfinance，改由 scripts/hk_quote.py 覆盖（见 HK_OVERRIDE 注释）。
  7. .info 的 gm/om/nm 会单字段损坏（operatingMargins 尤甚），出表前必过
     check_margin_integrity() 自检（om>gm / om==gm 为硬错误，nm>gm 为待核），
     命中者才拉损益表重算年报与 TTM 两组口径，见输出结尾「⚠ 利润率完整性」一节。
     重算值只并列呈现、绝不静默替换 .info 原值；重算失败照样记 N/A。
  8. 现金流与负债压档（2026-10-22 起）：FCF 率一律取现金流量表（TTM 4 季与损益表同期对齐 ->
     年报 -> N/A），**绝不用 .info 的 freeCashflow**（实测偏离可达 28pt）；负债取 .info 的
     净负债/EBITDA。阈值只在 CASH_DEBT_TH 一处定义，算出的 rating_cap 由
     `baseline.py validate --caps` 强制执行，见输出结尾「现金流与负债 · 评级上限」一节。

退出码: 全部标的取数失败时 1，其余 0（部分失败在结尾汇总，便于按 --tickers 重跑）。
"""

import argparse
import datetime
import json
import re
import subprocess
import sys
import time
import unicodedata
from pathlib import Path

import requests

# yfinance 故意**不**在这里导入，而是延迟到 main() 里（见文件末尾附近的
# `import yfinance as yf`，再作为参数传进 fetch_info()）。这样缺库时
# `--help` 仍可用，且报的是带安装指引的友好错误而不是 ImportError traceback。

# ---------------------------------------------------------------- 路径定位
# 脚本随技能分发，一律以 __file__ 相对定位，从任意 cwd 都能跑。
HERE = Path(__file__).resolve().parent
SKILL_DIR = HERE.parent
UNIVERSE_PATH = SKILL_DIR / "assets" / "universe.json"
HK_QUOTE_SCRIPT = HERE / "hk_quote.py"


def tilde_path(path) -> str:
    """把家目录折叠成 ~，用于回显**用户自传**的路径（如 --json 落盘位置）。

    这类路径不属于技能自身、rel_path 会把它退化成裸文件名，找不回文件；
    但它同样含本机用户名，而本脚本输出会被贴进报告并推 Slack，所以要折叠。
    """
    p = Path(path)
    try:
        return "~/" + str(p.resolve().relative_to(Path.home()))
    except (ValueError, OSError):
        return str(p)


def rel_path(path: Path) -> str:
    """技能自身的路径一律相对技能根目录展示。

    取数输出会被贴进运行结果正文、再推到 Slack，绝不能把含本机用户名的
    绝对路径带出去。（用户自己传进来的 --json 落盘路径不走这里。）
    """
    try:
        return str(Path(path).resolve().relative_to(SKILL_DIR))
    except ValueError:
        return Path(path).name

# ---------------------------------------------------------------- 取数配置
# 原表那串 Chrome UA，逐字沿用（换 UA 曾导致 Yahoo 限流返回空 info）。
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

RETRIES = 3
RETRY_SLEEP = 0.8

# 字段映射 F：yfinance .info 键 -> 本表字段名。逐字沿用原表的 23 个字段。
F = {
    "longName": "name",
    "marketCap": "mktcap",
    "trailingPE": "PE",
    "forwardPE": "fwdPE",
    "priceToSalesTrailing12Months": "PS",
    "priceToBook": "PB",
    "enterpriseToEbitda": "EVE",
    "trailingPegRatio": "PEG",
    "grossMargins": "gm",
    "operatingMargins": "om",
    "profitMargins": "nm",
    "returnOnEquity": "ROE",
    "returnOnAssets": "ROA",
    "revenueGrowth": "revG",
    "earningsGrowth": "epsG",
    "freeCashflow": "FCF",
    "totalCash": "cash",
    "totalDebt": "debt",
    "fiftyTwoWeekHigh": "hi",
    "52WeekChange": "chg52",
    "SandP52WeekChange": "spx",
    "targetMeanPrice": "tgt",
    "recommendationKey": "rec",
}

# 打印分组（仅影响人类可读输出的排版，不影响 JSON）
PRINT_GROUPS = [
    ["price", "fromHi%", "hi", "chg52", "spx"],
    ["mktcap", "PE", "fwdPE", "PEG", "PS", "PB", "EVE"],
    ["gm", "om", "nm", "ROE", "ROA"],
    ["revG", "epsG", "FCF", "cash", "debt"],
    ["tgt", "tgt%", "rec"],
]

PCT_LEVEL = {"gm", "om", "nm", "ROE", "ROA"}                 # 水位：不强制正号
PCT_SIGNED = {"revG", "epsG", "chg52", "spx"}                # 变动：强制正负号
PCT_FRACTION = PCT_LEVEL | PCT_SIGNED                        # 这些是分数，需 *100
PCT_ALREADY = {"fromHi%", "tgt%"}                            # 这些本来就是百分数
MONEY = {"mktcap", "FCF", "cash", "debt"}
PRICE_LIKE = {"price", "hi", "tgt"}


# ---------------------------------------------------------------- 小工具
def disp_w(s):
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in s)


def pad(s, width):
    return s + " " * max(0, width - disp_w(s))


def money(v):
    """大额金钱：同时给 B/T 与 亿（产业表正文用「亿」，减少换算出错）。"""
    if v is None:
        return "N/A"
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "N/A"
    a = abs(v)
    if a >= 1e12:
        head = f"{v / 1e12:.2f}T"
    elif a >= 1e9:
        head = f"{v / 1e9:.2f}B"
    elif a >= 1e6:
        head = f"{v / 1e6:.1f}M"
    else:
        head = f"{v:,.0f}"
    return f"{head}({v / 1e8:,.1f}亿)"


def fmt(key, v):
    if v is None or v == "":
        return "N/A"
    if key in MONEY:
        return money(v)
    if isinstance(v, str):
        return v
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    if key in PCT_LEVEL:
        return f"{f * 100:.1f}%"
    if key in PCT_SIGNED:
        return f"{f * 100:+.1f}%"
    if key in PCT_ALREADY:
        return f"{f:+.1f}%"
    if key in PRICE_LIKE:
        return f"{f:,.2f}"
    return f"{f:.2f}"


# ---------------------------------------------------------------- universe
def load_universe(path=UNIVERSE_PATH):
    if not path.exists():
        sys.exit(f"找不到标的清单: {path}")
    with open(path, encoding="utf-8") as fh:
        u = json.load(fh)
    rows = u.get("tickers") or []
    if not rows:
        sys.exit(f"标的清单为空: {path}")
    # 行序以 order 为准（universe.json 是权威行序），order 缺失时退回文件顺序。
    rows = sorted(rows, key=lambda r: r.get("order", 10**6))
    return u, rows


def hk_key(code):
    """0700.HK / 00700.HK / hk00700 一律规约成 5 位数字，用于两侧对齐。"""
    c = str(code).strip().upper().replace("HK.", "").replace(".HK", "")
    c = c[2:] if c.startswith("HK") and c[2:].isdigit() else c
    return c.zfill(5) if c.isdigit() else c


# ---------------------------------------------------------------- yfinance
_SESSION_OK = True


def make_session():
    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    return s


def fetch_info(yf, sym, sess):
    """3 次重试；成功判定沿用原表条件。取不到返回 {}。"""
    global _SESSION_OK
    info = {}
    for attempt in range(RETRIES):
        try:
            if _SESSION_OK:
                try:
                    info = yf.Ticker(sym, session=sess).info
                except TypeError:
                    # 极少数 yfinance 版本移除了 Ticker(session=...)。
                    # 退回默认引擎，但必须显式告警：代理环境下 curl_cffi 可能全 null。
                    _SESSION_OK = False
                    print(
                        "⚠ 当前 yfinance 不支持 Ticker(session=...)，已退回默认引擎；"
                        "若 .info 大面积为 null，请 pip install -U yfinance",
                        file=sys.stderr,
                    )
                    info = yf.Ticker(sym).info
            else:
                info = yf.Ticker(sym).info
            if info and (
                info.get("currentPrice")
                or info.get("regularMarketPrice")
                or len(info) > 20
            ):
                return info
        except Exception:
            info = {}
        if attempt < RETRIES - 1:
            time.sleep(RETRY_SLEEP)
    return info or {}


def build_row(info):
    """原表取数逻辑逐字沿用：price 三级回退，fromHi% = round((p/fh-1)*100, 1)。"""
    p = (
        info.get("currentPrice")
        or info.get("regularMarketPrice")
        or info.get("previousClose")
    )
    fh = info.get("fiftyTwoWeekHigh")
    frm = round((p / fh - 1) * 100, 1) if (p and fh) else None
    row = {"price": p, "fromHi%": frm}
    row.update({v: info.get(k) for k, v in F.items()})
    return row


def add_target_upside(row):
    """派生字段：目标价相对现价的缺口（正文「目标价+46%」那一栏），缺任一端记 None。"""
    p, t = row.get("price"), row.get("tgt")
    row["tgt%"] = round((t / p - 1) * 100, 1) if (p and t) else None
    return row


# ---------------------------------------------------------------- 利润率完整性
# yfinance `.info` 的 gm/om/nm 会**单字段损坏**（operatingMargins 尤甚）：2026-09-02 实测
# 46 档里 3 档命中，全在 L2 记忆体层；毛利/净利同时正确 -> 肉眼极难发现，靠人眼在周报里
# 抓每周换一档的坏字段是碰运气，故改成取完数就机器自检。
#
# 只用 .info 自己的 gm/om/nm 就能判，不需要外部源（比较前一律四舍五入到 0.1 个百分点）：
#   om > gm   -> 硬错误。营业利益 = 毛利 − 营业费用，营业费用 >= 0，故 om 恒 <= gm。
#   om == gm  -> 硬错误。真实公司营业费用为 0 的概率为零，这是字段串位的特征。
#   nm > gm   -> **可疑、非错误**。净利可因巨额营业外收入超过毛利，罕见但并非不可能 -> 标待核。
# 命中者才去拉损益表（每档多 2 次网络请求，正常档完全不受影响），用
# Total Revenue / Gross Profit / Operating Income / Net Income 重算**年报**与
# **TTM(季报 4 季相加)** 两组口径，逐字段比对，判断这行是「只坏一个字段」还是「整行弃用」。
#
# 另一类完全不同的东西（勿混为一谈）：om 为正而 nm 大幅为负时，多半不是字段损坏，而是
# 一次性费用。此时去查现金流量表：若存在把该费用原数加回的非现金项（如 Operating Gains
# Losses）且营业现金流/FCF 为正 -> 判「疑似非现金一次性费用」。**措辞一律疑似/待核**，
# 未读 10-K 不得断言成因；这条不是脏值、不会自行消失、补交叉源也无用（同一个 GAAP 数字）。
#
# 硬规则：**绝不静默拿重算值替换 .info 原值**。技能规定「缺失记 N/A 绝不估算」，评级规则
# 规定「利润率冲突时一律不作评级依据」-> 两个口径并列呈现，由写报告的人按规则判断。
# 重算失败（拿不到财报）同样记 N/A，不估算。
MARGIN_MATCH_PT = 0.5     # 重算值与 .info 差 <= 0.5 个百分点即视为「同一口径、对得上」
# om > 0 且 nm < 0 且两者相差 >= 100pt（净亏损大过一整年营收）才走一次性费用分支。
# 100pt 是实测校准的：2026-09-02 全量 46 档里 LITE 差 258pt（FY26 一笔 −77.4 亿的
# Special Income Charges）命中，INTC 差 32pt（常态性减值/权益法亏损，非一次性）不命中。
# 调低这个数会把「营业外费用偏重」的常态公司一并卷进来，那是误报，不是发现。
ONEOFF_GAP_PT = 100.0

# .info 里的原始字段名（告警要指名道姓说「哪个字段可疑」，别只说 om）
INFO_FIELD = {"gm": "grossMargins", "om": "operatingMargins", "nm": "profitMargins"}

# 损益表/现金流量表行名（yfinance 不同标的行名有出入，按顺序回退；一个都没有就记 None）
_IS_ROWS = {
    "rev": ("Total Revenue", "Operating Revenue"),
    "gm": ("Gross Profit",),
    "om": ("Operating Income", "Total Operating Income As Reported"),
    "nm": ("Net Income", "Net Income Common Stockholders", "Net Income Including Noncontrolling Interests"),
}
_CF_ROWS = {
    "addback": ("Operating Gains Losses",),   # 把非现金费用原数加回的那一行
    "ocf": ("Operating Cash Flow",),
    "fcf": ("Free Cash Flow",),
}


def _pt(v):
    """分数 -> 百分点，四舍五入到 0.1pt（三条判据比较前都必须先过这里，容忍浮点噪声）。"""
    if v is None or v == "":
        return None
    try:
        return round(float(v) * 100, 1)
    except (TypeError, ValueError):
        return None


def mpct(v):
    """利润率专用打印（含重算值）：分数 -> 'xx.x%'，缺失 N/A。"""
    p = _pt(v)
    return "N/A" if p is None else f"{p:.1f}%"


def _raw(v):
    """原始百分点值，用于说明四舍五入前的真实差距。"""
    return "N/A" if v is None else f"{v * 100:.2f}%"


def screen_margins(row):
    """只看 .info 的 gm/om/nm 三者关系。返回 (硬错误, 可疑, 是否走一次性费用分支)。"""
    gm_raw, om_raw, nm_raw = row.get("gm"), row.get("om"), row.get("nm")
    gm, om, nm = _pt(gm_raw), _pt(om_raw), _pt(nm_raw)
    errors, suspects = [], []
    if gm is not None and om is not None:
        if om > gm:
            errors.append(f"om>gm（{om:.1f}% > {gm:.1f}%，营业费用不可能为负 -> 算术不可能）")
        elif om == gm:
            # 四舍五入到 0.1pt 后相等；原始值可能是 om 略高于 gm（如 000660.KS 高 0.06pt），
            # 也可能确实逐位相同。两者都是字段串位特征，但别把理由写成「营业费用为 0」。
            errors.append(
                f"om==gm（四舍五入后同为 {gm:.1f}%；原始 om={_raw(om_raw)} gm={_raw(gm_raw)}"
                f" -> 营业费用为 0 或 om 反超，均属字段串位特征）")
    if gm is not None and nm is not None and nm > gm:
        suspects.append(f"nm>gm（{nm:.1f}% > {gm:.1f}%，须有巨额业外收益才成立 -> 罕见，待核）")
    oneoff = (
        om is not None and nm is not None and om > 0 and nm < 0 and (om - nm) >= ONEOFF_GAP_PT
    )
    return errors, suspects, oneoff


def _new_ticker(yf, sym, sess):
    try:
        return yf.Ticker(sym, session=sess) if _SESSION_OK else yf.Ticker(sym)
    except TypeError:                            # 老版本没有 Ticker(session=...)
        return yf.Ticker(sym)


def _frame(t, attr, renew=None):
    """取一张财报表。yfinance 偶发返回空表（限流），重试 RETRIES 次；始终失败返回 None。

    重试必须换一个新的 Ticker（renew）：yfinance 会把一次失败取回的**空表缓存在 Ticker 对象上**
    （1.7.0 实测：get_cash_flow_time_series 里 `if freq not in res: res[freq] = ...`），
    在同一个对象上重读只会读到缓存、一次新请求都不发。2026-10-22 实测这让一次瞬时限流就把
    INTC 的 FCF 从 TTM 打回年报口径、上限 🔵 变 🟡——重试形同虚设，还会改动硬上限。
    """
    for attempt in range(RETRIES):
        if attempt and renew is not None:
            t = renew()
        df = None
        try:
            df = getattr(t, attr)
        except Exception:
            df = None
        if df is not None and not getattr(df, "empty", True) and len(getattr(df, "columns", [])):
            return df
        if attempt < RETRIES - 1:
            time.sleep(RETRY_SLEEP)
    return None


def _usable_columns(df, cols, need):
    """挑出前 cols 个**所需科目都有值**的期别列。

    yfinance 会给刚结束、尚未填数的财季一个占位列：该列其它科目可能有值，
    但营收/毛利/营业利益/净利全是 NaN（实测 SNDK 的 2026-06-30 与 2024-12-31）。
    只看「整列是否全 NaN」会漏掉这种列，旧实现因此让整个 TTM 口径作废，
    还把理由写成「拿不到财报」——那会让人以为是网络或限流，重跑一百次也一样。
    这里逐列检查所需科目，跳过缺数的占位列，用其后真正有数的期别凑满 cols 期；
    凑不满仍返回 None（宁缺勿估，绝不用 3 季凑 TTM）。
    所有科目共用同一组列，保证期别对齐。
    """
    def has(col, names):
        for name in names:
            if name not in df.index:
                continue
            series = df.loc[name]
            if getattr(series, "ndim", 1) > 1:
                series = series.iloc[0]
            try:
                v = float(series.get(col))
            except (TypeError, ValueError):
                continue
            if v == v:                          # 非 NaN
                return True
        return False

    out = []
    for c in df.columns:
        if all(has(c, names) for names in need):
            out.append(c)
            if len(out) == cols:
                break
    return out if len(out) == cols else None


def _row_total(df, names, columns):
    """在给定期别列上把指定行相加。行缺失或任一期为 NaN 一律 None —— 宁缺勿估。"""
    if not columns:
        return None
    for name in names:
        if name not in df.index:
            continue
        series = df.loc[name]
        if getattr(series, "ndim", 1) > 1:      # 极少数标的行名重复
            series = series.iloc[0]
        total = 0.0
        for c in columns:
            try:
                v = float(series.get(c))
            except (TypeError, ValueError):
                return None
            if v != v:                          # NaN：该期缺数，整个口径作废
                return None
            total += v
        return total
    return None


def _stmt_margins(df, cols, basis):
    """按给定期数（年报取最近 1 期、TTM 取最近 4 季相加）重算 gm/om/nm，缺项记 None。"""
    if df is None or len(df.columns) < cols:
        return None
    columns = _usable_columns(df, cols, [_IS_ROWS[k] for k in ("rev", "gm", "om", "nm")])
    if columns is None:
        return None
    rev = _row_total(df, _IS_ROWS["rev"], columns)
    if not rev:
        return None
    out = {"basis": basis, "period_end": str(columns[0])[:10], "periods": cols}
    for k in ("gm", "om", "nm"):
        v = _row_total(df, _IS_ROWS[k], columns)
        out[k] = None if v is None else v / rev
    if all(out[k] is None for k in ("gm", "om", "nm")):
        return None
    return out


def _cash_evidence(t, renew=None):
    """一次性费用分支的依据：非现金加回项 + 营业现金流 + FCF（皆取最近一期年报）。"""
    df = _frame(t, "cashflow", renew)
    if df is None:
        return None
    # 现金流三项各自独立（某项缺数不该拖垮另两项），故逐项挑自己的可用期别列，
    # 不共用一组列——这与损益表口径要求期别对齐的做法不同。
    out = {"period_end": str(df.columns[0])[:10]}
    for k, names in _CF_ROWS.items():
        cols = _usable_columns(df, 1, [names])
        out[k] = _row_total(df, names, cols) if cols else None
    return out


def recompute_margins(yf, sym, sess, want_cash=False):
    """**只有命中自检的标的**才会走到这里（每档多 2~3 次网络请求）。取不到就 None，不估算。"""
    renew = lambda: _new_ticker(yf, sym, sess)  # noqa: E731
    t = renew()
    out = {"annual": None, "ttm": None, "cash": None, "errors": []}
    for key, attr, cols, basis in (
        ("annual", "income_stmt", 1, "年报(最近1期)"),
        ("ttm", "quarterly_income_stmt", 4, "TTM(季报4季)"),
    ):
        try:
            out[key] = _stmt_margins(_frame(t, attr, renew), cols, basis)
        except Exception as e:
            # 异常讯息常带绝对路径，输出会贴进报告正文推 Slack -> 只记类名，不记原文。
            out["errors"].append(f"{key}:{type(e).__name__}")
    if want_cash:
        try:
            out["cash"] = _cash_evidence(t, renew)
        except Exception as e:
            out["errors"].append(f"cash:{type(e).__name__}")
    return out


def _flat_recalc(recomp):
    """重算值摊平进 row：gm_annual/om_annual/nm_annual 与 gm_ttm/om_ttm/nm_ttm，缺失 None。"""
    flat = {}
    for key, suffix in (("annual", "annual"), ("ttm", "ttm")):
        b = (recomp or {}).get(key) or {}
        for k in ("gm", "om", "nm"):
            flat[f"{k}_{suffix}"] = b.get(k)
    return flat


def audit_margins(sym, row, recomp, errors, suspects, oneoff):
    """把 .info 与两组重算值逐字段比对，产出结构化 margin_flags（人类段落与 JSON 共用）。"""
    bases = {k: recomp.get(k) for k in ("annual", "ttm") if recomp.get(k)}
    ok_fields, bad_fields, matched = [], [], {}
    for k in ("gm", "om", "nm"):
        info_pt = _pt(row.get(k))
        if info_pt is None:
            continue
        hit = [
            name for name, b in bases.items()
            if b.get(k) is not None and abs(_pt(b[k]) - info_pt) <= MARGIN_MATCH_PT
        ]
        if hit:
            ok_fields.append(k)
            for name in hit:
                matched[name] = matched.get(name, 0) + 1
        else:
            bad_fields.append(k)
    # 与 .info 吻合的重算口径（对得上的字段最多的那个）；一个都对不上则 None
    matched_basis = max(matched, key=matched.get) if matched else None
    if not bases:
        # 两个口径都没取到时「哪个字段可疑」根本无从判断，别把三个字段一律打成可疑。
        ok_fields, bad_fields = [], []

    cash = recomp.get("cash") or {}
    flag = {
        "ticker": sym,
        "severity": "nonrecurring" if oneoff else ("error" if errors else "suspect"),
        "checks": (errors + suspects) if not oneoff else [
            f"om-nm 差距悬殊（om {_pt(row.get('om')):.1f}% > 0 而 nm {_pt(row.get('nm')):.1f}% 大幅为负）"
        ],
        "info": {INFO_FIELD[k]: row.get(k) for k in ("gm", "om", "nm")},
        "annual": bases.get("annual"),
        "ttm": bases.get("ttm"),
        # 一次性费用与字段串位可以并存（LITE 即两者兼有：.info 营益率 28.0% 实为
        # Normalized EBITDA 28.4%）。所以 oneoff 分支**不再**丢弃字段比对结果。
        "suspect_fields": [INFO_FIELD[k] for k in bad_fields],
        "ok_fields": [INFO_FIELD[k] for k in ok_fields],
        "matched_basis": bases[matched_basis]["basis"] if matched_basis else None,
        "cash_evidence": cash or None,
        "recompute_errors": recomp.get("errors") or [],
    }

    if oneoff:
        add, ocf, fcf = cash.get("addback"), cash.get("ocf"), cash.get("fcf")
        if add and ocf and ocf > 0 and (fcf is None or fcf > 0):
            flag["verdict"] = (
                "系**疑似非现金一次性费用**（待核）：现金流量表存在把该费用原数加回的"
                f"非现金项 Operating Gains Losses {money(add)}，且营业现金流 {money(ocf)}、"
                f"FCF {money(fcf)} 均为正。未读 10-K，不得断言成因；此为真实 GAAP 数字，"
                "不会自行消失、补交叉源亦无用 -> 净利率照实记，评级须并看 TTM 口径与营业现金流"
            )
        else:
            flag["verdict"] = (
                "om 为正而 nm 大幅为负，但现金流量表未见非现金加回项/营业现金流非正 -> "
                "性质待核（既不得当字段损坏、也不得断言为一次性费用），本周利润率不作评级依据"
            )
        return flag

    if not bases:
        flag["verdict"] = (
            "重算失败（拿不到损益表）-> gm/om/nm 一律记 N/A，不估算，本行利润率不作评级依据"
        )
    elif not ok_fields:
        flag["verdict"] = "三项全错（无一对得上重算值）-> 整行利润率弃用，不作评级依据"
    elif bad_fields:
        flag["verdict"] = (
            f"仅 {'/'.join(INFO_FIELD[k] for k in bad_fields)} 损坏；"
            f"{'/'.join(INFO_FIELD[k] for k in ok_fields)} 与{flag['matched_basis']}口径逐项吻合、可用。"
            f"坏字段记 N/A 或改引重算值（须注明口径），不作评级依据"
        )
    else:
        flag["verdict"] = (
            "三项均对得上重算值，但 .info 内部关系仍自相矛盾 -> 口径混用，写表前逐项复核"
        )
    return flag


def check_margin_integrity(yf, sym, row, sess):
    """取完一档就跑：不命中返回 (None, {})，命中才拉财报重算并返回 (flag, 摊平重算值)。"""
    errors, suspects, oneoff = screen_margins(row)
    if not (errors or suspects or oneoff):
        return None, {}
    recomp = recompute_margins(yf, sym, sess, want_cash=oneoff)
    flag = audit_margins(sym, row, recomp, errors, suspects, oneoff)
    return flag, _flat_recalc(recomp)


def print_margin_section(records):
    """人类可读输出结尾单列的「⚠ 利润率完整性」一节。"""
    hits = [r for r in records if r["meta"].get("margin_flags")]
    print("## ⚠ 利润率完整性")
    if not hits:
        print("  本次全部标的利润率关系正常（gm ≥ om）")
        print("")
        return
    print(f"  自检命中 {len(hits)} 档（判据: om>gm 与 om==gm 为硬错误 ｜ nm>gm 为待核 ｜")
    print("  om>0 而 nm 大幅为负走一次性费用分支）；.info 原值一律保留，重算值并列呈现，不静默替换。")
    print("")
    sev = {"error": "硬错误", "suspect": "待核", "nonrecurring": "一次性费用"}
    for rec in hits:
        f = rec["meta"]["margin_flags"]
        print(f"  {f['ticker']}  [{sev.get(f['severity'], f['severity'])}]")
        for c in f["checks"]:
            print(f"     判据: {c}")
        i = f["info"]
        print(
            f"     {pad('.info', 15)}gm={mpct(i['grossMargins'])}  "
            f"om={mpct(i['operatingMargins'])}  nm={mpct(i['profitMargins'])}"
        )
        for key, label in (("annual", "年报重算"), ("ttm", "TTM重算(4季)")):
            b = f.get(key)
            if b:
                print(
                    f"     {pad(label, 15)}gm={mpct(b.get('gm'))}  om={mpct(b.get('om'))}  "
                    f"nm={mpct(b.get('nm'))}   [截至 {b['period_end']}]"
                )
            else:
                print(f"     {pad(label, 15)}N/A（拿不到财报，不估算）")
        if f["suspect_fields"]:
            print(f"     可疑字段: {', '.join(f['suspect_fields'])}"
                  + (f"；可用字段: {', '.join(f['ok_fields'])}" if f["ok_fields"] else ""))
        ce = f.get("cash_evidence")
        if ce:
            print(
                f"     {pad('现金流依据', 15)}Operating Gains Losses={money(ce.get('addback'))}  "
                f"营业现金流={money(ce.get('ocf'))}  FCF={money(ce.get('fcf'))}   [截至 {ce['period_end']}]"
            )
        print(f"     -> {f['verdict']}")
        print("")


def margin_integrity_payload(records):
    """JSON 里与「⚠ 利润率完整性」一节同名的结构化字段（逐档重算值另摊平在各 row 上）。"""
    hits = [r["meta"]["margin_flags"] for r in records if r["meta"].get("margin_flags")]
    return {
        "checked": len(records),
        "clean": not hits,
        "flagged_tickers": [f["ticker"] for f in hits],
        "summary": (
            "本次全部标的利润率关系正常（gm ≥ om）" if not hits
            else f"{len(hits)} 档命中：" + "；".join(
                f"{f['ticker']}={f['severity']}" for f in hits
            )
        ),
        "criteria": {
            "error": ["om>gm", "om==gm"],
            "suspect": ["nm>gm"],
            "nonrecurring": [f"om>0 且 nm<0 且相差>={ONEOFF_GAP_PT}pt"],
            "round_to_pt": 0.1,
            "match_tolerance_pt": MARGIN_MATCH_PT,
        },
        "flagged": hits,
    }


# ---------------------------------------------------------------- 现金流与负债 · 评级上限
# 2026-10-22 起评级规则把**自由现金流**与**负债**纳入硬性压档：FCF 低、负债高 -> 评级上限更低。
# 本脚本按下面的阈值逐档算出 rating_cap（评级上限），`baseline.py validate --caps` 拿它拒收
# 评级高于上限的新表。规则形状写在 references/rating-rules.md 正文，**数字只在这里**：
# 两处各抄一份数字就是 neocloud 两个 TH dict 的陷阱（一边改了另一边没跟上，哪边跑就以哪边为准）。
# 每次运行都把本 dict 原样打印在「现金流与负债」一节表头与 --json 的 cash_debt.criteria 里。
# 改数字只改这里，并在 rating-rules.md 顶部编者注记一笔改动日期与依据。
CASH_DEBT_TH = {
    # FCF 率 = FCF / 同期营收（同一张报表、同一币别 -> 币别中性，ADR/KRW/HKD 行同样适用）
    "fcf_low_margin": 0.05,              # 0 <= FCF 率 < 5% -> 至多 🔵（不得 🟢）
    # FCF < 0 时：OCF > 0 且 净负债/EBITDA <= 1.0（净现金也算）-> 视为资本开支期，至多 🔵；
    # 否则（OCF 不为正，或靠借钱撑资本开支）-> 至多 🟡
    "capex_exempt_max_nd_ebitda": 1.0,
    # 资本开支例外还要设一个烧钱下限：FCF 率低于它就不算「资本开支期」，照样至多 🟡。
    # 否则 SPCX（年报 FCF 率 −75.6%，净现金来自 IPO 而非经营）与 AMZN（−0.3%）同落 🔵。
    "capex_exempt_min_fcf_margin": -0.10,
    "nd_ebitda_elevated": 2.0,           # 净负债/EBITDA > 2.0x -> 至多 🔵
    "nd_ebitda_high": 4.0,               # 净负债/EBITDA > 4.0x，或 EBITDA <= 0 而有净负债 -> 至多 🟡
    # FCF < 0（且不属资本开支期）叠加上一条的高负债 -> 🔴（现金流与负债双弱）
    "ttm_max_span_days": 300,            # TTM 4 季的期末跨度上限：超过即非连续季，不拼 TTM
    "ttm_stale_days": 240,               # TTM 最近一季期末距今超过即标「陈旧」（仍用，但须注明）
    "annual_max_age_days": 550,          # 年报回退：期末距今超过即弃用记 N/A（新鲜的低级源优于陈旧的高级源）
}

# 评级档位从好到坏，与 universe.json 的 ratings 同序（baseline.py 按那份清单比较高低）。
RATING_LADDER = ("🟢", "🔵", "🟡", "🔴")

# **FCF 一律取现金流量表，绝不用 .info 的 freeCashflow。** 2026-10-22 全量实测，.info 的
# freeCashflow 与报表 TTM「Free Cash Flow」（OCF − CapEx）两个方向都会大幅偏离：
#   NVDA 13.8% vs 41.9%｜MSFT 5.0% vs 20.2%｜ALAB 7.6% vs 23.0%｜NET 27.5% vs 12.5%｜SPCX N/A vs 年报 −75.6%
# （FCF 率 = FCF/营收）。疑为 Yahoo 统计页的 levered FCF，定义不公开。拿它压档会把 MSFT 打成
# 「FCF 低」。row 里的 FCF 字段（来自字段映射 F）原样保留，只作参考、不作压档依据。
#
# TTM 必须让现金流量表与损益表**同期对齐**：两表各自挑「有值的前 4 列」会错位
# （实测 MU 营收 TTM 只有 .info 的 0.68 倍，因为现金流表比损益表少一季）。
_CD_CF = {
    "fcf": ("Free Cash Flow",),
    "ocf": ("Operating Cash Flow", "Cash Flow From Continuing Operating Activities"),
}
# 资产负债率只作参考、不触发压档：2026-10-22 实测 NET 75%（净现金）、VRT 70%、ASML 57%
# 主要是客户预付/递延收入这类经营性负债，不是借款；拿它压档会误伤 VRT（🟢）与 ASML。
_CD_BS = {
    "liab": ("Total Liabilities Net Minority Interest",),
    "assets": ("Total Assets",),
}


_NO_FRAME = "拿不到现金流量表或损益表"


def _series(df, names):
    """按行名回退取一行；行名重复时取第一行。拿不到返回 None。"""
    if df is None:
        return None
    for name in names:
        if name in df.index:
            s = df.loc[name]
            return s.iloc[0] if getattr(s, "ndim", 1) > 1 else s
    return None


def _num(series, col):
    """取单格数值；缺失、非数字、NaN 一律 None。"""
    if series is None:
        return None
    try:
        v = float(series.get(col))
    except (TypeError, ValueError):
        return None
    return v if v == v else None


def _col_date(col):
    try:
        return datetime.date.fromisoformat(str(col)[:10])
    except ValueError:
        return None


def _fcf_window(cf, inc, periods, basis, today):
    """在两表**共同**的期别列上取最近 periods 期（FCF 与营收都须有值），返回 (结果, 不可用原因)。"""
    if cf is None or inc is None:
        return None, _NO_FRAME
    fcf_s, ocf_s = _series(cf, _CD_CF["fcf"]), _series(cf, _CD_CF["ocf"])
    rev_s = _series(inc, _IS_ROWS["rev"])
    if fcf_s is None or rev_s is None:
        return None, "报表缺 Free Cash Flow 或 Total Revenue 行"
    common = sorted(set(cf.columns) & set(inc.columns), key=lambda c: str(c), reverse=True)
    cols = [c for c in common if _num(fcf_s, c) is not None and _num(rev_s, c) is not None][:periods]
    if len(cols) < periods:
        return None, f"两表同期且有值的期别不足 {periods} 期"
    dates = [_col_date(c) for c in cols]
    if None in dates:
        return None, "期别列不是日期，无法判断连续性与新鲜度"
    if periods > 1 and (dates[0] - dates[-1]).days > CASH_DEBT_TH["ttm_max_span_days"]:
        return None, (f"最近 {periods} 个有值的季度不连续（{dates[-1]}~{dates[0]}），"
                      "不拿非连续季拼 TTM")
    age = (today - dates[0]).days
    if periods == 1 and age > CASH_DEBT_TH["annual_max_age_days"]:
        return None, f"最近年报期末 {dates[0]} 距今 {age} 天，过旧弃用"
    fcf = sum(_num(fcf_s, c) for c in cols)
    rev = sum(_num(rev_s, c) for c in cols)
    ocf_vals = [_num(ocf_s, c) for c in cols]
    ocf = None if None in ocf_vals else sum(ocf_vals)
    if rev <= 0:
        return None, "同期营收 <= 0，FCF 率无意义"
    return {
        "basis": basis,
        "period_end": str(dates[0]),
        "periods": periods,
        "age_days": age,
        "stale": periods > 1 and age > CASH_DEBT_TH["ttm_stale_days"],
        "fcf": fcf,
        "ocf": ocf,
        "revenue": rev,
        "fcf_margin": fcf / rev,
        "ocf_margin": None if ocf is None else ocf / rev,
    }, None


def _liab_to_assets(t, renew=None):
    """资产负债率（参考值）：最近一季资产负债表，取不到退年报；仍取不到 None。"""
    for attr in ("quarterly_balance_sheet", "balance_sheet"):
        df = _frame(t, attr, renew)
        liab, assets = _series(df, _CD_BS["liab"]), _series(df, _CD_BS["assets"])
        if liab is None or assets is None:
            continue
        for c in df.columns:
            lv, av = _num(liab, c), _num(assets, c)
            if lv is not None and av:
                return {"ratio": lv / av, "period_end": str(c)[:10]}
    return None


def fetch_cash_debt_statements(yf, sym, sess, today):
    """拉报表算 FCF 率（TTM -> 年报 -> N/A）与资产负债率。每档 3~5 次网络请求。"""
    renew = lambda: _new_ticker(yf, sym, sess)  # noqa: E731
    t = renew()
    out = {"fcf": None, "fcf_fallback_notes": [], "liab": None, "errors": []}
    try:
        res, why = _fcf_window(_frame(t, "quarterly_cashflow", renew),
                               _frame(t, "quarterly_income_stmt", renew), 4, "TTM(季报4季)", today)
        if res is None:
            # 回退必须响：记下 TTM 为什么不可用，年报口径的行要能看出它是回退来的。
            # 「拿不到报表」分不清是限流还是该标的本来就没有季报（港股常态），所以照实说两种可能。
            if why == _NO_FRAME:
                why += f"（已换新连接重试 {RETRIES} 次；港股/部分 ADR 本就无季度现金流，其余标的多半是限流，重跑可能取回 TTM）"
            out["fcf_fallback_notes"].append(f"TTM 不可用：{why}")
            res, why = _fcf_window(_frame(t, "cashflow", renew), _frame(t, "income_stmt", renew),
                                   1, "年报(最近1期)", today)
            if res is None:
                out["fcf_fallback_notes"].append(f"年报不可用：{why}")
        out["fcf"] = res
    except Exception as e:
        out["errors"].append(f"fcf:{type(e).__name__}")   # 异常讯息常带绝对路径，只记类名
    try:
        out["liab"] = _liab_to_assets(t, renew)
    except Exception as e:
        out["errors"].append(f"liab:{type(e).__name__}")
    return out


def _finite(v):
    """.info 数值清洗：非数字、NaN、inf 一律 None（随后走既有的 N/A 分支），不让一格脏值拖垮整轮。"""
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None


def _worse(a, b):
    """两个上限取更严的那个（None 表示不设限）。"""
    if a is None:
        return b
    if b is None:
        return a
    return a if RATING_LADDER.index(a) >= RATING_LADDER.index(b) else b


def _x(v):
    return "N/A" if v is None else f"{v:.2f}x"


def _m(v):
    return "N/A" if v is None else f"{v * 100:.1f}%"


def _m2(v):
    """判语专用：与阈值比较的数字多给一位，免得 4.97% 印成「5.0% < 5%」。"""
    return "N/A" if v is None else f"{v * 100:.2f}%"


def evaluate_cash_debt(sym, info, stm):
    """按 CASH_DEBT_TH 判出 FCF 与负债两条的上限，合成 rating_cap。

    JSON 等价规则 2：判不了的一律 evaluated=False / capped=None 并给原因，绝不写成「未触发」。
    """
    th = CASH_DEBT_TH
    # ---- 负债：.info 的 totalDebt / totalCash / ebitda（同一报表币别，比值币别中性）
    # 一律先过 _finite：NaN / 字符串 / inf 会让下面的比较要么抛错拖垮整轮、要么两边都不成立。
    debt, cash, ebitda = (_finite(info.get(k)) for k in ("totalDebt", "totalCash", "ebitda"))
    lev = {"net_debt": None, "ebitda": ebitda, "nd_ebitda": None, "net_cash": None,
           "evaluated": False, "capped": None, "cap": None, "verdict": None, "na_reason": None}
    if debt is None or cash is None:
        # yfinance 对无负债公司有时不给 totalDebt——但「没给」不等于 0，不替它补数
        lev["na_reason"] = f"totalDebt/totalCash 缺失（debt={debt}, cash={cash}），不当 0 处理"
    else:
        nd = debt - cash
        lev["net_debt"], lev["net_cash"] = nd, nd <= 0
        if ebitda is not None and ebitda > 0:
            lev["nd_ebitda"] = nd / ebitda
        if nd <= 0:
            lev.update(evaluated=True, capped=False, verdict="净现金（现金 >= 有息负债），负债不设限")
        elif ebitda is None:
            lev["na_reason"] = "有净负债但 EBITDA 缺失，算不出倍数"
        elif ebitda <= 0:
            lev.update(evaluated=True, capped=True, cap="🟡",
                       verdict=f"EBITDA <= 0 而有净负债 {money(nd)} -> 至多 🟡")
        else:
            r = lev["nd_ebitda"]
            if r > th["nd_ebitda_high"]:
                lev.update(evaluated=True, capped=True, cap="🟡",
                           verdict=f"净负债/EBITDA {_x(r)} > {th['nd_ebitda_high']}x -> 至多 🟡")
            elif r > th["nd_ebitda_elevated"]:
                lev.update(evaluated=True, capped=True, cap="🔵",
                           verdict=f"净负债/EBITDA {_x(r)} > {th['nd_ebitda_elevated']}x -> 至多 🔵")
            else:
                lev.update(evaluated=True, capped=False,
                           verdict=f"净负债/EBITDA {_x(r)} <= {th['nd_ebitda_elevated']}x，负债不设限")
    # 低杠杆三态：True / False / None（判不了）。有净负债但 EBITDA 缺失时必须是 None——
    # 写成 False 会让判语断言一个根本没算出来的「净负债/EBITDA > 1.0x」。
    if lev["net_cash"] is None:
        low_lev, lev_why = None, "杠杆 N/A，无法认定为自有资金撑资本开支"
    elif lev["net_cash"]:
        low_lev, lev_why = True, None
    elif lev["nd_ebitda"] is not None:
        low_lev = lev["nd_ebitda"] <= th["capex_exempt_max_nd_ebitda"]
        lev_why = (f"净负债/EBITDA {_x(lev['nd_ebitda'])} > {th['capex_exempt_max_nd_ebitda']}x，"
                   "靠借钱撑资本开支")
    elif ebitda is not None and ebitda <= 0:
        low_lev, lev_why = False, "EBITDA <= 0 而有净负债，靠借钱撑资本开支"
    else:
        low_lev, lev_why = None, "有净负债但 EBITDA 缺失，杠杆 N/A，无法认定为自有资金撑资本开支"

    # ---- FCF：现金流量表（TTM -> 年报），见上方「绝不用 .info freeCashflow」
    w = stm.get("fcf")
    fcf = {"margin": None, "ocf_margin": None, "basis": None, "period_end": None, "stale": None,
           "capex_exempt": None, "fallback_notes": stm.get("fcf_fallback_notes") or [],
           "evaluated": False, "capped": None, "cap": None, "verdict": None, "na_reason": None}
    if not w:
        fcf["na_reason"] = "；".join(fcf["fallback_notes"] + stm.get("errors", [])) or "拿不到报表 FCF"
    else:
        fcf.update(margin=w["fcf_margin"], ocf_margin=w["ocf_margin"], basis=w["basis"],
                   period_end=w["period_end"], stale=w["stale"], evaluated=True)
        mg, om = w["fcf_margin"], w["ocf_margin"]
        tag = f"[{w['basis']} 截至 {w['period_end']}{'，陈旧' if w['stale'] else ''}] "
        if mg < 0:
            floor = th["capex_exempt_min_fcf_margin"]
            if om is None:
                why = "OCF 缺失，无法认定为资本开支期"
            elif om <= 0:
                why = "OCF 不为正（经营本身在烧钱）"
            elif mg < floor:
                why = f"烧钱幅度超过资本开支例外的下限 {_m2(floor)}"
            else:
                why = lev_why
            # capex_exempt 也守 JSON 规则 2：判不了（OCF 或杠杆 N/A）是 None，不是 False
            exempt = (None if om is None else False if (om <= 0 or mg < floor)
                      else low_lev)
            fcf["capex_exempt"] = exempt
            if exempt:
                lev_txt = ("净现金" if lev["net_cash"]
                           else f"净负债/EBITDA {_x(lev['nd_ebitda'])} <= {th['capex_exempt_max_nd_ebitda']}x")
                fcf.update(capped=True, cap="🔵", verdict=(
                    f"{tag}FCF 率 {_m2(mg)} < 0，但 OCF 率 {_m2(om)} 为正、未低于例外下限 {_m2(floor)}、且{lev_txt}"
                    " -> 视为自有资金撑资本开支，至多 🔵"))
            else:
                fcf.update(capped=True, cap="🟡", verdict=f"{tag}FCF 率 {_m2(mg)} < 0 且{why} -> 至多 🟡")
        elif mg < th["fcf_low_margin"]:
            fcf.update(capped=True, cap="🔵",
                       verdict=f"{tag}FCF 率 {_m2(mg)} < {th['fcf_low_margin'] * 100:.0f}% -> 至多 🔵")
        else:
            fcf.update(capped=False, verdict=f"{tag}FCF 率 {_m2(mg)} >= {th['fcf_low_margin'] * 100:.0f}%，不设限")

    # ---- 合成
    cap = _worse(fcf["cap"], lev["cap"])
    reasons = [v["verdict"] for v in (fcf, lev) if v["capped"]]
    if fcf["cap"] == "🟡" and lev["cap"] == "🟡":
        cap = "🔴"
        reasons.append("FCF 为负（非资本开支期）且高负债 -> 现金流与负债双弱，评 🔴")
    na = [f"{k}: {v['na_reason']}" for k, v in (("FCF", fcf), ("负债", lev)) if not v["evaluated"]]
    evaluated = ("full" if fcf["evaluated"] and lev["evaluated"]
                 else "partial" if fcf["evaluated"] or lev["evaluated"] else "none")
    return {
        "ticker": sym,
        "applicable": True,
        "evaluated": evaluated,
        "cap": cap,
        "reasons": reasons,
        "na_reasons": na,
        "fcf": fcf,
        "leverage": lev,
    }


def not_applicable_cash_debt(sym, why):
    return {"ticker": sym, "applicable": False, "evaluated": "none", "cap": None,
            "reasons": [], "na_reasons": [why], "fcf": None, "leverage": None}


def cash_debt_row_fields(cd, stm, info, row):
    """摊平进 row 的字段：**所有**标的都有这些键（不适用/判不了为 None），下游直读不会 KeyError。"""
    fcf, lev = cd.get("fcf") or {}, cd.get("leverage") or {}
    liab = (stm or {}).get("liab") or {}
    fcf_i, rev_i = row.get("FCF"), info.get("totalRevenue")
    return {
        "fcf_margin": fcf.get("margin"),
        "ocf_margin": fcf.get("ocf_margin"),
        "fcf_basis": fcf.get("basis"),
        "fcf_period_end": fcf.get("period_end"),
        "net_debt": lev.get("net_debt"),
        "nd_ebitda": lev.get("nd_ebitda"),
        "debt_to_equity": info.get("debtToEquity"),   # 参考值（%），负权益时 yfinance 给 None
        "liab_to_assets": liab.get("ratio"),          # 参考值，不触发压档
        "info_fcf_margin": (fcf_i / rev_i) if (fcf_i is not None and rev_i) else None,  # 仅供对照，禁作依据
        "rating_cap": cd.get("cap"),
    }


CASH_DEBT_PROHIBITIONS = [
    "FCF 一律取现金流量表口径（fcf_margin），不得用 .info 的 freeCashflow（row.FCF / info_fcf_margin）"
    "作评级依据——两者实测偏离可达 28pt（NVDA 13.8% vs 41.9%），且方向不定",
    "资产负债率（liab_to_assets）与 D/E（debt_to_equity）只作参考、不触发压档："
    "它们把客户预付/递延收入等经营性负债与借款混在一起（NET 75% 却是净现金）",
    "判不了（N/A）不等于通过：evaluated 不是 full 的标的，论点栏须注明哪一项 N/A，"
    "且不得以「现金流强/负债低」作为 🟢 的依据",
    "上限只往下压、从不往上抬：评级可以比 rating_cap 更低，不能更高",
]


def baseline_ratings(columns):
    """只读当前基准表的评级（代码 -> 评级），用来提前点名「本周须下调」的行。

    读不到就返回 None（不是 {}）：「没比」与「比过了没有要下调的」必须分得开。
    列位置按 universe.json 的 columns 找，与 baseline.py 一致；拆格尊重 `\\|` 转义。
    """
    path = SKILL_DIR / "assets" / "baseline.md"
    cols = columns or []
    code_i = cols.index("代码") if "代码" in cols else 0
    rate_i = cols.index("评级") if "评级" in cols else 4
    try:
        lines = [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines()
                 if ln.strip().startswith("|")]
    except (OSError, UnicodeDecodeError):
        return None
    out = {}
    for ln in lines[2:]:                     # 跳过表头与分隔行
        cells = [c.strip() for c in re.split(r"(?<!\\)\|", ln.strip().strip("|"))]
        if len(cells) > max(code_i, rate_i):
            out[cells[code_i]] = cells[rate_i]
    return out or None


def cash_debt_payload(records, base):
    """JSON 里与「现金流与负债 · 评级上限」一节同名的结构化字段。

    caps 是 baseline.py validate --caps 读取的契约：每个本次取数的代码一项，
    {cap, applicable, evaluated, reasons, na_reasons}。
    """
    cds = [r["meta"]["cash_debt"] for r in records]
    caps = {cd["ticker"]: {k: cd[k] for k in ("cap", "applicable", "evaluated", "reasons", "na_reasons")}
            for cd in cds}
    capped = [cd["ticker"] for cd in cds if cd["cap"]]
    not_eval = [cd["ticker"] for cd in cds if cd["applicable"] and cd["evaluated"] != "full"]
    over = None if base is None else [
        {"ticker": cd["ticker"], "baseline_rating": base.get(cd["ticker"]), "cap": cd["cap"]}
        for cd in cds
        if cd["cap"] and base.get(cd["ticker"]) in RATING_LADDER
        and RATING_LADDER.index(base[cd["ticker"]]) < RATING_LADDER.index(cd["cap"])
    ]
    stale = [cd["ticker"] for cd in cds if (cd.get("fcf") or {}).get("stale")]
    annual = [cd["ticker"] for cd in cds if ((cd.get("fcf") or {}).get("basis") or "").startswith("年报")]
    reasons = []
    if base is None:
        reasons.append("读不到 assets/baseline.md 的评级，未能比对「基准表现有评级是否高于本周上限」")
    elif over:
        reasons.append("基准表现有评级高于本周上限（本周须下调）：" + "；".join(
            f"{o['ticker']} {o['baseline_rating']}->至多{o['cap']}" for o in over))
    if not_eval:
        reasons.append(f"{len(not_eval)} 档现金流/负债未能完整判定：{', '.join(not_eval)}")
    if annual:
        reasons.append(f"{len(annual)} 档 FCF 回退到年报口径：{', '.join(annual)}")
    if stale:
        reasons.append(f"{len(stale)} 档 TTM 期末已陈旧（>{CASH_DEBT_TH['ttm_stale_days']} 天）：{', '.join(stale)}")
    return {
        "criteria": CASH_DEBT_TH,
        "rating_ladder": list(RATING_LADDER),
        "fcf_source": ("现金流量表 Free Cash Flow 行（OCF − CapEx）/ 同期营收；"
                       "TTM=季报最近 4 季且与损益表同期对齐 -> 回退最近年报 -> 再不行 N/A"),
        "leverage_source": "yfinance .info totalDebt − totalCash（净负债）/ ebitda",
        "prohibitions": CASH_DEBT_PROHIBITIONS,
        "summary": (f"{len(capped)} 档设上限：" + "；".join(
            f"{cd['ticker']}≤{cd['cap']}" for cd in cds if cd["cap"]) if capped else "本次无标的触发上限"),
        "capped_tickers": capped,
        "not_fully_evaluated": not_eval,
        "baseline_above_cap": over,
        "degraded": bool(reasons),
        "degraded_reasons": reasons,
        "caps": caps,
    }


def print_cash_debt_section(records, payload):
    """人类可读输出结尾的「现金流与负债 · 评级上限」一节（全部正常时也写，免得被读成没跑）。"""
    th = CASH_DEBT_TH
    print("## 现金流与负债 · 评级上限")
    print(f"  阈值（唯一定义处：fetch_fundamentals.py 的 CASH_DEBT_TH）: "
          f"FCF 率 <0 -> 至多🟡（OCF>0、FCF 率>={th['capex_exempt_min_fcf_margin'] * 100:.0f}% 且 净负债/EBITDA<={th['capex_exempt_max_nd_ebitda']}x 或净现金 -> 至多🔵）｜"
          f"0<=FCF 率<{th['fcf_low_margin'] * 100:.0f}% -> 至多🔵｜"
          f"净负债/EBITDA >{th['nd_ebitda_elevated']}x -> 至多🔵、>{th['nd_ebitda_high']}x 或 EBITDA<=0 有净负债 -> 至多🟡｜"
          "FCF 负（非资本开支期）且负债档至多🟡 -> 🔴")
    print(f"  FCF 口径: {payload['fcf_source']}")
    print("")
    print(f"  {pad('代码', 11)}{pad('FCF率', 9)}{pad('口径/期末', 26)}{pad('OCF率', 9)}"
          f"{pad('净负债/EBITDA', 15)}{pad('资产负债率*', 12)}上限")
    for rec in records:
        cd, row = rec["meta"]["cash_debt"], rec["row"]
        if not cd["applicable"]:
            continue
        f = cd.get("fcf") or {}
        basis = f"{f['basis']} {f['period_end']}" + (" 陈旧" if f.get("stale") else "") if f.get("basis") else "N/A"
        nde = row.get("nd_ebitda")
        lev_txt = "净现金" if (cd.get("leverage") or {}).get("net_cash") else _x(nde)
        print(f"  {pad(cd['ticker'], 11)}{pad(_m(row.get('fcf_margin')), 9)}{pad(basis, 26)}"
              f"{pad(_m(row.get('ocf_margin')), 9)}{pad(lev_txt, 15)}{pad(_m(row.get('liab_to_assets')), 12)}"
              f"{cd['cap'] or '—'}")
    print("  （* 资产负债率只作参考，不触发压档）")
    print("")
    hits = [r["meta"]["cash_debt"] for r in records if r["meta"]["cash_debt"]["cap"]]
    if hits:
        print(f"  设上限 {len(hits)} 档：")
        for cd in hits:
            print(f"    {pad(cd['ticker'], 11)}≤{cd['cap']}  " + "；".join(cd["reasons"]))
    else:
        print("  本次无标的触发上限")
    if payload["baseline_above_cap"] is None:
        print("")
        print("  ⚠ 读不到 assets/baseline.md 的评级，未比对哪些现有评级高于本周上限（不是「没有」）")
    elif payload["baseline_above_cap"]:
        print("")
        print("  ⚠ 基准表现有评级高于本周上限（本周须下调，摘要里注明「现金流/负债压档」及触发数字）：")
        for o in payload["baseline_above_cap"]:
            print(f"    {pad(o['ticker'], 11)}{o['baseline_rating']} -> 至多 {o['cap']}")
    gaps = [r["meta"]["cash_debt"] for r in records
            if r["meta"]["cash_debt"]["applicable"] and r["meta"]["cash_debt"]["evaluated"] != "full"]
    if gaps:
        print("")
        print("  ⚠ 未能完整判定（N/A 不等于通过；论点栏须注明，且不得据此评 🟢）：")
        for cd in gaps:
            print(f"    {pad(cd['ticker'], 11)}" + "；".join(cd["na_reasons"]))
    fallbacks = [r["meta"]["cash_debt"] for r in records
                 if ((r["meta"]["cash_debt"].get("fcf") or {}).get("basis") or "").startswith("年报")]
    if fallbacks:
        print("")
        print("  ⚠ FCF 回退到年报口径（TTM 不可用，期末较旧，引用时注明「年报」）：")
        for cd in fallbacks:
            print(f"    {pad(cd['ticker'], 11)}截至 {cd['fcf']['period_end']}；" + "；".join(cd["fcf"]["fallback_notes"]))
    print("")
    print("  禁令：")
    for p in CASH_DEBT_PROHIBITIONS:
        print(f"    - {p}")
    print("")


# ---------------------------------------------------------------- HK_OVERRIDE
def fetch_hk_quotes(codes):
    """
    调 scripts/hk_quote.py --json 取港股原始未复权行情。

    为什么必须覆盖 yfinance：
      - Yahoo 港股报价延迟 >= 15 分钟；
      - yfinance history() 默认股息复权，0700.HK 52周高被调成 675.1（原始 683.0）、
        0941.HK 被调成 86.3（原始 90.6）-> price / hi / fromHi% 随之失真。
    只覆盖 price / hi / fromHi% 三个字段；财务字段（PE/利润率/ROE/现金流等）仍留 yfinance。
    """
    if not codes:
        return {}, []
    if not HK_QUOTE_SCRIPT.exists():
        return {}, [f"找不到 {HK_QUOTE_SCRIPT}，港股价格无法覆盖"]
    cmd = [sys.executable, str(HK_QUOTE_SCRIPT), *codes, "--json"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except Exception as e:
        return {}, [f"hk_quote.py 调用失败: {e}"]
    if r.returncode != 0:
        tail = (r.stderr or "").strip().splitlines()[-3:]
        return {}, [f"hk_quote.py 退出码 {r.returncode}: {' / '.join(tail)}"]
    try:
        items = json.loads(r.stdout)
    except json.JSONDecodeError as e:
        return {}, [f"hk_quote.py 输出不是合法 JSON: {e}"]
    out, warns = {}, []
    for it in items:
        k = hk_key(it.get("code", ""))
        out[k] = it
        if it.get("error"):
            warns.append(f"{it.get('code')} hk_quote 取数失败: {it['error']}")
    return out, warns


def apply_hk_override(row, meta, quote):
    """用 hk_quote 的 last / hi52 / pct_from_hi52 覆盖 yfinance 同名字段。"""
    if not quote or quote.get("error"):
        meta["price_source"] = "yfinance(港股覆盖失败，价格可能为复权/延迟口径)"
        meta["hk_stale"] = None
        return row, [f"{meta['ticker']}: hk_quote 未返回可用报价，price/hi/fromHi% 仍是 yfinance 口径，不可当收盘价用"]
    warns = []
    if quote.get("last") is not None:
        row["price"] = quote["last"]
    if quote.get("hi52") is not None:
        row["hi"] = quote["hi52"]
    if quote.get("pct_from_hi52") is not None:
        row["fromHi%"] = quote["pct_from_hi52"]
    elif row.get("price") and row.get("hi"):
        row["fromHi%"] = round((row["price"] / row["hi"] - 1) * 100, 1)
    meta["price_source"] = "hk_quote.py(原始未复权)"
    meta["hk_market_status"] = quote.get("market_status")
    meta["hk_quote_time"] = quote.get("quote_time")
    meta["hk_age_min"] = quote.get("age_min")
    meta["hk_hi52_source"] = quote.get("hi52_source")
    meta["hk_stale"] = quote.get("stale")
    if quote.get("stale"):
        warns.append(
            f"{meta['ticker']}: hk_quote stale=true（报价时间 {quote.get('quote_time')}，"
            f"龄 {quote.get('age_min')} 分钟，状态 {quote.get('market_status')}）"
            f" -> 该价不得当最新收盘价用，重跑或记 N/A"
        )
    elif quote.get("market_status") and "已收盘" not in str(quote.get("market_status")):
        warns.append(
            f"{meta['ticker']}: market_status={quote.get('market_status')}"
            f"（非「已收盘(收盘价)」）-> 此为盘中/延迟价，写表前确认口径"
        )
    return row, warns


# ---------------------------------------------------------------- 打印
def print_rows(records, uni_meta, warnings, failed, partial):
    print("# AI 算力产业链 · 基本面取数")
    print(f"# 清单: {rel_path(UNIVERSE_PATH)}")
    print(f"# 取数时间: {datetime.datetime.now().astimezone().isoformat(timespec='seconds')}")
    print(f"# 标的数: {len(records)}（清单共 {uni_meta['total']} 个）")
    print("# 缺失一律 N/A，不估算。gm/om/nm/ROE/ROA/revG/epsG/chg52/spx 已由分数换算成 %。")
    print("")
    for rec in records:
        m, row = rec["meta"], rec["row"]
        tags = []
        if m.get("currency"):
            tags.append(m["currency"])
        if m.get("etf"):
            tags.append("ETF")
        if m.get("ratio_distorted"):
            tags.append("比率失真")
        if m.get("price_source", "").startswith("hk_quote"):
            tags.append("港股原始未复权")
        head = f"[{m['order']:>2}] {pad(m['ticker'], 11)} {m.get('theme', '-')}/{m.get('layer', '-')}"
        if tags:
            head += "  [" + " ".join(tags) + "]"
        if m.get("status") != "ok":
            head += f"  <<{m['status']}>>"
        print(head)
        name = row.get("name")
        print(f"     name={name if name else 'N/A'}")
        for group in PRINT_GROUPS:
            print("     " + "  ".join(f"{k}={fmt(k, row.get(k))}" for k in group))
        cd = m.get("cash_debt") or {}
        if cd.get("applicable"):
            nde = "净现金" if (cd.get("leverage") or {}).get("net_cash") else _x(row.get("nd_ebitda"))
            print(
                f"     现金流/负债: FCF率={_m(row.get('fcf_margin'))}"
                f"({row.get('fcf_basis') or 'N/A'}{' ' + row['fcf_period_end'] if row.get('fcf_period_end') else ''})"
                f"  OCF率={_m(row.get('ocf_margin'))}  净负债/EBITDA={nde}"
                f"  资产负债率={_m(row.get('liab_to_assets'))}(参考)  上限={cd.get('cap') or '—'}"
            )
        if m.get("hk_stale") is not None:
            print(
                f"     hk: status={m.get('hk_market_status')}  quote_time={m.get('hk_quote_time')}"
                f"  age_min={m.get('hk_age_min')}  stale={m.get('hk_stale')}  hi52_src={m.get('hk_hi52_source')}"
            )
        print("")

    if warnings:
        print("## 告警")
        for w in warnings:
            print(f"  ⚠ {w}")
        print("")
    if partial:
        print(f"## 取数不完整（{len(partial)}）: 有 info 但拿不到 price，相关字段记 N/A")
        print("  " + ", ".join(partial))
        print("")
    if failed:
        print(f"## 取数失败（{len(failed)}）: 重跑命令")
        print("  " + ", ".join(failed))
        print(f"  python3 {rel_path(Path(__file__))} --tickers {','.join(failed)}" + "   # 从技能目录下跑，或改用绝对路径")
        print("")
    if not failed and not partial:
        print("## 全部标的取数成功")
        print("")

    # 「⚠ 利润率完整性」固定单列一节、放在最后：全部正常时也写一行，
    # 免得「没有这一节」被读成「没跑自检」。
    print_margin_section(records)


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(
        description="按 assets/universe.json 批量抓取基本面（yfinance + 港股 hk_quote.py 覆盖）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--tickers", help="只取这些标的（逗号分隔，如 NVDA,TSM,0700.HK）")
    ap.add_argument("--json", metavar="PATH", help="额外把结果写成 JSON")
    ap.add_argument("--quiet", action="store_true", help="只写 JSON，不做人类可读打印")
    args = ap.parse_args()

    uni, rows = load_universe()

    if args.tickers:
        want = [t.strip() for t in args.tickers.split(",") if t.strip()]
        idx = {r["ticker"].upper(): r for r in rows}
        picked, unknown = [], []
        for t in want:
            r = idx.get(t.upper())
            (picked.append(r) if r else unknown.append(t))
        if unknown:
            sys.exit(f"以下代码不在 {UNIVERSE_PATH.name} 里: {', '.join(unknown)}")
        rows = picked

    try:
        import yfinance as yf
    except ImportError:
        sys.exit(
            "缺少 yfinance。请先执行：\n"
            "  pip install -q yfinance requests\n"
            "（PEP668 报错时加 --break-system-packages）"
        )

    sess = make_session()

    # 港股先批量取一次实时行情（一次 subprocess 取全部，省得逐只起进程）
    hk_rows = [r for r in rows if r.get("hk_quote")]
    hk_quotes, warnings = fetch_hk_quotes([r["ticker"] for r in hk_rows])

    today = datetime.date.today()
    records, failed, partial = [], [], []
    for r in rows:
        sym = r["ticker"]
        info = fetch_info(yf, sym, sess)
        row = build_row(info)
        meta = {
            "order": r.get("order"),
            "ticker": sym,
            "theme": r.get("theme"),
            "layer": r.get("layer"),
            "currency": r.get("currency"),
            "etf": bool(r.get("etf")),
            "ratio_distorted": bool(r.get("ratio_distorted")),
            "hk_quote": bool(r.get("hk_quote")),
            "price_source": "yfinance",
            "info_keys": len(info),
        }
        if r.get("hk_quote"):
            row, w = apply_hk_override(row, meta, hk_quotes.get(hk_key(sym)))
            warnings.extend(w)
        add_target_upside(row)

        # 利润率完整性自检：不命中就一次网络请求都不多花；命中者才拉损益表重算。
        flag, recalc = check_margin_integrity(yf, sym, row, sess)
        # 六个重算键在**所有**标的上都存在（未命中档为 None），
        # 否则下游按 row['gm_ttm'] 直读会 KeyError。
        row.update({k: None for k in ("gm_annual", "om_annual", "nm_annual",
                                      "gm_ttm", "om_ttm", "nm_ttm")})
        if flag:
            meta["margin_flags"] = flag
            row.update(recalc)

        # 现金流与负债 · 评级上限：ETF 没有公司财报，取数失败的行没有 .info，两者都不判。
        stm = None
        if r.get("etf"):
            cd = not_applicable_cash_debt(sym, "ETF 无公司财报，本规则不适用（ETF 照正文 ETF 规则评级）")
        elif not info:
            cd = not_applicable_cash_debt(sym, "取数失败，无 .info 可判")
            cd["applicable"] = True        # 本该判却没判成：算进「未能完整判定」，不能悄悄当成不适用
        else:
            stm = fetch_cash_debt_statements(yf, sym, sess, today)
            try:
                cd = evaluate_cash_debt(sym, info, stm)
            except Exception as e:        # 一档的脏数据不得拖垮整轮、让第三步拿不到 --json
                cd = not_applicable_cash_debt(sym, f"判定异常（{type(e).__name__}），本档未判")
                cd["applicable"] = True
        meta["cash_debt"] = cd
        row.update(cash_debt_row_fields(cd, stm, info, row))

        if not info and row.get("price") is None:
            meta["status"] = "取数失败"
            failed.append(sym)
        elif row.get("price") is None:
            meta["status"] = "取数不完整(无 price)"
            partial.append(sym)
        else:
            meta["status"] = "ok"
        records.append({"meta": meta, "row": row})

    uni_meta = {"total": len(uni.get("tickers") or []), "columns": uni.get("columns")}

    cd_payload = cash_debt_payload(records, baseline_ratings(uni.get("columns")))
    # --quiet 只关人类可读渲染、从不关告警（JSON 规则 4）：现金流/负债的降级与「须下调」照样进 stderr
    for reason in cd_payload["degraded_reasons"]:
        print(f"⚠ 现金流/负债: {reason}", file=sys.stderr)
    top_reasons = list(cd_payload["degraded_reasons"])
    if failed:
        top_reasons.insert(0, f"{len(failed)} 档取数失败：{', '.join(failed)}")
    if partial:
        top_reasons.insert(0, f"{len(partial)} 档取数不完整（无 price）：{', '.join(partial)}")

    if args.json:
        payload = {
            "fetched_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
            "universe": rel_path(UNIVERSE_PATH),
            "universe_total": uni_meta["total"],
            "requested": len(records),
            "ok": len(records) - len(failed) - len(partial),
            "degraded": bool(top_reasons),
            "degraded_reasons": top_reasons,
            "failed": failed,
            "partial": partial,
            "warnings": warnings,
            "margin_integrity": margin_integrity_payload(records),
            "cash_debt": cd_payload,
            "field_map": F,
            "rows": [{**rec["meta"], **rec["row"]} for rec in records],
        }
        out = Path(args.json).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        # 用户自传的路径同样含用户名，而本脚本输出会被贴进报告并推 Slack。
        print(f"JSON 已写入 {tilde_path(out)}", file=sys.stderr)

    if not args.quiet:
        print_rows(records, uni_meta, warnings, failed, partial)
        print_cash_debt_section(records, cd_payload)

    sys.exit(1 if records and len(failed) == len(records) else 0)


if __name__ == "__main__":
    main()
