# Subagent-Driven Development Progress

Branch: `feature/agent-sdk-implementation`
Worktree: `D:\code\AgentSDK\.worktrees\agent-sdk-implementation`
Started from: `4751118`

M01-T001: complete (commits 8e9e9ce..065aaa1, review clean)
Minor review notes for final triage: narrow `pytest.raises(Exception)` to `ValidationError`; add frozen-assignment and package-root export regression assertions.
M01-T002: complete (commits ff7d4cb..475d221, review clean)
M01-T003: complete (commits 5a3d481..5ee5c97, review clean)
M01-T004: complete (commits b8b130a..6eae6a1, review clean)
M01-T005: complete (commits ef4cd3b..933bc71, review clean)
M01-T006: complete (commits c8308f6..2259b85, review clean)
M01-T007: complete (commits 8b0afe5..29c38a6, review clean)
M01-T008: complete (commits cad4c7b..7fdd03d, review clean)
M01-T009: complete (commits 936abe4..0d1ca6c, review clean)
M01-T010: complete (commits 77878e2..346693c, review clean)
M02-T001: complete (review clean, final Task 5 re-review Critical 0 / Important 0 / Minor 0)
Design/plan commits: b3387dc, d9d596a, 358c027, 2972245, 31e347e, aeae7fb, aa3c1b8
Implementation/fix commits: 48fcf84, 687b234, 850248c, fea7460, 163f8fa, caebeb4, 4fcecae, 1af28f8, d502547, 3b689b5, 657e015, 2341f8f
Final gates: Python 3.13.14 and 3.12.13 focused 152 passed, full 705 passed, Ruff clean, mypy clean across 70 source files, build/CLI/diff-check passed
M02-T002: in progress
Phase 1 implementation: 4371aac0754b399661e23b82b808520ceaf867ba
Phase 1: complete (commits ff0e181..a78e366, initial review C0/I5/M0, fix re-review C0/I0/M0, spec compliant and task quality approved)
Phase 1 gates: lease+migration 44 passed; T001 storage/idempotency 160 passed; lazy SQLite 5 passed; full 750 passed; Ruff clean; mypy clean across 71 source files; diff-check clean
Phase 1 review range: ff0e181ddf505baed3be8fa0a793cce68e4617a6..4371aac0754b399661e23b82b808520ceaf867ba
Phase 1 review inputs: .superpowers/sdd/M02-T002-task1-brief.md, M02-T002-phase-plan.md, M02-T002-phase1-report.md, M02-T002-phase1-review.md
Phase 1 fixed findings: generation reset after release; out-of-order renewal timestamp regression; non-strict v3 JSON identity; non-canonical UTC lease timestamp storage; incomplete v2-to-v3 failure/version matrix
Phase 1 final gates: focused lease/v3 68 passed; T001 regressions 160 passed; full 774 passed; Ruff clean; mypy clean across 71 source files; diff-check clean; controller focused rerun 68 passed
Phase 2: complete (commits a78e366..2309dfb, final review C0/I0/M1, spec compliant and task quality approved)
Phase 2 brief: .superpowers/sdd/M02-T002-phase2-brief.md
Phase 2 final gates: focused 136 passed; Phase1+T001 regressions 188 passed; full 910 passed; Ruff clean; mypy clean across 72 source files; diff-check clean; controller focused rerun 136 passed
Phase 2 final Minor: duplicated Memory/SQLite pure validation remains for whole-branch triage
Phase 3: in progress
Phase 3A: complete (commits 2309dfb..ade5f55, initial review C0/I2/M0, fix re-review C0/I0/M1, spec and task quality approved)
Phase 3A brief/report: .superpowers/sdd/M02-T002-phase3a-brief.md, .superpowers/sdd/M02-T002-phase3a-report.md
Phase 3A final gates: focused 117 passed; Phase2 136 passed; Phase1+T001 188 passed; full 1027 passed; Ruff clean; mypy clean across 72 source files; diff/scope clean; controller focused rerun 117 passed
Phase 3A fixed findings: duplicate snapshot identity made exact replay impossible; Python arbitrary-precision integers caused Memory/SQLite parity and raw OverflowError gaps
Phase 3A final Minor: add direct shared-validator automation for bool/float/int-subclass, INT64_MIN/MAX acceptance, and operation/checkpoint expected branches during whole-branch triage
Phase 3B: complete (commits ade5f55..25db10a, initial review C0/I2/M0, fix re-review C0/I0/M0, spec and task quality approved)
Phase 3B brief: .superpowers/sdd/M02-T002-phase3b-brief.md
Phase 3B final gates: focused 38 passed; Phase3A 117 passed; Phase2 136 passed; Phase1+T001 188 passed; runtime/Tool/Workflow/subagent 137 passed; full 1065 passed; Ruff clean; mypy clean across 72 source files; diff/scope clean; controller focused rerun 38 passed
Phase 3B fixed review findings: lease-loss delta timer outlived execute; repeated cancellation left release task pending; defensive missing-ModelCompleted path left a started operation unresolved
Phase 3C: in progress
Phase 3C1: complete (commits 25db10a..4e6c138, initial review C0/I2/M0, fix re-review C0/I0/M0, spec and task quality approved)
Phase 3C1 brief/report: .superpowers/sdd/M02-T002-phase3c1-brief.md, .superpowers/sdd/M02-T002-phase3c1-report.md
Phase 3C1 final gates: focused 115 passed; Phase3B 38; Phase3A 117; Phase2 136; Phase1+T001 188; Session/ownership 108; full 1179 passed + 1 pre-existing environment skip; Ruff clean; mypy clean across 73 source files; diff/scope/schema clean; controller focused rerun 115 passed
Phase 3C1 fixed review findings: event-tail terminal ownership was not bidirectional; lease-free reconciliation exact replay did not strictly validate durable request wrapper and linked operation before early return
Phase 3C2: complete (commits 4e6c138..9ad98f0, initial review C0/I3/M0, second re-review C0/I1/M0, final re-review Spec C0/I0/M1 and Quality C0/I0/M1; approved)
Phase 3C2 brief/report: .superpowers/sdd/M02-T002-phase3c2-brief.md, .superpowers/sdd/M02-T002-phase3c2-report.md
Phase 3C2 fixed review findings: recovery-start lacked checkpoint CAS; LeaseHeld followers could wait forever after owner loss/expiry/close; completed no-Tool Model operation could be resent after terminal precommit failure; READY_FOR_TOOL did not require an exact completed current-turn Model operation/outcome/checkpoint/event relation
Phase 3C2 final second-fix gates: focused 89 passed; Phase3C1 115; Phase3B 38; Phase3A 117; Phase2 139; Phase1+T001 188; Session/Run/Tool/MCP/Workflow recovery/child compatibility 237; full 1272 passed; Ruff clean; mypy clean across 73 source files; diff/scope/schema clean
Phase 3C2 final Minor: multi-turn READY_FOR_TOOL validates aggregate started/completed counts and the final completed payload, but not every historical turn's event ordering and completed payload; add Memory/SQLite multi-turn reopen negative coverage during whole-branch triage
Next action: execute Phase 3D provider authoritative-status and certified same-operation-id recovery adapters; Workflow recovery remains Phase 4
Phase 3D: in progress
Phase 3D operational plan: .superpowers/sdd/M02-T002-phase3d-plan.md
Phase 3D1: complete - certified provider recovery adapters
Phase 3D1 brief: .superpowers/sdd/M02-T002-phase3d1-brief.md
Phase 3D1 implementation: da7282ea5e0f73630380e6c801092df35ae766a3
Phase 3D1 report: .superpowers/sdd/M02-T002-phase3d1-report.md
Phase 3D1 implementation gates: focused/fault/e2e 182 passed; Phase3C2 89; Phase3C1 115; Phase3B 38; Phase3A 123; Phase2 139; Phase1+T001 188; compatibility 264; full 1337 passed with zero skips; Ruff clean; mypy clean across 74 source files; diff/scope/schema/public imports clean
Phase 3D1 initial review: Not Approved; Spec C1/I0/M0 and Quality C1/I0/M0. Sole finding: forged exact-type ProviderRecoveryResult could raise strict revalidation outside the invalid-result sanitization boundary and retain secret result data in the coordinator task traceback without reconciliation
Phase 3D1 review fix: strict RED reproduced the raw retained ValidationError; exact-type checking and strict detached reconstruction now share the invalid-result cleanup boundary, which deletes all sensitive references and admits one bounded reconciliation request
Phase 3D1 review-fix gates: targeted 1 passed; invalid/timeout/secret 9; focused/fault/e2e 183; Phase3C2 89; Phase3C1 115; Phase3B 38; Phase3A 123; Phase2 139; Phase1+T001 188; compatibility 264; full 1338 passed with zero skips; Ruff clean; mypy clean across 74 source files; diff/scope/schema/public imports clean
Phase 3D1 final review: approved; Spec C0/I0/M0 and Quality C0/I0/M0. Fresh reviewer targeted 1, neighboring 11, focused 183, Phase3C2 89, Ruff/mypy/diff/scope/schema/public imports clean
Phase 3D2: implementation complete; pending independent review - certified Tool retry
Phase 3D2 brief: .superpowers/sdd/M02-T002-phase3d2-brief.md
Phase 3D2 report: .superpowers/sdd/M02-T002-phase3d2-report.md
Phase 3D2 implementation gates: focused/live 82; Phase3D1 183; Phase3C2 89; Phase3C1 115; Phase3A 123; Phase2 139; Phase1+T001 188; compatibility 237; full Python3.13 1382 passed with zero skips; Ruff/mypy/diff/public import/scope/schema clean
Phase 3D2 initial review: Not Approved; Spec C1/I1/M1 and Quality C1/I1/M1. Findings: checkpoint transcript was not authoritatively replayed from every durable turn; Tool registration could change after planning/audit or during permission early paths without one durable reconciliation; recovery audit/permission identities were application-controlled and unbounded
Phase 3D2 review fix: descriptor-to-checkpoint exact multi-turn replay now validates messages, Tool results, joined output, usage, Model/Tool fingerprints/outcomes, and critical event payload/order; exact RegisteredTool identity is checked after audit and across all permission/handler/completion boundaries with owned atomic reconciliation; recovery audit/permission/authorization identities are stable SHA-256 objects
Phase 3D2 review-fix gates: targeted authoritative/forgery 17 passed; complete Phase3D2 policy/recovery/live 109 passed; Phase3D1+Store reconciliation+recovery API neighbors 195 passed; full Python3.13 1409 passed with zero skips; Ruff clean; mypy clean across 75 source files; diff/public import/scope/schema clean
Phase 3D2 evidence limitation: durable records preserve exact joined Model output and joined delta text, but not original provider chunk partition; recovery verifies exact joined text and does not invent unavailable chunk boundaries
Phase 3D2 second review: Not Approved; Spec C0/I2/M0 and Quality C0/I2/M0; the prior C1/I1/M1 findings were explicitly confirmed closed. Findings: final handler preflight could use the old RegisteredTool after a lease-assert await; authoritative replay rejected valid historical handler-before safe results that correctly had no ToolCallOperation
Phase 3D2 second-review fix: every preflight synchronously revalidates exact RegisteredTool/spec/capability/metadata after the lease await and the final handler boundary has no subsequent await; historical Model-only turns are admitted only when the durable completion event, normalized missing/invalid/denied result, descriptor/schema or missing capability, permission evidence, Tool message, checkpoint, and Model evidence all match exactly
Phase 3D2 second-review RED/GREEN: final fourth-assert barrier RED invoked old handler once and GREEN invokes old/new/model zero with one reconciliation; Memory/SQLite historical permission-denied/invalid-arguments/tool-not-found RED 6/6 reconciled and GREEN 6/6 resumes same current operation; no-op ToolResult/permission modification/insertion negative matrix 4/4 and prior forgery matrix 15/15 remain green
Phase 3D2 second-review fresh gates: reviewer targeted 11 passed; complete Phase3D2 policy/recovery/live 120; Phase3D1/provider/store/recovery neighbors 195; Phase3C1 115; Phase3B 40; Phase3A 123; Phase2 139; Phase1+T001 188; compatibility 150 + ownership 87 = 237; full Python3.13 1420 passed with zero skips; Ruff clean; mypy clean across 75 source files; diff/import/scope/schema clean
Phase 3D2 third review: Not Approved; Spec C0/I1/M0 and Quality C0/I1/M0; every prior Critical/Important/Minor finding was explicitly confirmed closed. Sole finding: safe no-operation authoritative replay accepted broker-invalid ask/unknown permission resolutions and did not authenticate the complete Run event envelope id/ownership/cursor/continuous-sequence contract before external work
Phase 3D2 third-review RED/GREEN: exact deny-to-ask and historical sequence+1000 reviewer cases failed 4/4 across Memory/SQLite by reaching permission/MCP handler/transport/LiteLLM, then passed 4/4 with zero external work and one atomic bounded reconciliation; expanded strict permission/envelope matrix passes 44/44 across safe no-op and current Tool/Model events
Phase 3D2 third-review fix: provider/Tool certification now shares complete Run-envelope admission at plan and coordination, including global event-id uniqueness, target cursor order, exact Run/Session/agent ownership, schema/timestamp shape, contiguous SDK sequence, and exact run.created payload; PermissionRequest/Decision are canonically reconstructed with forbidden extras and only allow|deny resolution; reconciliation alone can atomically append max-positive-sequence+1 when malformed evidence makes the normal strict sequence query fail
Phase 3D2 third-review fresh gates: complete Phase3D2 policy/recovery/live 164; Phase3D1/provider/store/recovery neighbors 195; Phase3C1 115; Phase3B 40 included; Phase3A 123; Phase2 139; Phase1+T001 188; compatibility 150 + ownership 87 = 237; full Python3.13 1464 passed with zero skips; Ruff clean; mypy clean across 75 source files; diff/import/scope/schema clean
Phase 3D2 fourth review: Not Approved; Spec C0/I1/M0 and Quality C0/I1/M0; every prior finding was again confirmed closed. Sole finding: a second unique, contiguous `run.created` with changed ownership payload could be ignored outside index zero, allowing Tool work before the interrupt and Provider work before or after it
Phase 3D2 fourth-review RED/GREEN: Tool/Provider x Memory/SQLite x before/after exact duplicate matrix was 6 RED + 2 existing strict-tail GREEN, then 8/8 GREEN with zero permission/handler/MCP/LiteLLM/query/resend work and exactly one reconciliation; expanded duplicate/unknown lifecycle matrix is 19/19 GREEN
Phase 3D2 fourth-review fix: certified recovery now uses a closed SDK Run grammar; `run.created` and `run.started` are exact singletons at index 0/1, unknown Run events fail closed, interrupt/recovery counts and payloads are complete, Provider event counts cross durable operation/checkpoint state, and model/tool recovery audits cross their durable operations without rejecting valid repeated cancellation/retry audit histories
Phase 3D2 fourth-review fresh gates: Tool file 126; Provider file 45; prior envelope44 retained; expanded Provider/Store/RecoveryAPI neighbor superset 246; full Python3.13 1483 passed with zero skips; Ruff clean; mypy clean across 75 source files
Phase 3D2 fifth review: Not Approved; Spec C0/I1/M0 and Quality C0/I1/M0; every prior finding was again confirmed closed. Sole finding: known recovery audits with exact valid payloads could be inserted at impossible pre-interrupt positions and remain unconsumed by Provider count/tail or Tool selective per-turn validation
Phase 3D2 fifth-review RED/GREEN: Provider query audit and Tool retry audit before the initial interrupt were 4/4 RED across Memory/SQLite by reaching query or permission/handler/MCP/LiteLLM, then 4/4 GREEN with zero external work and exactly one reconciliation; seven further position REDs covered pre-interrupt usage/permission pairs, audit-to-recovery and permission-state insertions, and late Model tokens after Tool start
Phase 3D2 fifth-review fix: a shared ordered lifecycle FSM now consumes every certified event exactly once; audit transitions require interrupted state and the exact current turn/operation/call/Tool, recovery start consumes its matching audit, and all Model/Tool/permission transitions validate payload shape plus operation/checkpoint/descriptor identity. Valid audit-only cancellation, query-to-resend, permission cancellation, lease-loss retry, and repeated cycles remain accepted
Phase 3D2 fifth-review fresh gates: exact/expanded position-terminal-wrong-operation 18; legal cycle 12; duplicate/unknown19; envelope44; Tool131; Provider58; expanded Provider/Store/RecoveryAPI259; Phase3C2 89; Phase3C1 115; Phase3B 40; Phase3A 123; Phase2 139; Phase1+T001 188; compatibility150+ownership87; full Python3.13 1501 passed with zero skips; Ruff clean; mypy clean across 75 source files
Phase 3D2 sixth review: Not Approved; Spec C0/I2/M0 and Quality C0/I2/M0; every prior finding was again confirmed closed. Findings: historical recovery transitions were crossed against operations' later final projections and Tool admission selected the first global interrupt, rejecting legal Provider-to-Tool and Tool-to-Provider repeated recovery; Provider history did not share Tool's strict canonical normal PermissionRequest/Decision/result reconstruction
Phase 3D2 sixth-review RED/GREEN: public cross-kind Provider-to-Tool and Tool-to-Provider x Memory/SQLite were 4/4 RED by reconciling legal current work, then 4/4 GREEN and execute the current handler/Provider plus next normal phase; historical ask-allow current-Model permission request mutations x Memory/SQLite were 6/6 RED for forbidden extra, malformed arguments, and Tool mismatch, then 6/6 GREEN with zero external work and one reconciliation; legal ask allow/deny x Memory/SQLite remain 4/4 GREEN
Phase 3D2 sixth-review fix: lifecycle replay derives historical state from the ordered event prefix and crosses only its final current state with checkpoint operation/kind; Tool retry anchors at the interrupt following the current Tool's own start; recovery-only controls are excluded from logical turn counts after strict FSM consumption; shared canonical PermissionRequest/Decision parsing crosses request identity, Run/Session/Tool/arguments/effects, allow|deny decision scope/reason, and denied result for Provider and Tool histories
Phase 3D2 sixth-review fresh gates: exact cross-kind/strict-permission/legal matrix14; Provider72; Tool+RecoveryAPI220; Provider/live/scanner/Store neighbor241; full Python3.13 1515 passed with zero skips; Ruff clean; mypy clean across 75 source files; diff-check clean
Phase 3D2 seventh review: Not Approved; Spec C0/I1/M0 and Quality C0/I1/M0; all earlier findings were confirmed closed and there were no other Critical/Important/Minor findings. Sole finding: Provider history accepted a canonical historical ToolResult event without crossing ask-allow success against the corresponding terminal ToolCallOperation outcome and ordered checkpoint ToolResult/message
Phase 3D2 seventh-review RED/GREEN: public ask-allow success event value/status/content substitutions x Memory/SQLite were 6/6 RED by still completing Provider query, then 6/6 GREEN with zero query/resend/new permission/handler/LiteLLM work and exactly one reconciliation; positive Memory/SQLite success, ask-deny, handler exception, non-JSON, timeout, and Tool-recovery-produced success/failed histories remain certified
Phase 3D2 seventh-review fix: the shared lifecycle FSM now uniquely maps every Tool completion to its turn/call and ordered checkpoint ToolResult/message; operations additionally require exact terminal status/outcome, SDK-normalized result, capability/retry metadata/request fingerprint/ownership, while no-operation history remains limited to independently derived missing/invalid/deny normalized results
Phase 3D2 seventh-review fresh gates: exact+expanded28; Provider86; Tool131; Provider+Tool+RecoveryAPI306; Provider/live/scanner/Store neighbors255; Phase3C1+3B+3A+Phase2+Phase1/T001 combined605; full Python3.13 1529 passed with zero skips; Ruff clean; mypy clean across 75 source files; diff/scope clean; schema3 unchanged
Phase 3D2 eighth review: Not Approved; Spec C0/I1/M0 and Quality C0/I1/M0; all earlier findings were confirmed closed and there were no other Critical/Important/Minor findings. Sole finding: canonical normal permission requested/resolved events were not crossed against the decision reachable from the recorded execution policy, so a direct-deny history could contain a forged ask/deny pair and still reach Provider work
Phase 3D2 eighth-review RED/GREEN: public direct-deny plus inserted canonical matching requested/deny-resolved x Memory/SQLite was 2/2 RED by completing Provider query, then 2/2 GREEN with zero query/resend and exactly one reconciliation; expanded 36-case matrix covers legal/forged direct allow and deny, legal ask allow/deny, strict request reconstruction, authoritative ToolResult, and cross-kind histories
Phase 3D2 eighth-review fix: every historical call is evaluated deterministically through the production PolicyEngine from the recorded descriptor and strictly reconstructed request; ASK alone admits exact requested/resolved events, ALLOW requires authorization/execution without permission events, and DENY admits no permission/authorization events and only exact normalized denied no-op. Provider and Tool share the lifecycle semantics and replay invokes no application permission bridge. The persisted policy descriptor currently has only permission_default, so no parallel rule/workspace interpreter was invented
Phase 3D2 eighth-review fresh gates: permission reachability matrix36; Provider+Tool225; Provider+Tool+RecoveryAPI314; Provider/live/scanner/Store neighbors301; Phase3C1+3B+3A+Phase2+Phase1/T001 combined605; full Python3.13 1537 passed with zero skips; Ruff clean; mypy clean across 75 source files; diff/scope clean; schema3 unchanged
Phase 3D2 final review: Approved; Spec C0/I0/M0 and Quality C0/I0/M0. Fresh permission-reachability36, Provider+Tool225, Provider+Tool+RecoveryAPI314, neighbors301, Ruff/mypy/diff/import/scope/schema clean; full1537 implementation gate retained
Phase 3 release execution gates: passed - focused 856 plus e2e3; full Python3.12 1537 and Python3.13 1537 with zero skips; Ruff/mypy75; external build and dual-version wheel imports; diff/import/scope/schema clean
Phase 3 release report: .superpowers/sdd/M02-T002-phase3-report.md
Phase 3 release gate: implementation evidence complete; pending fresh whole-Phase-3 Spec/Quality C0/I0 review over 2309dfb..HEAD
Phase 3 whole review: Not Approved; Spec C0/I1/M1 and Quality C0/I1/M1; no other Critical/Important/Minor findings. I1: Provider query/resend could use a stale adapter because registry resolution preceded audit/refence and the final lease await. M1: READY_FOR_TOOL safe resume authenticated aggregate Model counts and only the last completion, not every historical turn relation
Phase 3 whole-review RED/GREEN: Provider Memory/SQLite final query preflight unregister/same-metadata/version/adapter-id/certification plus query-result-to-resend same-metadata, with two SDK owner/follower, was 12/12 RED via stale callbacks then 12/12 GREEN with zero affected callback and exactly one owner reconciliation; READY_FOR_TOOL 13 historical turn corruptions x Memory/SQLite were 26/26 RED by reaching Tool/MCP/LiteLLM then 26/26 GREEN before all external work, while legal multi-turn Memory/SQLite remain 2/2 GREEN
Phase 3 whole-review fix: RecoveryPlan retains the exact planned Provider adapter; query and resend each synchronously re-resolve after their final lease assertion and require exact object identity plus recorded id/version/certification metadata with no await before callback entry, otherwise the owner atomically reconciles. READY_FOR_TOOL reconstructs every descriptor-based Model request/fingerprint/outcome, assistant/Tool transcript, usage/joined output and ordered event relation through the shared lifecycle consumer without requiring Tool retry certification for the unstarted pending call
Phase 3 whole-review fresh gates: exact Provider12; exact READY_FOR_TOOL28 and all ready-tool54; Provider+Tool+RecoveryAPI354; all17 Phase3 changed files896; e2e3; full Python3.12 1577 and Python3.13 1577 with zero skips; Ruff/mypy75; external sdist/wheel and dual-version import smoke; diff/scope/schema3 clean
Phase 3 whole-review final: Approved; Spec C0/I0/M0 and Quality C0/I0/M0. Fresh exact40, Provider+Tool+RecoveryAPI354, all17 changed files896, e2e3, Ruff/mypy/import/diff/scope/schema clean; dual full1577/build gates retained
Phase 3: complete - durable progress, conservative recovery, certified Provider recovery, and certified Tool retry
Phase 4: in progress - Workflow recovery
Next action: execute the Phase 4 Workflow recovery plan with strict TDD and independent slice review
Phase 4 operational plan: .superpowers/sdd/M02-T002-phase4-plan.md
Phase 4A: in progress - exact Workflow admission and single-coordinator recovery
Phase 4A brief: .superpowers/sdd/M02-T002-phase4a-brief.md
Phase 4B remains pending until Phase 4A independent Spec C0/I0 and Quality C0/I0 review
Phase 4A implementation: a50b05017c90e19951d87fa3b836ea9f3dc4ce0a
Phase 4A initial review: Not Approved; Spec C0/I3/M0 and Quality C0/I3/M0
Phase 4A review findings: capability-admission TOCTOU allowed node CAS before failure; child creation did not authenticate the durable completed parent Run; normal-live and explicit recovery create/lease/projection races did not converge and could synthesize Workflow failure
Phase 4A review-fix brief: .superpowers/sdd/M02-T002-phase4a-review-fix-brief.md
Phase 4A review-fix implementation: e3d29654ef5cb505c36f65c8f3acc4ddc6476925
Phase 4A review-fix gates: exact 14; Phase 4A file 51; adjacent core 270; Provider/Tool 261; full Python3.13 1631 passed with zero skips; Ruff/mypy75/diff/import/scope/schema clean
Phase 4A final re-review: Approved; Spec C0/I0/M0 and Quality C0/I0/M0
Phase 4A: complete and independently approved
Phase 4B: in progress - two-SDK concurrency, external-side-effect, crash-boundary, lifecycle, and sanitization hardening
Phase 4B brief: .superpowers/sdd/M02-T002-phase4b-brief.md
Phase 4B implementation: 39485e16c456ff83b2a4a7a41d44e7e33fa76450
Phase 4B implementation gates: admission 86; Workflow combined 146; full Python3.13 1666 passed with zero skips; Ruff/mypy75/diff/import/scope/schema clean
Phase 4 whole review: Not Approved; Spec C0/I3/M0 and Quality C0/I1/M0
Phase 4 review findings: unintended public WorkflowExecutor.recover; incomplete two-backend two-SDK pending/missing/CREATED/live/interrupted/expired/delete matrix; ambiguous-commit tests omitted paired Session ownership assertions; barrier waits mixed brittle one-second and unbounded waits
Phase 4 review-fix brief: .superpowers/sdd/M02-T002-phase4-review-fix-brief.md
Phase 4 review fix: in progress; Phase 5 remains blocked
Phase 4 first review-fix implementation: 2f9ff7460c0b8aada5cb5716d10c2b6cea166a38
Phase 4 first review-fix gates: admission97; full Python3.13 1677 passed with zero skips; Ruff/mypy75/diff/import/scope/schema clean
Phase 4 first re-review: Not Approved; Spec C0/I1/M0 and Quality C0/I0/M0
Phase 4 remaining finding: expired/interrupted test fabricated an unreachable Run snapshot instead of real lease-expiry scanner transition; delete race bypassed supported busy lifecycle through direct Store deletion
Phase 4 second review fix: in progress - real scanner interruption and public busy-delete race; Phase 5 remains blocked
Phase 4 second review-fix implementation: 20cf09ec40f89f63e055202b4deab24f543124f5
Phase 4 second review-fix gates: authenticity6; admission99; full Python3.13 1679 passed with zero skips; Ruff/mypy75/diff/import/scope/schema clean
Phase 4 final review: Approved; Spec C0/I0/M0 and Quality C0/I0/M1
Phase 4 final fresh review gates: authenticity7; admission99; adjacent recovery/ownership673; full Python3.13 1679 with zero skips; Ruff/mypy75/diff/import/scope/schema clean
Phase 4 nonblocking minor: one older single-SDK waiting-reconciliation test fabricates INTERRUPTED state; replace with reachable state in Phase 5
Phase 4: complete - exact sequential Workflow recovery, two-SDK concurrency, fault boundaries, lifecycle, and sanitization
Phase 5: in progress - reconciliation decisions, subprocess/fault E2E, dual-Python release gate, and ledger
Phase 5 operational plan: .superpowers/sdd/M02-T002-phase5-plan.md
Phase 5A: in progress - strict resolution admission and explicit safe retry decisions
Phase 5A brief: .superpowers/sdd/M02-T002-phase5a-brief.md
Phase 5A implementation commits: 8c86bfe, 187b5b7, 7d60cd1, 3dacbe6, 111fea9
Phase 5A final report: .superpowers/sdd/M02-T002-phase5a-report.md
Phase 5A final gates: public/storage 168 passed; Provider/Tool lifecycle 132 passed; seven-file focused superset 546 passed; full Python3.13 1801 passed with zero skips/failures; Ruff clean; mypy clean across 75 source files; diff/import/scope/schema/signature clean
Phase 5A review: initial C0/I3/M0; successive re-reviews closed cancellation sanitization, exact Store old-generation admission, complete resolved-row discovery/pair grammar, unique turn-scoped attempt slicing, later-turn admission, and canonical lifecycle ordering
Phase 5A final review: Approved; Spec C0/I0/M0 and Quality C0/I0/M0
Phase 5A: complete - strict operator decisions, exact safe retries, atomic two-backend resolution, closed recovery grammar, lifecycle races, and secret sanitization
Phase 5B: in progress - strict confirmed external outcome projection
Phase 5B1 brief: .superpowers/sdd/M02-T002-phase5b1-brief.md
Phase 5B1 implementation/review commits: 8d9d671, 3aeaddb, 85856f4, f1b9ba0, 54b31ba
Phase 5B1 report: .superpowers/sdd/M02-T002-phase5b1-report.md
Phase 5B1 review history: four repair rounds closed replay stability after continued execution, exact terminal event/Session projection, empty usage recovery, Session successor evolution, reconciliation/operation closed-world grammar, full terminal lifecycle/provider certification, and partial-stream prefix admission
Phase 5B1 final gates: focused 288 passed; seven-file core 666 passed; adjacent 543 passed; full Python3.13 1921 passed with zero skips/failures; Ruff clean; mypy clean across 75 source files; 103 exports/signatures/schema3/diff/scope clean
Phase 5B1 final independent review: Approved; Spec C0/I0/M0 and Quality C0/I0/M0; fresh focused 310 passed and partial-stream matrix 12 passed
Phase 5B1: complete - strict confirmed Model text/ToolCall/failure outcomes, terminalization gap, exact atomic Run/checkpoint/Session/event projection, stable closed-world replay, and zero external callbacks
Phase 5B2: in progress - strict confirmed Tool outcomes and Workflow projection
Phase 5B2A brief: .superpowers/sdd/M02-T002-phase5b2a-brief.md
Phase 5B2A implementation/review commits: 2178fab, 4f22203, a05ac6b
Phase 5B2A report: .superpowers/sdd/M02-T002-phase5b2a-report.md
Phase 5B2A review history: two repair rounds closed stable replay across chronological prior/later resolved attempts and later certified READY_FOR_TOOL safe states without weakening orphan/duplicate/current-state corruption checks
Phase 5B2A final gates: focused 350 passed; core 728 passed; adjacent 543 passed; full Python3.13 1983 passed with zero failures; Ruff clean; mypy clean across 75 source files; exports/signatures/schema3/diff/scope clean
Phase 5B2A final independent review: Approved; Spec C0/I0/M0 and Quality C0/I0/M0; fresh confirmed-Tool matrix 70 passed and reconciliation/Store 350 passed
Phase 5B2A: complete - strict confirmed Tool outcomes, exact atomic projection, stable multi-resolution replay, safe explicit recovery, and zero repeated Tool side effects
Phase 5B2B: in progress - Workflow projection after confirmed Run outcomes
Phase 5B2B brief: .superpowers/sdd/M02-T002-phase5b2b-brief.md
Phase 5B2B implementation/review commits: 3bacf27, 069ec71, a5829c7
Phase 5B2B report: .superpowers/sdd/M02-T002-phase5b2b-report.md
Phase 5B2B review history: initial C0/I2/M0 and re-review C0/I1/M0 closed terminal-certification/node-CAS Run/Session/parent TOCTOU, cumulative Model-confirmed multi-resolution normalization, two-SDK follower convergence, and universal Session-exists node-transition regression
Phase 5B2B final gates: projection+admission 147 passed; Workflow recovery/admission/ownership 207 passed; Phase5 core 772 passed; adjacent 692 passed; full Python3.13 2031 passed with zero skips/failures; Ruff/mypy75/diff/import/signature/schema clean
Phase 5B2B final independent review: Approved; Spec C0/I0/M0 and Quality C0/I0/M0
Phase 5B2B: complete - explicit Workflow projection of certified terminal/interrupted Run outcomes, exact atomic node preconditions, stable cumulative decisions, Session lifecycle, and zero repeated side effects
Phase 5B whole review: in progress - fresh read-only review across Phase 5B1, 5B2A, and 5B2B
Phase 5B whole-review initial result: Not Approved; Spec C0/I4/M0 and Quality C0/I4/M0
Phase 5B whole-review fix brief: .superpowers/sdd/M02-T002-phase5b-whole-review-fix-brief.md
Phase 5B whole-review fix commits: d605adb, 74fcb16, 532839a, 9102544
Phase 5B whole-review fix report: .superpowers/sdd/M02-T002-phase5b-whole-review-fix-report.md
Phase 5B whole-review fixes: total bounded strict Tool evidence; exact shared READY_FOR_MODEL relation; cumulative terminal-decision normalization; atomic Workflow binding of complete checkpoint/operation/reconciliation/event recovery evidence on Memory and SQLite
Phase 5B final gates: full Python3.13 2147 passed with zero skips/failures; Ruff clean; mypy clean across 75 source files; 53 module imports; 103 unique root exports; exact public signatures; SQLite schema3; diff/scope clean
Phase 5B final independent whole re-review: Approved; Spec C0/I0/M0 and Quality C0/I0/M0; no Critical/Important/Minor findings
Phase 5B: complete - strict confirmed Model/Tool outcomes, stable closed-world multi-resolution replay, exact safe recovery, and certified atomic Workflow projection
Phase 5C: in progress - subprocess hard-exit E2E and dual-Python/package release gate
Phase 5C implementation/review commits: 64b8854, 70fb21d, 5cb53e7, b91c7c2, 3c7fe7c
Phase 5C report: .superpowers/sdd/M02-T002-phase5c-report.md
Phase 5C fault evidence: real SQLite subprocess os._exit boundaries for Provider acceptance, application Tool side effect, MCP session.call_tool, committed safe Tool outcome, and safe Workflow outcome; durable lease-expiry scanner clocks and cross-process side-effect counts are exact
Phase 5C release gates: full Python3.12 2153 and Python3.13 2153 passed with zero skips/failures; Ruff clean; mypy75; 53 modules; 103 root exports; exact public signatures; schema3/migration hashes; external sdist/wheel and dual-version clean installs; reference CLI --help opened no Store and invoked no model
Phase 5C initial review: Not Approved; Spec C0/I1/M1 and Quality C0/I1/M0 because safe Tool cases lacked a true handler-side cross-process effect marker and scanner time was not bound to the durable lease
Phase 5C review fixes: safe Tool/Workflow record distinct handler-side and post-commit markers; every subprocess scanner reads the actual durable lease and advances to expires_at+1us
Phase 5C final independent review: Approved; Spec C0/I0/M0 and Quality C0/I0/M0
Phase 5C: complete - real hard-exit recovery, exact no-default-replay proofs, lifecycle/Workflow E2E, and supported-package release certification
M02-T002 whole review initial result: Not Approved; Spec C0/I0/M0 and Quality C0/I2/M2
M02-T002 whole-review Important findings: default cross-SDK followers busy-polled the Store with sleep(0); recovery evidence materialized the entire database event log before filtering the target Session
M02-T002 whole-review fix brief/report: .superpowers/sdd/M02-T002-whole-review-fix-brief.md, .superpowers/sdd/M02-T002-whole-review-fix-report.md
M02-T002 whole-review fixes: f9971af adds one private 50ms default follower interval for Run and resolution followers; efd62b0 fixes recovery evidence to the target Session at one upper cursor and preserves fail-closed target evidence validation
M02-T002 final gates: exact I1/I2 10 passed; Workflow evidence/Session 52 passed; subprocess/scanner/MCP 60 passed; broad recovery matrix 1267 passed; full Python3.13 2159 passed with zero skips/failures; Ruff/mypy75/import/export/signature/schema/migration/diff/scope clean
M02-T002 final independent whole re-review: Approved; Spec C0/I0/M0 and Quality C0/I0/M2
M02-T002 retained nonblocking Minors: consolidate duplicated Memory/SQLite strict recovery validators during later storage maintenance; add direct signed-int64 type/subclass/MIN/MAX-focused automation without changing current semantics
M02-T002: complete - generation-fenced leases, durable checkpoints/external operations, conservative explicit recovery, immutable reconciliation decisions, certified Provider/Tool outcomes, exact Workflow projection, Session ownership, and real process-death proofs
M02-T003: in progress - artifact lifecycle and generalized migration checksums/coordinator
Next action: prepare and execute the M02-T003 Artifact Lifecycle and Migrations task plan; do not enter M02-T004 cancellation/control scope
M02-T003 operational plan: .superpowers/sdd/M02-T003-phase-plan.md
M02-T003 Phase A: in progress - checksum bootstrap, migration/open coordinator, and schema-generation write fence
M02-T003 Phase A brief: .superpowers/sdd/M02-T003-phaseA-brief.md
M02-T003 Phases B-D remain pending until Phase A independent Spec C0/I0 and Quality C0/I0 review
M02-T003 Phase A implementation: ac7f0ca
M02-T003 Phase A initial review: Not Approved; Spec C0/I4/M0 and Quality C0/I1/M0
M02-T003 Phase A review findings: coordinator hangs across threads/event loops; plan/applied lack one read snapshot; SQL normalization changes quoted literal semantics; migrations 1-3 share one transaction and v4 BEGIN cancellation lacks settlement; public open errors leak absolute paths
M02-T003 Phase A review-fix brief: .superpowers/sdd/M02-T003-phaseA-review-fix-brief.md
M02-T003 Phase A review-fix implementation: db7cd77
M02-T003 Phase A review-fix report: .superpowers/sdd/M02-T003-phaseA-review-fix-report.md
M02-T003 Phase A review fixes: loop-neutral per-database coordination; one explicit WAL-safe read snapshot; quoted-token-preserving SQL lexer; one cancellation-safe transaction per migration; stable sanitized public filesystem/open/resource errors
M02-T003 Phase A review-fix gates: focused 144 passed; storage 593 passed; full Python3.13 2303 passed; Ruff clean; mypy76 and focused2 clean; py_compile/build/import/wheel resources/diff/scope clean
M02-T003 Phase A first re-review: Not Approved; Spec C0/I2/M0 and Quality C0/I3/M0
M02-T003 Phase A first re-review findings: legacy cross-process peers fail when a competitor reaches v4; SQL lexer merges blob/numeric/NBSP token boundaries; post-WAL busy exhaustion leaks RuntimeError; corrupt apply/open is misclassified as I/O; non-OSError resource backend failures leak
M02-T003 Phase A second-fix brief: .superpowers/sdd/M02-T003-phaseA-rereview-fix-brief.md
M02-T003 Phase A second-fix implementation: f6e6b8f
M02-T003 Phase A second fixes: trusted cross-process v4 convergence; complete SQLite lexical DDL comparison; typed busy exhaustion and exact public I/O mapping; numeric NOTADB/CORRUPT schema classification; sanitized ordinary resource backend boundaries
M02-T003 Phase A second-fix gates: focused 43 passed; migration/review 233 passed; storage 633 passed; full Python3.13 2343 passed; Ruff clean; strict mypy77 clean; py_compile/build/import/isolated-wheel resources/diff/scope clean
M02-T003 Phase A second re-review: Not Approved; Spec C0/I1/M0 and Quality C0/I1/M0
M02-T003 Phase A second re-review findings: SQLite Tcl-style $ parameter suffixes are not longest-matched and collide with whitespace-split SQL; plan/applied inspection-time numeric SQLite I/O is misclassified as schema corruption
M02-T003 Phase A third-fix brief: .superpowers/sdd/M02-T003-phaseA-second-rereview-fix-brief.md
M02-T003 Phase A third-fix implementation: 3bceab5
M02-T003 Phase A third fixes: complete SQLite Tcl-style $ variable longest matching; consistent numeric inspection-time IOERR/BUSY/LOCKED public I/O classification
M02-T003 Phase A third-fix gates: exact 22 passed; lexical/boundary 74 passed; migration/review 255 passed; storage 655 passed; full Python3.13 2365 passed; Ruff clean; strict mypy77 clean; py_compile/build/import/isolated-wheel resources/diff/scope clean
M02-T003 Phase A third re-review: Not Approved; Spec C0/I1/M0 and Quality C0/I1/M0
M02-T003 Phase A third re-review finding: real SQLite accepts leading-empty Tcl variable `$::foo`, but the lexer requires a non-empty initial identifier segment and the GREEN matrix omitted the boundary
M02-T003 Phase A fourth-fix brief: .superpowers/sdd/M02-T003-phaseA-leading-empty-variable-fix-brief.md
M02-T003 Phase A fourth fix: paused at a safe checkpoint; Phase B remains blocked
M02-T003 Phase A fourth-fix checkpoint: leading-empty Tcl variable code/tests implemented; exact16, Tcl32, lexer/schema/public-boundary90, and migration/v3/review271 passed; root re-ran Tcl32 passed; diff-check clean
M02-T003 Phase A fourth-fix pending gates: complete storage, full Python3.13, Ruff, strict mypy, py_compile, build/wheel/import/resources/scope; then append report, commit final evidence, and run a fresh independent C0/I0 review
Next action on resume: read M02-T003-phaseA-leading-empty-variable-fix-brief.md, verify checkpoint commit, start with complete tests/integration/storage; do not redo the completed RED/GREEN or enter Phase B

v0.1 release convergence decision (2026-07-17): written specification approved; detailed implementation planning complete
v0.1 design: docs/superpowers/specs/2026-07-17-agent-sdk-v0.1-release-design.md
v0.1 implementation index: docs/superpowers/plans/2026-07-17-agent-sdk-v0.1-implementation-index.md
v0.1 executable plans: R0 release harness; R1 built-in Tools/policy; R2 Workflow control; R3 automatic Context; R4 Child mailbox/tools; R5 Trace attribution/release
v0.1 goal: release a usable functional closed loop before further production-grade hardening
v0.1 recovery contract: resume from the last committed safe boundary; unknown in-flight Model/Tool work becomes interrupted and is never automatically replayed
v0.1 required slices: R0 scope reset/release harness; R1 built-in read/write/bash and basic policy; R2 Workflow conditions/bounded loops; R3 automatic L0-L4 Context; R4 spawn/message/wait/list Child tools and mailbox; R5 Trace attribution/package/release
v0.1 current implementation status: R0-R4 completed; R5 pending. R4 checkpoint is recorded with one known pre-R4 recovery debt; its raw aggregate is not PASS.
v0.1 M02-T003 decision: freeze after the committed Phase A focused checkpoint; absorb its pending full storage/project/build gates into the one release-candidate gate
v0.1 deferred work: M02-T003 Artifact Phases B-D, M02-T004 advanced controls/sync, multi-worker exact recovery, complex Workflow scheduling, advanced Child scheduling, vector retrieval, advanced analytics/exporters, compatibility/performance/conformance hardening
v0.1 R0 Task 1: complete (commits 0edb3c9 and 723f118; review Spec approved / Quality approved; fresh 2 tests passed and Ruff clean)
v0.1 R0 plan ordering correction: 83dce26
v0.1 R0 Task 2: complete (commits 908fe01 and 1a59ad2; review Spec approved / Quality approved; fresh 3 tests passed and Ruff clean)
v0.1 R0 checkpoint: complete (2026-07-17; commit: 74c1e3b)
v0.1 R0 checkpoint exact fresh evidence:
```text
$ .\.venv\Scripts\python.exe -m pytest tests\docs\test_v01_release_ledger.py tests\e2e\test_v01_release.py tests\e2e\test_vertical_slice.py -q
....                                                                     [100%]
4 passed in 4.74s

$ .\.venv\Scripts\python.exe -m ruff check tests\fixtures\v01_runtime.py tests\e2e\test_v01_release.py tests\docs\test_v01_release_ledger.py
All checks passed!
```
v0.1 R0 Task 3: complete (commits 7036622 and 74c1e3b; review Spec approved / Quality approved; exact checkpoint evidence retained)
v0.1 R0 final hardening: 35ace95; background non-replay assertion, deterministic R1 resume handoff, immutable checkpoint evidence, and release-ledger contract checks
v0.1 R0 final independent review: Approved; Critical 0 / Important 0 / Minor 0; fresh 4 tests passed and Ruff clean; ready to proceed to R1
v0.1 R1 Task 1: complete (commits 8fc36ad and 8c2982b; final review Spec approved / Quality approved; fresh 61 focused tests and 127 regression tests passed; strict mypy and Ruff clean)
v0.1 R1 Task 2: complete (commits e6d9f3b and 2b145a7; final review Spec approved / Quality approved; fresh 60 focused tests with 1 platform skip and 147 recovery tests passed; strict mypy and Ruff clean)
v0.1 R1 Task 3: complete (commits e8ce3db, 8fb3836, and cd82a6f; final review Spec approved / Quality approved; workspace-scoped built-in Tools, canonical permission resources, and isolated recovery capabilities)
v0.1 R1 checkpoint: complete (2026-07-17; final hardening through 2f0e922)
v0.1 R1 initial checkpoint historical evidence:
```text
$ .\.venv\Scripts\python.exe -m pytest tests/unit/permissions/test_policy_rules.py tests/unit/tools/test_workspace_paths.py tests/integration/tools/test_builtin_tools.py tests/integration/tools/test_permissioned_tool_slice.py tests/e2e/test_v01_release.py -q
..............s......................................................... [ 83%]
..............                                                           [100%]
85 passed, 1 skipped in 6.12s

$ .\.venv\Scripts\python.exe -m ruff check src/agent_sdk/config.py src/agent_sdk/permissions src/agent_sdk/tools tests/unit/permissions tests/unit/tools tests/integration/tools
All checks passed!

$ .\.venv\Scripts\python.exe -m mypy --strict src/agent_sdk/config.py src/agent_sdk/permissions src/agent_sdk/tools
Success: no issues found in 16 source files
```
v0.1 R1 final hardening: commits d4cd336 and 2f0e922; canonical built-in permission-resource binding, exact durable recovery, and repeatable pending-permission recovery
v0.1 R1 final independent review: Approved; Critical 0 / Important 0 / Minor 0; Ready to proceed to R2: Yes
v0.1 R1 final checkpoint exact fresh evidence:
```text
$ .\.venv\Scripts\python.exe -m pytest tests\unit\permissions\test_policy_rules.py tests\unit\tools\test_workspace_paths.py tests\unit\runtime\test_session_workspace_roots.py tests\integration\tools\test_builtin_tools.py tests\integration\tools\test_permissioned_tool_slice.py tests\integration\runtime\test_builtin_tool_recovery.py tests\e2e\test_v01_release.py -q
..............s............ss........................................... [ 72%]
............................                                             [100%]
97 passed, 3 skipped in 7.94s

$ .\.venv\Scripts\python.exe -m ruff check src\agent_sdk tests\unit\permissions tests\unit\tools tests\unit\runtime\test_session_workspace_roots.py tests\integration\tools tests\integration\runtime\test_builtin_tool_recovery.py tests\e2e\test_v01_release.py
All checks passed!

$ .\.venv\Scripts\python.exe -m mypy --strict src\agent_sdk
Success: no issues found in 84 source files
```
v0.1 R2 Task 1: complete (commits e3494ae and 1fc9c72; final review Spec approved / Quality approved; fresh 82 focused and schema-v1 compiler tests passed; strict mypy and Ruff clean)
v0.1 R2 Task 2: complete (commits 9b23e5a and cfdf43a; final review Spec approved / Quality approved; fresh 120 unit/descriptor and 253 Workflow integration tests passed; strict mypy and Ruff clean)
v0.1 R2 Task 3: complete (commits e4624f7 and 36a7268; final review Spec approved / Quality approved; fresh 19 focused and 370 Workflow unit/integration tests passed; strict mypy and Ruff clean)
v0.1 R2 Task 4: complete (commit 04d8ee2; final review Spec approved / Quality approved; historical independent 380 Workflow unit/integration and v0.1 E2E tests passed; strict mypy and Ruff clean)
v0.1 R2 implementation checkpoint: `56d60a8` (2026-07-17)
Historical R2 pre-final-hardening checkpoint evidence:
```text
$ .\.venv\Scripts\python.exe -m pytest tests\unit\workflow tests\integration\workflow tests\e2e\test_v01_release.py -q
........................................................................ [ 18%]
........................................................................ [ 37%]
........................................................................ [ 56%]
........................................................................ [ 75%]
........................................................................ [ 94%]
....................                                                     [100%]
380 passed in 44.02s

$ .\.venv\Scripts\python.exe -m ruff check src\agent_sdk\workflow src\agent_sdk\runtime\execution.py tests\unit\workflow tests\integration\workflow
All checks passed!

$ .\.venv\Scripts\python.exe -m mypy --strict src\agent_sdk\workflow src\agent_sdk\runtime\execution.py
Success: no issues found in 10 source files
```
v0.1 R2 final hardening: `852692f fix: bind child to executed workflow parent`; `309d63c fix: preserve child parent execution identity`
v0.1 R2 final independent re-review: Critical 0 / Important 0 / Minor 0; Spec compliance PASS; Code quality PASS; Ready to proceed to R3: Yes
Current canonical R2 final checkpoint evidence:
```text
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'
$ .\.venv\Scripts\python.exe -m pytest -p pytest_asyncio.plugin tests\unit\workflow tests\integration\workflow tests\e2e\test_v01_release.py -q
403 passed in 43.03s

$ .\.venv\Scripts\python.exe -m pytest -p pytest_asyncio.plugin tests\integration\workflow\test_control_child_parent.py tests\integration\workflow\test_control_recovery.py tests\integration\workflow\test_control_state.py tests\unit\workflow\test_control_compiler.py -q
47 passed in 7.31s

$ .\.venv\Scripts\python.exe -m ruff check src\agent_sdk\workflow tests\unit\workflow tests\integration\workflow tests\e2e\test_v01_release.py
All checks passed!

$ .\.venv\Scripts\python.exe -m mypy --strict src\agent_sdk\workflow src\agent_sdk\runtime\execution.py
Success: no issues found in 10 source files

$ git diff --check 56d60a8..309d63c
clean
```
v0.1 active next plan: docs/superpowers/plans/2026-07-17-agent-sdk-v0.1-r4-child-mailbox.md
v0.1 resume command: `$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; .\.venv\Scripts\python.exe -m pytest -p pytest_asyncio.plugin tests\unit\runtime\test_capability_intersection.py -q`
v0.1 R4 Task 1: complete (implementation/hardening commits `23c2c39`, `5466757`, `fd6d728`, `f320642`; final independent review Spec approved / Quality approved; Critical 0 / Important 0 / Minor 0)
v0.1 R4 Task 1 delivered persisted per-Run Tool/workspace capabilities, canonical inheritance/intersection with explicit-empty semantics, descriptor-selected Tool catalogs across execution/recovery, authenticated workspace boundary enforcement, schema-v3 provenance, and schema-v3 public execution-tree support.
v0.1 R4 Task 1 final gates: 184 passed, 5 skipped across capability/runtime/workspace/builtin/prompt slices; observability 67 passed; strict mypy clean across 93 source files; Ruff and diff-check clean.
v0.1 current implementation status: R0-R3 and R4 Task 1 completed; R4 Task 2 pending.
v0.1 resume command: `$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; .\.venv\Scripts\python.exe -m pytest -p pytest_asyncio.plugin tests\unit\subagents\test_mailbox.py -q`
v0.1 R4 Task 2: complete (implementation `ae6aa89`; review hardening `1326fb8`; final independent review Spec approved / Quality approved; Critical 0 / Important 0 / Minor 0)
v0.1 R4 Task 2 delivered a durable direct parent/child mailbox, recipient-local immutable sequences, bounded optimistic concurrency and idempotent replay, Memory/SQLite legacy-Run exact authentication, sender-attributed protected Context sources, and atomic L0-L4 Context View/cursor consumption.
v0.1 R4 Task 2 final gates: 39 focused passed; 48 expanded subagent passed; Context 144 passed with one proven baseline recovery node deselected; Task 1 smoke 184 passed, 5 skipped; strict mypy clean across 94 source files; Ruff and diff-check clean.
v0.1 current implementation status: R0-R3 and R4 Tasks 1-2 completed; R4 Task 3 pending.
v0.1 resume command: `$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; .\.venv\Scripts\python.exe -m pytest -p pytest_asyncio.plugin tests\integration\subagents\test_child_coordinator.py -q`
v0.1 R4 Task 3: complete (implementation `b90c8f7`; review hardening `3e71c9a`; final independent review Spec approved / Quality approved; Critical 0 / Important 0 / Minor 0)
v0.1 R4 Task 3 delivered shared API/Workflow Child coordination, durable depth/per-parent/per-Session limits, process-local queued concurrency, authoritative ancestor capability intersection with atomic raw preconditions, public `sdk.children` spawn/send/wait/list, SQLite-reopen progress, and bounded non-cancelling recovery waits.
v0.1 R4 Task 3 final gates: 42 focused passed; 353 broad subagent/Workflow passed; Task 1/2 smoke 79 passed; strict mypy clean across 96 source files; Ruff and diff-check clean.
v0.1 current implementation status: R0-R3 and R4 Tasks 1-3 completed; R4 Task 4 pending.
v0.1 resume command: `$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; .\.venv\Scripts\python.exe -m pytest -p pytest_asyncio.plugin tests\integration\subagents\test_child_tools.py -q`
v0.1 R4 Task 4: complete (implementation `c1e570b`; independent review Spec approved / Quality approved; Critical 0 / Important 0 / Minor 0)
v0.1 R4 Task 4 delivered `spawn_agent`, `send_message`, `wait_child`, and `list_children` through the normal Tool/permission/trace pipeline, ToolContext-derived identity, shared Coordinator/Mailbox handlers, builtin registration/collision behavior, and a deterministic bidirectional parent/Child v0.1 E2E.
v0.1 R4 Task 4 final gates: 17 focused passed; Workflow 274 passed; Task 1-3 smoke 121 passed; broad subagent/Context/v0.1 gate 198 passed with one proven baseline recovery node deselected; strict mypy clean across 97 source files; Ruff and diff-check clean.
v0.1 current implementation status: R0-R4 completed; R5 pending. R4 Task 5 checkpoint is complete with the known pre-R4 recovery debt retained, so the raw aggregate is not PASS.
v0.1 R4 checkpoint recovery command: `$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; .\.venv\Scripts\python.exe -m pytest -p pytest_asyncio.plugin tests\unit\subagents tests\integration\subagents tests\integration\context tests\e2e\test_v01_release.py -q --deselect=tests/integration/context/test_context_recovery.py::test_authoritative_recovery_receives_exact_stored_prepared_request`
v0.1 R3 Task 1 deterministic L0-L2 is complete (commits 2bda910, ba9d05d, and ead396b; began with `tests/unit/context/test_deterministic_strategies.py`)
v0.1 R3 Task 1 final review: Critical 0 / Important 0 / Minor 0; Spec PASS; Quality PASS
v0.1 R3 Task 1 controller gates: 42 deterministic strategy tests; 48 context integration tests; Ruff clean; strict mypy clean across 4 files; diff-check clean
v0.1 R3 Task 2: complete (automatic L0-L4 recommendation/application; `allow_lossy=False` caps L3/L4 at exact L2; distinct LiteLLM L3 summary and L4 rebase with purpose `context_compaction`; same-Session recursive evidence; atomic Context View/capsule/event persistence)
v0.1 R3 Task 2 fallback contract: invalid, timeout, schema, reference, input-bound, or output-budget L3/L4 results use the exact deterministic L2 renderer without failing the main Run
v0.1 R3 Task 2 final safety fix: `c3dc154`; final independent re-review: `3d8458e`, Critical 0 / Important 0 / Minor 0; Spec PASS; Quality PASS
v0.1 R3 Task 2 fresh gates: Context 102 passed; Ruff clean; strict mypy clean; diff-check clean
v0.1 R3 Task 3: complete (implementation `9fbcd16`; final approval `2bd48e3`; Critical 0 / Important 0 / Minor 0; Spec PASS; Quality PASS)
v0.1 R3 Task 3 delivered durable `AgentSpec`/`DurableAgentSpec` prompt and Context fields; public `SkillRegistry` exposure with one shared direct/Workflow/subagent preflight; ordered default/application/Skill prompt layers with persisted manifest; redacted public `run.created` schema v2; and authenticated genuine R2 schema-v1 recovery compatibility.
v0.1 R3 Task 3 effective evidence: controller mainline 201 passed; implementer gate 521 passed, 1 skipped; Workflow/recovery/release gate 25 passed; Ruff clean; strict mypy clean across 92 source files.
v0.1 R3 Task 4: complete (implementation `2ea0464`; recovery-evidence fix `3a4b65f`; final approval `b98e93f`; Critical 0 / Important 0 / Minor 0; Spec PASS; Quality PASS)
v0.1 R3 Task 4 final approval: Critical 0 / Important 0 / Minor 0; Spec PASS; Quality PASS
v0.1 R3 Task 4 delivered ContextMiddleware preparation before each new model call, durable exact prepared requests, authenticated Context View/Prompt Manifest bindings, strict provider request validation, and no-side-effect failure for corrupted recovery evidence.
v0.1 R3 checkpoint: complete (2026-07-20; Tasks 1-4 approved)
v0.1 R3 checkpoint fresh evidence: 221 passed, 1 skipped in 25.32s across unit/context, integration/context, integration/prompts, reconciliation models, and v0.1 E2E; Ruff clean; strict mypy clean across 93 source files.
v0.1 active next plan: docs/superpowers/plans/2026-07-17-agent-sdk-v0.1-r5-trace-release.md
v0.1 R5 first expected RED: `tests/unit/observability/test_stage_projection.py` does not yet exist because R5 Task 1 creates it. It is expected RED work, not an existing failure.
v0.1 resume command: `$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; .\.venv\Scripts\python.exe -m pytest -p pytest_asyncio.plugin tests\unit\observability\test_stage_projection.py -q`
v0.1 R4 checkpoint (2026-07-21): complete with scope confined to Child Tool/mailbox exchange. R4 Tasks 1-4 are independently approved: capability persistence/catalog selection (`23c2c39`, `5466757`, `fd6d728`, `f320642`); direct durable mailbox plus atomic Context consumption (`ae6aa89`, `1326fb8`); bounded Child coordination and public API (`b90c8f7`, `3e71c9a`); and ordinary Tool-pipeline child controls (`c1e570b`). Each final review reports Critical 0 / Important 0 / Minor 0, Spec approved, and Quality approved.
v0.1 R4 checkpoint fresh raw gate: `tests/unit/subagents tests/integration/subagents tests/integration/context tests/e2e/test_v01_release.py -q` with `.venv`, `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`, and explicit `pytest_asyncio.plugin` produced 198 passed / 1 failed in 14.05s. The sole failure is the known pre-R4 recovery debt `tests/integration/context/test_context_recovery.py::test_authoritative_recovery_receives_exact_stored_prepared_request` (`AgentSDKError: recovery required`), previously proven with the same shape at Tasks 2 and 4. Raw R4 checkpoint status is therefore NOT PASS; the node was not changed or repaired.
v0.1 R4 clean gate: the exact-node `--deselect` command above produced 198 passed / 1 deselected in 13.33s. Ruff plan scope passed; strict mypy passed for the 31 planned source files and all 97 `src/agent_sdk` files. The deselected clean gate does not turn the raw aggregate into a PASS.
v0.1 R4 Task 5 final ledger gate: corrected the stale R4-pending contract and the R5 Task 1 resume target; `tests/docs/test_v01_release_ledger.py` produced 3 passed in 0.04s, Ruff clean, diff-check clean.
v0.1 R4 final independent review: APPROVE for `4eb84b5..0071094`; Critical 0 / Important 0 / Minor 0; Spec PASS; Code Quality PASS. Final controller gate: 201 passed / 1 known-debt node deselected; Ruff clean; strict mypy clean across 97 source files; whole-range diff-check clean. R5 remains pending.
v0.1 R5 Task 1: complete (commits `5673ae3`, `a3a2ebb`, `0b1793b`; final independent review Critical 0 / Important 0 / Minor 0; Spec approved / Quality approved).
v0.1 R5 Task 1 delivered normalized sanitized Trace stages, stable Run/Workflow timelines, public `sdk.trace.timeline/subscribe`, finite provider cost capture, authenticated Child/Tool/permission correlations, and strict schema-v1/v2 recovery compatibility.
v0.1 R5 Task 1 final controller gate: 85 passed across public observability and real recovery-permission timeline; strict mypy clean across 100 source files; Ruff and diff-check clean.
v0.1 current implementation status: R0-R4 and R5 Task 1 completed; R5 Task 2 pending.
v0.1 resume command: `$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; $env:PYTHONPATH='src'; .\.venv\Scripts\python.exe -m pytest -p pytest_asyncio.plugin tests\unit\observability\test_attribution.py -q`
