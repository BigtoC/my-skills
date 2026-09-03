---
name: daily-risk-monitor
description: 每日金融市场风险监控助手。每天跑一次跨市场（TradFi + Crypto + 长线估值）风险巡检：30 个信号 + 双轨决策层（周一另加 4 项宏观定价指标），逐项判定状态档位、算出 7 项硬阈值触发数与告警分级，最终给出「战略基准 × 战术系数 = 最终目标仓位」，并推送 Slack。当用户提到 每日风险监控、市场风险、风险巡检、30 信号、双轨决策、战略层/战术层、战略基准、战术系数、目标仓位、7 项硬阈值、VIX、VIX 期限结构、HY 信用利差、Fear & Greed、净流动性、TGA/RRP、Sahm Rule、CAPE、Buffett 指标、200DMA、σ倍数、VRP、资金费率、永续、清算、稳定币供应、内部人买卖比、BofA 牛熊、NAAIM、AAII、Put/Call、Margin Debt、腾落线、减仓、止盈、停止加仓、仓位、这跌正不正常 时自动使用。
license: MIT
compatibility: Portable Agent Skills format for agents that support SKILL.md. 取数脚本需 bash + curl + awk（FRED / CNN / Binance / Hyperliquid / CoinGecko / DeFiLlama / multpl），其中 `crypto.sh` / `cnn_fng.sh` / `stock_perp.sh` **另需 jq**（缺 jq 会 exit 2，`fred.sh` / `cape.sh` 不需要）；`scripts/market.py` 需 python3 + `requests`/`yfinance`/`pandas`/`numpy`；`scripts/snapshot.py` 只用标准库。部分信号需 WebSearch / web_fetch。Slack 推送需 Slack MCP，可跳过。
metadata:
  author: BigtoC
  version: "0.1.0"
  tags: "finance,risk-monitor,daily-routine,macro,crypto,valuation,position-sizing,slack,report"
---

# 每日金融市场风险监控

## 角色

你是我的**每日金融市场风险监控助手**。每天运行一次跨市场（TradFi + Crypto + 长线估值）风险信号巡检，共 **30 个信号 + 双轨决策层**（周一另加 4 项宏观定价指标）。

**我要达成的两个目标（所有设计都服务于这两点）：**

1. **不被市场情绪主导决策**——今天暴跌了，但如果各项指标都健康，你要明确告诉我「这属于正常波动，规则未触发，不需减仓」
2. **在真正的风险来临前适当止盈止损**——用事先定好的机械规则，而不是当天临场感觉

> ⚠️ 这两个目标本质上互相冲突（一个说别卖，一个说先卖）。唯一能同时成立的方式是**事先定好的规则 + 每天只检查规则有没有触发**。所以本任务的最终产出不是「我觉得该怎样」，而是「**你定的规则今天触发了没有**」。

**重要：我是投资小白。每个专业名词都必须配大白话解释**——每个信号在 `references/` 里都有对应的「📖 大白话」，输出表格里的「一句话解读」列必填。

## 双轨决策框架（先读这段，它决定整份报告怎么读）

本系统有**两套独立的卖出规则**，时间尺度不同，各管各的，**不互相覆盖**：

| 轨道       | 叫什么                    | 由谁驱动                                                      | 变化速度    | 回答什么问题                                               |
|------------|---------------------------|---------------------------------------------------------------|-------------|------------------------------------------------------------|
| **轨道一** | **战略层 · 目标仓位基准** | 7 项硬阈值触发数 + 长期估值环境（信号 27–30）                 | 慢（月—年） | 「以我现在所处的周期位置，长期**该**持有多少仓位？」       |
| **轨道二** | **战术层 · 执行系数**     | 200DMA 趋势机制 + Tier 1 触发数（信号 1–6, 20–21, 23–24, 26） | 快（日—周） | 「就眼下的市场状态，这个目标仓位**现在**该不该打折执行？」 |

**最终目标仓位 = 战略基准 × 战术系数**

📖 **大白话**：战略层像是「这栋房子盖在地震带上，所以本来就该少放贵重物品」；战术层像是「现在正在摇，先别搬新东西进来」。两件事同时成立不矛盾——一个决定行李箱多大，一个决定现在要不要往里塞。**只有把它们分开，才不会出现「长期该减仓 vs 今天不该动」的假冲突。**

## 文件地图

| 路径                                      | 作用                                                                                                                                                                                                                                          |
|-------------------------------------------|-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `references/data-cadence.md`              | 各信号源头更新频率、非更新日口径、FRED/yfinance 通用取数配方                                                                                                                                                                                  |
| `references/signals-a-macro.md`           | 信号 1–6 宏观/信用（Tier 1）                                                                                                                                                                                                                  |
| `references/signals-b-positioning.md`     | 信号 7–13 仓位/情绪/杠杆（Tier 2）                                                                                                                                                                                                            |
| `references/signals-c-crypto.md`          | 信号 14–18 加密与美股 24/7 永续                                                                                                                                                                                                               |
| `references/signals-d-antiemotion.md`     | 信号 19–22 抗情绪层（服务目标 1）                                                                                                                                                                                                             |
| `references/signals-e-cycle-valuation.md` | 信号 23–30 周期趋势与长期估值                                                                                                                                                                                                                 |
| `references/signals-f-monday.md`          | 信号 31–34 周一附加（不计入 30 项、不参与触发计数）                                                                                                                                                                                           |
| `references/decision-framework.md`        | 告警分级、7 项硬阈值、双轨决策层、停止加仓定义、恢复条件                                                                                                                                                                                      |
| `references/output-format.md`             | 报告 9 个部分的结构、强制归因规则、Slack 推送格式                                                                                                                                                                                             |
| `references/known-traps.md`               | **行为准则**、实测基线、已知失效/陷阱全表                                                                                                                                                                                                     |
| `scripts/fred.sh`                         | FRED 序列取数（信号 1、4、23、24、32）；`--net-liquidity` 信号 5；`--buffett` 信号 27（**两序列同季对齐**后取末行）                                                                                                                           |
| `scripts/cnn_fng.sh`                      | CNN Fear & Greed（信号 9）                                                                                                                                                                                                                    |
| `scripts/crypto.sh`                       | 资金费率 / 清算 / BTC Dominance / 稳定币（信号 14–17）；子命令必给，`liquidations` 设计上一定 exit 3；**信号 16 的 7d 腿靠自己累积的本地历史**（见下）                                                                                        |
| `scripts/stock_perp.sh`                   | Hyperliquid `xyz` 池美股永续（信号 18）                                                                                                                                                                                                       |
| `scripts/cape.sh`                         | multpl.com Shiller CAPE（信号 28）                                                                                                                                                                                                            |
| `scripts/market.py`                       | yfinance 行情与波动率块（信号 19–22、26、33–34）。信号 20 的 VIX 走 `fred.sh VIXCLS`（与信号 4／硬阈值 1 同源同日）；信号 26 出 `above_200dma_streak`（连续站稳交易日数，纯历史计算、不依赖状态档）+ `above_200dma`/`slope_positive` 当日布尔 |
| `scripts/snapshot.py`                     | 滚动状态档 `assets/last_run.json` 的 show / diff / write（子命令用法见 `--help`）                                                                                                                                                             |
| `assets/.gitkeep`                         | 占位档，**只为让 git 跟踪 `assets/` 这个目录**（git 不跟踪空目录）。不要删                                                                                                                                                                    |
| `assets/last_run.json`                    | 上次运行的各信号档位 + 两条轨道档位，**每次运行后被覆写**；**随技能分发的版本里没有这个档**（见下）                                                                                                                                           |
| `assets/dominance_history.jsonl`          | BTC Dominance 每日读数（一天一笔），**信号 16 的 7d 腿靠它自答**；每次 `crypto.sh dominance` 成功取数后追加/覆写，**随技能分发的版本里也没有这个档**（见下）                                                                                  |

**与 cwd 无关，但理由分三种**：两支 python 脚本（`market.py` / `snapshot.py`）内部用 `__file__` 锚定技能根目录；`crypto.sh` 用 `$(dirname "$0")` 锚定——它是**唯一会读写技能目录内档案的 shell 脚本**（信号 16 的本地 dominance 历史）；其余四支 shell 脚本**不读写技能目录内的任何文件**（纯网路取数 → stdout），所以它们既不需要、也确实没有做锚定。唯一吃路径的是 `stock_perp.sh --closes FILE` 与 `market.py --json FILE`，那是调用方明确给的路径。但**调用命令**本身仍要给对路径，故下文一律用 `$SKILL_DIR` 绝对路径调用。

**`assets/` 里的两个状态档都不随技能分发**：刚安装完 `assets/` 里只有一个 `.gitkeep`（git 不跟踪空目录，所以必须放个占位档，否则整个目录连同它在文件地图里的位置都不会被分发）。

- `assets/last_run.json` 要到第 6 步 `snapshot.py write` **第一次成功执行**后才生成。因此**首次运行时第 0 步读不到它是预期行为**，`snapshot.py show` 会明说「无昨日基准，本次为首次建立」——那不是安装缺档，也不需要去别处找这个文件。
- `assets/dominance_history.jsonl` 要到 `crypto.sh dominance` **第一次成功取数**后才生成，而且**要连跑 7 天**信号 16 的 7d 腿才会有答案。在那之前 7d 一律 ⚪️「历史不足（已累积 N 天）」——**那是正确输出，不是故障，更不准当成「未触发」**。两个档都是每次运行会变的运行时状态，`git status` 显示它们被修改是预期行为；把它们 commit 进去才是在推进基准。

## 第 0 步 · 前置检查 + 与昨日对照

### 0.1 定位技能目录与频道变量

`$SKILL_DIR` = **本 SKILL.md 所在目录的绝对路径**。你已经知道本 SKILL.md 从哪个路径加载——直接填进去，别猜、别写死家目录：

```bash
SKILL_DIR=<本 SKILL.md 所在目录的绝对路径>
echo "${RISK_MONITOR_SLACK_CHANNEL_ID:-<unset>}"
```

拿不准时探测（两种安装形态都覆盖）：

```bash
for d in "$HOME/.claude/skills/daily-risk-monitor" "$(git rev-parse --show-toplevel 2>/dev/null)/skills/daily-risk-monitor"; do
  [ -f "$d/SKILL.md" ] && [ -f "$d/scripts/snapshot.py" ] && SKILL_DIR="$d" && break
done
```

Slack 频道 ID 只从环境变量 **`RISK_MONITOR_SLACK_CHANNEL_ID`** 读，本技能不带任何配置文件。**未设置** → 跳过第 7 步推送，在正文末尾注明「本次未推送 Slack（`RISK_MONITOR_SLACK_CHANNEL_ID` 未设置）」；**报告本身照常完整输出**。本仓库公开：**绝不把真实频道 ID 写进任何文件，也绝不写进运行结果正文**，一律只用 `$RISK_MONITOR_SLACK_CHANNEL_ID` 占位符。

### 0.2 与昨日对照（先做这个）

```bash
python3 "$SKILL_DIR/scripts/snapshot.py" show
```

它打印上次运行写下的**每个信号的状态档位**、**战略基准**与**战术档位**。

- **本地状态档是首选来源。** 只有 `assets/last_run.json` 不存在 / 解析失败时，才**回退**用 `slack_read_channel` 读 `$RISK_MONITOR_SLACK_CHANNEL_ID`，找标题含「每日风险监控」的最近一条本任务报告。两条路都不通 / 首次运行 → 标注「无昨日基准，本次为首次建立」，不影响其余部分，**不重试超过 2 次**。
- **只比对状态档位（🟢🟡🔴），不比对具体数值**——数值天天动，档位才是信号。
- 今日报告必须回答：**哪些信号档位变了、哪些没变**。
- **若价格大跌但 0 个信号档位改变**，「今日解读」第一句必须是：「**30 个信号中 0 个状态改变，变的只有价格。**」

> 📖 **为什么这是第 0 步**：情绪的最大来源是「今天感觉和昨天不一样」。但绝大多数下跌日，基本面信号一个都没变——**变的只有价格和你的心情**。先把这件事摆在最前面。

## 第 1 步 · 取数

**先读 `references/data-cadence.md`**：全部 30 项每天都抓，但各项源头更新频率不同，**非更新日抓到相同值 = 正常**，标 `as of MM/DD`，**绝不因为数值没变就判定异常或标数据暂缺**。

curl 类取数一律走独立 shell script（可直接单独调用调试）：

⚠️ **`fred.sh` 与 `crypto.sh` 不带参数只会印用法并 exit 1**——前者要序列 ID 或组合模式，后者要子命令。下面这几行是可以照抄直接跑的完整命令：

```bash
# 信号 1、4、23、24（+ 周一的 32 DFII10）—— 一次给多个序列 ID 即可，逐个请求
"$SKILL_DIR/scripts/fred.sh" BAMLH0A0HYM2 VIXCLS VXVCLS T10Y2Y T10Y3M SAHMREALTIME
"$SKILL_DIR/scripts/fred.sh" --net-liquidity --days 5   # 信号 5：要看「连 4 周下降」，故取 5 笔
"$SKILL_DIR/scripts/fred.sh" --buffett                  # 信号 27：**同季对齐**后取末行 + 50–250% 量级自检
"$SKILL_DIR/scripts/cnn_fng.sh"                         # 信号 9    → references/signals-b-positioning.md
"$SKILL_DIR/scripts/crypto.sh" all                      # 信号 14–17（子命令必给）→ references/signals-c-crypto.md
"$SKILL_DIR/scripts/stock_perp.sh" --from-fred          # 信号 18   → references/signals-c-crypto.md「D. 美股 24/7 永续」
"$SKILL_DIR/scripts/cape.sh"                            # 信号 28   → references/signals-e-cycle-valuation.md
python3 "$SKILL_DIR/scripts/market.py"                  # 信号 19–22、26、33–34 → signals-d-antiemotion.md、signals-e-cycle-valuation.md
```

几个必须知道的实际行为（都是设计如此，不是故障）：

- **`crypto.sh liquidations` 一定 exit 3**——信号 15 没有任何免费公开源（Coinglass v4 需 API key）。脚本会**实测并印出每个来源的 HTTP 码**再标 ⚪️，这就是它的正常结局；`crypto.sh all` 因此必然把「信号15 清算」列进「本次数据暂缺项」，而 `all` 本身仍回 0。**接到这个 exit 3 就走 `web_search "coinglass liquidations 24h"`**，并在报告里写全三件事：24h 总清算金额（>\$500M = 杠杆洗盘｜>\$1B = 重大事件）、**多头 vs 空头哪一方被清算更多**、以及「上次已知读数 X @ YYYY-MM-DD，已滞后 N 周」。**搜不到也照样要报滞后周数**，不得写成「未触发」。
- **信号 16 的「7d 跌幅 >3%」由 `crypto.sh` 自己累积的历史回答，不要去换源。** 每次 `dominance` 成功取数会往 `assets/dominance_history.jsonl` 追加一笔（同日重跑覆盖，最多留 90 笔），累积够天数后脚本自己算 7d 变动，并同时印 **Δpt 与相对百分比**两个口径（阈值按相对百分比判定，与 24h 同一套规则）。三种 ⚪️ 的意思不同，报告要照抄脚本的说法：**历史不足**（`已累积 N 天`，连跑就会补齐）、**历史断层**（6–10 天窗口内没有基准；拿 31 天前的读数算出来的是「31 日变动」，贴「7d」的标签就是编数字）、**来源不同**（7 日前那笔是 CoinPaprika、今日是 CoinGecko 之类——两家分母不同，实测同日可差 2pt 以上而阈值只有 3%，一律拒绝比较）。**这三种都必须写成 ⚪️，绝不能因为「其余条件都正常」就推断这条腿安全**——少一条腿就少一次触发机会，会让加密信号触发计数系统性偏低（2 个 = 🟠 过热、3 个 = 🔴 警告）。排查用 `"$SKILL_DIR/scripts/crypto.sh" dominance --history`（不连网，印已累积几天与来源分布）。历史档写不进去（目录只读等）只会在 stderr 印一行告警，取数照常输出、照常 exit 0。
- `stock_perp.sh` 不给 `--spx/--ndx/--from-fred` 也会跑完并回 0，但隐含跳空全部标 ⚪️。`--from-fred` 的收盘价**滞后 1 个交易日**，脚本会把观测日与滞后天数印出来，报告须照抄这个滞后。
- `fred.sh --buffett` / `--net-liquidity` / `cape.sh` 量级自检不过时 **exit 4**：这时**不要引用那个数字**，按「先怀疑单位」处理。
- 信号 32（10Y TIPS 实质殖利率）只在**周一**取：`"$SKILL_DIR/scripts/fred.sh" DFII10`。

脚本覆盖不到的信号（2 200DMA 比例、3 BofA 牛熊、6 A/D Line、7 NAAIM、8 AAII、10 Put/Call、11 Margin Debt、12 内部人、13 IPO、25 LEI、29 AAII 配置、30 Margin Debt/GDP，以及周一的 31 Forward P/E）用 WebSearch / web_fetch，逐项按各 `references/signals-*.md` 里写明的搜索顺序与来源优先级执行，**每项附来源**。

**能用 API 就不要用搜索**——搜索来的资金费率、利差经常是几小时前的缓存值。

**周一**额外执行 `references/signals-f-monday.md` 的 31–34 四项 + Tier 1 四周趋势回顾（HY 利差 4 周变化、净流动性 4 周方向）。这四项**不计入 30 个信号，也不参与任何触发计数**。

### 工具与已知坑（完整表见 `references/known-traps.md`，逐条遵守）

| 用途                                                            | 工具                                       | 注意                                                                                                                                      |
|-----------------------------------------------------------------|--------------------------------------------|-------------------------------------------------------------------------------------------------------------------------------------------|
| FRED 经济数据                                                   | `scripts/fred.sh`（**curl**）              | `fredgraph.csv` 免 API key。**必须用 curl，python `requests` 在本环境会超时**。信号 27 走 `--buffett`（内建同季对齐），别自己各取末行相除 |
| 股价 / 波动率 / 均线                                            | `scripts/market.py`（**python yfinance**） | **必须用 `requests.Session` + UA，urllib 会 SSL 验证失败**                                                                                |
| 加密永续 / 稳定币                                               | `scripts/crypto.sh`（curl）                | Binance / Hyperliquid 公开 API，免 key；稳定币亦可走 DeFiLlama MCP `get_stablecoins`                                                      |
| 美股 24/7 永续                                                  | `scripts/stock_perp.sh`（curl）            | Hyperliquid 主池 `SPX` 是 SPX6900 迷因币 → **必须 `"dex":"xyz"`**                                                                         |
| CNN Fear & Greed                                                | `scripts/cnn_fng.sh`（curl）               | 裸请求回 **HTTP 418** → 必须带 `Referer: https://www.cnn.com/` + `Origin`                                                                 |
| Shiller CAPE                                                    | `scripts/cape.sh`（curl + 正则）           | multpl.com，解析配方见 `references/signals-e-cycle-valuation.md` 信号 28                                                                  |
| 其余（BofA、内部人、IPO、A/D Line、NAAIM、AAII、Put/Call、LEI） | WebSearch / web_fetch                      | 上面取不到时才用                                                                                                                          |

⚠️ **最容易搞混的一对相反要求**：**FRED 必须用 curl（`requests` 超时）**，而 **yfinance 必须用 `requests.Session` + UA（urllib SSL 失败）**。两者方向相反，改脚本时不要互相「统一」。

其余高频坑（脚本已内建，人工补数时同样适用）：yfinance `^VIX3M`/`^VIX9D`/`^VIX6M` 已停更 → 信号 4 改用 FRED `VXVCLS`；`WALCL`/`WTREGEN` 单位是百万、`RRPONTSYD` 是十亿 → 前两者 ÷1000；Buffett 两序列末行常不同季 → 必须 `merge(on="date")` 后取末行（**`fred.sh --buffett` 已内建这一步并会把两序列各自的末行日期一并印出**；人工补数或换源时这条仍然适用）。

**单位量级自检（算出来量级不对，先怀疑单位，不要直接报出来）**：净流动性 5–7 兆美元｜HY OAS 2–10%｜Sahm −1–2｜Buffett 50–250%｜CAPE 5–50。

## 第 2 步 · 判定 30 个信号档位

逐项按 `references/signals-a-macro.md` → `signals-b-positioning.md` → `signals-c-crypto.md` → `signals-d-antiemotion.md` → `signals-e-cycle-valuation.md` 里写死的阈值判 🟢🟡🔴（取不到写 ⚪️）。每项标注实际数据日期与 **↑/↓/→**（对比第 0 步的昨日档位）。

判完后**立刻**把结果写成 `today.json`（形状见第 6 步）并跑一次 diff —— 不要等到第 6 步：

```bash
python3 "$SKILL_DIR/scripts/snapshot.py" diff /tmp/today.json
```

`diff` 只比档位不比数值，直接产出报告第 1 部分强制要求的那句话（**「30 个信号中 X 个状态改变」**，
0 个改变时是「**30 个信号中 0 个状态改变，变的只有价格。**」），并列出是哪几项变了、
⚪️ 项的新增与恢复、以及两条轨道的回补进度（「触发数 ≤1 已连续 N 个交易日，距满 2 周还差 M 个」）。
**这句话必须抄脚本的输出，不要自己数**——自己数正是第 0 步要防的那种「凭感觉」。

`diff` 不写任何文件，可以反复跑；真正落盘是第 6 步的 `write`。

## 第 3 步 · 7 项硬阈值 + 告警分级

**读 `references/decision-framework.md`**「告警分级」与「🚨 7 项硬阈值 · 战略卖出触发追踪」两节：输出那张 7 行表（「数据日期」列必填，滞后 >2 周要写 `(滞后 N 周)`），按 Tier 1 / Tier 2 / 加密计数定 🟢正常 / 🟡留意 / 🟠过热 / 🔴警告，并按同一文件的「数据暂缺的计数规则」「警戒升级规则」「估值环境联动」逐条执行。

## 第 4 步 · 双轨决策层

同一份 `references/decision-framework.md` 的「🎯 决策层」一节：轨道一查表得**战略基准**，轨道二查表得**战术系数**，明写三个数 `战略基准 × 战术系数 = 最终目标仓位`。触发「停止加仓」时照抄该文件里那张「停止加仓 = 下列全部停止」的表（定投、股息再投、新增资金、逢低加仓、再平衡买入端）。处在非满仓状态时，每次都要报告「距离恢复还差什么」；战术层进 🔴 或战略基准 ≤70% 时，复述一遍「这套规则的代价」。

战术层恢复条件里的「**连续 5 个交易日**站稳 200DMA」**以 `market.py` 的 `above_200dma_streak` 为准**（`--json` 里信号 26 那块）。它是从日线历史直接算的纯历史计算——逐根用**该根当日**的 200DMA 比较，不依赖任何状态档，所以漏跑、状态档遗失、首次运行都照样答得出。

- 数到可得历史尽头才停时，说明文字会写「**至少**连续 N 个交易日」——那是**下界**不是确定值，报告须照此措辞。
- 历史不足 205 根日线时记 `null` 并写「历史不足，无法判定」，**不填 0、不视同已满足**。
- `snapshot.py` 也会报一个连续天数，但它数的是**运行日**（每次 `write` 一笔），漏跑或周末会与交易日口径分叉。**两者不一致时以 `market.py` 的交易日口径为准**，`snapshot.py` 那个仅作交叉验证。
- 恢复要**两个条件同时成立**：连续 5 日站稳 **且** 200DMA 斜率转正。`market.py` 给前者的完整答案与后者的当日值；斜率的跨日确认仍看 `snapshot.py`。不得凭当日一个 `above_200dma=true` 就宣告恢复。

## 第 5 步 · 输出报告

**读 `references/output-format.md`**，按「开场 · 今日大白话」+ 第 0–8 部分逐节写全，遵守其中的强制归因规则、全绿写法与周一附加节。**报告写在本次运行结果（对话回复正文）里**，Slack 推送是额外分发，不是替代。

## 第 6 步 · 写回今日档位（在 Slack 推送之前）

先把今日结果写成 `today.json`，再写回状态档：

```bash
cat > /tmp/today.json <<'JSON'
{
  "signals": {
    "1": {"state": "🟢", "notes": "HY OAS 2.71%"},
    "2": {"state": "🟢"},
    "…": "1–30 必须齐全，缺一个就拒写；31–34 是周一附加，不要混进来",
    "30": {"state": "🔴"}
  },
  "hard_thresholds": {
    "1": {"state": "❌"}, "2": {"state": "⚪️"},
    "…": "1–7 必须齐全",
    "7": {"state": "⚪️"}
  },
  "tracks": {
    "strategic": {"baseline_pct": 100},
    "tactical":  {"state": "🟢", "above_200dma": true, "dma200_slope_positive": true}
  }
}
JSON
python3 "$SKILL_DIR/scripts/snapshot.py" write /tmp/today.json --date 2026-09-03
```

**校验很严（宁可拒写也不写坏），照下面的取值范围填**：

| 字段                                    | 允许值               | 备注                                                      |
|-----------------------------------------|----------------------|-----------------------------------------------------------|
| `signals.1..30.state`                   | `🟢` `🟡` `🔴` `⚪️`  | 必须 1–30 齐全；31–34 混入会被点名拒绝                    |
| `hard_thresholds.1..7.state`            | `❌` `⚠️` `✅` `⚪️`  | 必须 1–7 齐全                                             |
| `tracks.strategic.baseline_pct`         | `100` `85` `70` `50` | 只收这四档                                                |
| `tracks.tactical.state`                 | `🟢` `🟡` `🔴`       | **不收 `⚪️`**——战术层没有「数据暂缺」档                   |
| `tracks.tactical.above_200dma`          | `true` / `false`     | 布尔，写成字符串会被拒。取自 `market.py --json` 的信号 26 |
| `tracks.tactical.dma200_slope_positive` | `true` / `false`     | 同上                                                      |

写入的字段走**白名单**：不在上表里的键会被丢弃并在 stderr 点名，不会被静默写进公开仓库。
`history` 由脚本自己维护（保留 14 个交易日），不要自己传。

写入今日各信号档位 + 战略基准 + 战术档位到 `assets/last_run.json`，供下次运行的第 0 步比对。

- **必须在第 5 步报告写完之后、第 7 步 Slack 推送之前执行**——这样即使 Slack 推送失败，档位状态也已经滚动到位，明天照样能做对照。
- `assets/last_run.json` **每次运行后被覆写是预期行为**；git 仓库形态下 `git status` 显示它被修改不是意外脏文件。`assets/dominance_history.jsonl` 同理（在第 2 步 `crypto.sh dominance` 时就已经被追加/覆写了一笔），它承载的是信号 16 的 7d 腿，**别把它 checkout 掉**——丢了就要重新连跑 7 天才能恢复 7d 判定。

## 第 7 步 · Slack 推送

按 `references/output-format.md`「Slack 推送」一节（精简版、真表格不用代码块、Slack 表格规则）发到 `$RISK_MONITOR_SLACK_CHANNEL_ID`。未设置该变量 → 跳过本步并在正文末尾注明，**不影响正文完整性**。推送失败 → 说明可能原因并给出可复制的兜底文本。

## 第 8 步 · 交付自检

逐条核对，任一为「否」就补齐再结束：

1. [ ] 开场大白话 + 第 0–8 部分齐全，30 个信号一个不少（周一另有 31–34）
2. [ ] 每个专业名词都有大白话；每行有「一句话解读」
3. [ ] 7 项硬阈值表的「数据日期」列全填，滞后 >2 周已标 `(滞后 N 周)`
4. [ ] 已写「今日共 X / N 项触发（M 项数据暂缺）」及最坏情况推演
5. [ ] 三个数都明写：战略基准 × 战术系数 = 最终目标仓位
6. [ ] 与昨日档位的对照结论已写；0 变动时那句话已照写
7. [ ] `snapshot.py write` 已成功执行（exit 0）且在推送之前
8. [ ] 正文与 Slack 文本里没有真实频道 ID、没有本机绝对家目录路径

## 🔴 两条红线（最容易被违反，每次运行前重读一遍）

### 红线一：绝不编数字

`references/known-traps.md`「行为准则」第 1 条是所有准则里最重要的一条：

- 取不到就写「⚪️ 数据暂缺」+ **列出尝试过的每一个来源**，并**报出滞后周数**：「上次已知读数 X @ YYYY-MM-DD，已滞后 N 周」。**没有滞后周数的「数据暂缺」是不合格的输出。**
- **不要用训练数据、记忆、或「合理推断」填充。** 一个看起来很合理的数字，比一个明显缺失的空格危险得多——空格你会去查，合理的数字你会直接拿来做决策。
- **⚪️ 不计入触发数，也不计入分母**（N = 7 − M）；**绝不允许因为「其余几项都很安全」就推断暂缺项也安全**。⚪️ ≥3 项时战略基准维持昨日档位，不因计数下降而回补仓位。
- **最高风险的两项是信号 3（BofA 牛熊）与信号 12（内部人买卖比）**：两者都没有稳定数据源（BofA 只在美银每周 Flow Show 报告公布、靠媒体转载；GuruFocus 实测回 403），**却同时是 7 项硬阈值的第 6、第 7 项，编造的数字会直接改变战略层的目标仓位基准。**
- **不要用口径不同的替代源去比对原口径的阈值**：宁可标 ⚪️，也不要拿 openinsider 的数字去比 GuruFocus 的 0.17。

### 红线二：阈值永远不因市场情绪动态调整

**VIX < 13 永远是「自满」**，不能说「现在结构性偏低所以正常」。**CAPE 42 永远是极端**，不能说「因为科技股占比高所以合理」。触发状态严格按阈值判断，**不加「但是」「不过」之类的软化语言**——达成就是达成。

## 依赖

**本技能不依赖任何其它技能**（这一点与 `ai-pullback-daily` 不同，后者硬依赖 `ai-industry-weekly`）。它自带全部脚本与状态档，可以单独安装、单独运行。

`ai-pullback-daily` 与本技能都从 Hyperliquid 的 `xyz` 池读美股永续，但**各自维护自己的脚本、互不引用**——本技能用 `scripts/stock_perp.sh`，那边用它自己的 `scripts/perp_quotes.py`。不要把两者合并或交叉 import。

运行需要：`bash` + `curl` + `awk`；**`jq`**（`crypto.sh` / `cnn_fng.sh` / `stock_perp.sh` 硬依赖，缺了会 exit 2 并印安装指令与降级办法；`fred.sh` / `cape.sh` 不需要）；`python3` + `requests`/`yfinance`/`pandas`/`numpy`（只有 `market.py` 用，`snapshot.py` 纯标准库）；可联网（FRED / yfinance / Binance / Hyperliquid / CoinGecko / CoinPaprika / DeFiLlama / CNN / multpl）；WebSearch / web_fetch 用于脚本覆盖不到的信号；Slack MCP 可选，仅第 7 步用。

## 规则

- 每天运行一次；周六日与美股假期须注明「TradFi 休市，VIX / HY 利差 / 200DMA / σ倍数 / 曲线 数据为上一交易日」，加密与美股永续照常；
- 数据缺失记 ⚪️ 数据暂缺 + 尝试过的来源 + 滞后周数，**绝不编造、绝不估算**；
- 阈值不因情绪动态调整，触发判定不加软化语言；
- 只判定规则状态，**不给投资/交易建议、不喊多空、不预测点位**——决策层报告的是「我预先定好的规则今天触发了没有」，不是意见；
- 第 0 步以本地 `assets/last_run.json` 为准、Slack 仅作回退；第 6 步写回必须早于第 7 步推送；
- 仅风险监控框架，不构成投资建议。
