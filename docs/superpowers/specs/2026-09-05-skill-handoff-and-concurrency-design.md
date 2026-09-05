# 交接面自足化、检索传输层与并发 —— 设计文档

- 日期：2026-09-05
- 分支：`feat/script-output-contract`（原名 `feat/subagent-architecture`，因设计否决了 subagent 拆分而改名）
- 状态：设计已确认，待实施
- 触发问题：①「可以把这些 skills 改成 subagent 吗，获取数据的是一个 agent，分析数据的是一个 agent，避免上下文过长与污染」②「整个流程太慢了，能不能并发获取数据」

---

## 0. 结论摘要

**不按「取数 agent / 分析 agent」拆。** 四个独立视角（数据正确性、上下文实测、可移植性、时序与副作用）分别压测该切法，四个都得出同一结论：这条轴切错了。理由见第 1 节，作为否决记录保留。

改为三件事，共享同一条法则：

> **两个交接面，一条法则：交接必须走文件 + 自足 JSON。**
> **B** 管脚本那一半，**A** 管检索那一半，**C** 让两半重叠起来跑。

| 编号   | 名称        | 解决什么                                        |
|--------|-------------|-------------------------------------------------|
| **B1** | JSON 等价律 | 机读输出与人类输出信息量不等 —— 最贵的一类缺陷  |
| **B2** | 窄读纪律    | 脚本 stdout 挤占分析注意力                      |
| **A**  | 检索传输层  | WebSearch 原始 snippet（40–120 KB）污染主上下文 |
| **C**  | 并发与调度  | 取数 30–75s 串行；**但端到端只值 10–20%**       |

三者有硬依赖：**B1 → C 的脚本间并发**（并发最大的腐蚀风险是 stdout 交织，`--json FILE --quiet` 从构造上消除它）；**B1 → B2**（JSON 不自足时窄读会主动丢掉 stderr 与 exit code）。

---

## 1. 否决记录：为什么不按 fetch/analyze 拆

保留此节，避免日后重新提议。

### 1.1 所有写盘副作用都在分析的下游

没有任何东西可以挪到取数侧：

- `baseline.py write` 吃的是分析产出的整表；`snapshot.py write` 吃的是分析判完的 30 个档位（经由 `/tmp/today.json`，而该文件在判定完成前不存在）。
- 两个 `diff` 都在**调用时现读**磁盘状态文件（`baseline.py:633`、`snapshot.py:763`）。

把 `write` 放到取数侧，`diff` 变成 no-op —— exit 0，然后往报告里打印一句自信的假话：「本周评级无变动」／「30 个信号中 0 个状态改变」。**该失败今天没有任何可观测症状。**

### 1.2 报告正文是父 agent 的回复，不是子 agent 的返回值

三个技能都有交付双轨类规则（`ai-industry-weekly/SKILL.md:22`：①②③ 必须完整写在运行结果正文里）。子 agent 只能经由 final message 返回，正文要付两遍。

两条反向边在单向 DAG 里无路可走：

- `write` 被拒后的返工环：修表 → `validate` → `diff` → **修正正文里已写出的摘要①** → `write`（`ai-industry-weekly/SKILL.md:249`，且脚本不提供 `--force`）；
- Slack `message_ts` → thread → 把两条链接补回正文之后。

子 agent 的上下文一次性、不可重入。

### 1.3 脚本层早已解决，references 层会被拆得更糟

| 事实                            | 实测                                                                                                                                                                  |
|---------------------------------|-----------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `technicals.py --json`          | 写文件 + 打印一行就 `return`（:1036），**从不调用 `print_report`** —— 仓库最大的 JSON（60–80 KB）本来就不进上下文                                                     |
| `ai-industry-weekly` 真正的大头 | `baseline.py show` = **19,125 字节**（`wc -lc` 实测），同一张 46 行表还要出现在临时文件、正文②、Slack thread → 57–76 KB，**拆 agent 一字节都省不掉**                  |
| `daily-risk-monitor` 拆完       | 取数侧必须加载 `signals-*.md`（53,766 B）才知道搜索顺序，分析侧同样需要 → 引用文档总量 77,612 → **127,381 B（+64%）**，重复 49,769 B，只为赶走 12–18 KB 的脚本 stdout |

### 1.4 可移植性

`.claude/agents/` 是 Claude Code 专有格式，跨运行时不可移植；而本仓库的存在理由是 agentskills.io 开放规范 + `npx skills add`。仓库法律是**每个可选依赖都要响亮降级**（缺 `AV_API_KEYS` →「少一路增强，不少一段交付」；缺 `jq` → exit 2；缺姊妹技能 → `_weekly.py` 报错「绝不静默回退」）。

缺一个 `.claude/agents/*.md` 会**降级成什么都没有**：无探测、无退出码、无候选路径、无环境变量覆盖。它会是仓库里第一个**静默失败**的依赖。`git ls-files .claude` 目前只跟踪 `settings.json` 一个文件，README 记载的手工安装方式是 `cp -r skills/<name> ~/.claude/skills/` —— 子 agent 定义文件不会被任何已文档化的安装路径带走。

---

## 2. B1 · JSON 等价律

### 2.1 写进 CLAUDE.md 的新不变量

与「Fallback chains — the rule」并列：

> **任何脚本的 `--json` 必须与它的人类输出等价。** 凡是文本分支会打印的告警、禁令、判决、口径声明，JSON 里必须有对应字段。exit code 与 stderr 只能是**冗余**通道，不能是唯一通道 —— 任何窄读、任何交接、任何自动化都只拿得到 stdout。
>
> 推论：`--quiet` 的语义是「stdout 不做人类可读渲染」，**永远不等于「不告警」**。stderr 保持活的；每一个降级同时必须是 JSON 里的结构化字段。

### 2.2 缺陷分级

两类，修法不同，优先级不同。

**甲类 · 自相矛盾**（显眼字段说谎，真相在不显眼的字段里）
窄读与交接恰恰会挑显眼字段，所以仍必须修，但不构成数据不可恢复。

| 脚本                   | 说谎的字段                                                                                       | 说真话的字段                                              | 后果                                                                                                        |
|------------------------|--------------------------------------------------------------------------------------------------|-----------------------------------------------------------|-------------------------------------------------------------------------------------------------------------|
| `cnn_fng.sh:168`       | `triggers.hard_threshold_4_greed_burst: false`（`T_BURST=0` 初始化，仅在 `PEAK != "NA"` 时重算） | `peak.value: null`                                        | **硬阈值第 4 项**。⚪️ 要从分母扣除（`N = 7 − M`），`false` 不扣 —— 读 `triggers.*` 的消费者会系统性高估安全 |
| `fred.sh:251` / `:353` | `"ok":true` 写死                                                                                 | `sanity.pass`（条件的，:258 / :363）                      | 净流动性 + 巴菲特指标两个模式                                                                               |
| `cape.sh:170`          | `"ok":true` 写死                                                                                 | `sanity.pass`（:176）                                     | 信号 28 CAPE                                                                                                |
| `stock_perp.sh:300`    | `ok:true` 写死                                                                                   | `triggers.wrong_market[]` / 逐行 `wrong_market_suspected` | 信号 18                                                                                                     |
| `etf_holdings.py:1233` | `derived.full_coverage: true` + 空 `na_reasons`（`derive([], full=True)`）                       | `coverage:"none"`、`meta.notes`、顶层 `warnings`          | 唯一全 N/A 的情形是唯一没有理由附着的情形                                                                   |

**乙类 · 真正缺失**（JSON 里根本没有对应物）

| 脚本                              | 缺什么                                                                                                                                                                                                                                                        |
|-----------------------------------|---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `neocloud_credit_monitor.py:1170` | `res.pop("cfg", None)` 剥掉了 ⑧ 数据缺口 赖以声明「**无实时 CDS 数据（本层最大盲区）**」的键（判定在 :1015-1017 读 `cfg.manual_flags`）→ 该层自述的最大盲区只存在于 markdown                                                                                  |
| `snapshot.py:699`                 | `show --json` 直接 dump 原始 `last_run.json`，**严格少于 `show`**：漏掉 `staleness_banner()`（含「⚠️ 基准是 N 天前的，中间漏跑过。报告里请写「对比 N 天前」，不要写成「对比昨日」」）、`state_counts()`、`print_tracks()`、`print_hard()`、`recovery_lines()` |
| `fred.sh:355`                     | `--buffett --json` 给了两个序列末行日期，却丢了文本分支（:374-376）那句「**不要**改用各取各的末行相除」—— 已知陷阱 #6 只在人类输出里防守                                                                                                                      |
| `hk_quote.py:236`                 | 「口径：原始未复权。52周高/低 = 252交易日不复权日K线自算」无 JSON 等价物（顶层是裸数组，没有信封）                                                                                                                                                            |

**丙类 · 完全没有 JSON**

| 脚本                                                                      | 说明                                                                                                                                                                                                                                                                                |
|---------------------------------------------------------------------------|-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `neocloud_credit_lite.py`                                                 | **最贵的一个**。两个脚本必须对引爆点 ④ 达成一致（CLAUDE.md 称其分歧为本技能最坏的失败），今天只有 monitor 那个能被机器 diff。加 JSON 安全（`json` 是标准库），但**必须自足，不得 import 脚本目录里的其他模块**；且它的 `TH` 字典键名与 monitor 不同，共用 schema 是一次**两处编辑** |
| `baseline.py` validate / diff / write                                     | `validate` 的编号错误表、`diff` 的拒绝语「本次**没有**产出任何变动摘要：结构不对的表对出来的结论一定是错的，不得贴进正文」（:625）都只在 stderr                                                                                                                                     |
| `etf_holdings.py --check`（:1156）<br>`industry_table.py --check`（:676） | `--check` 分支在 `--json` 之前 return，静默打人类文本。前者的 key 分类结论（`✓ 可用` / `⚠ 瞬时限流` / `✗ 日配额已耗尽` / `✗ API 参数错误`）正是 CLAUDE.md 点名「最容易搞错」的那个 burst-vs-daily 判定                                                                          |

**丁类 · 缺 `--quiet`**

| 脚本                 | 说明                                                                           |
|----------------------|--------------------------------------------------------------------------------|
| `perp_quotes.py:729` | 无条件 `print(render(result))`，位置在 `--json` 分支之前，**没有任何办法关掉** |

### 2.3 独立行为 bug（非 JSON）

`neocloud_credit_monitor.py:1153` 的 `append_history` 位于 `--json` / `--compact` / markdown 三分支**之前**，因此 `ai-pullback-daily/SKILL.md` 第二步的两次调用**都写历史档**并同日覆盖（`:483` `recs = [r for r in load_history() if r.get("date") != rec["date"]]`）。明天的 ⑩ 跨档变化比对的是第二次取数，而报告引用的是第一次。

**修法要求：一次取数、两次渲染。** 给第二次加 `--no-history` 只治覆盖，不治「两版基于同一次取数，数据严格一致」这条规则本身 —— 那仍是两轮独立网络取数。实现为 `--emit both`（或 `--compact-also FILE`），两份渲染必须**源自同一个内存判定对象**，使 compact 行与完整 markdown 在结构上不可能对引爆点 ④ 有分歧。`append_history` 保持在渲染分支之前，使渲染失败不会丢记录。

---

## 3. B2 · 窄读纪律

**前提：B1 全部落地之后才能做。** JSON 尚不自足时窄读，等于主动丢掉 stderr 与 exit code —— 会把潜在缺陷变成实际缺陷。

1. 重脚本一律 `--json OUT.json --quiet`，正文只读回需要的字段。
2. 只需要少数字段的步骤用 `jq` 点读。已确认的一处：`daily-risk-monitor` 第 4 步只需要 `market.py` JSON 的 `above_200dma` / `slope_positive` / `above_200dma_streak` 三个字段。
3. **豁免名单 —— 这些绝不窄读**，写进各自 SKILL.md，不靠记忆：
   - `neocloud_credit_monitor.py` / `neocloud_credit_lite.py` 的 markdown（`ai-pullback-daily/references/output-format.md:63`：「整段贴入完整版，不删节、不改写数字」）；
   - `baseline.py show`（正文②要逐行全表）；
   - 任何带「照抄」「原样保留」字样要求的输出。

---

## 4. A · 检索传输层

### 4.1 范围

清点出 **39 个检索项**：`daily-risk-monitor` 18 项、`ai-pullback-daily` 21 项。

**`ai-industry-weekly` 不接入** —— 它只有 1 个检索项（发行商官网持仓页，且要求逐字权威）。YAGNI。

### 4.2 契约：公共信封 + 逐项 payload

**统一标量元组装不下其中约 20 项**，这是本设计最重要的实证结论。契约分两层。

**公共信封（每项都有）**

```
item_id, name
status               : ok | missing | not_applicable
tier_used            # 文档化来源顺序里，实际答上来的是第几级
attempted[]          # {source, url, http, outcome} —— 成功时也要有
source_label, source_url
as_of
as_of_granularity    : trading_day | survey_week | month | quarter | meeting_date
caliber              # 口径标签：8h vs 4h 资金费率、Equity vs Total put/call、百万 vs 十亿
threshold_comparable : {value: bool, reason: str}
carry_forward_policy : must_carry | must_refetch | n_a
last_known           : {value, date} | null
staleness            : {n, unit: "weeks" | "days"} | null
missing_sentinel     : "⚪️" | "N/A" | "（无）" | null      # 由调用方指定
counts_toward        : {numerator: bool, denominator: bool}
payload              # 逐项定义，见 4.3
```

两个槽位是调查逼出来的，缺了整层白做：

- **`threshold_comparable`** —— 信号 12 可以从 openinsider 取回一个**真实、新鲜、解析正确**的数字，而它仍必须报 `⚪️ 数据暂缺`，因为 `0.17` 只对 GuruFocus 的美元加权月度 USA-Overall-Market 算法校准（`signals-b-positioning.md:62-69`）。没有这个显式布尔，「有值」会被读成「可比阈值」，⚪️ 会计静默塌缩成 ❌。同型：CBOE Total vs Equity、跨所资金费率、CoinPaprika 主导率。
- **`carry_forward_policy`** —— 同一技能里既有**必须沿用**的项（FOMC 政策倾向：`as_of` 是会议日期，陈旧是正确状态），也有**沿用即缺陷**的项（FOMC 日历与 FedWatch 隐含概率：「每次运行重新确认、不得沿用」）。只看 `staleness` 分不出这两类。

**四个缺失哨兵不是一个符号**：`⚪️`（引爆点、利率、信用层）、`N/A`（技术字段）、`（无）`（空的「✅ 未触发」清单）、`null`（`manual_flags`）。且 `⚪️` 有独有会计规则：不计入 🟡/🔴 计数，但要在完整版列出 —— 既不进分子也不进分母。**哨兵由调用方指定，不由取数方选。**

### 4.3 payload 八族

| 族       | 项                                    | 形状                                                                                                                                                             |
|----------|---------------------------------------|------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| 标量     | NAAIM、LEI、CAPE 类                   | `{value, unit}`                                                                                                                                                  |
| 多值     | 信号 11 融资余额                      | `{abs, yoy_pct, mom_direction, three_month_streak[]}` ——「连续 3 个月月减」标量答不了（硬阈值第 2 项）                                                           |
| 多值     | 信号 8 AAII                           | `{bull, bear, spread, weeks_above_30}` ——「连续多周 >30」需要持续性                                                                                              |
| 背离裁决 | 信号 6 A/D 线、信号 2 200DMA 比例     | `{current, prior_peak, peak_date}` + 由**父级**从 `market.py` 供给的「SPX 是否创新高」布尔                                                                       |
| 分布     | CME FedWatch                          | `{cut_pct, hold_pct, hike_pct}` 三者同源同刻，拆成三个元组会丢掉该约束                                                                                           |
| 记录表   | 信号 13 IPO、`primary_market.deals[]` | `readings[]` + 集中度 + 剔除最大单后的重算。信号 13 有两个同阈值却互相矛盾的口径（件数 −51% vs 金额 +470%，2026-08-10 实测）                                     |
| 同源对   | 信号 4 VIX 期限结构                   | `{near, far, shared_source, shared_date}` —— 两腿必须同源同日，正是 `signals-a-macro.md:5-9` 编者注要防的                                                        |
| 纯方向   | 信号 3 BofA 牛熊、信号 12 内部人      | `{direction_text}` 且 `status = missing`（可以说「近两周集中卖出为主」而仍然是 ⚪️；信号 3 在连 `last_known` 都取不到时要能表达「无历史基准，本项完全不可判定」） |

四个 web 项同时是硬阈值行：**11（#2）、6（#5）、3（#6）、12（#7）**。任一 ⚪️ 都改变分母并强制最坏情况行「若暂缺的 M 项全部触发，计数将达 X+M」；三项以上 ⚪️ 冻结战略基准。**这些字段是仓位输入，不是记账。**

### 4.4 顺带修复的规格缺陷

`ai-pullback-daily` 的 `manual_flags` 三项目前是**三态无 false**：证据字符串 = 发生；`null` =「未观察到」；没有「查过、确认没发生」。加显式 `checked_not_observed`，否则「今天查过」与「今天没查」在数据里不可区分。

### 4.5 明确排除在契约之外

传输层**不返回**：任何 🟢🟡🔴 状态、任何阈值比较、任何「一句话解读」、任何叙事散文。

**用 schema 强制** —— 返回结构里没有这些字段，而不是靠提示词自律。判定留在写报告的那个 agent。

### 4.6 调度与交接

- 按 **governing reference 分组**派发，一组加载一个参考文件：`daily-risk-monitor` 约 5 组（signals-a/b/c/e/f），`ai-pullback-daily` 约 4 组（引爆点 / 宏观利率 / 事件日历 / 债券与一级市场）。
- 每组把结果**写成 JSON 文件**，返回消息只有一行：`wrote N items to <path>, M ok / K missing`。父级读文件。**这就是 B 的文件交接法则应用到检索半边。**
- 返回值**可审计**：`attempted[]` 成功时也要有，父级不看 snippet 就能验证文档化的来源顺序是否被真的走过；tier-1 未被尝试就打回重取。

### 4.7 成本的诚实说明

**A 会让总 token 上升，换取父级上下文变干净。** 检索侧仍要加载 `signals-*.md` 才知道搜索顺序 —— 那份重复付在**子 agent 的上下文**里，父级不变；父级省掉的是 40–120 KB 原始 snippet。

痛点是注意力不是容量，这笔交换成立。**但它不是省钱，文档不把它写成省钱。**

---

## 5. C · 并发与调度

### 5.1 实测基线

全部真跑过（`etf_holdings.py` 除外 —— Alpha Vantage 免费层 25 次/日，与真实周更共享配额，仅读代码）。

| 单元                         | 实测                                                | 结构                                                                                                            |
|------------------------------|-----------------------------------------------------|-----------------------------------------------------------------------------------------------------------------|
| `fetch_fundamentals.py` 3 档 | **5.19s**                                           | `.info` 每档 **3 次串行** Yahoo 往返（+ 每进程 1 次 crumb 引导）；全量 46 档**外推 ≈60–90s / ~143 次串行 HTTP** |
| `technicals.py` 全量         | ~45s，其中 **~39s 是 41 次 `get_earnings_dates()`** | 逐档串行                                                                                                        |
| `neocloud_credit_monitor.py` | **13.9s**，而 SKILL.md 跑**两次**                   | 14 个 FRED 序列串行                                                                                             |
| `crypto.sh all`              | 7.16s                                               | 4 个 block 串行，4 个不同主机                                                                                   |
| `fred.sh` 6 序列             | **6.02s**；同样 6 个 URL 合并成一次 curl **3.89s**  | 逐序列 curl                                                                                                     |
| `stock_perp.sh --from-fred`  | 5.41s                                               | 2 个 FRED + 1 个 Hyperliquid 串行                                                                               |
| `market.py`                  | 3.70s                                               | —                                                                                                               |
| `hk_quote.py` 3 档           | 2.78s                                               | 1 次批量实时报价 + N 次串行日 K                                                                                 |
| `baseline.py show`           | 0.10s                                               | 纯本地，零网络                                                                                                  |
| `etf_holdings.py` 5 档       | 未跑；读码估 8–13s                                  | 其中 **4.8s 是刻意 sleep**                                                                                      |

并发后每技能取数层：`ai-pullback-daily` ~75s→~12s、`daily-risk-monitor` ~29s→~4s、`ai-industry-weekly` ~75s→~22s。

### 5.2 诚实的量级

**整轮运行是 5–15 分钟，取数只占 30–75 秒。端到端最多快 10–20%。**

felt slowness 的主体是模型读 references、判 30 个信号、跑 WebSearch、写长报告。把仓库所有循环都改成线程池也动不了那 90%。真正移动这个数字的是 A 与 B，以及下面的调度层。

### 5.3 三个「不是并发」的修复

优先级高于大部分线程池。

1. **`technicals.py` 的 41 次 earnings 调用占它 45s 里的 39s。** `--no-earnings`（:1027）已存在，但会丢掉节奏层的 ⚠️EARN 标签。**已决定：保留 ⚠️EARN，走池化**（39s→~7s，见 5.5）。
2. **`neocloud_credit_monitor.py` 每天跑两次 = 两轮完整 13.9s 取数 + 两次历史档写入。** 改成一次取数两次渲染：**−13.9s**，同时关掉 2.3 节的双写 bug。
3. **`technicals.py --macro-only` 重下 8 个指数符号**，而全量那次已写进 `tech.json`：**−2s**，且减少对唯一会限流那台主机的压力。保留 `--macro-only` 作为**全量失败时的降级路径**。

   字段等价性是**结构上成立的**，不需要逐字段比对：两条分支返回的是同一个对象 —— macro-only 路径 `:941` `"macro": macro` / `:943` `"indices": macro.get("indices", {})`，全量路径 `:986` / `:988` 写法完全相同，而 `indices` 在 `:731` 就已挂进 `macro` 本身。**仍须核对的只有一件事**：`tech.json` 里 `macro` 块确实存在（全量取数失败时它可能缺席）。缺席时报告必须明写并显式回退到 `--macro-only`，**绝不能用一个不存在的字段拼出宏观小节**。

### 5.4 头号收益：调度，不是线程

**t=0 把所有独立取数单元甩到后台，立刻派发 A 的检索分组，让 30–75 秒的取数整个消失在检索延迟底下。** 脚本内部零改动。

```
t=0   industry_table.py --check &                    # 先起：stop-the-line 条件
      ( technicals.py --json … && perp_quotes.py --spot … ) &   # 有序边留在同一个 job 内
      neocloud_credit_monitor.py --emit both --json … &
      A 的 4~5 个检索分组并发派发
t=?   逐 PID wait，读回各自的 JSON
```

**守则**（缺一不可）：

- 每个单元 `--json /tmp/run/<unit>.json --quiet`，**没有两个 job 共享 stdout**（依赖 B1）。
- **有序边留在 job 内部，不跨 job**：`technicals → perp_quotes` 是**一个**顺序 job，不是两个。
- **逐 PID 收退出码**（`wait $pid` 每一个），不能裸 `wait` —— `crypto.sh` 的 exit 3 是设计出来的、有意义的。
- join 步骤对**缺文件**必须响亮失败：缺一个单元绝不能被读成「该单元无数据」。
- A 的分组也写 JSON 文件，最终要 join 来自两个生产者的 ~8–9 个文件 —— **给它们不同的命名空间**。

### 5.5 分层并发计划（按 收益/风险 排序）

| 层     | 改动                                    | 收益                       | 风险             |
|--------|-----------------------------------------|----------------------------|------------------|
| 调度   | 5.4 的后台并发 + 与 A 重叠              | 隐藏几乎全部取数层         | 极低             |
| 脚本间 | 信用监控一次取数两次渲染                | −13.9s                     | ~0               |
| 脚本间 | 去掉 `--macro-only` 重复下载            | −2s                        | 极低             |
| 脚本内 | `fred.sh` 合并成单次多 URL curl         | **−2.1s（已实测）**        | 极低             |
| 脚本内 | `stock_perp.sh` 3 个请求并发            | 5.41s→~1.8s                | 低               |
| 脚本内 | 信用监控 FRED 池化(5)                   | 13.9s→~4s                  | 低-中            |
| 脚本内 | `crypto.sh` 4 个 block 并发             | 7.16s→~3.5s                | **中**           |
| 脚本内 | `technicals.py` earnings 池化(6)        | 39s→~7s                    | 中-高            |
| 脚本内 | `fetch_fundamentals.py` `.info` 池化(6) | 65s→~12s（周更，每周一次） | **最高，放最后** |

逐项守则：

- **`crypto.sh`** —— 风险全在 `OKCOUNT` / `MISSING` 的归约。写错会让一次完全成功的运行报成全盘缺数，或更糟：报出一个空的暂缺清单。四个 block 各写 `$WORK/out_<block>.json` + `$WORK/status_<block>`（退出码、OKCOUNT 增量、MISSING 行），父级 `wait` 后按**固定 block 顺序**归约。
- **`crypto.sh` 的两个 Coinglass 清算探针不要删。** 这里诱人的改动是删除而非并发：两个探针都打向脚本已知会失败的调用（`:371` 硬编码 `return 1`），但 ⚪️ 判定必须携带每个源的 HTTP 证据（`daily-risk-monitor/SKILL.md:134`，行为准则第 1 条）。少探一次是证据变弱，不是脚本变快。
- **`fred.sh`** —— 人类输出按 `$IDS` 顺序逐序列打印、`--json` 的 `series` 也是该顺序的数组：并发后必须**按声明顺序归约**，不能按完成顺序。

### 5.6 传输层：两个必须记住的事实

**yfinance 的 crumb 竞态是假警报。** 核过装着的 yfinance 1.4.1：`YfData` 是进程级单例（docstring：「Singleton means one session one cookie shared by all threads」），metaclass 上有 `threading.Lock`，crumb/cookie 取用有 `_cookie_lock`（`data.py:55-93`、`:103-124`），`_set_session(None)` 是显式 no-op。这条本会一票否决池化的顾虑不成立。

**但限流是真的，而且仓库被迫用的那条路正是更容易被限的那条。** `market.py:303` 记着实测：「requests.Session 路径回空表、默认引擎(curl_cffi)取到数据 —— 多半是 Yahoo 限流挡了裸 Session」；`stock_perp.sh:28` 独立记着 Yahoo chart 端点在本机稳定回 429。**在今天的串行负载下就已经在失败了。**

结论因此劈成两半：

- `technicals.py` 的 earnings 调用（`:433`）**不传 session**，走 curl_cffi 的浏览器指纹 TLS → 池化 6 路可行；
- `fetch_fundamentals.py` 的 `.info` 走那条脆弱 Session 打 crumb 门控端点 → **必须先跑一次限流探针再决定**，不能靠假设。

两个机械前提：

1. `HTTPAdapter` 的 `pool_maxsize` 要 ≥ worker 数（requests 默认 10，超了会静默 churn 连接）；
2. `fetch_fundamentals.py:210` 的全局 `_SESSION_OK`（`:231` 写、`:471` 读）**没有锁** —— 仓库今天零并发，没有任何一行是防御性写的。

**FRED 的约束是 header 不是语言**：**绝不能加浏览器 User-Agent**（实测 2026-09-05：Chrome UA → 25–30s 超时；`requests` 默认 UA 0.50s、curl 1.16s，两者都通）。`fred.sh` 留在 curl 是为了不引入 Python 套件依赖，不是因为 `requests` 打不通（`neocloud_credit_monitor.py:195-198` 记着：加 UA → 10s，不加 → 0.7s）。**不要「统一」这两边。**

### 5.7 绝不并发

| 单元                                            | 理由                                                                                                                                                   |
|-------------------------------------------------|--------------------------------------------------------------------------------------------------------------------------------------------------------|
| `etf_holdings.py` 档间 1.2s 间隔                | 那个 1.2 不是保守，是实测「1 req/sec 就会触发瞬时限流」（`:148`）。5 档齐发 = 每档掉进 1.5s→3.0s 退避梯，**更慢，还烧掉与真实周更共享的 25/日 配额**   |
| `technicals.py` → `perp_quotes.py --spot`       | 有序边（SKILL.md `:113`、`:119`），且失败静默：漏了 `--spot` 脚本照样 exit 0、照样打 perp 价，产出看似正常的空壳 🌙 节；休市日该节是当日唯一活数据     |
| `baseline.py` validate→diff→write               | `write` 是无备份原子覆写，git 是唯一回滚；半写文件被并发读取不可恢复                                                                                   |
| `snapshot.py` diff/write、Slack 推送            | 消费 `/tmp/today.json`（分析产物）；write 必须先于 Slack，使推送失败不会丢状态                                                                         |
| `neocloud_credit_monitor.py` 与自身或 lite 并发 | `:1153` 每次运行都 append，同日覆盖；两个并发运行争抢同一次 append，一份读数静默丢失                                                                   |
| 任何第二个并发 yfinance 进程                    | `market.py:1034-1036` 记着 Yahoo 限流时单次调用可挂数分钟。也因此 `fetch_fundamentals.py` 不得与 `etf_holdings.py` 第二级重叠 —— 两边打同样那 5 档 ETF |
| `recompute_margins` 的两次报表取数              | 该重试路径存在的原因就是 yfinance 在负载下不可靠。外层池化后，命中行的 recompute **必须留在自己的 worker 内串行**                                      |
| `hk_quote.py` 逐档 52 周日 K 循环               | N=3，值 1–2s；且它是腾讯→东方财富两级回退链，池化会交织回退尝试、使 `hi52_source` 标签难以推理。HK 档数长到 ~10 之前不动                               |

### 5.8 C 与 B 的冲突（一条，正确性的）

**`--quiet` 绝不能吃掉降级信号。** 今天「回退必须响」靠 stderr 上的 ⚠ 行（`fetch_fundamentals.py:231`、`market.py:294`/`:303`、`crypto.sh` 的 `mark_missing`、`hk_quote` 的 `hi52_source` 标签）。**并发会诱发限流**，所以这些信号在并发下更重要。

配套规则：

1. `--quiet` 只关 stdout 的人类渲染，stderr 保持活的；
2. 每个降级同时是 JSON 结构化字段，外加单元级 `degraded: bool`；
3. 调度侧：`wait` 之后，任何 `degraded=true` 或非零退出的单元，**必须在写任何报告之前先浮出来**。

否则被限流的并发运行会变成一次被静默正常化的运行 —— 正是 ⚪️ 会计和回退第 6 条要防的失败，只不过这次由「让它更快」这个改动触发。

---

## 6. 可移植性封装

- **SKILL.md 里只写能力，不写机制。** 照抄仓库既有的中立措辞（`a Slack MCP server`、`WebSearch / web_fetch`、`python3 + yfinance`、`jq`），把 A 写成「检索传输层（可选）」。**一旦出现 `@agent-name` 或 `mcp__…`，可移植层就死了。**
- 按仓库既有的六处样板落地（`compatibility:` 追加一句、第零步交代降级、正文注明），引用仓库自己的法律：「**少一路增强，不少一段交付**」。
- **降级**：没有传输层能力 → 父级自己搜，**同一份契约、同一个 JSON 文件**，报告字节相同。
- `.claude/agents/` 的定义**禁用 `isolation: worktree`**，理由写进 CLAUDE.md。三个日更状态档在 git 里的身份**并不相同**（`git ls-files` 实测），两种情形都坏，但坏法不同：

  | 状态档                                                   | git        | worktree 里会发生什么                                                                                                            |
  |----------------------------------------------------------|------------|----------------------------------------------------------------------------------------------------------------------------------|
  | `daily-risk-monitor/assets/last_run.json`                | **未跟踪** | 文件根本不存在 → 脚本判为「首次运行、无昨日基准」，`diff` 的日环比整段失效                                                       |
  | `daily-risk-monitor/assets/dominance_history.jsonl`      | **未跟踪** | 同上；信号 16 的 7d 腿基准点永远累积不起来                                                                                       |
  | `ai-pullback-daily/assets/neocloud_credit_history.jsonl` | **已跟踪** | 文件在，但内容是**最后一次 commit 的旧状态**；当日 append 落进临时树，随 worktree 一起蒸发 → 明天的 ⑩ 跨档变化对着一份过期基准比 |

  未跟踪的那两个会**响亮地表现成「首次运行」**；已跟踪的那个最危险 —— 它看起来完全正常，只是悄悄回到了过去。（注意 `.gitignore` 里并没有这些文件，它们只是从未被 `git add` 过。）
- **契约文档两份拷贝**（`ai-pullback-daily` / `daily-risk-monitor`）。CLAUDE.md 明令 `daily-risk-monitor` 独立、两者不得共享代码，故按 `TH` 双份字典的先例，在 CLAUDE.md 登记为**两处编辑**。

### 6.1 降级通知的位置（一个刻意的偏离）

**检索传输层缺席时的通知进运行输出，不进报告正文。**

理由：`AV_API_KEYS` 缺席会改变数据**口径**（全量 → top-N），所以必须进正文；检索传输层缺席时数据**字节相同**，只是搜索发生在别处。写进正文是噪音。

这偏离了「回退必须响」的字面，因此**该区分本身要明文写进 CLAUDE.md**（判据：降级是否改变数据口径），避免日后被侵蚀成「所有降级都可以不写进正文」。

---

## 7. 落地顺序

依赖是硬的，顺序不可换。

1. **B1 JSON 等价律** —— 写进 CLAUDE.md；修甲类 5 处、乙类 4 处；补丙类 4 处缺 JSON；补 `perp_quotes.py --quiet`；钉死 `--quiet` 语义（5.8）。
2. **三个非并发修复** —— 信用监控一次取数两次渲染（含双写 bug）；去掉 `--macro-only` 重复下载；`technicals.py` earnings 池化（保留 ⚠️EARN）。
3. **调度层** —— 5.4 的后台并发 + 与 A 的检索重叠。零脚本改动，吃掉大部分收益。
4. **B2 窄读** + **A 检索传输层**（契约文档 ×2 → SKILL.md 中立措辞 → `.claude/agents/` 可选定义 → README / CLAUDE.md 注记）。
5. **廉价的脚本内并发** —— `fred.sh` 多 URL curl、`stock_perp.sh`、信用监控 FRED 池、`crypto.sh` block 并发。
6. **限流探针** → 视结果决定 `fetch_fundamentals.py` `.info` 的池化。

### 7.1 建议拆成两个实施计划

本设计横跨四件事，一个实施计划装不下，且前后有硬依赖。切点在 **2 与 3 之间**：

- **计划一 = 步骤 1–2。** 纯正确性，零架构改动，**独立成立**：即使 A / C 从此不做，这批修复本身也值得落地（`cnn_fng.sh` 的硬阈值第 4 项、信用监控双写、earnings 池化）。改动集中在脚本内部与 CLAUDE.md。
- **计划二 = 步骤 3–6。** 架构层：调度、窄读、检索传输层、脚本内并发。全部依赖计划一的 JSON 自足与 `--quiet` 语义。

计划一交付并验证后再开计划二，避免在一个不自足的交接面上叠加并发。

---

## 8. 验证方法

仓库没有测试套件，按 CLAUDE.md 既有办法现场跑。

**B1 每个改过的脚本**：
```sh
python3 <script> --json /tmp/a.json --quiet   # JSON
python3 <script>                              # 人类输出
```
逐条核对：文本分支打印的每一个告警、禁令、判决、口径声明，JSON 里都找得到对应字段。甲类缺陷额外核对：显眼字段与真相字段不再矛盾。

**基准表不变性**（触碰 `baseline.py` 时必做）：
```sh
md5 skills/ai-industry-weekly/assets/baseline.md      # 前
python3 …/baseline.py write /tmp/bad.md               # 预期 exit 1
md5 skills/ai-industry-weekly/assets/baseline.md      # 必须字节相同
```

**往返无损**：
```sh
python3 …/baseline.py show > /tmp/t.md
python3 …/baseline.py validate /tmp/t.md   # exit 0
python3 …/baseline.py diff /tmp/t.md       # 「本周评级无变动」
grep -v '^| ASML ' /tmp/t.md > /tmp/bad.md
python3 …/baseline.py diff /tmp/bad.md     # exit 1，stdout 0 字节
```

**并发正确性**：并发版与串行版跑同一天，比对 JSON。字段值必须一致，**顺序也必须一致**（`fred.sh` 的 `series` 数组按 `$IDS`、`crypto.sh` 按固定 block 顺序）。

**路径泄漏**：从外来 cwd（`cd /tmp`，绝对路径调用）跑一遍，确认任何输出都不含本机绝对路径或用户名。

**降级响亮**：人为制造降级（断网、清空 `AV_API_KEYS`、喂坏 `universe.json`），确认 `degraded` 字段、stderr ⚠ 行、退出码三者一致，且调度层在写报告前先浮出来。

---

## 9. 明确不做的事

- 不把 skills 改成「取数 agent + 分析 agent」（第 1 节）。
- 不给 `ai-industry-weekly` 接检索传输层（1 个检索项）。
- 不并发 `etf_holdings.py`、不删 `crypto.sh` 的失败探针、不合并 FRED 与 yfinance 的传输层配置（5.7、5.6）。
- 不引入 ETF 持仓缓存（CLAUDE.md 既有决定）。
- 不给 `baseline.py` 加 `--force`、不加备份/快照文件（CLAUDE.md 既有决定）。
- 不收敛 `scrub()` 的 5 份拷贝与 `rel_display()` 的 3 份拷贝 —— 与本设计无关，不夹带。

---

## 10. 待确认的外部事实

`CLAUDE.md` 目前记载的 Slack 频道变量名是 `AI_INDUSTRY_SLACK_CHANNEL_ID` 与 `RISK_MONITOR_SLACK_CHANNEL_ID`，而三个 SKILL.md 已于 2026-09-04 改为统一的 `NOTIFICATION_SLACK_CHANNEL_ID`。**CLAUDE.md 该节已陈旧。** 本设计不含该修正（不夹带），但实施 B1 时会改动 CLAUDE.md，届时应顺手对齐。
