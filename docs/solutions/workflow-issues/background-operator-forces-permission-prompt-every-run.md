---
title: Shell background operator in an agent-typed command forces a permission prompt every run
date: 2026-09-17
category: workflow-issues
module: daily-risk-monitor
problem_type: workflow_issue
component: development_workflow
related_components:
  - tooling
  - documentation
symptoms:
  - Every run opens a permission dialog for the same ten-unit fetch-launch command
  - Dialog offers only Deny and Allow once, with no persistent-rule row
  - "`permissions.allow` entries never match the command however they are written"
  - Local bypass-permissions sessions never show it; the cloud container prompts every run
root_cause: missing_tooling
resolution_type: tooling_addition
severity: high
applies_when:
  - An instruction file tells an agent to type a multi-line shell block instead of invoking a script
  - A block uses the background operator so work survives across separate tool calls
  - A routine is expected to run unattended on a schedule
tags:
  - claude-code
  - permissions
  - background-jobs
  - agent-skills
  - shell-scripting
  - unattended-automation
---

# Shell background operator in an agent-typed command forces a permission prompt every run

## Context

`skills/daily-risk-monitor/SKILL.md` step 1.1 used to hand the agent a 45-line shell block to type verbatim (16 executable lines, the rest comments): a `bg()` function definition, `mkdir -p` plus a glob `rm -f "$RUN"/*.json`, and ten `( … ) &` background subshells fetching ten independent sources concurrently.

The backgrounding is load-bearing by design. Launch and join are deliberately **two separate tool calls** with search dispatch in between, so the jobs must outlive the call that started them and report exit codes through `<unit>.rc` files rather than `wait`.

Every run produced a permission dialog showing that whole block, offering only **Deny** and **Allow once** — no "don't ask again" row. The other two skills in the repo did not prompt.

### Approaches considered and rejected

- **Add `permissions.allow` rules.** Cannot work. The breaker fires *after* rule matching has already returned `allow`, so no rule of any shape reaches it.
- **A `PreToolUse` hook returning an allow decision.** Viable in principle and touches only the non-shipping `.claude/` layer, but it approves by matching command *shape*. The identity gate proposed would have accepted any absolute path matching `/…/skills/<name>/scripts/` — which is exactly what the repo's own documented install path produces (`npx skills add`, `cp -r skills/<name> ~/.claude/skills/`). That is blanket approval for running scripts out of any third-party skill.
- **Decompose the block into smaller, individually allow-listable commands.** Every resulting subcommand would still sit inside a `( … ) &` subshell, so the breaker
fires on each block just the same. It splits one dialog into several rather than removing any.
- **Assume the persisted rule would be a `:*` prefix.** Measured false — see *Why This Matters*.

## Guidance

**Keep the background operator, glob `rm`, shell function definitions, and `for`/`while` loops out of anything an instruction file tells an agent to _type_. Put them in a checked-in script and have the instruction name the script.**

The host parses the command string the agent types; it never parses the script's interior. Moving the logic behind a script boundary changes nothing about behaviour and everything about what the permission layer sees.

Before — typed by the agent, every run:

```bash
SKILL_DIR=<absolute path>
RUN=/tmp/drm-fetch; mkdir -p "$RUN"
rm -f "$RUN"/*.json "$RUN"/*.err "$RUN"/*.rc "$RUN"/*.out

bg() { u=$1; out=$2; shift 3
       ( "$@" >"$RUN/$out" 2>"$RUN/$u.err"; echo $? >"$RUN/$u.rc" ) & }

bg fred_series  fred_series.json  -- "$SKILL_DIR/scripts/fred.sh" VIXCLS … --json
# … nine more
```

After:

```bash
<absolute path>/scripts/fetch_all.sh launch
```

Two properties of that script are load-bearing and easy to erode:

1. **`list` must print commands runnable exactly as printed.** Since SKILL.md no longer carries the block, `fetch_all.sh list` is the *only* place a reader can see the three redirections (`>"$RUN/<unit>.json"`, `2>"$RUN/<unit>.err"`, `echo $? >"$RUN/<unit>.rc"`) that implement per-unit isolation, per-unit exit codes, and loud missing-file failure. It prints `SCRIPTS=` and `RUN=` first so the lines paste and run. Replace it with a prettified summary and the documented "debug one unit alone" and "serial by hand" fallbacks silently stop working.
2. **The units table's 2nd and 3rd columns must not be merged.** Column 2 is the stdout target, column 3 the file that carries the payload. They differ for exactly one unit — `market|market.out|market.json` — because `market.py` writes its own JSON and leaves only a one-line 「已写入 …」 on stdout. `join` verifies column 3. Merge them and a run where `market.py` wrote nothing still passes the "exists and non-empty" check on that stub.

Because the ten argv lines now live only in `fetch_all.sh`, SKILL.md must not restate them. Two copies of the unit list is the same drift trap the repo documents elsewhere; `fetch_all.sh list` is the single source.

## Why This Matters

Read directly from the shipped binary (`~/.local/share/claude/versions/2.1.274`), the
permission layer holds a circuit-breaker registry. De-minified, and **two further fields
per entry elided** (`hostPersonOnly`, `localProjectionOnly`):

```js
{ dangerousRemoval:   { bypassImmune: true,  classifierRouted: true, … },
  backgroundOperator: { bypassImmune: false, classifierRouted: true, … }, … }
```

and this decision path. **The snippet below is a readable reconstruction, not
verbatim source** — the shipped bundle is minified, so the real identifiers are
single-use names like `X8o` / `HDn`. Grep the binary for the string literals
(`backgroundOperator`, `classifierApprovable`) to relocate it, not for these
names:

```js
let g = /* rule matching */;
if (g.behavior !== "allow" || !e.command.includes("&")) return …;
if (/* auto-allowed by sandbox */) return g;
const ast = await parse(e.command);
if (ast && !hasRealBackgroundOperator(ast)) return g;
return { behavior: "ask",
         decisionReason: { circuitBreaker: "backgroundOperator",
                           classifierApprovable: false }, … };
```

`hasRealBackgroundOperator` walks the AST and returns true on any `&` node whose parent is
**not** a `binary_expression` — so `&&` and `2>&1` are correctly excluded, and a genuine
trailing `&` is not. It **also returns true on any `ERROR` node**, which is why "simplify
the command until it passes" is not a workaround: anything the parser cannot read is
treated as if it backgrounded. The reconstruction above likewise collapses a parse-bailout
sentinel in the real guard — the clause that decides the fate of an unparseable command.

Three consequences explain every observed symptom:

- The check runs **after** rule matching already returned `allow`, so **no allowlist entry can ever prevent it**.
- `classifierApprovable: false` plus `suppressAlwaysAllowRule` is why the dialog drops the "don't ask again" row — the host knows no rule it could write would help.
- `bypassImmune: false` means bypass-permissions mode *does* skip it. That is why the problem is invisible on a local machine running with bypass and fires on every run in the scheduled cloud container. Its sibling `dangerousRemoval` is `bypassImmune: true` — that one asks even under bypass.
Note it is **not** tripped by the mere presence of a glob or variable: the binary resolves
each removal target against the working directories and asks only when the target is
genuinely dangerous or statically unresolvable. The old block's
`rm -f "$RUN"/*.json` never tripped it, which is consistent with local bypass sessions
never prompting.

The only escape hatch, `sandbox.autoAllowBashIfSandboxed` (default on), is checked *before* the background-operator rule but requires a sandboxable command. These units need broad network egress to FRED, CNN, Binance, Hyperliquid, CoinGecko and multpl, so they do not qualify.

**A separate measured finding, because it changes what you can promise:** clicking "don't ask again" on a *path-invoked script* saves the **exact full command line**, not a `:*` prefix. Evidence from this machine's own accumulated rules — `~/Documents/env/futu/.claude/settings.local.json` holds five separate exact rules for one `get_kline.py`, differing only in flags, while `cargo build:*` and `git add:*` in the same files are wildcarded. The discriminator is "recognized tool", not "clean prefix".

The cross-skill evidence matches the mechanism exactly:

| Skill                | Bash blocks with a real background operator | Prompts?                              |
|----------------------|---------------------------------------------|---------------------------------------|
| `ai-industry-weekly` | none                                        | never — genuinely unaffected          |
| `ai-pullback-daily`  | one (step 1.1 **launch** block)             | yes, once per run — **still unfixed** |
| `daily-risk-monitor` | was one large block                         | was every run — **now none**          |

## When to Apply

- Authoring or reviewing any instruction file (SKILL.md, AGENTS.md, a runbook) that hands an agent a shell block to type.
- Any time a command must background work so it outlives the tool call that started it.
- Whenever a permission dialog appears repeatedly with no "don't ask again" option — **that missing row is the diagnostic signal** that a circuit breaker fired, not that a rule is merely absent.
- Before promising anyone that an allowlist entry will stop a recurring prompt.

## Examples

### Verification method — the part worth copying

The repo's `CLAUDE.md` records a precedent where a frozen-fixture oracle scored **119/130 green while `stock_perp.py` was 100% broken against the real network**. So this fix was validated live, not against fixtures:

- **Interleaved live comparison.** The old inline block and the new script were each run once against real upstreams, then diffed field by field: all ten units' **exit codes identical**, all **JSON keys identical**, **zero structural differences**. The only deltas were live market values and fetch timestamps between two launches ~30s apart.
- `launch --serial` (the no-job-control degradation path): full run **14.2s**, all ten `exit 0`.
- Stale artifacts from a previous run are cleared; the missing-payload warning fires only for the unit actually missing; an empty run directory yields 10 × `TIMEOUT` plus 10 warnings.
- Run from a foreign cwd with absolute paths; a line copied out of `list` output runs standalone and produces byte-identical output.
- `shellcheck -S warning` clean (one `SC1007` on the intentional `CDPATH= cd` idiom suppressed inline with a reason).

### Portability check

Adding the launcher required **no README change**, which is positive evidence the move was portability-neutral: the README's skills table keys on `SKILL.md` paths and names no individual scripts, and the documented install path `cp -r skills/<name> ~/.claude/skills/` already carries `scripts/`. The launcher therefore ships on every install route — unlike `.claude/agents/`, which the README explicitly documents as *not* shipping.

### Residual limitations — state these rather than claiming the prompt is gone

- The uncoverable breaker is removed, but the command is still an ordinary "no rule matched" ask on first encounter. Because a path-invoked script persists as an *exact* rule, `launch` and `join` become two separate one-time approvals unless a wildcard rule is hand-written, e.g. `Bash(/abs/path/to/scripts/fetch_all.sh:*)`.
- In a container rebuilt per run, locally saved rules may not persist at all. The value there is that the *uncoverable* prompt is gone, not that prompting is eliminated.
- `.claude/settings.local.json` was previously ignored only by the operator's machine-local `~/.config/git/ignore`. Since any rule in it embeds an absolute home path and this is a public repo, it was added to the repo's own `.gitignore` as part of the same work.

## Related

- `skills/daily-risk-monitor/scripts/fetch_all.sh` — the launcher (`launch` / `join` / `list`, `--serial`, `--run DIR`, `--timeout N`)
- `skills/daily-risk-monitor/SKILL.md` step 1.1 — now names the script instead of carrying the block
- `CLAUDE.md`「`fetch_all.sh` owns the ten fetch invocations」 — the durable record of the mechanism
- `docs/solutions/conventions/cite-docs-by-content-anchor-not-line-number.md` — companion learning from the same session; this doc's SKILL.md rewrite is what exposed it
- Commit `4860376` on branch `fix/drm-fetch-launcher-and-citation-anchors`
- **Supersedes** `docs/superpowers/specs/2026-09-05-skill-handoff-and-concurrency-design.md` §5.4 on one point: that spec prescribes 「逐 PID 收退出码（`wait $pid` 每一个）」 and predicts 「零脚本改动」. Both are now false — the launch/join split makes PIDs unrecoverable across calls, so exit codes travel via `<unit>.rc` files, and a 237-line script was added. The spec's other three 守则 survive verbatim. It carries a 2026-09-07 note declaring itself frozen; prefer extending that note over editing §5.4.
- **Open follow-up:** `skills/ai-pullback-daily/SKILL.md` has the identical defect — a real background operator at `:239` in its step 1.1 launch block, plus `for`/`while` loops (`:129`, `:274`) and `rm -f` (`:79`, `:211`) in blocks the agent is told to type. The fix is a sibling launcher, not a CLAUDE.md edit.
- No GitHub issues to link: the repo has never had an issue opened.
