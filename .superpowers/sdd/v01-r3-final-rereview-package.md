# Review package: e0b34c3..HEAD

## Commits
2f9b142 fix: close R3 final review findings

## Files changed
 .superpowers/sdd/v01-r3-final-fix-report.md        |  94 ++++++++++++
 .superpowers/sdd/v01-r3-task4-rereview.md          |   1 -
 .superpowers/sdd/v01-r3-task4-review.md            |   1 -
 .../2026-07-17-agent-sdk-v0.1-r3-auto-context.md   |   9 +-
 src/agent_sdk/context/compactor.py                 |   5 +
 src/agent_sdk/context/planner.py                   |   1 +
 src/agent_sdk/prompts/persistence.py               |   1 +
 src/agent_sdk/runtime/recovery.py                  | 162 +++++++++++++++++++++
 tests/docs/test_v01_release_ledger.py              |  16 ++
 tests/integration/context/test_compaction_slice.py |  29 +++-
 .../integration/context/test_context_compaction.py |  47 ++++++
 tests/integration/context/test_context_recovery.py |  81 ++++++++++-
 tests/integration/prompts/test_runtime_prompt.py   |  14 +-
 13 files changed, 445 insertions(+), 16 deletions(-)

## Diff
diff --git a/.superpowers/sdd/v01-r3-final-fix-report.md b/.superpowers/sdd/v01-r3-final-fix-report.md
new file mode 100644
index 0000000..ae632fa
--- /dev/null
+++ b/.superpowers/sdd/v01-r3-final-fix-report.md
@@ -0,0 +1,94 @@
+# v0.1 R3 Final Review Fix Report
+
+## Scope
+
+This change closes every finding in the independent R3 final review at
+`e0b34c3`: I1, I2, M1, and M2. It does not start R4.
+
+## I1 - Safe first-use L4
+
+Root cause: `ContextCompactor.rebase()` accepted an empty prior-capsule set, so
+both automatic and forced first-use L4 could persist an incomplete L4 capsule
+whose recursive lineage omitted older sources.
+
+The minimum safe invariant is now enforced at the compactor boundary: L4 with
+no validated prior capsule returns a no-usage compaction failure without a
+LiteLLM call. The existing planner path persists the deterministic L2 result
+with `fallback_from=L4`. L4 with a real prior capsule is unchanged and must cite
+that capsule before it can persist successfully.
+
+TDD evidence:
+
+- RED: automatic and forced first-use L4 both persisted `applied_level=L4`;
+  `2 failed`.
+- GREEN: automatic/forced first-use fallback plus existing-prior rebase;
+  `3 passed`.
+
+The SQLite recursive-retrieval scenario now creates an L3 capsule before L4
+and proves the final capsule recursively resolves the complete original source
+order.
+
+## I2 - Complete prepared-attribution authentication
+
+Root cause: recovery authenticated snapshot identity, ownership, links, and
+read/commit stability, but did not prove the snapshot attribution was the
+projection originally recorded by immutable creation events or derivable from
+the exact prepared request.
+
+Prepared recovery now fails closed before provider, Tool, reconciliation,
+resend, or terminal-certification side effects unless all of the following
+hold:
+
+- exactly one same-Session `context.view.created` and
+  `prompt.manifest.created` creation event exists for the referenced ids;
+- both event payloads are strict closed projections of the complete snapshots;
+- Context View level, fallback, capsule, token estimate, budget, message/source
+  refs, transformations, consumed message ids, and compaction usage are valid;
+- Prompt Manifest id/view/model/tool hash, aggregate hash, and ordered
+  layer id/version/hash tuples exactly match its creation event;
+- the exact prepared request begins with the ordered system layers, every
+  system-layer content hash and the aggregate prompt hash match, and canonical
+  request Tool schemas reproduce `tools_sha256`;
+- exact snapshot and creation-event identity preconditions still hold at the
+  no-write authentication commit.
+
+`prompt.manifest.created` now includes layer versions and
+`context.view.created` includes consumed message ids, so the creation payloads
+are complete rather than silently accepting older incomplete prepared
+evidence. Legacy operations with no prepared references retain their existing
+compatibility path; prepared operations without complete evidence fail closed.
+
+TDD evidence:
+
+- RED: the original 14 missing/owner/id/link cases passed, while all 14 new
+  valid-but-altered cases failed to raise (`7 attribution mutations x Memory /
+  SQLite`).
+- GREEN: the expanded matrix covers 17 corruption classes on both backends;
+  `34 passed`, with zero provider and Tool calls on every rejection.
+- Covered mutations include Manifest tool/aggregate/layer hashes, layer
+  version/order, and View level/refs/transformations/consumed ids/budget.
+
+## M1 and M2
+
+- Removed the extra EOF blank lines from both R3 Task 4 review artifacts.
+- Corrected the historical R3 plan handoff: R4 Task 1 starts at
+  `tests/unit/runtime/test_capability_intersection.py` with an expected RED;
+  mailbox work is R4 Task 2.
+- Docs-contract TDD: the new handoff assertion failed before the plan fix and
+  the complete docs suite now passes (`3 passed`).
+
+## Fresh verification
+
+- Context and Prompt suites: `175 passed, 1 skipped in 11.58s`.
+- R3 representative combination (Context, Prompt, reconciliation models,
+  release E2E, docs): `246 passed, 1 skipped in 12.47s`.
+- Provider, Tool, built-in Tool, and text-loop recovery representative suite:
+  `276 passed in 39.82s`.
+- The single skip is the existing optional tokenizer-backend case.
+- Ruff over all changed source/tests: clean.
+- Strict mypy over all 93 source files: clean.
+- Whole-R3 `git diff --check aa2d410..HEAD`: clean after M1.
+
+The known non-R3 `tests/integration/runtime/test_recovery_api.py` fixture /
+built-in-capability mismatch remains release-candidate debt and was not
+weakened or broadened by this fix.
diff --git a/.superpowers/sdd/v01-r3-task4-rereview.md b/.superpowers/sdd/v01-r3-task4-rereview.md
index ca14073..1a3ce2b 100644
--- a/.superpowers/sdd/v01-r3-task4-rereview.md
+++ b/.superpowers/sdd/v01-r3-task4-rereview.md
@@ -121,11 +121,10 @@ Strict mypy:
 Success: no issues found in 93 source files

 git diff --check:
 clean

 worktree before review artifact:
 clean
 ```

 The single skip is the existing optional tokenizer-backend test.
-
diff --git a/.superpowers/sdd/v01-r3-task4-review.md b/.superpowers/sdd/v01-r3-task4-review.md
index 223298f..720d2da 100644
--- a/.superpowers/sdd/v01-r3-task4-review.md
+++ b/.superpowers/sdd/v01-r3-task4-review.md
@@ -177,11 +177,10 @@ Success: no issues found in 22 source files
 git diff --check:
 clean
 ```

 The sampled `tests/integration/runtime/test_recovery_api.py` still has the
 pre-existing built-in-Tool capability mismatch described in the Task 4 report
 (`5 passed` before the first three failures under `--maxfail=3`). The Task 4
 diff does not change the capability gate that raises those failures, so it is
 not counted as a Task 4 finding. It remains a project-level release-suite debt
 that Task 5 must not silently present as a fully green repository.
-
diff --git a/docs/superpowers/plans/2026-07-17-agent-sdk-v0.1-r3-auto-context.md b/docs/superpowers/plans/2026-07-17-agent-sdk-v0.1-r3-auto-context.md
index b40c456..6f96ed4 100644
--- a/docs/superpowers/plans/2026-07-17-agent-sdk-v0.1-r3-auto-context.md
+++ b/docs/superpowers/plans/2026-07-17-agent-sdk-v0.1-r3-auto-context.md
@@ -626,22 +626,27 @@ git commit -m "feat: apply durable context to every model call"
 ```powershell
 uv run pytest tests/unit/context tests/unit/prompts tests/integration/context tests/integration/prompts tests/e2e/test_v01_release.py -q
 uv run ruff check src/agent_sdk/context src/agent_sdk/prompts src/agent_sdk/runtime tests/unit/context tests/integration/context
 uv run mypy --strict src/agent_sdk/context src/agent_sdk/prompts src/agent_sdk/runtime
 ```

 Expected: PASS.

 - [ ] **Step 2: Record R3 and R4 resume command**

-Set next command:
+Set the R4 Task 1 resume command for
+`tests/unit/runtime/test_capability_intersection.py`. This test is created by
+R4 Task 1; the first expected RED confirms the capability-intersection boundary:

 ```powershell
-uv run pytest tests/unit/subagents/test_mailbox.py -q
+$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'; .\.venv\Scripts\python.exe -m pytest -p pytest_asyncio.plugin tests\unit\runtime\test_capability_intersection.py -q
 ```

+R4 Task 2 adds `tests/unit/subagents/test_mailbox.py`; it is not the first R4
+resume boundary.
+
 - [ ] **Step 3: Commit**

 ```powershell
 git add docs/plans/releases/v0.1.md .superpowers/sdd/progress.md
 git commit -m "docs: record v0.1 R3 checkpoint"
 ```
diff --git a/src/agent_sdk/context/compactor.py b/src/agent_sdk/context/compactor.py
index 2d0835f..fbc1e51 100644
--- a/src/agent_sdk/context/compactor.py
+++ b/src/agent_sdk/context/compactor.py
@@ -70,20 +70,25 @@ class ContextCompactor:
             )

     async def rebase(
         self,
         capsules: tuple[ContextCapsule, ...],
         source: tuple[ContextItem, ...],
         protected: set[str],
         *,
         capsule_ids: tuple[str, ...] = (),
     ) -> CompactionResult:
+        if not capsules:
+            return CompactionResult(
+                capsule=None,
+                usage=UsageReported(None, None, None),
+            )
         try:
             if capsule_ids and len(capsule_ids) != len(capsules):
                 raise ValueError("capsule ids must correspond to capsules")
             retained_source = tuple(
                 item for item in source if item.event_id in protected
             )
             prior_source_refs = {
                 ref for capsule in capsules for ref in capsule.source_event_ids
             }
             prior_refs = set(capsule_ids) if capsule_ids else prior_source_refs
diff --git a/src/agent_sdk/context/planner.py b/src/agent_sdk/context/planner.py
index 38d0310..85465bc 100644
--- a/src/agent_sdk/context/planner.py
+++ b/src/agent_sdk/context/planner.py
@@ -957,15 +957,16 @@ class ContextPlanner:
                 ),
                 "estimated_tokens": view.estimated_tokens,
                 "budget": (
                     view.budget.model_dump(mode="json")
                     if view.budget is not None
                     else None
                 ),
                 "message_refs": list(view.message_refs),
                 "source_refs": list(view.source_refs),
                 "transformations": list(view.transformations),
+                "consumed_message_ids": list(view.consumed_message_ids),
                 "compaction_usage": (
                     usage.to_payload() if usage is not None else None
                 ),
             },
         )
diff --git a/src/agent_sdk/prompts/persistence.py b/src/agent_sdk/prompts/persistence.py
index 7e6c32e..c6b3425 100644
--- a/src/agent_sdk/prompts/persistence.py
+++ b/src/agent_sdk/prompts/persistence.py
@@ -29,20 +29,21 @@ class PromptManifestPersistence:
             sequence=1,
             payload={
                 "manifest_id": manifest.manifest_id,
                 "context_view_id": manifest.context_view_id,
                 "sha256": manifest.sha256,
                 "model": manifest.model,
                 "tools_sha256": manifest.tools_sha256,
                 "layers": [
                     {
                         "layer_id": layer.layer_id,
+                        "version": layer.version,
                         "sha256": layer.sha256,
                     }
                     for layer in manifest.layers
                 ],
             },
         )
         try:
             await self._store.commit(
                 CommitBatch(
                     events=(event,),
diff --git a/src/agent_sdk/runtime/recovery.py b/src/agent_sdk/runtime/recovery.py
index 75858a8..9d1678d 100644
--- a/src/agent_sdk/runtime/recovery.py
+++ b/src/agent_sdk/runtime/recovery.py
@@ -1,13 +1,14 @@
 from __future__ import annotations

 import asyncio
+import hashlib
 import json
 import math
 import os
 import sys
 from collections.abc import Awaitable, Callable, Mapping
 from dataclasses import dataclass, replace
 from datetime import UTC, datetime, timedelta
 from time import monotonic
 from pathlib import Path
 from typing import Any, Literal
@@ -4652,44 +4653,205 @@ class RunRecoveryService:
             view = ContextView.model_validate(raw_view)
             manifest = PromptManifest.model_validate(raw_manifest)
             if (
                 view.view_id != context_view_id
                 or view.session_id != session_id
                 or manifest.manifest_id != prompt_manifest_id
                 or manifest.context_view_id != context_view_id
                 or manifest.model != operation.provider_identity
             ):
                 raise ValueError("prepared reference identity mismatch")
+            events = await self._store.read_events(
+                after_cursor=0,
+                session_id=session_id,
+            )
+            view_events = tuple(
+                stored
+                for stored in events
+                if stored.event.type == "context.view.created"
+                and stored.event.run_id == context_view_id
+            )
+            manifest_events = tuple(
+                stored
+                for stored in events
+                if stored.event.type == "prompt.manifest.created"
+                and stored.event.run_id == prompt_manifest_id
+            )
+            if len(view_events) != 1 or len(manifest_events) != 1:
+                raise ValueError("prepared creation evidence is missing")
+            view_stored = view_events[0]
+            manifest_stored = manifest_events[0]
+            self._authenticate_context_view_event(view, view_stored.event)
+            self._authenticate_prompt_manifest_event(
+                operation,
+                manifest,
+                manifest_stored.event,
+            )
             await self._store.commit(
                 CommitBatch(
                     events=(),
                     preconditions=(
                         SnapshotPrecondition(
                             "context_view",
                             context_view_id,
                             session_id=session_id,
                             data=view.model_dump(mode="json"),
                         ),
                         SnapshotPrecondition(
                             "prompt_manifest",
                             prompt_manifest_id,
                             session_id=session_id,
                             data=manifest.model_dump(mode="json"),
                         ),
                     ),
+                    event_preconditions=(
+                        EventPrecondition(
+                            view_stored.event.event_id,
+                            view_stored.cursor,
+                            view_stored.event.session_id,
+                            view_stored.event.run_id,
+                            view_stored.event.type,
+                            view_stored.event.sequence,
+                        ),
+                        EventPrecondition(
+                            manifest_stored.event.event_id,
+                            manifest_stored.cursor,
+                            manifest_stored.event.session_id,
+                            manifest_stored.event.run_id,
+                            manifest_stored.event.type,
+                            manifest_stored.event.sequence,
+                        ),
+                    ),
                 )
             )
         except RecoveryStateConflictError:
             raise
         except (AgentSDKError, SnapshotPreconditionError, TypeError, ValueError):
             raise RecoveryStateConflictError from None

+    @staticmethod
+    def _authenticate_context_view_event(
+        view: ContextView,
+        event: EventEnvelope,
+    ) -> None:
+        if (
+            event.session_id != view.session_id
+            or event.run_id != view.view_id
+            or event.type != "context.view.created"
+        ):
+            raise ValueError("context View creation identity mismatch")
+        payload = dict(event.payload)
+        usage = payload.get("compaction_usage")
+        if usage is not None and not RunRecoveryService._valid_usage_payload(usage):
+            raise ValueError("context View usage evidence is invalid")
+        expected = {
+            "view_id": view.view_id,
+            "capsule_id": view.capsule_id,
+            "recommended_level": view.recommended_level.value,
+            "applied_level": view.applied_level.value,
+            "fallback_from": (
+                view.fallback_from.value
+                if view.fallback_from is not None
+                else None
+            ),
+            "estimated_tokens": view.estimated_tokens,
+            "budget": (
+                view.budget.model_dump(mode="json")
+                if view.budget is not None
+                else None
+            ),
+            "message_refs": list(view.message_refs),
+            "source_refs": list(view.source_refs),
+            "transformations": list(view.transformations),
+            "consumed_message_ids": list(view.consumed_message_ids),
+            "compaction_usage": usage,
+        }
+        if payload != expected:
+            raise ValueError("context View creation projection mismatch")
+
+    @staticmethod
+    def _authenticate_prompt_manifest_event(
+        operation: ModelCallOperation,
+        manifest: PromptManifest,
+        event: EventEnvelope,
+    ) -> None:
+        if (
+            event.session_id != operation.session_id
+            or event.run_id != manifest.manifest_id
+            or event.type != "prompt.manifest.created"
+        ):
+            raise ValueError("prompt Manifest creation identity mismatch")
+        expected = {
+            "manifest_id": manifest.manifest_id,
+            "context_view_id": manifest.context_view_id,
+            "sha256": manifest.sha256,
+            "model": manifest.model,
+            "tools_sha256": manifest.tools_sha256,
+            "layers": [
+                {
+                    "layer_id": layer.layer_id,
+                    "version": layer.version,
+                    "sha256": layer.sha256,
+                }
+                for layer in manifest.layers
+            ],
+        }
+        if dict(event.payload) != expected:
+            raise ValueError("prompt Manifest creation projection mismatch")
+        if operation.prepared_request is None:
+            raise ValueError("prepared request is missing")
+        request = deserialize_model_request(thaw_json(operation.prepared_request))
+        if request.model != manifest.model or len(request.messages) < len(
+            manifest.layers
+        ):
+            raise ValueError("prepared prompt request mismatch")
+        layer_texts: list[str] = []
+        for layer, message in zip(manifest.layers, request.messages, strict=False):
+            if (
+                set(message) != {"role", "content"}
+                or message.get("role") != "system"
+                or not isinstance(message.get("content"), str)
+            ):
+                raise ValueError("prepared prompt layer shape mismatch")
+            content = message["content"]
+            if hashlib.sha256(content.encode("utf-8")).hexdigest() != layer.sha256:
+                raise ValueError("prepared prompt layer hash mismatch")
+            layer_texts.append(content)
+        aggregate = hashlib.sha256("\n\n".join(layer_texts).encode("utf-8")).hexdigest()
+        if aggregate != manifest.sha256:
+            raise ValueError("prepared prompt aggregate hash mismatch")
+        canonical_tools = json.dumps(
+            list(request.tools),
+            ensure_ascii=False,
+            allow_nan=False,
+            sort_keys=True,
+            separators=(",", ":"),
+        )
+        if (
+            hashlib.sha256(canonical_tools.encode("utf-8")).hexdigest()
+            != manifest.tools_sha256
+        ):
+            raise ValueError("prepared Tool schema hash mismatch")
+
+    @staticmethod
+    def _valid_usage_payload(value: object) -> bool:
+        if not isinstance(value, Mapping) or set(value) != {
+            "prompt_tokens",
+            "completion_tokens",
+            "total_tokens",
+        }:
+            return False
+        return all(
+            item is None or (type(item) is int and item >= 0)
+            for item in value.values()
+        )
+
     @staticmethod
     def _is_pristine_created(evidence: _RecoveryEvidence) -> bool:
         run = evidence.run
         return (
             run.status is RunStatus.CREATED
             and run.version == 1
             and evidence.checkpoint is None
             and not evidence.operations
             and not evidence.pending
             and len(evidence.run_events) == 1
diff --git a/tests/docs/test_v01_release_ledger.py b/tests/docs/test_v01_release_ledger.py
index 2fb2ada..da9f940 100644
--- a/tests/docs/test_v01_release_ledger.py
+++ b/tests/docs/test_v01_release_ledger.py
@@ -219,10 +219,26 @@ def test_v01_release_ledger_names_every_required_slice() -> None:
     assert "v0.1 current implementation status: R0-R3 completed; R4 pending" in progress
     _assert_release_checkpoint_and_r3_resume(ledger)
     _assert_release_checkpoint_and_r3_resume(progress)


 def test_active_roadmap_links_the_v01_plan_index() -> None:
     root = Path(__file__).parents[2]
     expected = "2026-07-17-agent-sdk-v0.1-implementation-index.md"
     assert expected in (root / "docs/plans/00-roadmap.md").read_text(encoding="utf-8")
     assert expected in (root / "docs/plans/tasks/index.md").read_text(encoding="utf-8")
+
+
+def test_r3_plan_hands_r4_to_capability_intersection_before_mailbox() -> None:
+    root = Path(__file__).parents[2]
+    plan = (
+        root
+        / "docs/superpowers/plans/2026-07-17-agent-sdk-v0.1-r3-auto-context.md"
+    ).read_text(encoding="utf-8")
+
+    assert R4_TASK1_TEST in plan
+    assert "first expected RED" in plan
+    assert "R4 Task 1" in plan
+    assert "R4 Task 2" in plan
+    assert R4_TASK2_MAILBOX_TEST in plan
+    assert plan.index(R4_TASK1_TEST) < plan.index(R4_TASK2_MAILBOX_TEST)
+    assert "uv run pytest tests/unit/subagents/test_mailbox.py -q" not in plan
diff --git a/tests/integration/context/test_compaction_slice.py b/tests/integration/context/test_compaction_slice.py
index 08c2f99..5dd29ea 100644
--- a/tests/integration/context/test_compaction_slice.py
+++ b/tests/integration/context/test_compaction_slice.py
@@ -1149,54 +1149,71 @@ async def test_commit_failure_does_not_claim_fallback_or_leave_partial_state() -
     assert await durable.get_snapshot("context_capsule", attempted_capsule_id) is None
     assert await durable.get_snapshot("context_view", attempted_view_id) is None


 @pytest.mark.asyncio
 async def test_sqlite_reopen_retrieval_order_and_session_deletion(tmp_path: Path) -> None:
     database = tmp_path / "context.db"
     store = await SQLiteStore.open(database)
     await _seed_projection(store)

-    async def acompletion(**_: object) -> dict[str, object]:
+    async def acompletion(**kwargs: object) -> dict[str, object]:
+        document = json.loads(kwargs["messages"][-1]["content"])
+        if document["operation"] == "summarize":
+            return _structured_response(
+                ["evt_projection_user", "evt_projection_assistant"]
+            )
         return _structured_response(
             [
-                "evt_projection_latest",
-                "evt_projection_user",
+                document["capsule_ids"][0],
                 "evt_projection_tool",
+                "evt_projection_latest",
             ]
         )

+    prior = await _planner(store, acompletion).build(
+        "ses_projection",
+        force_level="L3",
+        protected_event_ids={"evt_projection_tool"},
+    )
+    assert prior.capsule_id is not None
+
     view = await _planner(store, acompletion).build(
         "ses_projection",
         force_level="L4",
         protected_event_ids={"evt_projection_tool"},
     )
     assert view.capsule_id is not None
     await store.close()

     reopened = await SQLiteStore.open(database)
     try:
         retrieval = ContextRetrieval(reopened)
         capsule = await retrieval.get_capsule(
             view.capsule_id,
             session_id="ses_projection",
         )
         assert capsule.source_event_ids == (
-            "evt_projection_latest",
-            "evt_projection_user",
+            prior.capsule_id,
             "evt_projection_tool",
+            "evt_projection_latest",
         )
         sources = await retrieval.read_sources(
             view.capsule_id,
             session_id="ses_projection",
         )
-        assert tuple(item.event.event_id for item in sources) == capsule.source_event_ids
+        assert tuple(item.event.event_id for item in sources) == (
+            "evt_projection_user",
+            "evt_projection_assistant",
+            "evt_projection_tool",
+            "evt_projection_latest",
+        )
         with pytest.raises(AgentSDKError):
             await retrieval.read_sources(
                 view.capsule_id,
                 session_id="ses_other",
             )

         await reopened.delete_session("ses_projection")
         assert await reopened.get_snapshot("context_view", view.view_id) is None
         assert await reopened.get_snapshot(
             "context_capsule",
diff --git a/tests/integration/context/test_context_compaction.py b/tests/integration/context/test_context_compaction.py
index 8ee62df..3a27fce 100644
--- a/tests/integration/context/test_context_compaction.py
+++ b/tests/integration/context/test_context_compaction.py
@@ -301,20 +301,67 @@ async def test_forced_l3_with_empty_closed_slice_skips_model_and_falls_back() ->
         60,
         recent_messages=5,
     ).build("ses_task2", force_level="L3")

     assert model_calls == 0
     assert view.applied_level is CompactionLevel.L2
     assert view.fallback_from is CompactionLevel.L3
     assert view.capsule_id is None


+@pytest.mark.asyncio
+@pytest.mark.parametrize("force_level", [None, "L4"])
+async def test_first_use_l4_without_prior_capsule_falls_back_without_model_call(
+    force_level: str | None,
+) -> None:
+    store = InMemoryStore()
+    await _seed(store)
+    model_calls = 0
+
+    async def acompletion(**_: object) -> dict[str, object]:
+        nonlocal model_calls
+        model_calls += 1
+        return _response(
+            "evt_recent_answer",
+            "evt_latest_user",
+            objective="incomplete first rebase",
+        )
+
+    view = await _planner(store, acompletion, token_count=96).build(
+        "ses_task2",
+        force_level=force_level,
+    )
+
+    assert view.recommended_level is CompactionLevel.L4
+    assert view.applied_level is CompactionLevel.L2
+    assert view.fallback_from is CompactionLevel.L4
+    assert view.capsule_id is None
+    assert model_calls == 0
+    assert view.source_refs == (
+        "evt_old_user",
+        "evt_old_answer",
+        "evt_old_tool",
+        "evt_recent_answer",
+        "evt_latest_user",
+    )
+    events = await store.read_events(after_cursor=0, session_id="ses_task2")
+    failed = [
+        item.event for item in events if item.event.type == "context.compaction.failed"
+    ]
+    assert failed[-1].payload["requested_level"] == "L4"
+    assert failed[-1].payload["usage"] == {
+        "prompt_tokens": None,
+        "completion_tokens": None,
+        "total_tokens": None,
+    }
+
+
 @pytest.mark.asyncio
 async def test_l4_rebases_prior_capsule_evidence() -> None:
     store = InMemoryStore()
     await _seed(store)
     call_count = 0

     async def acompletion(**kwargs: object) -> dict[str, object]:
         nonlocal call_count
         call_count += 1
         if call_count == 1:
diff --git a/tests/integration/context/test_context_recovery.py b/tests/integration/context/test_context_recovery.py
index 9007e6e..bd89b7e 100644
--- a/tests/integration/context/test_context_recovery.py
+++ b/tests/integration/context/test_context_recovery.py
@@ -329,20 +329,50 @@ async def _tamper_prepared_reference(
         data = dict(snapshot.data)
         session_id = snapshot.session_id
         if corruption.endswith("_owner"):
             session_id = "ses_other"
         elif corruption == "view_identity":
             data["view_id"] = "view_other"
         elif corruption == "manifest_identity":
             data["manifest_id"] = "pmf_other"
         elif corruption == "manifest_link":
             data["context_view_id"] = "view_other"
+        elif corruption == "manifest_tools_sha256":
+            data["tools_sha256"] = "f" * 64
+        elif corruption == "manifest_sha256":
+            data["sha256"] = "f" * 64
+        elif corruption == "manifest_layer_sha256":
+            data["layers"][0]["sha256"] = "f" * 64
+        elif corruption == "manifest_layer_version":
+            data["layers"][0]["version"] = "tampered"
+        elif corruption == "manifest_layer_order":
+            data["layers"] = list(reversed(data["layers"]))
+        elif corruption == "view_level":
+            data["recommended_level"] = "L1"
+            data["applied_level"] = "L1"
+        elif corruption == "view_refs":
+            data["source_refs"] = [*data["source_refs"], "evt_tampered"]
+        elif corruption == "view_transformations":
+            data["transformations"] = [
+                *data["transformations"],
+                "tampered:evt_tampered",
+            ]
+        elif corruption == "view_consumed_message_ids":
+            data["consumed_message_ids"] = ["msg_tampered"]
+        elif corruption == "view_budget":
+            budget = data["budget"]
+            budget["output_reserve"] += 1
+            budget["available_input_tokens"] -= 1
+            budget["watermark_ratio"] = (
+                budget["projected_source_tokens"]
+                / budget["available_input_tokens"]
+            )
         else:
             raise AssertionError(f"unknown corruption: {corruption}")
         store._snapshots[target] = SnapshotWrite(
             snapshot.kind,
             snapshot.entity_id,
             session_id,
             snapshot.version,
             data,
         )
         return
@@ -370,20 +400,55 @@ async def _tamper_prepared_reference(
                 ("ses_other", *target),
             )
         else:
             assert snapshot_data is not None
             if corruption == "view_identity":
                 snapshot_data["view_id"] = "view_other"
             elif corruption == "manifest_identity":
                 snapshot_data["manifest_id"] = "pmf_other"
             elif corruption == "manifest_link":
                 snapshot_data["context_view_id"] = "view_other"
+            elif corruption == "manifest_tools_sha256":
+                snapshot_data["tools_sha256"] = "f" * 64
+            elif corruption == "manifest_sha256":
+                snapshot_data["sha256"] = "f" * 64
+            elif corruption == "manifest_layer_sha256":
+                snapshot_data["layers"][0]["sha256"] = "f" * 64
+            elif corruption == "manifest_layer_version":
+                snapshot_data["layers"][0]["version"] = "tampered"
+            elif corruption == "manifest_layer_order":
+                snapshot_data["layers"] = list(
+                    reversed(snapshot_data["layers"])
+                )
+            elif corruption == "view_level":
+                snapshot_data["recommended_level"] = "L1"
+                snapshot_data["applied_level"] = "L1"
+            elif corruption == "view_refs":
+                snapshot_data["source_refs"] = [
+                    *snapshot_data["source_refs"],
+                    "evt_tampered",
+                ]
+            elif corruption == "view_transformations":
+                snapshot_data["transformations"] = [
+                    *snapshot_data["transformations"],
+                    "tampered:evt_tampered",
+                ]
+            elif corruption == "view_consumed_message_ids":
+                snapshot_data["consumed_message_ids"] = ["msg_tampered"]
+            elif corruption == "view_budget":
+                budget = snapshot_data["budget"]
+                budget["output_reserve"] += 1
+                budget["available_input_tokens"] -= 1
+                budget["watermark_ratio"] = (
+                    budget["projected_source_tokens"]
+                    / budget["available_input_tokens"]
+                )
             else:
                 raise AssertionError(f"unknown corruption: {corruption}")
             await store._connection.execute(
                 """
                 UPDATE snapshots SET data_json = ?
                 WHERE kind = ? AND entity_id = ?
                 """,
                 (
                     json.dumps(
                         snapshot_data,
@@ -403,20 +468,30 @@ async def _tamper_prepared_reference(
 @pytest.mark.parametrize(
     "corruption",
     [
         "view_missing",
         "manifest_missing",
         "view_owner",
         "manifest_owner",
         "view_identity",
         "manifest_identity",
         "manifest_link",
+        "manifest_tools_sha256",
+        "manifest_sha256",
+        "manifest_layer_sha256",
+        "manifest_layer_version",
+        "manifest_layer_order",
+        "view_level",
+        "view_refs",
+        "view_transformations",
+        "view_consumed_message_ids",
+        "view_budget",
     ],
 )
 async def test_recovery_rejects_unauthenticated_prepared_references(
     backend: str,
     corruption: str,
     tmp_path: Path,
 ) -> None:
     store: InMemoryStore | SQLiteStore = (
         InMemoryStore()
         if backend == "memory"
@@ -432,21 +507,25 @@ async def test_recovery_rejects_unauthenticated_prepared_references(
             accepted.set()
             await asyncio.Event().wait()
             yield {"choices": []}

         return chunks()

     async def tool_handler(_: ToolContext) -> None:
         nonlocal tool_calls
         tool_calls += 1

-    spec = AgentSpec(name="reference-auth", model="test/model")
+    spec = AgentSpec(
+        name="reference-auth",
+        model="test/model",
+        system_prompt="Application recovery constraints.",
+    )
     sdk = AgentSDK.for_test(
         store=store,
         acompletion=hanging_provider,
         enable_builtin_tools=False,
     )
     sdk.tools.register(_recovery_tool(), tool_handler)
     session = await sdk.sessions.create(workspaces=[])
     handle = await sdk.runs.start(
         session.session_id,
         spec,
diff --git a/tests/integration/prompts/test_runtime_prompt.py b/tests/integration/prompts/test_runtime_prompt.py
index 10d1da4..318a2a4 100644
--- a/tests/integration/prompts/test_runtime_prompt.py
+++ b/tests/integration/prompts/test_runtime_prompt.py
@@ -297,25 +297,29 @@ async def test_runtime_prompt_orders_layers_and_persists_manifest_by_reference()
         created = next(
             item.event
             for item in events
             if item.event.type == "prompt.manifest.created"
         )
         assert created.payload == {
             "manifest_id": built.manifest.manifest_id,
             "context_view_id": view.view_id,
             "sha256": built.manifest.sha256,
             "model": spec.model,
-            "tools_sha256": built.manifest.tools_sha256,
-            "layers": [
-                {"layer_id": layer.layer_id, "sha256": layer.sha256}
-                for layer in built.manifest.layers
-            ],
+                "tools_sha256": built.manifest.tools_sha256,
+                "layers": [
+                    {
+                        "layer_id": layer.layer_id,
+                        "version": layer.version,
+                        "sha256": layer.sha256,
+                    }
+                    for layer in built.manifest.layers
+                ],
         }
         public_payload = json.dumps(created.payload, sort_keys=True)
         for raw_text in (
             "Application constraint.",
             activated[0].instructions,
             built.messages[0]["content"],
         ):
             assert raw_text not in public_payload
     finally:
         await sdk.close()
