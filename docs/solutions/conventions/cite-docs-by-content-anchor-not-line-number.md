---
title: Cite documentation by content anchor, never by line number
date: 2026-09-17
category: conventions
module: daily-risk-monitor
problem_type: convention
component: documentation
severity: medium
related_components:
  - tooling
symptoms:
  - "19 of 59 `file:line` citations pointed at unrelated text or at blank lines"
  - A wrong line number renders identically to a correct one, so readers blame themselves
  - An unrelated edit to one file silently invalidated several citations in another
applies_when:
  - A markdown doc cross-references specific rules that live in other files
  - Reference targets are edited on a different cadence than the citing doc
  - Citations must be auditable by a reader or agent who did not write them
  - A repo keeps deliberately duplicated docs that must not silently disagree
tags:
  - documentation
  - citations
  - cross-references
  - doc-rot
  - markdown
  - verification
  - agent-skills
---

# Cite documentation by content anchor, never by line number

## Context

`skills/daily-risk-monitor/references/search-contract.md` is a handoff contract: it specifies what a retrieval unit must return, and backs almost every rule with a citation into the file that actually owns that rule (`SKILL.md`, `signals-*.md`, `known-traps.md`, `decision-framework.md`, and some scripts). Those citations were written as `file:line`.

Reading every cited line found **19 of 59 were wrong** — about a third. By target: `SKILL.md` 8, `signals-a-macro.md` 5, `known-traps.md` 2, `scripts/crypto.py` 2, `signals-f-monday.md` 1, `data-cadence.md` 1.

Concretely:

- Two pointed at **blank lines** in `SKILL.md`.
- `signals-a-macro.md:40-41` was cited for 「无历史基准」 but pointed at the HY/IG/BBB caliber trap. The real text is at `:109`.
- `SKILL.md:148` was cited for 「每项附来源」 but pointed at an unrelated blockquote about concurrency overlap. The real text is at `:211`.

Most were already wrong before the session that fixed them. The trigger that exposed the class was an **unrelated** edit: restructuring `SKILL.md` step 1.1 shifted line numbers, invalidating several more and landing two of them *inside freshly written prose* — where they read as though they had a source.

### This is repo-wide, not one file's problem

The same audit found the same rot in three other places:

| File                                                                        | Rot found                                                                                                          |
|-----------------------------------------------------------------------------|--------------------------------------------------------------------------------------------------------------------|
| `docs/superpowers/specs/2026-09-05-skill-handoff-and-concurrency-design.md` | **9 of 26** line citations rotted — 3 point at files deleted in 2026-09-07, 1 at a blank line, 5 at unrelated code |
| `CLAUDE.md` (the FRED/Yahoo/CNN evidence table)                             | `market.py:303` → really `:343-346`; `stock_perp.py:29` → really `:34`                                             |
| `known-traps.md`                                                            | cites 「`CLAUDE.md` 记的 `scrub()` 十处、`rel_display()` 五处」; CLAUDE.md now says **fourteen and six**           |

Both CLAUDE.md citations were re-verified by hand against the current files. The file that now mandates anchors still carries two stale line numbers in an earlier section — which is the most honest possible demonstration that good intentions do not stop this failure mode.

## Guidance

Cite by **content anchor**, never by line number. Two forms, both closed with `「」`:

```
`file.md`「verbatim phrase」      ← 36 in use
`file.md` §「heading」             ← 31 in use
```

Four rules make this work:

1. **The anchor must be bytes that exist in the target.** Copy them; never paraphrase, normalize punctuation, or tidy. The anchor is a pointer *into* the file, so it has to match exactly.
2. **Delimit the heading form too.** A bare `§heading` has no terminator in running prose, so neither a reader nor a checker can tell where the heading stops and the sentence resumes. This was not theoretical: the verification regex silently swallowed the following sentence and reported ~21 false failures. `§「heading」` removes the ambiguity for both audiences at once.
3. **Do not let the anchor restate its own sentence.** Where the citing sentence already quotes the supporting text verbatim, a phrase anchor renders as an immediate duplicate. Re-anchor those to the enclosing heading. 10 of the 59 needed this.
4. **Nearest-enclosing-heading derivation needs a semantic check.** Deriving the heading for an `exit 4` bullet returned `§「四条守则（缺一不可）」` — structurally the nearest heading, but that bullet is not one of the four rules. It was hand-corrected to a phrase anchor. Mechanical derivation proposes; a reviewer disposes.

## Why This Matters

**A rotted line number looks exactly like a correct one.** That is the whole reason this failure mode is expensive.

|                     | Broken URL / missing file | Rotted `file:line`                          |
|---------------------|---------------------------|---------------------------------------------|
| Signal on follow    | 404 — announces itself    | Renders perfectly; you land *somewhere*     |
| Reader's conclusion | "The document is wrong"   | "**I** must have looked in the wrong place" |
| Gets reported       | Usually                   | Almost never                                |

The reader jumps, finds unrelated text, assumes they misread the pointer, and moves on. The document is never corrected, so the rot accumulates silently and compounds: every subsequent reformat of any referenced file breaks a few more, and nobody is counting.

A content anchor cannot fail this way. If the phrase moves, the anchor still finds it. If the phrase is deleted, the anchor greps zero times — a *loud* failure, discoverable by a script, across the whole file at once. The convention converts a silent, per-reader failure into a mechanical, whole-file check.

The second-order reason: this repo deliberately keeps two copies of the retrieval contract that must not silently disagree. Citations are how a maintainer verifies a copy still matches the rule it claims to carry. Unverifiable citations quietly disable that check.

## When to Apply

- Any markdown doc that cross-references rules living in other files.
- Whenever the referenced files are edited on a different cadence than the citing doc — the larger the gap, the faster line numbers rot.
- Whenever citations need to be auditable by someone who did not write them, including agents.
- Especially in repos with intentionally duplicated docs, where citations are the drift-detection mechanism.
- **Before a large restructuring edit**, check whether any other file cites the file you are about to reflow. That is what turned a pre-existing problem into a visible one here.

## Examples

**Before — three citations, all genuinely rotted** (non-contiguous lines, shown together; `…` marks truncation):

```markdown
`SKILL.md:148` 要求**每项附来源**。
| `attempted[].http` | …父级要能在第 8 部分列出来 | `known-traps.md:57-70`、… |
| `last_known` | …说明「无历史基准，本项完全不可判定」 | `signals-a-macro.md:40-41` |
```

Each was wrong in a different way, which is why all three are shown: `SKILL.md:148`
pointed at an unrelated blockquote (real text `:211`); `known-traps.md:57-70` pointed
into the measured-baseline table, while 「已知失效 / 陷阱清单」 actually starts at `:75`;
`signals-a-macro.md:40-41` pointed at the HY/IG/BBB caliber-trap heading (real text `:109`).

**After — every pointer greppable:**

```markdown
`SKILL.md` §「1.2 检索分组（与 1.1 同时派发，不等脚本）」 要求**每项附来源**。
| `attempted[].http` | … | `known-traps.md` §「已知失效 / 陷阱清单」、… |
| `last_known` | … | `signals-a-macro.md` §「3. 美银牛熊指标（BofA Bull & Bear Indicator）」 |
```

**The whole-file check the convention buys** — every anchor resolves or the file is broken:

```python
# `contract` is the citing file's text; `resolve()` maps a bare cited filename
# (`known-traps.md`, `crypto.py`) to its path — they live in several directories.
# The trailing `+` matters: with `*` this also matches every backticked filename
# that carries NO anchor, and the first bare mention blows up in resolve().
ANCH = re.compile(r'`([\w./-]+\.(?:md|sh|py|json))`((?:\s*(?:§)?「[^」]*」)+)')
H    = re.compile(r'§「([^」]+)」')

broken = []
for m in ANCH.finditer(contract):
    body = open(resolve(m.group(1))).read()
    for h in H.findall(m.group(2)):                                # heading anchors
        if not re.search(r'^(?:>\s*)*#{1,6}\s+.*' + re.escape(h), body, re.M):
            broken.append((m.group(1), h))
    for q in re.findall(r'「([^」]+)」', H.sub('', m.group(2))):     # phrase anchors
        if q not in body:
            broken.append((m.group(1), q))
```

Collect rather than `assert`: the point is a whole-file report, and an assert stops
at the first failure — which is how the blockquote bug below would have been missed.

Two details in that check earned their place:

- The heading pattern allows a `> ` prefix. Several real headings are **inside blockquotes**; without `(?:>\s*)*` the checker reports four false failures on correct anchors.
- It asserts a heading anchor is an actual **heading line**, not merely that the text appears somewhere. Otherwise `§「X」` degrades into an unvalidated phrase anchor.

### Verification

The edits were applied by one script and checked by a **second, independently written** one — deliberately, so a bug in the applier could not certify its own output. The independent pass confirmed:

- **0** `file:line` citations remain anywhere in the file.
- **60 anchors all resolve** — the 59 rewritten citations plus one that already used a
  bare `` `SKILL.md` `` anchor with no line number and needed no change. They decompose into 67 components — 31 heading components (each confirmed to be a real markdown heading line, including blockquoted ones) and 36 phrase components (each an exact substring). Some anchors carry both a heading and a phrase, which is why components exceed anchors.
- **Line count unchanged by the rewrite step**, and **per-row markdown table pipe counts unchanged** — citations live in table cells, where one stray `|` silently wrecks the table.
- **0 self-duplicating anchors.**

(The commit itself is +8 lines, not zero: the editor's note described above was added
in the same commit. The zero-delta check applies to the citation rewrite alone.)

The convention was then validated by accident, within the hour. An unrelated
commit (`3d1ac79`, "align table formatting and spacing") reflowed **49 lines** of
this same file — the tables where most citations live. Re-running the checker
afterwards: **60 anchors, 0 broken.** Every `file:line` token in those tables
would have shifted. That is the whole argument in one commit.

One self-referential trap worth remembering: the editor note added to warn against line numbers originally cited `SKILL.md:140` as a *negative example*, and the checker correctly flagged the file's own warning. It had to be reworded to describe the rot without writing a token that greps as a citation — **a rule stated in a file must not violate itself**, or the mechanical check becomes unrunnable.

### Scope finding — check before assuming a two-place edit

The repo rule says the retrieval contract is deliberately duplicated and that changes to the common envelope are two-place edits. Checked against that rule, this turned out to be **one place**:

- The sibling `skills/ai-pullback-daily/references/search-contract.md` has **zero** `file:line` citations; the 依據 citation column does not exist there.
- The §2 envelope field list is **identical in both set and order** across the two copies, so rewriting pointers could not make them disagree.

The generalizable lesson: **the copy that never used line numbers never rotted.** When two parallel documents diverge in quality, check whether one has already solved the problem before designing a new fix — the sibling was the reference implementation.

Surfaced while checking and *not* fixed, because it genuinely is two-place: the risk-monitor copy's `as_of` field has lost the comment `# 数据自身的日期，不是取数日` that the sibling still carries.

## Related

- `skills/daily-risk-monitor/references/search-contract.md` — the file fixed; its header note now states the convention
- `CLAUDE.md`「The contract is deliberately duplicated」 — records the anchor convention and the open `as_of` drift
- `docs/solutions/workflow-issues/background-operator-forces-permission-prompt-every-run.md` — companion learning; its SKILL.md rewrite is what exposed this rot
- `known-traps.md` 2026-09-14 editor's note — prior art for the same discipline applied to *values* rather than citations: 「改之前先 `grep -rn`」, count don't trust
- Commit `24df096` on branch `fix/drm-fetch-launcher-and-citation-anchors`
- No GitHub issues to link: the repo has never had an issue opened.
