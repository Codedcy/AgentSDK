# Public v0.1 README Implementation Plan

> **For Codex:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace the repository README with a truthful public v0.1 entry point that is runnable from source, exposes the shipped capability boundary, and links users to the detailed guides.

**Architecture:** Treat the README as a tested public contract. Add focused repository tests for installation, release metadata, navigation, and v0.1 boundaries before rewriting the document; then validate the finished Markdown against those tests and the repository's existing documentation checks.

**Tech Stack:** Markdown, Python 3.12, pytest, Ruff, Git

---

### Task 1: Lock the public README contract

**Files:**
- Create: `tests/docs/test_public_readme.py`
- Test: `tests/docs/test_public_readme.py`

**Step 1: Write the failing contract tests**

```python
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
README = ROOT / "README.md"


def _readme() -> str:
    return README.read_text(encoding="utf-8")


def test_readme_uses_source_install_and_deterministic_smoke() -> None:
    readme = _readme()

    assert "git clone https://github.com/Codedcy/AgentSDK.git" in readme
    assert "python -m pip install ." in readme
    assert "python -m pip install agent-sdk" not in readme
    assert "--smoke --database .agent-sdk/state.db --workspace ." in readme


def test_readme_states_release_and_recovery_boundaries() -> None:
    readme = _readme()

    for text in (
        "0.1.0",
        "Python 3.12 and 3.13",
        "one SDK instance in one process",
        "exactly-once",
        "compile",
        "application confirmation",
        "cross-run",
    ):
        assert text in readme


def test_readme_links_public_v01_documentation() -> None:
    readme = _readme()

    for target in (
        "docs/guides/v01-quickstart.md",
        "docs/guides/v01-recovery.md",
        "docs/guides/v01-tracing-and-analysis.md",
        "docs/design/00-high-level-design.md",
        "docs/plans/releases/v0.1.md",
    ):
        assert f"]({target})" in readme


def test_readme_does_not_claim_unsupported_distribution_or_badges() -> None:
    readme = _readme().lower()

    assert "published on pypi" not in readme
    assert "pypi.org" not in readme
    assert "shields.io" not in readme
```

**Step 2: Run the focused test and confirm RED**

Run:

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'
$env:PYTHONPATH='src'
C:\Users\10176\AppData\Local\Temp\agent-sdk-python312-gate\source-venv\Scripts\python.exe -m pytest -p pytest_asyncio.plugin tests/docs/test_public_readme.py -q
```

Expected: FAIL because the current README still presents an unqualified package-index install and does not contain the complete public v0.1 contract.

### Task 2: Rewrite the README for public SDK users

**Files:**
- Modify: `README.md`
- Test: `tests/docs/test_public_readme.py`

**Step 1: Replace the README information architecture**

Write the approved sections in this order:

1. v0.1 positioning and release status.
2. Source installation and deterministic smoke run.
3. Minimal LiteLLM-backed Agent Run.
4. Capability matrix.
5. Permission and generated Workflow admission examples.
6. Recovery and observability entry points.
7. Visible v0.1 boundaries.
8. Documentation navigation and maintainer verification.

Keep all examples copyable, import every referenced symbol, and do not claim PyPI publication or distributed/exactly-once guarantees.

**Step 2: Run the focused contract test and confirm GREEN**

Run the same command from Task 1.

Expected: `4 passed`.

**Step 3: Run all documentation tests**

Run:

```powershell
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'
$env:PYTHONPATH='src'
C:\Users\10176\AppData\Local\Temp\agent-sdk-python312-gate\source-venv\Scripts\python.exe -m pytest -p pytest_asyncio.plugin tests/docs -q
```

Expected: PASS.

**Step 4: Run static and whitespace checks**

Run:

```powershell
python -m ruff check tests/docs/test_public_readme.py
git diff --check
```

Expected: both commands pass with no output indicating errors.

**Step 5: Review the diff against the design**

Run:

```powershell
git diff -- README.md tests/docs/test_public_readme.py
```

Confirm the README is concise, source-install-first, and accurately separates shipped v0.1 behavior from deferred work.

**Step 6: Commit**

```powershell
git add README.md tests/docs/test_public_readme.py
git commit -m "docs: refresh public v0.1 readme"
```
