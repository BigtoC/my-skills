---
name: rust-best-practices
description: Rust coding conventions and review guidance. Auto-invoke whenever working with .rs files or Rust code -- implementing features, reviewing changes, refactoring modules, designing public APIs, improving tests, or optimizing performance.
license: MIT
compatibility: Portable Agent Skills format for agents that support SKILL.md. The skill itself has no script, package, or network requirements.
metadata:
  author: BigtoC
  version: "0.1.0"
  tags: "rust,coding,review,refactoring,performance,api-design"
  triggers: "*.rs Cargo.toml build.rs"
---

# Rust Best Practices

**Auto-invoke this skill whenever a task touches a `.rs` file or any Rust
code. This applies to all agent implementations.**

Use this skill when a task involves Rust code: implementing features, reviewing
changes, refactoring modules, designing public APIs, improving tests, or
optimizing performance.

## Auto-Trigger Setup

### All agents (description-based)

Agents that load skill descriptions at startup will auto-invoke this skill
whenever `.rs` files or Rust keywords appear in the task context. No extra
configuration is needed if your agent follows the Agent Skills specification.

### Claude Code (hook-based enforcement)

For guaranteed invocation regardless of context, add this hook to
`~/.claude/settings.json`:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Write|Edit",
        "hooks": [
          {
            "type": "command",
            "command": "jq -r '.tool_input.file_path // \"\"' | { read -r f; if printf '%s' \"$f\" | grep -qE '\\.rs$'; then printf '{\"hookSpecificOutput\":{\"hookEventName\":\"PreToolUse\",\"additionalContext\":\"Rust (.rs) file detected. Invoke the rust-best-practices skill now if you have not already done so this session.\"}}'; fi; } 2>/dev/null || true",
            "timeout": 10
          }
        ]
      }
    ]
  }
}
```

Merge into any existing `hooks.PreToolUse` array rather than replacing it.

Keep solutions idiomatic, safe, and maintainable. Favor strong typing, explicit
error handling, public API stability, and borrowing-first designs.

## Core Workflow

1. Identify the task type: new implementation, review, refactor, API design,
   debugging, or optimization.
2. Preserve correctness and readability before chasing cleverness.
3. Prefer borrowing over cloning, `Result` over panics, and focused modules over
   large mixed-responsibility files.
4. When refactoring, preserve the public surface and use `mod.rs` re-exports or
   other facade patterns to hide internal changes.
5. When optimizing, profile first and improve algorithms before micro-tuning
   allocations or concurrency.
6. Document public APIs, cover edge cases with tests, and keep standard Rust
   tooling green if the project uses it.

## Default Guidance

- Write idiomatic Rust that compiles cleanly and avoids warnings.
- Prefer `&str` over `String` for parameters when ownership is not required.
- Avoid `unwrap()`, `expect()`, and panics in library code.
- Use meaningful error types and add context to failures.
- Avoid duplicated logic, unnecessary wrappers, and deep nesting.
- Keep modules cohesive and split large files by responsibility.
- Treat performance advice as opt-in for measured bottlenecks, not default
  complexity.

## Review Priorities

When reviewing Rust code, prioritize:

1. Safety and correctness
2. Error handling and API clarity
3. Ownership, borrowing, and allocation behavior
4. Test coverage and rustdoc quality
5. Performance issues backed by real workload needs

## Refactoring Guidance

- Keep `main.rs` and `lib.rs` thin.
- Prefer package-style modules for larger components.
- Use private helper modules and expose only an intentional public surface.
- Remove pass-through wrapper functions that add no value.

## Performance Guidance

- Optimize only after profiling identifies a real bottleneck.
- Prefer iterator pipelines, pre-allocation, streaming, and lazy evaluation
  before more invasive rewrites.
- Consider memory layout and concurrency changes only when the workload justifies
  them.

## Detailed Reference

See [references/REFERENCE.md](references/REFERENCE.md) for the full Rust
guidance, including:

- ownership and lifetime rules
- patterns to follow and avoid
- API design guidelines
- testing and documentation expectations
- project organization rules
- performance techniques and optimization priorities
- a publish/review checklist
