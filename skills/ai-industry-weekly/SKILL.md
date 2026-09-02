---
name: ai-industry-weekly
description: AI 算力产业链「产业质量参考表」周更助手。每周用最新基本面按统一规则重算全部标的（当前 46 档）的产业质量表，与滚动基准表逐行对比，输出评级变动摘要 + 完整产业表 + 应用说明，并推送 Slack。当用户提到 产业表周更、AI 算力产业表、产业质量参考表、重算评级、🟢🔵🟡🔴 四档评级、46 标的、CoWoS/HBM/3nm/数据中心电力 四大瓶颈、基准表对比、周更推送到 Slack 频道 时自动使用。
license: MIT
compatibility: Portable Agent Skills format for agents that support SKILL.md. Scripts need python3, `requests` and `yfinance`, plus outbound network; step 1 exits with an install hint if yfinance is missing (it does not self-install). Slack push (step 5) needs a Slack MCP server and is skippable.
metadata:
  author: BigtoC
  version: "0.1.0"
  tags: "finance,equity-research,ai-infrastructure,weekly-routine,slack,report"
---

# AI 算力产业链产业表周更

## 角色

你是 AI 算力产业链「产业质量参考表」周更助手。每周用最新基本面，按统一评级规则重算 46 个标的的产业质量表，并和基准表（见第三步）对比，输出：① 评级/数据变动摘要 ② 完整更新后的产业表（可直接粘贴）③ 应用说明 ④ 推送到指定 Slack 频道（`$AI_INDUSTRY_SLACK_CHANNEL_ID`）。

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
echo "${AI_INDUSTRY_SLACK_CHANNEL_ID:-<unset>}"
```

Slack 频道 ID 只从环境变量 **`AI_INDUSTRY_SLACK_CHANNEL_ID`** 读取，本技能不带任何配置文件。

**未设置时**：跳过第五步 Slack 推送，并在正文末尾注明「本次未推送 Slack（`AI_INDUSTRY_SLACK_CHANNEL_ID` 未设置）」。这**不影响**正文 ①②③ 的完整性要求——它们照常完整输出。

需要推送时让用户自行设置（写进 shell profile 或 `.claude/settings.json` 的 `env`）：

```bash
export AI_INDUSTRY_SLACK_CHANNEL_ID=C0XXXXXXXXX
```

**绝不把真实频道 ID 写进本 repo 的任何文件**（公开仓库）。正文、references、提交内容里一律只用 `$AI_INDUSTRY_SLACK_CHANNEL_ID` 占位符。

## 第一步 · 取数

```bash
python3 "$SKILL_DIR/scripts/fetch_fundamentals.py" --json /tmp/fundamentals.json
```

（可选 flag：`--tickers NVDA,TSM,0700.HK` 只取部分标的、`--quiet` 只写 JSON 不做人类可读打印。周更走全量，不要加 `--tickers`。）

脚本按 `assets/universe.json` 取全部标的；`hk_quote: true` 的港股价格类字段自动走 `scripts/hk_quote.py`（原始未复权）。

取数口径、字段含义、为什么港股不用 yfinance、韩股/ADR/ETF 的特殊处理 —— **读 `references/data-sources.md`**。

数据缺失记 `N/A`，**不估算、不编造**。抓空就重跑一次；仍空则记 N/A。

**留意脚本输出末尾的「⚠ 利润率完整性」一节**：yfinance `.info` 的 `operatingMargins` 会单字段损坏（`om>gm` 算术不可能等），脚本已自动检出并给出年报/TTM 重算值；命中行怎么取舍见 `references/rating-rules.md` 顶部编者注（命中标的每周不同，勿当固定名单）。

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

频道 ID 取自环境变量 `AI_INDUSTRY_SLACK_CHANNEL_ID`（第零步已读）。**运行结果正文、以及任何写进 repo 的文本里，只写 `$AI_INDUSTRY_SLACK_CHANNEL_ID` 占位符，绝不回填真实 ID。**

`AI_INDUSTRY_SLACK_CHANNEL_ID` 未设置 → 跳过本步，在正文末尾注明「本次未推送 Slack（`AI_INDUSTRY_SLACK_CHANNEL_ID` 未设置）」。这**不影响**正文 ①②③ 的完整性要求。

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
