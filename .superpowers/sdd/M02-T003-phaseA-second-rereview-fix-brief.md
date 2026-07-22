# M02-T003 Phase A Second Re-review Fix Brief

## Context and scope

Independent review of `cdb699d..663ecb0` returned REQUEST CHANGES:

- Spec: C0 / I1 / M0
- Quality: C0 / I1 / M0

Close only the two remaining Phase A findings below. Preserve all approved
cross-process convergence, coordinator, read snapshot, per-migration
transaction/cancellation, checksum bootstrap, schema-generation fence, busy,
corrupt-content, and resource-boundary behavior. Do not enter Phase B or
M02-T004 and do not edit task checkboxes/indexes.

Use strict RED/GREEN against `663ecb0`. Append exact evidence to
`.superpowers/sdd/M02-T003-phaseA-review-fix-report.md`.

## Fix 1 - complete SQLite `$` variable token longest matching

### Root cause

`_parameter_sql_token()` stops after the first `$foo` identifier segment.
SQLite tokenizes Tcl-style `$foo::bar(baz)` as one parameter token. The current
normalizer therefore reports `$foo(bar)` and `$foo (bar)` as equal even though
the first is one bind parameter and the second is a SQLite syntax error.

### Required implementation

- Keep `?NNN`, `:name`, and `@name` behavior unchanged.
- For `$` variables, match the complete SQLite variable token:
  - one required initial identifier segment;
  - zero or more adjacent `::` name segments according to actual SQLite
    longest-match behavior;
  - one optional adjacent parenthesized Tcl suffix as part of the same token.
- Whitespace/comments before `::` or `(` must end the parameter token.
- Preserve the complete parameter spelling and case; parameter names are not
  unquoted identifiers and must not be case-folded.
- Reject malformed/unclosed Tcl suffixes fail closed. Do not add SQL grammar
  parsing outside the lexical token.

### Required RED/GREEN tests

- Use real SQLite plus direct `_sql_shapes_equal` assertions for:
  - `$foo(bar)` versus `$foo (bar)`;
  - `$foo::bar` versus `$foo ::bar`;
  - `$foo::bar(baz)` versus a whitespace-split form;
  - exact bound values for each valid complete token;
  - malformed/unclosed suffix and any boundary cases discovered by the real
    SQLite probe.
- Retain the complete existing lexical/schema selection, including blob,
  numeric/exponent/underscore, NBSP, quoted-token, comments, and simple
  parameter cases.

## Fix 2 - classify inspection-time SQLite I/O consistently

### Root cause

After a read-only connection is established, `_inspect_applied()` converts
every `sqlite3.Error` to `MigrationSchemaError`. Numeric `SQLITE_IOERR` and
inspection-time `SQLITE_BUSY/LOCKED` therefore differ from the already-correct
`apply()` / `SQLiteStore.open()` public classification.

### Required implementation

- Route caught `sqlite3.Error` from `_inspect_applied()` through the existing
  numeric `_database_boundary_error()` classifier.
- `SQLITE_NOTADB` and `SQLITE_CORRUPT`, including extended codes, remain exactly
  `MigrationSchemaError("incompatible database schema")`.
- `SQLITE_IOERR`, `SQLITE_CANTOPEN`, `SQLITE_BUSY`, and `SQLITE_LOCKED`,
  including extended codes, become exactly
  `MigrationIOError("migration database I/O failed")`.
- Preserve explicitly raised migration schema/checksum/resource errors and
  `CancelledError`; do not parse exception messages or broadly catch
  `RuntimeError`/`BaseException`.

### Required RED/GREEN tests

- After a real read-only connection succeeds, inject numeric extended IOERR,
  BUSY, and LOCKED during inspection for public `plan()` and `applied()`.
  Assert exact type/message and no path/backend message leakage.
- Re-run real NOTADB and injected extended CORRUPT across
  plan/applied/apply/store-open to prove schema classification is unchanged.
- Re-run public stat/connect/config/WAL/busy/resource/cancellation boundary
  coverage.

## Completion gates

1. Exact two-finding RED/GREEN selection.
2. Complete lexical/schema and public-boundary selections.
3. Complete migration/review suite.
4. Complete `tests/integration/storage` suite.
5. Complete Python 3.13 project suite.
6. Ruff `src tests examples`, strict mypy `src`, `py_compile`,
   `git diff --check`, build/wheel isolated import and migration-resource audit.
7. Diff/scope audit: only the private lexer, migration inspection boundary,
   their tests, report, and ledger; no unrelated formatting.
8. Commit production/tests coherently, then append the report/update ledger in
   one documentation commit.

Phase B remains blocked until a fresh independent review reports Spec C0/I0
and Quality C0/I0.
