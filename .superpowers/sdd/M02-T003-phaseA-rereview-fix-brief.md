# M02-T003 Phase A Re-review Fix Brief

## Context and objective

The independent re-review of `db7cd77` / `83c1d54` returned REQUEST CHANGES:

- Spec: C0 / I2 / M0
- Quality: C0 / I3 / M0

This is a second, surgical Phase A repair. Close all five findings without
entering Artifact publish/read/delete, Phase B, or M02-T004. Preserve the
already-approved coordinator, read-snapshot, per-migration transaction,
checksum bootstrap, schema-generation fence, and cancellation behavior.

Use strict RED/GREEN. Each new regression test must fail against `83c1d54` for
the expected reason before production code changes. Append exact evidence to
`.superpowers/sdd/M02-T003-phaseA-review-fix-report.md`.

## Repair 1 - converge after a cross-process peer reaches schema 4

### Root cause

`MigrationRunner._apply_locked()` selects the legacy migration path from an
inspection performed before the writer lock. Once `SQLiteStore._migrate()`
obtains `BEGIN IMMEDIATE`, `_discover_schema_state()` only recognizes
EMPTY/V1/V2/V3. If a competing process reached exact v4 while this process was
waiting, the second process rejects the valid v4 table set with raw
`ValueError` instead of converging.

### Required implementation

- Re-discover through the transaction-local trusted migration inspection while
  each legacy migration writer transaction is held, or add an equivalently
  exact v4 discovery branch that validates the complete trusted v4 schema and
  migration checksums before returning.
- A peer-observed exact v4 is success. A malformed or untrusted v4 remains a
  stable migration schema/checksum failure; never accept it from table names
  alone.
- Do not weaken the independent transaction boundary for migrations 1-4.

### Required RED/GREEN tests

- Deterministic real-subprocess races from initial EMPTY, v1, and v2: process B
  finishes its pre-lock inspection and pauses; process A applies through exact
  v4; process B resumes. Both processes must exit 0 and `applied()` must be
  exactly versions 1-4 with trusted checksums.
- Include the existing v3 cross-process race and same-process coordinator tests
  in the focused gate.
- The tests must use bounded waits and always terminate child processes in
  `finally`.

## Repair 2 - replace the partial SQL normalizer with SQLite-token comparison

### Root cause

The current lexer deletes all `str.isspace()` characters before tokenization
and scans decimal digits separately from following word tokens. It therefore
accepts different SQLite tokenizations, including `X'41'` versus `X '41'`,
`1e2` versus `1 e2`, and SQLite-non-whitespace such as NBSP versus ASCII space.

### Required implementation

- Tokenize from the original input using SQLite lexical boundaries. Ignore
  only SQLite ASCII whitespace (`U+0009`, `U+000A`, `U+000C`, `U+000D`,
  `U+0020`) and syntactically complete SQL comments.
- Recognize blob literals (`X'...'`) as one adjacency-sensitive token.
- Recognize complete numeric tokens: integer, decimal, leading-dot decimal,
  exponent, and hexadecimal forms supported by SQLite. Preserve token kind and
  boundaries; external whitespace must not merge or split tokens.
- Recognize word/identifier tokens, parameters, quoted strings/identifiers,
  longest-match operators, and punctuation. Preserve the exact content and
  escape spelling of single quotes, double quotes, backticks, and brackets.
- Treat non-ASCII characters according to SQLite identifier rules rather than
  Python `str.isspace()`. Reject malformed/unclosed quotes, comments, blobs,
  numeric literals, or parameters fail closed.
- Unquoted keyword/identifier case and genuine external SQLite whitespace may
  normalize; quoted contents must not.
- Keep all v1-v4 table/index validators on one guarded comparison helper. Do
  not add a parser dependency.

### Required RED/GREEN tests

- Direct comparison and real SQLite behavior for:
  - `SELECT X'41'` versus `SELECT X '41'`;
  - `SELECT 1e2` versus `SELECT 1 e2`;
  - ASCII space versus NBSP between identifiers;
  - decimal/exponent/hex numeric boundaries and `?NNN`, `:name`, `@name`, and
    `$name` parameter boundaries;
  - complete comments versus operators, and unterminated comments/quotes.
- Retain all earlier literal case/whitespace, escaped quote, quoted identifier,
  operator, word/number boundary, v3 tamper, and v4 tamper tests.

## Repair 3 - stable busy-exhaustion classification after WAL configuration

### Root cause

`_with_busy_retry()` raises a generic `RuntimeError` at its deadline.
`_apply_locked()` maps configuration failures but not busy exhaustion from a
later migration `BEGIN IMMEDIATE`, so public `apply()` / `SQLiteStore.open()`
leak `SQLite migration apply conflict` when the database is already in WAL.

### Required implementation

- Introduce one private SQLite busy-exhaustion exception that remains a
  `RuntimeError` subtype for existing internal compatibility.
- `_with_busy_retry()` raises that type only for exhausted BUSY/LOCKED retries.
- Public migration/open boundaries map that private type to exactly
  `MigrationIOError("migration database I/O failed")` after settling and
  closing the connection.
- Do not catch arbitrary `RuntimeError`; injected migration checkpoints and
  application bugs must still propagate for fault tests.

### Required RED/GREEN tests

- Put EMPTY, v1, v2, and v3 databases in WAL first, hold a real second writer
  lock, and verify `MigrationRunner.apply()` and `SQLiteStore.open()` return the
  exact sanitized public error with no path/SQLite message.
- Release the lock and prove a fresh apply/open succeeds. Assert no transaction
  or writer-lock leak.

## Repair 4 - classify corrupt database contents consistently

### Root cause

SQLite NOTADB/CORRUPT raised while enabling WAL is wrapped as configuration
`RuntimeError`, then incorrectly mapped to `MigrationIOError`. The same file is
already classified as `MigrationSchemaError` by `applied()`.

### Required implementation

- Use SQLite error codes, including extended-code primary masking where
  necessary, to recognize `SQLITE_NOTADB` and `SQLITE_CORRUPT` through the
  exception chain. Do not parse platform-specific exception text.
- `plan()`, `applied()`, `apply()`, and `SQLiteStore.open()` must all return
  exactly `MigrationSchemaError("incompatible database schema")` for corrupt
  database contents.
- Actual path/connect/permission/I/O/busy failures remain the sanitized
  `MigrationIOError`; checksum/resource failures retain their dedicated types.
- Preserve `CancelledError` unchanged.

### Required RED/GREEN tests

- Exercise all four public paths against a real NOTADB file and injected
  CORRUPT extended-code errors. Assert exact type/message and no path, OS text,
  or SQLite message.
- Retain parent/file/stat/connect/configure/WAL/resource and cancellation tests.

## Repair 5 - sanitize ordinary packaged-resource backend failures

### Root cause

`resources.files()`, `iterdir()`, and `read_bytes()` only map `OSError`.
Ordinary resource backends can also raise `ModuleNotFoundError`,
`zipfile.BadZipFile`, `EOFError`, and similar `Exception` subclasses containing
environment paths.

### Required implementation

- Isolate resource enumeration and resource byte reads in small synchronous
  boundary helpers.
- Map ordinary `Exception` failures from `resources.files`, enumeration,
  child-name access, `joinpath`, and `read_bytes` to exactly
  `MigrationResourceError("packaged migration resource is unavailable")`.
- Keep manifest-name/count/version validation, checksum mismatch, and UTF-8
  decode failures in their existing dedicated stable classifications. Do not
  catch `BaseException`.

### Required RED/GREEN tests

- Independently inject a non-`OSError` failure at files/enumeration/name/read
  boundaries, including at least `ModuleNotFoundError`, `BadZipFile`, and
  `EOFError`. Assert stable type/message and no backend path/message leakage.
- Re-run the trusted four-resource checksum/package audit.

## Completion gates

Before committing:

1. New five-finding RED/GREEN selection.
2. Complete migration plus review-fix suite.
3. Complete `tests/integration/storage` suite.
4. Complete project suite on Python 3.13.
5. Ruff on `src tests examples` and strict mypy on all source files plus focused
   migration/SQLite files.
6. `py_compile`, `git diff --check`, public import, build/wheel migration
   resource audit, and Phase A/Phase B/M02-T004 scope audit.
7. Append exact commands/counts and RED evidence to
   `.superpowers/sdd/M02-T003-phaseA-review-fix-report.md`.
8. Commit one coherent production/test repair and one documentation/ledger
   update. Do not change task checkboxes or task index.

Phase B remains blocked until a fresh independent review reports Spec C0/I0
and Quality C0/I0.
