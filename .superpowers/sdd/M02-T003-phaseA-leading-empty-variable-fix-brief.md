# M02-T003 Phase A Leading-Empty Tcl Variable Fix Brief

## Context

The independent final review of `7ed2465..03a3a20` returned REQUEST CHANGES:

- Spec: C0 / I1 / M0
- Quality: C0 / I1 / M0

The inspection I/O fix is approved. The only remaining defect is SQLite's
valid leading-empty Tcl variable form `$::foo`. Close this exact lexer gap
without changing any other approved Phase A behavior. Phase B and M02-T004
remain blocked.

## Required RED/GREEN

1. Against `03a3a20`, use real SQLite and direct normalizer comparison to prove:
   - `$::foo` and `$::foo(bar)` are valid single bind parameters;
   - valid repeated empty segments followed by an identifier, such as
     `$::::foo`, follow the real SQLite tokenizer result;
   - `$`, `$::`, `$::::`, `$(bar)`, and `$::(bar)` remain invalid;
   - whitespace/comment-split variants do not compare equal to the complete
     token;
   - spelling and case remain exact.
2. Verify the new tests fail on the production baseline for the expected
   early initial-segment rejection.
3. Make the smallest lexer change:
   - the `$` branch may begin with zero identifier characters;
   - scan every adjacent `::` segment with longest matching;
   - track whether at least one identifier character was consumed anywhere in
     the complete variable name;
   - reject the variable before an optional Tcl suffix if no identifier was
     consumed anywhere;
   - consume the optional suffix using the already-approved behavior.
4. Re-run the complete Tcl-variable, lexical/schema, and public-boundary
   selections, then migration/review, storage, and full Python 3.13 suites.
5. Run Ruff, strict mypy, `py_compile`, `git diff --check`, wheel/isolated
   import/migration-resource audit, and scope audit.
6. Append exact RED/GREEN/gate evidence to the cumulative Phase A repair
   report and update the ledger. Commit production/tests, then docs.

No task checkbox/index edit and no unrelated formatting are allowed. Phase B
can start only after a fresh independent Spec C0/I0 and Quality C0/I0 review.
