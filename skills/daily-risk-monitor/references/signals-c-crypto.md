# 信号 14–18 · 加密与美股 24/7 永续

> 本文件自作者私有的每日 routine 文档 `daily-risk-monitor-v2.md` **逐字迁移**，未改写。
>
> **编者注**：那份原文是作者本机的私人笔记，**不随本技能分发，也不在本仓库里**——
> 按图索骥是找不到的，也不需要找。本文件就是原文该章节的完整内容；
> 技能运行所需的全部口径都在 `references/` 这十个文件里。

# C. 加密（每日核心）

### 14. BTC / ETH / SOL 永续资金费率
- **触发**：三者同时 >0.05%/8h（年化约 55%）持续 ≥24 小时 = 多头杠杆过热
- **极端触发**：任一 >0.1%/8h = 急迫反转风险
- **实测基线 2026-08-10**：BTC `lastFundingRate` = 0.00006129 → **0.0061%/8h（年化约 6.7%）**，完全正常
- 📖 **大白话**：永续合约里，多空双方每隔一段时间互相付一次钱。**费率是正的 = 做多的人在付钱给做空的人**，数字越大代表多头越拥挤、越急着追。愿意付年化 55% 的成本去做多，说明杠杆已经很烫——**这种时候只要跌一点，就容易连环爆仓**。

#### 数据源 A（首选）：Binance 公开 API — 免费、免 API key
（2026-08-10 实测本机可直连，HTTP 200，未被地区限制）

当前费率 + 下次结算时间：

```bash
for s in BTCUSDT ETHUSDT SOLUSDT; do
  curl -s "https://fapi.binance.com/fapi/v1/premiumIndex?symbol=$s" \
  | jq -r '"\(.symbol) rate=\(.lastFundingRate) mark=\(.markPrice) next=\(.nextFundingTime)"'
done
```

过去 24 小时是否**持续**高于阈值（触发条件要求持续 ≥24h，必须查历史，不能只看当下一个数）：

```bash
for s in BTCUSDT ETHUSDT SOLUSDT; do
  echo "== $s"; curl -s "https://fapi.binance.com/fapi/v1/fundingRate?symbol=$s&limit=6" \
  | jq -r '.[] | "\(.fundingTime) \(.fundingRate)"'
done
```

**结算周期必须先确认**（Binance 部分币种已改成 4 小时结算一次）：

```bash
curl -s "https://fapi.binance.com/fapi/v1/fundingInfo" \
| jq -r '.[] | select(.symbol|test("^(BTC|ETH|SOL)USDT$")) | "\(.symbol) intervalHours=\(.fundingIntervalHours)"'
```

若某币种未出现在结果中，按默认 **8 小时**处理。（BTC / ETH / SOL 长期为 `intervalHours=8`，但 Binance 会不定期调整个别币种，所以每次仍要查。）

#### ⚠️ 口径统一（最容易搞错的一步）

阈值 0.05% 是 **8 小时口径**。各家给的原始数字周期不同，**必须先换算再比对阈值**：

| 来源 | 原始费率周期 | 换算成 8h |
|---|---|---|
| Binance（默认） | 8 小时 | 直接用 |
| Binance（部分币种） | 4 小时 | **× 2** |
| Hyperliquid | **1 小时** | **× 8** |

年化换算：`年化% = 8h费率 × 3 × 365`。报告里**同时给出 8h 费率和年化**，年化更直观。

> 📖 **为什么要在意**：SOL 如果是 4 小时结算一次，你看到 0.05% 其实相当于 8 小时 0.1%，是**两倍**——直接从「偏热」跳到「极端触发」。不换算就会漏掉真正危险的信号。

#### 数据源 B（备援）：Hyperliquid API

**触发时机**：Binance 返回 `451` / `403`（云端 IP 常被地区限制）、超时、或字段缺失时改用。

```bash
curl -s -X POST https://api.hyperliquid.xyz/info \
  -H 'Content-Type: application/json' -d '{"type":"metaAndAssetCtxs"}' \
| jq -r '[.[0].universe, .[1]] | transpose
         | map(select(.[0].name=="BTC" or .[0].name=="ETH" or .[0].name=="SOL"))
         | .[] | "\(.[0].name) hourly=\(.[1].funding) OI=\(.[1].openInterest) mark=\(.[1].markPx)"'
```

返回的 `funding` 是**每小时**费率 → **× 8** 才是 8h 口径。

跨交易所预测费率（一次拿到多家、可交叉验证 Binance 的数是否离谱）：

```bash
curl -s -X POST https://api.hyperliquid.xyz/info \
  -H 'Content-Type: application/json' -d '{"type":"predictedFundings"}'
```

#### 兜底顺序
`Binance` → `Hyperliquid` → `web_search coinglass` → 标注「数据暂缺」。
用了备援源**必须在表格里注明来源**（不同交易所费率可以差一倍，来源不同不能直接跨日比较）。

### 15. 清算数据（过去 24h）
- **数据源**：搜 "coinglass liquidations 24h"
- **触发**：24h 总清算 >$500M = 杠杆洗盘；>$1B = 重大事件
- **必须注明哪一方被清算更多（多头 vs 空头）**
- 📖 **大白话**：过去 24 小时有多少钱**被交易所强制平仓**（保证金不够、被系统砍仓）。多头被清算多 = 下跌把借钱做多的人洗出去；空头被清算多 = 逼空。金额越大代表这波动作杠杆味越重，而不是现货真实买卖。

### 16. BTC Dominance
- **数据源**：搜 "BTC dominance" 或 web_fetch coingecko / tradingview
- **触发**：24h 跌幅 >2% 或 7d 跌幅 >3% = 山寨狂热期
- 📖 **大白话**：比特币市值占整个加密市场的百分比。**快速下滑 = 钱正从相对稳的 BTC 跑去炒小币**，这是风险偏好冲到顶的典型信号，历史上常出现在牛市后段。

### 17. 稳定币总供应（USDT + USDC）
- **数据源**：DeFiLlama MCP 的 `get_stablecoins`
- **触发**：7 日净流出（总供应下降）= 流动性撤出｜**中期确认**：近 2 周持平或萎缩｜**关注**：单日净流出 >$1B 必须标注
- 📖 **大白话**：USDT + USDC 的总发行量 ≈ 币圈账户里躺着的「现金 / 子弹总量」。**变多 = 有新钱进场准备买；变少 = 有人把币换回美元、真的离场了**。价格可以靠杠杆撑住，但这个数字骗不了人。

---

# D. 美股 24/7 永续（信号 18）

### 18. `xyz:SP500` / `xyz:XYZ100` 永续隐含价 vs 上一美股收盘

**每日都查**（一次 API 调用，很便宜）。在**美股开盘前**和**周末**价值最高。

| 市场 | 跟踪标的 | 口径 | yfinance 对照 |
|---|---|---|---|
| `xyz:SP500` | 标普 500 | **指数点位 1:1**（≈7,760，不是 SPY 的 ≈773） | `^GSPC` |
| `xyz:XYZ100` | 纳斯达克 100（改名避商标） | **指数点位 1:1**（≈29,800，不是 QQQ 的 ≈700） | `^NDX` |

```bash
curl -s -X POST https://api.hyperliquid.xyz/info \
  -H 'Content-Type: application/json' -d '{"type":"metaAndAssetCtxs","dex":"xyz"}' \
| jq -r '[.[0].universe, .[1]] | transpose
   | map(select(.[0].name=="xyz:SP500" or .[0].name=="xyz:XYZ100"))
   | .[] | "\(.[0].name) mark=\(.[1].markPx) prevDay=\(.[1].prevDayPx) hrFunding=\(.[1].funding) notionalOI=\((.[1].markPx|tonumber)*(.[1].openInterest|tonumber)) dayVlm=\(.[1].dayNtlVlm)"'
```

**实测基线 2026-08-10**：`xyz:SP500` mark 7759.0（prevDay 7767.9）｜`xyz:XYZ100` mark 29773.0（prevDay 29770.0）。对照 `^GSPC` 收盘 7757.64 → 隐含跳空 **+0.02%**，正常。

#### 三个观察维度

**① 隐含跳空（主指标）** = `perp mark ÷ 上一美股收盘 − 1`
- 触发：|偏离| ≥ 2% → **计为 1 个 Tier 2 触发**｜≥1% 时即使不触发也必须在解读里点名

**② 科技 vs 大盘相对强弱** = `XYZ100 偏离% − SP500 偏离%`
- 正值 = 科技/AI 领涨，负值 = 科技在拖累大盘｜差距 ≥1.5pt 时点名——这直接关系到 AI 算力持仓

**③ 资金费率（弱信号）**
- 触发：年化 **>+15%**（真实多头拥挤）或 **<−10%**（强烈对冲需求）
- **基线 = 每小时 `0.00000625`（＝ 8h 0.005%，年化 +5.48%）**。读到这个数字就是**「无方向信号，纯粹是利率」**。2026-08-10 实测 `xyz:XYZ100` 正好停在这个数上。

#### 📖 大白话：为什么股票 perp 的资金费率没什么用，但价格很有用

币圈资金费率能反映情绪，是因为**比特币没有到期结算、没有真实标的可以随时套利**，费率完全由多空谁更急决定。

**股票永续不一样**——背后有真股票，盘中随时能套利，所以资金费率会被套利者钉在「无风险利率 − 股息率」附近，**年化 5% 上下**。你看到 SP500 永续正资金费率，那反映的是**利率是 5%，不是有人在追高**。

**但价格是真信息**：CME 的 ES / NQ 期货在**周五美东 17:00 到周日 18:00 完全休市**，这 49 小时全世界没有任何地方在给美股定价，只有这些 24/7 永续在报。所以周末币圈大跌时，看一眼 SP500 perp 有没有跟着跌，立刻分清「币圈自己的事」还是「全球 risk-off」。

#### ⚠️ 两个必须保留的检查
1. **流动性门槛**：名义 OI = `markPx × openInterest`，**< $5M 就标「不适用」**。历史基线：`xyz:SP500` ≈ $483M OI／$129M 日成交，`xyz:XYZ100` ≈ $282M／$177M。骤降到千万以下 = 池子在迁移，报价不可信。
2. **代码撞名**：Hyperliquid **主池**的 `SPX` 是 SPX6900 迷因币，不是标普500。必须指定 `"dex":"xyz"`，且标记价与真实指数收盘差 **>10% 就判定取错市场**。

---
