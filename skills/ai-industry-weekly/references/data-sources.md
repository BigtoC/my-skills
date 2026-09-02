# 取数口径与数据源

> 本文件自 `AI产业表周更.md` 第一步**逐字迁移**，未改写。
>
> **编者注**：原文第一步内联的那段 Python（含硬编码的 `T = [...]` 清单）已**整段抽出**为
> `scripts/fetch_fundamentals.py`，取数逻辑逐字沿用（UA / `requests.Session` / 3 次重试 /
> 成功判定 / 23 个字段映射 `F` / price 三级回退 / `fromHi%` 公式），本文只留口径说明。
> 标的清单与行序改由 `assets/universe.json` 的 `tickers` 提供、由脚本读取——它是唯一真相源，
> 增减标的只改该文件。内联的 T 列表连同代码一并删除：它虽与 `universe.json` 同为 46 个代码，
> **行序早已不一致**（T 的第 3 个是 INTC，清单的第 3 行是 ASML），留着只会让人抓错顺序。
>
> **编者注**：原文写的是 `pip install -q yfinance pandas`。本技能实际依赖为
> **`yfinance` + `requests`**（`requests.Session` 是绕过代理下 curl_cffi TLS 失败的硬约束）；
> `pandas` 两个脚本都未 import，可不装。脚本不会自动安装 yfinance，缺失时会退出并打印安装提示。
>
> **编者注**：文中的 `$SKILL_DIR` 指**本技能根目录**（`scripts/` 与本 `references/` 同级），
> 由 SKILL.md 第零步定出。两个脚本内部用 `__file__` 相对定位 `assets/`，找数据文件与 cwd 无关；
> 但**调用命令本身**必须给出绝对路径，故一律写成 `python3 "$SKILL_DIR/scripts/xxx.py" ...`。

用 Bash 执行（环境可联网）：先 `pip install -q yfinance requests`（如遇 PEP668 报错，加 `--break-system-packages`），再跑取数脚本拉取 `universe.json` 全清单的最新基本面。

```bash
# $SKILL_DIR = 本技能根目录（references/ 的上一层），见 SKILL.md 第零步；用绝对路径调用，任意 cwd 均可
python3 "$SKILL_DIR/scripts/fetch_fundamentals.py" --json /tmp/fundamentals.json
python3 "$SKILL_DIR/scripts/fetch_fundamentals.py" --tickers NVDA,TSM   # 只重跑抓空的那几只
```

注意：本执行环境出站走代理，yfinance 默认的 curl_cffi 引擎会 TLS 握手失败、导致 `.info` 全部为 null；故改用 requests.Session（实测可正常取数）并加 3 次重试。**这两点已固化在脚本里**，勿在脚本外另写 yfinance 调用。

脚本内已固化、不得改动的取数口径（对应 `fetch_fundamentals.py` 顶部 docstring 的「硬约束」）：
- **清单与行序**：读 `assets/universe.json` 的 `tickers`（按 `order` 排序）；标的数不硬编码，脚本会打印「清单共 N 个」。
- **引擎**：`requests.Session` + 原表那串 Chrome UA（换 UA 曾致 Yahoo 限流返回空 info）；3 次重试，成功判定沿用 `i and (currentPrice or regularMarketPrice or len(i) > 20)`。
- **字段映射 `F`**：原表 23 个字段逐字沿用（`longName→name`、`marketCap→mktcap`、`trailingPE→PE`、`priceToSalesTrailing12Months→PS`、…、`recommendationKey→rec`），不增不减不改名。
- **`price` 三级回退**：`currentPrice → regularMarketPrice → previousClose`。
- **`fromHi%`** `= round((price / fiftyTwoWeekHigh - 1) * 100, 1)`，缺任一端记 N/A。
- **港股**（`universe.json` 里标了 `hk_quote: true` 的行）的 price / hi / fromHi% 由 `hk_quote.py` 覆盖，见下节。

数据缺失记 N/A，不估算。若偶尔抓空，重跑一次（用 `--tickers` 只补那几只）或 pip install -U yfinance。

**港股三只（0700.HK / 1810.HK / 0941.HK）的价格与 52 周高/低不取 yfinance，改用专用脚本（原始未复权、实时）**：
```bash
# $SKILL_DIR = 本技能根目录（references/ 的上一层），见 SKILL.md 第零步；用绝对路径调用，任意 cwd 均可
python3 "$SKILL_DIR/scripts/hk_quote.py" 0700.HK 1810.HK 0941.HK --json
```
（`scripts/fetch_fundamentals.py` 会对 `universe.json` 里标了 `hk_quote: true` 的标的自动调用本脚本，通常无须手跑；手动核对时用上面这行。）
- **为什么**：Yahoo 港股报价延迟 ≥15 分钟、WebSearch 常返回缓存旧页 → 价格过时；yfinance `history()` 默认股息复权，0700.HK 52周高被调成 675.1（原始 683.0）、0941.HK 被调成 86.3（原始 90.6）→ 本表 price / hi / fromHi% 随之失真；腾讯/新浪实时行情的 52 周高字段又是前复权口径，多源混用必不一致。
- **口径**：脚本输出的 `last / chg_pct / hi52 / lo52 / pct_from_hi52` 直接用于本行 price / fromHi% 字段（覆盖上面 yfinance 脚本的同名字段）；财务字段（PE/利润率/ROE/现金流等）仍用 yfinance（0700.HK / 1810.HK 财务字段为港币计价，ADR 交叉核对可用 TCEHY / XIACY）。
- **校验**：脚本 `market_status` 应为「已收盘(收盘价)」且 `quote_time` 为最近一个港股交易日的 16:08 前后（HKT），否则该价为盘中/延迟价，不得当收盘价用；`stale=true` 时重跑或记 N/A。SK海力士用 000660.KS（KRW 计价、韩股主挂牌；因 SKHY 在 yfinance 无行情数据、SKHYY 已 404/下市，故取韩股主挂牌取数）。三星电子用 005930.KS（KRW 计价、韩股主挂牌；ADR SSNLF 为场外粉单、报价与数据品质差不可用；yfinance 常抓不到 trailingPE / priceToBook / trailingEps → 一律记 N/A 不估算，改以 forwardPE + PEG + 营益率/ROE 判断）。SPCX = Space Exploration Technologies Corp.（SpaceX），现于 NasdaqGS 上市、USD 计价。CSCO = Cisco Systems（USD 计价）。MSFT = Microsoft、META = Meta Platforms（均 USD、干净大盘）；ASML = ASML Holding（USD ADR，欧洲 EUV/High-NA 光刻独占）；BABA = Alibaba（USD ADR、中国超大厂，财报 RMB 计价 → PS/营益率等比率可能失真，按业务+PE 判）；NET = Cloudflare（USD，边缘网络/CDN/Zero Trust 安全 + Workers AI 边缘推理平台）；0941.HK = 中国移动（HKD 计价，ADR CHL 因制裁 2021 摘牌 → 取港股主挂牌 0941.HK，或 A 股 600941.SS/CNY 交叉核对；智算中心 AIDC + 移动云需求侧运营商）。
