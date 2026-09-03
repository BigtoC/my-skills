#!/usr/bin/env bash
# stock_perp.sh —— 美股 24/7 永续（信号 18）
#
# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ 🚨 必须带 "dex":"xyz" —— 这是本脚本最重要的一行                          ║
# ║                                                                          ║
# ║ Hyperliquid **主池**的 `SPX` 是 SPX6900 **迷因币**（约 $0.34），         ║
# ║ 不是标普 500。不指定 dex 就会取到迷因币，数字完全错误却看起来像有数。     ║
# ║ 本脚本只接受 xyz 池，且只认 `xyz:` 前缀的市场名。                        ║
# ╚══════════════════════════════════════════════════════════════════════════╝
#
# ⚠️ 两个必须保留的检查（references/signals-c-crypto.md 信号 18，逐条实现）：
#   1. **流动性门槛**：名义 OI = markPx × openInterest，**< $5M 就标「不适用」**。
#      历史基线 xyz:SP500 ≈ $483M OI／$129M 日成交；xyz:XYZ100 ≈ $282M／$177M。
#      骤降到千万以下 = 池子在迁移，报价不可信。
#   2. **代码撞名**：标记价与真实指数收盘差 **>10% 就判定取错市场**，
#      该市场整笔作废、不得用于报告。
#
# 口径（reference 明订，不可改）：
#   xyz:SP500  ↔ ^GSPC   指数点位 1:1（≈7,7xx，不是 SPY 的 ≈77x）
#   xyz:XYZ100 ↔ ^NDX    指数点位 1:1（≈29,xxx，不是 QQQ 的 ≈7xx）
#   资金费率：Hyperliquid 是**每小时**结算 → 8h 费率 = hourly × 8；
#            年化% = 8h费率 × 3 × 365（等价于 hourly × 24 × 365）。
#            基线 hourly 0.00000625 = 8h 0.005% = 年化 +5.48%
#            → 读到这个数就是**「无方向信号，纯粹是利率」**。
#
# 上一美股收盘从哪来：
#   · Yahoo chart 端点本机实测稳定回 **HTTP 429**（带 UA、带 cookie jar 都一样），
#     stooq CSV 端点已下线 —— 两者都不可靠，本脚本不用。
#   · `--from-fred` 走 FRED 的 SP500 / NASDAQ100 日收盘序列（免 API key，稳定），
#     但它**滞后 1 个交易日**，所以每次都会把观测日期与滞后天数印出来。
#     拿滞后的收盘当「上一收盘」算出来的隐含跳空是错的，必须让人看得见滞后。
#   · 最准的还是呼叫方直接传 --spx / --ndx。绝不自己编一个收盘价。
#
# 依赖：bash、curl、jq、awk。
# 退出码：0 正常｜1 参数错误｜2 依赖缺失｜3 取数失败（数据暂缺）｜4 判定取错市场

set -euo pipefail

PROG="$(basename "$0")"
API="https://api.hyperliquid.xyz/info"
DEX="xyz"                       # 绝不可省，见档头
TIMEOUT=25

OI_MIN_USD=5000000              # 名义 OI 门槛：< $5M 标「不适用」
WRONG_MARKET_PCT=10.0           # 与现货收盘差 >10% → 判定取错市场
GAP_TIER2_PCT=2.0               # |隐含跳空| ≥2% → 计 1 个 Tier 2 触发
GAP_MENTION_PCT=1.0             # ≥1% 即使不触发也必须在解读里点名
RELSTR_PCT=1.5                  # 科技 vs 大盘差距 ≥1.5pt 要点名
FUND_HOT_ANNUAL=15.0            # 年化 >+15% = 真实多头拥挤
FUND_COLD_ANNUAL=-10.0          # 年化 <−10% = 强烈对冲需求

usage() {
  cat <<EOF
${PROG} —— 美股 24/7 永续 xyz:SP500 / xyz:XYZ100（信号 18）

用法:
  ${PROG} [--spx <上一美股收盘>] [--ndx <上一美股收盘>] [选项]
  ${PROG} --closes <file.json> [选项]

选项:
  --spx N          ^GSPC 上一美股收盘价（指数点位，例如 7757.64）
  --ndx N          ^NDX  上一美股收盘价（指数点位，例如 29800.12）
  --from-fred      自动从 FRED 抓 SP500 / NASDAQ100 的最近收盘（免 API key）。
                   ⚠️ FRED 的日收盘序列**会滞后 1 个交易日**，本脚本一定会把
                      观测日期与滞后天数印出来；滞后 >1 天时另外告警。
                      --spx / --ndx 明确给的值永远优先于 FRED。
  --closes FILE    从 JSON 档读收盘价；下列任一形状都认得：
                     {"indices":{"^GSPC":{"close":7757.64},"^NDX":{"close":29800.1}}}
                     {"^GSPC":7757.64,"^NDX":29800.1}
                   （即姊妹技能 ai-pullback-daily 的 technicals.py --json 输出形状）
  --json           以 JSON 输出
  -h, --help       显示本说明

不给收盘价也能跑：会输出 perp 标记价、prevDayPx、名义 OI、资金费率与流动性判定，
但**隐含跳空一律标 ⚪️ 无法判定**，绝不用 prevDayPx 冒充美股收盘
（prevDayPx 是 perp 自己 24 小时前的价，不是现货收盘）。

例子:
  ${PROG}
  ${PROG} --from-fred                      # 隐含跳空对照 FRED 的最近收盘（会标滞后）
  ${PROG} --spx 7757.64 --ndx 29800.12
  ${PROG} --closes tech.json --json

判定规则:
  隐含跳空 = perp markPx ÷ 上一美股收盘 − 1
    |偏离| ≥ ${GAP_TIER2_PCT}%  → 计 1 个 Tier 2 触发
    |偏离| ≥ ${GAP_MENTION_PCT}%  → 即使不触发也必须在解读里点名
  科技 vs 大盘相对强弱 = XYZ100 偏离% − SP500 偏离%，差距 ≥${RELSTR_PCT}pt 时点名
  资金费率（弱信号）：年化 >+${FUND_HOT_ANNUAL}% 或 <${FUND_COLD_ANNUAL}%
EOF
}

die()  { printf '错误：%s\n' "$1" >&2; exit "${2:-1}"; }
warn() { printf '%s\n' "$1" >&2; }

command -v curl >/dev/null 2>&1 || die "找不到 curl。" 2
command -v awk  >/dev/null 2>&1 || die "找不到 awk。" 2
if ! command -v jq >/dev/null 2>&1; then
  warn "错误：找不到 jq。Hyperliquid 回的是巢状 JSON 阵列，没有 jq 无法解析。"
  warn '     安装：macOS 用 brew install jq｜Debian/Ubuntu 用 apt-get install jq。'
  warn "     降级办法：手打下列请求，肉眼找 xyz:SP500 / xyz:XYZ100 的 markPx / openInterest —"
  warn "       curl -s -X POST ${API} -H 'Content-Type: application/json' \\"
  warn "            -d '{\"type\":\"metaAndAssetCtxs\",\"dex\":\"${DEX}\"}'"
  warn "     ⚠️ 手打时 \"dex\":\"${DEX}\" 一样不能省，主池 SPX 是迷因币。"
  exit 2
fi

SPX_CLOSE=""
NDX_CLOSE=""
SPX_CLOSE_DATE=""
NDX_CLOSE_DATE=""
SPX_SRC="未提供"
NDX_SRC="未提供"
CLOSES_FILE=""
FROM_FRED=0
JSON=0

is_num() { echo "$1" | grep -qE '^-?[0-9]+(\.[0-9]+)?$'; }

while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    --json) JSON=1; shift ;;
    --from-fred) FROM_FRED=1; shift ;;
    --spx) [ $# -ge 2 ] || die "--spx 需要一个数字。" 1
           is_num "$2" || die "--spx 必须是数字，收到「$2」。" 1
           SPX_CLOSE="$2"; SPX_SRC="--spx 参数"; shift 2 ;;
    --ndx) [ $# -ge 2 ] || die "--ndx 需要一个数字。" 1
           is_num "$2" || die "--ndx 必须是数字，收到「$2」。" 1
           NDX_CLOSE="$2"; NDX_SRC="--ndx 参数"; shift 2 ;;
    --closes) [ $# -ge 2 ] || die "--closes 需要一个 JSON 档路径。" 1
              [ -f "$2" ] || die "--closes 指定的档案不存在。" 1
              CLOSES_FILE="$2"; shift 2 ;;
    *) die "未知参数「$1」。用 ${PROG} --help 看用法。" 1 ;;
  esac
done

WORK="$(mktemp -d 2>/dev/null)" || die "无法建立临时目录。" 2
trap 'rm -rf "$WORK"' EXIT INT TERM

# ── --closes：容错读取（容器键只展开一层，与姊妹技能 perp_quotes.py 同语义）──
if [ -n "$CLOSES_FILE" ]; then
  jq -e . "$CLOSES_FILE" >/dev/null 2>&1 || die "--closes 指定的档案不是合法 JSON。" 1
  read_close() {   # $1 = 逗号分隔的等价代码
    jq -r --arg keys "$1" '
      ($keys | split(",")) as $K
      | [ ., (to_entries[] | select(.value|type=="object") | .value) ]      # 顶层 + 展开一层
      | map(to_entries[] | select(.key as $k | $K | index($k))
            | (if (.value|type)=="object" then (.value.close // .value.price // empty) else .value end))
      | flatten | map(select(type=="number")) | first // empty' "$CLOSES_FILE"
  }
  if [ -z "$SPX_CLOSE" ]; then
    SPX_CLOSE="$(read_close '^GSPC,GSPC,SPX,SP500,标普500')"
    [ -z "$SPX_CLOSE" ] || SPX_SRC="--closes 档案"
  fi
  if [ -z "$NDX_CLOSE" ]; then
    NDX_CLOSE="$(read_close '^NDX,NDX,NASDAQ100,NDX100,纳斯达克100')"
    [ -z "$NDX_CLOSE" ] || NDX_SRC="--closes 档案"
  fi
fi

# ── --from-fred：从 FRED 日收盘序列补上一收盘 ──
# 这段刻意与 fred.sh 重复一小块取数逻辑，好让 stock_perp.sh 可以单独发布、单独执行，
# 不依赖同目录还有没有 fred.sh。两处的规则必须一致：
#   · **必须用 curl**（python requests 打 FRED 会超时）
#   · **不要加自订 User-Agent**（带 UA 会挂住到超时）
#   · FRED 用 `.` 表示缺值 → 跳过，取最近一个有值的点，并回报该点日期
fred_last() {   # $1=SERIES_ID → 「值<TAB>日期」；取不到回空字串
  local id="$1" code rc=0
  code="$(curl -sS --max-time "$TIMEOUT" --retry 1 -o "$WORK/fred_${id}.csv" -w '%{http_code}' \
            "https://fred.stlouisfed.org/graph/fredgraph.csv?id=${id}" 2>/dev/null)" || rc=$?
  [ "$rc" -eq 0 ] && [ "$code" = "200" ] || return 0
  awk -F, 'NR>1 { sub(/\r$/,""); if ($2 != "" && $2 != ".") { v=$2; d=$1 } }
           END { if (v != "") printf "%s\t%s", v, d }' "$WORK/fred_${id}.csv"
}
if [ "$FROM_FRED" -eq 1 ]; then
  if [ -z "$SPX_CLOSE" ]; then
    L="$(fred_last SP500)"
    if [ -n "$L" ]; then SPX_CLOSE="${L%%	*}"; SPX_CLOSE_DATE="${L##*	}"; SPX_SRC="FRED SP500"; fi
  fi
  if [ -z "$NDX_CLOSE" ]; then
    L="$(fred_last NASDAQ100)"
    if [ -n "$L" ]; then NDX_CLOSE="${L%%	*}"; NDX_CLOSE_DATE="${L##*	}"; NDX_SRC="FRED NASDAQ100"; fi
  fi
  if [ -z "$SPX_CLOSE" ] && [ -z "$NDX_CLOSE" ]; then
    warn "⚠️ --from-fred 两个序列都没取到（FRED SP500 / NASDAQ100）。隐含跳空将标 ⚪️ 无法判定。"
  fi
fi

# 收盘价滞后天数（BSD / GNU date 都支援）
_BSD_DATE=0; date -v-1d +%Y-%m-%d >/dev/null 2>&1 && _BSD_DATE=1
lag_days() {   # $1=YYYY-MM-DD → 距今天数；无日期回 -1
  [ -n "$1" ] || { echo -1; return 0; }
  local e
  if [ "$_BSD_DATE" -eq 1 ]; then e="$(date -j -f "%Y-%m-%d" "$1" +%s 2>/dev/null || true)"
  else e="$(date -d "$1" +%s 2>/dev/null || true)"; fi
  [ -n "$e" ] || { echo -1; return 0; }
  echo $(( ( $(date +%s) - e ) / 86400 ))
}

# ── 取数：POST，必须带 "dex":"xyz" ──
RC=0
CODE="$(curl -sS --max-time "$TIMEOUT" --retry 1 --retry-delay 2 \
          -o "$WORK/hl.json" -w '%{http_code}' \
          -X POST -H 'Content-Type: application/json' \
          -d "{\"type\":\"metaAndAssetCtxs\",\"dex\":\"${DEX}\"}" \
          "$API" 2>"$WORK/curl.err")" || RC=$?

if [ "$RC" -ne 0 ]; then
  warn "⚪️ 信号 18 数据暂缺 —— 已尝试来源：${API}（dex=${DEX}）"
  die "Hyperliquid 连线失败（curl 退出码 ${RC}）。不得以记忆或推断填补。" 3
fi
if [ "$CODE" != "200" ] || ! jq -e 'type=="array"' "$WORK/hl.json" >/dev/null 2>&1; then
  warn "⚪️ 信号 18 数据暂缺 —— 已尝试来源：${API}（dex=${DEX}），回 HTTP ${CODE}"
  die "Hyperliquid 回传不是预期的阵列结构。不得以记忆或推断填补。" 3
fi

# 抽出两个市场 → TSV：name markPx prevDayPx funding oi dayNtlVlm
jq -r '[.[0].universe, .[1]] | transpose
       | map(select(.[0].name == "xyz:SP500" or .[0].name == "xyz:XYZ100"))
       | .[] | [ .[0].name, .[1].markPx, .[1].prevDayPx, .[1].funding,
                 .[1].openInterest, .[1].dayNtlVlm ] | @tsv' "$WORK/hl.json" > "$WORK/mk.tsv"

if [ ! -s "$WORK/mk.tsv" ]; then
  warn "⚪️ 信号 18 数据暂缺 —— xyz 池里找不到 xyz:SP500 / xyz:XYZ100。"
  warn "   可能是 Hyperliquid 改了市场名，或 dex 参数失效。"
  warn "   ⚠️ 不要因此退回主池：主池的 SPX 是 SPX6900 迷因币，取到的数完全无关。"
  die "不得以记忆或推断填补。" 3
fi

# ── 逐市场计算 ──
# 输出栏位：name index close mark prev funding_h f8 fann oi notional vlm liquid gap wrong
: > "$WORK/rows.tsv"
WRONG_ANY=0
while IFS="$(printf '\t')" read -r NAME MARK PREV FUND OI VLM; do
  case "$NAME" in
    "xyz:SP500")  IDX="^GSPC"; CLOSE="$SPX_CLOSE"; CDATE="$SPX_CLOSE_DATE"; CSRC="$SPX_SRC" ;;
    "xyz:XYZ100") IDX="^NDX";  CLOSE="$NDX_CLOSE"; CDATE="$NDX_CLOSE_DATE"; CSRC="$NDX_SRC" ;;
    *) continue ;;
  esac
  # 注意：awk 里不能用 close 当变量名（内建函式名），故写成 cls。
  awk -v name="$NAME" -v idx="$IDX" -v cls="${CLOSE:-}" -v mark="$MARK" -v prev="$PREV" \
      -v fund="$FUND" -v oi="$OI" -v vlm="$VLM" \
      -v oimin="$OI_MIN_USD" -v wrongp="$WRONG_MARKET_PCT" \
      -v cdate="${CDATE:-}" -v csrc="${CSRC:-未提供}" 'BEGIN{
    notional = mark * oi
    f8   = fund * 8 * 100            # 每小时 → 8h 口径百分比
    fann = f8 * 3 * 365              # 年化%
    liquid = (notional >= oimin) ? "YES" : "NO"
    if (cls == "") { gap = "NA"; wrong = "NA" }
    else {
      g = (mark / cls - 1) * 100
      gap = sprintf("%.4f", g)
      wrong = ((g > wrongp) || (g < -wrongp)) ? "YES" : "NO"
    }
    printf "%s\t%s\t%s\t%s\t%s\t%s\t%.4f\t%.2f\t%s\t%.0f\t%.0f\t%s\t%s\t%s\t%s\t%s\n",
           name, idx, (cls=="" ? "NA" : cls), mark, prev, fund, f8, fann, oi, notional, vlm, liquid, gap, wrong,
           (cdate=="" ? "NA" : cdate), csrc
  }' >> "$WORK/rows.tsv"
done < "$WORK/mk.tsv"

# 第 14 栏 = 撞名判定；任何一个 YES 都要让整支脚本以 4 退出
if awk -F'\t' '$14=="YES"{ found=1 } END{ exit found?0:1 }' "$WORK/rows.tsv"; then WRONG_ANY=1; fi

get() { awk -F'\t' -v n="$1" -v c="$2" '$1==n { print $c }' "$WORK/rows.tsv"; }

SPX_GAP="$(get "xyz:SP500" 13)";  [ -n "$SPX_GAP" ] || SPX_GAP="NA"
NDX_GAP="$(get "xyz:XYZ100" 13)"; [ -n "$NDX_GAP" ] || NDX_GAP="NA"
# 任一市场被判定取错，相对强弱就不可信 → 一律 NA
WRONG_SEEN="$(awk -F'\t' '$14=="YES"{c++} END{print c+0}' "$WORK/rows.tsv")"
RELSTR="NA"
if [ "$SPX_GAP" != "NA" ] && [ "$NDX_GAP" != "NA" ] && [ "$WRONG_SEEN" -eq 0 ]; then
  RELSTR="$(awk -v a="$NDX_GAP" -v b="$SPX_GAP" 'BEGIN{ printf "%.4f", a - b }')"
fi

if [ "$JSON" -eq 1 ]; then
  jq -n --rawfile tsv "$WORK/rows.tsv" \
        --arg api "$API" --arg dex "$DEX" --arg relstr "$RELSTR" \
        --argjson oimin "$OI_MIN_USD" --argjson gap2 "$GAP_TIER2_PCT" \
        --argjson gap1 "$GAP_MENTION_PCT" --argjson rel "$RELSTR_PCT" \
        --argjson fhot "$FUND_HOT_ANNUAL" --argjson fcold "$FUND_COLD_ANNUAL" \
        --argjson wrongp "$WRONG_MARKET_PCT" '
    ($tsv | rtrimstr("\n") | split("\n") | map(select(length>0) | split("\t") |
      {market:.[0], index:.[1],
       cash_close:(if .[2]=="NA" then null else (.[2]|tonumber) end),
       mark:(.[3]|tonumber), prev_day_px:(.[4]|tonumber),
       funding_hourly:(.[5]|tonumber), funding_8h_pct:(.[6]|tonumber),
       funding_annual_pct:(.[7]|tonumber), open_interest:(.[8]|tonumber),
       notional_oi_usd:(.[9]|tonumber), day_ntl_vlm_usd:(.[10]|tonumber),
       liquid:(.[11]=="YES"),
       implied_gap_pct:(if .[12]=="NA" then null else (.[12]|tonumber) end),
       wrong_market_suspected:(if .[13]=="NA" then null else (.[13]=="YES") end),
       cash_close_date:(if .[14]=="NA" then null else .[14] end),
       cash_close_source:.[15]})) as $rows
    | {ok:true, signal:18, name:"美股 24/7 永续", source:($api + " dex=" + $dex),
       thresholds:{notional_oi_min_usd:$oimin, gap_tier2_pct:$gap2, gap_mention_pct:$gap1,
                   relative_strength_pt:$rel, funding_annual_hot_pct:$fhot,
                   funding_annual_cold_pct:$fcold, wrong_market_pct:$wrongp},
       markets:$rows,
       relative_strength_pt:(if $relstr=="NA" then null else ($relstr|tonumber) end),
       triggers:{
         gap_tier2:[ $rows[] | select(.wrong_market_suspected != true and .implied_gap_pct != null and (.implied_gap_pct|fabs) >= $gap2) | .market ],
         gap_mention:[ $rows[] | select(.wrong_market_suspected != true and .implied_gap_pct != null and (.implied_gap_pct|fabs) >= $gap1) | .market ],
         funding_crowded_long:[ $rows[] | select(.funding_annual_pct > $fhot) | .market ],
         funding_hedging_demand:[ $rows[] | select(.funding_annual_pct < $fcold) | .market ],
         illiquid:[ $rows[] | select(.liquid == false) | .market ],
         wrong_market:[ $rows[] | select(.wrong_market_suspected == true) | .market ]
       }}'
  [ "$WRONG_ANY" -eq 0 ] || exit 4
  exit 0
fi

echo "【信号 18】美股 24/7 永续　来源：${API}（dex=${DEX}）"
echo "口径：指数点位 1:1｜资金费率为每小时结算，已换算成 8h 与年化"
echo
printf '  %-12s %-7s %12s %12s %12s %10s %12s %14s\n' "市场" "对照" "标记价" "上一收盘" "隐含跳空%" "年化费率%" "名义OI(\$M)" "日成交(\$M)"
awk -F'\t' '{
  gap = ($13=="NA") ? "⚪️暂缺" : sprintf("%+.2f", $13)
  cl  = ($3=="NA")  ? "—"      : $3
  printf "  %-12s %-7s %12s %12s %12s %10.2f %12.1f %14.1f\n", $1, $2, $4, cl, gap, $8, $10/1e6, $11/1e6
}' "$WORK/rows.tsv"
echo
echo "上一美股收盘来源："
awk -F'\t' '{
  if ($3 == "NA") printf "  %-12s %s：未提供 → 隐含跳空无法判定\n", $1, $2
  else            printf "  %-12s %s = %s（来源：%s，观测日 %s）\n", $1, $2, $3, $16, $15
}' "$WORK/rows.tsv"

while IFS="$(printf '\t')" read -r _n _i _c _m _p _f _f8 _fa _oi _no _v _l _g _w CDT _s; do
  [ "$CDT" != "NA" ] || continue
  D="$(lag_days "$CDT")"
  if [ "$D" -gt 1 ]; then
    echo "  ⚠️ ${_n} 的收盘价观测日 ${CDT} 已滞后 ${D} 天——隐含跳空是拿 perp 现价对**旧收盘**算的，"
    echo "     不是「对上一收盘」。报告里必须写明这个滞后，或改传当日实际收盘。"
  fi
done < "$WORK/rows.tsv"
echo

echo "两个必须保留的检查："
awk -F'\t' -v oimin="$OI_MIN_USD" -v wrongp="$WRONG_MARKET_PCT" '{
  if ($12 == "YES")
    printf "  ① 流动性  %-12s 名义 OI $%.1fM ≥ $%.0fM 门槛 → 可用\n", $1, $10/1e6, oimin/1e6
  else
    printf "  ① 流动性  %-12s 名义 OI $%.1fM < $%.0fM 门槛 → **标「不适用」**，池子可能在迁移，报价不可信\n", $1, $10/1e6, oimin/1e6
}' "$WORK/rows.tsv"
awk -F'\t' -v wrongp="$WRONG_MARKET_PCT" '{
  if ($14 == "NA")
    printf "  ② 撞名    %-12s ⚪️ 未提供 %s 收盘价，无法做 >%.0f%% 撞名检查（已强制 dex=xyz，未落主池）\n", $1, $2, wrongp
  else if ($14 == "YES")
    printf "  ② 撞名    %-12s 🔴 与 %s 收盘差 %+.2f%%（>%.0f%%）→ **判定取错市场，本市场整笔作废**\n", $1, $2, $13, wrongp
  else
    printf "  ② 撞名    %-12s 与 %s 收盘差 %+.2f%%，在 ±%.0f%% 内 → 市场正确\n", $1, $2, $13, wrongp
}' "$WORK/rows.tsv"
echo

echo "阈值判定："
awk -F'\t' -v g2="$GAP_TIER2_PCT" -v g1="$GAP_MENTION_PCT" '{
  if ($14 == "YES") {
    # 取错市场 → 整笔作废，**不得计任何触发**（否则一个错市场会凭空造出 Tier 2 触发）
    printf "  隐含跳空  %-12s 🔴 作废（撞名检查判定取错市场）→ 不计触发\n", $1
  } else if ($13 == "NA") {
    printf "  隐含跳空  %-12s ⚪️ 无法判定 —— 未提供上一美股收盘价\n", $1
  } else {
    a = ($13 < 0) ? -$13 : $13
    if (a >= g2)      printf "  隐含跳空  %-12s ✅ 触发（%+.2f%%，|偏离| ≥ %.0f%%）→ 计 1 个 Tier 2 触发\n", $1, $13, g2
    else if (a >= g1) printf "  隐含跳空  %-12s ❌ 未触发（%+.2f%%），但 ≥%.0f%% → **解读里必须点名**\n", $1, $13, g1
    else              printf "  隐含跳空  %-12s ❌ 未触发（%+.2f%%）\n", $1, $13
  }
}' "$WORK/rows.tsv"

if [ "$RELSTR" = "NA" ]; then
  if [ "$WRONG_SEEN" -gt 0 ]; then
    echo "  相对强弱  🔴 无法判定（有市场被撞名检查判定取错，两边偏离不可比）"
  else
    echo "  相对强弱  ⚪️ 无法判定（两个指数收盘价须都提供）"
  fi
else
  awk -v r="$RELSTR" -v t="$RELSTR_PCT" 'BEGIN{
    a = (r<0) ? -r : r
    if (a >= t) printf "  相对强弱  XYZ100 − SP500 = %+.2f pt（≥%.1f pt）→ **必须点名**：%s\n", r, t, (r>0 ? "科技/AI 领涨" : "科技在拖累大盘")
    else        printf "  相对强弱  XYZ100 − SP500 = %+.2f pt（<%.1f pt，无须点名）\n", r, t
  }'
fi

awk -F'\t' -v hot="$FUND_HOT_ANNUAL" -v cold="$FUND_COLD_ANNUAL" '{
  if ($8 > hot)       printf "  资金费率  %-12s ✅ 触发：年化 %+.2f%% > +%.0f%% → 真实多头拥挤\n", $1, $8, hot
  else if ($8 < cold) printf "  资金费率  %-12s ✅ 触发：年化 %+.2f%% < %.0f%% → 强烈对冲需求\n", $1, $8, cold
  else                printf "  资金费率  %-12s ❌ 未触发：年化 %+.2f%%（基线 hourly 0.00000625 = 年化 +5.48%%，纯粹是利率，无方向信号）\n", $1, $8
}' "$WORK/rows.tsv"

if [ -z "$SPX_CLOSE" ] || [ -z "$NDX_CLOSE" ]; then
  echo
  echo "提示：隐含跳空要有「上一美股收盘」才算得出来。请传 --spx / --ndx，或用 --closes 读"
  echo "      姊妹技能 ai-pullback-daily 的 technicals.py --json 输出。"
  echo "      **不要拿 prevDayPx 当收盘价**——那是 perp 自己 24 小时前的报价，不是现货收盘。"
fi

if [ "$WRONG_ANY" -eq 1 ]; then
  warn ""
  warn "⚠️ 有市场触发「取错市场」判定（与现货收盘差 >${WRONG_MARKET_PCT}%）。"
  warn "   该市场的数字整笔作废，不得写进报告。先确认传入的收盘价口径是不是指数点位"
  warn "   （^GSPC ≈7,7xx 而非 SPY ≈77x；^NDX ≈29,xxx 而非 QQQ ≈7xx）。"
  exit 4
fi
exit 0
