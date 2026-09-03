#!/usr/bin/env bash
# crypto.sh —— 加密永续与市场（信号 14–17）
#
# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ ⚠️ 口径统一（references/signals-c-crypto.md「最容易搞错的一步」，硬约束）║
# ║                                                                          ║
# ║ 阈值 0.05% / 0.1% 都是 **8 小时口径**。各家原始费率的结算周期不同，       ║
# ║ **必须先换算到 8h 再比阈值**：                                           ║
# ║   来源                    原始周期    换算成 8h                          ║
# ║   Binance（默认）         8 小时      直接用                             ║
# ║   Binance（部分币种）     4 小时      × 2                                ║
# ║   Hyperliquid             1 小时      × 8                                ║
# ║                                                                          ║
# ║ 所以每次都必须先打 /fapi/v1/fundingInfo 查 fundingIntervalHours——        ║
# ║ Binance 会不定期调整个别币种。**未出现在结果中的币种按默认 8 小时处理。**║
# ║ 反例：SOL 若已改 4 小时结算，看到的 0.05% 其实等于 8h 的 0.1%，          ║
# ║ 从「偏热」直接跳到「极端触发」。不换算就会漏掉真正危险的信号。            ║
# ║                                                                          ║
# ║ 年化换算固定为：年化% = 8h费率 × 3 × 365。报告同时给 8h 费率与年化。      ║
# ╚══════════════════════════════════════════════════════════════════════════╝
#
# ┌─ 其它踩坑 ───────────────────────────────────────────────────────────────┐
# │ · 兜底顺序（references/signals-c-crypto.md）：                            │
# │     Binance → Hyperliquid → web_search coinglass → 标「数据暂缺」        │
# │   用了备援源**必须在输出里注明来源**：不同交易所费率可以差一倍，          │
# │   来源不同**不能直接跨日比较**。本脚本每一行都带 source 栏。             │
# │ · 触发条件要求「持续 ≥24 小时」→ 必须查历史结算，不能只看当下一个数。    │
# │ · 清算数据（信号 15）没有免费公开 API：Coinglass v4 需 API key。         │
# │   本脚本会**实际探测并回报每个来源的 HTTP 码**，然后标「数据暂缺」，     │
# │   绝不用推断值填补（行为准则第 1 条）。                                  │
# │ · BTC Dominance 的 CoinGecko 与 CoinPaprika **口径不同**（分母不同，     │
# │   实测同日 59.1% vs 56.9%）→ 换源当日必须注明，不可跨日直接比较。        │
# │ · 信号 16 的「7d 跌幅 >3%」这条腿曾经**结构性永久不可判定**（固定印     │
# │   「需 CoinGecko Pro」），害得加密信号触发计数系统性偏低。              │
# │   关键认识：缺的是「全市场市值**历史序列**」，不是「**当日** dominance」│
# │   ——当日值每天都免费取得到。换第三方源只会引入第三套分母口径，所以      │
# │   正解是**自己按天累积同源历史**（assets/dominance_history.jsonl），     │
# │   7 天后这条腿就能自己回答，且分母口径天然一致。见 dom_history_append。 │
# └──────────────────────────────────────────────────────────────────────────┘
#
# 依赖：bash、curl、jq、awk。
# 退出码：0 正常｜1 参数错误｜2 依赖缺失｜3 数据暂缺（全部来源失败或本无免费源）
#
# ⚠️ 本脚本是五支 shell 脚本里**唯一**会读写技能目录内档案的一支
#    （assets/dominance_history.jsonl）。因此它由 $0 反推技能根目录，
#    **与 cwd 无关**；历史档写不进去只告警、取数照常输出并 exit 0。

set -euo pipefail

PROG="$(basename "$0")"
TIMEOUT=25

# ── 技能目录锚定（只有信号 16 的本地历史档需要）──────────────────────────
# 由 $0 反推，不看 cwd：SKILL.md 一律用绝对路径调用，但排查时常从别的 cwd 跑。
# 印在输出里的一律是 $DOM_HISTORY_REL（相对路径）——绝对路径含家目录形状，
# 而本技能的输出会进报告并推 Slack（行为准则第 4 条）。
SCRIPT_DIR="$(cd "$(dirname "$0")" 2>/dev/null && pwd -P)" || SCRIPT_DIR=""
SKILL_ROOT="$(dirname "${SCRIPT_DIR:-.}")"
DOM_HISTORY_REL="assets/dominance_history.jsonl"
DOM_HISTORY="${SKILL_ROOT}/${DOM_HISTORY_REL}"
DOM_HISTORY_MAX=90      # 保留上限，超出丢最旧的；约 3 个月，档案不会无限增长

BINANCE="https://fapi.binance.com/fapi/v1"
HYPERLIQUID="https://api.hyperliquid.xyz/info"
COINGECKO="https://api.coingecko.com/api/v3"
COINPAPRIKA="https://api.coinpaprika.com/v1"
LLAMA_STABLE="https://stablecoins.llama.fi"

# 阈值（8h 口径，百分比）—— 行为准则第 3 条：阈值永远不因市场情绪动态调整
FUND_HOT_8H=0.05        # 三者同时 >0.05%/8h 且持续 ≥24h = 多头杠杆过热
FUND_EXTREME_8H=0.10    # 任一 >0.1%/8h = 急迫反转风险
DOM_DROP_24H=2.0        # BTC Dominance 24h 跌幅 >2%
DOM_DROP_7D=3.0         # BTC Dominance 7d 跌幅 >3%
# 7d 基准的可用年龄窗口（天）。下限 6 是为了不拿三四天前的读数冒充 7 日变动；
# **上限 10 同样是硬约束**：漏跑几天后最接近的一笔可能是 31 天前，
# 拿它算出来的数字是「31 日变动」，贴上「7d 变动」的标签就是编数字。
# 窗口外一律 ⚪️ 断层，绝不将就。
DOM_7D_MIN_AGE=6
DOM_7D_MAX_AGE=10
STABLE_DAY_OUT_USD=1000000000   # 单日净流出 >$1B 必须标注

SYMBOLS="BTC ETH SOL"

usage() {
  cat <<EOF
${PROG} —— 加密永续与市场（信号 14–17）

用法:
  ${PROG} <子命令> [选项]

子命令:
  funding       信号 14：BTC/ETH/SOL 永续资金费率（自动做 8h 口径换算 + 24h 持续性检查）
  liquidations  信号 15：过去 24h 清算（无免费公开源，会实测各来源并回报 HTTP 码）
  dominance     信号 16：BTC Dominance
  stablecoins   信号 17：USDT + USDC 总供应与净流入/流出
  all           依序跑上面四项

选项:
  --json        以 JSON 输出
  --symbols A,B 只查指定币种（默认 BTC,ETH,SOL；仅对 funding 有效）
  --history     只印本地累积的 dominance 历史（仅对 dominance 有效，不连网）
  -h, --help    显示本说明

例子:
  ${PROG} funding
  ${PROG} funding --symbols BTC,ETH
  ${PROG} dominance
  ${PROG} dominance --history          # 排查 7d 腿：看已累积几天、来源是否一致
  ${PROG} stablecoins --json
  ${PROG} all --json

信号 16 的 7d 腿:
  「7d 跌幅 >3%」不再是永久不可判定。dominance 每次成功取数会往
  ${DOM_HISTORY_REL} 追加一笔（同日重跑覆盖，最多留 ${DOM_HISTORY_MAX} 笔），
  累积满 7 天后本脚本自己算 7d 变动。**7 日前那笔的 source 必须与今日相同**，
  否则一律拒绝比较（分母口径不同）。历史不足 7 天一律 ⚪️，绝不当成未触发。

退出码:
  all 只要有任一区块取到数就回 0，并在结尾列出「本次数据暂缺项」；
  四个区块全部失败才回 3。单一子命令取不到数一律回 3。
EOF
}

die()  { printf '错误：%s\n' "$1" >&2; exit "${2:-1}"; }
warn() { printf '%s\n' "$1" >&2; }

command -v curl >/dev/null 2>&1 || die "找不到 curl。" 2
command -v awk  >/dev/null 2>&1 || die "找不到 awk。" 2
if ! command -v jq >/dev/null 2>&1; then
  warn "错误：找不到 jq。本脚本的四个数据源全部回 JSON，没有 jq 无法解析。"
  warn '     安装：macOS 用 brew install jq｜Debian/Ubuntu 用 apt-get install jq。'
  warn "     降级办法（不建议长期用）：直接手打下列请求，肉眼读数字——"
  warn "       curl -s '${BINANCE}/premiumIndex?symbol=BTCUSDT'"
  warn "       curl -s '${BINANCE}/fundingInfo'          # 先确认结算周期，再换算 8h 口径"
  warn "       curl -s '${COINGECKO}/global'"
  warn "       curl -s '${LLAMA_STABLE}/stablecoins'"
  exit 2
fi

# ── 参数 ──
JSON=0
CMD=""
HISTORY_ONLY=0
while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    --json) JSON=1; shift ;;
    --history) HISTORY_ONLY=1; shift ;;
    --symbols)
      [ $# -ge 2 ] || die "--symbols 需要一个以逗号分隔的清单，例如 BTC,ETH。" 1
      echo "$2" | grep -qE '^[A-Za-z0-9]+(,[A-Za-z0-9]+)*$' || die "--symbols 格式错误，收到「$2」。" 1
      SYMBOLS="$(echo "$2" | tr ',' ' ' | tr '[:lower:]' '[:upper:]')"; shift 2 ;;
    funding|liquidations|dominance|stablecoins|all)
      [ -z "$CMD" ] || die "只能给一个子命令（已收到「${CMD}」又收到「$1」）。" 1
      CMD="$1"; shift ;;
    -*) die "未知选项「$1」。用 ${PROG} --help 看用法。" 1 ;;
    *)  die "未知子命令「$1」。可用：funding / liquidations / dominance / stablecoins / all。" 1 ;;
  esac
done
# --history 单独给也算数：它只对 dominance 有意义，直接补上子命令，
# 不要为了「子命令必给」的规矩逼使用者多打一个字。
[ "$HISTORY_ONLY" -eq 0 ] || [ -n "$CMD" ] || CMD="dominance"
[ -n "$CMD" ] || { usage; exit 1; }
[ "$HISTORY_ONLY" -eq 0 ] || [ "$CMD" = "dominance" ] \
  || die "--history 只对 dominance 子命令有效（收到「${CMD}」）。" 1

WORK="$(mktemp -d 2>/dev/null)" || die "无法建立临时目录。" 2
trap 'rm -rf "$WORK"' EXIT INT TERM

NOW_MS="$(( $(date +%s) * 1000 ))"

# ── HTTP 工具：回传 http_code 到 stdout；连线层失败回 000 ──
http_get() {   # $1=url  $2=输出档
  local rc=0 code
  code="$(curl -sS --max-time "$TIMEOUT" -o "$2" -w '%{http_code}' "$1" 2>>"$WORK/curl.err")" || rc=$?
  [ "$rc" -eq 0 ] || code="000"
  printf '%s' "$code"
}
http_post_json() {  # $1=url  $2=body  $3=输出档
  local rc=0 code
  code="$(curl -sS --max-time "$TIMEOUT" -o "$3" -w '%{http_code}' \
            -X POST -H 'Content-Type: application/json' -d "$2" "$1" 2>>"$WORK/curl.err")" || rc=$?
  [ "$rc" -eq 0 ] || code="000"
  printf '%s' "$code"
}

num() { awk -v v="$1" -v f="${2:-2}" 'BEGIN{ printf "%." f "f", v }'; }
# 带正负号的格式化。**变动量一律走这个**：印在「跌幅」这类带方向的标签底下时，
# 无号的 "1.50" 会被读成「跌了 1.5%」，实际却是涨了 1.5%——方向读反比读不到更危险。
snum() { awk -v v="$1" -v f="${2:-2}" 'BEGIN{ printf "%+." f "f", v }'; }
ok_json() { jq -e . "$1" >/dev/null 2>&1; }

# ════════════════════════════ 信号 14 · 资金费率 ════════════════════════════
# 产出 $WORK/funding.tsv，栏位：
#   symbol source raw_rate interval_h rate8h_pct annual_pct mark next_ms persist n24h
FUNDING_SOURCE=""
FUNDING_NOTE=""

funding_binance() {
  local code sym raw mark next iv ok=1
  : > "$WORK/funding.tsv"

  # 步骤 1：先查结算周期。**这一步不能省** —— 不查就无法做 8h 口径换算。
  code="$(http_get "${BINANCE}/fundingInfo" "$WORK/fundinfo.json")"
  if [ "$code" != "200" ] || ! ok_json "$WORK/fundinfo.json"; then
    FUNDING_NOTE="Binance /fundingInfo 回 HTTP ${code}"
    return 1
  fi

  for sym in $SYMBOLS; do
    # 结算周期：查不到该币种 → 按 Binance 默认 8 小时（reference 明订）
    iv="$(jq -r --arg s "${sym}USDT" '[.[] | select(.symbol==$s) | .fundingIntervalHours] | first // 8' "$WORK/fundinfo.json")"
    [ -n "$iv" ] && [ "$iv" != "null" ] || iv=8

    code="$(http_get "${BINANCE}/premiumIndex?symbol=${sym}USDT" "$WORK/pi_${sym}.json")"
    if [ "$code" != "200" ] || ! ok_json "$WORK/pi_${sym}.json"; then
      FUNDING_NOTE="Binance /premiumIndex(${sym}USDT) 回 HTTP ${code}"
      ok=0; break
    fi
    raw="$(jq -r '.lastFundingRate // empty' "$WORK/pi_${sym}.json")"
    mark="$(jq -r '.markPrice // empty' "$WORK/pi_${sym}.json")"
    next="$(jq -r '.nextFundingTime // 0' "$WORK/pi_${sym}.json")"
    if [ -z "$raw" ] || [ -z "$mark" ]; then
      FUNDING_NOTE="Binance /premiumIndex(${sym}USDT) 缺 lastFundingRate 或 markPrice 栏位"
      ok=0; break
    fi

    # 步骤 2：24h 持续性。limit=12 足以覆盖 8h 与 4h 两种周期的 24 小时。
    code="$(http_get "${BINANCE}/fundingRate?symbol=${sym}USDT&limit=12" "$WORK/fr_${sym}.json")"
    local persist n24
    if [ "$code" = "200" ] && ok_json "$WORK/fr_${sym}.json"; then
      # 每一笔历史结算都要各自换算成 8h 口径再比阈值
      # 输出 @tsv 并把 IFS 限定成 tab：默认 IFS 含空格，字段一旦带空格就会串位。
      # 这里两个字段目前都不含空格，但用 tab 分隔可以让它对将来的改动免疫，
      # 也与本文件其余 read 的写法一致。
      IFS="$(printf '\t')" read -r n24 persist <<EOF
$(jq -r --argjson now "$NOW_MS" --argjson iv "$iv" --argjson hot "$FUND_HOT_8H" '
    [ .[] | select(.fundingTime >= ($now - 86400000)) | ((.fundingRate|tonumber) * 8 / $iv * 100) ] as $r
    | if ($r|length) == 0 then ["0", "UNKNOWN"]
      else [ ($r|length|tostring),
             (if ([ $r[] | select(. > $hot) ] | length) == ($r|length) then "YES" else "NO" end) ]
      end | @tsv' "$WORK/fr_${sym}.json")
EOF
    else
      n24=0; persist="UNKNOWN"
    fi

    # 注意：awk 里不能用 next 当变量名（保留字），故写成 nxt。
    awk -v s="$sym" -v src="Binance" -v raw="$raw" -v iv="$iv" -v mark="$mark" \
        -v nxt="$next" -v p="$persist" -v n="$n24" 'BEGIN{
      r8 = raw * 8 / iv * 100          # → 8h 口径百分比
      ann = r8 * 3 * 365               # 年化%
      printf "%s\t%s\t%s\t%s\t%.4f\t%.2f\t%s\t%s\t%s\t%s\n", s, src, raw, iv, r8, ann, mark, nxt, p, n
    }' >> "$WORK/funding.tsv"
  done

  [ "$ok" -eq 1 ] || return 1
  [ -s "$WORK/funding.tsv" ] || return 1
  return 0
}

funding_hyperliquid() {
  # 备援源。Hyperliquid 的 funding 是**每小时**费率 → × 8 才是 8h 口径。
  # 主池（不带 dex）拿 BTC / ETH / SOL；本函数不查历史，故 24h 持续性标 UNKNOWN。
  local code
  code="$(http_post_json "$HYPERLIQUID" '{"type":"metaAndAssetCtxs"}' "$WORK/hl.json")"
  if [ "$code" != "200" ] || ! ok_json "$WORK/hl.json"; then
    FUNDING_NOTE="${FUNDING_NOTE:+${FUNDING_NOTE}；}Hyperliquid 回 HTTP ${code}"
    return 1
  fi
  : > "$WORK/funding.tsv"
  local sym raw mark
  for sym in $SYMBOLS; do
    raw="$(jq -r --arg s "$sym" '[.[0].universe, .[1]] | transpose | map(select(.[0].name==$s)) | .[0][1].funding // empty' "$WORK/hl.json")"
    mark="$(jq -r --arg s "$sym" '[.[0].universe, .[1]] | transpose | map(select(.[0].name==$s)) | .[0][1].markPx // empty' "$WORK/hl.json")"
    [ -n "$raw" ] || continue
    awk -v s="$sym" -v src="Hyperliquid" -v raw="$raw" -v mark="$mark" 'BEGIN{
      iv = 1                            # Hyperliquid 每小时结算
      r8 = raw * 8 / iv * 100
      ann = r8 * 3 * 365
      printf "%s\t%s\t%s\t%s\t%.4f\t%.2f\t%s\t%s\t%s\t%s\n", s, src, raw, iv, r8, ann, mark, 0, "UNKNOWN", 0
    }' >> "$WORK/funding.tsv"
  done
  [ -s "$WORK/funding.tsv" ] || return 1
  return 0
}

run_funding() {
  if funding_binance; then
    FUNDING_SOURCE="Binance"
  else
    warn "⚠️ Binance 取数失败（${FUNDING_NOTE:-原因未回报}），依兜底顺序改用 Hyperliquid。"
    if funding_hyperliquid; then
      FUNDING_SOURCE="Hyperliquid"
      FUNDING_NOTE="${FUNDING_NOTE:+${FUNDING_NOTE}；}已改用备援源 Hyperliquid，**不同交易所费率可差一倍，不可与前日 Binance 读数直接比较**"
    else
      FUNDING_SOURCE=""
      return 1
    fi
  fi
  return 0
}

render_funding_text() {
  # 标题用实际查询的币种，不要写死 BTC/ETH/SOL —— --symbols 可以只查子集，
  # 标题与表格内容不符会让人误以为漏了币种。
  echo "【信号 14】$(echo "$SYMBOLS" | tr ' ' '/') 永续资金费率　来源：${FUNDING_SOURCE}"
  echo "口径：原始费率已按结算周期换算成 **8h 口径**；年化 = 8h × 3 × 365。"
  echo
  printf '  %-5s %-12s %10s %6s %12s %10s %14s %s\n' "币种" "来源" "原始费率" "周期h" "8h费率%" "年化%" "标记价" "24h持续>阈值"
  awk -F'\t' -v hot="$FUND_HOT_8H" '{
    p = ($9=="YES") ? "是(" $10 "笔全部)" : (($9=="NO") ? "否(" $10 "笔)" : "无法判定(备援源无历史)")
    printf "  %-5s %-12s %10s %6s %12.4f %10.2f %14s %s\n", $1, $2, $3, $4, $5, $6, $7, p
  }' "$WORK/funding.tsv"
  echo
  echo "阈值判定（8h 口径，严格比对，不加软化语言）："
  local n_all n_hot n_persist n_extreme extreme_list
  n_all="$(wc -l < "$WORK/funding.tsv" | tr -d ' ')"
  n_hot="$(awk -F'\t' -v h="$FUND_HOT_8H" '$5 > h' "$WORK/funding.tsv" | wc -l | tr -d ' ')"
  n_persist="$(awk -F'\t' -v h="$FUND_HOT_8H" '$5 > h && $9=="YES"' "$WORK/funding.tsv" | wc -l | tr -d ' ')"
  n_extreme="$(awk -F'\t' -v e="$FUND_EXTREME_8H" '$5 > e' "$WORK/funding.tsv" | wc -l | tr -d ' ')"
  extreme_list="$(awk -F'\t' -v e="$FUND_EXTREME_8H" '$5 > e { printf "%s(%.4f%%) ", $1, $5 }' "$WORK/funding.tsv")"

  if [ "$n_persist" = "$n_all" ] && [ "$n_all" -gt 0 ]; then
    printf '  多头杠杆过热（三者同时 >%s%%/8h 持续 ≥24h） ... ✅ 触发\n' "$FUND_HOT_8H"
  elif [ "$n_hot" = "$n_all" ] && [ "$n_all" -gt 0 ]; then
    printf '  多头杠杆过热（三者同时 >%s%%/8h 持续 ≥24h） ... ❌ 未触发（当下三者皆 >阈值，但未满足持续 ≥24h）\n' "$FUND_HOT_8H"
  else
    printf '  多头杠杆过热（三者同时 >%s%%/8h 持续 ≥24h） ... ❌ 未触发（%s/%s 个币种当下高于阈值）\n' "$FUND_HOT_8H" "$n_hot" "$n_all"
  fi
  if [ "$n_extreme" -gt 0 ]; then
    printf '  急迫反转风险（任一 >%s%%/8h） .............. ✅ 触发：%s\n' "$FUND_EXTREME_8H" "$extreme_list"
  else
    printf '  急迫反转风险（任一 >%s%%/8h） .............. ❌ 未触发\n' "$FUND_EXTREME_8H"
  fi
  [ -z "$FUNDING_NOTE" ] || echo "  注：${FUNDING_NOTE}"
}

funding_json() {
  # TSV → JSON 一律交给 jq（--rawfile）处理，不要用 awk 手拼 JSON：
  # awk 拼字串一旦有个引号没跳脱，产出的就是「看起来像 JSON 的坏字串」，
  # 下游 jq 会整个炸掉，而错误讯息完全指不出是哪一栏出的问题。
  jq -n --arg source "$FUNDING_SOURCE" --arg note "$FUNDING_NOTE" \
        --argjson hot "$FUND_HOT_8H" --argjson extreme "$FUND_EXTREME_8H" \
        --rawfile tsv "$WORK/funding.tsv" \
    '($tsv | rtrimstr("\n") | split("\n") | map(select(length>0) | split("\t") |
        {symbol:.[0], source:.[1], raw_rate:(.[2]|tonumber), interval_hours:(.[3]|tonumber),
         rate_8h_pct:(.[4]|tonumber), annualized_pct:(.[5]|tonumber), mark:(.[6]|tonumber),
         next_funding_ms:(.[7]|tonumber), persist_24h:.[8], settlements_24h:(.[9]|tonumber)})) as $rows
     | {signal:14, name:"永续资金费率", status:"ok", source:$source, note:$note,
      caliber:"所有费率已换算成 8h 口径；年化 = 8h × 3 × 365",
      thresholds:{hot_8h_pct:$hot, extreme_8h_pct:$extreme},
      rows:$rows,
      triggers:{
        leverage_overheated:(([ $rows[] | select(.rate_8h_pct > $hot and .persist_24h == "YES") ] | length) == ($rows|length) and ($rows|length) > 0),
        imminent_reversal:(([ $rows[] | select(.rate_8h_pct > $extreme) ] | length) > 0)
      }}'
}

# ════════════════════════════ 信号 15 · 清算数据 ════════════════════════════
# 没有免费公开源。这里**实际探测**并回报每个来源的 HTTP 码与判读，
# 让「数据暂缺」带得出证据，而不是一句笼统的「取数失败」。
run_liquidations() {
  local c1 c2 v1 v2
  c1="$(http_get "https://open-api-v4.coinglass.com/api/futures/liquidation/history?symbol=BTCUSDT&interval=1d" "$WORK/cg1.json")"
  v1="$(jq -r '(.msg // .message // "无 msg 栏位")' "$WORK/cg1.json" 2>/dev/null || echo "回应非 JSON")"
  c2="$(http_get "https://fapi.coinglass.com/api/futures/liquidation/info?symbol=all&timeType=4" "$WORK/cg2.json")"
  v2="$(jq -r 'if has("data") then "有 data 栏位" else "回 success 但无 data 栏位" end' "$WORK/cg2.json" 2>/dev/null || echo "回应非 JSON")"
  printf '%s\t%s\n%s\t%s\n' \
    "open-api-v4.coinglass.com/api/futures/liquidation/history｜HTTP ${c1}" "$v1" \
    "fapi.coinglass.com/api/futures/liquidation/info｜HTTP ${c2}" "$v2" > "$WORK/liq_attempts.tsv"
  return 1   # 目前没有任何免费源能回出数字 → 一律 missing
}

render_liquidations_text() {
  echo "【信号 15】过去 24h 清算　⚪️ 数据暂缺"
  echo "  已尝试来源（实测结果）："
  awk -F'\t' '{ printf "    · %s → %s\n", $1, $2 }' "$WORK/liq_attempts.tsv"
  echo "  结论：Coinglass v4 需 API key，公开端点不回明细；本项**无免费公开 API**。"
  echo "  下一步（由上层 agent 执行，不由本脚本假造）：web_search \"coinglass liquidations 24h\"。"
  echo "  报告要求：24h 总清算 >\$500M = 杠杆洗盘｜>\$1B = 重大事件；"
  echo "            **必须注明多头与空头哪一方被清算更多**，并写出上次已知读数与滞后周数。"
}

liquidations_json() {
  jq -n --rawfile tsv "$WORK/liq_attempts.tsv" \
    '($tsv | rtrimstr("\n") | split("\n") | map(select(length>0) | split("\t") | {source:.[0], result:.[1]})) as $attempts
     | {signal:15, name:"24h 清算", status:"missing",
      reason:"无免费公开 API（Coinglass v4 需 API key）",
      attempted:$attempts,
      next_step:"web_search \"coinglass liquidations 24h\"",
      report_requirements:["总额 >$500M = 杠杆洗盘；>$1B = 重大事件",
                           "必须注明多头 vs 空头哪一方被清算更多",
                           "必须写出上次已知读数与滞后周数"]}'
}

# ═══════════════════════════ 信号 16 · BTC Dominance ═══════════════════════
#
# ── 本地 dominance 历史：7d 腿为什么自己累积，而不是换源 ─────────────────
# 免费层拿不到的是「全市场市值**历史序列**」（/global/market_cap_chart 实测
# HTTP 401，需 Pro）；「**当日** dominance」每天都免费取得到。两者是两回事。
# 若改用第三方历史源，就会引入**第三套分母口径**——档头已写明 CoinGecko 与
# CoinPaprika 实测同日 59.1% vs 56.9%，差约 2pt，而 7d 阈值只有 3%：跨源相减
# 出来的「跌幅」多半是分母差，不是真实资金流动。所以正解是按天累积同源历史，
# 7 天后这条腿自己回答，分母口径天然一致（行为准则第 2 条）。
#
# 硬规矩：7 日前那笔的 source 必须与今日相同，否则**拒绝比较**并印 ⚪️；
#         历史不足 7 天也印 ⚪️，**绝不因为其余条件都正常就推断这条腿安全**。
DOM_STATUS="pending"
DOM_SEVEN_JSON='{"status":"insufficient","history_days":0,"history_since":null}'

dom_history_read() {
  # 印出档里所有**合法**纪录（一行一笔 JSON）到 stdout。
  # 逐行校验而不是让 jq 一口气吃整个档：一行坏掉就整档解析失败的话，
  # 会把已经累积好的历史整批当成不存在——那比少一行严重得多。
  [ -f "$DOM_HISTORY" ] || return 0
  local line
  while IFS= read -r line || [ -n "$line" ]; do
    [ -n "$line" ] || continue
    printf '%s\n' "$line" | jq -ce '
      select(type=="object"
             and (.date? | type) == "string"
             and (.dominance_pct? | type) == "number"
             and (.source? | type) == "string")' 2>/dev/null || continue
  done < "$DOM_HISTORY"
  return 0
}

dom_history_append() {   # $1=date $2=dominance_pct $3=source_key $4=fetched_at
  # 一天一笔：同日重跑**覆盖**当日那笔（不追加第二笔），再按日期排序、
  # 只留最新 ${DOM_HISTORY_MAX} 笔。写入走「同目录 mktemp → mv」原子替换，
  # 中途失败不会留下半截档把既有历史毁掉。
  # 写失败一律**只告警不中断**：取数照常输出、照常 exit 0（行为准则第 5 条）。
  local dir tmp new fail
  fail="⚠️ 本地 dominance 历史写入失败（${DOM_HISTORY_REL}）：%s。本次取数照常输出，但 7d 腿的基准点未累积。"
  dir="$(dirname "$DOM_HISTORY")"
  if ! mkdir -p "$dir" 2>/dev/null; then
    warn "$(printf "$fail" "无法建立所在目录")"; return 1
  fi
  new="$(jq -nc --arg d "$1" --argjson p "$2" --arg s "$3" --arg t "$4" \
           '{date:$d, dominance_pct:$p, source:$s, fetched_at:$t}' 2>/dev/null)" || new=""
  if [ -z "$new" ]; then
    warn "$(printf "$fail" "无法组出纪录（dominance=${2}）")"; return 1
  fi
  tmp="$(mktemp "${dir}/.dominance_history.XXXXXX" 2>/dev/null)" || tmp=""
  if [ -z "$tmp" ]; then
    warn "$(printf "$fail" "所在目录不可写")"; return 1
  fi
  if { dom_history_read | jq -c --arg d "$1" 'select(.date != $d)'; printf '%s\n' "$new"; } \
       | jq -sc --argjson cap "$DOM_HISTORY_MAX" \
           'sort_by(.date) | (if length > $cap then .[length-$cap:] else . end) | .[]' \
       > "$tmp" 2>/dev/null \
     && mv -f "$tmp" "$DOM_HISTORY" 2>/dev/null; then
    # mktemp 给的是 0600；这个档会被 commit 进仓库，跟其它状态档保持一致。
    chmod 644 "$DOM_HISTORY" 2>/dev/null || true
    return 0
  fi
  rm -f "$tmp" 2>/dev/null || true
  warn "$(printf "$fail" "档案不可写")"
  return 1
}

dom_seven_day() {   # $1=today $2=dominance_now $3=source_key → JSON 到 stdout
  # 基准取「年龄落在 [6,10] 天、且最接近 7 天」的那一笔。
  # 下限 6：不拿三四天前的读数冒充 7 日变动。
  # 上限 10：漏跑几天后最接近的一笔可能是 31 天前 —— 那是「31 日变动」，
  #          贴上「7d 变动」的标签就是编数字。窗口外一律 ⚪️ 断层。
  # 排序键 |age-7| 优先、其次取较新的一笔，纯粹为了结果可重现。
  dom_history_read | jq -sc \
      --arg today "$1" --argjson now "$2" --arg src "$3" --argjson t7 "$DOM_DROP_7D" \
      --argjson amin "$DOM_7D_MIN_AGE" --argjson amax "$DOM_7D_MAX_AGE" '
    def epochday: (strptime("%Y-%m-%d") | mktime) / 86400 | floor;
    . as $all
    | ($all | map(.date) | unique) as $days
    | ($today | epochday) as $t0
    | [ $all[] | select(.date != $today) | . + {age: ($t0 - (.date | epochday))} ] as $prev
    | ( $prev | map(select(.age >= $amin and .age <= $amax)) ) as $inwin
    # 同源优先：先在窗口内找与今日同源的，找不到才退回任意源（那时必然报 source_mismatch）。
    # 若只按年龄挑，窗口内「7天前·异源」会盖掉「8天前·同源」，
    # 明明有可用的同源基准却被判成不可比 —— 白白丢掉一次本可成立的判定。
    | ( ( $inwin | map(select(.source == $src)) | sort_by(((.age - 7) | length), .age) | first )
        // ( $inwin | sort_by(((.age - 7) | length), .age) | first ) ) as $base
    | ( $prev | map(select(.age >= $amin)) | sort_by(.age) | first ) as $nearest
    | { history_days: ($days | length),
        history_since: ($days | first),
        history_until: ($days | last),
        today_source: $src,
        base_age_window_days: [$amin, $amax] }
      + ( if $base != null and $base.source != $src then
            {status:"source_mismatch",
             reason:"历史记录来自不同数据源，分母口径不同不可比较",
             base_date:$base.date, base_age_days:$base.age,
             base_source:$base.source, base_dominance_pct:$base.dominance_pct}
          elif $base != null then
            (($now - $base.dominance_pct)) as $dpt
            | (($dpt / $base.dominance_pct) * 100) as $drel
            | {status:"ok",
               base_date:$base.date, base_age_days:$base.age,
               base_source:$base.source, base_dominance_pct:$base.dominance_pct,
               delta_pt:$dpt, delta_rel_pct:$drel,
               triggered:($drel < (0 - $t7))}
          elif $nearest != null then
            {status:"gap",
             reason:"本地历史有断层：最接近的一笔落在 \($amin)–\($amax) 天的可用窗口之外",
             nearest_date:$nearest.date, nearest_age_days:$nearest.age,
             nearest_source:$nearest.source}
          else
            {status:"insufficient",
             reason:"本地同源历史不足 7 天，7d 变动无法判定（不得视为未触发）"}
          end )' 2>/dev/null
}

dom_record_and_seven() {
  # 取数成功后：先落一笔历史，再据历史算 7d。日期一律 UTC，与纪录里的一致。
  local today ts dom src
  today="$(date -u +%Y-%m-%d)"
  ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  dom="$(jq -r '.dominance_pct // empty' "$WORK/dom.json")"
  src="$(jq -r '.source_key // empty' "$WORK/dom.json")"
  if [ -z "$dom" ] || [ -z "$src" ]; then
    warn "⚠️ 本次 dominance 读数缺 dominance_pct / source_key，跳过历史累积。"
    return 0
  fi
  dom_history_append "$today" "$dom" "$src" "$ts" || true
  local sev
  sev="$(dom_seven_day "$today" "$dom" "$src")" || sev=""
  [ -n "$sev" ] || sev='{"status":"insufficient","history_days":0,"history_since":null,"reason":"历史档读取失败"}'
  DOM_SEVEN_JSON="$sev"
  return 0
}

render_dom_history() {
  if [ "$JSON" -eq 1 ]; then
    dom_history_read | jq -s --arg f "$DOM_HISTORY_REL" --argjson cap "$DOM_HISTORY_MAX" \
      '{signal:16, name:"BTC Dominance 本地历史", file:$f, retention_max:$cap,
        count:length, records:sort_by(.date)}'
    return 0
  fi
  echo "【信号 16】BTC Dominance 本地历史　档案：${DOM_HISTORY_REL}（保留上限 ${DOM_HISTORY_MAX} 笔，日期为 UTC）"
  if [ ! -f "$DOM_HISTORY" ]; then
    echo "  ⚪️ 尚无本地历史——「dominance」第一次成功取数后才会建立（这不是安装缺档）。"
    echo "  在那之前 7d 腿一律 ⚪️「历史不足」，**不得当成未触发**。"
    return 0
  fi
  # 表头用字面量对齐：printf 的 %-12s 数的是**位元组**，中文字一个 3 位元组、
  # 显示宽度却是 2 —— 拿 %-12s 排中文表头，栏位一定歪。
  echo "  日期(UTC)     dominance%  来源           取数时间(UTC)"
  dom_history_read | jq -sr 'sort_by(.date)[] | [.date, (.dominance_pct|tostring), .source, (.fetched_at // "—")] | @tsv' \
    | awk -F'\t' '{ printf "  %-12s %11.2f  %-14s %s\n", $1, $2, $3, $4 }'
  dom_history_read | jq -sr \
      --arg today "$(date -u +%Y-%m-%d)" \
      --argjson amin "$DOM_7D_MIN_AGE" --argjson amax "$DOM_7D_MAX_AGE" '
      def epochday: (strptime("%Y-%m-%d") | mktime) / 86400 | floor;
      ($today | epochday) as $t0
      | (map(.date) | unique) as $d
      | [ .[] | select(.date != $today) | ($t0 - (.date|epochday)) | select(. >= $amin and . <= $amax) ] as $win
      | "  合计 \($d|length) 天（\($d|first // "—") … \($d|last // "—")）；来源分布："
        + ((group_by(.source) | map("\(.[0].source)×\(length)") | join("、")) // "—"),
      (if ($win|length) > 0 then
         "  7d 腿：以今日（\($today)）为准已具备判定条件（仍须该笔与今日同源，否则拒绝比较）。"
       elif ($d|length) >= 7 then
         "  7d 腿：⚪️ 以今日（\($today)）为准，\($amin)–\($amax) 天窗口内没有基准（历史断层）；不得当成未触发。"
       else
         "  7d 腿：⚪️ 历史不足（已累积 \($d|length) 天，7d 判定需 ≥7 天）；不得当成未触发。" end)' \
    2>/dev/null || true
}

run_dominance() {
  local code
  code="$(http_get "${COINGECKO}/global" "$WORK/cg_global.json")"
  if [ "$code" = "200" ] && ok_json "$WORK/cg_global.json" \
     && [ "$(jq -r '.data.market_cap_percentage.btc // "null"' "$WORK/cg_global.json")" != "null" ]; then
    local code2
    code2="$(http_get "${COINGECKO}/coins/markets?vs_currency=usd&ids=bitcoin" "$WORK/cg_btc.json")"
    if [ "$code2" = "200" ] && ok_json "$WORK/cg_btc.json"; then
      # 24h 前的 dominance 由「市值」变动率回推（两个变动率都是市值口径，不是价格口径）：
      #   dom_24h_ago = [btc_mc/(1+b)] / [total_mc/(1+t)] × 100
      # 这是从真实数据推导，不是估计；供应量 24h 变动 <0.01%，可忽略。
      jq -n \
        --argjson g "$(cat "$WORK/cg_global.json")" \
        --argjson b "$(cat "$WORK/cg_btc.json")" '
        ($g.data.market_cap_percentage.btc)                as $dom_now
        | ($g.data.total_market_cap.usd)                   as $tot
        | ($g.data.market_cap_change_percentage_24h_usd)   as $tchg
        | ($b[0].market_cap)                               as $bmc
        | ($b[0].market_cap_change_percentage_24h)         as $bchg
        | (($bmc / (1 + $bchg/100)) / ($tot / (1 + $tchg/100)) * 100) as $dom_prev
        | {source:"CoinGecko /global + /coins/markets",
           source_key:"coingecko",
           dominance_pct:$dom_now, dominance_24h_ago_pct:$dom_prev,
           delta_pt:($dom_now - $dom_prev),
           delta_rel_pct:(($dom_now - $dom_prev)/$dom_prev*100),
           total_mcap_usd:$tot, btc_mcap_usd:$bmc,
           total_mcap_change_24h_pct:$tchg, btc_mcap_change_24h_pct:$bchg}' > "$WORK/dom.json"
      DOM_STATUS="ok"; return 0
    fi
    jq -n --argjson g "$(cat "$WORK/cg_global.json")" '
      {source:"CoinGecko /global", source_key:"coingecko",
       dominance_pct:$g.data.market_cap_percentage.btc,
       dominance_24h_ago_pct:null, delta_pt:null, delta_rel_pct:null,
       total_mcap_usd:$g.data.total_market_cap.usd, btc_mcap_usd:null,
       total_mcap_change_24h_pct:$g.data.market_cap_change_percentage_24h_usd,
       btc_mcap_change_24h_pct:null}' > "$WORK/dom.json"
    DOM_STATUS="partial"; return 0
  fi

  warn "⚠️ CoinGecko /global 回 HTTP ${code}，改用备援源 CoinPaprika。"
  code="$(http_get "${COINPAPRIKA}/global" "$WORK/cp_global.json")"
  if [ "$code" = "200" ] && ok_json "$WORK/cp_global.json" \
     && [ "$(jq -r '.bitcoin_dominance_percentage // "null"' "$WORK/cp_global.json")" != "null" ]; then
    jq -n --argjson p "$(cat "$WORK/cp_global.json")" '
      {source:"CoinPaprika /global（⚠️ 与 CoinGecko 口径不同，分母不同，实测同日可差 2pt 以上——不可与前日 CoinGecko 读数比较）",
       source_key:"coinpaprika",
       dominance_pct:($p.bitcoin_dominance_percentage),
       dominance_24h_ago_pct:null, delta_pt:null, delta_rel_pct:null,
       total_mcap_usd:($p.market_cap_usd), btc_mcap_usd:null,
       total_mcap_change_24h_pct:($p.market_cap_change_24h), btc_mcap_change_24h_pct:null}' > "$WORK/dom.json"
    DOM_STATUS="partial"; return 0
  fi
  DOM_STATUS="missing"; return 1
}

render_dominance_text() {
  local src dom prev dpt drel
  src="$(jq -r '.source' "$WORK/dom.json")"
  dom="$(jq -r '.dominance_pct' "$WORK/dom.json")"
  prev="$(jq -r '.dominance_24h_ago_pct // "null"' "$WORK/dom.json")"
  dpt="$(jq -r '.delta_pt // "null"' "$WORK/dom.json")"
  drel="$(jq -r '.delta_rel_pct // "null"' "$WORK/dom.json")"

  echo "【信号 16】BTC Dominance　来源：${src}"
  printf '  当前 dominance     %s%%\n' "$(num "$dom" 2)"
  if [ "$prev" = "null" ]; then
    echo "  24h 前             ⚪️ 数据暂缺（备援源未提供可回推的市值变动率）"
    echo "  24h 变动           ⚪️ 无法判定 → 本项不得填数字"
  else
    printf '  24h 前（回推）     %s%%\n' "$(num "$prev" 2)"
    printf '  24h 变动           %s pt（相对 %s%%）　正=上升／负=下降\n' "$(snum "$dpt" 2)" "$(snum "$drel" 2)"
  fi
  # 7d 变动：由 assets/dominance_history.jsonl 里**同源**的历史自答。
  local st7 hd hs bdate bage bsrc bdom d7pt d7rel
  st7="$(printf '%s' "$DOM_SEVEN_JSON"  | jq -r '.status')"
  hd="$(printf '%s' "$DOM_SEVEN_JSON"   | jq -r '.history_days')"
  hs="$(printf '%s' "$DOM_SEVEN_JSON"   | jq -r '.history_since // "—"')"
  case "$st7" in
    ok)
      bdate="$(printf '%s' "$DOM_SEVEN_JSON" | jq -r '.base_date')"
      bage="$(printf  '%s' "$DOM_SEVEN_JSON" | jq -r '.base_age_days')"
      bsrc="$(printf  '%s' "$DOM_SEVEN_JSON" | jq -r '.base_source')"
      bdom="$(printf  '%s' "$DOM_SEVEN_JSON" | jq -r '.base_dominance_pct')"
      d7pt="$(printf  '%s' "$DOM_SEVEN_JSON" | jq -r '.delta_pt')"
      d7rel="$(printf '%s' "$DOM_SEVEN_JSON" | jq -r '.delta_rel_pct')"
      printf '  7d 前（%s，%s 天前）  %s%%　来源 %s（与今日同源）\n' \
             "$bdate" "$bage" "$(num "$bdom" 2)" "$bsrc"
      printf '  7d 变动            %s pt（相对 %s%%）　正=上升／负=下降\n' \
             "$(snum "$d7pt" 2)" "$(snum "$d7rel" 2)"
      printf '     基准：本地历史 %s（自 %s 起累积，已 %s 天）\n' "$DOM_HISTORY_REL" "$hs" "$hd"
      ;;
    source_mismatch)
      bdate="$(printf '%s' "$DOM_SEVEN_JSON" | jq -r '.base_date')"
      bsrc="$(printf  '%s' "$DOM_SEVEN_JSON" | jq -r '.base_source')"
      echo "  7d 变动            ⚪️ 无法判定 → 本项不得填数字"
      printf '     原因：历史记录来自不同数据源（今日 %s vs 7日前 %s @ %s），分母口径不同不可比较。\n' \
             "$(jq -r '.source_key' "$WORK/dom.json")" "$bsrc" "$bdate"
      echo "           口径红线：不同源的 dominance 分母不同（CoinGecko 与 CoinPaprika 实测同日可差 2pt 以上，"
      printf '           而 7d 阈值只有 %s%%），跨源相减出来的「跌幅」多半是分母差，不是真实资金流动。\n' "$DOM_DROP_7D"
      printf '           等 %s 累积满 7 天同源读数后，本项自动恢复。\n' "$DOM_HISTORY_REL"
      ;;
    gap)
      bdate="$(printf '%s' "$DOM_SEVEN_JSON" | jq -r '.nearest_date')"
      bage="$(printf  '%s' "$DOM_SEVEN_JSON" | jq -r '.nearest_age_days')"
      echo "  7d 变动            ⚪️ 无法判定 → 本项不得填数字"
      printf '     原因：本地历史有断层——最接近的一笔是 %s（%s 天前），落在 %s–%s 天的可用窗口之外。\n' \
             "$bdate" "$bage" "$DOM_7D_MIN_AGE" "$DOM_7D_MAX_AGE"
      printf '           拿 %s 天前的读数算出来的是「%s 日变动」，贴上「7d 变动」的标签就是编数字。\n' "$bage" "$bage"
      printf '           连跑几天补上 %s 就会自动恢复。\n' "$DOM_HISTORY_REL"
      ;;
    *)
      printf '  7d 变动            ⚪️ 历史不足（已累积 %s 天，7d 判定需 ≥7 天）\n' "$hd"
      printf '     本地历史自 %s 起累积（%s）；每天跑一次 dominance 就会自己补齐。\n' "$hs" "$DOM_HISTORY_REL"
      echo "     注：缺的只是「历史序列」，当日 dominance 本身取得到——所以这里不换源"
      echo "         （换源会引入第三套分母口径），而是等同源历史累积够。"
      ;;
  esac
  echo
  echo "阈值判定（信号 16：24h 跌幅 >2% 或 7d 跌幅 >3% = 山寨狂热期）："
  if [ "$drel" = "null" ]; then
    printf '  24h 跌幅 >%s%% ... ⚪️ 无法判定\n' "$DOM_DROP_24H"
  else
    # 这里印的是**带号的变动量**（正=上升、负=下降），不是「跌幅」的绝对值。
    # 在「跌幅」标签底下印无号数字，涨 1.5% 会被读成跌 1.5%，方向刚好相反。
    if awk -v d="$drel" -v t="$DOM_DROP_24H" 'BEGIN{ exit (d < -t) ? 0 : 1 }'; then
      printf '  24h 跌幅 >%s%% ... ✅ 触发（24h 变动 相对 %s%%）\n' "$DOM_DROP_24H" "$(snum "$drel" 2)"
    else
      printf '  24h 跌幅 >%s%% ... ❌ 未触发（24h 变动 相对 %s%%，正=上升／负=下降）\n' "$DOM_DROP_24H" "$(snum "$drel" 2)"
    fi
    echo "     ⚠️ 口径提示：上面按**相对百分比**判定。若报告采用**百分点**口径，"
    printf '        请改用 Δpt = %s pt 自行判定——两种口径结论可能不同，别混用。\n' "$(snum "$dpt" 2)"
  fi
  case "$st7" in
    ok)
      # 与 24h 同一套规则：阈值按**相对百分比**判定，Δpt 另印一份供百分点口径使用。
      if awk -v d="$d7rel" -v t="$DOM_DROP_7D" 'BEGIN{ exit (d < -t) ? 0 : 1 }'; then
        printf '  7d 跌幅 >%s%% .... ✅ 触发（7d 变动 相对 %s%%，基准日 %s）\n' \
               "$DOM_DROP_7D" "$(snum "$d7rel" 2)" "$bdate"
      else
        printf '  7d 跌幅 >%s%% .... ❌ 未触发（7d 变动 相对 %s%%，正=上升／负=下降）\n' \
               "$DOM_DROP_7D" "$(snum "$d7rel" 2)"
      fi
      echo "     ⚠️ 口径提示：上面按**相对百分比**判定。若报告采用**百分点**口径，"
      printf '        请改用 Δpt = %s pt 自行判定——两种口径结论可能不同，别混用。\n' "$(snum "$d7pt" 2)"
      ;;
    source_mismatch)
      printf '  7d 跌幅 >%s%% .... ⚪️ 无法判定（7 日前那笔与今日不同源，分母口径不可比较）\n' "$DOM_DROP_7D"
      ;;
    gap)
      printf '  7d 跌幅 >%s%% .... ⚪️ 无法判定（本地历史断层，%s–%s 天窗口内没有基准）\n' \
             "$DOM_DROP_7D" "$DOM_7D_MIN_AGE" "$DOM_7D_MAX_AGE"
      echo "     **不得因为其余条件都正常就把这条腿写成「未触发」**——那会让加密信号触发计数偏低。"
      ;;
    *)
      printf '  7d 跌幅 >%s%% .... ⚪️ 无法判定（本地历史仅 %s 天，未满 7 天）\n' "$DOM_DROP_7D" "$hd"
      echo "     **不得因为其余条件都正常就把这条腿写成「未触发」**——那会让加密信号触发计数偏低。"
      ;;
  esac
}

dominance_json() {
  jq --argjson t24 "$DOM_DROP_24H" --argjson t7 "$DOM_DROP_7D" --arg st "$DOM_STATUS" \
     --argjson sev "$DOM_SEVEN_JSON" --arg hf "$DOM_HISTORY_REL" --argjson hcap "$DOM_HISTORY_MAX" '
    . + {signal:16, name:"BTC Dominance", status:$st,
         thresholds:{drop_24h_rel_pct:$t24, drop_7d_rel_pct:$t7},
         seven_day:($sev + {history_file:$hf, history_retention_max:$hcap}),
         caliber_note:"24h 与 7d 判定都用相对百分比口径；delta_pt 为百分点口径，两者不可混用。7d 基准取自本地同源历史，源不同一律拒绝比较"}' "$WORK/dom.json"
}

# ═══════════════════════ 信号 17 · 稳定币总供应（USDT+USDC）══════════════════
run_stablecoins() {
  local code c1 c2
  code="$(http_get "${LLAMA_STABLE}/stablecoins" "$WORK/st_all.json")"
  if [ "$code" != "200" ] || ! ok_json "$WORK/st_all.json"; then
    STABLE_NOTE="DeFiLlama /stablecoins 回 HTTP ${code}"; return 1
  fi
  c1="$(http_get "${LLAMA_STABLE}/stablecoincharts/all?stablecoin=1" "$WORK/st_usdt.json")"
  c2="$(http_get "${LLAMA_STABLE}/stablecoincharts/all?stablecoin=2" "$WORK/st_usdc.json")"
  if [ "$c1" != "200" ] || [ "$c2" != "200" ] || ! ok_json "$WORK/st_usdt.json" || ! ok_json "$WORK/st_usdc.json"; then
    STABLE_NOTE="DeFiLlama /stablecoincharts 回 HTTP ${c1}/${c2}（USDT/USDC）"
    return 1
  fi

  # 两条序列按 date 对齐后相加（日期不一致时只取交集，避免拿两个不同日的数相加）
  jq -n \
    --argjson t "$(cat "$WORK/st_usdt.json")" \
    --argjson c "$(cat "$WORK/st_usdc.json")" '
    ( [ $t[] | {d:(.date|tonumber), v:(.totalCirculating.peggedUSD)} ] ) as $T
    | ( [ $c[] | {d:(.date|tonumber), v:(.totalCirculating.peggedUSD)} ] ) as $C
    | ( $C | map({key:(.d|tostring), value:.v}) | from_entries ) as $cm
    | [ $T[] | select($cm[(.d|tostring)] != null)
        | {date:(.d|gmtime|strftime("%Y-%m-%d")), usdt:.v, usdc:$cm[(.d|tostring)], total:(.v + $cm[(.d|tostring)])} ]
    | sort_by(.date)' > "$WORK/st_series.json"

  local n
  n="$(jq 'length' "$WORK/st_series.json")"
  if [ "$n" -lt 2 ]; then
    STABLE_NOTE="USDT / USDC 两条序列的日期无交集（只对齐到 ${n} 天）"; return 1
  fi
  return 0
}

stable_metrics() {   # 输出 JSON 到 stdout
  jq --argjson dayout "$STABLE_DAY_OUT_USD" '
    . as $s | ($s|length) as $n
    | $s[-1] as $last
    | (if $n >= 2  then $s[-2]  else null end) as $d1
    | (if $n >= 8  then $s[-8]  else null end) as $d7
    | (if $n >= 15 then $s[-15] else null end) as $d14
    | {asof:$last.date, usdt:$last.usdt, usdc:$last.usdc, total:$last.total,
       change_1d:(if $d1  then $last.total - $d1.total  else null end),
       change_7d:(if $d7  then $last.total - $d7.total  else null end),
       change_14d:(if $d14 then $last.total - $d14.total else null end),
       base_1d:(if $d1 then $d1.date else null end),
       base_7d:(if $d7 then $d7.date else null end),
       base_14d:(if $d14 then $d14.date else null end),
       triggers:{
         net_outflow_7d:(if $d7 then ($last.total - $d7.total) < 0 else null end),
         midterm_flat_or_shrink_14d:(if $d14 then ($last.total - $d14.total) <= 0 else null end),
         daily_outflow_gt_1b:(if $d1 then (($d1.total - $last.total) > $dayout) else null end)
       }}' "$WORK/st_series.json"
}

render_stablecoins_text() {
  local m; m="$(stable_metrics)"
  local asof usdt usdc total c1 c7 c14 b1 b7 b14
  asof="$(echo "$m"  | jq -r '.asof')"
  usdt="$(echo "$m"  | jq -r '.usdt')"
  usdc="$(echo "$m"  | jq -r '.usdc')"
  total="$(echo "$m" | jq -r '.total')"
  c1="$(echo "$m"    | jq -r '.change_1d  // "null"')"
  c7="$(echo "$m"    | jq -r '.change_7d  // "null"')"
  c14="$(echo "$m"   | jq -r '.change_14d // "null"')"
  b1="$(echo "$m"    | jq -r '.base_1d  // "—"')"
  b7="$(echo "$m"    | jq -r '.base_7d  // "—"')"
  b14="$(echo "$m"   | jq -r '.base_14d // "—"')"

  bn() { awk -v v="$1" 'BEGIN{ if (v=="null") { printf "—" } else { printf "%+.2f", v/1e9 } }'; }

  echo "【信号 17】稳定币总供应（USDT + USDC）　来源：DeFiLlama stablecoins.llama.fi"
  printf '  as of              %s\n' "$asof"
  printf '  USDT               $%s B\n' "$(awk -v v="$usdt" 'BEGIN{printf "%.2f", v/1e9}')"
  printf '  USDC               $%s B\n' "$(awk -v v="$usdc" 'BEGIN{printf "%.2f", v/1e9}')"
  printf '  合计               $%s B\n' "$(awk -v v="$total" 'BEGIN{printf "%.2f", v/1e9}')"
  echo
  printf '  1 日净流入/出      $%s B（基准日 %s）\n'  "$(bn "$c1")"  "$b1"
  printf '  7 日净流入/出      $%s B（基准日 %s）\n'  "$(bn "$c7")"  "$b7"
  printf '  14 日净流入/出     $%s B（基准日 %s）\n'  "$(bn "$c14")" "$b14"
  echo
  echo "阈值判定："
  echo "$m" | jq -r '
    "  7 日净流出（流动性撤出） ...... " + (if .triggers.net_outflow_7d == null then "⚪️ 无法判定（序列不足 8 天）" elif .triggers.net_outflow_7d then "✅ 触发" else "❌ 未触发" end),
    "  中期确认：近 2 周持平或萎缩 ... " + (if .triggers.midterm_flat_or_shrink_14d == null then "⚪️ 无法判定（序列不足 15 天）" elif .triggers.midterm_flat_or_shrink_14d then "✅ 触发" else "❌ 未触发" end),
    "  单日净流出 >$1B（须标注） ..... " + (if .triggers.daily_outflow_gt_1b == null then "⚪️ 无法判定" elif .triggers.daily_outflow_gt_1b then "✅ 触发，必须在报告中标注" else "❌ 未触发" end)'
}

stablecoins_json() {
  stable_metrics | jq '. + {signal:17, name:"稳定币总供应（USDT+USDC）", status:"ok",
                            source:"DeFiLlama stablecoins.llama.fi（/stablecoincharts/all?stablecoin=1|2，按日期对齐后相加）"}'
}

# ═══════════════════════════════ 分派 ═══════════════════════════════
MISSING=""
OKCOUNT=0
mark_missing() { MISSING="${MISSING}${MISSING:+、}$1"; }

do_funding() {
  if run_funding; then
    OKCOUNT=$((OKCOUNT+1))
    if [ "$JSON" -eq 1 ]; then funding_json > "$WORK/out_funding.json"; else render_funding_text; fi
  else
    mark_missing "信号14 资金费率"
    if [ "$JSON" -eq 1 ]; then
      jq -n --arg note "$FUNDING_NOTE" '{signal:14, name:"永续资金费率", status:"missing",
        attempted:["Binance fapi","Hyperliquid info"], note:$note,
        next_step:"web_search coinglass funding rate；仍无则标 ⚪️ 数据暂缺 + 报滞后周数"}' > "$WORK/out_funding.json"
    else
      echo "【信号 14】永续资金费率　⚪️ 数据暂缺"
      echo "  已尝试来源：Binance fapi.binance.com → Hyperliquid api.hyperliquid.xyz"
      echo "  失败细节：${FUNDING_NOTE}"
      echo "  下一步：web_search coinglass funding rate；仍取不到就标 ⚪️ + 写出上次已知读数与滞后周数。"
    fi
  fi
}
do_liquidations() {
  run_liquidations || true
  mark_missing "信号15 清算"
  if [ "$JSON" -eq 1 ]; then liquidations_json > "$WORK/out_liq.json"; else render_liquidations_text; fi
}
do_dominance() {
  if run_dominance; then
    OKCOUNT=$((OKCOUNT+1))
    dom_record_and_seven
    [ "$DOM_STATUS" = "ok" ] || mark_missing "信号16 dominance 24h 变动"
    [ "$(printf '%s' "$DOM_SEVEN_JSON" | jq -r '.status')" = "ok" ] \
      || mark_missing "信号16 dominance 7d 变动"
    if [ "$JSON" -eq 1 ]; then dominance_json > "$WORK/out_dom.json"; else render_dominance_text; fi
  else
    mark_missing "信号16 BTC Dominance"
    if [ "$JSON" -eq 1 ]; then
      jq -n '{signal:16, name:"BTC Dominance", status:"missing",
              attempted:["CoinGecko /global","CoinPaprika /global"],
              next_step:"web_fetch coingecko / tradingview BTC.D；标 ⚪️ + 报滞后周数"}' > "$WORK/out_dom.json"
    else
      echo "【信号 16】BTC Dominance　⚪️ 数据暂缺"
      echo "  已尝试来源：CoinGecko /global → CoinPaprika /global（皆失败）"
      echo "  下一步：web_fetch coingecko / tradingview 的 BTC.D；标 ⚪️ + 写出上次已知读数与滞后周数。"
    fi
  fi
}
do_stablecoins() {
  if run_stablecoins; then
    OKCOUNT=$((OKCOUNT+1))
    if [ "$JSON" -eq 1 ]; then stablecoins_json > "$WORK/out_stable.json"; else render_stablecoins_text; fi
  else
    mark_missing "信号17 稳定币供应"
    if [ "$JSON" -eq 1 ]; then
      jq -n --arg note "${STABLE_NOTE:-}" '{signal:17, name:"稳定币总供应（USDT+USDC）", status:"missing",
        attempted:["DeFiLlama /stablecoins","DeFiLlama /stablecoincharts/all?stablecoin=1|2"], note:$note,
        next_step:"DeFiLlama MCP get_stablecoins；标 ⚪️ + 报滞后周数"}' > "$WORK/out_stable.json"
    else
      echo "【信号 17】稳定币总供应　⚪️ 数据暂缺"
      echo "  已尝试来源：DeFiLlama /stablecoins、/stablecoincharts/all?stablecoin=1|2"
      echo "  失败细节：${STABLE_NOTE:-未知}"
      echo "  下一步：改走 DeFiLlama MCP 的 get_stablecoins；标 ⚪️ + 写出上次已知读数与滞后周数。"
    fi
  fi
}

STABLE_NOTE=""

# --history：只读本地历史，不连网、不取数（排查 7d 腿用）。
if [ "$HISTORY_ONLY" -eq 1 ]; then
  render_dom_history
  exit 0
fi

case "$CMD" in
  funding)
    do_funding
    [ "$JSON" -eq 0 ] || cat "$WORK/out_funding.json"
    [ "$OKCOUNT" -gt 0 ] || exit 3 ;;
  liquidations)
    do_liquidations
    [ "$JSON" -eq 0 ] || cat "$WORK/out_liq.json"
    exit 3 ;;
  dominance)
    do_dominance
    [ "$JSON" -eq 0 ] || cat "$WORK/out_dom.json"
    [ "$OKCOUNT" -gt 0 ] || exit 3 ;;
  stablecoins)
    do_stablecoins
    [ "$JSON" -eq 0 ] || cat "$WORK/out_stable.json"
    [ "$OKCOUNT" -gt 0 ] || exit 3 ;;
  all)
    do_funding;      [ "$JSON" -eq 1 ] || echo
    do_liquidations; [ "$JSON" -eq 1 ] || echo
    do_dominance;    [ "$JSON" -eq 1 ] || echo
    do_stablecoins
    if [ "$JSON" -eq 1 ]; then
      jq -n --slurpfile f "$WORK/out_funding.json" --slurpfile l "$WORK/out_liq.json" \
            --slurpfile d "$WORK/out_dom.json" --slurpfile s "$WORK/out_stable.json" \
            --arg missing "$MISSING" \
        '{ok:true, funding:$f[0], liquidations:$l[0], dominance:$d[0], stablecoins:$s[0],
          missing:(if $missing == "" then [] else ($missing | split("、")) end)}'
    else
      echo
      if [ -n "$MISSING" ]; then
        echo "本次数据暂缺项：${MISSING}"
        echo "  → 每一项都必须在报告中标 ⚪️、列出已尝试来源、并写出「上次已知读数 X @ YYYY-MM-DD，已滞后 N 周」。"
        echo "  → 没有滞后周数的「数据暂缺」是不合格输出（行为准则第 1 条）。"
      else
        echo "本次无数据暂缺项。"
      fi
    fi
    [ "$OKCOUNT" -gt 0 ] || exit 3 ;;
esac
exit 0
