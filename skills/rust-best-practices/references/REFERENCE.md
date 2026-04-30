# Rust Coding Conventions and Best Practices Reference

This reference expands the `rust-best-practices` skill with detailed guidance
for writing, reviewing, refactoring, and optimizing Rust code.

These instructions are based on [The Rust Book](https://doc.rust-lang.org/book/),
[Rust API Guidelines](https://rust-lang.github.io/api-guidelines/),
[RFC 430 naming conventions](https://github.com/rust-lang/rfcs/blob/master/text/0430-finalizing-naming-conventions.md),
and the broader Rust community at [users.rust-lang.org](https://users.rust-lang.org).
The performance optimization techniques are referenced from
[Unofficial Guide to Rust Optimization Techniques](https://chiayong.com/articles/rust-optimization).

## General Instructions

- Always prioritize readability, safety, and maintainability.
- Use strong typing and leverage Rust's ownership system for memory safety.
- Break down complex functions into smaller, more manageable functions.
- For algorithm-related code, include explanations of the approach used.
- Write code with good maintainability practices, including comments on why
  certain design decisions were made.
- Handle errors gracefully using `Result<T, E>` and provide meaningful error
  messages.
- For external dependencies, mention their usage and purpose in documentation.
- Use consistent naming conventions following
  [RFC 430](https://github.com/rust-lang/rfcs/blob/master/text/0430-finalizing-naming-conventions.md).
- Write idiomatic, safe, and efficient Rust code that follows the borrow
  checker's rules.
- Ensure code compiles without warnings.
- Do not write duplicated code; refactor common logic into reusable functions or
  modules.

## Patterns to Follow

- Use modules (`mod`) and public interfaces (`pub`) to encapsulate logic.
- Handle errors properly using `?`, `match`, or `if let`.
- Use `serde` for serialization and `thiserror` or `anyhow` for custom errors.
- Implement traits to abstract services or external dependencies.
- Structure async code using `async/await` and `tokio` or `async-std`.
- Prefer enums over flags and states for type safety.
- Use builders for complex object creation.
- Split binary and library code (`main.rs` vs `lib.rs`) for testability and
  reuse.
- Use `rayon` for data parallelism and CPU-bound tasks.
- Use iterators instead of index-based loops because they are often faster and
  safer.
- Use `&str` instead of `String` for function parameters when you do not need
  ownership.
- Prefer borrowing and zero-copy operations to avoid unnecessary allocations.

### Ownership, Borrowing, and Lifetimes

- Prefer borrowing (`&T`) over cloning unless ownership transfer is necessary.
- Use `&mut T` when you need to modify borrowed data.
- Explicitly annotate lifetimes when the compiler cannot infer them.
- Use `Rc<T>` for single-threaded reference counting and `Arc<T>` for
  thread-safe reference counting.
- Use `RefCell<T>` for interior mutability in single-threaded contexts and
  `Mutex<T>` or `RwLock<T>` for multi-threaded contexts.

## Patterns to Avoid

- Do not use `unwrap()` or `expect()` unless absolutely necessary; prefer proper
  error handling.
- Avoid panics in library code; return `Result` instead.
- Do not rely on global mutable state; use dependency injection or thread-safe
  containers.
- Avoid deeply nested logic; refactor with functions or combinators.
- Do not ignore warnings; treat them as errors during CI.
- Avoid `unsafe` unless required and fully documented.
- Do not overuse `clone()`; borrow instead unless ownership transfer is needed.
- Avoid premature `collect()`; keep iterators lazy until you actually need the
  collection.
- Avoid unnecessary allocations; prefer borrowing and zero-copy operations.

## Performance Optimization

Apply these techniques only after profiling identifies a real bottleneck.
Optimize algorithms (O-complexity) before micro-optimizing memory or CPU usage.

### Profiling and Benchmarking First

- Use **Criterion** for micro-benchmarks with statistical analysis; never rely
  on intuition alone.
- Add structured `tracing` spans around hot paths to identify production
  bottlenecks.
- Track throughput and latency percentiles (p50, p95, p99) for I/O-heavy paths.
- Always measure before and after an optimization to confirm improvement.

### Allocation Patterns

- Use `Vec::with_capacity(n)` and `HashMap::with_capacity(n)` whenever the final
  size is known or estimable.
- For hot loops that allocate repeatedly, consider an **object pool**: reuse
  allocations rather than freeing and re-creating them.
- For tree or graph structures with a single logical lifetime, consider an
  **arena allocator** such as `bumpalo`.
- Use **adaptive pre-allocation** by tracking previous allocation sizes and
  using a rolling average to seed future `with_capacity` calls in recurring
  workloads.

### Memory Layout Optimization

- Use **hot/cold field separation** so frequently accessed fields share a cache
  line while rarely used fields move later or into a separate allocation.
- Prefer **Structure of Arrays (SoA)** over **Array of Structures (AoS)** when
  tight loops only touch one field.
- Use `#[repr(C)]` when you need a predictable, C-compatible memory layout for
  FFI, SIMD, or memory-mapped I/O.
- Add `#[repr(align(64))]` padding to hot concurrent data to prevent false
  sharing between CPU cores.

### Compile-Time Optimizations

- Use **const generics** to encode array sizes as type parameters when that lets
  the compiler eliminate runtime bounds checks.
- Prefer **`const fn`** for pure computations such as lookup tables, bitmasks,
  and hash seeds so they run at compile time.
- Use **phantom-type state machines** (`PhantomData<State>`) to enforce valid
  operation sequences at compile time.

### Lazy Evaluation and Streaming

- Use `std::cell::OnceCell` or `once_cell::sync::OnceCell` for fields whose
  computation is expensive and may not always be needed.
- Process large datasets as streams or iterators rather than loading them fully
  into memory. Keep iterator chains lazy and only `.collect()` when the caller
  truly needs a collection.
- Apply backpressure with bounded channels or semaphores when feeding async
  pipelines to prevent unbounded memory growth.

### I/O and Database Optimization

- Wrap `File` or `TcpStream` in `BufReader` or `BufWriter` to reduce system
  calls. A 64 KB buffer is a reasonable default.
- Batch database writes instead of committing one row at a time.
- Use memory-mapped files (`memmap2`) for zero-copy reads of large, read-heavy
  datasets.
- Pool database connections (`sqlx::Pool`) rather than opening a new connection
  per request.

### Lock-Free Concurrency

- Prefer `DashMap` over `Mutex<HashMap>` for high-read, moderate-write
  concurrent maps.
- Use `crossbeam::queue::SegQueue` or `std::sync::mpsc` for producer-consumer
  patterns instead of `Mutex<VecDeque>`.
- Use atomic types (`AtomicUsize`, `AtomicBool`) for simple counters and flags.
- When parallelizing with Rayon, size chunks as
  `(total_items / rayon::current_num_threads()).max(1).min(1000)` to balance
  overhead against distribution.

### Optimization Priority Order

| Impact | Effort | Techniques |
| --- | --- | --- |
| High | Low | Zero-copy patterns, `with_capacity`, iterator chains |
| High | Medium | Rayon parallelism, streaming, memory layout redesign, lazy evaluation |
| High | High | Cache-friendly restructuring, lock-free concurrency, custom allocators |

## Code Style and Formatting

- Follow the Rust Style Guide and use `rustfmt` for automatic formatting.
- Keep lines under 100 characters when possible.
- Place function and struct documentation immediately before the item using
  `///`.
- Use `cargo clippy` to catch common mistakes and enforce best practices.

## Error Handling

- Use `Result<T, E>` for recoverable errors and `panic!` only for unrecoverable
  errors.
- Prefer the `?` operator over `unwrap()` or `expect()` for error propagation.
- Create custom error types using `thiserror` or implement
  `std::error::Error`.
- Use `Option<T>` for values that may or may not exist.
- Provide meaningful error messages and context.
- Error types should be meaningful and well-behaved, including standard trait
  implementations.
- Validate function arguments and return appropriate errors for invalid input.

## API Design Guidelines

### Common Traits Implementation

Eagerly implement common traits where appropriate:

- `Copy`, `Clone`, `Eq`, `PartialEq`, `Ord`, `PartialOrd`, `Hash`, `Debug`,
  `Display`, `Default`
- Use standard conversion traits: `From`, `AsRef`, `AsMut`
- Collections should implement `FromIterator` and `Extend`
- `Send` and `Sync` are auto-implemented by the compiler when safe; avoid manual
  implementation unless you are writing documented `unsafe` code

### Type Safety and Predictability

- Use newtypes to provide static distinctions.
- Arguments should convey meaning through types; prefer specific types over
  generic `bool` parameters.
- Use `Option<T>` appropriately for truly optional values.
- Functions with a clear receiver should be methods.
- Only smart pointers should implement `Deref` and `DerefMut`.

### Future Proofing

- Use sealed traits to protect against downstream implementations.
- Structs should have private fields.
- Functions should validate their arguments.
- All public types must implement `Debug`.

## Testing and Documentation

- Write comprehensive unit tests using `#[cfg(test)]` modules and `#[test]`
  annotations.
- Use test modules alongside the code they test (`mod tests { ... }`).
- Write integration tests in `tests/` with descriptive filenames.
- Write clear and concise comments for each function, struct, enum, and complex
  logic.
- Ensure functions have descriptive names and include comprehensive
  documentation.
- Document all public APIs with rustdoc (`///` comments) following the
  [API Guidelines](https://rust-lang.github.io/api-guidelines/).
- Use `#[doc(hidden)]` to hide implementation details from public
  documentation.
- Document error conditions, panic scenarios, and safety considerations.
- Examples should use the `?` operator, not `unwrap()` or deprecated `try!`.

## Project Organization

- Use semantic versioning in `Cargo.toml`.
- Include comprehensive metadata: `description`, `license`, `repository`,
  `keywords`, `categories`.
- Use feature flags for optional functionality.
- Organize code into modules using `mod.rs` or named files.
- Keep `main.rs` or `lib.rs` minimal and move logic to modules.
- **Refactoring and modularity**: when refactoring large files (for example
  above 300 lines) into smaller submodules, use the **Facade Pattern**.
  Re-export the public items from submodules in the parent `mod.rs` so the
  refactor stays internal and does not break downstream APIs.
- **Package-style modules for large components**: when a component grows beyond
  a single focused file, prefer converting `foo.rs` into `foo/` with a real
  `foo/mod.rs` public surface and private submodules beneath it.
- **Intentional public surface**: use `mod.rs` as the canonical API boundary.
  Export only the small set of items consumers should use, and keep internal
  helpers in private modules with `pub(super)` visibility where possible.
- **Avoid deep public module trees**: do not make helper modules public just to
  avoid re-exports. Consumers should depend on
  `crate::path::component::TypeOrFunction`, not
  `crate::path::component::internal_helper::...`.
- **Facade, not barrel**: a parent `mod.rs` should not mechanically re-export
  everything. It should intentionally expose a stable public API and hide
  implementation details.
- **Split by responsibility**: for medium-sized Rust components, separate
  orchestration and runtime flow, prompt building, validation and parsing, token
  accounting, and tests into distinct files when that improves cohesion.
- **Test placement for large refactors**: prefer a dedicated `tests.rs` sibling
  module for larger integration-style unit tests, while keeping tiny
  helper-specific tests next to the helper module only when the coupling is
  strong and local.

## Quality Checklist

Before publishing or reviewing Rust code, ensure:

### Core Requirements

- [ ] **Naming**: follows RFC 430 naming conventions
- [ ] **Traits**: implements `Debug`, `Clone`, `PartialEq` where appropriate
- [ ] **Error Handling**: uses `Result<T, E>` and provides meaningful error
  types
- [ ] **Documentation**: all public items have rustdoc comments with examples
- [ ] **Testing**: comprehensive test coverage including edge cases

### Safety and Quality

- [ ] **Safety**: no unnecessary `unsafe` code and proper error handling
- [ ] **Performance**: efficient iterators, minimal allocations, and profiled
  validation before and after any non-trivial optimization
- [ ] **API Design**: functions are predictable, flexible, and type-safe
- [ ] **Future Proofing**: private fields in structs and sealed traits where
  appropriate
- [ ] **Duplicate Code**: repeated logic is refactored into reusable functions
  or modules
- [ ] **Wrapper Functions**: avoid wrapper functions that only forward to
  another function without adding value
- [ ] **Design Patterns**: apply appropriate patterns such as Builder,
  Strategy, Factory, Newtype, and Facade
- [ ] **Module Cohesion**: each file has a single focused responsibility; split
  files exceeding roughly 500 lines or mixing multiple concerns into separate
  modules while using a facade in `mod.rs`
- [ ] **Tooling**: code passes `cargo fmt -- --check`,
  `cargo clippy --workspace --all-targets -- -D warnings`, and
  `cargo nextest run --workspace --all-features --locked --no-fail-fast`
