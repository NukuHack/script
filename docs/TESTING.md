# General Testing Summary (Language-Agnostic)

**Applies to C, C++, Rust, Python, Java, Go, JavaScript, C#, etc.**

**Goal:** reduce bugs, not prove zero bugs. No language guarantees bug-free code. Combine several testing methods.

---

## 1. Start from the contract, not the code

For every function/module, define:

- **Valid inputs**
- **Invalid inputs**
- **Preconditions** — what must be true before calling
- **Postconditions** — what must be true after
- **Invariants** — what must always hold
- **Errors/failures** — what can go wrong
- **Side effects** — files, network, DB, global state

Then write tests that check those.

---

## 2. Use a mix of test types

| Type | Purpose | Example |
|---|---|---|
| **Unit** | Test small isolated pieces | `add(2, 3) == 5` |
| **Edge/Boundary** | Special values | `0`, max, empty, null, NaN, overflow |
| **Property-based** | General laws over many inputs | `reverse(reverse(x)) == x` |
| **Integration** | Modules together, DB, network, files | API + database |
| **Regression** | Every fixed bug becomes a test | bug #123 test |
| **Golden/Snapshot** | Output formatting/serialization | JSON, XML, reports |
| **Fuzz** | Random malformed input | crashes, security holes |
| **Concurrency** | Races, deadlocks, async | threads, shared state |
| **Performance** | Speed, memory | benchmarks |

**Test pyramid:** many fast unit tests, fewer integration tests, very few end-to-end tests.

---

## 3. Choose test cases systematically

Don’t rely on a fix count. Use:

- **Equivalence partitioning** — group inputs into valid and invalid classes.
- **Boundary value analysis** — test just inside and just outside limits.
- **Special values** — `null`, `0`, `-0`, empty, `NaN`, `Inf`, max/min, overflow, unicode, control chars, very long strings.
- **State/sequence** — repeated calls, order dependence, concurrent access.
- **Error paths** — I/O failure, timeout, permission denied, out of memory.

---

## 4. Make tests deterministic

- Log the random seed on failure. (use sys.time) 
  Unseeded randomness makes tests non-reproducible. 
- Mock/stub external services, time, network, filesystem. 
- Avoid flaky tests. 

---

## 5. Practical workflow

1. Write the contract.
2. Add unit tests for normal and boundary cases.
3. Add property-based tests for general laws.
4. Add integration tests for real dependencies.
5. Run coverage to find untested branches.
6. Add a regression test for every bug.
7. Automate everything in CI.

---

## 6. Common mistakes

- Only testing the happy path.
- Using arbitrary small counts instead of systematic coverage.
- Testing implementation details instead of behavior.
- Ignoring error paths.
- Flaky integration tests.
- No regression tests after bug fixes.

---

## 7. Tooling examples by language

| Language | Unit/Integration | Property/Fuzz |
|---|---|---|
| C | Unity, CMocka, Check | libFuzzer, AFL |
| C++ | GoogleTest, Catch2 | RapidCheck, libFuzzer |
| Rust | built-in `#[test]` | proptest, quickcheck, cargo-fuzz |
| Python | pytest, unittest | Hypothesis |
| Java/Kotlin | JUnit, TestNG | jqwik, Kotest |
| JavaScript/TS | Jest, Mocha | fast-check |
| Go | `testing` | rapid, go-fuzz |
| C# | xUnit, NUnit | FsCheck |

---
