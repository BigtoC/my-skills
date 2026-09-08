#!/usr/bin/env bash
# cnn_fng.sh —— CNN Fear & Greed Index（信号 9）
#
# ┌─ 踩坑记录（references/known-traps.md）──────────────────────────────────┐
# │ CNN 端点**裸请求回 HTTP 418「I'm a teapot. You're a bot.」**。          │
# │ 必须同时带：                                                            │
# │   Referer: https://www.cnn.com/                                        │
# │   Origin:  https://www.cnn.com                                         │
# │   一个浏览器 User-Agent                                                 │
# │ 实测：只带 Referer + Origin、不带 UA，仍然回 418。三个都要。            │
# │ 被 418 / 403 挡下时**必须明说是被反爬挡了**，不能笼统写「取数失败」——   │
# │ 反爬和网路故障的处理方式完全不同。                                       │
# └────────────────────────────────────────────────────────────────────────┘
#
# 触发（references/signals-b-positioning.md 信号 9）：
#   >75 极度贪婪｜<25 极度恐惧（反向留意）
#   **7 项硬阈值之第 4 项**：从 >75 回落到 <50（贪婪破裂）
#   —— 这一项要看历史，不能只看当下一个数，故本脚本会扫近 N 日高点。
#
# 依赖：bash、curl、jq。退出码：0 正常｜1 参数错误｜2 依赖缺失｜3 取数失败（数据暂缺）

set -euo pipefail

PROG="$(basename "$0")"
ENDPOINT="https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
UA="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
TIMEOUT=30
PEAK_WINDOW=30      # 「从 >75 回落」的回看天数

usage() {
  cat <<EOF
${PROG} —— CNN Fear & Greed Index（信号 9）

用法:
  ${PROG} [选项]

选项:
  --json          以 JSON 输出（含近 N 日序列）
  --history N     额外列出最近 N 个交易日的读数（默认不列；--json 固定带 ${PEAK_WINDOW} 日）
  --peak-window N 「从 >75 回落到 <50」的回看天数（默认 ${PEAK_WINDOW}）
  -h, --help      显示本说明

例子:
  ${PROG}                 # 当前值 + 分档 + 前收盘 / 1週前 / 1月前 / 1年前对照
  ${PROG} --history 10    # 再列最近 10 个读数
  ${PROG} --json

口径说明:
  分档字串直接采用 CNN 自己回传的 rating 栏位，不自行改判。
  阈值判定用的是数值 score，与 rating 无关（行为准则第 3 条：阈值不随情绪调整）。
EOF
}

die()  { printf '错误：%s\n' "$1" >&2; exit "${2:-1}"; }
warn() { printf '%s\n' "$1" >&2; }

command -v curl >/dev/null 2>&1 || die "找不到 curl。" 2
if ! command -v jq >/dev/null 2>&1; then
  # 可读降级：不静默失败，把原始 JSON 吐出来让人肉眼看，并说清楚缺什么。
  warn "错误：找不到 jq，无法解析 CNN 回传的 JSON。"
  warn '     安装：macOS 用 brew install jq｜Debian/Ubuntu 用 apt-get install jq。'
  warn "     以下为未解析的原始回应，请人工读取 fear_and_greed.score / rating / timestamp："
  curl -sS --max-time "$TIMEOUT" \
    -H "User-Agent: ${UA}" -H "Referer: https://www.cnn.com/" \
    -H "Origin: https://www.cnn.com" -H "Accept: application/json" \
    "$ENDPOINT" || true
  echo
  exit 2
fi

HISTORY_N=0
JSON=0
while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    --json) JSON=1; shift ;;
    --history)
      [ $# -ge 2 ] || die "--history 需要一个正整数。" 1
      case "$2" in ''|*[!0-9]*) die "--history 的值必须是正整数，收到「$2」。" 1 ;; esac
      HISTORY_N="$2"; shift 2 ;;
    --peak-window)
      [ $# -ge 2 ] || die "--peak-window 需要一个正整数。" 1
      case "$2" in ''|*[!0-9]*) die "--peak-window 的值必须是正整数，收到「$2」。" 1 ;; esac
      PEAK_WINDOW="$2"; shift 2 ;;
    *) die "未知参数「$1」。用 ${PROG} --help 看用法。" 1 ;;
  esac
done

WORK="$(mktemp -d 2>/dev/null)" || die "无法建立临时目录。" 2
trap 'rm -rf "$WORK"' EXIT INT TERM

RC=0
CODE="$(curl -sS --max-time "$TIMEOUT" --retry 1 --retry-delay 2 \
          -o "$WORK/fng.json" -w '%{http_code}' \
          -H "User-Agent: ${UA}" \
          -H "Referer: https://www.cnn.com/" \
          -H "Origin: https://www.cnn.com" \
          -H "Accept: application/json" \
          "$ENDPOINT" 2>"$WORK/curl.err")" || RC=$?

if [ "$RC" -ne 0 ]; then
  warn "⚪️ CNN Fear & Greed 数据暂缺"
  warn "   已尝试来源：${ENDPOINT}（带 UA + Referer + Origin）"
  warn "   失败原因：**网路层面**失败，curl 退出码 ${RC}（非反爬）。"
  die "报告中请标 ⚪️ 数据暂缺，并写出上次已知读数与滞后周数。不得填任何数字。" 3
fi

case "$CODE" in
  200) : ;;
  418|403|429)
    warn "⚪️ CNN Fear & Greed 数据暂缺"
    warn "   已尝试来源：${ENDPOINT}"
    warn "   失败原因：**被反爬挡下**，HTTP ${CODE}$([ "$CODE" = "418" ] && echo "「I'm a teapot. You're a bot.」")。"
    warn "   本脚本已带齐 Referer / Origin / 浏览器 UA；仍被挡代表 CNN 调整了规则，或本机 IP 被列管。"
    warn "   这**不是**网路故障，重试同一请求通常无用；请改走 web_fetch cnn.com/markets/fear-and-greed。"
    die "报告中请标 ⚪️ 数据暂缺 +「CNN HTTP ${CODE} 反爬」，并写出上次已知读数与滞后周数。" 3 ;;
  *)
    warn "⚪️ CNN Fear & Greed 数据暂缺 —— 端点回 HTTP ${CODE}（已尝试：${ENDPOINT}）"
    die "报告中请标 ⚪️ 数据暂缺，并写出上次已知读数与滞后周数。" 3 ;;
esac

jq -e '.fear_and_greed.score' "$WORK/fng.json" >/dev/null 2>&1 || {
  warn "⚪️ CNN 回了 HTTP 200，但内容里没有 fear_and_greed.score 栏位（端点结构可能已变更）。"
  die "报告中请标 ⚪️ 数据暂缺，并写出上次已知读数与滞后周数。" 3
}

# @tsv 是 tab 分隔，但默认 IFS 含空格 —— rating 的 "extreme greed"/"extreme fear"
# 会被拆成两段，之后每个字段整体后移（且这恰好只发生在读数最极端时）。
# 必须把 IFS 限定成 tab。
IFS="$(printf '\t')" read -r SCORE RATING ASOF PREV W1 M1 Y1 <<EOF
$(jq -r '.fear_and_greed | [ (.score|tostring), .rating, .timestamp,
                             (.previous_close|tostring), (.previous_1_week|tostring),
                             (.previous_1_month|tostring), (.previous_1_year|tostring) ] | @tsv' "$WORK/fng.json")
EOF

# 近 N 日高点（用于硬阈值第 4 项「从 >75 回落到 <50」）
# 端点会在当日盘中重复追加同一天的点，先按日期去重（同日取最后一笔）再回看，
# 否则 --history N 会少列一天，近 N 日高点的窗口也会短一天。
DEDUP='[ (.fear_and_greed_historical.data // [])[]
         | {date: (.x/1000|floor|gmtime|strftime("%Y-%m-%d")), score: .y, rating: .rating} ]
       | group_by(.date) | map(.[-1])'

PEAK_LINE="$(jq -r --argjson n "$PEAK_WINDOW" "${DEDUP}"' | .[-$n:]
  | if length == 0 then "NA\tNA"
    else (max_by(.score)) | [ (.score|tostring), .date ] | @tsv
    end' "$WORK/fng.json")"
PEAK="${PEAK_LINE%%	*}"
PEAK_DATE="${PEAK_LINE##*	}"

# CNN rating → 中文（rating 直接沿用 CNN 的判定，不自行改判）
zh_rating() {
  case "$1" in
    "extreme fear")  echo "极度恐惧" ;;
    "fear")          echo "恐惧" ;;
    "neutral")       echo "中性" ;;
    "greed")         echo "贪婪" ;;
    "extreme greed") echo "极度贪婪" ;;
    *)               echo "$1" ;;
  esac
}

# 缺值绝不进 awk。端点改版整栏消失时，PREV/W1/M1/Y1 会是空字串，或是 jq `tostring`
# 把 null 转出来的字串 "null"（近 N 日高点缺序列时同理是 "NA"）。这三种喂进 awk：
#   fmt   → 印成「0.0」            —— 一个凭空捏造的读数
#   delta → 印成「↑ +41.9」        —— 一个凭空捏造的单日变动
# 而文字分支是要被逐字照抄进报告的那一份，所以缺就是缺：一律记 N/A，不估算也不补 0。
# 判定条件与 jnum 保持同一套口径（''|null），另加历史序列那边的 NA。
# 只筛 ''/null/NA 不够：**任何**非数值都会被下面 fmt 的 awk 当成 0 印出「0.0」，
# 而 0.0 在恐惧贪婪刻度上是「极度恐惧」——整条刻度上最可操作的那个读数。
# 端点改版回 "undefined"、回一段 HTML、甚至只是把 NA 写成小写 n/a，都会走到这里。
# 把「读不到」讲成「极度恐惧」比讲成「没事」更贵：它会直接催出一个买入动作。
has_num() {
  case "$1" in ''|null|NA) return 1 ;; esac
  awk -v v="$1" 'BEGIN{ exit (v ~ /^[+-]?([0-9]+\.?[0-9]*|\.[0-9]+)([eE][+-]?[0-9]+)?$/) ? 0 : 1 }'
}
fmt() {
  has_num "$1" || { printf 'N/A'; return 0; }
  awk -v v="$1" 'BEGIN{ printf "%.1f", v }'
}
delta() {
  # 两个操作数缺任一个就没有「变动」可言 —— 不是 0.0，也不是箭头，是算不出来。
  if ! has_num "$1" || ! has_num "$2"; then printf 'N/A（缺对照读数，不计算变动）'; return 0; fi
  awk -v a="$1" -v b="$2" 'BEGIN{ d=a-b; printf "%s %+.1f", (d>0.05?"↑":(d<-0.05?"↓":"→")), d }'
}
yesno() { if [ "$1" -eq 1 ]; then echo "✅ 触发"; else echo "❌ 未触发"; fi; }

T_GREED=0;  awk -v s="$SCORE" 'BEGIN{ exit (s>75)?0:1 }' && T_GREED=1
T_FEAR=0;   awk -v s="$SCORE" 'BEGIN{ exit (s<25)?0:1 }' && T_FEAR=1

# 硬阈值第 4 项要回看历史序列，端点没回序列时只能是「⚪️ 无法判定」——不是「❌ 未触发」。
# 文字分支一直分得清楚（⚪️ vs ❌），JSON 也必须分得清楚：**null ≠ false**。
# decision-framework.md：⚪️ 项不计入触发数，**也不计入分母**（N = 7 − M）；
# 只读 triggers.* 的调用方若把 false 当「查过了，没触发」，等于把「不知道」静默记成「安全」，
# 而这一格直接进 7 项硬阈值计数、计数又定战略仓位基准。
T_BURST=0
T_BURST_JSON=null       # 三态：null（无法判定）/ true / false
T_BURST_REASON=""       # 只有无法判定时才有值；措辞照抄文字分支那一行
if [ "$PEAK" != "NA" ]; then
  awk -v p="$PEAK" -v s="$SCORE" 'BEGIN{ exit (p>75 && s<50)?0:1 }' && T_BURST=1
  T_BURST_JSON="$([ "$T_BURST" -eq 1 ] && echo true || echo false)"
else
  T_BURST_REASON="⚪️ 无法判定（端点未回历史序列）—— 不计入触发数，也不计入分母（N = 7 − M），不得当成「❌ 未触发」。"
fi

# ── 自检与降级台帐 ──
# CLAUDE.md「JSON equivalence」：ok 不是字面量（自检没过就得是 false）；
# 每一个降级除了 stderr 一行，还必须是结构化字段（degraded / degraded_reasons）。
# 理由字串会进 JSON，只放中文与数字，不放引号/反斜线/换行。
SANITY_OK=1
awk -v s="$SCORE" 'BEGIN{ exit (s>=0 && s<=100)?0:1 }' || SANITY_OK=0

OK="$SANITY_OK"
DEGRADED=0
DEG_REASONS=""
add_degraded() {  # $1=简短理由（一行）
  DEGRADED=1
  DEG_REASONS="${DEG_REASONS}$1
"
}

# 四个对照读数是端点自己给的，改版时可能整栏消失。空字串喂给 jq --argjson 会让整份 JSON
# 产不出来（连 ⚪️ 都印不出），所以缺就是 null —— 记 N/A，不估算也不补 0。
# 同一个洞的 JSON 侧：非数值原样喂给 jq --argjson 会让 jq 解析失败、整份 JSON
# 产不出来（上面那段注解已记下空字串的情形，但没涵盖「回了别的东西」）。
jnum() { if has_num "$1"; then printf '%s\n' "$1"; else echo null; fi; }
PREV_J="$(jnum "$PREV")"; W1_J="$(jnum "$W1")"; M1_J="$(jnum "$M1")"; Y1_J="$(jnum "$Y1")"

[ "$SANITY_OK" -eq 1 ] || add_degraded "量级自检未通过：score ${SCORE} 落在 0–100 之外，端点结构可能已变更，数字不可引用"
[ "$PEAK" != "NA" ]    || add_degraded "硬阈值第 4 项无法判定：端点未回历史序列（回看 ${PEAK_WINDOW} 日窗口为空），记 ⚪️ 并从分母扣除"
[ "$PREV_J" != null ]  || add_degraded "前收盘读数缺失，记 N/A（不估算）"
[ "$W1_J" != null ]    || add_degraded "1 週前读数缺失，记 N/A（不估算）"
[ "$M1_J" != null ]    || add_degraded "1 月前读数缺失，记 N/A（不估算）"
[ "$Y1_J" != null ]    || add_degraded "1 年前读数缺失，记 N/A（不估算）"

if [ "$JSON" -eq 1 ]; then
  # --json 的 stdout 是给机器读的，stderr 才是给人看的那条通道，所以降级要在这里吼一声。
  # 文字分支不吼：它已经把 ⚪️ 那行印在 stdout 上了，再吼一次只是重复。
  if [ "$DEGRADED" -eq 1 ]; then
    warn "⚠️ 本次取数有降级（JSON 已带 degraded=true / degraded_reasons）："
    printf '%s' "$DEG_REASONS" | sed 's/^/   · /' >&2
  fi
  HIST="$(jq -c --argjson n "$PEAK_WINDOW" "${DEDUP}"' | .[-$n:]' "$WORK/fng.json")"
  DEG_JSON="$(printf '%s' "$DEG_REASONS" | jq -R -s 'split("\n") | map(select(length > 0))')"
  jq -n \
    --arg source "$ENDPOINT" \
    --argjson score "$SCORE" --arg rating "$RATING" --arg rating_zh "$(zh_rating "$RATING")" \
    --arg asof "$ASOF" \
    --argjson prev "$PREV_J" --argjson w1 "$W1_J" --argjson m1 "$M1_J" --argjson y1 "$Y1_J" \
    --arg peak "$PEAK" --arg peak_date "$PEAK_DATE" --argjson peak_window "$PEAK_WINDOW" \
    --argjson t_greed "$T_GREED" --argjson t_fear "$T_FEAR" \
    --argjson t_burst "$T_BURST_JSON" --arg t_burst_reason "$T_BURST_REASON" \
    --argjson ok "$OK" --argjson sanity "$SANITY_OK" \
    --argjson degraded "$DEGRADED" --argjson degraded_reasons "$DEG_JSON" \
    --argjson history "$HIST" \
    '{ok:($ok==1), degraded:($degraded==1), degraded_reasons:$degraded_reasons,
      signal:9, name:"CNN Fear & Greed Index", source:$source,
      score:$score, rating:$rating, rating_zh:$rating_zh, asof:$asof,
      previous_close:$prev, previous_1_week:$w1, previous_1_month:$m1, previous_1_year:$y1,
      peak:{window_days:$peak_window, value:(if $peak=="NA" then null else ($peak|tonumber) end), date:(if $peak=="NA" then null else $peak_date end)},
      caliber:"阈值判定严格按数值，不加软化语言；分档字串直接采用 CNN 回传的 rating，不自行改判。",
      sanity:{score_range:[0,100], pass:($sanity==1)},
      triggers:{extreme_greed_gt75:($t_greed==1), extreme_fear_lt25:($t_fear==1),
                hard_threshold_4_greed_burst:$t_burst,
                hard_threshold_4_greed_burst_reason:(if $t_burst_reason=="" then null else $t_burst_reason end)},
      history:$history}'
  exit 0
fi

echo "CNN Fear & Greed Index（信号 9）"
echo "来源：${ENDPOINT}（必带 Referer + Origin + 浏览器 UA，否则回 HTTP 418）"
echo
printf '  当前     %6s  %s / %s\n' "$(fmt "$SCORE")" "$RATING" "$(zh_rating "$RATING")"
printf '  as of    %s\n' "$ASOF"
echo
printf '  前收盘   %6s   %s\n' "$(fmt "$PREV")" "$(delta "$SCORE" "$PREV")"
printf '  1 週前   %6s   %s\n' "$(fmt "$W1")"   "$(delta "$SCORE" "$W1")"
printf '  1 月前   %6s   %s\n' "$(fmt "$M1")"   "$(delta "$SCORE" "$M1")"
printf '  1 年前   %6s   %s\n' "$(fmt "$Y1")"   "$(delta "$SCORE" "$Y1")"
echo
echo "阈值判定（严格按数值，不加软化语言）："
printf '  >75 极度贪婪 .......................... %s（%s）\n' "$(yesno "$T_GREED")" "$(fmt "$SCORE")"
printf '  <25 极度恐惧（反向留意） .............. %s（%s）\n' "$(yesno "$T_FEAR")"  "$(fmt "$SCORE")"
if [ "$PEAK" = "NA" ]; then
  printf '  硬阈值第 4 项「从 >75 回落到 <50」 .... ⚪️ 无法判定（端点未回历史序列）\n'
else
  printf '  硬阈值第 4 项「从 >75 回落到 <50」 .... %s\n' "$(yesno "$T_BURST")"
  printf '     近 %s 日高点 %s @ %s；今日 %s\n' "$PEAK_WINDOW" "$(fmt "$PEAK")" "$PEAK_DATE" "$(fmt "$SCORE")"
fi

if [ "$HISTORY_N" -gt 0 ]; then
  echo
  echo "最近 ${HISTORY_N} 个读数："
  jq -r --argjson n "$HISTORY_N" "${DEDUP}"' | .[-$n:] | .[]
    | "  \(.date)  \(.score*10|round/10)  \(.rating)"' "$WORK/fng.json"
fi
exit 0
