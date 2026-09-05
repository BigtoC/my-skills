# 检索传输层契约（Search Transport Contract）

> **本文件不是逐字迁移。** 本目录其余 `*.md` 均自 `AI算力产业链回调进场监控日更.md` 逐字迁移、只可改必须改的那一行；
> 本文件是本技能新增的**接口定义**，可以正常编辑。
> 但文中所有 `「」` 引号内的文字是从那些迁移文件里**原样引用**的口径，改动即等于改动原文——
> 只能连同来源文件一起改，不得在本文件里单方面改写或「顺手统一措辞」。

> 设计出处：`docs/superpowers/specs/2026-09-05-skill-handoff-and-concurrency-design.md` 第 4 节（A · 检索传输层）。
> 本文件是该节对 `ai-pullback-daily` 的落地版，不重新设计。

---

## 0. 这层解决什么

WebSearch 的原始 snippet 一次 40–120 KB。它们进了写报告那个上下文，就再也出不去：
后面每一次判档、每一次分桶，都在跟一堆没结构、没日期、没口径标签的散文抢注意力。

**检索传输单元（search-transport unit）**把这件事切开：

> 取数侧读 governing reference、按文档化的来源顺序检索、把**读数与出处**写成结构化 JSON；
> **判断留给写报告的那个 agent。**
> 原始 snippet 死在取数侧，一个字都不进报告上下文。

一个「传输单元」= 本清单里的一项（item）。本技能共 **21 项**。

**引爆点④ 不在其中。** ④ 的状态由 `scripts/neocloud_credit_monitor.py` 产出，
`SKILL.md` 第三步逐字：「**第④项是唯一例外**：其状态**由 `neocloud_credit_monitor.py` 脚本产出**，
直接引用脚本输出的第⑨块，**不另行人工判读、不得与脚本结论冲突**」。
把 ④ 做成检索项，就是给一个已有唯一权威的判定造第二个来源——**永远不要把它加进本契约**。
（④ 的**输入**——债券报价、一级市场条款、`manual_flags`——是检索项，见 D 组；输入与判定是两回事。）

---

## 1. 明确排除在契约之外（先读这条）

传输层**不返回**下列任何一种东西：

| 不返回                                | 为什么                                                                       |
|---------------------------------------|------------------------------------------------------------------------------|
| 任何 🟢🟡🔴 状态                        | 档位由 `tripwires.md` 的定性规则判定，判定权在写报告的 agent                 |
| 任何阈值比较（「>600bp」「≤2 交易日」） | 阈值与口径绑定，比较必须在能看见 `caliber` 与 `threshold_comparable` 的地方做 |
| 任何「一句话解读」「说明栏」文字        | 那是报告的判读产物，不是读数                                                 |
| 任何叙事散文、任何「建议」「前瞻」      | 同上                                                                         |

**用 schema 强制，不靠提示词自律**：返回结构里**没有**这些字段。
取数侧写不进去，写报告侧就不会误以为已经判过。

一个必须说清的例外形状：**引爆点 ⚪ 的「沿用上次状态」不是判档**。
取数侧只把上次运行已记录的标记**原样搬运**到 `last_known.value`，不看今天的证据、不重新判读。
搬运 ≠ 判定。

---

## 2. 公共信封（每项都有）

```
item_id, name
status               : ok | missing | not_applicable
tier_used              # 文档化来源顺序里，实际答上来的是第几级
attempted[]            # {source, url, http, outcome} —— 成功时也要有
source_label, source_url
as_of                  # 数据自身的日期，不是取数日
as_of_granularity    : trading_day | survey_week | month | quarter | meeting_date
caliber                # 口径标签
threshold_comparable : {value: bool, reason: str}
carry_forward_policy : must_carry | must_refetch | n_a
last_known           : {value, date} | null
staleness            : {n, unit: "weeks" | "days"} | null
missing_sentinel     : "⚪" | "N/A" | "（无）" | null       # 由调用方指定
counts_toward        : {numerator: bool, denominator: bool}
payload                # 逐项定义，见第 5 节
```

### 2.1 `attempted[]` 成功时也要有

这不是错误日志，是**可审计性**：父级不看一个字的 snippet，就能验证文档化的来源顺序**真的被走过**。

本技能里唯一写死了顺序的检索项是个股技术补数——`data-acquisition.md` 逐字：
「数据源优先级仍是 **Finviz > Yahoo Finance > TradingView > StockAnalysis.com**，且完整版逐点附来源 URL」。
若 `tier_used = 2` 而 `attempted[]` 里没有 Finviz 那一笔，父级应当**打回重取**，而不是接受一个「Yahoo 先答上来」的结果。

其余项多数只有单一来源或一句关键词提示（引爆点 ①②③⑤ 的「主要数据源」栏是关键词/站点提示，**不是排序回退链**）。
这种情况下 `attempted[]` 仍必须列出实际搜过的站点与 URL——它同时就是完整版要求的「逐项附来源 URL」的原料。

### 2.2 `caliber` 不是可选装饰

本技能的口径陷阱都不在数字上，在标签上：
MRR vs 年化 run-rate（引爆点⑤）、capex 绝对额 vs 增速（引爆点①）、
交易日 vs 日历日（FOMC 倒数）、干净价 vs 全价（债券报价）、
复权 vs 不复权（52周高/20日高）、水平值 vs 30日变化（HY OAS）。
`caliber` 缺失时该项按 `status = missing` 处理，**不允许「数字看起来合理就先用着」**。

---

## 3. 两个槽位为什么必须存在

### 3.1 `threshold_comparable` —— 「有值」不等于「可比阈值」

一个**真实、新鲜、解析正确**的数字，仍然可能必须报成缺失，因为阈值只对**某一个源的算法**校准。

本技能最硬的一例是 HY 基准利差。引爆点④ 的相对基准检验，两种口径逐字为：
「**变化率法**：`超额走阔 = neocloud 基准债利差30日变化 − HY OAS 30日变化`；≥ +50bp → 判「个体」」、
「**水平法**：直接看**同评级指数溢价**（公司层利差 − Single-B OAS）是否 > 250bp…分母是**同评级同侪**而非全 HY」。
两者都跑在脚本从 FRED `BAMLH0A0HYM2` / Single-B OAS 取来的序列上。
WebSearch 取回的一个 HY 水平值——即使完全正确——**没有 30 日序列，分母也不是 Single-B**，
所以它 `threshold_comparable = {value: false, reason: "无30日序列；④ 的两种口径均校准在脚本侧 FRED 读数上"}`，
只能进 📊 子表展示。

没有这个显式布尔，「有值」会被读成「可比阈值」，**⚪ 会计静默塌缩成 ❌**——
本技能里 ⚪ 的语义是「不知道」，❌ 的语义是「查过、没触发」，把前者写成后者就是把「不知道」交接成「已查、没事」。

同型的还有：`output-format.md` 逐字「不用 HYG/JNK 代替个股债券利差（两者已脱钩），不用指数 OAS 反推个股利差」；
`SKILL.md` 逐字「**不得拿 10Y 反推**」（2Y / 2s10s）；
`data-acquisition.md` 举证的复权口径差（「yfinance `history()` 默认复权会把 0700.HK 52周高算成 675.1…原始分别为 683.0 / 90.6」）。

### 3.2 `carry_forward_policy` —— 同一个技能里，沿用既是义务也是缺陷

`staleness` 分不出这两类，因为它们的陈旧度**长得一模一样**：

| 项                                     | 政策           | 出处逐字                                                             |
|----------------------------------------|----------------|----------------------------------------------------------------------|
| 最近一次 FOMC 政策倾向                 | `must_carry`   | 「**慢变量**：…**沿用并注明会议日期，不必每天重取**」                |
| 引爆点 ①②③⑤ 取不到新数据时             | `must_carry`   | 「⚪ 数据不足/未更新（本次无可靠新数据，沿用上次状态并注明）」       |
| FOMC 日历 / 距决议交易日数             | `must_refetch` | 「**每次运行须重新确认日期，不得沿用上次结果**」                    |
| CME FedWatch 隐含概率                  | `must_refetch` | 「FOMC 日历与隐含概率**每次运行重新确认、不得沿用**」               |
| 未来 1–3 交易日事件日历                | `must_refetch` | 同上（`data-acquisition.md` 宏观催化段）                            |
| 债券报价 `quote.as_of`                 | `must_refetch` | 「超过 `--max-quote-age`（默认 5 天）的债券判 ⚪、**不参与触发判定**（不把旧价当新价）」 |

**`must_carry` 的项，陈旧是正确状态**，`staleness` 在那里必须被读成「无关」；
**`must_refetch` 的项，沿用即缺陷**，哪怕昨天的数字今天仍然「差不多对」。
一个 `age` 字段同时承担这两种语义，必然有一半被读错。

---

## 4. 缺失哨兵由调用方指定

本技能的缺失哨兵**不是一个符号，是四个，会计规则各不相同**：

| 哨兵     | 用在哪                                                            | 会计规则                                                                                     |
|----------|-------------------------------------------------------------------|----------------------------------------------------------------------------------------------|
| `⚪`     | 引爆点 ①②③⑤、宏观利率各行、信用层 L1–L4                          | 「⚪ 数据不足项不计入 🟡/🔴 计数，但要在完整版列出「本次未取到新数据、沿用上次状态」」 → `counts_toward {numerator:false, denominator:false}`，**但行必须存在** |
| `N/A`    | 个股技术字段、EUR 债绝对利差、20日高缺失时的 T2触发价             | 「MA5/10/60 缺失时…按可得均线给出、缺项标 N/A」「「20日高」缺失时 T2触发价一并标 N/A」        |
| `（无）` | 空的「✅ 未触发」清单                                             | 列表为空的写法，不是缺数据                                                                    |
| `null`   | `neocloud_bonds.json` 的 `manual_flags`                           | 「没有可靠新证据就保持 null(=未观察到)，不要猜」                                              |

**哨兵由调用方（写报告那侧）在派单时指定，取数侧不自选。**
取数侧只负责把 `status` 填成 `missing` 并把指定的 `missing_sentinel` 原样带回。
理由很直接：同一个「取不到」，在引爆点行要渲染成 ⚪ 并触发「沿用上次状态」，
在技术表里要渲染成 `N/A` 并让 T2 标注「数据不足，暂不判定」，在 `manual_flags` 里要落成 `null`。
取数侧看不见这些下游规则。

> ⚠️ **本技能的陈旧度单位是「天」，不是「周」。**
> 「上次已知读数 X @ YYYY-MM-DD，已滞后 N 周」是 `daily-risk-monitor` 的规则，**不适用于这里**。
> 这里的硬门槛是债券报价 5 天、一级市场 30 天；引爆点与宏观利率各项**不要求任何陈旧度数字**，
> 它们要求的是「沿用上次状态并注明」。

---

## 5. payload 分族

信封统一，payload 逐项定义。本技能用到六族。

### 5.1 三档裁决族 —— 引爆点 ①②③⑤

它们**根本不返回标量**：返回的是**证据散文 + URL**，档位由写报告侧按 `tripwires.md` 的定性规则判。

```
payload = {
  evidence      : str        # 关键读数/证据原文，进完整版「关键读数/证据（YYYY-MM-DD）」栏
  evidence_date : "YYYY-MM-DD"   # 证据自己的日期，不是取数日
  reading_unit  : str        # ⑤ 必填：MRR 还是年化 run-rate；① 必填：绝对额还是增速
  legs[]        : [{name, evidence, evidence_date, url, status}]   # ③ 用；其余项可为空
  window        : {covered_from, covered_to, n_observations} | null # ② 用
}
```

三个形状上的特殊要求：

- **② 需要一段序列，不是一个读数。** 🔴 档逐字是「**持续多周下行趋势**（明确供过于求/需求转弱），
  而非一次性回落或停滞整理」。`window` 为 null 或 `n_observations = 1` 时，
  写报告侧**不得**据此判 🔴——契约让这件事在数据里可见，而不是靠记性。
- **③ 的 🔴 是合取。** 逐字：「**合约价翻转下行** **且** 买方要求重谈供货协议 / 推迟订单（两者同时出现）」。
  故 ③ 的 `legs[]` 固定两条（`contract_price_direction` / `buyer_renegotiation`），
  **各带自己的 status**；只取到一条时另一条 `status = missing`，如实写明另一半未取到。
- **⚪ 时值在别处。** `status = missing`、`missing_sentinel = "⚪"`、`carry_forward_policy = must_carry`、
  `last_known = {value: <上次运行记录的状态标记，原样搬运>, date: <上次运行日>}`。
  信封因此能表达「本次没有新证据，本项的值在上一次运行里」——这正是
  「⚪ 数据不足/未更新（本次无可靠新数据，沿用上次状态并注明）」在数据层的对应物。

`counts_toward`：⚪ 项 `{false, false}`（不计入 🟡/🔴 计数，也不进分母），但完整版必须列出该行。

### 5.2 标量行族 —— 2Y、2s10s、HY 基准利差、（可选）MOVE / Net Liquidity

```
payload = { value, unit, daily_change, five_day_change }
```

- 都进「宏观利率与 Fed」子表，表头逐字「| 指标 | 收盘/当前 | 日变动 | 近5日变动 | 说明 |」。
- `as_of` 必须是该市场**最近完整交易日**：「返回的是盘中实时/未收盘报价（而非已收盘日线收盘价）时，
  回退该市场上一交易日」。
- **缺失时行仍必须存在**：`SKILL.md` 逐字「**必须用 WebSearch 补**，取不到记 ⚪，
  **不得拿 10Y 反推、不得省略这两行**」。故 2Y / 2s10s 的 `missing_sentinel = "⚪"` 且行不可删。
- 2s10s **禁止跨源合成**：不得用脚本的 10Y 减 WebSearch 的 2Y 拼出来。两个数不同源、可能不同数据日，
  按 CLAUDE.md「Fallback chains — the rule」第 3 条不得并进同一行。这一条落在
  `threshold_comparable.reason` 与 `caliber` 里，取数侧只能返回**直接取到的** 2s10s。
- 这一族全部 `counts_toward {false, false}`：驱动源四分法只吃 10Y、SMH−QQQ、引爆点计数，
  这几行不进任何计算 tier。

### 5.3 分布族 —— CME FedWatch

```
payload = { cut_pct, hold_pct, hike_pct, quoted_at }
```

返回的是**一个分布，不是一个值**。三个百分比互相约束、**同源同刻**；
拆成三个独立单元会把这个约束丢掉，写报告侧就可能拼出一组不自洽的概率。
`quoted_at` 三者共用一个。

精简版渲染逐字为「隐含<持稳X%·降Y%·升Z%>」，完整版在子表下一行
「**CME FedWatch 隐含概率（降/持/升）**」——两处必须是同一次取数。

`carry_forward_policy = must_refetch`。
表述硬约束（属写报告侧，不属本层）：「**只描述状态，严禁预测利率路径**」——
契约不返回方向判断，正是为了让这条规则没有被违反的材料。

### 5.4 会议日期族 —— FOMC 倒数、FOMC 政策倾向、事件日历

**FOMC 倒数（`must_refetch`）**

```
payload = { decision_date, trading_days_to, has_sep, statement_time_et, presser_time_et }
```

- `trading_days_to` 的单位是**交易日**，而 ≤2 这个阈值就校准在这个单位上：
  ⚠️FOMC 逐字「距 FOMC 决议 ≤2 个交易日，或决议日当天/次日」；强制完整推送情形①同此。
- **它不能由日期相减得到**——需要市场日历。换算必须在取数侧完成，
  信封里放一个 `caliber = "trading_days"` 是不够的：写报告侧拿到的必须已经是交易日数。
  用日历日代入这些档位会错标 ⚠️FOMC，进而错误改写建议时序与推送等级。
- 时点逐字：「**FOMC 决议日**（声明 14:00 ET / 记者会 14:30 ET；注明是否含 SEP 点阵图）」。

**FOMC 政策倾向（`must_carry`）**

```
payload = { stance_wording, dot_plot_median | null, meeting_date }
```

`as_of_granularity = meeting_date`，`as_of` **就是会议日期**，陈旧是正确状态。
完整版位置逐字：「**最近一次 FOMC 政策倾向（注明会议日期，慢变量沿用）**」。
它与上面那项是镜像关系，**两者不可混同**——同一组里放着「必须沿用」和「沿用即缺陷」两项，
这就是 `carry_forward_policy` 存在的原因。

**事件日历（`must_refetch`）**

```
payload = { events[]: [{name, date, time_et | null, has_sep | null, drives_tag}] }
```

`drives_tag ∈ {⚠️FOMC, ⚠️MACRO, null}`。⚠️MACRO 逐字「**CPI / PCE / 非农 发布前 1 个交易日**（仅标注，不改建议）」。
两个标签都是**节奏层**，`output-format.md` 逐字「绝不改变 T1/T2/T3 触发与分桶」。
返回标签归属是允许的（那是日历事实），返回「因此要降级/等待」不允许（那是判读）。

### 5.5 因由检索族 —— 当日异动个股新闻起因、永续 ≥5% 异动起因

```
payload = { subject, cause_text, url, trigger_value | null }
```

- 永续那项是**条件触发**，门槛写死在脚本产出的隐含变动上：`output-format.md` 逐字
  「**≥5% 的重大异动须 WebSearch 查起因并附 URL**，同时在「⚠️ 风险提示明细」单列」；
  `perp-overnight.md` 逐字「**|隐含变动| ≥ 2%** 的标的才输出；≥ 5% 视为重大异动」。
  `trigger_value` 记该标的的隐含变动%，供父级核对确实过门槛。
- 「无明显起因」是**合法返回**，不是缺失：说明栏的合法取值逐字为
  「（财报反应 / 隔夜消息 / 休市期间重新定价 / 无明显起因）」。此时 `status = ok`、`cause_text = "无明显起因"`。
- 两项都 `counts_toward {false, false}`，`threshold_comparable = {false, "纯观察节点"}`：
  `perp-overnight.md` 逐字「**绝不改变分桶**」，跨阈值「只能写成**预告**…**不得记为已触发**」。

### 5.6 资产档写入族 —— 债券报价、`primary_market.deals[]`、`manual_flags`

**这一族的返回值与报告里的值是两个不同的东西。**
它们的目的地不是报告正文，是 `assets/neocloud_bonds.json`；
报告引用的是脚本从它们**派生**出来的 YTM / 利差 / 全包成本 / 四层判定。
所以 payload 里必须带 `destination`（写进资产档的哪个字段路径），
写报告侧才不会把输入值当成报告值直接引用。

**债券二级报价**

```
payload = {
  bond_key,
  destination : "neocloud_bonds.json:bonds[<key>].quote",
  price       : <干净价 clean price>,
  price_kind  : "clean",          # 契约只接受 clean；见下
  as_of       : "YYYY-MM-DD",     # 报价自己的日期
  source      : str               # 原始 URL / 来源说明
}
```

- **必须是干净价。** 脚本自己加应计利息（求解式：折现全价 == 干净价 + 应计利息）。
  `neocloud-credit.md` 逐字：「**应计利息不可省**——早期版本拿干净价直接比对折现值，
  把 YTM 高估最多约 70bp…足以虚假触发 L1」。喂全价 = 把这条修正反向做一遍。
  故 `price_kind` 只接受 `"clean"`；拿不到干净价时 `status = missing`，**不换算、不估**。
- **三个字段必须一起落地。** `price` 或 `as_of` 任一缺失，脚本记错误
  「债券 <key>: 无报价（quote.price / quote.as_of 未填）」，该债 `usable=False`、完全不参与判定。
- **陈旧度以天计、5 天是硬门槛**：「`quote.as_of` 超过 `--max-quote-age`（默认 5 天）的债券判 ⚪、
  **不参与触发判定**（不把旧价当新价）」。故 `staleness.unit = "days"`，`carry_forward_policy = must_refetch`。
- **`source` 的流向要知道**：脚本把它带进 `--json` 的 `bonds[].quote_source`，
  但第③块 markdown 表只有「| 债券 | 层 | 价格 | 报价日 | YTM | 基准 | 利差 | 同评级溢价 | 状态 |」九栏，
  **`quote.source` 不在表里**。而 `output-format.md` 要求「报告引用该字段而非重新检索」——
  要引用来源，只能读 `--json` 或资产档本身。
- `threshold_comparable = {false, ...}`：报价本身不与任何阈值比，
  阈值（L1 600/800bp、同评级溢价 250/500bp、L2 250/400bp、SPV 价 <95）全部作用在脚本派生出的利差上。
  转债「不可直读为纯信用利差」、EUR 债「绝对利差标 N/A」——这两类 `not_applicable`，别喂。

**一级市场条款（多字段记录）**

```
payload = {
  destination : "neocloud_bonds.json:primary_market.deals[]",
  deals[] : [{
    date            : "YYYY-MM-DD",     # 必须 ISO
    issuer, instrument, size_usd_mm,
    base_rate       : "SOFR" | ...,     # 只有大写 "SOFR" 会被计进全包成本
    initial_spread_bp, final_spread_bp, # 两者都必须非空
    initial_oid, final_oid,
    status          : "priced" | "repricing" | "pulled" | "postponed",
    purpose, source
  }]
}
```

三个陷阱，全部在契约里显式化：

1. **`date` 必须 ISO。** 非 ISO（如种子档里的「2026-06-中」）→ 脚本 `age_days = None` →
   该笔判「日期非 ISO 格式、无法计龄 → 仅历史参照，不计入判定」。
2. **`status` 是枚举，且决定走哪条代码路径。** 只有 `"priced" | "repricing"` 且两个 spread 都在的笔数进候选，
   脚本按日期倒序取**最近一笔**；30 天观察窗（`TH["L1_primary_window_days"] = 30`）之内才计入 L1。
3. **`"pulled" / "postponed"` 填在 `deals[]` 里不会产生任何 🔴。** 它们被候选过滤器直接排除。
   撤回/延期必须**另外**写 `manual_flags.primary_deal_pulled_or_postponed` 才会触发——
   这是本族最容易漏的一步，取数侧检到撤回/延期时**两个地方都要写**。

**`manual_flags`（三态，本契约补一个状态）**

```
payload = {
  destination : "neocloud_bonds.json:manual_flags.<field>",
  state       : "observed" | "checked_not_observed" | "not_checked",
  evidence    : str | null       # state=observed 时必填，会被脚本原样印进 L1/L4 明细
}
```

资产档现状是**三态无 false**：证据字符串 = 发生；`null` = 「未观察到」；
**没有「查过、确认没发生」**。`_readme` 逐字：「没有可靠新证据就保持 null(=未观察到)，不要猜」。
于是「今天查过、确认没发生」与「今天根本没查」在数据里**不可区分**——
一个是完成的核查，一个是漏掉的工作，落盘后长得一模一样。

**本契约因此增加显式 `checked_not_observed`**，并规定它**只活在传输单元里**：
写进资产档时 `observed → 证据字符串`、其余两态 → `null`（资产档格式不变，脚本无需改动）。
`checked_not_observed` 与 `not_checked` 的区别保存在当日的检索 JSON 里，供当日报告的「已核查项」自述与事后审计。

两个 `manual_flags` 布尔项还有一个**没有防护的失效模式**，取数侧必须知道：
它们**没有 `as_of`、没有时效窗**（对比 `deals[]` 的 30 天窗），一旦填入就永久生效直到人工清空。
`hyperscaler_capex_guide_cut` 的爆炸半径尤其大——它属论点侧 L4，
映射规则逐字「**论点侧**（L2 或 L4）出现 🔴 → **④ = 🔴**」，而步骤3 会把当日**全部**买入桶降级为 👀观察。
它与引爆点① 同源，**一次判读要写两个地方**，两处不一致会让 ① 与 ④ 互相矛盾。

**CDS（`cds_5y_bp` / `cds_5y_as_of` / `cds_source`）—— 文档与实现不符，必须知道**

`neocloud-credit.md` 与资产档 `_readme` 两处都写「有 Bloomberg/Markit 数据源时填入 `manual_flags.cds_5y_bp`，
**L1 判定即改用 CDS 基准**」。**实现里没有这条路径**：`cds_5y_bp` 从不进入 `evaluate()`，
它与 `cds_5y_as_of` 只被 `build_data_gaps()` 读取，带进 `--json` 的 `data_gaps.cds_realtime`
并决定第⑧块是否印出盲区声明「无实时 CDS 数据（本层最大盲区）」。
**填入的唯一可见效果是让盲区声明消失。** 在这条路径修好之前，
把 CDS 值当作已并入判定来写报告即为错报；本项 `threshold_comparable = {false, "实现未接入 evaluate()"}`。

### 5.7 技术字段补数族（条件触发）

```
payload = { ticker, fields: {<field>: {value, url, page}}, market_asof }
```

只在脚本取不到某字段时才触发。三条硬规则：

- **港股是禁区**：`data-acquisition.md` 逐字「**禁止**用 WebSearch 的 Finviz/Yahoo/TradingView 页面取港股价」——
  港股价格字段必须走姊妹技能的 `hk_quote.py`。港股行的这一族一律 `not_applicable`。
- **来源顺序写死**：Finviz > Yahoo Finance > TradingView > StockAnalysis.com，`attempted[]` 必须体现。
- **冲突裁决在父级**：`SKILL.md` 逐字「同一字段脚本与 WebSearch 冲突时以脚本为准，并在完整版注明差异」。
  取数侧返回读数，不做取舍。

`missing_sentinel = "N/A"`（不是 ⚪）。缺失时的下游措辞由父级写：
「若 yfinance 与 WebSearch 都取不到，则该股 T2 标注「数据不足，暂不判定」、不强行触发」。

---

## 6. HY 基准利差：唯一被两套源同时覆盖的项

`SKILL.md` 把 HY 基准利差列为「脚本不产出、由 WebSearch 取」；
而 `neocloud_credit_monitor.py` **独立地**从 FRED `BAMLH0A0HYM2` 取同一指标（`neocloud-credit.md` 自动取数段）；
`output-format.md` 的来源标注也写「HY 基准利差（FRED: BAMLH0A0HYM2）」。

按 CLAUDE.md「Fallback chains — the rule」第 3 条（**Never merge tiers into one table**），
**这两个读数不得并进同一行、不得互相顶替、不得取其一充当另一个**。
它们是两个不同时点、不同口径的快照。

契约的处理是**两个独立的传输单元**（`item_id` 不同、`source_label` 不同、`as_of` 各自独立），
其中 WebSearch 那个：

```
threshold_comparable = {value: false,
  reason: "④ 的变化率法(≥+50bp)与水平法(>250bp) 均校准在脚本侧 FRED/Single-B 读数上；本读数无30日序列、分母非Single-B"}
counts_toward = {numerator: false, denominator: false}
```

它只能进 📊 子表展示。另有一条**缺数据即封顶**的规则要一并带给父级——
`tripwires.md` 逐字：「取不到 HY 基准数据时，④ **最高只记🟡（不得升🔴）**，
并注明「缺基准、无法区分宏观 vs 个体」」。这是全技能唯一「缺数据直接封顶某个引爆点档位」的规则，
而这里的「取不到」指的是**脚本侧**那个读数取不到，不是 WebSearch 侧那个。

---

## 7. 分组派发与交接

**按 governing reference 分组，一组加载一个参考文件。** 一组的取数侧只需读它那一份，
不必加载 `output-format.md` 全文或别组的参考。

| 组    | 名称                        | 加载的 governing reference                         | 项数   |
|-------|-----------------------------|----------------------------------------------------|--------|
| **A** | 引爆点 ①②③⑤                | `references/tripwires.md`                          | 4      |
| **B** | 宏观利率与 Fed ＋ 技术补数   | `references/data-acquisition.md`                   | 9      |
| **C** | 事件日历与因由检索          | `references/data-acquisition.md`（宏观催化段）＋ `references/drawdown-driver.md`＋ `references/perp-overnight.md` | 3      |
| **D** | 债券报价与一级市场          | `references/neocloud-credit.md`                    | 5      |
|       |                             |                                                    | **21** |

> B 组的「技术补数」子族与「宏观利率」子族共用 `data-acquisition.md`，但**触发条件与哨兵不同**：
> 宏观利率各行每日必取、缺失记 ⚪ 且行不可删；技术补数只在脚本取不到某字段时才触发、缺失记 `N/A`。
> 派单时须分开说明，不要让一条规则串到另一族上。
>
> C 组的两项因由检索（当日异动个股 / 永续 ≥5%）与事件日历同属「宏观催化」段落，
> 但门槛不同：永续那条门槛写死为 ≥5%，「当日明显异动个股」**没有量化门槛**（见第 8 节）。

### 7.1 交接：一组一个文件，返回一行

- 每组把结果**写成一个 JSON 文件**（一组一个，不是一项一个）。
- 返回消息**只有一行**，形如：

  ```
  wrote 9 items to <path>, 7 ok / 2 missing
  ```

- **返回消息永远不是内容的渲染。** 不摘要、不贴表、不带一句「其中 2Y 取到 3.62%」。
  父级读文件；一旦返回消息开始携带读数，原始 snippet 的污染就从后门回来了，这层白做。
- 父级读回文件后，按 `attempted[]` 审计来源顺序：tier-1 未被尝试就**打回重取**，不接受结果。

### 7.2 一组失败不是「组失败」

取数组不是脚本，**没有退出码**。某一项取不到，在文件里表达为该项 `status = missing` 加它的哨兵；
**绝不表达为整组失败、也绝不让整组文件不落盘**。
这是仓库法则「少一路增强，不少一段交付」在检索半边的对应物：
一项缺失只让报告少一行读数，不让报告少一段。

（本仓库保留的退出码 1=参数错误 / 2=依赖缺失 / 3=取数失败 / 4=量级自检未通过 是**脚本**的约定，
与本层无关，不要把它们套到检索单元上。）

---

## 8. 已知规格欠缺（照实记，别当成契约缺陷去补）

两项在原文里就没写全，本契约**不替它们发明规则**：

- **MOVE / Net Liquidity**：原文只有一句「若有MOVE/HY利差/Net Liquidity可参考」——
  无来源、无优先级、**无缺失标记**、无阈值。故它是本清单里唯一 `missing_sentinel = null` 的项：
  取不到就整项省略，不写 ⚪。要改这一点，得先改 `output-format.md`。
- **「当日明显异动个股」的门槛未量化**：原文只有「以及当日明显异动个股（大涨/大跌）的新闻起因」，
  「明显异动」没有数字——与永续那条写死 ≥5% 恰成对比。
  取数侧应在 `caliber` 里写明本次实际用的判定口径（如「日涨跌幅 |≥5%|」），
  让这个自选门槛**可审计**，而不是让它隐形。

---

## 9. 落单前自检

一项在写进组文件之前，逐条核对：

1. `payload` 里有没有混进 🟢🟡🔴、阈值比较、或一句解读？有 → 删掉，那不是本层的东西。
2. `as_of` 是**数据自己的日期**还是取数日？填错就是把今天的日期贴到三周前的读数上。
3. `caliber` 填了吗？MRR 还是年化 run-rate、交易日还是日历日、干净价还是全价、水平值还是 30 日变化。
4. `threshold_comparable = true` 的依据是什么？说得出「这个数就是那个阈值校准的那个源那个口径」才填 true。
5. `carry_forward_policy` 是 `must_carry` 还是 `must_refetch`？查第 3.2 节的表，不要凭印象。
6. `attempted[]` 有没有？**成功时也要有。**
7. `missing_sentinel` 是父级派单时指定的那一个吗？不是自己挑的吧。
8. 目的地是资产档的项，`destination` 填了吗？`price_kind` 是 `"clean"` 吗？
9. 检到一级市场撤回/延期的，`manual_flags` 那一笔也写了吗（只写 `deals[]` 不会触发任何 🔴）？
10. 返回消息是不是只有一行、不含任何读数？
