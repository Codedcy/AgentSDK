# M01-T001 Project Scaffold and Core Configuration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create an installable typed package with deterministic ids, frozen configuration, and stable public errors.

**Architecture:** Keep root primitives dependency-light. Later modules import config/errors/ids; these files import no runtime adapters.

**Tech Stack:** Python 3.12+, uv, Pydantic v2, pytest, Ruff, mypy.

## Global Constraints

- Package/import name is `agent_sdk`.
- Python floor is 3.12.
- Configuration models are frozen and reject unknown fields.
- Public errors expose stable `ErrorCode` values.

---

### Task 1: Scaffold the package

**Files:**
- Create: `pyproject.toml`
- Create: `src/agent_sdk/__init__.py`
- Create: `src/agent_sdk/config.py`
- Create: `src/agent_sdk/errors.py`
- Create: `src/agent_sdk/ids.py`
- Create: `tests/unit/test_core_config.py`

**Interfaces:**
- Produces: `AgentSDKConfig`, `CaptureLevel`, `AgentSDKError`, `ErrorCode`, `new_id(prefix: str) -> str`.
- Consumes: no project interfaces.

- [ ] **Step 1: Write the failing core test**

```python
from pathlib import Path
import pytest
from agent_sdk.config import AgentSDKConfig, CaptureLevel
from agent_sdk.errors import AgentSDKError, ErrorCode
from agent_sdk.ids import new_id

def test_core_contracts_are_stable() -> None:
    config = AgentSDKConfig(database_path=Path("state.db"))
    assert config.capture_level is CaptureLevel.PREVIEW
    assert new_id("run").startswith("run_")
    with pytest.raises(Exception):
        AgentSDKConfig(database_path=Path("x.db"), unknown=True)
    error = AgentSDKError(ErrorCode.INVALID_STATE, "bad state", retryable=False)
    assert error.to_dict()["code"] == "invalid_state"
```

- [ ] **Step 2: Run the test and confirm the package is absent**

Run: `python -m pytest tests/unit/test_core_config.py -v`

Expected: collection fails with `ModuleNotFoundError: No module named 'agent_sdk'`.

- [ ] **Step 3: Add package metadata and dependencies**

```toml
[project]
name = "agent-sdk"
version = "0.1.0.dev0"
requires-python = ">=3.12"
dependencies = [
  "pydantic>=2,<3", "litellm>=1,<2", "aiosqlite>=0.20,<1",
  "mcp>=1,<2", "PyYAML>=6,<7",
]

[dependency-groups]
dev = ["pytest>=8,<9", "pytest-asyncio>=0.25,<1", "hypothesis>=6,<7", "ruff>=0.9,<1", "mypy>=1.14,<2"]

[build-system]
requires = ["hatchling>=1.27,<2"]
build-backend = "hatchling.build"

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]

[tool.ruff]
target-version = "py312"
line-length = 100

[tool.mypy]
python_version = "3.12"
strict = true
packages = ["agent_sdk"]
```

- [ ] **Step 4: Implement frozen config, ids, and errors**

```python
# src/agent_sdk/config.py
from enum import StrEnum
from pathlib import Path
from pydantic import BaseModel, ConfigDict

class CaptureLevel(StrEnum):
    METADATA = "metadata"
    PREVIEW = "preview"
    FULL = "full"

class AgentSDKConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    database_path: Path
    capture_level: CaptureLevel = CaptureLevel.PREVIEW
```

```python
# src/agent_sdk/errors.py
from enum import StrEnum
from typing import Any

class ErrorCode(StrEnum):
    INVALID_STATE = "invalid_state"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    INTERNAL = "internal"

class AgentSDKError(Exception):
    def __init__(self, code: ErrorCode, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.code, self.message, self.retryable = code, message, retryable
    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code.value, "message": self.message, "retryable": self.retryable}
```

```python
# src/agent_sdk/ids.py
from uuid import uuid4
def new_id(prefix: str) -> str:
    if not prefix or not prefix.replace("_", "").isalnum():
        raise ValueError("prefix must be alphanumeric/underscore")
    return f"{prefix}_{uuid4().hex}"
```

- [ ] **Step 5: Export the public primitives and verify**

Run: `uv sync && uv run pytest tests/unit/test_core_config.py -v && uv run ruff check src tests && uv run mypy src`

Expected: one test passes; Ruff and mypy exit 0.

- [ ] **Step 6: Commit**

```powershell
git add pyproject.toml uv.lock src/agent_sdk tests/unit/test_core_config.py
git commit -m "chore: scaffold agent sdk package"
```
