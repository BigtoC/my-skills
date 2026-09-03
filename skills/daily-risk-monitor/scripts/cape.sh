#!/usr/bin/env bash
# cape.sh —— Shiller CAPE / PE10（信号 28）
#
# 解析配方（references/signals-e-cycle-valuation.md 信号 28，逐字照做）：
#   剥掉 <script> / <style> 与所有标签后，页面上「Current …Shiller PE Ratio」后的
#   **第一个浮点数**即当前值，随后依序是历史 **均值 / 中位数 / 最低 / 最高**。
#
# ┌─ 实作注记 ───────────────────────────────────────────────────────────────┐
# │ · multpl.com 要带浏览器 User-Agent（这一点与 FRED **相反**——FRED 带 UA  │
# │   反而会挂住到超时。两支脚本的规则方向相反，改动前先看清楚是哪一支）。   │
# │ · awk 没有非贪婪比对，剥 script/style 用「以结束标签切段、每段再从第一个 │
# │   开始标签处截断」的做法，等价于 python 的 (?s)<(script|style).*?</\1>。 │
# │ · 解析出来的五个数必须满足「min 最小、max 最大」，否则判定页面版型已变、 │
# │   解析错位 → 标数据暂缺，**绝不输出一组看起来合理但错位的数字**。        │
# │ · 量级自检：CAPE 应在 5–50（行为准则第 6 条）。超出即告警并以 4 退出。   │
# └──────────────────────────────────────────────────────────────────────────┘
#
# 依赖：bash、curl、awk、sed、tr。**不需要 jq**（来源是 HTML 不是 JSON）。
# 退出码：0 正常｜1 参数错误｜2 依赖缺失｜3 取数或解析失败（数据暂缺）｜4 量级自检不通过

set -euo pipefail

PROG="$(basename "$0")"
URL="https://www.multpl.com/shiller-pe"
UA="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
TIMEOUT=30

CAPE_MIN=5          # 量级自检下界
CAPE_MAX=50         # 量级自检上界
CAPE_TRIGGER=30     # 触发：>30（历史前 5% 区间）

usage() {
  cat <<EOF
${PROG} —— Shiller CAPE / PE10（信号 28）

用法:
  ${PROG} [--json]

选项:
  --json      以 JSON 输出
  -h, --help  显示本说明

输出:
  当前 CAPE + 历史均值 / 中位数 / 最低（含年月）/ 最高（含年月）+ 页面时间戳，
  以及 >${CAPE_TRIGGER} 的触发判定与 ${CAPE_MIN}–${CAPE_MAX} 量级自检结果。

例子:
  ${PROG}
  ${PROG} --json
EOF
}

die()  { printf '错误：%s\n' "$1" >&2; exit "${2:-1}"; }
warn() { printf '%s\n' "$1" >&2; }

command -v curl >/dev/null 2>&1 || die "找不到 curl。" 2
command -v awk  >/dev/null 2>&1 || die "找不到 awk。" 2

JSON=0
while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    --json) JSON=1; shift ;;
    *) die "未知参数「$1」。用 ${PROG} --help 看用法。" 1 ;;
  esac
done

WORK="$(mktemp -d 2>/dev/null)" || die "无法建立临时目录。" 2
trap 'rm -rf "$WORK"' EXIT INT TERM

RC=0
CODE="$(curl -sS --max-time "$TIMEOUT" --retry 2 --retry-delay 2 \
          -o "$WORK/page.html" -w '%{http_code}' \
          -H "User-Agent: ${UA}" \
          -H "Accept: text/html,application/xhtml+xml" \
          "$URL" 2>"$WORK/curl.err")" || RC=$?

if [ "$RC" -ne 0 ]; then
  warn "⚪️ 信号 28（Shiller CAPE）数据暂缺 —— 已尝试来源：${URL}"
  die "连线失败（curl 退出码 ${RC}）。不得以记忆或推断填补（行为准则第 1 条）。" 3
fi
if [ "$CODE" != "200" ]; then
  warn "⚪️ 信号 28（Shiller CAPE）数据暂缺 —— ${URL} 回 HTTP ${CODE}"
  case "$CODE" in 403|429) warn "   （HTTP ${CODE} = 被反爬挡下，重试同一请求通常无用）" ;; esac
  die "不得以记忆或推断填补。备援源：currentmarketvaluation.com / gurufocus 的 CAPE 页。" 3
fi

# ── 解析 ──
tr '\n' ' ' < "$WORK/page.html" | awk '
{
  s = $0
  # 时间戳要在剥标签之前抓（剥完就没有 id 可以定位了）
  ts = ""
  k = index(s, "<div id=\"timestamp\">")
  if (k > 0) {
    rest = substr(s, k + 20)
    j = index(rest, "</div>")
    if (j > 0) {
      ts = substr(rest, 1, j - 1)
      gsub(/<[^>]*>/, " ", ts)
      gsub(/^[ \t]+|[ \t]+$/, "", ts)
      gsub(/  +/, " ", ts)
      gsub(/["\\]/, "", ts)      # 之后要塞进 JSON，先去掉会破坏字串的字元
    }
  }
  # 剥 <script> / <style>：以结束标签切段，每段再从第一个开始标签处截断
  n = split(s, parts, /<\/script>|<\/style>/)
  out = ""
  for (i = 1; i <= n; i++) {
    p = parts[i]
    if (match(p, /<script|<style/)) p = substr(p, 1, RSTART - 1)
    out = out " " p
  }
  gsub(/<[^>]*>/, "|", out)
  print "TS\t" ts
  print out
}' > "$WORK/stripped.txt"

TS="$(head -n 1 "$WORK/stripped.txt" | cut -f2-)"
tail -n +2 "$WORK/stripped.txt" | tr '|' '\n' | awk '{
  gsub(/^[ \t]+|[ \t]+$/, "")
  if ($0 ~ /^[0-9]{1,3}\.[0-9]{1,2}$/)          print "NUM\t" $0
  else if ($0 ~ /^\([A-Za-z]{3} [0-9]{4}\)$/)   print "DAT\t" $0
}' > "$WORK/tokens.txt"

NUMS="$(awk -F'\t' '$1=="NUM"{ print $2 }' "$WORK/tokens.txt")"
DATS="$(awk -F'\t' '$1=="DAT"{ print $2 }' "$WORK/tokens.txt")"
NCOUNT="$(printf '%s\n' "$NUMS" | grep -c '[0-9]' || true)"

if [ "$NCOUNT" -lt 5 ]; then
  warn "⚪️ 信号 28（Shiller CAPE）数据暂缺 —— 页面取到了（HTTP 200），但解析只抓到 ${NCOUNT} 个数字，不足 5 个。"
  warn "   multpl.com 版型可能已变更。已尝试来源：${URL}"
  die "不得用旧读数或推断值顶替。备援源：currentmarketvaluation.com / gurufocus 的 CAPE 页。" 3
fi

CUR="$(printf '%s\n'    "$NUMS" | sed -n 1p)"
MEAN="$(printf '%s\n'   "$NUMS" | sed -n 2p)"
MEDIAN="$(printf '%s\n' "$NUMS" | sed -n 3p)"
MIN="$(printf '%s\n'    "$NUMS" | sed -n 4p)"
MAX="$(printf '%s\n'    "$NUMS" | sed -n 5p)"
MIN_DATE="$(printf '%s\n' "$DATS" | sed -n 1p)"; MIN_DATE="${MIN_DATE:-未提供}"
MAX_DATE="$(printf '%s\n' "$DATS" | sed -n 2p)"; MAX_DATE="${MAX_DATE:-未提供}"

# 结构自检：解析出的第 4 个必须是五者最小、第 5 个必须是五者最大。
# 版型一变，数字顺序就会错位——错位后的五个数每个看起来都很合理，
# 这个检查是唯一能挡住「合理但全错」的关卡。
if ! awk -v c="$CUR" -v me="$MEAN" -v md="$MEDIAN" -v mn="$MIN" -v mx="$MAX" 'BEGIN{
      ok = (mn <= c && mn <= me && mn <= md && mn <= mx) && (mx >= c && mx >= me && mx >= md && mx >= mn)
      exit ok ? 0 : 1 }'; then
  warn "⚪️ 信号 28（Shiller CAPE）数据暂缺 —— 解析结构自检失败。"
  warn "   抓到的五个数为：current=${CUR} mean=${MEAN} median=${MEDIAN} min=${MIN} max=${MAX}"
  warn "   但第 4 个不是最小值、或第 5 个不是最大值 → multpl.com 版型已变，数字顺序错位。"
  die "错位后的数字看起来都很合理却全错，绝不可输出。请人工核对 ${URL} 后再改解析。" 3
fi

SANITY_OK=1
awk -v v="$CUR" -v lo="$CAPE_MIN" -v hi="$CAPE_MAX" 'BEGIN{ exit (v>=lo && v<=hi)?0:1 }' || SANITY_OK=0

TRIGGERED=0
awk -v v="$CUR" -v t="$CAPE_TRIGGER" 'BEGIN{ exit (v>t)?0:1 }' && TRIGGERED=1

# 文字用带正负号的写法读起来清楚；JSON 不能有前导「+」（不是合法 JSON 数字），
# 所以两种格式各算一份，不要图省事共用一个变数。
VS_MAX="$(awk -v c="$CUR" -v m="$MAX" 'BEGIN{ printf "%+.2f", c - m }')"
VS_MEAN="$(awk -v c="$CUR" -v m="$MEAN" 'BEGIN{ printf "%+.1f", (c/m - 1) * 100 }')"
VS_MAX_J="$(awk -v c="$CUR" -v m="$MAX" 'BEGIN{ printf "%.2f", c - m }')"
VS_MEAN_J="$(awk -v c="$CUR" -v m="$MEAN" 'BEGIN{ printf "%.1f", (c/m - 1) * 100 }')"

if [ "$JSON" -eq 1 ]; then
  printf '{"ok":true,"signal":28,"name":"Shiller CAPE / PE10","source":"%s",' "$URL"
  printf '"asof_page_timestamp":"%s",' "$TS"
  printf '"current":%s,"mean":%s,"median":%s,' "$CUR" "$MEAN" "$MEDIAN"
  printf '"min":{"value":%s,"when":"%s"},"max":{"value":%s,"when":"%s"},' "$MIN" "$MIN_DATE" "$MAX" "$MAX_DATE"
  printf '"vs_max_abs":%s,"vs_mean_pct":%s,' "$VS_MAX_J" "$VS_MEAN_J"
  printf '"threshold":%s,"triggered":%s,' "$CAPE_TRIGGER" "$([ "$TRIGGERED" -eq 1 ] && echo true || echo false)"
  printf '"sanity":{"range":[%s,%s],"pass":%s}}\n' "$CAPE_MIN" "$CAPE_MAX" "$([ "$SANITY_OK" -eq 1 ] && echo true || echo false)"
else
  echo "【信号 28】Shiller CAPE / PE10　来源：${URL}"
  printf '  页面时间戳     %s\n' "${TS:-未提供}"
  echo
  printf '  当前 CAPE      %s\n' "$CUR"
  printf '  历史均值       %s（当前较均值 %s%%）\n' "$MEAN" "$VS_MEAN"
  printf '  历史中位数     %s\n' "$MEDIAN"
  printf '  历史最低       %s %s\n' "$MIN" "$MIN_DATE"
  printf '  历史最高       %s %s（当前距史高 %s）\n' "$MAX" "$MAX_DATE" "$VS_MAX"
  echo
  echo "阈值判定（>${CAPE_TRIGGER} = 历史前 5% 区间；阈值不因市场情绪调整）："
  if [ "$TRIGGERED" -eq 1 ]; then
    printf '  CAPE >%s ... ✅ 触发（%s）\n' "$CAPE_TRIGGER" "$CUR"
  else
    printf '  CAPE >%s ... ❌ 未触发（%s）\n' "$CAPE_TRIGGER" "$CUR"
  fi
  echo
  echo "量级自检（${CAPE_MIN}–${CAPE_MAX}）：$([ "$SANITY_OK" -eq 1 ] && echo "✅ 通过" || echo "🔴 未通过")"
fi

if [ "$SANITY_OK" -ne 1 ]; then
  warn ""
  warn "⚠️ 量级自检未通过：CAPE ${CUR} 落在 ${CAPE_MIN}–${CAPE_MAX} 之外。"
  warn "   行为准则第 6 条：算出来量级不对，先怀疑解析，不要直接报出来。"
  warn "   最可能的原因：multpl.com 版型变更，抓到的不是 Shiller PE 那一栏。"
  warn "   请先人工核对 ${URL} 再引用这个数字。"
  exit 4
fi
exit 0
