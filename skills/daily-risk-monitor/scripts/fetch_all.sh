#!/usr/bin/env bash
# fetch_all.sh —— 第 1.1 步取数调度器（起十个单元 / 逐单元收退出码）
#
# ┌─ 为什么调度要是一支脚本，而不是 SKILL.md 里一段照抄的 shell ───────────────┐
# │ 因为**被调用方打出来的那一行命令**才是外界看得到的东西，脚本内部不是。    │
# │ 原本那段二十行的调度块里有函式定义、后台运算子与万用字元删档；把同一段    │
# │ 逻辑收进本档后，调用方打出来的只剩 `fetch_all.sh launch` 一条普通命令，    │
# │ 十个单元的行为一个字都没有改——同样的重导向、同样的 <unit>.rc、同样的     │
# │ 退出码。这同时让这一步跟本技能其余步骤一样：**一条命令、可单独调用调试**。│
# │ （某些宿主还会因为那几个构造而每次都要人工核可；细节属于宿主，不属于本    │
# │ 档，写在 repo 的 CLAUDE.md 里。）                                        │
# └──────────────────────────────────────────────────────────────────────────┘

# ┌─ 单一真源 ───────────────────────────────────────────────────────────────┐
# │ 十个单元的清单、各自的参数、以及每个参数为什么不能省，**只写在本档**。    │
# │ SKILL.md 不再复述这些命令——两份清单各自漂移、哪一份跑了就决定报告写什么， │
# │ 正是本仓库点名要避免的最坏失败（见 CLAUDE.md 的两份 TH 字典）。          │
# │ 要看清单而不想开档：`fetch_all.sh list` —— 它印的是**可以逐条手跑的完整   │
# │ 命令**（含重导向与 .rc 落档），所以「单独跑一支来除错」「没有作业控制时    │
# │ 逐条串行」这两条路，不需要本档也走得通。                                  │
# └──────────────────────────────────────────────────────────────────────────┘
#
# 用法:
#   fetch_all.sh launch [--run DIR] [--serial]   起十个单元（预设丢背景，立刻返回）
#   fetch_all.sh join   [--run DIR] [--timeout N] 逐单元读 <unit>.rc，印退出码与 stderr
#   fetch_all.sh list                            只印单元清单与各自覆盖的信号，不取数
#
# ⚠️ launch 与 join **刻意是两次呼叫**：中间要去派发 1.2 的检索分组，取数那几十秒
#    才藏得进检索延迟底下。每次呼叫都是全新 shell，所以退出码一律经 <unit>.rc 落档
#    回收，**绝不用 $! / wait**（跨呼叫收不到 PID，裸 wait 会把每个单元误判成失败）。
#
# ⚠️ **没有作业控制的环境改用 `launch --serial`**：十个单元逐条串行跑完才返回，
#    落档、退出码、stderr 分流与背景模式**逐字相同**，只是慢。join 照跑不误。
#
# 依赖：bash。各单元自己的依赖见 SKILL.md 的 compatibility 一行（本档不新增任何依赖）。
# 退出码：0 正常｜1 参数错误｜2 依赖缺失（定位不到 scripts 目录）

set -uo pipefail

PROG="$(basename "$0")"
# shellcheck disable=SC1007  # `CDPATH= cd` 是刻意的：只为这一条命令清掉 CDPATH
SCRIPTS="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
RUN_DEFAULT="/tmp/drm-fetch"
JOIN_TIMEOUT_DEFAULT=180        # join 最多再等 3 分钟；超时按取数失败处理

# 单元清单 —— 一行一个单元，栏位以 `|` 分隔：
#   <单元名> | <stdout 目标档名> | <承载资料的档名> | <命令>
# ⚠️ 第 2、3 栏对九个单元是同一个档，**只有 market 不是**：`market.py` 自己用 --json
#    落档，stdout 只剩一行「已写入 …」，所以它的 stdout 收进 market.out，而 join 要
#    验的是 market.json。两栏合并就会变成「验一行『已写入』当作资料在」——缺档静默
#    通过，正是守则三要挡的那件事。
units() {
  cat <<UNITS
fred_series|fred_series.json|fred_series.json|"\$SCRIPTS"/fred.sh VIXCLS VXVCLS T10Y2Y T10Y3M SAHMREALTIME --days 30 --json
fred_netliq|fred_netliq.json|fred_netliq.json|"\$SCRIPTS"/fred.sh --net-liquidity --days 5 --json
fred_buffett|fred_buffett.json|fred_buffett.json|"\$SCRIPTS"/fred.sh --buffett --json
fred_credit|fred_credit.json|fred_credit.json|"\$SCRIPTS"/fred.sh --credit --json
fred_rates|fred_rates.json|fred_rates.json|"\$SCRIPTS"/fred.sh --rates --json
cnn_fng|cnn_fng.json|cnn_fng.json|"\$SCRIPTS"/cnn_fng.sh --json
crypto_all|crypto_all.json|crypto_all.json|python3 "\$SCRIPTS"/crypto.py all --json
stock_perp|stock_perp.json|stock_perp.json|python3 "\$SCRIPTS"/stock_perp.py --from-fred --json
cape|cape.json|cape.json|"\$SCRIPTS"/cape.sh --json
market|market.out|market.json|python3 "\$SCRIPTS"/market.py --json "\$RUN"/market.json
UNITS
}

# 每个单元覆盖哪些信号、以及参数为什么不能省 —— `list` 与本档注解共用这一份。
notes() {
  cat <<'NOTES'
fred_series   信号 4、23、24 —— 一次给多个序列 ID，脚本逐个请求。
              ⚠️ 信号 1 的 BAMLH0A0HYM2 **已从这一批移走**，改由 fred_credit 统一产出。
                 理由：信号 1 现在是三条腿且要同日对齐，留在这里等于让同一个信号有两个
                 产出口，两边哪天不一致，报告引用到的就是当天碰巧读了哪一份。
              ⚠️ --days 30 **不可省**：fred.sh 预设只回最近 1 笔，而这一组里有两个判定
                 要历史序列——信号 23 是「倒挂后重新转正」（要有前一笔倒挂读数）、硬阈值
                 第 1 项「VIX >25 连 3 个交易日」同理。只有一笔时这两项根本不可判定，却
                 看不出缺了什么：单一读数在绝对值远离阈值的日子会「碰巧」得出 ❌，等到
                 真的逼近阈值那天才错。
fred_netliq   信号 5 —— 要看「连 4 周下降」，故取 5 笔。
fred_buffett  信号 27 —— 同季对齐后取末行 + 50–300% 量级自检
              （上限 2026-09-14 由 250 放宽，见 signals-e-cycle-valuation.md 编者注）。
fred_credit   信号 1 的三条腿：HY / IG / BBB OAS，同日对齐后并排 + 分化判定。
              HY 的口径不变、仍是唯一计入 Tier 1 的腿；IG/BBB 只是观察腿，用各自的 p95。
fred_rates    信号 32 的三条腿：名目 / 实质 / 盈亏平衡，同日对齐 + 恒等式自检 + 驱动源拆解。
              ⚠️ 本项自 2026-09-14 起改为**每日**（原为仅周一），但仍不计入 30 项、
                 不参与触发计数。
cnn_fng       信号 9 → references/signals-b-positioning.md
crypto_all    信号 14–17（子命令必给）→ references/signals-c-crypto.md
stock_perp    信号 18 → references/signals-c-crypto.md「D. 美股 24/7 永续」
cape          信号 28 → references/signals-e-cycle-valuation.md
market        信号 19–22、26、33–34 → signals-d-antiemotion.md、signals-e-cycle-valuation.md
              market.py 自己用 --json 落档，stdout 只有一行「已写入 …」，故 stdout 收进 .out。
NOTES
}

usage() {
  cat <<EOF
${PROG} —— daily-risk-monitor 第 1.1 步取数调度器

用法:
  ${PROG} launch [--run DIR] [--serial]      起十个取数单元
  ${PROG} join   [--run DIR] [--timeout N]   逐单元收退出码与 stderr
  ${PROG} list                               只印单元清单与覆盖的信号

选项:
  --run DIR      产物目录（预设 ${RUN_DEFAULT}，亦可用环境变数 DRM_RUN）
                 launch 与 join **必须给同一个路径**。
  --serial       launch 改为逐条串行（没有作业控制的环境用），输出逐字相同
  --timeout N    join 等待每个 .rc 的总期限秒数（预设 ${JOIN_TIMEOUT_DEFAULT}）

退出码：0 正常｜1 参数错误｜2 依赖缺失
EOF
}

die() { printf '%s: %s\n' "$PROG" "$1" >&2; exit "${2:-1}"; }

unit_names() { units | cut -d'|' -f1; }

cmd_list() {
  local RUN="$1"
  printf '取数单元（共 %s 个）——清单的单一真源就是本档。\n' "$(unit_names | wc -l | tr -d ' ')"
  printf '下面是 `launch` 实际执行的完整命令，可逐条手跑；先设好这两个变数：\n\n'
  printf '  SCRIPTS=%s\n' "$SCRIPTS"
  printf '  RUN=%s\n\n' "$RUN"
  printf '（背景模式行末的 & 就是 launch 起的那个；launch --serial 等同于**把 & 拿掉**逐条跑，\n'
  printf '  落档、退出码、stderr 分流逐字相同。本段是除错用的运行输出，不要贴进报告正文。）\n\n'
  units | while IFS='|' read -r u out payload cmd; do
    [ -n "$u" ] || continue
    printf '  # %s\n' "$u"
    printf '  ( %s >"$RUN/%s" 2>"$RUN/%s.err" </dev/null; echo $? >"$RUN/%s.rc" ) &\n' \
           "$cmd" "$out" "$u" "$u"
    [ "$out" = "$payload" ] || printf '  #   ↑ 资料在 $RUN/%s，$RUN/%s 只有一行「已写入 …」；join 验的是前者。\n' "$payload" "$out"
  done
  printf '\n各单元覆盖的信号与参数注记：\n\n'
  notes | sed 's/^/  /'
}

cmd_launch() {
  local RUN="$1" SERIAL="$2"
  mkdir -p "$RUN" || die "无法建立产物目录 $RUN" 1

  # 清掉上次残留：「档在」≠「本次写的」。收作业那一侧只看「存在且非空」，
  # 不清掉的话，本次失败的单元会拿到昨天那一份而毫无征兆。
  rm -f "$RUN"/*.json "$RUN"/*.err "$RUN"/*.rc "$RUN"/*.out 2>/dev/null

  local n=0
  while IFS='|' read -r u out payload cmd; do
    [ -n "$u" ] || continue
    # shellcheck disable=SC2086
    eval "set -- $cmd"
    if [ "$SERIAL" = "1" ]; then
      ( "$@" >"$RUN/$out" 2>"$RUN/$u.err" </dev/null; echo $? >"$RUN/$u.rc" )
    else
      ( "$@" >"$RUN/$out" 2>"$RUN/$u.err" </dev/null; echo $? >"$RUN/$u.rc" ) &
    fi
    n=$((n + 1))
  done <<EOF
$(units)
EOF

  if [ "$SERIAL" = "1" ]; then
    printf '%s 个单元已**串行**跑完（--serial），产物在 %s\n' "$n" "$RUN"
  else
    printf '%s 个单元已丢到背景，产物在 %s\n' "$n" "$RUN"
    printf '起完就立刻去派发 1.2 的检索分组，不要在这里等；稍后用 `%s join` 收。\n' "$PROG"
  fi
  printf '单元：%s\n' "$(unit_names | tr '\n' ' ')"
}

cmd_join() {
  local RUN="$1" TIMEOUT="$2"
  local deadline=$(( $(date +%s) + TIMEOUT ))
  local u rc

  printf '收作业（产物目录 %s，期限 %s 秒）——逐单元读 <unit>.rc，绝不用 wait。\n\n' "$RUN" "$TIMEOUT"
  for u in $(unit_names); do
    while [ ! -f "$RUN/$u.rc" ] && [ "$(date +%s)" -lt "$deadline" ]; do sleep 1; done
    if [ -f "$RUN/$u.rc" ]; then rc="$(cat "$RUN/$u.rc")"; else rc="TIMEOUT"; fi
    printf '── %s exit=%s\n' "$u" "$rc"
    # 缺档必须响亮失败：缺档绝不能被读成「该单元没有数据」。验的是**承载资料的那个档**
    # （market 是 market.json，不是只有一行「已写入 …」的 market.out）。
    local payload
    payload="$(units | awk -F'|' -v u="$u" '$1==u {print $3}')"
    [ -s "$RUN/$payload" ] || printf '  ⚠️ %s 没有产出 %s —— 按取数失败处理，不得读成「该单元无数据」\n' "$u" "$payload"
    if [ -s "$RUN/$u.err" ]; then
      printf '  stderr:\n'
      sed 's/^/    /' "$RUN/$u.err"
    fi
  done

  cat <<'TAIL'

⚠️ rc=TIMEOUT 与 rc=0 一样要当一件事处理：它代表这个单元至今没跑完，不代表它没数据。
   按取数失败走（该单元覆盖的信号逐项 ⚪️ + 列已尝试来源 + 报滞后周数），不得写成「未触发」。
⚠️ 几个非零码是**设计如此、不是故障**：crypto.py 的 liquidations 一定 exit 3（在 all 里
   是暂缺项、all 本身仍回 0）；fred.sh --buffett / --net-liquidity 与 cape.sh 量级自检不过
   是 exit 4；stock_perp.py 撞上 ctxs 短缺是 exit 3（逐市场降级，另一个市场照常输出）。
⚠️ join 之后、写任何一个字之前，先把降级浮出来：任何单元 degraded: true、do_not_quote
   非 null、或退出码非零，必须先列出来（哪个单元、退出码、degraded_reasons[] 照抄措辞），
   再开始判信号。顺序反过来，一次被限流的运行就会被静默正常化成一次正常运行。
TAIL
}

main() {
  [ $# -ge 1 ] || { usage >&2; exit 1; }

  local sub="$1"; shift
  local RUN="${DRM_RUN:-$RUN_DEFAULT}"
  local SERIAL=0
  local TIMEOUT="$JOIN_TIMEOUT_DEFAULT"

  while [ $# -gt 0 ]; do
    case "$1" in
      --run)     [ $# -ge 2 ] || die "--run 需要一个目录" 1; RUN="$2"; shift 2 ;;
      --timeout) [ $# -ge 2 ] || die "--timeout 需要一个秒数" 1; TIMEOUT="$2"; shift 2 ;;
      --serial)  SERIAL=1; shift ;;
      -h|--help) usage; exit 0 ;;
      *)         die "未知参数：$1" 1 ;;
    esac
  done

  case "$TIMEOUT" in (*[!0-9]*|'') die "--timeout 要是非负整数：$TIMEOUT" 1 ;; esac
  case "$RUN" in
    ''|'/') die "--run 不可为空或 /（本脚本会清空该目录下的 *.json / *.err / *.rc / *.out）" 1 ;;
  esac

  case "$sub" in
    list)   cmd_list "$RUN" ;;
    launch) [ -d "$SCRIPTS" ] || die "定位不到 scripts 目录：$SCRIPTS" 2
            cmd_launch "$RUN" "$SERIAL" ;;
    join)   cmd_join "$RUN" "$TIMEOUT" ;;
    -h|--help) usage ;;
    *)      die "未知子命令：${sub}（要 launch / join / list）" 1 ;;
  esac
}

main "$@"
