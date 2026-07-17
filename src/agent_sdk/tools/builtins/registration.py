from __future__ import annotations

from functools import partial

from agent_sdk.storage.base import StateStore
from agent_sdk.tools.builtins.bash import bash_permission_arguments, run_bash
from agent_sdk.tools.builtins.files import (
    file_permission_arguments,
    read_file,
    write_file,
)
from agent_sdk.tools.models import ToolSpec
from agent_sdk.tools.registry import ToolRegistry


def register_builtin_tools(
    *,
    registry: ToolRegistry,
    store: StateStore,
    output_limit: int,
) -> None:
    registry.register(
        ToolSpec(
            name="read",
            description="Read a UTF-8 text preview from the configured workspace.",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "minLength": 1},
                    "max_bytes": {"type": "integer", "minimum": 1},
                },
                "required": ["path"],
                "additionalProperties": False,
            },
            source="builtin",
            effects=("filesystem.read",),
        ),
        partial(read_file, store=store, output_limit=output_limit),
        permission_arguments=partial(
            file_permission_arguments,
            store=store,
            for_write=False,
        ),
    )
    registry.register(
        ToolSpec(
            name="write",
            description="Atomically write UTF-8 text inside the configured workspace.",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "minLength": 1},
                    "content": {"type": "string"},
                    "overwrite": {"type": "boolean", "default": False},
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
            source="builtin",
            effects=("filesystem.write",),
        ),
        partial(write_file, store=store, output_limit=output_limit),
        permission_arguments=partial(
            file_permission_arguments,
            store=store,
            for_write=True,
        ),
    )
    registry.register(
        ToolSpec(
            name="bash",
            description="Run one argv-based process in a configured workspace directory.",
            input_schema={
                "type": "object",
                "properties": {
                    "argv": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                    },
                    "cwd": {"type": "string", "minLength": 1},
                    "timeout_seconds": {
                        "type": "number",
                        "exclusiveMinimum": 0,
                    },
                },
                "required": ["argv"],
                "additionalProperties": False,
            },
            source="builtin",
            effects=("process.execute",),
        ),
        partial(run_bash, store=store, output_limit=output_limit),
        permission_arguments=partial(
            bash_permission_arguments,
            store=store,
        ),
    )


__all__ = ["register_builtin_tools"]
