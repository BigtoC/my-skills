#!/usr/bin/env python3
"""
数据源可达性探针（一次性诊断 · 换源前必跑）

为什么需要它：
    technicals.py 在 Claude Routines 环境里 exit 3、不写 tech.json，成因被记作
    「Yahoo 对本环境全面 429」。但 429 只是**一种**可能，而且各种成因的解法互相冲突：

        TLS 握手失败   -> 装 CA bundle（certifi），换数据源没有用
        DNS 解析失败   -> 出口网络配置问题，换数据源没有用
        UA 被拒        -> 加浏览器 UA 即可，换数据源没有用
        批量被限、单只可用 -> 改逐只 + 限速即可，换数据源没有用
        边缘层 IP 级 429  -> 只有换上游才有救

    本脚本把这五种区分开，并对每个候选档位实测一次，回答唯一真正要紧的问题：
    **在这台机器上，哪条回退链是真的可用的。**

为什么不能只看 HTTP 状态码（本脚本的核心设计）：
    候选源里**每一种失败模式都长成 HTTP 200**——
      - Cloudflare 机器人拦截页：200 + text/html + 「Just a moment」
      - stockanalysis 的 range 静默降级：200 + 合法 JSON + 恰好 252 根
      - Naver 代码错误/窗口反了：200 + 只有表头的 70 字节 body
      - 腾讯 us.VIX：200 + code:0 + 非空 day 数组，最后一根停在数月前
    所以每一档都必须校验 content-type、body 内的状态、根数、以及数值是否离谱。
    只认状态码的探针会把这些全部报成「可用」，那比没有探针更危险。

用法:
    python3 scripts/source_probe.py                 # 人读表格
    python3 scripts/source_probe.py --json          # JSON 打到 stdout
    python3 scripts/source_probe.py --json out.json # 写文件
    python3 scripts/source_probe.py --quiet         # 不渲染表格；告警照出 stderr
    python3 scripts/source_probe.py --strict        # 无可用链时 exit 3（给调度层用）
    python3 scripts/source_probe.py --coverage      # 额外实测全部 46 标的覆盖率（请求变多）

退出码（沿用本仓库约定）:
    0  探针跑完了（**不论探到什么**——「全都不可用」也是一次成功的探测）
    1  参数错误
    2  依赖缺失（requests）
    3  仅 --strict：美股或韩股没有任何可用档位

请求预算：默认 ~14 个请求，刻意压到最小。
    腾讯 web.ifzq.gtimg.cn 在密集探测后会按 (host, path) 粒度封禁本 IP 约 30 分钟
    （2026-09-11 实测），期间 hk_quote.py 的 hi52 会整个塌掉。不要循环跑本脚本。

依赖: python3 + requests。yfinance 只在装了的时候才测，没装不影响其余档位。
"""

from __future__ import annotations

import argparse
import ast
import datetime as dt
import json
import re
import sys
import time

SCRIPT_NAME = "source_probe.py"

# ---------------------------------------------------------------------------
# 路径脱敏：输出会被贴进报告 / 推到 Slack，本仓库是公开仓库，绝不能带出用户名。
# 注意：这是本仓库第 11 份 scrub 实现（2026-09-11 实测 grep -rl '_HOMEISH_RE' = 10 份，
# 本文件是第 11 份）。修路径脱敏规则是 N 处编辑，grep '_HOMEISH_RE' 能找全。
# ---------------------------------------------------------------------------
_HOMEISH_RE = re.compile(r"(?:/Users|/home|/var/folders)/[^/\s\"']+")


def scrub(s) -> str:
    return _HOMEISH_RE.sub("~", str(s))


def err(msg: str) -> None:
    """告警一律走 stderr。--quiet 只关表格，永远不关告警。"""
    print(msg, file=sys.stderr)


# ---------------------------------------------------------------------------
# 逐 host 的 header 规则。这些是实测出来的，而且**互相冲突**，所以只能逐 host 配：
#   Yahoo          必须带浏览器 UA（裸 requests.Session 会被限流）
#   FRED           绝不能带浏览器 UA（Chrome UA -> 25~30s ReadTimeout）
#   api.nasdaq.com 不能带「机器人形状」的 UA：空 UA -> 200/1.33s；
#                  python-requests/2.32.3 与 curl/8.7.1 -> 20s ReadTimeout。
#                  注意规则方向和 FRED 相反，也和直觉相反——别写成「需要浏览器 UA」。
#   stockanalysis  无需任何 header
#   Naver          无需任何 header
#   腾讯/东财      沿用 hk_quote.py 里那串 UA（是否必需未定论，照抄最稳）
# ---------------------------------------------------------------------------
BROWSER_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

HEADERS_BROWSER = {"User-Agent": BROWSER_UA}
HEADERS_NONE: dict = {}
HEADERS_EMPTY_UA = {"User-Agent": ""}

# HTTP 200 + 机器人拦截页的特征串。Cloudflare 挡的就是「机房 IP」这一类流量，
# 而 Routines 盒子被 Yahoo 全面 429 本身就说明它的出口 IP 属于这一类，
# 所以这一段是本脚本最该命中的分支，不是理论情况。
_CHALLENGE_MARKERS = (
    "just a moment",
    "enable javascript",
    "requires javascript to verify",
    "cf-browser-verification",
    "attention required",
    "checking your browser",
    "captcha",
    "access denied",
    "cf_chl",
)


def classify_response(resp, body: str, want_json: bool) -> tuple[str, str | None]:
    """把一次 HTTP 应答归类。返回 (failure_class, detail)；可用时 ('ok', None)。"""
    ctype = (resp.headers.get("content-type") or "").lower()
    low = body[:4000].lower()

    if resp.headers.get("cf-mitigated"):
        return "bot_interstitial", f"Cloudflare 拦截（cf-mitigated: {resp.headers['cf-mitigated']}）"
    if resp.status_code == 429:
        return "http_429", "上游明确限流（429）"
    if resp.status_code == 403:
        return "http_403", "上游拒绝（403）"
    if resp.status_code >= 400:
        return "http_error", f"HTTP {resp.status_code}"
    if any(m in low for m in _CHALLENGE_MARKERS):
        return "bot_interstitial", "HTTP 200 但 body 是机器人验证/拦截页"
    if want_json:
        if low.lstrip().startswith(("<!doctype", "<html", "<?xml")):
            return "bot_interstitial", f"HTTP 200 但返回 HTML（content-type: {ctype or ' 未给'}）"
        if "json" not in ctype and "text/plain" not in ctype and not low.lstrip().startswith(("{", "[")):
            return "body_shape", f"content-type 不是 JSON（{ctype or '未给'}）"
    return "ok", None


def http_probe(url, headers, timeout=20, want_json=True, encoding=None):
    """
    单次探测。返回 dict，永远不抛。

    失败分类是本脚本的产品本身：TLS / DNS / 连接 / 超时 / 429 / 拦截页 / body 形状
    的解法互不相同，混成一句「取数失败」就等于没探。
    """
    import requests

    out = {
        "url": url,
        "http_status": None,
        "content_type": None,
        "elapsed_ms": None,
        "failure_class": None,
        "detail": None,
        "body_head": None,
    }
    t0 = time.time()
    try:
        resp = requests.get(url, headers=headers, timeout=timeout)
    except requests.exceptions.SSLError as exc:
        out["elapsed_ms"] = round((time.time() - t0) * 1000, 1)
        out["failure_class"] = "tls_handshake"
        # 这一支极易被误读成限流。本仓库已知：本机 stdlib urllib 对 FRED / multpl / CNN
        # 一律 CERTIFICATE_VERIFY_FAILED，因为 Python 没接系统 CA store。
        # 解法是装 certifi / 配 CA bundle，**不是换数据源**。
        out["detail"] = f"TLS 握手失败（{scrub(exc)[:200]}）——是证书问题，不是限流"
        return out
    except requests.exceptions.ConnectTimeout as exc:
        out["elapsed_ms"] = round((time.time() - t0) * 1000, 1)
        out["failure_class"] = "conn_timeout"
        out["detail"] = f"连接超时（{scrub(exc)[:160]}）"
        return out
    except requests.exceptions.ReadTimeout as exc:
        out["elapsed_ms"] = round((time.time() - t0) * 1000, 1)
        out["failure_class"] = "read_timeout"
        # api.nasdaq.com 对「机器人形状」的 UA 就是这个表现：不拒绝，直接吊死。
        out["detail"] = f"读超时（{scrub(exc)[:160]}）——常见于 UA 被上游静默丢弃"
        return out
    except requests.exceptions.ConnectionError as exc:
        out["elapsed_ms"] = round((time.time() - t0) * 1000, 1)
        text = scrub(exc)
        if "NameResolution" in text or "nodename nor servname" in text or "Name or service not known" in text:
            out["failure_class"] = "dns"
            out["detail"] = f"DNS 解析失败（{text[:160]}）——出口网络问题，换数据源无用"
        else:
            out["failure_class"] = "conn_error"
            out["detail"] = f"连接失败（{text[:160]}）"
        return out
    except Exception as exc:  # noqa: BLE001 — 探针不允许因为任何异常自己死掉
        out["elapsed_ms"] = round((time.time() - t0) * 1000, 1)
        out["failure_class"] = "unknown"
        out["detail"] = f"{type(exc).__name__}: {scrub(exc)[:160]}"
        return out

    out["elapsed_ms"] = round((time.time() - t0) * 1000, 1)
    out["http_status"] = resp.status_code
    out["content_type"] = resp.headers.get("content-type")
    if encoding:
        body = resp.content.decode(encoding, errors="replace")
    else:
        body = resp.text
    cls, detail = classify_response(resp, body, want_json)
    out["failure_class"] = cls
    out["detail"] = detail
    out["body_head"] = body[:300]
    out["_body"] = body
    return out


# ---------------------------------------------------------------------------
# 各档位探测
#
# 每个探测函数返回统一结构。ok 永远不是字面量：它由自检结果决定。
# 无法判定的检查记 null，不记 false——false 的意思是「查了、没触发」。
# ---------------------------------------------------------------------------
def _tier(tier, name, role, caliber=None):
    return {
        "tier": tier,
        "name": name,
        "role": role,
        "caliber": caliber,
        "ok": None,
        "verdict": None,
        "bars": None,
        "first_date": None,
        "last_date": None,
        "checks": {},
        "notes": [],
        "degraded": None,
        "degraded_reasons": [],
    }


def _fail(t, probe, extra=None):
    t["ok"] = False
    t["verdict"] = "不可用"
    t["degraded"] = True
    t["degraded_reasons"].append(probe.get("detail") or probe.get("failure_class") or "未知失败")
    if extra:
        t["notes"].append(extra)
    return t


def probe_stockanalysis(ticker="NVDA", etf_ticker="SMH"):
    """美股第 1 档。口径 = yfinance auto_adjust=False（c=Close, a=Adj Close）。"""
    t = _tier("us.1", "stockanalysis.com", "美股日线 第1档",
              "拆股已还原·股息未复权（== yfinance auto_adjust=False）")
    url = f"https://stockanalysis.com/api/symbol/s/{ticker.lower()}/history?range=5Y&period=Daily"
    t["url"] = url
    p = http_probe(url, HEADERS_NONE)
    t["transport"] = p
    if p["failure_class"] != "ok":
        return _fail(t, p)

    try:
        payload = json.loads(p["_body"])
        data = payload.get("data")
        rows = data.get("data") if isinstance(data, dict) else data
        if not isinstance(rows, list) or not rows:
            raise ValueError("data 不是非空数组")
        t["bars"] = len(rows)
        t["last_date"] = rows[0].get("t")    # 该源按时间倒序
        t["first_date"] = rows[-1].get("t")
        t["checks"]["body_status"] = payload.get("status")
    except Exception as exc:  # noqa: BLE001
        p2 = dict(p, failure_class="body_shape", detail=f"JSON 解析失败（{scrub(exc)[:160]}）")
        return _fail(t, p2)

    # range 静默降级：2Y/MAX/foo 一律返回 200 + 恰好 252 根。只有 5Y/10Y 被遵守。
    # 对 NVDA 这种上市够久的票，5Y 应该 ~1255 根；拿到恰好 252 就是被降级了。
    t["checks"]["range_honoured"] = t["bars"] != 252
    if t["bars"] == 252:
        t["degraded_reasons"].append(
            "range=5Y 被静默降级为 252 根（该源对无法识别的 range 一律返回恰好 252 根）"
            "——52周高会退化成零余量窗口")
    t["checks"]["enough_for_52w"] = t["bars"] >= 252

    # ETF 必须走同一个 s/ 前缀才成立（LYTE/NCLD/DRAM/SMH/SOXX 都在池子里）
    eurl = f"https://stockanalysis.com/api/symbol/s/{etf_ticker.lower()}/history?range=5Y&period=Daily"
    ep = http_probe(eurl, HEADERS_NONE)
    if ep["failure_class"] == "ok":
        try:
            edata = json.loads(ep["_body"])["data"]
            erows = edata.get("data") if isinstance(edata, dict) else edata
            t["checks"]["etf_same_prefix"] = isinstance(erows, list) and len(erows) >= 252
            t["notes"].append(f"ETF {etf_ticker} 走同一 s/ 前缀：{len(erows)} 根")
        except Exception:  # noqa: BLE001
            t["checks"]["etf_same_prefix"] = False
    else:
        t["checks"]["etf_same_prefix"] = False
        t["degraded_reasons"].append(f"ETF {etf_ticker} 探测失败：{ep.get('detail')}")

    if not t["checks"]["etf_same_prefix"]:
        t["degraded_reasons"].append(
            f"ETF（{etf_ticker}）不可用——池子里 5 只 ETF 会整体缺数，其中 SMH 是 US_REF_TICKER，"
            "缺了会让 technicals.py 无法确定美股完整交易日并 exit 3")

    t["ok"] = bool(t["checks"].get("enough_for_52w") and t["checks"].get("etf_same_prefix")
                   and t["checks"].get("range_honoured"))
    t["degraded"] = bool(t["degraded_reasons"])
    t["verdict"] = "可用" if t["ok"] else ("可疑（HTTP 200 但内容不合格）" if p["failure_class"] == "ok" else "不可用")
    return t


def probe_nasdaq(ticker="NVDA"):
    """美股第 2 档。存在的理由是它的**成交量**口径贴近 yfinance（量比要用）。"""
    t = _tier("us.2", "api.nasdaq.com", "美股日线 第2档（成交量口径用）",
              "拆股已还原·股息未复权")
    today = dt.date.today()
    start = today - dt.timedelta(days=400)
    url = (f"https://api.nasdaq.com/api/quote/{ticker}/historical"
           f"?assetclass=stocks&fromdate={start:%Y-%m-%d}&todate={today:%Y-%m-%d}&limit=400")
    t["url"] = url
    # 空 UA 是实测唯一稳定可用的配置；带 python-requests/curl 形状的 UA 会读超时。
    p = http_probe(url, HEADERS_EMPTY_UA, timeout=25)
    t["transport"] = p
    if p["failure_class"] != "ok":
        t["notes"].append("该 host 对 UA 极敏感：空 UA 实测可用，python-requests/curl 形状的 UA 会吊死到读超时")
        return _fail(t, p)
    try:
        payload = json.loads(p["_body"])
        rows = (payload.get("data") or {}).get("tradesTable", {}).get("rows")
        if not rows:
            raise ValueError("tradesTable.rows 为空")
        t["bars"] = len(rows)
        t["last_date"] = rows[0].get("date")
        t["first_date"] = rows[-1].get("date")
    except Exception as exc:  # noqa: BLE001
        return _fail(t, dict(p, failure_class="body_shape",
                             detail=f"JSON 结构不符（{scrub(exc)[:160]}）"))
    t["checks"]["has_rows"] = t["bars"] > 0
    t["ok"] = t["bars"] >= 200
    t["degraded"] = not t["ok"]
    if not t["ok"]:
        t["degraded_reasons"].append(f"只拿到 {t['bars']} 根，不足以支撑 252 根 52 周高")
    t["verdict"] = "可用" if t["ok"] else "可疑（HTTP 200 但根数不足）"
    return t


def probe_naver(symbol="005930"):
    """韩股第 1 档。"""
    t = _tier("kr.1", "api.finance.naver.com/siseJson", "韩股日线 第1档",
              "拆股已还原·股息未复权（同 yfinance auto_adjust=False）")
    today = dt.date.today()
    start = today - dt.timedelta(days=900)
    url = (f"https://api.finance.naver.com/siseJson.naver?symbol={symbol}"
           f"&requestType=1&startTime={start:%Y%m%d}&endTime={today:%Y%m%d}&timeframe=day")
    t["url"] = url
    p = http_probe(url, HEADERS_NONE)
    t["transport"] = p
    if p["failure_class"] != "ok":
        return _fail(t, p)

    body = p["_body"].strip()
    # 代码错 / 窗口反了 / 周末窗口 / requestType != 1 -> HTTP 200 + 只有表头的 ~70 字节
    if len(body) < 120:
        return _fail(t, dict(p, failure_class="body_shape",
                             detail=f"只返回表头（{len(body)} 字节）——代码错或窗口无效，但 HTTP 仍是 200"))
    try:
        # 注意：这不是合法 JSON（单引号 + 韩文表头），json.loads 会抛
        # JSONDecodeError: Expecting value: line 2 column 4。必须用 literal_eval。
        rows = ast.literal_eval(body)
        body_rows = [r for r in rows[1:] if r and str(r[0]).isdigit()]
        # 2018 年停牌日会给出 O/H/L 全 0 的行，必须剔掉
        body_rows = [r for r in body_rows if not (r[1] == 0 and r[2] == 0 and r[3] == 0)]
        if not body_rows:
            raise ValueError("无有效数据行")
        t["bars"] = len(body_rows)
        t["first_date"] = str(body_rows[0][0])
        t["last_date"] = str(body_rows[-1][0])
    except Exception as exc:  # noqa: BLE001
        return _fail(t, dict(p, failure_class="body_shape",
                             detail=f"literal_eval 解析失败（{scrub(exc)[:160]}）"))

    t["checks"]["parsed_with_literal_eval"] = True
    t["checks"]["enough_for_52w"] = t["bars"] >= 252
    t["ok"] = bool(t["checks"]["enough_for_52w"])
    t["degraded"] = not t["ok"]
    if not t["ok"]:
        t["degraded_reasons"].append(f"只拿到 {t['bars']} 根，不足 252 根")
    t["verdict"] = "可用" if t["ok"] else "可疑（HTTP 200 但根数不足）"
    t["notes"].append(
        "KRX 开市期间最后一根是**实时未完成**的 bar，消费端必须丢弃"
        "（2026-09-11 12:xx KST 实测：当日量 9,154,964 vs 前一日 22,517,075）")
    return t


def probe_tencent_quote():
    """腾讯实时报价。美/韩/港一次批量。注意：美股日线口径不合格，这里只验报价可达性。"""
    t = _tier("aux.tencent_quote", "qt.gtimg.cn", "腾讯实时报价（港股现价 / 韩股现价）",
              "实时快照")
    url = "https://qt.gtimg.cn/q=hk00700,usNVDA,kr005930"
    t["url"] = url
    p = http_probe(url, HEADERS_BROWSER, want_json=False, encoding="gbk")
    t["transport"] = p
    if p["failure_class"] != "ok":
        return _fail(t, p)
    body = p["_body"]
    got = {"hk": "v_hk00700" in body, "us": "v_usNVDA" in body, "kr": "v_kr005930" in body}
    t["checks"].update({f"quote_{k}": v for k, v in got.items()})
    t["ok"] = all(got.values())
    t["degraded"] = not t["ok"]
    for k, v in got.items():
        if not v:
            t["degraded_reasons"].append(f"{k} 报价未返回")
    t["verdict"] = "可用" if t["ok"] else "部分可用"
    return t


def probe_tencent_kline_hk(code="00700"):
    """港股日线（hk_quote.py 的 52 周高就靠它）。"""
    t = _tier("hk.1", "web.ifzq.gtimg.cn/fqkline", "港股日线 第1档（hk_quote.py 现用）",
              "原始未复权（拆股亦未还原——见禁止事项）")
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=hk{code},day,,,300,"
    t["url"] = url
    p = http_probe(url, HEADERS_BROWSER)
    t["transport"] = p
    if p["failure_class"] != "ok":
        return _fail(t, p, "该 host 会按 (host, path) 粒度封禁探测过密的 IP 约 30 分钟")
    try:
        d = json.loads(p["_body"])["data"][f"hk{code}"]
        kl = d.get("day") or d.get("qfqday") or []
        t["bars"] = len(kl)
        t["first_date"], t["last_date"] = (kl[0][0], kl[-1][0]) if kl else (None, None)
    except Exception as exc:  # noqa: BLE001
        return _fail(t, dict(p, failure_class="body_shape", detail=f"结构不符（{scrub(exc)[:160]}）"))
    t["ok"] = t["bars"] >= 252
    t["degraded"] = not t["ok"]
    if not t["ok"]:
        t["degraded_reasons"].append(f"只拿到 {t['bars']} 根，hi52 会退化或记 N/A")
    t["verdict"] = "可用" if t["ok"] else "可疑（根数不足）"
    t["notes"].append(
        "港股开市期间最后一根同样是**实时未完成**的 bar（与 Naver 同一陷阱）；"
        "hk_quote.py 现在只用它算 hi52，若将来加 --bars 模式必须丢弃这一根")
    return t


def probe_eastmoney(code="00700"):
    """港股日线第 2 档（hk_quote.py 的既有回退）。本机 2026-09-11 全程不可达。"""
    t = _tier("hk.2", "push2his.eastmoney.com", "港股日线 第2档（hk_quote.py 既有回退）",
              "不复权（fqt=0）")
    today = dt.date.today()
    beg = today - dt.timedelta(days=400)
    url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get"
           f"?secid=116.{code}&fields1=f1,f2,f3&fields2=f51,f52,f53,f54,f55,f56"
           f"&klt=101&fqt=0&beg={beg:%Y%m%d}&end={today:%Y%m%d}")
    t["url"] = url
    p = http_probe(url, HEADERS_BROWSER)
    t["transport"] = p
    if p["failure_class"] != "ok":
        return _fail(t, p, "本机 2026-09-11 全程不可达（curl exit 52 / 空应答）——港股第2档等于没有")
    try:
        d = json.loads(p["_body"]).get("data")
        kl = (d or {}).get("klines") or []
        t["bars"] = len(kl)
        t["first_date"] = kl[0].split(",")[0] if kl else None
        t["last_date"] = kl[-1].split(",")[0] if kl else None
    except Exception as exc:  # noqa: BLE001
        return _fail(t, dict(p, failure_class="body_shape", detail=f"结构不符（{scrub(exc)[:160]}）"))
    t["ok"] = t["bars"] >= 252
    t["degraded"] = not t["ok"]
    t["verdict"] = "可用" if t["ok"] else "可疑（根数不足）"
    return t


def probe_fred(series="DGS10"):
    """宏观第 2 档。注意：FRED 绝不能带浏览器 UA。"""
    t = _tier("macro.2", "fred.stlouisfed.org", "宏观利率 第2档（^TNX / ^VIX 回退）",
              "滞后一个交易日——阈值不可跨档沿用")
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}"
    t["url"] = url
    # 刻意不带 UA：Chrome UA 会让 FRED 吊死 25~30 秒。requests 默认 UA 是可用的。
    p = http_probe(url, HEADERS_NONE, want_json=False)
    t["transport"] = p
    if p["failure_class"] != "ok":
        return _fail(t, p)
    lines = [l for l in p["_body"].strip().splitlines() if l.strip()]
    vals = []
    for line in lines[1:]:
        parts = line.split(",")
        if len(parts) >= 2 and parts[1].strip() not in ("", "."):
            vals.append((parts[0], parts[1]))
    t["bars"] = len(vals)
    t["first_date"] = vals[0][0] if vals else None
    t["last_date"] = vals[-1][0] if vals else None
    t["checks"]["has_values"] = bool(vals)
    t["ok"] = bool(vals)
    t["degraded"] = not t["ok"]
    t["verdict"] = "可用" if t["ok"] else "不可用"
    t["notes"].append("FRED 假日为空单元格，空 != 0（实测 DGS10 2026-09-07 为空）")
    return t


# ---------------------------------------------------------------------------
# Yahoo 专项诊断 —— 本脚本存在的首要理由
# ---------------------------------------------------------------------------
def probe_yahoo():
    """
    三个子探测，用来把「限流」和「其实不是限流」分开：
      a) chart API + 浏览器 UA      -> 基准
      b) chart API + requests 默认 UA -> UA 是不是关键
      c) yfinance 批量下载           -> 单只可用而批量失败 = 改逐只即可
    """
    res = {"subprobes": {}, "verdict": None, "fixable_at_transport": None, "reason": None}

    a = http_probe("https://query1.finance.yahoo.com/v8/finance/chart/NVDA?range=1mo&interval=1d",
                   HEADERS_BROWSER)
    res["subprobes"]["chart_browser_ua"] = {k: v for k, v in a.items() if k != "_body"}
    time.sleep(1.5)

    b = http_probe("https://query1.finance.yahoo.com/v8/finance/chart/NVDA?range=1mo&interval=1d",
                   HEADERS_NONE)
    res["subprobes"]["chart_default_ua"] = {k: v for k, v in b.items() if k != "_body"}
    time.sleep(1.5)

    c = {"available": False, "ok": None, "detail": "yfinance 未安装（不影响其余档位的结论）"}
    try:
        import logging

        import yfinance as yf
        logging.getLogger("yfinance").setLevel(logging.CRITICAL)
        c["available"] = True
        t0 = time.time()
        try:
            raw = yf.download(["NVDA", "SMH", "AMD"], period="1mo", interval="1d",
                              progress=False, auto_adjust=False, group_by="ticker", threads=True)
            c["elapsed_ms"] = round((time.time() - t0) * 1000, 1)
            c["rows"] = 0 if raw is None else len(raw)
            c["ok"] = bool(raw is not None and len(raw) > 0)
            c["detail"] = None if c["ok"] else "yf.download 返回空——这正是 technicals.py exit 3 的那一支"
        except Exception as exc:  # noqa: BLE001
            c["elapsed_ms"] = round((time.time() - t0) * 1000, 1)
            c["ok"] = False
            c["detail"] = f"{type(exc).__name__}: {scrub(exc)[:200]}"
    except ImportError:
        pass
    res["subprobes"]["yfinance_batch"] = c

    a_ok = a["failure_class"] == "ok"
    b_ok = b["failure_class"] == "ok"
    c_ok = c.get("ok")

    # 诊断顺序要紧：先排除「根本不是限流」的成因，再谈换源。
    if a["failure_class"] == "tls_handshake":
        res["verdict"] = "TLS 握手失败——不是限流"
        res["fixable_at_transport"] = True
        res["reason"] = ("装 certifi / 配 CA bundle 即可。换数据源解决不了这个问题，"
                         "而且其他 https 源大概率同样失败。")
    elif a["failure_class"] == "dns":
        res["verdict"] = "DNS 解析失败——不是限流"
        res["fixable_at_transport"] = True
        res["reason"] = "出口网络/代理配置问题。所有外部源都会一起失败，先修网络。"
    elif a_ok and c_ok:
        res["verdict"] = "本次 Yahoo 正常——**没有复现故障**"
        res["fixable_at_transport"] = None
        res["reason"] = ("本次探测没有复现 429，所以本次结果不能证明回退链在故障时可用。"
                         "请在报告真的出现「三桶全空」之后、尽快在同一台机器上重跑本脚本。")
    elif a_ok and not b_ok:
        res["verdict"] = "UA 是关键：带浏览器 UA 可用，裸 UA 被限"
        res["fixable_at_transport"] = True
        res["reason"] = ("修 technicals.py 的 Yahoo 会话 UA 即可，无需换上游。"
                         "先确认 yf.download 这条路径是否真的带了浏览器 UA。")
    elif a_ok and c_ok is False:
        res["verdict"] = "单只可用、批量失败"
        res["fixable_at_transport"] = True
        res["reason"] = ("chart API 单只请求正常而 yf.download 批量失败——"
                         "改逐只 + 限速/退避即可，不必换上游。")
    elif not a_ok and not b_ok:
        cls = a["failure_class"]
        if cls in ("http_429", "http_403", "bot_interstitial"):
            res["verdict"] = f"边缘层 IP 级封禁（{cls}）——换 UA 无用"
            res["fixable_at_transport"] = False
            res["reason"] = "必须换上游。这正是回退链要解决的情况。"
        else:
            res["verdict"] = f"Yahoo 不可达（{cls}）"
            res["fixable_at_transport"] = False
            res["reason"] = a.get("detail") or "见 subprobes"
    else:
        res["verdict"] = "结果混合，需人工判读 subprobes"
        res["fixable_at_transport"] = None
        res["reason"] = "各子探测结论不一致，不自动下判断。"
    return res


# ---------------------------------------------------------------------------
# 本地检查（不走网络）：姊妹技能装没装。这是 technicals.py 另一条已知的死法。
# ---------------------------------------------------------------------------
def probe_sibling_skill():
    out = {"name": "ai-industry-weekly 姊妹技能", "ok": None, "detail": None, "universe_count": None}
    try:
        sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
        from _weekly import locate_weekly_skill_or_exit  # noqa: F401
        out["detail"] = "_weekly.py 可导入"
    except Exception as exc:  # noqa: BLE001
        out["ok"] = False
        out["detail"] = f"_weekly.py 不可导入：{scrub(exc)[:160]}"
        return out
    try:
        from pathlib import Path
        here = Path(__file__).resolve().parent
        cands = [here.parent.parent / "ai-industry-weekly" / "assets" / "universe.json"]
        import os
        if os.environ.get("AI_INDUSTRY_WEEKLY_DIR"):
            cands.insert(0, Path(os.environ["AI_INDUSTRY_WEEKLY_DIR"]) / "assets" / "universe.json")
        for c in cands:
            if c.exists():
                out["universe_count"] = len(json.loads(c.read_text())["tickers"])
                out["ok"] = True
                out["detail"] = f"universe.json 可读，{out['universe_count']} 只标的"
                return out
        out["ok"] = False
        out["detail"] = "找不到 ai-industry-weekly/assets/universe.json"
    except Exception as exc:  # noqa: BLE001
        out["ok"] = False
        out["detail"] = f"读取 universe.json 失败：{scrub(exc)[:160]}"
    return out


# ---------------------------------------------------------------------------
# 全标的覆盖率（--coverage）
# ---------------------------------------------------------------------------
def probe_coverage(sibling):
    """
    全标的覆盖率：universe.json 里**每一只**标的都实测一次它所属市场的第 1 档。

    覆盖范围（默认探测只各抽 1 只，这里是全量）：
        美股 41 只 -> us.1 stockanalysis
        韩股  2 只 -> kr.1 Naver（默认探测只测 005930，000660 在这里才被覆盖）
        港股  3 只 -> hk.1 腾讯（默认探测只测 00700）
      外加 us.2 的 assetclass 分支实测：api.nasdaq.com 对股票要 assetclass=stocks、
      对 ETF 要 assetclass=etf，走错分支会 HTTP 200 返回空 rows。池子里有 5 只 ETF，
      默认探测只用 NVDA 走了 stocks 分支，etf 分支从未被验证过。

    请求数约 51（美股 41 + 韩股 2 + 港股 3 + us.2 抽样 5）。
    腾讯 kline 只多打 3 个请求，远低于会触发 30 分钟封禁的探测强度。
    """
    from concurrent.futures import ThreadPoolExecutor
    from pathlib import Path
    import os

    cands = []
    if os.environ.get("AI_INDUSTRY_WEEKLY_DIR"):
        cands.append(Path(os.environ["AI_INDUSTRY_WEEKLY_DIR"]) / "assets" / "universe.json")
    cands.append(Path(__file__).resolve().parent.parent.parent / "ai-industry-weekly" / "assets" / "universe.json")
    uni = next((c for c in cands if c.exists()), None)
    if uni is None:
        return {"ok": False, "detail": "找不到 universe.json，跳过覆盖率探测",
                "us": None, "kr": None, "hk": None, "us2_assetclass": None}

    entries = json.loads(uni.read_text())["tickers"]
    tickers = [e["ticker"] for e in entries]
    etf_set = {e["ticker"] for e in entries if e.get("etf")}
    us = [t for t in tickers if not t.endswith((".HK", ".KS"))]
    kr = [t for t in tickers if t.endswith(".KS")]
    hk = [t for t in tickers if t.endswith(".HK")]

    # ---- 美股：us.1 全量
    def one_us(tk):
        url = f"https://stockanalysis.com/api/symbol/s/{tk.lower()}/history?range=5Y&period=Daily"
        p = http_probe(url, HEADERS_NONE, timeout=25)
        if p["failure_class"] != "ok":
            return tk, None, p["failure_class"]
        try:
            d = json.loads(p["_body"])["data"]
            rows = d.get("data") if isinstance(d, dict) else d
            return tk, len(rows), None
        except Exception:  # noqa: BLE001
            return tk, None, "body_shape"

    with ThreadPoolExecutor(max_workers=8) as ex:
        us_res = list(ex.map(one_us, us))
    us_ok = {t: n for t, n, e in us_res if n is not None}
    us_bad = {t: e for t, n, e in us_res if n is None}
    # 恰好 252 根是 range 被静默降级的特征（5Y 对上市够久的票应给 ~1255 根）。
    # 它和「上市太新」长得不一样：真新票是 24 / 61 / 110 这种零散数字。
    us_sus = {t: n for t, n in us_ok.items() if n == 252}
    us_thin = {t: n for t, n in us_ok.items() if n < 252}

    # ---- 韩股：kr.1 全量（默认探测漏掉的那只在这里）
    today = dt.date.today()
    start = today - dt.timedelta(days=900)

    def one_kr(tk):
        sym = tk.replace(".KS", "")
        url = (f"https://api.finance.naver.com/siseJson.naver?symbol={sym}"
               f"&requestType=1&startTime={start:%Y%m%d}&endTime={today:%Y%m%d}&timeframe=day")
        p = http_probe(url, HEADERS_NONE, timeout=25)
        if p["failure_class"] != "ok":
            return tk, None, p["failure_class"]
        body = p["_body"].strip()
        if len(body) < 120:
            return tk, None, "empty_header_only"
        try:
            rows = ast.literal_eval(body)
            good = [r for r in rows[1:] if r and str(r[0]).isdigit()
                    and not (r[1] == 0 and r[2] == 0 and r[3] == 0)]
            return tk, len(good), None
        except Exception:  # noqa: BLE001
            return tk, None, "body_shape"

    kr_res = [one_kr(t) for t in kr]
    kr_ok = {t: n for t, n, e in kr_res if n is not None}
    kr_bad = {t: e for t, n, e in kr_res if n is None}

    # ---- 港股：hk.1 全量（腾讯，串行且只有 3 个请求，避开封禁阈值）
    def one_hk(tk):
        code = tk.replace(".HK", "").zfill(5)
        url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=hk{code},day,,,300,"
        p = http_probe(url, HEADERS_BROWSER, timeout=25)
        if p["failure_class"] != "ok":
            return tk, None, p["failure_class"]
        try:
            d = json.loads(p["_body"])["data"][f"hk{code}"]
            kl = d.get("day") or d.get("qfqday") or []
            return tk, len(kl), None
        except Exception:  # noqa: BLE001
            return tk, None, "body_shape"

    hk_res = [one_hk(t) for t in hk]
    hk_ok = {t: n for t, n, e in hk_res if n is not None}
    hk_bad = {t: e for t, n, e in hk_res if n is None}

    # ---- us.2 的 assetclass 分支：ETF 走 etf、股票走 stocks，走错会 200 + 空 rows
    def one_us2(tk):
        ac = "etf" if tk in etf_set else "stocks"
        url = (f"https://api.nasdaq.com/api/quote/{tk}/historical"
               f"?assetclass={ac}&fromdate={start:%Y-%m-%d}&todate={today:%Y-%m-%d}&limit=400")
        p = http_probe(url, HEADERS_EMPTY_UA, timeout=30)
        if p["failure_class"] != "ok":
            return tk, ac, None, p["failure_class"]
        try:
            rows = (json.loads(p["_body"]).get("data") or {}).get("tradesTable", {}).get("rows") or []
            return tk, ac, len(rows), None if rows else "empty_rows"
        except Exception:  # noqa: BLE001
            return tk, ac, None, "body_shape"

    us2_sample = sorted(etf_set) + ["NVDA"]
    with ThreadPoolExecutor(max_workers=4) as ex:
        us2_res = list(ex.map(one_us2, us2_sample))
    us2 = {t: {"assetclass": ac, "bars": n, "error": e} for t, ac, n, e in us2_res}

    all_bad = bool(us_bad or kr_bad or hk_bad or us_sus)
    return {
        "ok": not all_bad,
        "detail": (f"美股 {len(us_ok)}/{len(us)}｜韩股 {len(kr_ok)}/{len(kr)}｜"
                   f"港股 {len(hk_ok)}/{len(hk)} 可取数"),
        "us": {"bars": us_ok, "unavailable": us_bad,
               "insufficient_history": us_thin, "suspect_range_degraded": us_sus},
        "kr": {"bars": kr_ok, "unavailable": kr_bad},
        "hk": {"bars": hk_ok, "unavailable": hk_bad},
        "us2_assetclass": us2,
    }


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------
PROHIBITIONS = [
    "腾讯 fqkline 的 day 键（美股）是**完全原始价**，拆股未还原；yfinance auto_adjust=False "
    "是拆股已还原。二者不同口径，腾讯美股日线**不得**作为 yfinance 的替代——"
    "实测 AVGO / ANET 的 T1 判定会由 False 翻成 True。",
    "m.stock.naver.com/api/stock/{code}/price 拆股未还原，**不得**作为 Naver 档的回退"
    "（与 siseJson 在 4740 根里有 2691 根不一致）。",
    "腾讯 us.VIX **不得**用于 VIX：返回 HTTP 200 + code:0 + 非空数组，但数据停在数月前。",
    "stockanalysis 的成交量比 yfinance 系统性偏低（AVGO −11.1%、NVDA −4.6%），"
    "量比（vol_ratio）**不可跨档比较**——要么从 us.2 单独取量且另起一行，要么记 N/A。",
    "stockanalysis 的 range 只有 5Y / 10Y 被遵守，其余值静默返回恰好 252 根：必须断言根数。",
    "FRED 档滞后一个交易日，^TNX 的 RATE_UP_1D_BP / RATE_DN_1D_BP 阈值**不可**直接沿用。",
    "取数失败**不得**落进 insufficient_history 桶——那读起来是「上市太新」而真相是「取不到」。",
]


def build_summary(probes, yahoo):
    by_tier = {p["tier"]: p for p in probes}

    def chain(name, tiers):
        avail = [t for t in tiers if by_tier.get(t, {}).get("ok")]
        return {
            "order": tiers,
            "first_viable": avail[0] if avail else None,
            "viable": bool(avail),
            "viable_tiers": avail,
            "dead_tiers": [t for t in tiers if t in by_tier and not by_tier[t].get("ok")],
        }

    yahoo_ok = yahoo["subprobes"]["chart_browser_ua"]["failure_class"] == "ok"
    chains = {
        "us": chain("美股", ["us.1", "us.2"]),
        "kr": chain("韩股", ["kr.1"]),
        "hk": chain("港股", ["hk.1", "hk.2"]),
        "macro": chain("宏观", ["macro.2"]),
    }
    for c in chains.values():
        c["yahoo_tier0_ok"] = yahoo_ok
    return chains


def render(payload, coverage):
    p = payload
    print(f"📡 数据源可达性探针 · {p['generated_at']}")
    print(f"   requests={p['environment']['requests']} yfinance={p['environment']['yfinance']} "
          f"python={p['environment']['python']}")
    print()

    y = p["yahoo_diagnosis"]
    print("── Yahoo 专项诊断 " + "─" * 40)
    print(f"   判定：{y['verdict']}")
    fx = {True: "是（改传输层即可，不必换源）", False: "否（必须换上游）", None: "无法判定"}[y["fixable_at_transport"]]
    print(f"   传输层可修：{fx}")
    print(f"   说明：{y['reason']}")
    for k, v in y["subprobes"].items():
        if k == "yfinance_batch":
            mark = {True: "✅", False: "❌", None: "⚪️"}[v.get("ok")]
            desc = v.get("detail") or f"rows={v.get('rows')} {v.get('elapsed_ms')}ms"
            print(f"     {mark} {k}: {desc}")
        else:
            mark = "✅" if v["failure_class"] == "ok" else "❌"
            print(f"     {mark} {k}: {v['failure_class']} "
                  f"http={v['http_status']} {v['elapsed_ms']}ms {v.get('detail') or ''}")
    print()

    print("── 各档位 " + "─" * 47)
    hdr = f"{'档位':<18} {'来源':<34} {'判定':<20} {'根数':>6}  区间"
    print(hdr)
    for t in p["probes"]:
        mark = {"可用": "✅", "不可用": "❌"}.get(t["verdict"], "⚠️ ")
        rng = f"{t['first_date'] or '-'} → {t['last_date'] or '-'}"
        print(f"{t['tier']:<18} {t['name']:<34} {mark}{t['verdict'] or '-':<18} "
              f"{t['bars'] if t['bars'] is not None else '-':>6}  {rng}")
        for r in t["degraded_reasons"]:
            print(f"         ⚠ {r}")
        for n in t["notes"]:
            print(f"         · {n}")
    print()

    print("── 回退链结论 " + "─" * 43)
    for mkt, c in p["chains"].items():
        mark = "✅" if c["viable"] else "❌"
        print(f"   {mark} {mkt.upper():<6} 首个可用档：{c['first_viable'] or '（无）'}   "
              f"顺序 {' → '.join(c['order'])}   Yahoo(第0档)={'可用' if c['yahoo_tier0_ok'] else '不可用'}")
        if c["dead_tiers"]:
            print(f"          不可用：{', '.join(c['dead_tiers'])}")
    print()

    sib = p["sibling_skill"]
    print(f"── 本地检查 {'─' * 46}")
    print(f"   {'✅' if sib['ok'] else '❌'} {sib['name']}：{sib['detail']}")
    print()

    if coverage:
        print("── 全标的覆盖率（--coverage）" + "─" * 28)
        print(f"   {coverage['detail']}")
        for mkt, label in (("us", "美股 us.1"), ("kr", "韩股 kr.1"), ("hk", "港股 hk.1")):
            blk = coverage.get(mkt)
            if not blk:
                continue
            if blk.get("unavailable"):
                print(f"   ❌ {label} 取不到："
                      f"{', '.join(f'{k}({v})' for k, v in blk['unavailable'].items())}")
            if blk.get("suspect_range_degraded"):
                print(f"   ⛔ {label} **疑似 range 静默降级**（恰好 252 根，应为 ~1255）："
                      f"{', '.join(blk['suspect_range_degraded'])}")
            if blk.get("insufficient_history"):
                print(f"   ⚠ {label} 根数不足 252（真·上市太新，各源皆然）："
                      f"{', '.join(f'{k}={v}' for k, v in blk['insufficient_history'].items())}")
        u2 = coverage.get("us2_assetclass") or {}
        bad2 = {k: v for k, v in u2.items() if v.get("error")}
        if bad2:
            items = ", ".join(f"{k}[{v['assetclass']}]({v['error']})" for k, v in bad2.items())
            print(f"   ❌ us.2 assetclass 分支失败：{items}")
        else:
            print("   ✅ us.2 assetclass 分支全部可用（stocks / etf 各自命中）")
        print()

    print("── 禁止事项（口径规则，与 --json 的 prohibitions 同文）" + "─" * 8)
    for pr in p["prohibitions"]:
        print(f"   ⛔ {pr}")
    print()
    if p["degraded"]:
        print("⚠ 本次探测存在降级项：")
        for r in p["degraded_reasons"]:
            print(f"   - {r}")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="数据源可达性探针：在目标机器上实测每个回退档位，并把 Yahoo 的各种失败成因区分开")
    ap.add_argument("--json", nargs="?", const="-", metavar="OUT",
                    help="输出 JSON；给路径则写文件，不给则打到 stdout")
    ap.add_argument("--quiet", action="store_true",
                    help="不渲染人读表格。**不会**关闭任何告警——告警走 stderr")
    ap.add_argument("--strict", action="store_true",
                    help="美股或韩股无任何可用档位时 exit 3（给调度层用）")
    ap.add_argument("--coverage", action="store_true",
                    help="额外实测 universe.json 全部美股的覆盖率（请求数明显增加）")
    args = ap.parse_args()

    try:
        import requests  # noqa: F401
    except ImportError as exc:
        err(f"错误：缺少依赖 requests（{scrub(exc)}）。请先 `pip install requests`。")
        return 2

    import platform
    try:
        import requests as _rq
        rq_ver = _rq.__version__
    except Exception:  # noqa: BLE001
        rq_ver = "?"
    try:
        import yfinance as _yf
        yf_ver = getattr(_yf, "__version__", "已安装")
    except ImportError:
        yf_ver = "未安装"

    err(f"[{SCRIPT_NAME}] 开始探测（约 14 个请求，刻意压到最小；勿循环运行）…")

    yahoo = probe_yahoo()
    probes = [
        probe_stockanalysis(),
        probe_nasdaq(),
        probe_naver(),
        probe_tencent_quote(),
        probe_tencent_kline_hk(),
        probe_eastmoney(),
        probe_fred(),
    ]
    # 探测细节里不留 body 原文（可能很大，也可能含无关内容）
    for t in probes:
        if isinstance(t.get("transport"), dict):
            t["transport"] = {k: v for k, v in t["transport"].items() if k != "_body"}

    sibling = probe_sibling_skill()
    coverage = probe_coverage(sibling) if args.coverage else None

    chains = build_summary(probes, yahoo)
    degraded_reasons = []
    for t in probes:
        for r in t["degraded_reasons"]:
            degraded_reasons.append(f"[{t['tier']}] {r}")
    if not sibling.get("ok"):
        degraded_reasons.append(f"[本地] {sibling['detail']}")
    if yahoo["fixable_at_transport"] is None and "没有复现" in (yahoo["verdict"] or ""):
        degraded_reasons.append(
            "[Yahoo] 本次未复现故障——本次结论不能证明故障发生时回退链可用")

    payload = {
        # ok 不是字面量：任一自检失败即 false
        "ok": all(t["ok"] for t in probes if t["tier"] in ("us.1", "kr.1")) and bool(sibling.get("ok")),
        "script": SCRIPT_NAME,
        "generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "environment": {"python": platform.python_version(), "requests": rq_ver, "yfinance": yf_ver},
        "yahoo_diagnosis": yahoo,
        "probes": probes,
        "chains": chains,
        "sibling_skill": sibling,
        "coverage": coverage,
        "prohibitions": PROHIBITIONS,
        "degraded": bool(degraded_reasons),
        "degraded_reasons": degraded_reasons,
    }

    if not args.quiet:
        render(payload, coverage)
    for r in degraded_reasons:
        err(f"⚠ {r}")

    if args.json:
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        if args.json == "-":
            print(text)
        else:
            with open(args.json, "w", encoding="utf-8") as fh:
                fh.write(text + "\n")
            err(f"[{SCRIPT_NAME}] JSON 已写入 {args.json}")

    if args.strict and not (chains["us"]["viable"] and chains["kr"]["viable"]):
        err("错误：美股或韩股没有任何可用档位——取数失败（exit 3）。")
        return 3
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        err("\n已中断。")
        sys.exit(130)
