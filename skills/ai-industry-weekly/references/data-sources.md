# 取数口径与数据源

> 本文件自 `AI产业表周更.md` 第一步**逐字迁移**，未改写。
>
> **编者注**：标的清单现由 `assets/universe.json` 提供（`tickers` 顺序即产业表行序），并由
> `scripts/fetch_fundamentals.py` 读取；下文正文内联的 `T = [...]` 列表仅作历史参考，
> **不是**真相源，增减标的请改 `assets/universe.json`。
>
> **编者注**：下文的 `pip install -q yfinance pandas` 是原文写法。本技能实际依赖为
> **`yfinance` + `requests`**（`requests.Session` 是绕过代理下 curl_cffi TLS 失败的硬约束）；
> `pandas` 两个脚本都未 import，可不装。脚本不会自动安装 yfinance，缺失时会退出并打印安装提示。
>
> **编者注**：文中的 `$SKILL_DIR` 指**本技能根目录**（`scripts/` 与本 `references/` 同级），
> 由 SKILL.md 第零步定出。两个脚本内部用 `__file__` 相对定位 `assets/`，找数据文件与 cwd 无关；
> 但**调用命令本身**必须给出绝对路径，故一律写成 `python3 "$SKILL_DIR/scripts/xxx.py" ...`。

用 Bash 执行（环境可联网）：先 pip install -q yfinance pandas（如遇 PEP668 报错，加 --break-system-packages），再运行以下脚本拉取 46 标的最新基本面。
注意：本执行环境出站走代理，yfinance 默认的 curl_cffi 引擎会 TLS 握手失败、导致 `.info` 全部为 null；故改用 requests.Session（实测可正常取数）并加 3 次重试。

```python
import yfinance as yf, requests
s = requests.Session()
s.headers["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
T = ["NVDA","TSM","INTC","GFS","MU","SNDK","TTMI","MRAAY","ADI","TXN","AMD","ARM","AVGO","MRVL","GOOGL","AMZN","COHR","LITE","GLW","NOK","AXTI","ALAB","AAOI","TSLA","ANET","VRT","BE","DELL","RKLB","DRAM","LYTE","NCLD","SMH","SOXX","0700.HK","1810.HK","SPCX","000660.KS","005930.KS","CSCO","MSFT","ASML","BABA","META","NET","0941.HK"]
F = {"longName":"name","marketCap":"mktcap","trailingPE":"PE","forwardPE":"fwdPE","priceToSalesTrailing12Months":"PS","priceToBook":"PB","enterpriseToEbitda":"EVE","trailingPegRatio":"PEG","grossMargins":"gm","operatingMargins":"om","profitMargins":"nm","returnOnEquity":"ROE","returnOnAssets":"ROA","revenueGrowth":"revG","earningsGrowth":"epsG","freeCashflow":"FCF","totalCash":"cash","totalDebt":"debt","fiftyTwoWeekHigh":"hi","52WeekChange":"chg52","SandP52WeekChange":"spx","targetMeanPrice":"tgt","recommendationKey":"rec"}
for t in T:
    i = {}
    for _ in range(3):
        try:
            i = yf.Ticker(t, session=s).info
            if i and (i.get("currentPrice") or i.get("regularMarketPrice") or len(i) > 20): break
        except Exception:
            i = {}
    p = i.get("currentPrice") or i.get("regularMarketPrice") or i.get("previousClose")
    fh = i.get("fiftyTwoWeekHigh")
    frm = round((p/fh-1)*100,1) if (p and fh) else None
    row = {"price": p, "fromHi%": frm}
    row.update({v: i.get(k) for k, v in F.items()})
    print(t, row)
```

数据缺失记 N/A，不估算。若偶尔抓空，重跑一次或 pip install -U yfinance。

**港股三只（0700.HK / 1810.HK / 0941.HK）的价格与 52 周高/低不取 yfinance，改用专用脚本（原始未复权、实时）**：
```bash
# $SKILL_DIR = 本技能根目录（references/ 的上一层），见 SKILL.md 第零步；用绝对路径调用，任意 cwd 均可
python3 "$SKILL_DIR/scripts/hk_quote.py" 0700.HK 1810.HK 0941.HK --json
```
（`scripts/fetch_fundamentals.py` 会对 `universe.json` 里标了 `hk_quote: true` 的标的自动调用本脚本，通常无须手跑；手动核对时用上面这行。）
- **为什么**：Yahoo 港股报价延迟 ≥15 分钟、WebSearch 常返回缓存旧页 → 价格过时；yfinance `history()` 默认股息复权，0700.HK 52周高被调成 675.1（原始 683.0）、0941.HK 被调成 86.3（原始 90.6）→ 本表 price / hi / fromHi% 随之失真；腾讯/新浪实时行情的 52 周高字段又是前复权口径，多源混用必不一致。
- **口径**：脚本输出的 `last / chg_pct / hi52 / lo52 / pct_from_hi52` 直接用于本行 price / fromHi% 字段（覆盖上面 yfinance 脚本的同名字段）；财务字段（PE/利润率/ROE/现金流等）仍用 yfinance（0700.HK / 1810.HK 财务字段为港币计价，ADR 交叉核对可用 TCEHY / XIACY）。
- **校验**：脚本 `market_status` 应为「已收盘(收盘价)」且 `quote_time` 为最近一个港股交易日的 16:08 前后（HKT），否则该价为盘中/延迟价，不得当收盘价用；`stale=true` 时重跑或记 N/A。SK海力士用 000660.KS（KRW 计价、韩股主挂牌；因 SKHY 在 yfinance 无行情数据、SKHYY 已 404/下市，故取韩股主挂牌取数）。三星电子用 005930.KS（KRW 计价、韩股主挂牌；ADR SSNLF 为场外粉单、报价与数据品质差不可用；yfinance 常抓不到 trailingPE / priceToBook / trailingEps → 一律记 N/A 不估算，改以 forwardPE + PEG + 营益率/ROE 判断）。SPCX = Space Exploration Technologies Corp.（SpaceX），现于 NasdaqGS 上市、USD 计价。CSCO = Cisco Systems（USD 计价）。MSFT = Microsoft、META = Meta Platforms（均 USD、干净大盘）；ASML = ASML Holding（USD ADR，欧洲 EUV/High-NA 光刻独占）；BABA = Alibaba（USD ADR、中国超大厂，财报 RMB 计价 → PS/营益率等比率可能失真，按业务+PE 判）；NET = Cloudflare（USD，边缘网络/CDN/Zero Trust 安全 + Workers AI 边缘推理平台）；0941.HK = 中国移动（HKD 计价，ADR CHL 因制裁 2021 摘牌 → 取港股主挂牌 0941.HK，或 A 股 600941.SS/CNY 交叉核对；智算中心 AIDC + 移动云需求侧运营商）。
