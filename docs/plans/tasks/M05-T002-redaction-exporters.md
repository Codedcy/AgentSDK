# M05-T002 Redaction and Exporters Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent secret leakage and export observable data reliably without affecting agent execution.

**Architecture:** Every externally visible payload crosses a fail-closed redaction pipeline and disclosure-level projector. Exporters consume the event log independently by cursor with retry/health/dead-letter state; exporter failure is isolated from the Run engine.

**Tech Stack:** Pydantic v2, OpenTelemetry SDK, JSONL, asyncio, pytest-asyncio.

## Global Constraints

- Disclosure levels are `metadata`, `preview`, and `full`; `full` still applies secret redaction.
- Known headers, environment entries, secret references, and application-defined patterns are redacted recursively.
- Redaction failure publishes a safe placeholder and diagnostic, never the unredacted value.
- Exporter delivery is at-least-once and cursor-resumable.

---

### Task 1: Implement recursive fail-closed redaction

**Files:**
- Modify: `src/agent_sdk/observability/redaction.py`
- Modify: `src/agent_sdk/secrets.py`
- Create: `tests/unit/observability/test_redaction.py`
- Create: `tests/property/test_redaction.py`

- [ ] **Step 1: Write failing secret tests**

```python
@pytest.mark.parametrize("key", ["authorization", "proxy-authorization", "x-api-key", "api_key", "password", "token"])
def test_sensitive_keys_are_redacted_case_insensitively(redactor, key) -> None:
    assert redactor.apply({key: "secret"})[key] == "[REDACTED]"

@given(nested_secret_payloads())
def test_registered_secret_never_appears_in_serialized_output(redactor, payload) -> None:
    output = json.dumps(redactor.apply(payload.value))
    assert payload.secret not in output
```

- [ ] **Step 2: Verify failure**

Run: `uv run pytest tests/unit/observability/test_redaction.py tests/property/test_redaction.py -v`

Expected: recursive/custom/fail-closed redaction is incomplete.

- [ ] **Step 3: Implement ordered redactors and disclosure views**

```python
class RedactionPipeline:
    def apply(self, value: Any, level: DisclosureLevel = DisclosureLevel.PREVIEW) -> Any:
        try:
            sanitized = redact_secret_refs(value)
            sanitized = redact_sensitive_keys(sanitized, self._sensitive_keys)
            sanitized = redact_registered_values(sanitized, self._secret_values)
            sanitized = apply_custom_redactors(sanitized, self._custom)
            return project_disclosure(sanitized, level, self._limits)
        except Exception as error:
            self._diagnostics.record("redaction_failed", type(error).__name__)
            return "[REDACTION FAILED]"
```

Make application redactors pure callables with execution limits and deterministic ordering. Preserve structure where safe and return artifact references instead of oversized values.

- [ ] **Step 4: Verify and commit**

Run: `uv run pytest tests/unit/observability/test_redaction.py tests/property/test_redaction.py -v`

Expected: nested models, cycles, bytes, exceptions, custom rules, levels, and serialization pass without leaks.

```powershell
git add src/agent_sdk/observability/redaction.py src/agent_sdk/secrets.py tests/unit/observability/test_redaction.py tests/property/test_redaction.py
git commit -m "feat: add fail-closed trace redaction"
```

---

### Task 2: Implement JSONL, Python, and OpenTelemetry exporters

**Files:**
- Create: `src/agent_sdk/observability/exporters/base.py`
- Create: `src/agent_sdk/observability/exporters/jsonl.py`
- Create: `src/agent_sdk/observability/exporters/python.py`
- Create: `src/agent_sdk/observability/exporters/otel.py`
- Create: `src/agent_sdk/observability/exporters/worker.py`
- Create: `tests/integration/observability/test_exporters.py`

- [ ] **Step 1: Write failing exporter isolation tests**

```python
@pytest.mark.asyncio
async def test_exporter_failure_does_not_fail_run(sdk, failing_exporter) -> None:
    sdk.observability.add_exporter(failing_exporter)
    result = await sdk.run(echo_agent(), "hello")
    assert result.status == "completed"
    assert (await sdk.observability.exporter_health(failing_exporter.name)).status == "degraded"

@pytest.mark.asyncio
async def test_exporter_resumes_from_checkpoint(export_fixture) -> None:
    await export_fixture.stop_after(5)
    await export_fixture.restart()
    assert await export_fixture.exported_event_ids() == await export_fixture.source_event_ids()
```

- [ ] **Step 2: Verify failure**

Run: `uv run pytest tests/integration/observability/test_exporters.py -v`

Expected: cursor workers and exporters are missing.

- [ ] **Step 3: Implement exporter protocol and isolated worker**

```python
class EventExporter(Protocol):
    name: str
    async def export(self, events: Sequence[EventEnvelope]) -> None: ...
    async def close(self) -> None: ...

async def export_batch(self) -> None:
    events = await self._store.events_after(self._checkpoint, limit=self._batch_size)
    try:
        await self._exporter.export(self._redact(events))
    except Exception as error:
        await self._health.record_failure(self._exporter.name, error)
        await self._backoff.wait(self._attempt)
        return
    await self._checkpoints.advance(self._exporter.name, events[-1].cursor)
```

JSONL appends one canonical object per event; Python invokes an application callback; OTEL maps Runs/tool/model/workflow attempts to spans and usage/failure data to metrics/events.

- [ ] **Step 4: Add retry ceiling and dead-letter diagnostics**

After the configured retry ceiling, retain the last failed batch range and sanitized error in exporter health/dead-letter storage, pause that exporter, and expose an application resume command.

```python
async def record_exhausted(self, exporter: str, events: Sequence[EventEnvelope], error: Exception) -> None:
    await self._health.pause(
        exporter,
        DeadLetter(first_cursor=events[0].cursor, last_cursor=events[-1].cursor, error=self._redactor.apply(str(error))),
    )

async def resume(self, exporter: str) -> None:
    await self._health.clear_pause(exporter)
    self._workers.wake(exporter)
```

- [ ] **Step 5: Verify and commit**

Run: `uv run pytest tests/integration/observability/test_exporters.py -v`

Expected: formats, retries, checkpoints, duplicate tolerance, health, dead-letter, redaction, and failure isolation pass.

```powershell
git add src/agent_sdk/observability/exporters tests/integration/observability/test_exporters.py
git commit -m "feat: add resilient observability exporters"
```
