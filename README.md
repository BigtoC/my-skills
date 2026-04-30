# my-skills

Portable Agent Skills repo.

This repository uses the open Agent Skills layout so the skills are portable
across compatible agents and suitable for discovery on services such as
`skills.sh`.

## Layout

Skills live under `skills/<skill-name>/SKILL.md`.

Optional supporting material goes in sibling folders such as:

- `references/` for on-demand documentation
- `scripts/` for helper executables
- `assets/` for templates and static resources

## Skills

| Skill | Path | Purpose |
| --- | --- | --- |
| `rust-best-practices` | `skills/rust-best-practices/SKILL.md` | Portable Rust coding, review, refactoring, API design, testing, and performance guidance. |

## Installation

### Via skills.sh

Install all skills in this repo:

```sh
npx skills add BigtoC/my-skills
```

Install a single skill:

```sh
npx skills add BigtoC/my-skills/rust-best-practices
```

### Manual

Copy or vendor the skill directory into your agent's preferred skills path:

```sh
cp -r skills/rust-best-practices ~/.claude/skills/
```

## Usage

Once installed, agents that load skill descriptions at startup will
auto-invoke `rust-best-practices` whenever you work on `.rs` files or Rust
code — no extra prompt needed.

You can also invoke it explicitly:

```text
Use the /rust-best-practices skill to review this Rust module and refactor it safely.
```

### Claude Code hook (optional)

For guaranteed auto-invocation in Claude Code regardless of context, see the
[Claude Code Auto-Trigger Setup](skills/rust-best-practices/SKILL.md#claude-code-hook-based-enforcement)
section in the skill's `SKILL.md`.

## Specification

This repo follows the `SKILL.md` format described by the open Agent Skills
specification: https://agentskills.io/specification
