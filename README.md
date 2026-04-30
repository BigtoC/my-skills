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

## Usage

Agent Skills-compatible tools can load this repository directly or consume
individual skill directories.

Example prompt for an agent that supports named skills:

```text
Use the /rust-best-practices skill to review this Rust module and refactor it safely.
```

If a specific tool expects a different on-disk location, copy or vendor the
`skills/rust-best-practices` directory into that tool's preferred skills path.

## Specification

This repo follows the `SKILL.md` format described by the open Agent Skills
specification: https://agentskills.io/specification
