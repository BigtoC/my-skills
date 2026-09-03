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

fmt() { awk -v v="$1" 'BEGIN{ printf "%.1f", v }'; }
delta() { awk -v a="$1" -v b="$2" 'BEGIN{ d=a-b; printf "%s %+.1f", (d>0.05?"↑":(d<-0.05?"↓":"→")), d }'; }
yesno() { if [ "$1" -eq 1 ]; then echo "✅ 触发"; else echo "❌ 未触发"; fi; }

T_GREED=0;  awk -v s="$SCORE" 'BEGIN{ exit (s>75)?0:1 }' && T_GREED=1
T_FEAR=0;   awk -v s="$SCORE" 'BEGIN{ exit (s<25)?0:1 }' && T_FEAR=1
T_BURST=0
if [ "$PEAK" != "NA" ]; then
  awk -v p="$PEAK" -v s="$SCORE" 'BEGIN{ exit (p>75 && s<50)?0:1 }' && T_BURST=1
fi

if [ "$JSON" -eq 1 ]; then
  HIST="$(jq -c --argjson n "$PEAK_WINDOW" "${DEDUP}"' | .[-$n:]' "$WORK/fng.json")"
  jq -n \
    --arg source "$ENDPOINT" \
    --argjson score "$SCORE" --arg rating "$RATING" --arg rating_zh "$(zh_rating "$RATING")" \
    --arg asof "$ASOF" \
    --argjson prev "$PREV" --argjson w1 "$W1" --argjson m1 "$M1" --argjson y1 "$Y1" \
    --arg peak "$PEAK" --arg peak_date "$PEAK_DATE" --argjson peak_window "$PEAK_WINDOW" \
    --argjson t_greed "$T_GREED" --argjson t_fear "$T_FEAR" --argjson t_burst "$T_BURST" \
    --argjson history "$HIST" \
    '{ok:true, signal:9, name:"CNN Fear & Greed Index", source:$source,
      score:$score, rating:$rating, rating_zh:$rating_zh, asof:$asof,
      previous_close:$prev, previous_1_week:$w1, previous_1_month:$m1, previous_1_year:$y1,
      peak:{window_days:$peak_window, value:(if $peak=="NA" then null else ($peak|tonumber) end), date:(if $peak=="NA" then null else $peak_date end)},
      triggers:{extreme_greed_gt75:($t_greed==1), extreme_fear_lt25:($t_fear==1),
                hard_threshold_4_greed_burst:($t_burst==1)},
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
