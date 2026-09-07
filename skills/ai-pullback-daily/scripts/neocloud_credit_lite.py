#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""neocloud_credit_lite.py —— 引爆点④ 信用层判定（纯标准库，云端 routine 内嵌用）

与本机完整版 neocloud_credit_monitor.py 同一套阈值与映射规则，差别：
  · 只用标准库（urllib/csv/math），不需要 yfinance / pandas
  · 无历史档（云端 persist_session=false）→ 相对基准检验只用「水平法」
  · L4 的 NVDA 距52周高由 --nvda-dd 传入（用本任务第二步已取到的数字，不重复取数）
  · 债券报价由 --quote KEY=PRICE@YYYY-MM-DD 传入（WebSearch 取得），未传则用内建基线并按报价日判过期

用法：
  python3 neocloud_credit_lite.py --nvda-dd -18.0 \
      --quote CRWV-9.75-2031=99.72@2026-07-30 \
      --quote BLACKPEARL-6.125-2031=101.28@2026-07-30
  可选：--capex-cut "证据一句话"   --deal-pulled "证据一句话"   --max-quote-age 5
        --json  输出机读 JSON（四层状态、引爆点④ 裁决、各判定的输入、数据缺口）而非 markdown
"""
import argparse, csv, io, json, math, re, sys, urllib.request, ssl
from datetime import date, datetime, timedelta
from pathlib import Path

# 本脚本不读写任何资产档（报价由 --quote 传入、无历史档），因此没有资产路径锚点；
# 但输出会被贴进日更报告正文并推 Slack，任何对外文字仍必须过 scrub()，
# 且不得让例外冒泡成裸 traceback（traceback 会印出 /Users/<用户名>/... 的脚本路径）。
SCRIPT_PATH = Path(__file__).resolve()
SKILL_ROOT = SCRIPT_PATH.parent.parent                   # <...>/ai-pullback-daily
SKILLS_DIR = SKILL_ROOT.parent

_HOMEISH_RE = re.compile(r"(?:/Users|/home|/var/folders)/[^/\s\"']+")


def scrub(text) -> str:
    """把文本里的家目录绝对路径折叠掉，绝不把本机用户名带进 Slack。"""
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


def err(msg: str = "") -> None:
    print(scrub(msg), file=sys.stderr)


def num(d, k, fmt="{:.2f}", suffix="", na="N/A"):
    """安全取数并格式化：取数失败时 d 为 None、或该栏位为 None，一律回 'N/A'。

    云端 routine 内嵌用，网路抖动是常态：这里少一个守卫，整份报告（连同已经拼好的
    「数据缺口」段）就会在最后一行 f-string 里一起消失。
    """
    v = d.get(k) if isinstance(d, dict) else None
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        return na
    return fmt.format(v) + suffix


GREEN, AMBER, RED, GREY = "🟢", "🟡", "🔴", "⚪"
RANK = {GREY: -1, GREEN: 0, AMBER: 1, RED: 2}

TH = {  # 与完整版 TH 字典一致
    "L1_corp_amber": 600, "L1_corp_red": 800,
    "L1_prem_amber": 250, "L1_prem_red": 500,
    "L1_primary_amber_bp": 100, "L1_primary_window_days": 30,
    "L2_amber": 250, "L2_red": 400, "L2_price_amber": 95.0,
    "L3_ccc90_amber": 150, "L3_hy_red_pct": 4.00, "L3_ccc_bb_amber": 800,
    "L4_nvda_dd_amber": -25.0,
}

FRED = {  # 显示名: (series_id, 是否为信用指数)
    "HY OAS": ("BAMLH0A0HYM2", 1), "BB OAS": ("BAMLH0A1HYBB", 1),
    "Single-B OAS": ("BAMLH0A2HYB", 1), "CCC & Lower OAS": ("BAMLH0A3HYC", 1),
    "IG Corp OAS": ("BAMLC0A0CM", 1), "EUR HY OAS": ("BAMLHE00EHYIOAS", 1),
    "UST 2Y": ("DGS2", 0), "UST 3Y": ("DGS3", 0), "UST 5Y": ("DGS5", 0),
    "UST 7Y": ("DGS7", 0), "UST 10Y": ("DGS10", 0), "SOFR": ("SOFR", 0),
}
TENOR = {"UST 2Y": 2.0, "UST 3Y": 3.0, "UST 5Y": 5.0, "UST 7Y": 7.0, "UST 10Y": 10.0}

BONDS = [  # key, 标签, 层, 币别, 票息, 到期, 同评级指数, 基准债?
    ("CRWV-9.75-2031", "CRWV 9.75% Oct-31", "corp", "USD", 9.75, "2031-10-15", "Single-B OAS", 1),
    ("CRWV-9.625-2032", "CRWV 9.625% Jul-32", "corp", "USD", 9.625, "2032-07-15", "Single-B OAS", 0),
    ("CRWV-9.00-2031", "CRWV 9.00% Feb-31", "corp", "USD", 9.0, "2031-02-15", "Single-B OAS", 0),
    ("BLACKPEARL-6.125-2031", "Black Pearl 6.125% Feb-31 (AWS租约SPV)", "spv", "USD", 6.125, "2031-02-15", "BB OAS", 1),
]
BASELINE_QUOTES = {  # 建立基线时的报价（2026-07-30）；每次运行应以 --quote 覆盖为最新
    "CRWV-9.75-2031": (99.72, "2026-07-30"),
    "CRWV-9.625-2032": (98.65, "2026-07-30"),
    "CRWV-9.00-2031": (98.94, "2026-07-30"),
    "BLACKPEARL-6.125-2031": (101.28, "2026-07-30"),
}
PRIMARY = {  # 一级市场最近一笔；有新交易时改这里
    "date": "2026-07-29", "issuer": "CoreWeave", "instrument": "Term Loan B",
    "size_mm": 2600, "base": "SOFR", "sp0": 425, "sp1": 550, "oid0": 99.0, "oid1": 96.5,
}


def worst(*st):
    real = [s for s in st if s in RANK and s != GREY]
    return max(real, key=lambda s: RANK[s]) if real else GREY


def fred(sid, timeout=15):
    """FRED CSV。注意：**不要加自订 User-Agent**，会挂住到超时。"""
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}&cosd={(date.today()-timedelta(days=400)).isoformat()}"
    try:
        ctx = ssl.create_default_context()
        try:
            import certifi
            ctx = ssl.create_default_context(cafile=certifi.where())
        except Exception:
            pass
        txt = urllib.request.urlopen(url, timeout=timeout, context=ctx).read().decode()
    except Exception:
        import subprocess
        p = subprocess.run(["curl", "-sS", "--max-time", str(timeout), url], capture_output=True, text=True)
        if p.returncode != 0 or not p.stdout.strip():
            raise RuntimeError("FRED 取数失败（urllib 与 curl 皆失败）")
        txt = p.stdout
    rows = [r for r in csv.reader(io.StringIO(txt))][1:]
    obs = [(r[0], float(r[1])) for r in rows if len(r) > 1 and r[1] not in (".", "")]
    if not obs:
        raise RuntimeError("FRED 无有效观测")
    return obs


def chg_bp(obs, days):
    last_d = date.fromisoformat(obs[-1][0])
    tgt = last_d - timedelta(days=days)
    prior = [o for o in obs if date.fromisoformat(o[0]) <= tgt]
    return round((obs[-1][1] - prior[-1][1]) * 100, 1) if prior else None


def ctimes(years, freq=2):
    n = max(1, math.ceil(years * freq))
    return [years - k / freq for k in range(n - 1, -1, -1)]


def accrued(coupon, years, freq=2):
    per = 1.0 / freq
    t1 = ctimes(years, freq)[0]
    return (coupon / freq) * max(0.0, min(1.0, (per - t1) / per))


def pv(y, coupon, years, freq=2):
    d = 1 + y / freq
    s = sum((coupon / freq) / d ** (t * freq) for t in ctimes(years, freq) if t > 0)
    return s + 100.0 / d ** (years * freq)


def ytm(clean, coupon, years, freq=2):
    """由干净价解 YTM：折现全价 == 干净价 + 应计利息。省掉应计会把 YTM 高估达 70bp。"""
    if clean is None or clean <= 0 or years <= 0:
        return None
    dirty = clean + accrued(coupon, years, freq)
    lo, hi = 1e-6, 3.0
    if pv(hi, coupon, years, freq) > dirty:
        return None
    for _ in range(200):
        m = (lo + hi) / 2
        if pv(m, coupon, years, freq) > dirty:
            lo = m
        else:
            hi = m
    return (lo + hi) / 2


def interp(curve, years):
    xs = sorted(curve)
    if not xs:
        return None
    if years <= xs[0]:
        return curve[xs[0]]
    if years >= xs[-1]:
        return curve[xs[-1]]
    for a, b in zip(xs, xs[1:]):
        if a <= years <= b:
            w = (years - a) / (b - a)
            return curve[a] + w * (curve[b] - curve[a])


def _parse_args():
    """argparse 提到 _render 外面。

    --json 与否决定外壳 finally 印哪一种输出，而外壳必须在 _render 半途中断时
    也知道该印 markdown 还是 JSON——参数解析留在 _render 里就办不到。
    """
    ap = argparse.ArgumentParser()
    ap.add_argument("--nvda-dd", type=float, default=None, help="NVDA 距52周高 %%（用第二步已取到的数字）")
    ap.add_argument("--quote", action="append", default=[], help="KEY=PRICE@YYYY-MM-DD，可重复")
    ap.add_argument("--max-quote-age", type=int, default=5)
    ap.add_argument("--capex-cut", default=None)
    ap.add_argument("--deal-pulled", default=None)
    ap.add_argument("--json", action="store_true",
                    help="输出机读 JSON（四层状态、引爆点④ 裁决、各判定的输入、数据缺口）而非 markdown")
    return ap.parse_args()


def _finalize(res, rc):
    """--json 收尾：ok / degraded / degraded_reasons 一律在这里算出来，绝不写死。

    · ok 不是字面量——任一自检失败（FRED 取数、--quote 解析、渲染中断）即 False。
    · 「没能判定」一律是 ⚪ / null，绝不是 false；false 只代表「查过、没触发」。
      本脚本的四层状态与引爆点④ 沿用 ⚪ 这个既有词汇，它就是 JSON 里的「未能判定」。
    · 每一次降级（取数失败、退回内建基线报价、报价过期、栏位记 N/A）都留一条短理由。
    · ok=True 只代表「本次自检没失败」，绝不代表「④ 已确认」——后者看 degraded 与
      eval.incomplete_layers，两者不可互相替代。
    """
    res.setdefault("meta", {})
    res.setdefault("errors", [])
    res.setdefault("degraded_reasons", [])
    res["errors"] = [scrub(e) for e in res["errors"]]   # 例外讯息可能夹带绝对路径 → 折叠家目录
    for e in res["errors"]:
        res["degraded_reasons"].append(f"取数/解析失败：{e}")
    ri = res.pop("render_interrupt_error", None)
    if ri:
        # 中断讯息自己就是一条 error，不再套「取数/解析失败」的前缀（那会误导来源）
        res["render_interrupted"] = True
        res["errors"].append(scrub(ri))
        res["degraded_reasons"].append(
            f"报告渲染中断：{scrub(ri)}；仅含中断前已判定的部分，缺项不得推高结论")
    seen, uniq = set(), []
    for r in res["degraded_reasons"]:
        if r not in seen:
            seen.add(r)
            uniq.append(r)
    res["degraded_reasons"] = uniq
    res["ok"] = (rc == 0) and not res["errors"] and not res.get("render_interrupted")
    res["degraded"] = (not res["ok"]) or bool(uniq)


def main():
    """外壳：保证「已拼好的内容一定印得出来」。

    全脚本只有一处 print，早期版本它在最末行；只要末段任何一个 f-string 取数失败
    （FRED 断线时 hy/ccc 为 None），stdout 会一个字都没有——连刚拼好的
    「④ 数据缺口」段也一起丢。改成 out 由外壳持有、finally 一定输出。

    --json 同一套：res 由外壳持有，中断时也要印出合法 JSON（含中断前已判定的层级、
    中断原因、degraded=true），而不是一个字都没有。
    """
    out, res = [], {}
    a = _parse_args()
    rc = 0
    try:
        rc = _render(out, res, a)
        return rc
    except Exception as exc:                      # noqa: BLE001 —— 刻意兜底，不吐裸 traceback
        out.append("")
        out.append(f"- ⚠️ 报告渲染中断：{type(exc).__name__}: {scrub(exc)}"
                   f"（以上为中断前已生成的内容；缺项不得推高结论）")
        # 同一件事也必须是 JSON 栏位：只靠退出码与 stderr，窄读 stdout 的下游看不见。
        res["render_interrupt_error"] = f"{type(exc).__name__}: {scrub(exc)}"
        rc = 1
        return rc
    finally:
        if a.json:
            _finalize(res, rc)
            print(json.dumps(res, ensure_ascii=False, indent=2, default=str))
        elif out:
            print("\n".join(out))


def _render(out, res, a):
    A = out.append
    today = date.today()

    idx, errs, curve = {}, [], {}

    # --json 骨架：键位先建好，中途中断也能印出「已判定的部分 + 中断原因」。
    # errs 与 res["errors"] 是同一个 list 物件，之后 errs.append() 会同步进 JSON。
    gen_at = datetime.now().strftime("%Y-%m-%d %H:%M")
    res.update({
        "meta": {
            "generated_at": gen_at,
            "variant": "lite",                        # 纯标准库云端版
            "history": None,                          # 云端 persist_session=false → 无历史档
            "relative_check_method": "水平法",         # 无历史档 → 变化率法不可用
            "max_quote_age_days": a.max_quote_age,
            # 本脚本的 TH 键名与完整版 neocloud_credit_monitor.py **不同**，两边不可互抄；
            # 重新校准阈值是两处分别改（见 CLAUDE.md）。
            "thresholds": TH,
        },
        "ok": None, "degraded": None, "degraded_reasons": [],
        "fred": {}, "fred_missing": [], "bonds": [], "primary_market": {},
        "eval": {}, "notes": [], "prohibitions": [], "errors": errs,
    })
    D = res["degraded_reasons"].append

    q = dict(BASELINE_QUOTES)
    passed = set()          # 本次真的由 --quote 传入的 key；其余是内建基线（= 退化的取价来源）
    for s in a.quote:
        try:
            k, rest = s.split("=", 1)
            p, d = rest.split("@", 1)
            q[k.strip()] = (float(p), d.strip())
            passed.add(k.strip())
        except Exception:
            errs.append(f"忽略无法解析的 --quote：{scrub(s)}（格式应为 KEY=PRICE@YYYY-MM-DD）")

    for name, (sid, is_credit) in FRED.items():
        try:
            obs = fred(sid)
            idx[name] = {"sid": sid, "lvl": obs[-1][1], "as_of": obs[-1][0],
                         "d1": chg_bp(obs, 1), "d5": chg_bp(obs, 7),
                         "d30": chg_bp(obs, 30), "d90": chg_bp(obs, 90)}
            if name in TENOR:
                curve[TENOR[name]] = obs[-1][1]
        except Exception as e:
            errs.append(f"FRED {sid}({name}): {type(e).__name__} {scrub(e)}")
            res["fred_missing"].append(name)          # 取不到就是取不到：缺哪一条要点名

    # --json：与完整版 neocloud_credit_monitor.py 的 fred 区块同键名，方便两边逐栏对拆。
    for name, d in idx.items():
        res["fred"][name] = {"series_id": d["sid"], "level_pct": d["lvl"], "as_of": d["as_of"],
                             "d1_bp": d["d1"], "d5_bp": d["d5"], "d30_bp": d["d30"],
                             "d90_bp": d["d90"], "tenor_y": TENOR.get(name)}
    res["meta"]["data_date"] = max((d["as_of"] for d in idx.values()), default=None)
    if res["fred_missing"]:
        D(f"FRED 未取到：{'/'.join(res['fred_missing'])}")

    A("### 💳 Neocloud 信用层监控（NEOCLOUD_CREDIT tripwire v2 · lite）")
    A("")
    A(f"生成 {gen_at}｜纯标准库云端版｜无历史档 → 相对基准检验用**水平法**")
    A("")
    A("**① 信用指数阶梯与无风险曲线**")
    A("")
    A("| 项目 | 水平 | 1d(bp) | 5d(bp) | 30d(bp) | 90d(bp) | 数据日 |")
    A("|---|---|---|---|---|---|---|")
    for name in FRED:
        if name in idx:
            d = idx[name]
            f = lambda v: f"{v:+.1f}" if v is not None else "N/A"
            A(f"| {name} | {d['lvl']:.2f}% | {f(d['d1'])} | {f(d['d5'])} | {f(d['d30'])} | {f(d['d90'])} | {d['as_of']} |")
    A("")

    # 债券
    rows = []
    for key, label, tier, ccy, cpn, mat, cohort, isbench in BONDS:
        price, as_of = q.get(key, (None, None))
        r = {"key": key, "label": label, "tier": tier, "bench": isbench, "price": price,
             "as_of": as_of, "spread": None, "prem": None, "usable": False, "stale": True}
        if price and as_of:
            age = (today - date.fromisoformat(as_of)).days
            r["age"] = age
            r["stale"] = age > a.max_quote_age
            yrs = (date.fromisoformat(mat) - today).days / 365.25
            y = ytm(price, cpn, yrs)
            if y:
                r["ytm"] = y * 100
                b = interp(curve, yrs)
                if b:
                    r["b"] = b
                    r["spread"] = round((y * 100 - b) * 100)
                    if cohort in idx:
                        r["cohort"] = cohort
                        r["cidx"] = round(idx[cohort]["lvl"] * 100)
                        r["prem"] = r["spread"] - r["cidx"]
                    r["usable"] = not r["stale"]
        else:
            errs.append(f"债券 {key}: 无报价")
        rows.append(r)

    # --json：债券明细。键名与完整版对齐（lite 的 tier 用 corp/spv，完整版用 corp/spv_ig_tenant）。
    # quote_source 标明这笔价是 --quote 传入还是内建基线——取价来源不明的数字日后无法复核。
    for r in rows:
        res["bonds"].append({
            "key": r["key"], "label": r["label"], "tier": r["tier"],
            "is_benchmark": bool(r["bench"]),
            "price": r["price"], "quote_as_of": r["as_of"],
            "quote_age_days": r.get("age"), "stale": bool(r["stale"]),
            "quote_source": ("--quote" if r["key"] in passed else "内建基线 BASELINE_QUOTES"),
            "ytm_pct": (round(r["ytm"], 2) if r.get("ytm") is not None else None),
            "bench_pct": (round(r["b"], 3) if r.get("b") is not None else None),
            "spread_bp": r["spread"], "cohort_index": r.get("cohort"),
            "cohort_index_bp": r.get("cidx"), "cohort_premium_bp": r["prem"],
            "usable": bool(r["usable"]),
        })
        if r["price"] is None or not r["as_of"]:
            D(f"债券 {r['key']}：无报价")
            continue
        if r["key"] not in passed:
            D(f"债券 {r['key']}：用内建基线报价（{r['as_of']}），本次未以 --quote 覆盖为最新")
        if r["stale"]:
            D(f"债券 {r['key']}：报价 {r.get('age')}天前·已过期不计入判定")
        elif r["spread"] is None:
            D(f"债券 {r['key']}：YTM 或 DGS 曲线插值不可得 → 利差 N/A")
        elif r["prem"] is None:
            D(f"债券 {r['key']}：缺同评级指数（{r.get('cohort') or 'FRED 未取到'}）→ 同评级溢价 N/A")

    A("**② Neocloud 债券利差阶梯**")
    A("")
    A("| 债券 | 层 | 价格 | 报价日 | YTM | 基准 | 利差 | 同评级溢价 |")
    A("|---|---|---|---|---|---|---|---|")
    for r in rows:
        t = "L1公司层" if r["tier"] == "corp" else "L2项目层★"
        st = f"（{r.get('age')}天前·已过期不计入判定）" if r["stale"] and r["as_of"] else ""
        A(f"| {r['label']} | {t} | {r['price'] if r['price'] else 'N/A'}{st} | {r['as_of'] or 'N/A'} | "
          f"{f'{r['ytm']:.2f}%' if r.get('ytm') else 'N/A'} | {f'{r['b']:.2f}%' if r.get('b') else 'N/A'} | "
          f"{f'+{r['spread']}bp' if r['spread'] is not None else 'N/A'} | "
          f"{f'{r['prem']:+d}bp' if r['prem'] is not None else 'N/A'} |")
    A("")
    A("- YTM 由脚本反解（半年付息，折现全价 = 干净价 + 应计利息）；利差 = YTM − DGS 曲线插值。")
    A("")

    # ---- L1
    corp = [r for r in rows if r["tier"] == "corp" and r["usable"]]
    l1b = next((r for r in corp if r["bench"]), None) or (max(corp, key=lambda r: r["spread"]) if corp else None)
    L1, l1det = [], []
    E1 = res["eval"]["L1"] = {}       # --json 子指标字典，键名与完整版 ev["L1"] 对齐
    if l1b:
        s = RED if l1b["spread"] > TH["L1_corp_red"] else (AMBER if l1b["spread"] > TH["L1_corp_amber"] else GREEN)
        L1.append(s); l1det.append(f"基准债 {l1b['label']} {l1b['spread']}bp（阈值 {TH['L1_corp_amber']}/{TH['L1_corp_red']}）→ {s}")
        E1["corp_spread"] = {"state": s, "bond": l1b["label"], "spread_bp": l1b["spread"],
                             "th": f"{TH['L1_corp_amber']}/{TH['L1_corp_red']}bp"}
        if l1b["prem"] is not None:
            s2 = RED if l1b["prem"] > TH["L1_prem_red"] else (AMBER if l1b["prem"] > TH["L1_prem_amber"] else GREEN)
            L1.append(s2); l1det.append(f"同评级溢价 {l1b['prem']:+d}bp vs {l1b['cohort']} {l1b['cidx']}bp（阈值 {TH['L1_prem_amber']}/{TH['L1_prem_red']}）→ {s2}")
            E1["cohort_premium"] = {"state": s2, "premium_bp": l1b["prem"], "index": l1b["cohort"],
                                    "index_bp": l1b["cidx"],
                                    "th": f"{TH['L1_prem_amber']}/{TH['L1_prem_red']}bp"}
    else:
        # 也把 ⚪ 记进状态列表：worst() 会丢弃 GREY，层汇总不受影响，
        # 但 *_inc 的 any(x == GREY ...) 从此能真的检测到「层内某个指标没数据」。
        L1.append(GREY)
        l1det.append(f"{GREY} 无可用公司层债报价（缺报价或已过期）")
        E1["corp_spread"] = {"state": GREY, "spread_bp": None,
                             "reason": "无可用公司层债报价（缺报价或已过期）"}
    # 一级市场重定价的 30 天观察窗（TH["L1_primary_window_days"]）：与完整版
    # neocloud_credit_monitor.py 的 in_window 判定同规则——超窗只作历史参照、不计入 L1，
    # 避免一笔旧交易把 L1 永久卡在🟡。日期非 ISO 时同样视为历史参照，不抛例外。
    try:
        pdays = (today - date.fromisoformat(PRIMARY["date"])).days
    except (ValueError, TypeError):
        pdays = None
    move = PRIMARY["sp1"] - PRIMARY["sp0"]
    sofr = idx.get("SOFR", {}).get("lvl")
    res["primary_market"] = {"issuer": PRIMARY["issuer"], "instrument": PRIMARY["instrument"],
                             "date": PRIMARY["date"], "size_usd_mm": PRIMARY["size_mm"],
                             "base": PRIMARY["base"], "initial_spread_bp": PRIMARY["sp0"],
                             "final_spread_bp": PRIMARY["sp1"], "spread_move_bp": move,
                             "initial_oid": PRIMARY["oid0"], "final_oid": PRIMARY["oid1"],
                             "age_days": pdays, "sofr_pct": sofr,
                             "window_days": TH["L1_primary_window_days"]}
    if pdays is not None and pdays <= TH["L1_primary_window_days"]:
        s3 = AMBER if move >= TH["L1_primary_amber_bp"] else GREEN
        L1.append(s3)
        allin = ""
        allin_d = {}
        if sofr is not None:
            c0 = sofr + PRIMARY["sp0"] / 100 + (100 - PRIMARY["oid0"]) / 3
            c1 = sofr + PRIMARY["sp1"] / 100 + (100 - PRIMARY["oid1"]) / 3
            allin = f"，全包成本 {c0:.2f}%→{c1:.2f}%（{(c1-c0)*100:+.0f}bp，SOFR {sofr:.2f}%）"
            allin_d = {"initial_pct": round(c0, 2), "final_pct": round(c1, 2),
                       "move_bp": round((c1 - c0) * 100), "sofr_pct": sofr}
        else:
            D("缺 SOFR → 一级市场全包成本 N/A")
        l1det.append(f"一级市场 {PRIMARY['issuer']} {PRIMARY['instrument']} {PRIMARY['date']}（{pdays}天前）"
                     f"：{PRIMARY['base']}+{PRIMARY['sp0']}→+{PRIMARY['sp1']}（{move:+d}bp）、OID {PRIMARY['oid0']}→{PRIMARY['oid1']}{allin} → {s3}")
        E1["primary_reprice"] = {"state": s3, "issuer": PRIMARY["issuer"], "date": PRIMARY["date"],
                                 "instrument": PRIMARY["instrument"], "spread_move_bp": move,
                                 "age_days": pdays, "in_window": True, "excluded_by_design": False,
                                 "all_in": allin_d,
                                 "th": f"单笔重定价 ≥{TH['L1_primary_amber_bp']}bp → 🟡"}
    else:
        aged = f"{pdays}天前，超出 {TH['L1_primary_window_days']} 天观察窗" if pdays is not None else "日期非 ISO 格式、无法计龄"
        l1det.append(f"一级市场最近一笔为 {PRIMARY['date']}（{aged}）→ 仅历史参照，不计入判定")
        # 超窗是「有意不计入」而非「没数据」（excluded_by_design），不算数据缺口；
        # 日期无法解析才是真的缺，另记一条降级理由。
        E1["primary_reprice"] = {"state": GREY, "issuer": PRIMARY["issuer"], "date": PRIMARY["date"],
                                 "instrument": PRIMARY["instrument"], "spread_move_bp": move,
                                 "age_days": pdays, "in_window": False, "excluded_by_design": True,
                                 "th": f"{PRIMARY['date']}（{aged}）→ 仅历史参照，不计入判定"}
        if pdays is None:
            D("一级市场日期非 ISO 格式、无法计龄 → 仅历史参照，不计入判定")
    if a.deal_pulled:
        L1.append(RED); l1det.append(f"{RED} 一级市场交易被撤回/延期：{a.deal_pulled}")
        E1["deal_pulled"] = {"state": RED, "detail": a.deal_pulled}
    L1s = worst(*L1) if L1 else GREY
    # worst() 丢弃 GREY，层汇总看不出「层内某个指标没数据」，单独记下来。
    L1_inc = (not L1) or any(x == GREY for x in L1)
    E1["state"], E1["incomplete"], E1["details"] = L1s, L1_inc, l1det

    # ---- L2
    spv = [r for r in rows if r["tier"] == "spv" and r["usable"]]
    l2b = next((r for r in spv if r["bench"]), None) or (spv[0] if spv else None)
    L2, l2det = [], []
    E2 = res["eval"]["L2"] = {}
    if l2b:
        s = RED if l2b["spread"] > TH["L2_red"] else (AMBER if l2b["spread"] > TH["L2_amber"] else GREEN)
        L2.append(s); l2det.append(f"IG租户SPV {l2b['label']} {l2b['spread']}bp（阈值 {TH['L2_amber']}/{TH['L2_red']}）→ {s}")
        E2["spv_spread"] = {"state": s, "bond": l2b["label"], "spread_bp": l2b["spread"],
                            "th": f"{TH['L2_amber']}/{TH['L2_red']}bp"}
        s2 = AMBER if l2b["price"] < TH["L2_price_amber"] else GREEN
        L2.append(s2); l2det.append(f"价格 {l2b['price']}（<{TH['L2_price_amber']} → 🟡）→ {s2}")
        E2["spv_price"] = {"state": s2, "price": l2b["price"],
                           "th": f"< {TH['L2_price_amber']} → 🟡"}
    else:
        # 也把 ⚪ 记进状态列表：worst() 会丢弃 GREY，层汇总不受影响，
        # 但 *_inc 的 any(x == GREY ...) 从此能真的检测到「层内某个指标没数据」。
        L2.append(GREY)
        l2det.append(f"{GREY} 无可用项目层 SPV 债报价")
        E2["spv_spread"] = {"state": GREY, "spread_bp": None, "reason": "无可用项目层 SPV 债报价"}
    L2s = worst(*L2) if L2 else GREY
    # worst() 丢弃 GREY，层汇总看不出「层内某个指标没数据」，单独记下来。
    L2_inc = (not L2) or any(x == GREY for x in L2)
    E2["state"], E2["incomplete"], E2["details"] = L2s, L2_inc, l2det

    # ---- L3
    L3, l3det = [], []
    E3 = res["eval"]["L3"] = {}
    hy, ccc, bb = idx.get("HY OAS"), idx.get("CCC & Lower OAS"), idx.get("BB OAS")
    if hy:
        s = RED if hy["lvl"] > TH["L3_hy_red_pct"] else GREEN
        L3.append(s); l3det.append(f"HY OAS {hy['lvl']:.2f}%（>{TH['L3_hy_red_pct']}% → 🔴）→ {s}")
        E3["hy_oas"] = {"state": s, "level_pct": hy["lvl"], "d30_bp": hy["d30"], "d90_bp": hy["d90"],
                        "th": f">{TH['L3_hy_red_pct']}% → 🔴"}
    if ccc and ccc["d90"] is not None:
        s = AMBER if ccc["d90"] > TH["L3_ccc90_amber"] else GREEN
        L3.append(s); l3det.append(f"CCC OAS {ccc['lvl']:.2f}%、90日 {ccc['d90']:+.0f}bp（>+{TH['L3_ccc90_amber']}bp → 🟡）→ {s}")
        E3["ccc_90d"] = {"state": s, "d90_bp": ccc["d90"], "level_pct": ccc["lvl"],
                         "th": f">+{TH['L3_ccc90_amber']}bp → 🟡"}
    if ccc and bb:
        diff = round((ccc["lvl"] - bb["lvl"]) * 100)
        s = AMBER if diff > TH["L3_ccc_bb_amber"] else GREEN
        L3.append(s); l3det.append(f"CCC−BB {diff}bp（>{TH['L3_ccc_bb_amber']}bp → 🟡）→ {s}")
        E3["ccc_minus_bb"] = {"state": s, "diff_bp": diff, "th": f">{TH['L3_ccc_bb_amber']}bp → 🟡"}
    L3s = worst(*L3) if L3 else GREY
    # worst() 丢弃 GREY，层汇总看不出「层内某个指标没数据」，单独记下来。
    L3_inc = (not L3) or any(x == GREY for x in L3)
    E3["state"], E3["incomplete"], E3["details"] = L3s, L3_inc, l3det

    # ---- L4
    L4, l4det = [], []
    E4 = res["eval"]["L4"] = {}
    if a.nvda_dd is not None:
        s = AMBER if a.nvda_dd < TH["L4_nvda_dd_amber"] else GREEN
        L4.append(s); l4det.append(f"NVDA 距52周高 {a.nvda_dd:+.1f}%（<{TH['L4_nvda_dd_amber']}% → 🟡）→ {s}")
        E4["nvda_drawdown"] = {"state": s, "dd_pct": a.nvda_dd, "source": "--nvda-dd",
                               "th": f"< {TH['L4_nvda_dd_amber']}% → 🟡"}
    else:
        # 也把 ⚪ 记进状态列表：worst() 会丢弃 GREY，层汇总不受影响，
        # 但 *_inc 的 any(x == GREY ...) 从此能真的检测到「层内某个指标没数据」。
        L4.append(GREY)
        l4det.append(f"{GREY} 未传入 --nvda-dd（应用第二步已取到的 NVDA 52周回撤）")
        E4["nvda_drawdown"] = {"state": GREY, "dd_pct": None,
                               "reason": "未传入 --nvda-dd（应用第二步已取到的 NVDA 52周回撤）"}
        D("未传入 --nvda-dd → L4 的 NVDA 52周回撤 N/A")
    if a.capex_cut:
        L4.append(RED); l4det.append(f"{RED} Hyperscaler capex 指引下修：{a.capex_cut}（须与引爆点① 一致）")
        E4["capex_cut"] = {"state": RED, "detail": a.capex_cut, "note": "须与引爆点① 一致"}
    L4s = worst(*L4) if L4 else GREY
    # worst() 丢弃 GREY，层汇总看不出「层内某个指标没数据」，单独记下来。
    L4_inc = (not L4) or any(x == GREY for x in L4)
    E4["state"], E4["incomplete"], E4["details"] = L4s, L4_inc, l4det

    # 数据不完整的层：④ 映射的措辞、汇总裁决的措辞、JSON 三处共用同一份。
    # 一旦分叉，同一份输出会一边说「缺数据」一边说「论点侧完好」。
    inc_layers = [n for n, st_, ic in (("L1", L1s, L1_inc), ("L2", L2s, L2_inc),
                                       ("L3", L3s, L3_inc), ("L4", L4s, L4_inc))
                  if st_ == GREY or ic]
    res["eval"]["incomplete_layers"] = inc_layers
    for n in inc_layers:
        D(f"{n} 本次⚪未取到数据，未能确认（缺项不得推高结论）")

    # ---- 相对基准检验（水平法）
    if l1b and l1b["prem"] is not None:
        rel = "个体" if l1b["prem"] > TH["L1_prem_amber"] else "宏观"
        reld = (f"水平法：{l1b['label']} 利差 {l1b['spread']}bp vs {l1b['cohort']} {l1b['cidx']}bp，"
                f"个体溢价 {l1b['prem']:+d}bp（阈值 {TH['L1_prem_amber']}bp）→ {rel}。"
                f"云端无历史档，变化率法不可用")
    else:
        rel, reld = "无法区分", "缺 HY/同评级基准或缺 neocloud 利差 → ④ 封顶🟡，不得升🔴"
        D("相对基准检验无法区分宏观 vs 个体 → ④ 封顶🟡，不得升🔴")
    res["eval"]["relative_check"] = {
        "verdict": rel, "detail": reld, "method": "水平法",
        "cohort_premium_bp": (l1b["prem"] if (l1b and l1b["prem"] is not None) else None),
        # 无历史档 → 30 日超额走阔无从算起，一律 null，绝不由残缺数据推算
        "excess_bp": None,
        "excess_bp_reason": "云端无历史档，变化率法不可用",
    }

    # ---- ④ 映射
    thesis, fin = worst(L2s, L4s), worst(L1s, L3s)
    if thesis == RED:
        t4, why = RED, "项目层/上游已破 → 论点侧受损"
    elif rel == "宏观" and fin == RED:
        t4, why = AMBER, "公司层达🔴但与基准同步 → 判宏观、归回调驱动源处理"
    elif rel == "无法区分" and fin in (RED, AMBER):
        t4, why = AMBER, "缺基准、无法区分宏观 vs 个体 → 封顶🟡"
    elif fin == RED and rel == "个体":
        t4, why = RED, "公司层达困境水平且相对基准显著走阔 → 个体融资链问题"
    else:
        t4 = worst(fin, thesis)
        # 层内某个指标为 ⚪ 会被 worst() 丢掉、冒不到层汇总，所以这里按「层是否完整」判，
        # 而不是只看层汇总是不是 GREY。否则「一级市场在窗🟢 + 二级报价全过期⚪」
        # 会让 L1 汇总显示 🟢，输出却断言「四层皆未触发·论点侧支撑完好」。
        _inc = list(inc_layers)
        if t4 in (AMBER, GREEN) and _inc:
            _tail = "/".join(_inc) + " 本次⚪未取到数据，未能确认"
            why = ("融资成本上升/尾部走阔，" + _tail) if t4 == AMBER else ("已取到的层级均未触发，但 " + _tail)
        else:
            _inc = []
            why = {RED: "见明细", AMBER: "融资成本上升/尾部走阔，项目层与上游仍完好",
                   GREEN: "四层皆未触发", GREY: "本次未取到可用信用数据"}.get(t4, "")
    moved = "/".join([n for n, s in (("L1", L1s), ("L3", L3s)) if s in (AMBER, RED)]) or "L1/L3"
    # tag = 精简版一行的结论片段。**必须与 verdict 同源**：精简版是唯一进手机的那行，
    # 以前它自己重算条件、漏看 L2s/L4s，于是 L2 为⚪（报价过期）时表里写⚪、
    # 同一次输出的精简版却断言「L2/L4未破」——把「数据缺失」讲成「论点侧完好」，
    # 正是本框架反复强调最贵的那个错误。
    if thesis == RED:
        verdict = "🔴 主题崩坏风险：项目层或上游已确认受损，回调不是机会"
        tag = "🔴主题崩坏"
    elif t4 == RED:
        verdict = "🔴 个体融资链告警（融资侧）：论点侧未破 → **不走论点闸门、不改分桶**，只减半节奏"
        tag = "🔴融资链告警"
    elif fin in (AMBER, RED):
        # ⚪ 不得读成好消息：论点侧缺数据时只能说「未能确认」，不能说「未破」
        both_green = (L2s == GREEN and L4s == GREEN and not L2_inc and not L4_inc)
        side = ("未破 → 主题仍在，**不降级买入桶**" if both_green
                else "**数据不足、未能确认**（缺项不计入升档，但也不得当作未破）→ 分桶维持、节奏转保守")
        verdict = (f"🟡 可买的回撤（限定在融资成本这条腿）：{moved} 已动，衡量的是「股东被稀释多少」；"
                   f"L2{L2s}、L4{L4s}（项目层与上游）{side}")
        tag = (f"🟡可买的回撤（{moved}动，L2/L4未破）" if both_green
               else f"🟡{moved}动·L2{L2s}/L4{L4s}数据不足未能确认")
    elif t4 == GREY:
        verdict = "⚪ 信用层数据不足，④ 沿用上次状态，不改变分桶"
        tag = "⚪数据不足"
    else:
        # worst() 丢弃 GREY，所以 t4=🟢 并不代表四层都有数据。
        # 任一层为 ⚪ 时只能说「未见触发」，绝不能说「四层皆未触发·支撑完好」——
        # 那是把数据缺失讲成好消息，与上面 🟡 分支要防的是同一个错误。
        _grey = list(inc_layers)
        if _grey:
            verdict = ("🟢 已取到的层级均未触发，但 " + "/".join(_grey)
                       + " 本次⚪未取到数据 → 非已确认「四层皆未破」")
            tag = "🟢未见触发·" + "/".join(_grey) + "⚪缺数据"
        else:
            verdict = "🟢 信用层四层皆未触发，论点侧支撑完好"
            tag = "🟢论点侧完好"
    res["eval"]["tripwire_4"] = {"state": t4, "why": why,
                                 "thesis_side": thesis, "financing_side": fin,
                                 "moved_layers": moved}
    res["eval"]["verdict_line"] = verdict
    res["eval"]["verdict_tag"] = tag

    A("**③ 四层判定**")
    A("")
    A("| 层 | 明细 | 汇总 |")
    A("|---|---|---|")
    A(f"| L1 公司层 | {'<br>'.join(l1det)} | **{L1s}** |")
    A(f"| L2 项目层★ | {'<br>'.join(l2det)} | **{L2s}** |")
    A(f"| L3 宏观确认 | {'<br>'.join(l3det)} | **{L3s}** |")
    A(f"| L4 上游传染★ | {'<br>'.join(l4det)} | **{L4s}** |")
    A("")
    A(f"- **相对基准检验：{rel}** — {reld}")
    A(f"- ★ L2/L4 = 论点侧（走步骤3 宏观闸门）；L1/L3 = 融资侧（只走步骤4 节奏层，**不得降桶**）。")
    A("")
    A(f"**汇总裁决：{verdict}**")
    A("")
    A(f"**引爆点④ = {t4}** — {why}（论点侧 {thesis}｜融资侧 {fin}）")
    A("")
    if errs:
        A("**④ 数据缺口（缺项不得推高结论）**")
        A("")
        for e in errs:
            A(f"- {scrub(e)}")      # 例外讯息可能带绝对路径 → 一律折叠家目录
        A("")
    A("- **无实时 CDS 数据是本层最大盲区**（CoreWeave 5y CDS：2025-12 峰值 881bp、2026-06 降至 452bp）。")
    A("- 债券报价成交稀薄，与 TRACE 实际成交可能差数十 bp；样本以 CoreWeave（公司层）+ Black Pearl（项目层）为主。")
    A("")
    # 文字分支印出的每一条盲区与禁令，也必须是 JSON 栏位（只看 --json 的下游读不到 markdown）。
    # 措辞一律照抄上面的输出，不另行改写。
    res["notes"] = [
        "YTM 由脚本反解（半年付息，折现全价 = 干净价 + 应计利息）；利差 = YTM − DGS 曲线插值。",
        "**无实时 CDS 数据是本层最大盲区**（CoreWeave 5y CDS：2025-12 峰值 881bp、2026-06 降至 452bp）。",
        "债券报价成交稀薄，与 TRACE 实际成交可能差数十 bp；样本以 CoreWeave（公司层）+ Black Pearl（项目层）为主。",
        "无历史档 → 相对基准检验用**水平法**",
    ]
    res["prohibitions"] = [
        "④ 数据缺口：缺项不得推高结论",
        "★ L2/L4 = 论点侧（走步骤3 宏观闸门）；L1/L3 = 融资侧（只走步骤4 节奏层，**不得降桶**）。",
        "⚪ 不得读成好消息：论点侧缺数据时只能说「未能确认」，不能说「未破」",
        "报价超过 --max-quote-age 天即已过期，不计入判定",
        "一级市场超出 30 天观察窗的交易仅历史参照，不计入判定",
    ]
    if rel == "无法区分":
        res["prohibitions"].append("缺基准、无法区分宏观 vs 个体 → ④ 封顶🟡，不得升🔴")
    A("**⑤ 贴回「🧭 引爆点监控」表第④行**")
    A("")
    # 下面两行以前是裸取 hy['lvl'] / ccc['d90']：FRED 断线时（云端 routine 的常态）
    # 会在最后一行 f-string 抛例外，整份报告连同「④ 数据缺口」段一起消失。一律走 num()。
    l1sp = f"{l1b['spread']}bp" if l1b and l1b.get("spread") is not None else "N/A"
    l2sp = f"{l2b['spread']}bp" if l2b and l2b.get("spread") is not None else "N/A"
    hy_lvl = num(hy, "lvl", "{:.2f}", "%")
    ccc90 = num(ccc, "d90", "{:+.0f}", "bp")
    hy_asof = (hy or {}).get("as_of") or "N/A"
    row4 = (f"| ④ | Neocloud 信用利差扩大至困境水平 | {t4} | "
            f"L1 {l1sp}｜L2 {l2sp}｜"
            f"HY {hy_lvl}｜CCC90d {ccc90}（{hy_asof}） | "
            f"FRED {'/'.join(FRED[k][0] for k in ('HY OAS','CCC & Lower OAS'))} + 债券报价本地反解 | {verdict} |")
    A(row4)
    A("")
    compact = (f"💳 信用④{t4}｜L1 {l1sp}｜L2 {l2sp}｜"
               f"HY {hy_lvl}｜CCC90d {ccc90}｜{rel}｜{tag}")
    A(f"**💳 精简版一行**：{compact}")
    # ⑤ 表格行与精简版一行都直接进日报/Slack：两条字串与 markdown 同源，不另拼一份。
    res["tripwire_row"] = row4
    res["compact_line"] = compact
    return 0


if __name__ == "__main__":
    # 最后一道闸：任何未预期的例外都不得吐出裸 traceback
    #（traceback 会印出 /Users/<用户名>/... 的脚本路径，而本脚本的输出会推 Slack）。
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        err("✗ 已中断。")
        sys.exit(130)
    except Exception as exc:                      # noqa: BLE001 —— 刻意兜底
        err(f"✗ 信用层 lite 脚本执行失败：{type(exc).__name__}: {scrub(exc)}")
        sys.exit(1)
