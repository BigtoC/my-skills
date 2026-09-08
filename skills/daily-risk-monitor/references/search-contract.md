# 检索传输契约 · 搜索项的返回形状

> **编者注 · 这一份不是逐字迁移。**
> `references/` 里其余九个文件都是自作者私有的 `daily-risk-monitor-v2.md` 逐字迁移的口径原文，
> 本文件不是——它是本技能自己的**交接面契约**，规定「一个检索单元回什么」，
> 好让 WebSearch / web_fetch 的**原始 snippet 永远不进写报告的那个上下文**。
> 因此本文件可以正常编辑维护；但它**引用**的每一条口径都必须回到那九个文件里核对，
> 契约不得改写口径，只规定口径怎么被搬运。

---

## 0. 为什么要有这一层

`SKILL.md`「脚本覆盖不到的信号」那一行列出的项目，全部靠 WebSearch / web_fetch 取。
原始搜索结果是**大段散文 + 广告 + 过期转载**，一项就可能几十 KB。
把它们直接倒进写报告的上下文，会同时产生两个后果：

1. 判定被 snippet 的措辞带着走——搜索结果自己就写着「市场情绪已极度贪婪」，
   而 `known-traps.md:20` 要求**触发状态严格按阈值判断，不加「但是」「不过」之类的软化语言**；
2. 来源与资料日期在几千字里被冲淡，而 `output-format.md:72` 要求**每项都必须标注资料日期**、
   `SKILL.md:148` 要求**每项附来源**。

所以检索被拆成**传输**与**判定**两件事：
传输层只回**读数与出处**，判定层（写报告的那个 agent）拿到的是结构化字段，不是 snippet。

---

## 1. 范围：18 项，按 governing reference 分 5 组

契约只覆盖**靠检索取数**的项。脚本能取的（FRED、market.py、cape.sh、cnn_fng.sh、
crypto.py 的 14/16/17、stock_perp）不进契约，照 `SKILL.md` 第 1 步原样跑。

| 组 | 加载的参考文件 | 项数 | 检索项 |
|----|----------------|------|--------|
| A | `references/signals-a-macro.md` | 4 | 2 200DMA 比例、3 BofA 牛熊、4 VIX 期限结构（**仅备援腿**）、6 A/D Line 顶背离 |
| B | `references/signals-b-positioning.md` | 6 | 7 NAAIM、8 AAII 多空差、10 CBOE 权益 Put/Call、11 FINRA Margin Debt、12 内部人买卖比、13 IPO 发行量 |
| C | `references/signals-c-crypto.md` | 3（实际派发 2）| 14 资金费率（**仅第三级兜底**）、15 24h 清算、16 BTC Dominance（**禁止检索，见 §9.C**） |
| E | `references/signals-e-cycle-valuation.md` | 4 | 25 LEI、27 Buffett（**仅对照**）、29 AAII 家庭配置、30 Margin Debt/GDP |
| F | `references/signals-f-monday.md` | 1 | 31 Forward P/E（**仅周一**） |

**一组只加载一个参考文件。** 分组按 governing reference 而不是按主题，
就是为了让每个单元只需要读一份口径文件——搜索顺序、阈值、口径陷阱、抓不到时的强制处理，
都写在同一份里。跨文件的项（例如信号 30 的自算路径要用到信号 11）在 §9.E 单独交代。

---

## 2. 公共信封（每项都有）

```
item_id, name
status               : ok | missing | not_applicable
tier_used                # 文档化来源顺序里，实际答上来的是第几级
attempted[]              # {source, url, http, outcome} —— 成功时也要有
source_label, source_url
as_of
as_of_granularity    : trading_day | survey_week | month | quarter | meeting_date
caliber                  # 口径标签：8h vs 4h 资金费率、Equity vs Total put/call、百万 vs 十亿
threshold_comparable : {value: bool, reason: str}
carry_forward_policy : must_carry | must_refetch | n_a
last_known           : {value, date} | null
staleness            : {n, unit: "weeks" | "days"} | null
missing_sentinel     : "⚪️" | "N/A" | null        # 由调用方指定，见 §4
counts_toward        : {numerator: bool, denominator: bool}
payload                  # 逐项定义，见 §6
```

逐字段的硬要求：

| 字段 | 要求 | 依据 |
|------|------|------|
| `attempted[]` | **成功时也要有**，按文档化顺序排列。父级不看 snippet 就能审计「写死的来源顺序有没有被真的走过」；第一级没被尝试就打回重取 | `SKILL.md:148`「逐项按各 `references/signals-*.md` 里写明的搜索顺序与来源优先级执行」 |
| `attempted[].http` | 实测到的 HTTP 码要照填。403 / 418 / 451 这些是本仓库已知陷阱清单上的常客，父级要能在第 8 部分列出来 | `known-traps.md:57-70`、`output-format.md:103` |
| `as_of` + `as_of_granularity` | 两个都必填。粒度不是装饰：信号 11 必须报**月份**、信号 27 必须报**季度**、信号 31 必须报**是哪一个周五那期 FactSet**。粒度丢了，滞后就算不出来 | `signals-b-positioning.md:54`、`signals-e-cycle-valuation.md:51-61`、`signals-f-monday.md:14` |
| `caliber` | 口径标签**跟着数字走**，不是注释。Equity vs Total、8h vs 4h vs 1h、USA Overall Market vs openinsider、百万 vs 十亿 | `signals-b-positioning.md:45`、`signals-c-crypto.md:55-67`、`signals-b-positioning.md:64-73` |
| `staleness` | **只要 `last_known` 有值就必须有**——没有滞后周数的「数据暂缺」是不合格输出。**唯一的例外是 `last_known` 本身为 `null`**（首跑、或该项从来没有过基准）：此时滞后**在数学上算不出来**，`staleness` 记 `null`，并在 payload 写死 `signals-a-macro.md:41` 那句「**无历史基准，本项完全不可判定**」。**绝不允许为了满足本栏而编一个周数**——那是红线一，而且 §8 已说明这些字段是仓位输入。 | `known-traps.md:15` ＋ `signals-a-macro.md:41` |
| `last_known` | `{value, date}`；查不到写 `null`，并在 payload 里说明「无历史基准，本项完全不可判定」 | `signals-a-macro.md:40-41` |
| `attempted[]` 长度 | 检索不重试超过 2 次 | `data-cadence.md:21` |

`staleness` 与 `last_known` 是一对：速览卡要把它们渲染成一行内联文字
「⚪️ 无法判定（上次 6.2 @07-18，滞后 3 周）」（`output-format.md:42`），
所以传输层给的是**两个字段**，不是一句已经拼好的话。

---

## 3. 两个必须解释的槽位

这两个槽位不是为了整齐加上去的。少任何一个，整层白做。

### 3.1 `threshold_comparable` —— 有值 ≠ 可比阈值

信号 12 可以从 openinsider.com 取回一个**真实、新鲜、解析正确**的数字，
而它**仍然必须报 ⚪️ 数据暂缺**——因为 `0.17` 是**为 GuruFocus 的特定算法校准的**
（美元加权、全市场汇总、月度口径），openinsider 用的是另一套算法，数值尺度完全不同
（`signals-b-positioning.md:64-73`）。原文把话说到底：

> **用替代源的数去比对为原口径校准的阈值，和把 WALCL 的「百万」当成「十亿」是同一类错误**——数字看起来很合理，结论完全错。

没有这个显式布尔，下游只会看到「有一个数」，于是把它当成「阈值可判定」，
⚪️ 会计就**静默塌缩成 ❌**——而 `decision-framework.md:44` 说得很清楚，
`❌ 未触发` 和 `⚪️ 无法判定` 是完全不同的两件事：一个是「查过了，安全」，一个是「不知道」。

同型的还有三处，都写进 `reason`：

| 项 | 有值也不可比阈值的情形 | 依据 |
|----|------------------------|------|
| 信号 12 内部人 | 数来自 openinsider / SEC EDGAR Form 4 / 口径不明的转载 | `signals-b-positioning.md:64-73`、`SKILL.md` 红线一 |
| 信号 10 Put/Call | 拿到的是 **Total** 而不是 **Equity** | `signals-b-positioning.md:45` |
| 信号 14 资金费率 | 第三级 coinglass 搜到的是跨所或非 8h 口径 | `signals-c-crypto.md:55-67`、`:90-92` |
| 信号 27 Buffett | 数来自 currentmarketvaluation / gurufocus 而不是同季对齐的 FRED 计算 | `signals-e-cycle-valuation.md:47-61` |

`threshold_comparable.value = false` 时，`status` **仍可以是 `ok`**（数字是真的、抓到了），
但 `counts_toward` 必须是 `{numerator: false, denominator: false}`，
且 payload 里要带上可以照抄进报告的那句限制语——信号 12 的原文规定是
「**口径不同，无法判定第 7 项硬阈值**」（`signals-b-positioning.md:77`）。

### 3.2 `carry_forward_policy` —— 沿用是必须还是缺陷，只看滞后分不出来

同一个契约里既有**必须沿用**的项（`as_of` 是会议日期那种，陈旧本身就是正确状态），
也有**沿用即缺陷**的项。只看 `staleness` 两类长得一模一样：都是「一个旧日期」。

**本技能这 18 项目前全部是 `must_refetch`**，只有一个例外分支：
信号 31 非周一时不派发，记 `n_a`（`signals-f-monday.md:9-11`，仅周一执行）。

理由必须写下来，否则这一栏看起来像可以省：

- `data-cadence.md:11`：**全部 30 项每天都抓**（「宁可多花时间，也不要用过期数据做判断」）；
- `data-cadence.md:20-21`：**非更新日抓到相同值 = 正常**，标 `as of MM/DD`，
  但**抓不到就标「⚪️ 数据暂缺」，绝不用旧记忆或估算值填充**。

这两条合起来意味着：信号 11（月频、滞后约一个月）连续二十天回同一个数字是**正常**的，
但那是「今天又去取了一次、取回同一个数」，**不是**「沿用昨天的数」。
**「重复取到同一个值」与「沿用」在报告里长得一样，在契约里必须分开**——
前者 `status: ok` + 今天的 `attempted[]`，后者根本不允许发生。

`last_known` 的用途因此也被限死：它**只用来算 `staleness`、渲染速览卡那句话**，
**不得**被提升成本次的 `payload.value`。

---

## 4. 缺失哨兵由**调用方**指定，不由取数方选

本技能同时在用三个哨兵，会计规则各不相同：

| 哨兵 | 用在哪 | 会计规则 |
|------|--------|----------|
| `⚪️` | 信号档位、7 项硬阈值那一格 | **既不进分子也不进分母**（N = 7 − M），但必须在完整版列出、必须带滞后周数 |
| `N/A` | 一行里**某个子字段**缺（如 CNN F&G 的四个对照读数之一）：该读数印 `N/A`、变动栏印「N/A（缺对照读数，不计算变动）」 | 该行**仍是 ok**，只是变动栏不计算；**不补 0、不估算** |
| `null` | `--json` 机读面的三态 | `null` = ⚪️（分子分母都不进）；`false` = ❌（进分母不进分子）。**把 `null` 读成 `false` 就是把「不知道」记成「查过了没事」** |

依据：`decision-framework.md:42-51`、`SKILL.md:168`、`scripts/cnn_fng.sh:166-175`。

所以 `missing_sentinel` 是**调用方在派发时填进任务里的**，传输层照填回来。
取数方不知道这个读数最后要落进硬阈值表格、仪表盘还是数据品质附注，
**让它自己挑符号，就是让它替调用方决定会计规则**。

---

## 5. 明确排除在契约之外

传输层**不返回**：

- 任何 🟢 / 🟡 / 🔴 状态；
- 任何阈值比较（「已触发」「接近阈值」「低于 50bps」都不行）；
- 任何「一句话解读」；
- 任何叙事散文、任何 snippet 原文的整段搬运。

**用 schema 强制，不是靠提示词自律**：返回结构里**没有这些字段**。
一个只能填 `payload.value` 的槽位，填不进「已触发」。

理由是分工：判定的规则写在 `signals-*.md` 与 `decision-framework.md` 里，
读的是**全部 30 项加上下文**（Tier 1 计数、加密计数、估值环境联动）。
一个只看着一项搜索结果的单元，没有做那个判定所需的任何一样东西。
`output-format.md:69` 那个 ≤25 字的「一句话解读」同理——它要求
「用**大白话**说明这个数字现在代表什么，不要重复阈值」，那是写报告的人的活。

唯一的例外是**照抄型文字**：原文规定必须逐字出现在报告里的限制语
（信号 12 的「口径不同，无法判定第 7 项硬阈值」、信号 3 的「无历史基准，本项完全不可判定」、
信号 15 的三件事清单），可以放进 payload——它们是**口径文件里写死的措辞**，不是判定。

---

## 6. payload 族（本技能用到六族）

统一标量元组装不下其中一半。族由派发时指定，取数方不得自行降级成标量。

| 族 | 项 | 形状 |
|----|----|------|
| **标量** | 7 NAAIM、10 Put/Call、25 LEI、29 AAII 配置、30 Margin Debt/GDP、31 Forward P/E、27 Buffett（对照） | `{value, unit}` |
| **多值** | 11 Margin Debt | `{abs, yoy_pct, mom_direction, three_month_streak[]}` |
| **多值** | 8 AAII 多空差 | `{bull, bear, spread, weeks_above_30}` |
| **背离裁决** | 6 A/D Line、2 200DMA 比例 | `{current, prior_peak, peak_date}` + 「SPX 是否创新高」布尔——**已由脚本产出**：`market.py --json` 的 `meta.spx_new_high.at_new_high`（口径见同栏 `caliber`：252 交易日**收盘**新高，非盘中高点）。检索侧只取 A/D 线／比例本身，**不要自己去搜 SPX 有没有创新高** |
| **记录表** | 13 IPO | `readings[]`（件数腿、金额腿各一）+ 集中度 + 剔除最大单后的重算 |
| **同源对** | 4 VIX 期限结构 | `{near, far, shared_source, shared_date}` |
| **纯方向** | 3 BofA、12 内部人 | `{direction_text}` 且 `status = missing` |

逐族的理由：

**多值 · 信号 11** ——「**连续 3 个月**月减」（`signals-b-positioning.md:53`）是硬阈值第 2 项，
一个标量答不了「连续 3 个月」。原文另外强制**必须同时给出三个数**：
最新月度绝对值、年增率 YoY %、月增方向 ↑/↓（`:49`）。`three_month_streak[]` 是那三笔月度读数本身，
不是一个已经算好的布尔——布尔是判定，判定不在这一层。

**多值 · 信号 8** ——「连续多周 >30」（`:16`）需要持续性；
原文另外强制**同时记录 Bullish %、Bearish %、多空差三个数**（`:17`）。
`weeks_above_30` 是观察到的周数，单周一个数字**不足以**说它触发。

**背离裁决 · 信号 6 / 信号 2** —— 这两项的触发条件都是**条件式**的：
⚠️ **前提为假时记 ❌，不是 ⚪️。** `at_new_high = false` 表示「创新高」这个前提不成立，
两项触发因此**确定未触发** —— 记 ❌ 并照常进分母。只有 `at_new_high = null`（取不到 ^GSPC
序列）才记 ⚪️、才从分母扣除。把「前提不成立」误记成 ⚪️，硬阈值第 5 项就会**每天**被扣掉，
分母恒为 6、最坏情况恒被抬高 1 —— 那是 2026-09-05 实跑真正发生过的事。

信号 6 是「**SPX 创新高，但 A/D Line 未同步创新高**」（`signals-a-macro.md:76`），
信号 2 是「SPX 创新高但该比例 <60%」（`:26`）。
所以搜索回来的**不是一个可判定的读数**，而是背离的一条腿。
另一条腿——「SPX 今天是不是创新高」——是价格事实，**必须由父级从脚本侧供给，不得由搜索回答**。
搜索结果里的「标普再创历史新高」是一句转载散文，日期与口径都不受控，
拿它当布尔，就是拿 snippet 直接驱动一项硬阈值（信号 6 是第 5 项）。
⚠️ **今天没有任何脚本字段直接给这个布尔**：`market.py` 算的是 σ 倍数、VRP、跨资产、广度、
趋势机制（信号 19–22、26、33–34），里面没有「新高」这一项。
所以父级要么自己从价格序列判、要么就承认判不了——**判不了时这两项记 ⚪️**，
不得因为搜索腿抓到了就把整项当成已判定。

**记录表 · 信号 13** —— 两个**同阈值却互相矛盾**的口径，原文有实测证据
（`signals-b-positioning.md:89-95`）：2026-08-10 实测 2026 年至今 99 宗募 $251B，
对比 2025 全年 202 宗募 $44B → **件数在减少，金额 +470%**；
「只看件数会判 ❌ 未触发，只看金额会判 ✅ 触发」。
所以 payload 是 `readings[]`（每条自带 `caliber: "件数" | "金额"`），
外加集中度（最大单案占比，2026 年 SpaceX 一家 $85.7B 约占三分之一）
与**剔除最大单案后的金额同比重算**。
**任何一条腿单独回来都不构成本项的状态**，也不能把两条腿合并成一个数。

**同源对 · 信号 4** —— 两腿必须**同源同日**。
一个「一值一 `as_of`」的元组表达不了这个约束，而这正是
`signals-a-macro.md:5-9` 那条编者注要防的事（同一份报告里出现两个 VIX、资料日期常差一个交易日）。
`data-cadence.md:53` 给了它的具体死法：yfinance 的 `^VIX3M`/`^VIX9D`/`^VIX6M`
**全部停更在 2026-07-17** 而 `^VIX` 是当日的，拿它算期限结构会
**静默地用三周前的远月值去比今天的近月值**。
`shared_source` / `shared_date` 两个字段就是让这种事**变成显式的校验**而不是隐式的巧合。

**纯方向 · 信号 3 / 信号 12** —— 这两族的存在本身就是结论：
可以回一句有用的话，同时 `status` 仍是 `missing`。
信号 12 允许补一句 openinsider 的**方向性**观察（如「近两周集中卖出为主」），
但**必须紧接着写「口径不同，无法判定第 7 项硬阈值」**（`signals-b-positioning.md:77`）。
信号 3 在连 `last_known` 都取不到时，要能表达
「无历史基准，本项完全不可判定」（`signals-a-macro.md:41`）。
`direction_text` 就是这句话的槽位；它**不是** payload.value 的替代品，
`threshold_comparable.value` 在这一族里恒为 `false`。

---

## 7. 分派与交接

### 7.1 一组写一个 JSON 文件

每组把结果**写成一个 JSON 文件**，返回消息**只有一行**：

```
wrote 6 items to /tmp/drm-search-b.json, 4 ok / 2 missing
```

- 返回消息**绝不**渲染文件内容——不贴表格、不贴摘要、不贴任何一项的读数。
  一行渲染回来，snippet 就又进了父级上下文，这一层就白做了。
- 父级**读文件**，不读返回消息里的数字（那一行只是给人看的收据）。
- 一组一个文件，文件名按组：`/tmp/drm-search-{a,b,c,e,f}.json`。
  组之间不共享文件，也不追加进同一个文件——并发时会互相踩。

### 7.2 派发清单

| item_id | 信号 | 组 | payload 族 | 硬阈值 | 派发说明 |
|---------|------|----|------------|--------|----------|
| `drm-sig2-pct-above-200dma` | 2 200DMA 比例 | A | 背离裁决 | — | 需父级供 SPX 新高布尔 |
| `drm-sig3-bofa-bull-bear` | 3 BofA 牛熊 | A | 纯方向 / 标量 | **第 6 项** | 搜索顺序最多 2 轮 |
| `drm-sig4-vixcentral-backup` | 4 VIX 期限结构 | A | 同源对 | 第 1 项（VIX 绝对值） | **仅当 FRED 取不到才派发**；回退必在 warnings 明示不同源 |
| `drm-sig6-nyse-ad-line` | 6 A/D Line | A | 背离裁决 | **第 5 项** | 需父级供 SPX 新高布尔 |
| `drm-sig7-naaim` | 7 NAAIM | B | 标量 | — | 周三更新 |
| `drm-sig8-aaii-bull-bear` | 8 AAII 多空差 | B | 多值 | — | 周四更新；三个数缺一不可 |
| `drm-sig10-cboe-put-call` | 10 Put/Call | B | 标量 | — | 必须 Equity 口径 |
| `drm-sig11-finra-margin-debt` | 11 Margin Debt | B | 多值 | **第 2 项** | 必须标注数据所属月份 |
| `drm-sig12-insider-buy-sell` | 12 内部人 | B | 纯方向 / 标量 | **第 7 项** | GuruFocus 403 是已知实测结果 |
| `drm-sig13-ipo-issuance` | 13 IPO | B | 记录表 | — | 两个口径分别报 |
| `drm-sig14-funding-websearch-tier3` | 14 资金费率 | C | 标量 | — | **仅第三级兜底**（Binance → Hyperliquid → 本项） |
| `drm-sig15-liquidations` | 15 24h 清算 | C | 多值 | — | `crypto.py liquidations` **一定 exit 3**，那是正常结局 |
| `drm-sig16-dominance-antisearch` | 16 BTC Dominance | C | — | — | **不派发。禁止检索，见 §9.C** |
| `drm-sig25-conference-board-lei` | 25 LEI | E | 标量 | — | 要的是 6 个月年化变化率 |
| `drm-sig27-buffett-crosscheck` | 27 Buffett | E | 标量 | — | **仅对照**，不得替代 FRED 计算 |
| `drm-sig29-aaii-allocation` | 29 AAII 家庭配置 | E | 标量 | — | 与信号 8 是不同调查，不可互换 |
| `drm-sig30-margin-debt-to-gdp` | 30 Margin Debt/GDP | E | 标量 | — | 两条路径必须标明用了哪条 |
| `drm-sig31-forward-pe-monday` | 31 Forward P/E | F | 标量 | — | **仅周一**；无阈值、无档位，见 §9.F |

「派发说明」里的**角色**（备援腿 / 第三级兜底 / 仅对照 / 不派发 / 无档位）
不是信封里的字段——它是**派发时的属性**，写在这张表里。
需要在返回值里可见时，用信封既有的字段表达：

- **仅对照**（信号 27）→ `threshold_comparable {value: false, reason: "对照源，非同季对齐的 FRED 计算，不得比 200%"}`
  且 `counts_toward {false, false}`；
- **无阈值无档位**（信号 31）→ `threshold_comparable {value: false, reason: "本项无触发阈值"}`
  且 `counts_toward {false, false}`；
- **不派发**（信号 16）→ 根本不产生单元，组 C 的文件里没有这一项。

### 7.3 可审计性

父级收到文件后，逐项检查两件事，不合格就打回重取：

1. `attempted[]` 的第一条，是不是该项参考文件里写死的第一级来源？
   **第一级没被尝试就打回**——这正是 `attempted[]` 成功时也要有的原因。
2. `tier_used` 与 `source_label` 对不对得上？降级了但 `tier_used` 还写 1，
   就是 `SKILL.md:14-16` 里「回退必须响」那条被违反。

---

## 8. 四项硬阈值：这些字段是仓位输入，不是记账

18 项里有**四项同时是 7 项硬阈值那张表上的行**：

| 硬阈值 # | 信号 | 阈值 |
|----------|------|------|
| 第 2 项 | 11 Margin Debt | 连续 3 个月月减 |
| 第 5 项 | 6 A/D Line | SPX 新高但 A/D 未创新高 |
| 第 6 项 | 3 BofA Bull & Bear | >8.0 |
| 第 7 项 | 12 Insider Buy/Sell | <0.17 |

（另外信号 4 的 VIX 绝对值是第 1 项，但那一项正常走 FRED，只有备援腿会进本契约。）

任何一项 ⚪️ 都**改变分母**：`decision-framework.md:46` ——
**⚪️ 项一律不计入触发数，也不计入分母**，写法是「今日共 X / N 项触发（M 项数据暂缺）」，N = 7 − M。
并且 `:47` 强制**同时给出最坏情况**：「**若暂缺的 M 项全部触发，计数将达 X+M**」，
还要说明那会不会跨过警戒升级（≥2）或分批门槛。
`:48` 再加一条：**⚪️ 项 ≥3 时**，战略基准**维持昨日档位不变**，不因计数下降而回补仓位。

顺着这条链往下：触发数 T → 战略基准（`decision-framework.md:73-83`）→ 最终目标仓位。

**所以 `status` / `threshold_comparable` / `counts_toward` / `staleness` 这几个字段
是仓位输入，不是记账。** 一个单元把 `threshold_comparable` 填成 `true` 而它其实不可比，
分母就多一格，触发比例被压低，战略基准被推高——
`known-traps.md:14` 说的就是这件事：**编造的数字会直接改变战略层的目标仓位基准**。

`decision-framework.md:49`：**绝不允许**因为「其余几项都很安全」就推断暂缺项也安全。

---

## 9. 逐组备注（派发前必读的口径细则）

### 组 A · `signals-a-macro.md`

- **信号 2**（`:24-27`）：来源写的是 barchart.com 或 stockcharts，搜
  `"S&P 500 stocks above 200-day moving average"`。两家是并列的，不是排序；
  但换第三家仍受 `SKILL.md:14-16`「阈值不随源转移」约束——成分口径/调整方式不同的源不得静默替入。
- **信号 3**（`:29-43`）：搜索顺序是写死的三段、**最多 2 轮**：
  ① `BofA Bull Bear Indicator` + 当前月份／`Flow Show`；
  ② ZeroHedge、FT Unhedged、Reuters、Business Insider、MarketWatch；
  ③ `"Bull & Bear Indicator" site:x.com`。
  抓不到时的强制处理是原文写死的：**不得标 🟢，也不得填任何数字**；
  必须写出最后一次已知读数的日期与值并算出滞后几周；连上次读数都查不到就写
  「无历史基准，本项完全不可判定」。转载之所以可用，只因为它转载的是**美银自己那个数**——
  任何别家的牛熊合成指标都不得拿去比 8.0。
- **信号 4**（`:45-53`）：首选是 `fred("VIXCLS")` vs `fred("VXVCLS")`，
  「两者同源同日，比 yfinance 与 vixcentral 都可靠」。**只有首选失败才派发** vixcentral 备援腿，
  且降级必须响。VIX 绝对值那条硬阈值要「>25 且**连续 3 个交易日**站稳」——需要三天历史，不是一个读数。
- **信号 6**（`:74-77`）：搜 `"NYSE advance decline line divergence [当前月份]"`，或 stockcharts `$NYAD`。
  两条腿必须来自**同一套 A/D 构造**——拿甲家的历史峰值去比乙家的当前值，
  和信号 12 的 0.17 是同一类错误。

### 组 B · `signals-b-positioning.md`

- **信号 10**（`:41-45`）：走 cboe.com/us/options/market_statistics 的 web_fetch。
  **`cdn.cboe.com` 的 PCRATIO CSV 会回 403，不可用**（`known-traps.md:63` 同载）。
  口径必须是**权益（Equity）**——总和里混了指数期权，机构大量拿来对冲，会把散户情绪讯号糊掉。
  拿到 Total 就填 `threshold_comparable {value: false}`，不是把它当 Equity 用。
- **信号 11**（`:47-55`）：来源顺序是 macromicro 镜像页 web_fetch，或搜 `"FINRA margin statistics"`。
  三个数缺一不可，且**必须标注数据所属月份**（月频、滞后约一个月）。
  与信号 30 之间有单位耦合：**Margin Debt 单位通常是百万，GDP 是十亿**。
- **信号 12**（`:57-80`）：**唯一可用于判定阈值的口径**是 gurufocus 那一页的
  **USA Overall Market**；2026-08-10 实测该页对爬虫回 **HTTP 403**。
  替代源各自能做什么、**不能**做什么，原文有一张表（`:64-73`）：
  openinsider ✅ 200，只能看**方向**，❌ 算出比值去比 0.17；
  SEC EDGAR Form 4 ✅ 200（UA 需填自己的邮箱，SEC 规定），只能看原始流水；
  Barron's / InsiderScore 转载，**明确写明是 GuruFocus 口径才可用**。
- **信号 13**（`:82-96`）：走 renaissancecapital.com/IPO-Center/Stats。
  报告要求写成两行 `件数 X 宗（同比 ±Y%）` / `金额 $Z（同比 ±W%，其中最大单案占 V%）`，
  所以两条腿加集中度必须分开返回；剔除最大单案后的重算也要带回来
  （若扣掉后就不触发，报告要明说「触发由单一案件驱动」）。

### 组 C · `signals-c-crypto.md`

- **信号 14**：兜底顺序是写死的
  `Binance` → `Hyperliquid` → `web_search coinglass` → 标注「数据暂缺」（`:90-92`）。
  **本契约只覆盖第三级**；前两级由 `crypto.py` 走完再说。
  第二级的触发时机是 Binance 回 `451` / `403`、超时、或字段缺失。
  拿到第三级的数必须**先换算再比阈值**（`:55-67`）：阈值 0.05% 是 **8 小时口径**，
  Binance 默认 8h 直接用、部分币种 4h **× 2**、Hyperliquid **1 小时 × 8**；
  报告里**同时给出 8h 费率和年化**（`年化% = 8h费率 × 3 × 365`）。
  跨所不可直接跨日比较——「不同交易所费率可以差一倍」，所以 `caliber` 与 `source_label` 都必填。
- **信号 15**：`crypto.py liquidations` **一定 exit 3**（`scripts/crypto.py:1392` `do_liquidations`），
  这是它的正常结局，不是故障——本项没有任何免费公开源（Coinglass v4 需 API key）。
  脚本会实测并印出每个来源的 HTTP 码，那份清单**直接充当 `attempted[]`**。
  接到 exit 3 就走 `web_search "coinglass liquidations 24h"`，报告要写全三件事（`SKILL.md:140`）：
  24h 总清算金额（>$500M = 杠杆洗盘｜>$1B = 重大事件）、
  **多头 vs 空头哪一方被清算更多**、以及「上次已知读数 X @ YYYY-MM-DD，已滞后 N 周」。
  ⚠️ **搜不到也不得写成「未触发」**——本项即使 `status: missing` 也要照 §2 报滞后周数；
  但**首跑没有 `last_known` 时算不出滞后**，那时 `staleness` 记 `null` 并写「无历史基准，本项完全不可判定」，
  **不要为了填满这一栏而编一个周数**。>$1B 单独就会把告警拉到 🔴（`decision-framework.md:18`）。
- **信号 16**：**不派发，禁止检索。**
  正文（`:100-103`）写的「搜 "BTC dominance" 或 web_fetch coingecko / tradingview」
  **已被该文件开头的编者注收紧**（`signals-c-crypto.md:5-11`）：
  CoinGecko 免费层无全市场市值历史序列（`/global/market_cap_chart` 实测 HTTP 401），
  **换到别家会引入第三套分母口径**——CoinGecko 与 CoinPaprika 实测同日 59.1% vs 56.9%，
  差约 2pt，**而阈值只有 2%**。因此 `crypto.py` 改为按天累积同源本地历史
  `assets/dominance_history.jsonl` 自答 7d 腿，并强制校验基准笔与今日**同源**，异源一律拒绝比较记 ⚪️。
  原文的话是：**「不要为了补这条腿去换数据源 —— 缺的是历史序列，不是当日值。」**
  ⚠️ `crypto.py` 在两家全灭时确实会印一句 `web_fetch coingecko / tradingview BTC.D` 的 next_step
  （`scripts/crypto.py:1969`）——那是**当日值**的兜底，**不是**给 7d 腿开的口子。
  三种 ⚪️（历史不足 / 历史断层 / 来源不同）的措辞要照抄脚本，
  且**绝不能因为「其余条件都正常」就推断这条腿安全**（`SKILL.md:141`）。

### 组 E · `signals-e-cycle-valuation.md`

- **信号 25**（`:24-28`）：走 conference-board.org/topics/us-leading-indicators。
  要**两个数**：最新月度指数值 + **6 个月年化变化率**；触发只看后者（< −4%）。
  「单月的绝对值没意义，要看**六个月的斜率**」——一个换了窗口的成长率（3 个月、同比）
  不得拿去比 −4%。`known-traps.md:17` 把 LEI 列进「尤其容易凭印象写出一个『差不多的数』」那一组：
  **这几项每次都必须给出实际抓取到的数值来源**。
- **信号 27**（`:47-65`）：首选是**可算**的 `fred("NCBEILQ027S") ÷ fred("GDP")`，
  且 `SKILL.md:174` 规定走 `fred.sh --buffett`（内建同季对齐），别自己各取末行相除。
  本契约里那两个网页源（currentmarketvaluation、gurufocus）**是「对照」，不是替代**。
  对照取不到**不会**让信号 27 变成 ⚪️；对照与首选打架时，进
  `output-format.md:103` 第 8 部分的「**来源冲突的项目**」，不进仪表盘那一格。
  首选自身量级自检不过时是 `exit 4`，那时**不要引用那个数字**（`SKILL.md:145`）。
- **信号 29**（`:86-89`）：走 aaii.com/assetallocationsurvey——
  与信号 8 的 aaii.com/sentimentsurvey 是**不同的调查、不同的页面、不同的量纲**，
  「嘴巴会骗人，帐户不会」，两者**永不互相替代**。
- **信号 30**（`:91-94`）：两条路径——gurufocus 搜 `"margin debt to GDP"`，
  或用信号 11 的绝对值 ÷ `fred("GDP")` 自算（**注意 Margin Debt 单位通常是百万，GDP 是十亿**）。
  `source_label` 必须说清是**哪条**路径产的；两条路径是两套口径，跨日比较前要先看这一栏。
  若信号 11 是 ⚪️，自算路径同样不可用——**不得用残缺数据凑出一个数**，记 ⚪️ 并写明原因。

### 组 F · `signals-f-monday.md`

- **信号 31**（`:13-16`）：搜 `"S&P 500 forward P/E FactSet Earnings Insight"`
  （FactSet 每周五发布 PDF/网页），`as_of` 要说清是**哪一期**。
  参考区间 5 年均值约 19–20、10 年均值约 18——那是**参考**，不是触发阈值。
- ⚠️ **本项没有触发阈值，也没有档位。** `signals-f-monday.md:11` 与 `SKILL.md:152`：
  这四项**不计入 30 个信号，也不参与任何触发计数**。
  因此 `counts_toward` 恒为 `{false, false}`，且**绝不可写进 `snapshot.py` 的 `signals`**——
  该脚本只认 1–30（`scripts/snapshot.py:122`），
  混入 31–34 会被点名拒绝（`:312-313`：「31–34 是周一附加，不计入 30 个信号，不要放进 signals」）。
- 非周一不派发本组，`carry_forward_policy` 记 `n_a`。

---

## 10. 交件自检（每组写完文件后逐条过）

- [ ] 文件里的项数 = 派发清单里该组的项数（组 C 是 2 项，信号 16 不产生单元）
- [ ] 每一项都有 `attempted[]`，**成功的项也有**，且第一条是该项写死的第一级来源
- [ ] 每一个 `status: missing`：**有 `last_known` 就必须有 `staleness`**（单位写明）；
      `last_known` 为 `null`（首跑／从无基准）时 `staleness` 一并记 `null`，payload 写「无历史基准，本项完全不可判定」。
      **两者都不许靠编数字来填满**
- [ ] 每一项都有 `as_of` **与** `as_of_granularity`
- [ ] 每一项都有 `caliber`，且口径陷阱项（10、11、12、14、27、30）的 `caliber` 与阈值口径一致
- [ ] `threshold_comparable.value = false` 的项，`counts_toward` 是 `{false, false}`，且 `reason` 可直接抄进报告
- [ ] 没有任何 🟢🟡🔴、没有任何阈值比较、没有任何「一句话解读」、没有 snippet 原文
- [ ] `missing_sentinel` 用的是**派发时给的**那一个，不是自己挑的
- [ ] 返回消息只有一行：`wrote N items to <path>, M ok / K missing`——**没有渲染任何内容**
