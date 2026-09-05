#!/usr/bin/env bash
# fred.sh —— FRED 序列取数（信号 1 / 4 / 5 / 23 / 24 / 27 / 30 等共用）
#
# ┌─ 踩坑记录（references/known-traps.md，逐条都是实测换来的）───────────────┐
# │ 1. **绝不给 FRED 送浏览器 UA**（送了 25–30s 超时）。本脚本用 curl 是为   │
# │    了不引入 Python 套件依赖；`requests` 其实也打得通（实测 0.50s）。     │
# │    不要「顺手」把这支脚本改写成 python。                                 │
# │ 2. **不要加自订 User-Agent**。姊妹技能 neocloud_credit_monitor.py 实测： │
# │    带 UA 请求 FRED 会挂住直到超时，不带 UA 反而稳定 200。                │
# │    ⚠️ 这与 yfinance 的要求**方向相反**（yfinance 必须 requests.Session   │
# │       + UA，用 urllib 会 SSL 验证失败）。两条规则相反，最容易搞混。      │
# │ 3. FRED 用 `.` 表示缺值 → 必须跳过，取最近一个**有值**的点，            │
# │    并回报该点的日期与滞后天数/周数。                                     │
# │    （「数据暂缺」不报滞后周数 = 不合格输出，见行为准则第 1 条。）        │
# │ 4. 单位不一致：WALCL / WTREGEN = **百万**，RRPONTSYD = **十亿**。       │
# │    前两者 ÷1000 才能与 RRP 相加减。                                     │
# │ 5. `id=A,B,C` 多序列会回 **ZIP**，不是 CSV → 一个序列一次请求。         │
# │ 6. Buffett Indicator（信号 27）：NCBEILQ027S 与 GDP 的**末行常不同季**   │
# │    （例如股权数据到 Q1、GDP 已到 Q2）。各取各的末行相除 = 混用不同季度。│
# │    → `--buffett` 模式先按 date 做内连接（等价 merge(on="date")）再取末行。│
# │ 7. 多档 awk 合并**不可以数档案**（`FNR==1 { f++ }`）：空档案永远不触发   │
# │    FNR==1，之后每一档都往前挪一格 —— RRPONTSYD 掉进 TGA 桶被 ÷1000 当成  │
# │    百万。今天唯一挡着的是 14 行之前那个 `[ -s ]` 守卫；守卫一松，挪位后  │
# │    仍有料的组合算出来的数字会长得完全正常。一律用 FILENAME 对号入座，    │
# │    并在 END 做元数自检（桶对不上就退 3，不给「先算出来再说」留活路）。   │
# └──────────────────────────────────────────────────────────────────────────┘
#
# 依赖：bash（3.2 即可）、curl、awk、sed、date。**不需要 jq**（FRED 回 CSV）。
# 退出码：0 正常｜1 参数错误｜2 依赖缺失｜3 取数失败（数据暂缺）｜4 量级自检不通过

set -euo pipefail

PROG="$(basename "$0")"
FRED_CSV="https://fred.stlouisfed.org/graph/fredgraph.csv"
TIMEOUT=40

# 净流动性量级自检区间，单位十亿美元（= 5–7 兆美元，行为准则第 6 条）
NL_MIN_BN=5000
NL_MAX_BN=7000

# Buffett Indicator（信号 27）量级自检区间与触发阈值，单位 %（行为准则第 6 条）
BI_MIN_PCT=50
BI_MAX_PCT=250
BI_TRIGGER_PCT=200

usage() {
  cat <<EOF
${PROG} —— FRED 序列取数（免 API key）

用法:
  ${PROG} <SERIES_ID> [SERIES_ID ...] [选项]
  ${PROG} --net-liquidity [选项]
  ${PROG} --buffett [选项]

选项:
  --days N          输出最近 N 笔**有值**观测（默认 1）
                    --net-liquidity 模式下 N = 最近 N 个 WALCL 观测周
                    --buffett 模式下 N = 最近 N 个**同季对齐后**的季度
                    一般序列模式下**实得笔数不足 N 会照实印出实得笔数并标记降级**
                    （少拿了却照抄「最近 N 笔」= 把「没拿全」记成「查过了，没事」）
  --start YYYY-MM-DD  指定起始日期（默认按 --days 自动回看）
  --json            以 JSON 输出
  --net-liquidity   净流动性组合 = WALCL − WTREGEN − RRPONTSYD
                    自动做单位对齐（前两者百万÷1000，RRPONTSYD 十亿）
                    并做 ${NL_MIN_BN}–${NL_MAX_BN} 十亿（5–7 兆美元）量级自检
  --buffett         Buffett Indicator（信号 27）= NCBEILQ027S ÷ 1000 ÷ GDP × 100
                    **两序列先按 date 内连接做同季对齐再相除**——各取各的末行会
                    混用不同季度（known-traps 明列的陷阱，见档头第 6 条）。
                    并做 ${BI_MIN_PCT}–${BI_MAX_PCT}% 量级自检与 >${BI_TRIGGER_PCT}% 触发判定。
  -h, --help        显示本说明

例子:
  ${PROG} BAMLH0A0HYM2                 # 信号 1：HY OAS，最近一笔（值 + 日期 + 滞后）
  ${PROG} VIXCLS --days 5              # 信号 4：VIX 最近 5 笔
  ${PROG} VIXCLS VXVCLS                # 信号 4：VIX vs VIX3M 期限结构
  ${PROG} T10Y2Y T10Y3M --json         # 信号 23：收益率曲线
  ${PROG} SAHMREALTIME                 # 信号 24：Sahm Rule（月度，会报滞后周数）
  ${PROG} --net-liquidity --days 5     # 信号 5：净流动性最近 5 周（看是否连 4 周下降）
  ${PROG} WALCL WTREGEN RRPONTSYD --json   # 净流动性三个原始序列（未做单位对齐）
  ${PROG} --buffett                    # 信号 27：Buffett Indicator（同季对齐后的最新季）
  ${PROG} --buffett --days 8 --json    # 最近 8 个同季对齐季度的序列

常用序列:
  BAMLH0A0HYM2 HY OAS（百分点）    VIXCLS VIX         VXVCLS VIX3M
  WALCL Fed 总资产（百万）          WTREGEN TGA（百万） RRPONTSYD RRP（十亿）
  T10Y2Y / T10Y3M 收益率曲线        SAHMREALTIME Sahm  DFII10 10Y TIPS 实质殖利率
  NCBEILQ027S 股权市值代理（百万）  GDP（十亿）        —— 信号 27 需两者 merge 同季对齐
EOF
}

die()  { printf '错误：%s\n' "$1" >&2; exit "${2:-1}"; }
warn() { printf '%s\n' "$1" >&2; }

# $HOME 会被插进 sed 的 s|...| 表达式，所以必须先转义成 BRE 安全字串：
# 家目录含 `|`（撞到分隔符）、`.` `*` `[` `^` `$` `\`（改变比对语义）时，
# 直接内插会让 sed 报错或做出意外替换。空格不必转义（整条表达式是单一参数）。
HOME_RE="$(printf '%s' "${HOME:-/__no_home__}" | sed 's/[][\\^$.*|]/\\&/g')"

# 把家目录 / 临时目录绝对路径折叠掉。脚本输出会被贴进日报并推 Slack，
# 公开仓库不得泄漏本机用户名（CLAUDE.md「Public repo」）。
scrub() {
  sed -e "s|${HOME_RE}|~|g" \
      -e 's#/Users/[^/[:space:]"]*#~#g' \
      -e 's#/home/[^/[:space:]"]*#~#g' \
      -e 's#/var/folders/[^[:space:]"]*#~#g'
}

command -v curl >/dev/null 2>&1 || die "找不到 curl。本脚本是 shell 实作，取数走 curl。" 2
command -v awk  >/dev/null 2>&1 || die "找不到 awk。" 2

WORK="$(mktemp -d 2>/dev/null)" || die "无法建立临时目录。" 2
trap 'rm -rf "$WORK"' EXIT INT TERM

# ── 日期工具：同时支持 BSD(date -v/-j) 与 GNU(date -d) ──
_BSD_DATE=0
if date -v-1d +%Y-%m-%d >/dev/null 2>&1; then _BSD_DATE=1; fi

days_ago() {  # $1=天数 → YYYY-MM-DD
  if [ "$_BSD_DATE" -eq 1 ]; then date -v-"$1"d +%Y-%m-%d; else date -d "$1 days ago" +%Y-%m-%d; fi
}
to_epoch() {  # $1=YYYY-MM-DD → epoch 秒（失败回空字串）
  if [ "$_BSD_DATE" -eq 1 ]; then
    date -j -f "%Y-%m-%d" "$1" +%s 2>/dev/null || true
  else
    date -d "$1" +%s 2>/dev/null || true
  fi
}
lag_days() {  # $1=YYYY-MM-DD → 距今天数（失败回 -1）
  local e now
  e="$(to_epoch "$1")"
  [ -n "$e" ] || { echo -1; return 0; }
  now="$(date +%s)"
  echo $(( (now - e) / 86400 ))
}
lag_text() {  # $1=滞后天数 → 中文描述
  local d="$1"
  if [ "$d" -lt 0 ]; then echo "滞后未知"
  elif [ "$d" -le 9 ]; then echo "滞后 ${d} 天"
  else echo "滞后 $(( d / 7 )) 周（${d} 天）"
  fi
}

# ── 一次 curl --parallel 取回全部序列 → 每条一个 $WORK/<id>.csv ──
# 实测（6 条序列）：串行 3.26 秒 → 并行 1.08 秒。
# ⚠️ --parallel 的**完成顺序不等于命令列顺序**。所以这支函式只做「把每条序列
#    写进以 id 命名的档案」这一件事，顺序敏感的归并一律留给父层的串行迴圈
#    （JSON series 阵列顺序 = 人类分支印出顺序 = $IDS 顺序，三者必须一致，
#     见档尾那行注解）。累加器（ANY_FAIL / JSON_PARTS / DEG_JSON）也因此
#    绝不能跑进子 shell —— 子 shell 里的赋值出了 ) 就没了。
# `-w '%{url_effective} %{http_code}\n'` 让**每一笔**传输自己报出 URL 与状态码，
# 「哪一条挂了」才逐条归得回去；curl 的**总**退出码做不到这件事。
fetch_parallel() {  # $1=起始日期  $2..=SERIES_ID...；逐条结果写进 $WORK/<id>.state
  local start="$1"; shift
  local id code err rc=0
  local -a args
  # 注意：这里刻意不带 -H "User-Agent: ..."（见档头踩坑记录第 2 条）
  args=( -sS --max-time "$TIMEOUT" --retry 2 --retry-delay 2 \
         --parallel --parallel-max 6 -w '%{url_effective} %{http_code}\n' )
  for id in "$@"; do
    # 先把档案建出来：传输失败时 curl 根本不会产生 -o 档，后面要有东西可读。
    : > "$WORK/$id.csv"
    args+=( -o "$WORK/$id.csv" --url "${FRED_CSV}?id=${id}&cosd=${start}" )
  done
  : > "$WORK/http.status"
  curl "${args[@]}" > "$WORK/http.status" 2> "$WORK/curl.err" || rc=$?
  # curl 的 stderr 是**整批共用**的一份，没办法逐条切开（错误行里不带 URL）。
  # 所以这里去重后原样附在每条失败讯息后面，并在措辞上讲明「这是整批的」——
  # 把整批 stderr 讲成某一条自己的，就是在猜。
  err="$(scrub < "$WORK/curl.err" | awk '!seen[$0]++' | tr '\n' ' ')"
  for id in "$@"; do
    # 用 URL 里的 `id=<ID>&` 对号入座；结尾那个 & 让 GDP 不会比对到 GDPC1。
    code="$(awk -v pat="id=${id}&" 'index($0, pat) { c=$NF } END { print c }' "$WORK/http.status")"
    if [ -z "$code" ] || [ "$code" = "000" ]; then
      # 「哪一条挂了」靠的是上面那笔 000（或整行缺席），不是 $rc —— 并行模式下
      # $rc 与 stderr 都是**整批**的，讲成这一条专属的会误导排查方向。
      printf 'FRED 连线失败（curl 整批退出码 %s；下列 stderr 属整批并行传输，未逐条切分）：%s\n' \
        "$rc" "$err" > "$WORK/$id.state"
    elif [ "$code" != "200" ]; then
      printf 'FRED 回 HTTP %s\n' "$code" > "$WORK/$id.state"
    elif ! head -n 1 "$WORK/$id.csv" | grep -qiE '^(observation_date|DATE),'; then
      printf 'FRED 回传的不是 CSV（序列名可能拼错；或误用了 id=A,B,C 多序列写法——那会回 ZIP）\n' > "$WORK/$id.state"
    else
      printf 'ok\n' > "$WORK/$id.state"
    fi
  done
  return 0
}

# ── 逐条检查取数结果；失败时印出与串行版逐字相同的 ⚪️ 告警，回 3 ──
fetch_ok() {  # $1=SERIES_ID
  local id="$1" st
  st="$(cat "$WORK/$id.state" 2>/dev/null || true)"
  # 写成 if 而不是 `[ ] && return 0`——理由同下方参数检查处那条注解。
  if [ "$st" = "ok" ]; then return 0; fi
  warn "⚪️ ${id}：${st:-未取数（fetch_parallel 没跑到这条）}"
  return 3
}

# 抽出有值观测（跳过 FRED 的 `.` 缺值），输出 `YYYY-MM-DD,值` 每行一笔
valid_rows() { awk -F, 'NR>1 { sub(/\r$/,""); if ($2 != "" && $2 != ".") print $1 "," $2 }' "$1"; }

# ── 参数解析 ──
DAYS=1
START=""
JSON=0
NETLIQ=0
BUFFETT=0
IDS=""

while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    --json) JSON=1; shift ;;
    --net-liquidity) NETLIQ=1; shift ;;
    --buffett) BUFFETT=1; shift ;;
    --days)
      [ $# -ge 2 ] || die "--days 需要一个数字。" 1
      case "$2" in ''|*[!0-9]*) die "--days 的值必须是正整数，收到「$2」。" 1 ;; esac
      [ "$2" -ge 1 ] || die "--days 至少为 1。" 1
      DAYS="$2"; shift 2 ;;
    --start)
      [ $# -ge 2 ] || die "--start 需要一个 YYYY-MM-DD 日期。" 1
      echo "$2" | grep -qE '^[0-9]{4}-[0-9]{2}-[0-9]{2}$' || die "--start 格式必须是 YYYY-MM-DD，收到「$2」。" 1
      START="$2"; shift 2 ;;
    -*) die "未知选项「$1」。用 ${PROG} --help 看用法。" 1 ;;
    *)
      echo "$1" | grep -qE '^[A-Za-z0-9_.-]+$' || die "序列 ID 只能是英数字与 _ . -，收到「$1」。" 1
      IDS="${IDS}${IDS:+ }$1"; shift ;;
  esac
done

# 注意：这里刻意写成 if 而不是 `[ ] && [ ] && die`——后者在条件不成立时
# 整条 list 回非零，`set -e` 会直接把脚本结束掉。
if [ "$NETLIQ" -eq 1 ] && [ "$BUFFETT" -eq 1 ]; then
  die "--net-liquidity 与 --buffett 是两个不同的组合模式，一次只能给一个。" 1
fi

# ── 降级台帐（三个模式共用）──
# CLAUDE.md「JSON equivalence」第 4 条：每一个降级都要同时是 JSON 字段。
# stderr 与退出码是**冗余**通道，不是唯一通道——只读 stdout 的调用方看不到它们。
# 理由字串会直接内插进 JSON，所以只放中文与数字，不放引号/反斜线。
DEGRADED=0
DEG_JSON=""
add_degraded() {  # $1=简短理由
  DEGRADED=1
  DEG_JSON="${DEG_JSON}${DEG_JSON:+,}\"$1\""
}

# ═══════════════════════════ 净流动性模式 ═══════════════════════════
if [ "$NETLIQ" -eq 1 ]; then
  [ -z "$IDS" ] || die "--net-liquidity 与序列 ID 不能同时给（组合的三个序列是固定的）。" 1

  if [ -n "$START" ]; then NL_START="$START"; else NL_START="$(days_ago $(( DAYS * 7 + 120 )))"; fi

  # 三条一次并行取回；顺序敏感的部分（哪一条失败要挂哪一条的名字）留在下面串行做。
  fetch_parallel "$NL_START" WALCL WTREGEN RRPONTSYD

  FAILED=""
  for id in WALCL WTREGEN RRPONTSYD; do
    if fetch_ok "$id"; then
      valid_rows "$WORK/$id.csv" > "$WORK/$id.rows"
      [ -s "$WORK/$id.rows" ] || { FAILED="${FAILED}${FAILED:+, }${id}(无有值观测)"; }
    else
      FAILED="${FAILED}${FAILED:+, }${id}"
    fi
  done
  if [ -n "$FAILED" ]; then
    warn "⚪️ 净流动性数据暂缺 —— 已尝试来源：FRED fredgraph.csv（WALCL / WTREGEN / RRPONTSYD）"
    die "以下序列取数失败：${FAILED}。不得以记忆或推断填补（行为准则第 1 条）。" 3
  fi

  # 以 WALCL（周度，通常周三）为主轴；TGA / RRP 取「日期 ≤ 该周三」的最近一笔。
  # 单位对齐在这里一次做完：WALCL、WTREGEN 百万 ÷1000 → 十亿；RRPONTSYD 本就是十亿。
  #
  # ⚠️ 桶位**用 FILENAME 对号入座，不数档案**（档头踩坑记录第 7 条）：旧版的
  #    `FNR==1 { f++ }` 数的是「读到过的档案」，空档案永远不触发 FNR==1，
  #    RRPONTSYD 就整个挪进 TGA 桶再被 ÷1000 当成百万。
  #    实测（拿掉上面那个 `[ -s ]` 守卫、清空 WTREGEN）：旧版报的是
  #    「没有一个 WALCL 观测周能同时对上 TGA 与 RRP，请加大 --days」——
  #    诊断挂错科，加多长的回看都救不了；桶位一旦挪得「刚好有料」，
  #    出来的就不是错误讯息而是一张挂错序列的表了。
  #    END 的元数自检（三个桶都要有料、档案数刚好等于传进来的 3）是最后一道闸。
  NL_RC=0
  awk -F, -v n="$DAYS" \
      -v fw="$WORK/WALCL.rows" -v ft="$WORK/WTREGEN.rows" -v fr="$WORK/RRPONTSYD.rows" '
    FILENAME == fw { wd[++nw]=$1; wv[nw]=$2+0; fseen[fw]=1; next }
    FILENAME == ft { td[++nt]=$1; tv[nt]=$2+0; fseen[ft]=1; next }
    FILENAME == fr { rd[++nr]=$1; rv[nr]=$2+0; fseen[fr]=1; next }
    { printf "错误：合并时收到预期外的档案。\n" > "/dev/stderr"; bad=1; exit 3 }
    END {
      if (bad) exit 3
      k = 0; for (x in fseen) k++
      if (k != 3 || nw == 0 || nt == 0 || nr == 0) {
        printf "错误：三序列元数自检未通过 —— WALCL %d 笔／WTREGEN %d 笔／RRPONTSYD %d 笔，实际读到 %d 个有内容的档案（传进来 3 个）。\n", nw, nt, nr, k > "/dev/stderr"
        exit 3
      }
      start = nw - n + 1; if (start < 1) start = 1
      for (i = start; i <= nw; i++) {
        d = wd[i]
        tj = 0; for (j = nt; j >= 1; j--) if (td[j] <= d) { tj = j; break }
        rj = 0; for (j = nr; j >= 1; j--) if (rd[j] <= d) { rj = j; break }
        if (tj == 0 || rj == 0) continue
        walcl_bn = wv[i] / 1000.0          # 百万 → 十亿
        tga_bn   = tv[tj] / 1000.0         # 百万 → 十亿
        rrp_bn   = rv[rj]                  # 已是十亿，不要再除
        net_bn   = walcl_bn - tga_bn - rrp_bn
        printf "%s|%.1f|%.1f|%s|%.2f|%s|%.1f\n", d, walcl_bn, tga_bn, td[tj], rrp_bn, rd[rj], net_bn
      }
    }
  ' "$WORK/WALCL.rows" "$WORK/WTREGEN.rows" "$WORK/RRPONTSYD.rows" > "$WORK/nl.txt" || NL_RC=$?

  if [ "$NL_RC" -ne 0 ]; then
    warn "⚪️ 净流动性数据暂缺 —— 三序列合并元数自检未通过（原因见上一行）"
    die "桶位对不上就不算：挪一格会把 RRPONTSYD 的十亿当成 TGA 的百万，算出来的净流动性照样长得很正常。" 3
  fi

  [ -s "$WORK/nl.txt" ] || die "三个序列取到了，但没有一个 WALCL 观测周能同时对上 TGA 与 RRP。请加大 --days 或 --start 回看范围。" 3

  LAST_LINE="$(tail -n 1 "$WORK/nl.txt")"
  LAST_DATE="$(echo "$LAST_LINE" | cut -d'|' -f1)"
  LAST_NET="$(echo "$LAST_LINE" | cut -d'|' -f7)"
  LAG="$(lag_days "$LAST_DATE")"

  SANITY_OK=1
  if awk -v v="$LAST_NET" -v lo="$NL_MIN_BN" -v hi="$NL_MAX_BN" 'BEGIN{ exit (v>=lo && v<=hi) ? 0 : 1 }'; then :; else SANITY_OK=0; fi
  [ "$SANITY_OK" -eq 1 ] || add_degraded "量级自检未通过：净流动性 ${LAST_NET} 十亿，落在 ${NL_MIN_BN}–${NL_MAX_BN} 十亿之外"

  if [ "$JSON" -eq 1 ]; then
    NL_RANGE_TN="$(awk -v a="$NL_MIN_BN" -v b="$NL_MAX_BN" 'BEGIN{printf "%g–%g", a/1000, b/1000}')"
    {
      # ok 不是字面量：量级自检没过就是 false（写死 true 而 sanity.pass 是 false，
      # 等于让最显眼的字段说谎，真相却藏在最不显眼的那个）。
      printf '{"ok":%s,"mode":"net_liquidity","unit":"十亿美元","source":"FRED fredgraph.csv",' \
        "$([ "$SANITY_OK" -eq 1 ] && echo true || echo false)"
      printf '"formula":"WALCL/1000 - WTREGEN/1000 - RRPONTSYD","points":['
      awk -F'|' '{
        if (NR>1) printf ","
        printf "{\"date\":\"%s\",\"walcl_bn\":%s,\"tga_bn\":%s,\"tga_date\":\"%s\",\"rrp_bn\":%s,\"rrp_date\":\"%s\",\"net_bn\":%s}", $1,$2,$3,$4,$5,$6,$7
      }' "$WORK/nl.txt"
      printf '],"latest":{"date":"%s","net_bn":%s,"lag_days":%s},' "$LAST_DATE" "$LAST_NET" "$LAG"
      # 文字分支印的「本脚本不判 SPX」也必须是字段：没判过的触发是 null，不是 false
      # （false = 判过了、没触发）。理由字串与文字分支那行同源。
      printf '"trigger":{"signal":5,"rule":"连续 4 周下降且 SPX 同期上涨","fired":null,'
      printf '"reason":"环比栏连 4 个负值才算，本脚本不判 SPX。"},'
      printf '"sanity":{"range_bn":[%s,%s],"pass":%s},' "$NL_MIN_BN" "$NL_MAX_BN" "$([ "$SANITY_OK" -eq 1 ] && echo true || echo false)"
      # 禁令也要是字段：下面这段文字与 exit 4 前的 stderr 告警逐字同源。
      if [ "$SANITY_OK" -eq 1 ]; then
        printf '"do_not_quote":null,'
      else
        printf '"do_not_quote":{"reason":"量级自检未通过：净流动性 %s 十亿，落在 %s–%s 十亿（%s 兆美元）之外。行为准则第 6 条：算出来量级不对，先怀疑单位，不要直接报出来。最可能的原因：FRED 改了某个序列的单位（WALCL/WTREGEN 百万、RRPONTSYD 十亿）。请先人工核对再引用这个数字。","observed_bn":%s,"expected_range_bn":[%s,%s]},' \
          "$LAST_NET" "$NL_MIN_BN" "$NL_MAX_BN" "$NL_RANGE_TN" "$LAST_NET" "$NL_MIN_BN" "$NL_MAX_BN"
      fi
      printf '"degraded":%s,"degraded_reasons":[%s]}\n' \
        "$([ "$DEGRADED" -eq 1 ] && echo true || echo false)" "$DEG_JSON"
    }
  else
    echo "净流动性 = WALCL − WTREGEN − RRPONTSYD（单位：十亿美元；WALCL/WTREGEN 已 ÷1000 由百万转十亿）"
    echo "来源：FRED fredgraph.csv"
    echo
    echo "栏位：date=WALCL 观测周｜WALCL=Fed 总资产｜TGA=财政部一般帐户｜RRP=隔夜逆回购｜NET=净流动性｜WoW=环比"
    printf '%-12s %12s %12s %10s %12s %12s\n' "date" "WALCL" "TGA" "RRP" "NET" "WoW"
    awk -F'|' '{
      chg = (NR==1) ? "—" : sprintf("%+.1f", $7 - prev)
      printf "%-12s %12.1f %12.1f %10.2f %12.1f %12s\n", $1, $2, $3, $5, $7, chg
      prev = $7
    }' "$WORK/nl.txt"
    echo
    echo "最新：${LAST_NET} 十亿美元（约 $(awk -v v="$LAST_NET" 'BEGIN{printf "%.2f", v/1000}') 兆美元）@ ${LAST_DATE}，$(lag_text "$LAG")"
    TGA_D="$(echo "$LAST_LINE" | cut -d'|' -f4)"; RRP_D="$(echo "$LAST_LINE" | cut -d'|' -f6)"
    echo "  分量观测日：WALCL ${LAST_DATE}｜WTREGEN ${TGA_D}｜RRPONTSYD ${RRP_D}"
    echo "  触发条件（信号 5）：连续 4 周下降且 SPX 同期上涨 —— 环比栏连 4 个负值才算，本脚本不判 SPX。"
  fi

  if [ "$SANITY_OK" -ne 1 ]; then
    warn ""
    warn "⚠️ 量级自检未通过：净流动性 ${LAST_NET} 十亿，落在 ${NL_MIN_BN}–${NL_MAX_BN} 十亿（$(awk -v a="$NL_MIN_BN" -v b="$NL_MAX_BN" 'BEGIN{printf "%g–%g", a/1000, b/1000}') 兆美元）之外。"
    warn "   行为准则第 6 条：算出来量级不对，先怀疑单位，不要直接报出来。"
    warn "   最可能的原因：FRED 改了某个序列的单位（WALCL/WTREGEN 百万、RRPONTSYD 十亿）。"
    warn "   请先人工核对再引用这个数字。"
    exit 4
  fi
  exit 0
fi

# ═══════════════════ Buffett Indicator 模式（信号 27）═══════════════════
# known-traps.md：「Buffett Indicator 两序列末行日期常不同季 → 必须 merge(on="date")
# 后取末行」。这个模式就是把那条陷阱挡在脚本里，而不是留给人每次手动记得。
if [ "$BUFFETT" -eq 1 ]; then
  [ -z "$IDS" ] || die "--buffett 与序列 ID 不能同时给（组合的两个序列是固定的）。" 1

  # 季度序列：一个季度 ≈ 92 天，另加 400 天缓冲吸收 GDP 的发布滞后。
  if [ -n "$START" ]; then BI_START="$START"; else BI_START="$(days_ago $(( DAYS * 95 + 400 )))"; fi

  # 两条一次并行取回；哪一条失败仍逐条归得回去（见 fetch_ok）。
  fetch_parallel "$BI_START" NCBEILQ027S GDP

  FAILED=""
  for id in NCBEILQ027S GDP; do
    if fetch_ok "$id"; then
      valid_rows "$WORK/$id.csv" > "$WORK/$id.rows"
      [ -s "$WORK/$id.rows" ] || { FAILED="${FAILED}${FAILED:+, }${id}(无有值观测)"; }
    else
      FAILED="${FAILED}${FAILED:+, }${id}"
    fi
  done
  if [ -n "$FAILED" ]; then
    warn "⚪️ Buffett Indicator 数据暂缺 —— 已尝试来源：FRED fredgraph.csv（NCBEILQ027S / GDP）"
    die "以下序列取数失败：${FAILED}。不得以记忆或推断填补（行为准则第 1 条）。" 3
  fi

  # 同季对齐 = 只保留**两条序列都有观测**的那些季度（等价 pandas merge(on="date") 内连接），
  # 再取末行。单位对齐同时做完：NCBEILQ027S 百万 ÷1000 → 十亿；GDP 本就是十亿。
  #
  # ⚠️ 桶位同样**用 FILENAME 对号入座，不数档案**（档头踩坑记录第 7 条）：
  #    `FNR==1 { f++ }` 遇到空档案会让 GDP 的行掉进股权市值那一桶，内连接于是
  #    永远空手而归。实测（清空 NCBEILQ027S）旧版报的是「没有任何一个季度同时
  #    有两者的观测 → 无法同季对齐，请加大 --days」——这是本模式**最不该**误报的
  #    那句话（它正是陷阱 #6 的守门讯息），真因却是挂错桶。
  #    fseen 数的是**档案**，gseen 数的是**GDP 的季度**，两者不要混。
  BI_RC=0
  awk -F, -v n="$DAYS" \
      -v fe="$WORK/NCBEILQ027S.rows" -v fg="$WORK/GDP.rows" '
    FILENAME == fe { ed[++ne]=$1; ev[ne]=$2+0; fseen[fe]=1; next }
    FILENAME == fg { gv[$1]=$2+0; gseen[$1]=1; ng++; fseen[fg]=1; next }
    { printf "错误：合并时收到预期外的档案。\n" > "/dev/stderr"; bad=1; exit 3 }
    END {
      if (bad) exit 3
      k = 0; for (x in fseen) k++
      if (k != 2 || ne == 0 || ng == 0) {
        printf "错误：两序列元数自检未通过 —— NCBEILQ027S %d 笔／GDP %d 笔，实际读到 %d 个有内容的档案（传进来 2 个）。\n", ne, ng, k > "/dev/stderr"
        exit 3
      }
      m = 0
      for (i = 1; i <= ne; i++) if (gseen[ed[i]] && gv[ed[i]] > 0) { md[++m]=ed[i]; mv[m]=ev[i] }
      if (m == 0) exit 0
      start = m - n + 1; if (start < 1) start = 1
      for (i = start; i <= m; i++) {
        eq_bn = mv[i] / 1000.0        # 百万 → 十亿
        gdp_bn = gv[md[i]]            # 已是十亿，不要再除
        printf "%s|%.1f|%.1f|%.2f\n", md[i], eq_bn, gdp_bn, eq_bn / gdp_bn * 100
      }
    }
  ' "$WORK/NCBEILQ027S.rows" "$WORK/GDP.rows" > "$WORK/bi.txt" || BI_RC=$?

  if [ "$BI_RC" -ne 0 ]; then
    warn "⚪️ Buffett Indicator 数据暂缺 —— 两序列合并元数自检未通过（原因见上一行）"
    die "桶位对不上就不算：挂错桶的内连接会空手而归，看起来只像「这季还没对齐」。" 3
  fi

  EQ_LAST_DATE="$(tail -n 1 "$WORK/NCBEILQ027S.rows" | cut -d, -f1)"
  GDP_LAST_DATE="$(tail -n 1 "$WORK/GDP.rows" | cut -d, -f1)"

  if [ ! -s "$WORK/bi.txt" ]; then
    warn "两条序列都取到了（NCBEILQ027S 末行 ${EQ_LAST_DATE}｜GDP 末行 ${GDP_LAST_DATE}），"
    warn "但没有任何一个季度同时有两者的观测 → 无法同季对齐。"
    die "不得改用「各取各的末行相除」绕过——那正是本模式要挡掉的陷阱。请加大 --days 或用 --start 拉长回看。" 3
  fi

  LAST_LINE="$(tail -n 1 "$WORK/bi.txt")"
  LAST_DATE="$(echo "$LAST_LINE" | cut -d'|' -f1)"
  LAST_BI="$(echo "$LAST_LINE" | cut -d'|' -f4)"
  LAG="$(lag_days "$LAST_DATE")"

  SANITY_OK=1
  if awk -v v="$LAST_BI" -v lo="$BI_MIN_PCT" -v hi="$BI_MAX_PCT" 'BEGIN{ exit (v>=lo && v<=hi) ? 0 : 1 }'; then :; else SANITY_OK=0; fi
  [ "$SANITY_OK" -eq 1 ] || add_degraded "量级自检未通过：Buffett Indicator ${LAST_BI}%，落在 ${BI_MIN_PCT}–${BI_MAX_PCT}% 之外"

  FIRED=false
  if awk -v v="$LAST_BI" -v t="$BI_TRIGGER_PCT" 'BEGIN{ exit (v>t) ? 0 : 1 }'; then FIRED=true; fi

  # 两序列末行是否同季：文字分支据此印那句「**不要**改用各取各的末行相除」，
  # JSON 过去只给两个日期、把禁令丢了——已知陷阱 #6 只在人类输出里防守。
  if [ "$EQ_LAST_DATE" != "$GDP_LAST_DATE" ]; then
    LAST_OBS_DIFFER=true
    ALIGN_NOTE="⚠️ 两者末行不同季 —— 这正是 known-traps 记录的陷阱。上面的数字已用同季对齐算出，**不要**改用各取各的末行相除。"
  else
    LAST_OBS_DIFFER=false
    ALIGN_NOTE="（本次两者末行恰好同季；仍以对齐后的结果为准。）"
  fi

  if [ "$JSON" -eq 1 ]; then
    {
      # ok 不是字面量：量级自检没过就是 false（写死 true 而 sanity.pass 是 false，
      # 等于让最显眼的字段说谎，真相却藏在最不显眼的那个）。
      printf '{"ok":%s,"mode":"buffett_indicator","signal":27,"unit":"%%","source":"FRED fredgraph.csv",' \
        "$([ "$SANITY_OK" -eq 1 ] && echo true || echo false)"
      printf '"formula":"NCBEILQ027S/1000/GDP*100","aligned_on":"date（内连接，只取两序列都有观测的季度）",'
      printf '"series_last_obs":{"NCBEILQ027S":"%s","GDP":"%s"},' "$EQ_LAST_DATE" "$GDP_LAST_DATE"
      # 对齐口径与禁令都要是字段：措辞与文字分支逐字同源。
      printf '"alignment":{"method":"两序列按 date 内连接（同季对齐）后取末行","series_last_obs_differ":%s,' "$LAST_OBS_DIFFER"
      printf '"prohibition":"**不要**改用各取各的末行相除。","note":"%s"},' "$ALIGN_NOTE"
      printf '"points":['
      awk -F'|' '{
        if (NR>1) printf ","
        printf "{\"date\":\"%s\",\"equity_bn\":%s,\"gdp_bn\":%s,\"buffett_pct\":%s}", $1,$2,$3,$4
      }' "$WORK/bi.txt"
      printf '],"latest":{"date":"%s","buffett_pct":%s,"lag_days":%s},' "$LAST_DATE" "$LAST_BI" "$LAG"
      printf '"trigger":{"threshold_pct":%s,"fired":%s},' "$BI_TRIGGER_PCT" "$FIRED"
      printf '"sanity":{"range_pct":[%s,%s],"pass":%s},' "$BI_MIN_PCT" "$BI_MAX_PCT" "$([ "$SANITY_OK" -eq 1 ] && echo true || echo false)"
      # 禁令也要是字段：下面这段文字与 exit 4 前的 stderr 告警逐字同源。
      if [ "$SANITY_OK" -eq 1 ]; then
        printf '"do_not_quote":null,'
      else
        printf '"do_not_quote":{"reason":"量级自检未通过：Buffett Indicator %s%%，落在 %s–%s%% 之外。行为准则第 6 条：算出来量级不对，先怀疑单位，不要直接报出来。最可能的原因：FRED 改了 NCBEILQ027S（百万）或 GDP（十亿）的单位。请先人工核对再引用这个数字。","observed_pct":%s,"expected_range_pct":[%s,%s]},' \
          "$LAST_BI" "$BI_MIN_PCT" "$BI_MAX_PCT" "$LAST_BI" "$BI_MIN_PCT" "$BI_MAX_PCT"
      fi
      printf '"degraded":%s,"degraded_reasons":[%s]}\n' \
        "$([ "$DEGRADED" -eq 1 ] && echo true || echo false)" "$DEG_JSON"
    }
  else
    echo "Buffett Indicator = NCBEILQ027S ÷ 1000 ÷ GDP × 100（单位：%；NCBEILQ027S 已由百万转十亿）"
    echo "来源：FRED fredgraph.csv｜对齐：两序列按 date 内连接（同季对齐）后取末行"
    echo
    printf '%-12s %14s %14s %12s\n' "季度" "股权市值(十亿)" "GDP(十亿)" "Buffett%"
    awk -F'|' '{ printf "%-12s %14.1f %14.1f %12.2f\n", $1, $2, $3, $4 }' "$WORK/bi.txt"
    echo
    echo "最新（同季对齐）：${LAST_BI}% @ ${LAST_DATE}，$(lag_text "$LAG")"
    echo "  两序列各自末行：NCBEILQ027S ${EQ_LAST_DATE}｜GDP ${GDP_LAST_DATE}"
    if [ "$EQ_LAST_DATE" != "$GDP_LAST_DATE" ]; then
      echo "  ⚠️ 两者末行不同季 —— 这正是 known-traps 记录的陷阱。上面的数字已用同季对齐算出，"
      echo "     **不要**改用各取各的末行相除。"
    else
      echo "  （本次两者末行恰好同季；仍以对齐后的结果为准。）"
    fi
    if [ "$FIRED" = "true" ]; then
      printf '  触发判定（信号 27：>%s%%）... ✅ 触发\n' "$BI_TRIGGER_PCT"
    else
      printf '  触发判定（信号 27：>%s%%）... ❌ 未触发\n' "$BI_TRIGGER_PCT"
    fi
  fi

  if [ "$SANITY_OK" -ne 1 ]; then
    warn ""
    warn "⚠️ 量级自检未通过：Buffett Indicator ${LAST_BI}%，落在 ${BI_MIN_PCT}–${BI_MAX_PCT}% 之外。"
    warn "   行为准则第 6 条：算出来量级不对，先怀疑单位，不要直接报出来。"
    warn "   最可能的原因：FRED 改了 NCBEILQ027S（百万）或 GDP（十亿）的单位。"
    warn "   请先人工核对再引用这个数字。"
    exit 4
  fi
  exit 0
fi

# ═══════════════════════════ 一般序列模式 ═══════════════════════════
[ -n "$IDS" ] || { usage; exit 1; }

if [ -n "$START" ]; then
  DEF_START="$START"
else
  # 一季 95 天 + 400 天缓冲吸收发布滞后（与 --buffett 模式同一套算法）。
  # 旧式 DAYS*40+400 是照**日频**序列调的，季频序列一定短拿：
  # `GDP --days 20` 只回得到 13 笔。
  # ⚠️ 拉长回看只让「本来就存在的点」拿得回来，**不是**这个洞的修法：
  #    实得笔数仍然逐条与要求笔数比对，短少一律照实印出并标记降级（见下方 SHORT）。
  #    序列本身就没那么长（例如 2020 才开始的序列）时，加多长都还是短拿。
  LOOKBACK=$(( DAYS * 95 + 400 ))
  DEF_START="$(days_ago "$LOOKBACK")"
fi

ANY_FAIL=0
ANY_SHORT=0
JSON_PARTS=""

# 同一个 id 给两次时只取一次：并行下两笔传输会同时写同一个 -o 档，会互相踩烂。
# （$IDS 保持原样——它决定输出顺序，去重只影响「抓几次」。）
UNIQ_IDS=""
for id in $IDS; do
  case " ${UNIQ_IDS} " in *" ${id} "*) continue ;; esac
  UNIQ_IDS="${UNIQ_IDS}${UNIQ_IDS:+ }${id}"
done

# 一次并行取回全部序列；下面这圈**串行**归并，顺序完全由 $IDS 决定。
# $UNIQ_IDS 刻意不加引号：这里要的就是断词（一条 id 一个参数），同 `for id in $IDS`。
# shellcheck disable=SC2086
fetch_parallel "$DEF_START" $UNIQ_IDS

for id in $IDS; do
  if ! fetch_ok "$id"; then
    ANY_FAIL=1
    add_degraded "${id}：取数失败（已尝试来源：FRED fredgraph.csv）"
    if [ "$JSON" -eq 1 ]; then
      # short_return 是 null 不是 false：这一条根本没取到数，短拿判定**没判过**
      # （false = 判过了、没短拿）。
      JSON_PARTS="${JSON_PARTS}${JSON_PARTS:+,}{\"id\":\"${id}\",\"ok\":false,\"error\":\"取数失败\",\"days_requested\":${DAYS},\"observations_returned\":0,\"short_return\":null,\"observations\":[]}"
    else
      echo "${id}：⚪️ 数据暂缺（已尝试来源：FRED fredgraph.csv）。不得以记忆或推断填补。"
    fi
    continue
  fi

  valid_rows "$WORK/$id.csv" | tail -n "$DAYS" > "$WORK/$id.rows"

  if [ ! -s "$WORK/$id.rows" ]; then
    ANY_FAIL=1
    add_degraded "${id}：回看区间（自 ${DEF_START}）内全是 FRED 缺值符号「.」"
    if [ "$JSON" -eq 1 ]; then
      JSON_PARTS="${JSON_PARTS}${JSON_PARTS:+,}{\"id\":\"${id}\",\"ok\":false,\"error\":\"回看区间内全是缺值(.)\",\"days_requested\":${DAYS},\"observations_returned\":0,\"short_return\":null,\"observations\":[]}"
    else
      echo "${id}：⚪️ 数据暂缺 —— 回看区间（自 ${DEF_START}）内全是 FRED 缺值符号「.」。请用 --start 拉长回看。"
    fi
    continue
  fi

  LAST="$(tail -n 1 "$WORK/$id.rows")"
  LDATE="${LAST%%,*}"; LVAL="${LAST##*,}"
  LAG="$(lag_days "$LDATE")"

  # 要的笔数 vs 真正拿到的笔数。少拿了却照抄「最近 N 笔」，等于把「没拿全」
  # 记成「查过了，没事」——行为准则第 1 条挡的就是这个。
  GOT="$(awk 'END{print NR}' "$WORK/$id.rows")"
  if [ "$GOT" -lt "$DAYS" ]; then
    SHORT=true
    ANY_SHORT=1
    add_degraded "${id}：要求 ${DAYS} 笔有值观测，回看区间（自 ${DEF_START}）内只有 ${GOT} 笔"
  else
    SHORT=false
  fi

  if [ "$JSON" -eq 1 ]; then
    OBS="$(awk -F, '{ if (NR>1) printf ","; printf "{\"date\":\"%s\",\"value\":%s}", $1, $2 }' "$WORK/$id.rows")"
    # 要求笔数与实得笔数是**两个各自独立的字段**：只给其中一个，读的人就得自己
    # 去数 observations 的长度才知道有没有短拿——没人会数。
    JSON_PARTS="${JSON_PARTS}${JSON_PARTS:+,}{\"id\":\"${id}\",\"ok\":true,\"latest\":{\"date\":\"${LDATE}\",\"value\":${LVAL},\"lag_days\":${LAG}},\"days_requested\":${DAYS},\"observations_returned\":${GOT},\"short_return\":${SHORT},\"observations\":[${OBS}]}"
  else
    if [ "$DAYS" -eq 1 ]; then
      printf '%-14s %12s  @%s  %s\n' "$id" "$LVAL" "$LDATE" "$(lag_text "$LAG")"
    else
      # 标题一律印**实得**笔数；短拿时把要求笔数一起写出来，两个数字并列。
      if [ "$SHORT" = "true" ]; then
        echo "${id}  最近 ${GOT} 笔有值观测（要求 ${DAYS} 笔｜实得 ${GOT} 笔，最新 @${LDATE}，$(lag_text "$LAG")）"
      else
        echo "${id}  最近 ${DAYS} 笔有值观测（最新 @${LDATE}，$(lag_text "$LAG")）"
      fi
      awk -F, '{ printf "  %s  %12s\n", $1, $2 }' "$WORK/$id.rows"
      if [ "$SHORT" = "true" ]; then
        echo "  ⚠️ 回看区间（自 ${DEF_START}）内只有 ${GOT} 笔有值观测，少于要求的 ${DAYS} 笔。"
        echo "     引用时请照实写 ${GOT} 笔（这不是「最近 ${DAYS} 笔」），或用 --start 拉长回看。"
      fi
    fi
  fi
done

if [ "$JSON" -eq 1 ]; then
  # series 阵列的顺序 = 人类分支逐行印出的顺序 = 命令列上 $IDS 的顺序，三者必须一致。
  # `days` 是**要求**的笔数；真正拿到几笔看每条序列自己的 observations_returned。
  # ok 不是字面量：有任何一条短拿就不是 true——要了 20 笔只给 13 笔却回 ok:true、
  # degraded:false，正是「没拿全被记成查过了没事」的原形。
  # （短拿不套用退出码 3：3 是「取数失败」，这里数据是真的、只是不够长；
  #   降级走 degraded + ok:false 两个字段，不去挤占保留码。）
  printf '{"ok":%s,"source":"FRED fredgraph.csv","start":"%s","days":%s,"any_short_return":%s,"series":[%s],' \
    "$([ "$ANY_FAIL" -eq 0 ] && [ "$ANY_SHORT" -eq 0 ] && echo true || echo false)" \
    "$DEF_START" "$DAYS" "$([ "$ANY_SHORT" -eq 0 ] && echo false || echo true)" "$JSON_PARTS"
  printf '"degraded":%s,"degraded_reasons":[%s]}\n' \
    "$([ "$DEGRADED" -eq 1 ] && echo true || echo false)" "$DEG_JSON"
fi

[ "$ANY_FAIL" -eq 0 ] || exit 3
exit 0
