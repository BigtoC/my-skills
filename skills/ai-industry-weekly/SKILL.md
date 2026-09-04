---
name: ai-industry-weekly
description: AI 算力产业链「产业质量参考表」周更助手。每周用最新基本面按统一规则重算全部标的（当前 46 档）的产业质量表，与滚动基准表逐行对比，输出评级变动摘要 + 完整产业表 + 应用说明，并推送 Slack。当用户提到 产业表周更、AI 算力产业表、产业质量参考表、重算评级、🟢🔵🟡🔴 四档评级、46 标的、CoWoS/HBM/3nm/数据中心电力 四大瓶颈、基准表对比、周更推送到 Slack 频道 时自动使用。
license: MIT
compatibility: Portable Agent Skills format for agents that support SKILL.md. Scripts need python3, `requests` and `yfinance`, plus outbound network; step 1 exits with an install hint if yfinance is missing (it does not self-install). Slack push (step 5) needs a Slack MCP server and is skippable. The optional ETF holdings fetch (step 1.2) falls back Alpha Vantage -> yfinance -> the issuer site and caches nothing; it reads Alpha Vantage key(s) from `AV_API_KEYS`, and unset, that first tier is skipped and the rest of the run is unaffected.
metadata:
  author: BigtoC
  version: "0.1.0"
  tags: "finance,equity-research,ai-infrastructure,weekly-routine,slack,report"
---

# AI 算力产业链产业表周更

> **回退链规则**：本技能的多源取数遵循仓库通用规则，见 `CLAUDE.md` 的
> 「Fallback chains — the rule」一节（顺序固定并写下来／标注来源／不跨级合并／
> 阈值不随源转移／降级源算不出的记 N/A／回退必须响／新鲜的低级源优于陈旧的高级源）。

## 角色

你是 AI 算力产业链「产业质量参考表」周更助手。每周用最新基本面，按统一评级规则重算 46 个标的的产业质量表，并和基准表（见第三步）对比，输出：① 评级/数据变动摘要 ② 完整更新后的产业表（可直接粘贴）③ 应用说明 ④ 推送到指定 Slack 频道（`$NOTIFICATION_SLACK_CHANNEL_ID`）。

**⚠️ 交付双轨（最重要，勿省）：①②③ 必须【完整写在本次运行结果（对话回复正文）里】，④ 的 Slack 推送是【额外分发】而非替代。**「已推送 Slack」「详见 thread」「链接如上」等**都不算完成运行结果输出**——运行结果里没有完整 46 行产业表，本次任务即视为未完成。

目的：日报监控任务（引用本表的那个 routine）里的「产业质量参考表」是静态慢变量。本任务每周重算一次，让评级跟上财报与估值变化，避免过时。因运行结果常被直接复制去更新日报任务，故整表必须在运行结果中就地可取，不能只存在于 Slack。

> 上文「46」是当前标的数，**不是硬编码常数**：实际行数一律以 `assets/universe.json` 的 `tickers` 长度为准，增减标的后该数字随之改变（`meta` 的 `count` 是**基准表当前行数**，刚增减标的、write 还没跑时它仍是旧值；要看清单当前值请读 `assets/universe.json`，或跑 `fetch_fundamentals.py` 看它打印的「清单共 N 个」）。

## 文件地图

| 路径                            | 作用                                         |
|---------------------------------|----------------------------------------------|
| `assets/universe.json`          | 权威标的清单与行序（唯一真相源）             |
| `assets/baseline.md`            | 滚动基准表，**每次运行后被自动覆写**         |
| `references/data-sources.md`    | 第一步取数口径与数据源细则                   |
| `references/rating-rules.md`    | 第二步评级规则与逐标的特例（全文）           |
| `references/output-format.md`   | 输出格式、Slack 推送、交付自检清单           |
| `scripts/fetch_fundamentals.py` | 批量取基本面                                 |
| `scripts/hk_quote.py`           | 港股原始未复权实时行情                       |
| `scripts/etf_holdings.py`       | ETF 持仓/费率取数（AV→yfinance→官网，可选）  |
| `scripts/baseline.py`           | 基准表 show / meta / validate / diff / write |

脚本**内部**用 `__file__` 相对定位 `assets/`，所以脚本自己找得到数据文件，与 cwd 无关。但**调用脚本的那条命令**仍要给对路径：本技能被触发时 cwd 通常是用户自己的项目，`python3 scripts/baseline.py ...` 这种相对写法会直接 `can't open file`。因此下文所有命令一律用第零步定下的 `$SKILL_DIR` 绝对路径调用。

## 第零步 · 定位技能目录 + 读频道配置

### 0.1 定位技能目录

`$SKILL_DIR` = **本 SKILL.md 所在目录的绝对路径**（其下有 `SKILL.md`、`scripts/`、`assets/`、`references/`）。本技能有两种常见安装形态，两种都要能定位：

- 装到 `~/.claude/skills/ai-industry-weekly/`（README 主推的安装方式）；
- 直接在本 repo 内使用，即 `<repo>/skills/ai-industry-weekly/`。

你**已经知道**本 SKILL.md 是从哪个路径加载的——直接把那个目录填进去 export，别猜、别写死家目录：

```bash
export SKILL_DIR="<本 SKILL.md 所在目录的绝对路径>"
```

拿不准时用这条探测（先找已安装位置，再回落到当前 repo）：

```bash
for d in "$HOME/.claude/skills/ai-industry-weekly" "$(git rev-parse --show-toplevel 2>/dev/null)/skills/ai-industry-weekly"; do
  [ -f "$d/SKILL.md" ] && [ -f "$d/scripts/baseline.py" ] && export SKILL_DIR="$d" && break
done
echo "SKILL_DIR=${SKILL_DIR:-<not found>}"
```

（探测只覆盖上面两种常见形态，且 `git rev-parse` 那一支要求 cwd 在本 repo 内。打印 `<not found>` 时不要瞎试，直接用本 SKILL.md 的实际加载路径手动 export。）

自检（不通过就先解决路径，别往下走）：

```bash
ls "$SKILL_DIR/scripts/baseline.py" "$SKILL_DIR/assets/universe.json"
```

同一次运行里 `$SKILL_DIR` 只定一次，后面每条脚本命令都写成 `python3 "$SKILL_DIR/scripts/xxx.py" ...`。若你的 Bash 调用之间不保留环境变量，就把 `$SKILL_DIR` 换成那个绝对路径字面量，效果一样。

### 0.2 读频道配置

```bash
echo "${NOTIFICATION_SLACK_CHANNEL_ID:-<unset>}"
```

Slack 频道 ID 只从环境变量 **`NOTIFICATION_SLACK_CHANNEL_ID`** 读取，本技能不带任何配置文件。

**未设置时**：跳过第五步 Slack 推送，并在正文末尾注明「本次未推送 Slack（`NOTIFICATION_SLACK_CHANNEL_ID` 未设置）」。这**不影响**正文 ①②③ 的完整性要求——它们照常完整输出。

需要推送时让用户自行设置（写进 shell profile 或 `.claude/settings.json` 的 `env`）：

```bash
export NOTIFICATION_SLACK_CHANNEL_ID=C0XXXXXXXXX
```

**绝不把真实频道 ID 写进本 repo 的任何文件**（公开仓库）。正文、references、提交内容里一律只用 `$NOTIFICATION_SLACK_CHANNEL_ID` 占位符。

## 第一步 · 取数

### 1.1 基本面批量取数

```bash
python3 "$SKILL_DIR/scripts/fetch_fundamentals.py" --json /tmp/fundamentals.json
```

（可选 flag：`--tickers NVDA,TSM,0700.HK` 只取部分标的、`--quiet` 只写 JSON 不做人类可读打印。周更走全量，不要加 `--tickers`。）

脚本按 `assets/universe.json` 取全部标的；`hk_quote: true` 的港股价格类字段自动走 `scripts/hk_quote.py`（原始未复权）。

取数口径、字段含义、为什么港股不用 yfinance、韩股/ADR/ETF 的特殊处理 —— **读 `references/data-sources.md`**。

数据缺失记 `N/A`，**不估算、不编造**。抓空就重跑一次；仍空则记 N/A。

**留意脚本输出末尾的「⚠ 利润率完整性」一节**：yfinance `.info` 的 `operatingMargins` 会单字段损坏（`om>gm` 算术不可能等），脚本已自动检出并给出年报/TTM 重算值；命中行怎么取舍见 `references/rating-rules.md` 顶部编者注（命中标的每周不同，勿当固定名单）。

### 1.2 ETF 持仓取数（可选增强，每周一次）

```bash
# 先看变量在不在——只看有没有，别把 key 本身打印出来
[ -n "$AV_API_KEYS" ] && echo "AV_API_KEYS=<set>" || echo "AV_API_KEYS=<unset>"

python3 "$SKILL_DIR/scripts/etf_holdings.py" --json /tmp/etf_holdings.json
```

取的是 `assets/universe.json` 里 **`etf: true`** 的那几档（清单里有几档就取几档，别在这里硬编码档数或代码）。

**取数走三级回退：Alpha Vantage → yfinance → 发行商官网。不做本地缓存，每次现取。**

| 级别 | 来源                                            | 拿到什么                                                           | 口径                       |
|------|-------------------------------------------------|--------------------------------------------------------------------|----------------------------|
| 一   | Alpha Vantage `ETF_PROFILE`                     | 逐笔成分（`description` / `symbol` / `weight`）＋费率＋上市日＋AUM | **全量持仓**               |
| 二   | yfinance `funds_data.top_holdings`              | 只有前 N 大（`Name` / `Holding Percent`）                          | **top-N，不是全量**        |
| 三   | 发行商官网持仓页（roundhillinvestments.com 等） | 官方持仓表                                                         | **权威**（正文要的就是它） |

**为什么取消了缓存**：持仓是会变的（三档 Roundhill 都是主动管理、季度调仓），**陈旧快照比没有更危险**——它长得跟新数据一模一样，读者分辨不出。回退到一个新鲜的次选源，好过回退到一个过期的首选源。所以第一级取不到就往下走，而不是去翻上周的存档。

**实测能力（2026-09-03，勿再重新调查）**：yfinance 这一级对新 ETF 几乎无用——**LYTE 0 笔、NCLD 0 笔、DRAM 5 笔、SMH 10 笔、SOXX 10 笔**。也就是说三档 Roundhill 里只有 DRAM 落得到第二级，且只有 5 笔；LYTE / NCLD 一旦 AV 取不到就直接掉到第三级，只能人工翻官网。

一个反直觉的互补性：DRAM 在 AV 那边 `holdings[].symbol` **全是 `n/a`**（韩股成分给不出代码），yfinance 反而给对了（`005930.KQ` = 三星）。两家弱点互补，**但绝不能因此把两边的数字合并**——一个是全量、一个是 top-5，口径不同。

（可选 flag：`--tickers LYTE,NCLD` 只取部分标的、`--check` 做取数前自检、`--sleep N` 调请求间隔。周更走全量，不要加 `--tickers`。）

**API key 从环境变量 `AV_API_KEYS` 读，支持多个 key 用 `,` 分隔，某个 key 撞限流时脚本自动换下一个：**

```bash
export AV_API_KEYS=KEY1,KEY2,KEY3
```

脚本报「日配额耗尽」时**无法区分「这个 key 用完了」与「这个 key 打错了」**——Alpha Vantage 对无效 key 与配额耗尽的 key 返回**完全相同**的消息。两种情形都会被当成耗尽、换下一个 key；若所有 key 都在第一次调用就报耗尽，先怀疑 key 拼错，别怀疑配额。

**绝不把真实 key 写进本 repo 的任何文件**（公开仓库），也不要让它出现在运行结果正文、references 或 Slack 消息里——与频道 ID 同一条规矩。AV 的错误消息会**回显 key 原文**，异常文本还可能带出完整 URL（含 `apikey=`），脚本已做遮罩，粘贴脚本输出前仍要扫一眼。

**频率：每周跑一次就够。** ETF 持仓是慢变量（这几档是季度调仓），同一周内反复取毫无意义。免费层每日 25 次、约每秒 1 次，几档 ETF 绰绰有余。

**这是可选增强，不是必需依赖。** 本技能没有它照样跑完整流程。

**`AV_API_KEYS` 未设置时**：跳过第一级，直接从 yfinance 起——对 LYTE / NCLD 那是 0 笔，实际等于只剩第三级（人工翻官网）。**只有全量来源才有的字段**（逐笔全量成分、AUM、下面表里标 N/A 的那几项）记 `N/A`。**费率与上市日不受影响**——那两项 `rating-rules.md` 正文已按发行商官网核实过（三档均 0.65%），权威来源与 AV 无关，不因本步缺席而降级成 `N/A`，**正文 ①②③ 照常完整输出**。处置与第零步 `NOTIFICATION_SLACK_CHANNEL_ID` 未设置时完全一致：少一路增强，不少一段交付。限流用尽、字段回 `n/a`、整档抓空——同样记 `N/A`，**绝不估算**。

#### ⚠️ 口径红线一：top-N 来源下，有些派生量根本不可算

这是本小步最容易出错的地方，比 key 和限流都重要。**三级的口径不同，派生量不能一视同仁。** 落到 **yfinance（只有 top-N）** 这一级时：

| 派生量            | top-N 能不能算                                               |
|-------------------|--------------------------------------------------------------|
| 前三大合计        | N≥3 时**可算**——top-N 内的前三大就是全量口径下的前三大，同义 |　⚠**但有一个附加条件**：前三大里若混进现金/货币基金行项目，它与正文人工核过的「前三大＝三大**股票**」就不是同一口径。实测 DRAM 走 yfinance top-5 时第 3 名是货币基金 FGXXX 14.66%，算出 51.31%，而正文写的是 ≈73%，**差 22pt 却同名**。脚本会在 stdout 告警并在 `--json` 的 `derived.top3_cash_items` 里点名，引用前必须改口径或剔除。
| 前十大合计        | 仅 N≥10 可算；DRAM 只有 5 笔 → **记 N/A**                    |
| swap 部位合计     | **不可算 → 一律 N/A**（需全量持仓，top-N 无法计算）          |
| 非美股成分合计    | **不可算 → 一律 N/A**（需全量持仓，top-N 无法计算）          |
| 现金/国库券类合计 | **不可算 → 一律 N/A**（需全量持仓，top-N 无法计算）          |

后三项**绝不能拿 top-N 硬凑一个数出来**。`references/rating-rules.md` 正文那个陷阱——行销页把 **36% 国库券当成持仓**、算出「前十大 ≈ 82.9%」——正是靠**全量**持仓表才识破的。拿 top-N 去算「现金/国库券类合计」会原样复现同一个错误：top-N 里压根看不到那 36%，算出来的分母是错的，而结果看上去完全正常。宁可记 N/A。

#### ⚠️ 口径红线二：三级的数字不可拼进同一张表

AV（全量）、yfinance（top-N）、官网（权威）**口径互不相同**，也不是同一时点的快照（量级一致但逐位对不上是正常的，实测差异见 `references/rating-rules.md` 顶部编者注）。

- 一张持仓表**只能用一个来源**，要嘛整表 AV、要嘛整表官网，**不要各取一半**——同表混列会造出一份两个时点缝合起来、加总也对不上的「持仓表」；
- 报告里引用任何持仓数字，**必须写明「数据源 + 取数日」**：「Alpha Vantage，取数日 YYYY-MM-DD」／「yfinance top-N，取数日 YYYY-MM-DD」／「官方持仓表，取数日 YYYY-MM-DD」。绝不能写成「官方持仓表」或让读者以为它是；
- 脚本对每档输出都带 `source` 标注，**照抄它**，不要凭记忆写。

#### ⚠️ 口径红线三：AV 与 yfinance 都不是官方持仓表

`etf_holdings.py` 前两级取回的都是**第三方聚合数据**，不是发行商的官方持仓表：

- **与发行商官方持仓表冲突时，一律以官方为准。** AV 是省掉人工翻网页的**便利来源**，不是**权威来源**；yfinance 更弱，连全量都没有；
- **不要因为能自动取数就停止取官方表。** 正文 `rating-rules.md` 要求的是「持仓一律以官方持仓表为准」——那是要求**取**官方表。若周更只抓 AV/yfinance，它就成了唯一实际取到的来源，「冲突时以官方为准」将永远触发不了：没有第二个数，就比不出冲突。**官方表仍须每周（或每次调仓后）取一次做校验。**
- `references/rating-rules.md` 正文那句「持仓一律以官方持仓表为准，抓不到就记 N/A，绝不估算」**依然有效**，本小步没有取消它；正文对 DRAM「取得官方持仓表前，不得据『swap』断言其为杠杆」的约束也**不因拿到 AV 或 yfinance 数据而解除**。

这与本仓库既有的「**不要用口径不同的替代源去比对原口径的阈值**」（见 `daily-risk-monitor` 的口径陷阱一节）是同一条规矩，只是换了个场景：宁可记 N/A，也不要拿一个来源的数字去顶另一个来源的口径。

## 第二步 · 重算评级

**读 `references/rating-rules.md` 全文**，按其中的：

- **8 层归类**（L1 计算核心 … L8 太空）
- **四大瓶颈**（① CoWoS ② HBM ③ 3nm/2nm ④ 数据中心电力，卡瓶颈打 🔥 并注编号）
- **四档评级**（🟢 强烈关注 / 🔵 关注 / 🟡 观望 / 🔴 迴避）
- **一致性阈值**（亏损或 FCF 为负且目标价低于现价 → 压 🟡；fwdPE>80 无对等增速 或 目标价远低于现价(>20%) → 至多 🔵；ETF 默认 🔵）
- **逐标的特例**（ADR/外币失真、DELL/RKLB/VRT、三档 Roundhill ETF、SPCX、0941.HK 等）

逐档重算。评级规则每周保持一致以减少主观漂移，**任何评级变动都必须有本周基本面依据**。

## 第三步 · 对比基准并输出

1. 取当前基准表：

   ```bash
   python3 "$SKILL_DIR/scripts/baseline.py" show
   python3 "$SKILL_DIR/scripts/baseline.py" meta   # {date, updated_at, count, path}
   ```

2. 把重算后的**整表**写到临时文件（可含 `<<<产业表开始>>>` / `<<<产业表结束>>>` 标记和说明文字，脚本只取以 `|` 开头的行，其余忽略）：

   ```bash
   # 例：/tmp/industry_table_new.md
   ```

   **该临时文件里只放产业表这一张表。** 解析是「取所有以 `|` 开头的行」，所以变动摘要那张 markdown 表格若混进同一个文件，它的行会被当成产业表数据行，直接污染 validate / diff / write。摘要另存一个文件，或干脆只留在正文里。

3. **先校验，再对比**（顺序不可颠倒）：

   ```bash
   python3 "$SKILL_DIR/scripts/baseline.py" validate /tmp/industry_table_new.md
   ```

   **exit 0 才继续。** 校验不过时脚本会逐条列错（行数与 `universe.json` 不符、代码集合或行序不对、评级不在合法四档、列数不对、含「见 Slack」类占位符等）——**回去修表，改完重跑 validate，通过后才做下一步**。绝不能带着未校验的表去 diff 或写正文：那样产出的摘要①是建立在一张可能缺行、串行、评级非法的表上的，等于交付了假摘要。

4. 生成变动摘要素材：

   ```bash
   python3 "$SKILL_DIR/scripts/baseline.py" diff /tmp/industry_table_new.md
   ```

   输出评级变动 + 其他字段漂移，作为「本周变动摘要」的事实底稿——但摘要里每条变动的**原因**要你自己按第一步数据写。

5. 按 **`references/output-format.md`** 把 ①②③ 三节（连同 📅 标题行共四节）完整写进运行结果正文。该文件里的硬性要求（必须逐行写出全表、禁止用「见 Slack」代替、即使无变动也要重贴整表）**逐条遵守**。

## 第四步 · 自动更新基准表

> **这是本技能相对原 prompt 的关键改动。** 原 prompt 用「内置种子基准表」——每周都跟同一张固定的表比。本技能改为**滚动文件基准**：`assets/baseline.md` 每周被新表覆写，所以每周是**跟上周比**。

```bash
python3 "$SKILL_DIR/scripts/baseline.py" write /tmp/industry_table_new.md --date <数据日期 YYYY-MM-DD>
```

执行时机（两条都是硬性的）：

- **必须在正文输出完 ①②③ 之后**——正文才是交付物，基准更新是副作用。
- **必须在第五步 Slack 推送之前**——这样即使 Slack 失败，基准也已经滚动到位。

`write` 会再跑一遍 `validate`（行数与 `universe.json` 一致、代码集合与行序一致、评级在合法四档内、列数正确）——第三步已经单独跑过，正常情况下这里必过。**万一这里才报错**（例如你在第三步之后又改了临时文件），脚本拒写且完全不碰基准表，返工路径是完整的一轮：**修表 → 重跑 `validate` → 重跑 `diff` → 按新 diff 修正正文里已经写出的摘要① → 再 `write`**。只重跑 `write` 不算修好：正文里会留着一份按旧表算出来的错误摘要。脚本不提供 `--force` 之类的开关，也不要试图手改 `assets/baseline.md`。

给用户的说明（每次运行结束时提一句）：

- `assets/baseline.md` 每周被覆写是**预期行为**；在 git 仓库形态下 `git status` 会显示它被修改，这不是意外脏文件。

**关于版本历史与回滚——按安装形态分两种情况，别搞混：**

- **技能在 git 仓库内使用**（`$SKILL_DIR` 位于本 repo 的 `skills/ai-industry-weekly/`）：这是**推荐形态**，因为基准表的版本历史完全靠 git 保存。每周跑完提交一次：
  ```bash
  git -C "$SKILL_DIR" add assets/baseline.md
  # 回滚到某一周：
  git -C "$SKILL_DIR" checkout <commit> -- assets/baseline.md
  ```
- **技能装在 `~/.claude/skills/ai-industry-weekly/` 等非 git 目录**：那里不是 git 仓库，上面两条命令会直接 `fatal: not a git repository`。而 `write` 是原子覆写、**不保留上一版**，所以此形态下**覆写不可逆**——上周的基准表写完就没了。想要版本历史，请在 git 仓库形态下使用本技能，或自行 `git init "$SKILL_DIR"` 并每周提交。本技能不生成任何备份文件或快照目录。

## 第五步 · Slack 推送

细则见 **`references/output-format.md`「第四步」一节**：固定两条，第一条（摘要 + 关键数据漂移）发到频道，第二条（完整整表 + L8 太空曝险提醒 + 免责声明）以第一条的 `message_ts` 作 `thread_ts` 发到 thread，不勾 `reply_broadcast`。推送成功后把两条链接附在运行结果**整表之后**。

频道 ID 取自环境变量 `NOTIFICATION_SLACK_CHANNEL_ID`（第零步已读）。**运行结果正文、以及任何写进 repo 的文本里，只写 `$NOTIFICATION_SLACK_CHANNEL_ID` 占位符，绝不回填真实 ID。**

`NOTIFICATION_SLACK_CHANNEL_ID` 未设置 → 跳过本步，在正文末尾注明「本次未推送 Slack（`NOTIFICATION_SLACK_CHANNEL_ID` 未设置）」。这**不影响**正文 ①②③ 的完整性要求。

## 第六步 · 交付自检

逐条核对 `references/output-format.md`「第五步」的 7 条清单（标记齐全 / 实际数出全部数据行 / 代码集合与 universe.json 一致 / 每条评级变动有依据 / 缺失记 N/A / Slack 两条已发且链接已附 / 无「详见 Slack」式省略），任一项为「否」就补齐后再结束。

**外加第 8 条**：

8. [ ] `python3 "$SKILL_DIR/scripts/baseline.py" write` 是否已成功执行（exit 0），且 `python3 "$SKILL_DIR/scripts/baseline.py" meta` 打印的 `date` 等于本次数据日期？

## 增减标的

只改 `assets/universe.json` —— 它的 `tickers` **顺序即产业表行序**。改完后：

- `fetch_fundamentals.py` 下次运行自动按新清单取数；
- `baseline.py validate` 下次运行按新清单校验，行数与代码集合随之改变；
- 首次运行时旧 `baseline.md` 与新清单不符属正常，`diff` 会把新增/删除的标的列出来，`write` 成功后即对齐。

**脱困指引**：若 `validate` 反复报「行数不符」或「代码重复」，而你怎么改表都过不了，**先别再改表——去看 `assets/universe.json`**：`tickers` 里是否有重复条目、或某条目缺 `ticker` 字段。清单本身有问题时，表永远对不上，只能先修清单。

不要在 SKILL.md、references 或脚本里另行硬编码标的数量或代码列表。

## 规则

- 评级规则每周保持一致，减少主观漂移，变动必须有基本面依据；
- 数据缺失记 N/A 绝不编造；
- 港股价格/52 周高低/距高点% 一律以 `scripts/hk_quote.py` 输出为准（原始未复权、实时），yfinance 仅用于财务字段；
- **完整产业表必须同时出现在【运行结果正文】与【Slack thread】两处；Slack 推送成功不构成运行结果可省略整表的理由**；
- 结束前跑一遍第六步自检清单；
- 仅研究框架，不构成投资建议。
