#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
neocloud_credit_monitor.py —— 引爆点④「Neocloud 信用利差」日更取数脚本 (tripwire v2)

对应《AI算力产业链回调进场监控日更.md》「第二步之四：Neocloud 信用层监控」。
本脚本只负责**取数与规则化判定**，不做预测、不做投资建议；判读文字由日报生成。

四层判定（L1 公司层 / L2 项目层 / L3 宏观确认 / L4 上游传染）：
  L1 = neocloud 自身融资成本 → 衡量「股东被稀释多少」，不等于论点破
  L2 = 投资级租户项目债   → 衡量「AI 算力现金流本身是否被质疑」= 真正的主题指标
  L3 = HY/CCC 指数        → 区分个体问题 vs 全市场风险偏好（日更文件要求的相对基准检验）
  L4 = 上游(NVDA/hyperscaler capex) → 最终裁决：融资链问题是否已回传到算力需求

数据源（全部免费、无需 API key）：
  FRED CSV      https://fred.stlouisfed.org/graph/fredgraph.csv?id=<SERIES>   信用指数 OAS / 美债曲线 / SOFR / VIX
  yfinance      信用 ETF、杠杆贷款 ETF、neocloud 个股、基准
  neocloud_bonds.json  债券条款 + WebSearch 取得的报价 + 一级市场条款 + 定性开关

用法：
  python3 neocloud_credit_monitor.py                 # 输出 markdown（可直接贴进日报）
  python3 neocloud_credit_monitor.py --json          # 输出 JSON（机读）
  python3 neocloud_credit_monitor.py --compact       # 只输出精简版一行（Slack 用）
  python3 neocloud_credit_monitor.py --no-history    # 不写入历史档（试跑用）
  python3 neocloud_credit_monitor.py --max-quote-age 3
"""

import argparse
import io
import json
import math
import re
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

# 资产档一律锚在**技能根目录的 assets/**，与同技能 industry_table.py / technicals.py /
# perp_quotes.py 的 SKILL_ROOT 锚法一致。早期版本锚在脚本自身目录（scripts/），
# vendor 进技能目录时漏改，导致开箱即 FileNotFoundError。
SCRIPT_PATH = Path(__file__).resolve()
SKILL_ROOT = SCRIPT_PATH.parent.parent                   # <...>/ai-pullback-daily
SKILLS_DIR = SKILL_ROOT.parent                           # skills/ 或 ~/.claude/skills/
ASSETS_DIR = SKILL_ROOT / "assets"
BONDS_PATH = ASSETS_DIR / "neocloud_bonds.json"
HISTORY_PATH = ASSETS_DIR / "neocloud_credit_history.jsonl"


# ---------------------------------------------------------------- 路径相对化
# 本脚本的输出会被贴进日更报告正文并推 Slack，绝不能把 /Users/<用户名>/... 带出去。
# 任何对外打印的路径（含错误信息）都必须先过 rel_display() / tilde_path() / scrub()。

_HOMEISH_RE = re.compile(r"(?:/Users|/home|/var/folders)/[^/\s\"']+")


def scrub(text) -> str:
    """把文本里的家目录绝对路径折叠掉，用于错误信息与异常讯息。"""
    s = str(text)
    try:
        home = str(Path.home())
    except Exception:
        home = ""
    if home:
        s = s.replace(home, "~")
    return _HOMEISH_RE.sub("~", s)


def rel_display(path) -> str:
    """技能内的文件一律相对「技能安装目录」展示，如 assets/neocloud_bonds.json。"""
    p = Path(path)
    try:
        p = p.resolve()
    except OSError:
        pass
    for base in (SKILL_ROOT, SKILLS_DIR):
        try:
            return str(p.relative_to(base))
        except ValueError:
            continue
    return p.name


def tilde_path(path) -> str:
    """诊断用路径：先试相对化，再退回 ~ 折叠，最后只留档名。"""
    p = Path(path)
    s = scrub(p)
    try:
        return str(p.resolve().relative_to(SKILL_ROOT))
    except (ValueError, OSError):
        pass
    return s if not _HOMEISH_RE.search(s) else p.name


def err(msg: str = "") -> None:
    print(scrub(msg), file=sys.stderr)

# ---------------------------------------------------------------- 配置

# FRED 信用/利率序列。key = 报告显示名, value = (series_id, 说明)
FRED_CREDIT = {
    "HY OAS":            ("BAMLH0A0HYM2",    "ICE BofA US High Yield 指数 OAS —— 全市场风险偏好基准"),
    "BB OAS":            ("BAMLH0A1HYBB",    "HY 内最高评级档"),
    "Single-B OAS":      ("BAMLH0A2HYB",     "CRWV 评级同侪(S&P B) —— 个体溢价的正确分母"),
    "CCC & Lower OAS":   ("BAMLH0A3HYC",     "HY 尾部 —— neocloud 公司层债实际所处的风险档"),
    "IG Corp OAS":       ("BAMLC0A0CM",      "投资级整体"),
    "BBB OAS":           ("BAMLC0A4CBBB",    "IG 最低档 —— 超大规模企业发债供给冲击落点"),
    "EUR HY OAS":        ("BAMLHE00EHYIOAS", "欧元高收益 —— EUR 债的相对基准"),
}
FRED_RATES = {
    "UST 2Y":  ("DGS2",  2.0),
    "UST 3Y":  ("DGS3",  3.0),
    "UST 5Y":  ("DGS5",  5.0),
    "UST 7Y":  ("DGS7",  7.0),
    "UST 10Y": ("DGS10", 10.0),
}
FRED_OTHER = {
    "SOFR": "SOFR",
    "VIX":  "VIXCLS",
}

# yfinance：信用/杠杆贷款 ETF（一级市场氛围的可交易代理）
CREDIT_ETFS = {
    "HYG":  "iShares HY 公司债",
    "JNK":  "SPDR HY 公司债",
    "LQD":  "iShares IG 公司债",
    "ANGL": "堕落天使 HY",
    "BKLN": "高级担保杠杆贷款 —— CRWV 那笔 TLB 所在市场",
    "SRLN": "主动型杠杆贷款",
}

# yfinance：neocloud / AI 数据中心股 + 基准
NEOCLOUD_EQUITY = {
    "CRWV": "CoreWeave",
    "NBIS": "Nebius",
    "IREN": "IREN",
    "APLD": "Applied Digital",
    "WULF": "TeraWulf",
    "CIFR": "Cipher Mining",
    "GLXY": "Galaxy Digital",
    "DTCR": "Global X 数据中心 ETF",
}
BENCHMARK_EQUITY = {
    "NVDA": "上游算力核心 (L4 裁决)",
    "SMH":  "半导体",
    "QQQ":  "大盘科技",
}

# 判定阈值（v2）—— 集中在这里，方便日后校准
TH = {
    "L1_corp_spread_amber_bp": 600,
    "L1_corp_spread_red_bp": 800,
    "L1_primary_reprice_amber_bp": 100,
    "L1_primary_window_days": 30,         # 一级市场重定价的观察窗；超过则只作历史参照，避免🟡永久卡住
    "L1_cohort_premium_amber_bp": 250,   # 公司层利差 − 同评级指数 OAS
    "L1_cohort_premium_red_bp": 500,
    "L2_spv_spread_amber_bp": 250,
    "L2_spv_spread_red_bp": 400,
    "L2_spv_price_amber": 95.0,
    "L3_ccc_90d_amber_bp": 150,
    "L3_hy_oas_red_pct": 4.00,
    "L3_ccc_minus_bb_amber_bp": 800,
    "L4_nvda_drawdown_amber_pct": -25.0,
    "excess_widening_idio_bp": 50,       # neocloud 走阔 − HY 走阔，超过则判个体
}

GREEN, AMBER, RED, GREY = "🟢", "🟡", "🔴", "⚪"
RANK = {GREY: -1, GREEN: 0, AMBER: 1, RED: 2}


def worst(*states):
    """取最严重状态；⚪ 不参与升档（缺数据不得推高结论）。"""
    real = [s for s in states if s in RANK and s != GREY]
    if not real:
        return GREY
    return max(real, key=lambda s: RANK[s])


# ---------------------------------------------------------------- 取数

def _ssl_context():
    """有 certifi 就用它（macOS 系统 Python 常缺 CA 根憑證）。"""
    import ssl
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def _http_get(url, timeout=12):
    """urllib 优先；失败则回退 curl（跨环境可移植：本机、cron、云端）。

    注意：**不要**带自订 User-Agent —— fredgraph.csv 对自订 UA 会挂住直到超时
    （实测 UA='Mozilla/5.0 (...)' → 10s timeout；不带 UA → 0.7s 正常返回）。
    """
    try:
        with urllib.request.urlopen(url, timeout=timeout, context=_ssl_context()) as r:
            return r.read().decode("utf-8")
    except Exception:
        import subprocess
        p = subprocess.run(["curl", "-sS", "--max-time", str(timeout), url],
                           capture_output=True, text=True)
        if p.returncode != 0 or not p.stdout.strip():
            raise RuntimeError(f"urllib 与 curl 皆失败: {p.stderr.strip()[:200]}")
        return p.stdout


def fetch_fred(series_id, start=None, timeout=25):
    """抓 FRED CSV（公开端点、无需 key）。回传 pd.Series(index=date, float)。"""
    if start is None:
        start = (date.today() - timedelta(days=500)).isoformat()
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}&cosd={start}"
    txt = _http_get(url, timeout=timeout)
    df = pd.read_csv(io.StringIO(txt))
    datecol = df.columns[0]          # 'observation_date' 或 'DATE'，两种格式都吃
    valcol = df.columns[-1]
    df[valcol] = pd.to_numeric(df[valcol], errors="coerce")   # '.' → NaN
    s = pd.Series(df[valcol].values, index=pd.to_datetime(df[datecol])).dropna()
    s.name = series_id
    return s


def chg_bp(s, days):
    """s 单位为 %（OAS/殖利率）；回传 days 日历日前至今的变动 bp。"""
    if s is None or len(s) < 2:
        return None
    last_dt = s.index[-1]
    target = last_dt - pd.Timedelta(days=days)
    prior = s[s.index <= target]
    if prior.empty:
        return None
    return round((s.iloc[-1] - prior.iloc[-1]) * 100, 1)


def pull_fred_block():
    out, errors = {}, []
    for name, (sid, desc) in FRED_CREDIT.items():
        try:
            s = fetch_fred(sid)
            out[name] = {
                "series_id": sid, "desc": desc,
                "level_pct": round(float(s.iloc[-1]), 2),
                "as_of": s.index[-1].date().isoformat(),
                "d1_bp": chg_bp(s, 1), "d5_bp": chg_bp(s, 7),
                "d30_bp": chg_bp(s, 30), "d90_bp": chg_bp(s, 90),
                "_series": s,
            }
        except Exception as e:
            errors.append(f"FRED {sid} ({name}): {type(e).__name__} {e}")
    for name, (sid, tenor) in FRED_RATES.items():
        try:
            s = fetch_fred(sid)
            out[name] = {
                "series_id": sid, "tenor_y": tenor,
                "level_pct": round(float(s.iloc[-1]), 3),
                "as_of": s.index[-1].date().isoformat(),
                "d1_bp": chg_bp(s, 1), "d5_bp": chg_bp(s, 7),
                "d30_bp": chg_bp(s, 30), "d90_bp": chg_bp(s, 90),
            }
        except Exception as e:
            errors.append(f"FRED {sid} ({name}): {type(e).__name__} {e}")
    for name, sid in FRED_OTHER.items():
        try:
            s = fetch_fred(sid)
            out[name] = {
                "series_id": sid,
                "level_pct": round(float(s.iloc[-1]), 3),
                "as_of": s.index[-1].date().isoformat(),
                "d1_bp": chg_bp(s, 1), "d5_bp": chg_bp(s, 7),
            }
        except Exception as e:
            errors.append(f"FRED {sid} ({name}): {type(e).__name__} {e}")
    return out, errors


def pull_equities(tickers):
    """yfinance 批量取价；回传 {ticker: {...}}。个别失败不影响其余。"""
    out, errors = {}, []
    try:
        import yfinance as yf
    except ImportError as e:
        # 缺套件不该让整份报告消失：L4 落⚪、股债背离段略过，其余照常输出
        return out, [f"yfinance 未安装（{e.name}）→ L4 上游传染与股债背离段缺数据"]
    try:
        raw = yf.download(list(tickers), period="1y", interval="1d", progress=False,
                          auto_adjust=False, group_by="ticker", threads=True)
    except Exception as e:
        return out, [f"yfinance download 整批失败: {type(e).__name__} {e}"]
    for t in tickers:
        try:
            df = raw[t].dropna(subset=["Close"])
            if df.empty:
                errors.append(f"yfinance {t}: 无资料")
                continue
            c = df["Close"]

            def ret(n):
                return round(100 * (c.iloc[-1] / c.iloc[-1 - n] - 1), 2) if len(c) > n else None

            hi52 = float(df["High"].tail(252).max())
            vol = c.pct_change().tail(30).std() * math.sqrt(252) * 100
            out[t] = {
                "close": round(float(c.iloc[-1]), 2),
                "as_of": c.index[-1].date().isoformat(),
                "d1_pct": ret(1), "d5_pct": ret(5), "d21_pct": ret(21), "d63_pct": ret(63),
                "hi52": round(hi52, 2),
                "dd_from_hi52_pct": round(100 * (float(c.iloc[-1]) / hi52 - 1), 1),
                "vol30_ann_pct": round(float(vol), 0) if not np.isnan(vol) else None,
                "volume": int(df["Volume"].iloc[-1]),
                "vol20_avg": int(df["Volume"].tail(21)[:-1].mean()),
            }
        except Exception as e:
            errors.append(f"yfinance {t}: {type(e).__name__} {e}")
    return out, errors


# ---------------------------------------------------------------- 债券定价

def coupon_times(years, freq=2):
    """回传剩余付息时点（年，升序，最后一笔=到期）。"""
    n = max(1, int(math.ceil(years * freq)))
    return [years - k / freq for k in range(n - 1, -1, -1)]


def accrued_interest(coupon, years, freq=2):
    """应计利息。

    干净价(clean) + 应计 = 全价(dirty)，而现金流折现出来的是**全价**。
    早期版本拿干净价直接与折现值比对，会把 YTM 高估最多约 70bp
    （偏差随「距下次付息还有多久」变化：刚付完息偏差小，快付息偏差大），
    进而虚增利差、可能误触发 L1/L2。此函数即为修正项。
    """
    period = 1.0 / freq
    t_next = coupon_times(years, freq)[0]             # 距下次付息(年)
    elapsed_frac = max(0.0, min(1.0, (period - t_next) / period))
    return (coupon / freq) * elapsed_frac


def bond_price_from_ytm(y, coupon, years, freq=2):
    """由 YTM 折现出**全价(dirty)**；半年付息。"""
    if years <= 0:
        return 100.0
    c = coupon / freq
    disc = 1 + y / freq
    pv = sum(c / disc ** (t * freq) for t in coupon_times(years, freq) if t > 0)
    pv += 100.0 / disc ** (years * freq)
    return pv


def solve_ytm(clean_price, coupon, years, freq=2):
    """由**干净价**二分法解 YTM，回传小数（0.0982 = 9.82%）。

    求解式：折现全价 == 干净价 + 应计利息。
    """
    if clean_price is None or clean_price <= 0 or years <= 0:
        return None
    dirty = clean_price + accrued_interest(coupon, years, freq)
    lo, hi = 1e-6, 3.0
    if bond_price_from_ytm(hi, coupon, years, freq) > dirty:
        return None                                   # 价格过低，超出求解区间
    for _ in range(200):
        mid = (lo + hi) / 2
        if bond_price_from_ytm(mid, coupon, years, freq) > dirty:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def interp_ust(fred, years):
    """由 DGS 曲线线性插值出对应年期无风险利率(%)。"""
    pts = []
    for name, (sid, tenor) in FRED_RATES.items():
        if name in fred:
            pts.append((tenor, fred[name]["level_pct"]))
    if not pts:
        return None, None
    pts.sort()
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return round(float(np.interp(years, xs, ys)), 3), f"DGS 曲线插值 @{years:.1f}y"


def price_bonds(cfg, fred, today, max_quote_age):
    rows, errors = [], []
    for b in cfg["bonds"]:
        q = b.get("quote") or {}
        price, as_of = q.get("price"), q.get("as_of")
        r = {
            "key": b["key"], "label": b["label"], "issuer": b["issuer"], "tier": b["tier"],
            "currency": b["currency"], "coupon": b["coupon"], "maturity": b["maturity"],
            "rating": b.get("rating"), "rating_cohort": b.get("rating_cohort"),
            "is_l1_benchmark": bool(b.get("is_l1_benchmark")),
            "is_l2_benchmark": bool(b.get("is_l2_benchmark")),
            "price": price, "quote_as_of": as_of, "quote_source": q.get("source"),
            "note": b.get("note"),
            "ytm_pct": None, "bench_pct": None, "bench_label": None,
            "spread_bp": None, "cohort_premium_bp": None,
            "stale": True, "quote_age_days": None, "usable": False,
        }
        if price is None or not as_of:
            r["stale"] = True
            errors.append(f"债券 {b['key']}: 无报价（quote.price / quote.as_of 未填）")
            rows.append(r)
            continue
        age = (today - date.fromisoformat(as_of)).days
        r["quote_age_days"] = age
        r["stale"] = age > max_quote_age
        years = (date.fromisoformat(b["maturity"]) - today).days / 365.25
        r["years_to_maturity"] = round(years, 2)
        y = solve_ytm(price, b["coupon"], years)
        if y is None:
            errors.append(f"债券 {b['key']}: YTM 求解失败 (price={price})")
            rows.append(r)
            continue
        r["ytm_pct"] = round(y * 100, 2)
        if b["currency"] == "USD":
            bench, blabel = interp_ust(fred, years)
            if bench is not None:
                r["bench_pct"], r["bench_label"] = bench, blabel
                r["spread_bp"] = round((r["ytm_pct"] - bench) * 100, 0)
        else:
            # EUR 债不与美债曲线比 —— 只对 EUR HY 指数做相对比较，绝对利差标 N/A
            eur = fred.get("EUR HY OAS")
            if eur:
                r["bench_label"] = f"EUR HY 指数 OAS {eur['level_pct']:.2f}%（非无风险曲线，仅相对比较）"
        # 同评级指数溢价：公司层债的「个体信用溢价」
        cohort_map = {"single_b": "Single-B OAS", "bb": "BB OAS", "ccc": "CCC & Lower OAS"}
        cname = cohort_map.get(b.get("rating_cohort") or "")
        if r["spread_bp"] is not None and cname in fred:
            r["cohort_index"] = cname
            r["cohort_index_bp"] = round(fred[cname]["level_pct"] * 100, 0)
            r["cohort_premium_bp"] = round(r["spread_bp"] - r["cohort_index_bp"], 0)
        r["usable"] = (r["spread_bp"] is not None) and (not r["stale"]) and b["tier"] != "convert"
        rows.append(r)
    return rows, errors


def all_in_loan_cost(deal, sofr_pct):
    """杠杆贷款全包成本 ≈ 基准 + spread + OID 摊销（按 3 年平均寿命）。"""
    out = {}
    for stage in ("initial", "final"):
        sp = deal.get(f"{stage}_spread_bp")
        oid = deal.get(f"{stage}_oid")
        if sp is None:
            out[stage] = None
            continue
        base = sofr_pct if (deal.get("base_rate") or "").upper() == "SOFR" else 0.0
        oid_amort = ((100 - oid) / 3.0) if oid else 0.0     # 3年平均寿命摊销
        out[stage] = round(base + sp / 100.0 + oid_amort, 2)
    if out.get("initial") is not None and out.get("final") is not None:
        out["delta_bp"] = round((out["final"] - out["initial"]) * 100, 0)
    return out


# ---------------------------------------------------------------- 历史档

def load_history():
    """读历史档；缺档 = 首次运行，不是错误。读不动只警告，不抛裸 traceback。"""
    if not HISTORY_PATH.exists():
        return []
    recs = []
    try:
        with open(HISTORY_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        recs.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
    except OSError as e:
        err(f"⚠️ 历史档读取失败（{rel_display(HISTORY_PATH)}）：{type(e).__name__}；"
            f"本次相对基准检验改走水平法。")
        return []
    return recs


def append_history(rec):
    """一天一笔：同日重跑覆盖当日纪录，避免历史档被同日多次运行灌爆。"""
    recs = [r for r in load_history() if r.get("date") != rec["date"]]
    recs.append(rec)
    try:
        HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(HISTORY_PATH, "w", encoding="utf-8") as f:
            for r in recs:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    except OSError as e:
        err(f"⚠️ 历史档写入失败（{rel_display(HISTORY_PATH)}）：{type(e).__name__}；"
            f"本次读数照常输出，但 30 日变化率检验的基准点未累积。")


def hist_bond_stale(rec, entry, max_quote_age):
    """历史档里的这一笔债券读数是否由**过期报价**算出。

    过期报价算出的 spread_bp 只反映当日 DGS 曲线，与信用无关：拿它当 30 日变化率的
    基准，会把纯粹的曲线漂移读成「利差收窄/走阔」，进而污染「个体 vs 宏观」判定
    ——而该判定正是引爆点④ 能否升🔴 的闸门。
    新格式直接带 "stale" 标记；旧纪录（无标记）由「纪录日 − 报价日」回推。
    """
    if entry.get("stale") is not None:
        return bool(entry["stale"])
    quoted, logged = entry.get("quote_as_of"), rec.get("date")
    if not quoted or not logged:
        return True                                   # 无从判断 → 保守视为陈旧，不当基准
    try:
        age = (date.fromisoformat(logged) - date.fromisoformat(quoted)).days
    except (ValueError, TypeError):
        return True
    return age > max_quote_age


def hist_spread_delta(history, key, days, today, max_quote_age=5):
    """从历史档取 days 日历日前该债的利差，回传 (参照日期, 利差bp)。

    只认「当日报价仍新鲜」的纪录；找不到新鲜参照点则回传 None，由呼叫端走水平法回退。
    """
    target = today - timedelta(days=days)
    best = None
    for rec in history:
        try:
            d = date.fromisoformat(rec["date"])
        except Exception:
            continue
        if d > target:
            continue
        entry = (rec.get("bonds") or {}).get(key) or {}
        sp = entry.get("spread_bp")
        if sp is None:
            continue
        if hist_bond_stale(rec, entry, max_quote_age):
            continue
        if best is None or d > best[0]:
            best = (d, sp)
    return best


# ---------------------------------------------------------------- 判定引擎

def _deal_date_key(deal):
    """一级市场交易的排序键：(可解析?, 日期)，供「取最近一笔」用。

    日期非 ISO（登记表允许「2026-06-中」这类粗略写法）不得抛例外，只排到最后。
    """
    try:
        return (1, date.fromisoformat(deal.get("date") or ""))
    except (ValueError, TypeError, AttributeError):
        return (0, date.min)


def _layer_incomplete(layer: dict, states: list) -> bool:
    """判断某一层的数据是否不完整。

    不能扫 states 列表——GREY 从来不会被 append 进去（各分支只把 {"state": GREY}
    写进 ev[L] 的子指标字典），所以 `any(x == GREY for x in states)` 恒为 False。
    必须扫**子指标**：任一子指标为 ⚪ 即该层不完整，
    但带 excluded_by_design 的（如超出观察窗的一级市场交易）是「有意不计入」
    而非「没数据」，不算不完整。
    """
    if not states:
        return True
    for k, v in layer.items():
        if not isinstance(v, dict) or k == "state":
            continue
        if v.get("state") == GREY and not v.get("excluded_by_design"):
            return True
    return False


def _incomplete_layers(ev):
    """返回数据不完整的层名列表。

    两种都算不完整：① 层汇总本身是 ⚪（层内一个可用指标都没有）；
    ② 层汇总是 🟢/🟡/🔴，但层内**某个指标**为 ⚪ 被 worst() 丢掉了
       （例：公司层债报价全过期→corp_spread ⚪，一级市场在窗且🟢 → L1 汇总 🟢，
        而 L1 的二级利差其实压根没取到）。
    只看层汇总会把 ② 读成「已确认未破」，那正是本框架反复点名最贵的错误。
    """
    out = []
    for k in ("L1", "L2", "L3", "L4"):
        d = ev.get(k, {})
        if d.get("state") == GREY or d.get("incomplete"):
            out.append(k)
    return out


def evaluate(fred, bonds, equities, cfg, history, today, max_quote_age=5):
    flags = cfg.get("manual_flags", {})
    ev = {"L1": {}, "L2": {}, "L3": {}, "L4": {}, "checks": [], "notes": []}

    # ---- L1 公司层 ----
    corp = [b for b in bonds if b["tier"] == "corp" and b["usable"]]
    l1_bench = next((b for b in corp if b["is_l1_benchmark"]), None) or (
        max(corp, key=lambda b: b["spread_bp"]) if corp else None)
    l1_states = []
    if l1_bench:
        sp = l1_bench["spread_bp"]
        st = RED if sp > TH["L1_corp_spread_red_bp"] else (AMBER if sp > TH["L1_corp_spread_amber_bp"] else GREEN)
        l1_states.append(st)
        ev["L1"]["corp_spread"] = {"state": st, "bond": l1_bench["label"], "spread_bp": sp,
                                   "th": f"{TH['L1_corp_spread_amber_bp']}/{TH['L1_corp_spread_red_bp']}bp"}
        prem = l1_bench.get("cohort_premium_bp")
        if prem is not None:
            pst = RED if prem > TH["L1_cohort_premium_red_bp"] else (
                AMBER if prem > TH["L1_cohort_premium_amber_bp"] else GREEN)
            l1_states.append(pst)
            ev["L1"]["cohort_premium"] = {"state": pst, "premium_bp": prem,
                                          "index": l1_bench.get("cohort_index"),
                                          "index_bp": l1_bench.get("cohort_index_bp"),
                                          "th": f"{TH['L1_cohort_premium_amber_bp']}/{TH['L1_cohort_premium_red_bp']}bp"}
    else:
        ev["L1"]["corp_spread"] = {"state": GREY, "reason": "无可用公司层债报价（缺报价或已过期）"}

    # 一级市场重定价
    sofr = fred.get("SOFR", {}).get("level_pct")
    deals = (cfg.get("primary_market") or {}).get("deals") or []
    live = [d for d in deals if d.get("status") in ("repricing", "priced")
            and d.get("initial_spread_bp") and d.get("final_spread_bp")]
    # 按日期取**最近一笔**，不是阵列第一笔：登记表是**追加写**的
    #（见 assets/neocloud_bonds.json 的 primary_market._readme：「每次 WebSearch 到
    # neocloud 新发债/杠杆贷款条款就追加一条」→ 新交易在阵列末尾）。
    # 旧版取 live[0] = 阵列第一笔 = 最旧那笔，会让按文档指示追加的新交易被静默忽略：
    # 一笔 5 天前、+200bp 的被迫让价被 append 后，脚本仍显示 35 天前那笔、判「超窗→仅历史参照」，
    # L1 反而落⚪，而一级市场是 L1 里信息含量最高的一项。
    # 日期非 ISO（如「2026-06-中」）者排最后、不因解析失败而崩，后续 age_days 仍会把它当历史参照。
    live.sort(key=_deal_date_key, reverse=True)
    if live:
        d = live[0]
        cost = all_in_loan_cost(d, sofr) if sofr is not None else {}
        move = (d["final_spread_bp"] - d["initial_spread_bp"])
        try:
            age_days = (today - date.fromisoformat(d["date"])).days
        except (ValueError, TypeError):
            age_days = None                            # 日期非 ISO（如「2026-06-中」）→ 视为历史参照
        in_window = age_days is not None and age_days <= TH["L1_primary_window_days"]
        st = (AMBER if move >= TH["L1_primary_reprice_amber_bp"] else GREEN) if in_window else GREY
        if st != GREY:
            l1_states.append(st)
        ev["L1"]["primary_reprice"] = {
            "state": st, "issuer": d["issuer"], "date": d["date"], "instrument": d["instrument"],
            "spread_move_bp": move, "all_in": cost, "size_usd_mm": d.get("size_usd_mm"),
            "age_days": age_days,
            # 日期非 ISO 时 age_days 为 None，旧版会印出「（None 天前）」；
            # 措辞与 neocloud_credit_lite.py 的同一判定对齐。
            # 超窗是「有意不计入」而非「没数据」，不应让 L1 被判为数据不完整。
            "excluded_by_design": not in_window,
            "th": (f"单笔重定价 ≥{TH['L1_primary_reprice_amber_bp']}bp → 🟡" if in_window else
                   (f"超出 {TH['L1_primary_window_days']} 天观察窗（{age_days} 天前）→ 仅历史参照，不计入判定"
                    if age_days is not None else
                    "日期非 ISO 格式、无法计龄 → 仅历史参照，不计入判定")),
            "deal": d,
        }
    if flags.get("primary_deal_pulled_or_postponed"):
        l1_states.append(RED)
        ev["L1"]["deal_pulled"] = {"state": RED, "detail": flags["primary_deal_pulled_or_postponed"]}
    ev["L1"]["state"] = worst(*l1_states) if l1_states else GREY
    # worst() 丢弃 GREY，层汇总因此看不出「层内某个指标没数据」。
    # incomplete 把这件事显式记下来，供裁决措辞使用（见 _incomplete_layers）。
    ev["L1"]["incomplete"] = _layer_incomplete(ev["L1"], l1_states)

    # ---- L2 项目层（IG 租户 SPV）= 真正的主题指标 ----
    spv = [b for b in bonds if b["tier"] == "spv_ig_tenant" and b["usable"]]
    l2_bench = next((b for b in spv if b["is_l2_benchmark"]), None) or (spv[0] if spv else None)
    l2_states = []
    if l2_bench:
        sp = l2_bench["spread_bp"]
        st = RED if sp > TH["L2_spv_spread_red_bp"] else (AMBER if sp > TH["L2_spv_spread_amber_bp"] else GREEN)
        l2_states.append(st)
        ev["L2"]["spv_spread"] = {"state": st, "bond": l2_bench["label"], "spread_bp": sp,
                                  "th": f"{TH['L2_spv_spread_amber_bp']}/{TH['L2_spv_spread_red_bp']}bp"}
        pst = AMBER if l2_bench["price"] < TH["L2_spv_price_amber"] else GREEN
        l2_states.append(pst)
        ev["L2"]["spv_price"] = {"state": pst, "price": l2_bench["price"],
                                 "th": f"< {TH['L2_spv_price_amber']} → 🟡"}
    else:
        ev["L2"]["spv_spread"] = {"state": GREY, "reason": "无可用项目层 SPV 债报价"}
    ev["L2"]["state"] = worst(*l2_states) if l2_states else GREY
    # worst() 丢弃 GREY，层汇总因此看不出「层内某个指标没数据」。
    # incomplete 把这件事显式记下来，供裁决措辞使用（见 _incomplete_layers）。
    ev["L2"]["incomplete"] = _layer_incomplete(ev["L2"], l2_states)

    # ---- L3 宏观确认 ----
    l3_states = []
    hy = fred.get("HY OAS")
    ccc = fred.get("CCC & Lower OAS")
    bb = fred.get("BB OAS")
    if hy:
        st = RED if hy["level_pct"] > TH["L3_hy_oas_red_pct"] else GREEN
        l3_states.append(st)
        ev["L3"]["hy_oas"] = {"state": st, "level_pct": hy["level_pct"], "d30_bp": hy["d30_bp"],
                              "d90_bp": hy["d90_bp"], "th": f">{TH['L3_hy_oas_red_pct']}% → 🔴"}
    if ccc and ccc["d90_bp"] is not None:
        st = AMBER if ccc["d90_bp"] > TH["L3_ccc_90d_amber_bp"] else GREEN
        l3_states.append(st)
        ev["L3"]["ccc_90d"] = {"state": st, "d90_bp": ccc["d90_bp"], "level_pct": ccc["level_pct"],
                               "th": f">+{TH['L3_ccc_90d_amber_bp']}bp → 🟡"}
    if ccc and bb:
        diff = round((ccc["level_pct"] - bb["level_pct"]) * 100, 0)
        st = AMBER if diff > TH["L3_ccc_minus_bb_amber_bp"] else GREEN
        l3_states.append(st)
        ev["L3"]["ccc_minus_bb"] = {"state": st, "diff_bp": diff,
                                    "th": f">{TH['L3_ccc_minus_bb_amber_bp']}bp → 🟡"}
    ev["L3"]["state"] = worst(*l3_states) if l3_states else GREY
    # worst() 丢弃 GREY，层汇总因此看不出「层内某个指标没数据」。
    # incomplete 把这件事显式记下来，供裁决措辞使用（见 _incomplete_layers）。
    ev["L3"]["incomplete"] = _layer_incomplete(ev["L3"], l3_states)

    # ---- L4 上游传染 = 最终裁决 ----
    l4_states = []
    nv = equities.get("NVDA")
    if nv:
        st = AMBER if nv["dd_from_hi52_pct"] < TH["L4_nvda_drawdown_amber_pct"] else GREEN
        l4_states.append(st)
        ev["L4"]["nvda_drawdown"] = {"state": st, "dd_pct": nv["dd_from_hi52_pct"],
                                     "th": f"< {TH['L4_nvda_drawdown_amber_pct']}% → 🟡"}
    if flags.get("hyperscaler_capex_guide_cut"):
        l4_states.append(RED)
        ev["L4"]["capex_cut"] = {"state": RED, "detail": flags["hyperscaler_capex_guide_cut"],
                                 "note": "与引爆点① 同源，两处须一致"}
    ev["L4"]["state"] = worst(*l4_states) if l4_states else GREY
    # worst() 丢弃 GREY，层汇总因此看不出「层内某个指标没数据」。
    # incomplete 把这件事显式记下来，供裁决措辞使用（见 _incomplete_layers）。
    ev["L4"]["incomplete"] = _layer_incomplete(ev["L4"], l4_states)

    # ---- 相对 HY 基准检验（日更文件「引爆点④ 的 Fed 传导校准」硬要求）----
    rel = {"verdict": None, "detail": None, "excess_bp": None}
    if l1_bench and hy:
        h = hist_spread_delta(history, l1_bench["key"], 30, today, max_quote_age)
        if h:
            ref_date, ref_sp = h
            neo_d30 = round(l1_bench["spread_bp"] - ref_sp, 0)
            hy_d30 = hy["d30_bp"]
            if hy_d30 is not None:
                excess = round(neo_d30 - hy_d30, 0)
                rel["excess_bp"] = excess
                rel["neo_d30_bp"] = neo_d30
                rel["hy_d30_bp"] = hy_d30
                rel["ref_date"] = ref_date.isoformat()
                if excess >= TH["excess_widening_idio_bp"]:
                    rel["verdict"] = "个体"
                    rel["detail"] = (f"{l1_bench['label']} 30日走阔 {neo_d30:+.0f}bp，"
                                     f"HY 基准 {hy_d30:+.0f}bp，超出 {excess:+.0f}bp → 个体信用溢价扩大")
                else:
                    rel["verdict"] = "宏观"
                    rel["detail"] = (f"{l1_bench['label']} 30日走阔 {neo_d30:+.0f}bp，与 HY 基准 "
                                     f"{hy_d30:+.0f}bp 同步（超出仅 {excess:+.0f}bp）→ 属宏观风险偏好，"
                                     f"归回调驱动源处理、不单独计④为🔴")
        else:
            # 历史档没有「30 日前 + 当日报价仍新鲜」的参照点（缺纪录，或该纪录的利差
            # 是由过期报价算出的、只反映曲线漂移）→ 退回「同评级指数溢价」的绝对水平法
            prem = l1_bench.get("cohort_premium_bp")
            if prem is not None:
                rel["verdict"] = "个体(水平法)" if prem > TH["L1_cohort_premium_amber_bp"] else "宏观(水平法)"
                rel["cohort_premium_bp"] = prem
                rel["detail"] = (f"历史档无 30 日前的新鲜参照点（缺纪录或该日报价已过期），改用水平法："
                                 f"{l1_bench['label']} 利差 "
                                 f"{l1_bench['spread_bp']:.0f}bp vs {l1_bench.get('cohort_index')} "
                                 f"{l1_bench.get('cohort_index_bp'):.0f}bp，个体溢价 {prem:+.0f}bp"
                                 f"（阈值 {TH['L1_cohort_premium_amber_bp']}bp）。"
                                 f"变化率检验须待历史档累积 ≥30 日的**新鲜报价**纪录")
    if rel["verdict"] is None:
        rel["verdict"] = "无法区分"
        rel["detail"] = "缺 HY 基准或缺 neocloud 利差 → 按日更规则，引爆点④ 最高只记🟡，不得升🔴"
    ev["relative_check"] = rel

    # ---- 股债背离 ----
    neo_eq = [equities[t] for t in NEOCLOUD_EQUITY if t in equities and t != "DTCR"]
    if neo_eq:
        avg21 = round(float(np.mean([e["d21_pct"] for e in neo_eq if e["d21_pct"] is not None])), 1)
        avg_dd = round(float(np.mean([e["dd_from_hi52_pct"] for e in neo_eq])), 1)
        hyg = equities.get("HYG", {}).get("d21_pct")
        ev["divergence"] = {
            "neocloud_equity_avg_21d_pct": avg21,
            "neocloud_equity_avg_dd52_pct": avg_dd,
            "hyg_21d_pct": hyg,
            "gap_pt": round(avg21 - hyg, 1) if hyg is not None else None,
            "corp_spread_bp": l1_bench["spread_bp"] if l1_bench else None,
        }

    # ---- 引爆点④ 汇总（写回日报 🧭 表第④行）----
    # 论点侧真正的裁决在 L2/L4；L1/L3 衡量的是融资成本与全市场风险偏好
    thesis = worst(ev["L2"]["state"], ev["L4"]["state"])
    financing = worst(ev["L1"]["state"], ev["L3"]["state"])
    if thesis == RED:
        t4 = RED
        why = "项目层/上游已破 → 论点侧受损"
    elif rel["verdict"].startswith("宏观") and financing == RED:
        t4 = AMBER
        why = "公司层达🔴水平但与 HY 基准同步 → 判宏观驱动，按校准规则降为🟡、归回调驱动源处理"
    elif rel["verdict"] == "无法区分" and financing in (RED, AMBER):
        t4 = AMBER
        why = "缺基准、无法区分宏观 vs 个体 → 按日更规则封顶🟡"
    elif financing == RED and rel["verdict"].startswith("个体"):
        t4 = RED
        why = "公司层利差达困境水平且相对 HY 基准显著走阔 → 个体融资链问题"
    else:
        t4 = worst(financing, thesis)
        _inc = _incomplete_layers(ev)
        if t4 in (AMBER, GREEN) and _inc:
            # 任一层数据不完整时，绝不能写「仍完好」「四层皆未触发」。
            _tail = "/".join(_inc) + " 本次⚪未取到数据，未能确认"
            why = ("融资成本上升/尾部走阔，" + _tail) if t4 == AMBER else ("已取到的层级均未触发，但 " + _tail)
        else:
            why = {
                RED: "见 L1–L4 明细", AMBER: "融资成本上升/尾部走阔，项目层与上游仍完好",
                GREEN: "信用层四层皆未触发", GREY: "本次未取到可用信用数据",
            }.get(t4, "")
    ev["tripwire_4"] = {"state": t4, "why": why,
                        "thesis_side": thesis, "financing_side": financing}

    # 判读：可买的回撤 vs 主题崩坏（须点名实际动了哪几层，不用固定模板）
    moved = [lay for lay in ("L1", "L3") if ev[lay]["state"] in (AMBER, RED)]
    intact = [f"{lay}{ev[lay]['state']}" for lay in ("L2", "L4")]
    if thesis == RED:
        ev["verdict_line"] = "🔴 主题崩坏风险：项目层或上游已确认受损，回调不是机会"
        ev["verdict_tag"] = "🔴主题崩坏"
    elif t4 == RED:
        ev["verdict_line"] = "🔴 个体融资链告警：走论点闸门，全体买入桶降级观察"
        ev["verdict_tag"] = "🔴融资链告警"
    elif financing in (AMBER, RED):
        # ⚪ 不得读成好消息：论点侧缺数据时只能说「未能确认」，不能说「未破」
        both_green = ev["L2"]["state"] == GREEN and ev["L4"]["state"] == GREEN
        side = ("未破 → 主题仍在" if both_green else
                "**数据不足、未能确认**（缺项不计入升档，但也不得当作未破）→ 分桶维持、节奏转保守")
        ev["verdict_line"] = (f"🟡 可买的回撤（限定在融资成本这条腿）：{'/'.join(moved)} 已动，"
                              f"衡量的是「neocloud 股东被稀释多少」；"
                              f"{'、'.join(intact)}（项目层与上游）{side}")
        # 与 neocloud_credit_lite.py 的 tag 同格式：缺数据时点名是哪一层⚪，
        # 精简版一行是唯一进手机的那行，不能只说「数据不足」而不说缺在哪。
        ev["verdict_tag"] = (
            f"🟡可买的回撤（{'/'.join(moved)}动，L2/L4未破）" if both_green
            else f"🟡{'/'.join(moved)}动·L2{ev['L2']['state']}/L4{ev['L4']['state']}数据不足未能确认")
    elif t4 == GREY:
        ev["verdict_line"] = "⚪ 信用层数据不足，沿用上次状态，不改变分桶"
        ev["verdict_tag"] = "⚪数据不足"
    else:
        # worst() 丢弃 GREY，所以 t4=🟢 并不代表四层都有数据。
        # 任一层为 ⚪ 时只能说「未见触发」，绝不能说「四层皆未触发·支撑完好」——
        # 那是把数据缺失讲成好消息，与上面 🟡 分支要防的是同一个错误。
        _grey = _incomplete_layers(ev)
        if _grey:
            ev["verdict_line"] = ("🟢 已取到的层级均未触发，但 " + "/".join(_grey)
                                  + " 本次⚪未取到数据 → 非已确认「四层皆未破」")
            ev["verdict_tag"] = "🟢未见触发·" + "/".join(_grey) + "⚪缺数据"
        else:
            ev["verdict_line"] = "🟢 信用层四层皆未触发，论点侧支撑完好"
            ev["verdict_tag"] = "🟢论点侧完好"
    return ev


# ---------------------------------------------------------------- 输出

def fmt(v, unit="", nd=2, sign=False):
    if v is None:
        return "N/A"
    if isinstance(v, str):
        return v
    s = f"{v:+.{nd}f}" if sign else f"{v:.{nd}f}"
    return s + unit


def render_markdown(res):
    fred, bonds, eq, ev, meta = res["fred"], res["bonds"], res["equities"], res["eval"], res["meta"]
    L = []
    A = L.append
    A(f"### 💳 Neocloud 信用层监控（NEOCLOUD_CREDIT tripwire v2）")
    A("")
    A(f"生成时间 {meta['generated_at']}｜脚本 `{SCRIPT_PATH.name}`｜"
      f"报价档 `{meta.get('bonds_path', 'assets/neocloud_bonds.json')}`｜"
      f"历史档 `{meta.get('history_path', 'assets/neocloud_credit_history.jsonl')}` "
      f"{meta['history_records']} 笔")
    A("")
    A(f"**汇总裁决：{ev['verdict_line']}**")
    A("")
    A(f"**引爆点④ 状态 = {ev['tripwire_4']['state']}** — {ev['tripwire_4']['why']}"
      f"（论点侧 {ev['tripwire_4']['thesis_side']}｜融资侧 {ev['tripwire_4']['financing_side']}）")
    A("")

    # 无风险曲线
    A("**① 无风险曲线（利差分母）**")
    A("")
    A("| 年期 | 收益率 | 1d(bp) | 5d(bp) | 30d(bp) | 90d(bp) | 数据日 |")
    A("|---|---|---|---|---|---|---|")
    for name in FRED_RATES:
        if name in fred:
            d = fred[name]
            A(f"| {name} | {fmt(d['level_pct'],'%',2)} | {fmt(d['d1_bp'],'',1,True)} | "
              f"{fmt(d['d5_bp'],'',1,True)} | {fmt(d['d30_bp'],'',1,True)} | "
              f"{fmt(d['d90_bp'],'',1,True)} | {d['as_of']} |")
    if "SOFR" in fred:
        A(f"| SOFR | {fmt(fred['SOFR']['level_pct'],'%',2)} | {fmt(fred['SOFR']['d1_bp'],'',1,True)} | "
          f"{fmt(fred['SOFR']['d5_bp'],'',1,True)} | — | — | {fred['SOFR']['as_of']} |")
    A("")

    # 信用指数阶梯
    A("**② 信用指数阶梯（L3 宏观确认层）**")
    A("")
    A("| 指数 | OAS | 1d(bp) | 5d(bp) | 30d(bp) | 90d(bp) | 数据日 | 作用 |")
    A("|---|---|---|---|---|---|---|---|")
    for name in FRED_CREDIT:
        if name in fred:
            d = fred[name]
            A(f"| {name} | {fmt(d['level_pct'],'%',2)} | {fmt(d['d1_bp'],'',1,True)} | "
              f"{fmt(d['d5_bp'],'',1,True)} | {fmt(d['d30_bp'],'',1,True)} | "
              f"{fmt(d['d90_bp'],'',1,True)} | {d['as_of']} | {d['desc']} |")
    A("")
    if "CCC & Lower OAS" in fred and "BB OAS" in fred:
        diff = ev["L3"].get("ccc_minus_bb", {})
        A(f"- **HY 内部分化**：CCC − BB = {fmt(diff.get('diff_bp'),'bp',0)} "
          f"→ {diff.get('state','—')}（阈值 {diff.get('th','—')}）。"
          f"指数层面被掩盖的尾部压力，neocloud 公司层债正活在这个尾部。")
    A("")

    # 债券
    A("**③ Neocloud 债券利差阶梯（L1 公司层 / L2 项目层）**")
    A("")
    A("| 债券 | 层 | 价格 | 报价日 | YTM | 基准 | 利差 | 同评级溢价 | 状态 |")
    A("|---|---|---|---|---|---|---|---|---|")
    for b in bonds:
        tier = {"corp": "L1公司层", "spv_ig_tenant": "L2项目层", "convert": "转债·仅参考"}[b["tier"]]
        mark = GREY if b["stale"] or b["ytm_pct"] is None else ("—" if b["tier"] == "convert" else "✓")
        stale_note = f"（{b['quote_age_days']}天前·过期）" if b["stale"] and b["quote_as_of"] else ""
        A(f"| {b['label']} | {tier} | {fmt(b['price'],'',2)}{stale_note} | {b['quote_as_of'] or 'N/A'} | "
          f"{fmt(b['ytm_pct'],'%',2)} | {fmt(b['bench_pct'],'%',2) if b['bench_pct'] else (b['bench_label'] or 'N/A')} | "
          f"{('+'+str(int(b['spread_bp']))+'bp') if b['spread_bp'] is not None else 'N/A'} | "
          f"{fmt(b['cohort_premium_bp'],'bp',0,True)} | {mark} |")
    A("")
    A("- YTM 由脚本二分法反解（半年付息，折现全价 = 干净价 + 应计利息）；par 债不变量残差 <1bp。"
      "到期日仅知月份时按当月 15 日，另有约 ±3bp 口径误差。利差 = YTM − DGS 曲线插值。")
    A("- 转债利差含期权价值，**不可直读为纯信用利差**，一律不参与判定。")
    A("- EUR 债不对美债曲线，只对 EUR HY 指数做相对比较，绝对利差标 N/A。")
    A("")

    # 一级市场
    pr = ev["L1"].get("primary_reprice")
    if pr:
        A("**④ 一级市场重定价（L1 中信息含量最高的一项）**")
        A("")
        ai = pr.get("all_in") or {}
        d0 = pr.get("deal") or {}
        A(f"{pr['issuer']} {pr['instrument']}｜{pr['date']}｜规模 ${pr.get('size_usd_mm')}M")
        A("")
        A("| 项目 | 初始指导 | 最终讨论 | 变化 |")
        A("|---|---|---|---|")
        A(f"| 利差（{d0.get('base_rate','基准')} 之上） | {fmt(d0.get('initial_spread_bp'),'bp',0)} | "
          f"{fmt(d0.get('final_spread_bp'),'bp',0)} | {fmt(pr['spread_move_bp'],'bp',0,True)} |")
        A(f"| 发行折价 OID | {fmt(d0.get('initial_oid'),'',2)} | {fmt(d0.get('final_oid'),'',2)} | "
          f"{fmt((d0.get('final_oid')-d0.get('initial_oid')) if (d0.get('final_oid') and d0.get('initial_oid')) else None,'',2,True)} |")
        A(f"| **全包成本**（含 OID 按3年摊销） | **{fmt(ai.get('initial'),'%',2)}** | "
          f"**{fmt(ai.get('final'),'%',2)}** | **{fmt(ai.get('delta_bp'),'bp',0,True)}** |")
        A("")
        A(f"- 判定：{pr['state']}（{pr['th']}）。发行人被迫让价 ≠ 二级指数缓慢漂移，"
          f"这类信号信息含量远高于指数 OAS。")
        A("")

    # 股债背离
    dv = ev.get("divergence")
    if dv:
        A("**⑤ 股债背离（谁在领先）**")
        A("")
        A("| 标的 | 收盘 | 1d | 5d | 1M | 3M | 距52w高 | vol30 |")
        A("|---|---|---|---|---|---|---|---|")
        for t, nm in list(NEOCLOUD_EQUITY.items()) + list(BENCHMARK_EQUITY.items()) + [("HYG", ""), ("BKLN", "")]:
            if t in eq:
                d = eq[t]
                A(f"| {t} | {fmt(d['close'],'',2)} | {fmt(d['d1_pct'],'%',1,True)} | "
                  f"{fmt(d['d5_pct'],'%',1,True)} | {fmt(d['d21_pct'],'%',1,True)} | "
                  f"{fmt(d['d63_pct'],'%',1,True)} | {fmt(d['dd_from_hi52_pct'],'%',1,True)} | "
                  f"{fmt(d['vol30_ann_pct'],'',0)} |")
        A("")
        A(f"- neocloud 股票 1M 均值 {fmt(dv['neocloud_equity_avg_21d_pct'],'%',1,True)}、"
          f"距 52w 高均值 {fmt(dv['neocloud_equity_avg_dd52_pct'],'%',1,True)}；"
          f"HYG 1M {fmt(dv['hyg_21d_pct'],'%',1,True)} → 缺口 {fmt(dv['gap_pt'],'pt',1,True)}。")
        A(f"- 用 HYG/JNK 监控本主题会失灵：股票端与宽基 HY 完全脱钩，"
          f"neocloud 公司层债利差 {fmt(dv['corp_spread_bp'],'bp',0)} 才是对应读数。")
        A("")

    # 相对基准检验
    rc = ev["relative_check"]
    A("**⑥ 相对 HY 基准检验（日更文件「引爆点④ 的 Fed 传导校准」硬要求）**")
    A("")
    A(f"- 结论：**{rc['verdict']}**")
    A(f"- 依据：{rc['detail']}")
    A("")

    # L1-L4 判定
    A("**⑦ 四层判定明细**")
    A("")
    A("| 层 | 项目 | 读数 | 阈值 | 状态 |")
    A("|---|---|---|---|---|")
    layer_names = {"L1": "L1 公司层", "L2": "L2 项目层★", "L3": "L3 宏观确认", "L4": "L4 上游传染★"}
    for lay in ("L1", "L2", "L3", "L4"):
        for k, v in ev[lay].items():
            if k == "state" or not isinstance(v, dict):
                continue
            read = ", ".join(f"{kk}={vv}" for kk, vv in v.items()
                             if kk not in ("state", "th", "note", "deal", "reason", "all_in")
                             and vv is not None)
            A(f"| {layer_names[lay]} | {k} | {read or v.get('reason','—')} | {v.get('th','—')} | {v['state']} |")
        A(f"| **{layer_names[lay]}** | **本层汇总** | | | **{ev[lay]['state']}** |")
    A("")
    A("- ★ L2/L4 是真正该盯的：L2 = 投资级租户项目债（AI 算力现金流本身是否被质疑），"
      "L4 = 上游算力核心（融资链问题是否已回传到需求）。L1/L3 衡量的是股东稀释与全市场风险偏好。")
    A("")

    # 数据缺口
    A("**⑧ 数据缺口（必须声明，缺项不得推高结论）**")
    A("")
    for e in res["errors"]:
        A(f"- {scrub(e)}")          # 例外讯息可能带绝对路径 → 一律折叠家目录
    cds = (res["cfg"].get("manual_flags") or {})
    if not cds.get("cds_5y_bp"):
        A(f"- **无实时 CDS 数据**（本层最大盲区）：{cds.get('cds_source','')}")
    stale = [b["label"] for b in bonds if b["stale"] and b["quote_as_of"]]
    if stale:
        A(f"- **报价过期**（超过 --max-quote-age，已排除于判定）：{'、'.join(stale)}")
    if not res["errors"] and not stale:
        A("- 指数/曲线/股价三层皆取到当期数据，无缺口。")
    A("")

    # 可直接贴回日报的 ④ 行
    A("**⑨ 贴回日报「🧭 引爆点监控」表第④行**")
    A("")
    l1c = ev["L1"].get("corp_spread", {})
    l2c = ev["L2"].get("spv_spread", {})

    def leg(tag, d):
        """缺数据时只印一个 N/A。旧版 bond 与 spread 各自退化成 'N/A'，
        会输出重复的「L1 N/A N/A」。"""
        if d.get("spread_bp") is None:
            return f"{tag} N/A"
        name = d.get("bond")
        return f"{tag} {name + ' ' if name else ''}{fmt(d['spread_bp'], 'bp', 0)}"

    # 日期口径：OAS 读数后面必须标 **FRED 信用序列自己的 as_of**，不能用 meta['data_date']。
    # meta['data_date'] = max(FRED as_of, 股价 as_of)，而 FRED 的 OAS 永远比股价晚一个交易日，
    # max() 必然取到**股价**日期 → 会把 08-31 的 OAS/CCC 读数标成 09-01。
    # neocloud_credit_lite.py 的同一行用 hy['as_of']，两份产出同一张「🧭 引爆点监控」表第④行，
    # 日期必须一致。股价日期另行标示，不与信用数据混成一个。
    hy_d = fred.get("HY OAS", {})
    ccc_d = fred.get("CCC & Lower OAS", {})
    credit_asof = hy_d.get("as_of") or ccc_d.get("as_of") or "N/A"
    px_asof = max([d["as_of"] for d in eq.values() if d.get("as_of")], default=None)
    date_note = f"信用数据 {credit_asof}" + (f" / 股价 {px_asof}" if px_asof else "")
    A(f"| ④ | Neocloud 信用利差扩大至困境水平 | {ev['tripwire_4']['state']} | "
      f"{leg('L1', l1c)}｜"
      f"{leg('L2', l2c)}｜"
      f"HY {fmt(hy_d.get('level_pct'),'%',2)}｜"
      f"CCC 90d {fmt(ccc_d.get('d90_bp'),'bp',0,True)}"
      f"（{date_note}） | FRED BAMLH0A0HYM2/BAMLH0A3HYC + 本地脚本 | "
      f"{ev['verdict_line']} |")
    A("")
    return "\n".join(L)


def render_compact(res):
    ev, fred = res["eval"], res["fred"]
    l1 = ev["L1"].get("corp_spread", {})
    l2 = ev["L2"].get("spv_spread", {})
    hy = fred.get("HY OAS", {}).get("level_pct")
    ccc = fred.get("CCC & Lower OAS", {})
    return (f"💳 信用④{ev['tripwire_4']['state']}｜"
            f"L1 {fmt(l1.get('spread_bp'),'bp',0)}｜L2 {fmt(l2.get('spread_bp'),'bp',0)}｜"
            f"HY {fmt(hy,'%',2)}｜CCC90d {fmt(ccc.get('d90_bp'),'bp',0,True)}｜"
            f"{ev['relative_check']['verdict']}｜{ev.get('verdict_tag','')}")


# ---------------------------------------------------------------- 主流程

def main():
    ap = argparse.ArgumentParser(description="Neocloud 信用层日更监控 (引爆点④ tripwire v2)")
    ap.add_argument("--json", action="store_true", help="输出 JSON 而非 markdown")
    ap.add_argument("--compact", action="store_true", help="只输出精简版一行（Slack 用）")
    ap.add_argument("--bonds", default=BONDS_PATH, help="债券登记表路径")
    ap.add_argument("--max-quote-age", type=int, default=5, help="报价过期天数（默认5，超过判⚪不参与触发）")
    ap.add_argument("--no-history", action="store_true", help="不写入历史档")
    args = ap.parse_args()

    today = date.today()
    bonds_path = Path(args.bonds)
    # 读不到报价档就没有②③⑦可言 —— 报中文错误并退出，绝不抛裸 traceback
    #（traceback 会把 /Users/<用户名>/... 打进日报正文并推 Slack）
    try:
        with open(bonds_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except FileNotFoundError:
        expected = "" if bonds_path.resolve() == BONDS_PATH else f"（预设位置：{rel_display(BONDS_PATH)}）"
        err(f"✗ 找不到债券登记表 {tilde_path(bonds_path)}{expected}。"
            f"请确认技能安装完整（该档应在技能目录的 assets/ 下），或用 --bonds 指定路径。")
        return 1
    except (OSError, PermissionError) as e:
        err(f"✗ 债券登记表 {tilde_path(bonds_path)} 读取失败：{type(e).__name__}。")
        return 1
    except json.JSONDecodeError as e:
        err(f"✗ 债券登记表 {tilde_path(bonds_path)} 不是合法 JSON（第 {e.lineno} 行）：{e.msg}。")
        return 1
    if not isinstance(cfg, dict) or not cfg.get("bonds"):
        err(f"✗ 债券登记表 {tilde_path(bonds_path)} 缺 bonds 阵列，无法定价。")
        return 1

    errors = []
    fred, e1 = pull_fred_block()
    errors += e1
    tickers = list(CREDIT_ETFS) + list(NEOCLOUD_EQUITY) + list(BENCHMARK_EQUITY)
    equities, e2 = pull_equities(tickers)
    errors += e2
    bonds, e3 = price_bonds(cfg, fred, today, args.max_quote_age)
    errors += e3
    # 例外讯息可能夹带绝对路径；--json 与 markdown 两条输出路径都吃这份 errors，
    # 所以在这里一次折叠干净，而不是只在 render 时补。
    errors = [scrub(e) for e in errors]
    history = load_history()
    ev = evaluate(fred, bonds, equities, cfg, history, today, args.max_quote_age)

    data_date = max([d["as_of"] for d in fred.values() if "as_of" in d] +
                    [d["as_of"] for d in equities.values()], default=today.isoformat())
    res = {
        "meta": {
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S %Z").strip(),
            "data_date": data_date,
            "history_records": len(history),
            # 路径一律相对化：本 meta 会进 --json 输出与报告抬头
            "bonds_path": rel_display(bonds_path),
            "history_path": rel_display(HISTORY_PATH),
            "thresholds": TH,
        },
        "fred": {k: {kk: vv for kk, vv in v.items() if kk != "_series"} for k, v in fred.items()},
        "equities": equities,
        "bonds": bonds,
        "eval": ev,
        "errors": errors,
        "cfg": cfg,
    }

    # 跨档变化侦测（日更文件强调「状态是否跨档变化」）—— 与「上一个不同日期」的纪录比。
    # 这段只**读**历史档，与本次是否落盘无关：--no-history 的语义是「不写入」，
    # 旧版把它一并关掉，试跑时第⑩块整段消失，与 references 要求的「十个区块整段贴入」冲突。
    prev_recs = [r for r in history if r.get("date") != today.isoformat()]
    if prev_recs:
        prev = prev_recs[-1]
        changes = [f"{lay}: {prev.get('states',{}).get(lay)} → {ev[lay]['state']}"
                   for lay in ("L1", "L2", "L3", "L4")
                   if prev.get("states", {}).get(lay) not in (None, ev[lay]["state"])]
        if prev.get("tripwire_4") not in (None, ev["tripwire_4"]["state"]):
            changes.append(f"引爆点④: {prev['tripwire_4']} → {ev['tripwire_4']['state']}")
        res["meta"]["cross_tier_changes"] = changes
        res["meta"]["prev_run_date"] = prev.get("date")

    if not args.no_history:
        append_history({
            "date": today.isoformat(),
            "data_date": data_date,
            "indices": {k: v.get("level_pct") for k, v in res["fred"].items()},
            # 过期报价算出的 spread_bp 只反映当日 DGS 曲线漂移，不含任何信用信息：
            # 落盘时置 None 并标 stale，免得日后被 hist_spread_delta() 当成 30 日基准。
            "bonds": {b["key"]: {"price": b["price"], "ytm_pct": b["ytm_pct"],
                                 "spread_bp": (None if b["stale"] else b["spread_bp"]),
                                 "quote_as_of": b["quote_as_of"],
                                 "stale": bool(b["stale"])}
                      for b in bonds},
            "states": {lay: ev[lay]["state"] for lay in ("L1", "L2", "L3", "L4")},
            "tripwire_4": ev["tripwire_4"]["state"],
        })

    if args.json:
        res.pop("cfg", None)
        print(json.dumps(res, ensure_ascii=False, indent=2, default=str))
    elif args.compact:
        print(render_compact(res))
    else:
        print(render_markdown(res))
        ch = res["meta"].get("cross_tier_changes")
        if ch:
            print("**⑩ 跨档变化（vs 上次运行 " + str(res["meta"].get("prev_run_date")) + "）**\n")
            for c in ch:
                print(f"- {c}")
        elif "cross_tier_changes" in res["meta"]:
            print(f"**⑩ 跨档变化**：无（vs {res['meta'].get('prev_run_date')}）")
        else:
            # 十个区块须整段贴入日报，缺历史档也要出现这一块并说明原因
            print("**⑩ 跨档变化**：无历史档可比（首次运行，或历史档只有当日纪录）")
    return 0


if __name__ == "__main__":
    # 最后一道闸：任何未预期的例外都不得吐出裸 traceback。
    # traceback 会印出脚本与套件的绝对路径（/Users/<用户名>/...），而本脚本的输出
    # 会被贴进日更报告正文并推 Slack。
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        err("✗ 已中断。")
        sys.exit(130)
    except Exception as exc:                      # noqa: BLE001 —— 刻意兜底
        err(f"✗ 信用层脚本执行失败：{type(exc).__name__}: {scrub(exc)}")
        sys.exit(1)
