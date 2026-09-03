# 数据更新节奏与通用取数函数

> 本文件自作者私有的每日 routine 文档 `daily-risk-monitor-v2.md` **逐字迁移**，未改写。
>
> **编者注**：那份原文是作者本机的私人笔记，**不随本技能分发，也不在本仓库里**——
> 按图索骥是找不到的，也不需要找。本文件就是原文该章节的完整内容；
> 技能运行所需的全部口径都在 `references/` 这十个文件里。

# 数据更新节奏

**全部 30 项每天都抓**（这是我的选择：宁可多花时间，也不要用过期数据做判断）。但要理解各项**源头的实际更新频率**，非更新日抓到相同数值是正常的：

| 源头频率     | 信号                                                                                                                                                                        |
|--------------|-----------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| **每日更新** | 1 HY 利差、2 200DMA 比例、4 VIX 期限结构、5 TGA/RRP、6 A/D Line、9 Fear&Greed、10 Put/Call、14–17 加密、18 美股永续、19 σ倍数、20 VRP、21 跨资产、22 广度、23 曲线、26 趋势 |
| **每周更新** | 3 BofA 牛熊（周二/三）、5 的 WALCL（周四美东）、7 NAAIM（周三）、8 AAII（周四）、12 内部人买卖比                                                                            |
| **每月更新** | 11 FINRA Margin Debt（滞后约 1 个月）、24 Sahm Rule（滞后约 1 个月）、25 LEI、29 AAII 家庭配置、30 Margin Debt/GDP                                                          |
| **每季更新** | 13 IPO 发行量、27 Buffett Indicator                                                                                                                                         |

- **非更新日抓到相同值 = 正常**，标注 `as of MM/DD`，**绝不因为数值没变就判定异常或标注数据暂缺**。
- **抓不到就标「⚪️ 数据暂缺」并写明尝试过的来源，不重试超过 2 次，绝不用旧记忆或估算值填充。**
- **每周一**额外执行「H. 周一附加」四项 + Tier 1 四周趋势回顾（HY 利差 4 周变化、净流动性 4 周方向）。

## FRED 通用取数函数

（信号 1、4、5、23、24、27、32 共用；`cosd` 裁掉历史，否则单个序列 200KB+）

```python
import subprocess, pandas as pd, io
def fred(sid, since="2025-01-01"):
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}&cosd={since}"
    t = subprocess.run(["curl","-s","--max-time","45",url], capture_output=True, text=True).stdout
    df = pd.read_csv(io.StringIO(t)); df.columns = ["date","v"]
    df["v"] = pd.to_numeric(df["v"], errors="coerce")
    return df.dropna()          # 末行即最新值：df.iloc[-1]["date"], df.iloc[-1]["v"]
```

## yfinance 通用取数（信号 19–22、26、33–34 共用）

```python
import yfinance as yf, requests, numpy as np
s = requests.Session(); s.headers["User-Agent"] = "Mozilla/5.0"
T = ["^GSPC","^VIX","SPY","RSP","TLT","GLD","UUP","BTC-USD","GC=F","DX-Y.NYB"]
px = yf.download(T, period="2y", interval="1d", progress=False, auto_adjust=False, session=s)["Close"]

def vol_block(sym):
    p = px[sym].dropna(); r = p.pct_change()
    rv20 = float((r.rolling(20).std()*np.sqrt(252)*100).iloc[-1])   # 年化已实现波动率
    one_sigma = rv20/np.sqrt(252)                                    # 单日 1σ
    return float(r.iloc[-1]*100), rv20, one_sigma, abs(float(r.iloc[-1]*100))/one_sigma
```

> ⚠️ **不要用 yfinance 取 `^VIX3M` / `^VIX9D` / `^VIX6M`**——2026-08-10 实测，这三个序列全部停更在 2026-07-17，而 `^VIX` 是当日的。拿它算期限结构会静默地用三周前的远月值去比今天的近月值，**而信号 4 是 Tier 1**。改用 FRED（见信号 4）。

---
