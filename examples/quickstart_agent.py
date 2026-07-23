from __future__ import annotations

import argparse
from pathlib import Path

from agent_sdk import (
    AgentSDK,
    AgentSDKConfig,
    AgentSpec,
    SessionSnapshot,
)
from agent_sdk.permissions import PermissionRule


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build a small general Agent with Agent SDK.",
    )
    parser.add_argument("--model", default="openai/gpt-4o-mini")
    parser.add_argument(
        "--database",
        type=Path,
        default=Path(".agent-sdk/quickstart.db"),
    )
    parser.add_argument("--workspace", type=Path, default=Path("."))
    parser.add_argument("--session-id")
    return parser


def create_sdk(args: argparse.Namespace) -> AgentSDK:
    workspace = args.workspace.resolve()
    return AgentSDK(
        AgentSDKConfig(
            database_path=args.database,
            permission_default="ask",
            permission_rules=(
                PermissionRule(
                    outcome="allow",
                    tool="read",
                    path_prefix=workspace,
                ),
                PermissionRule(
                    outcome="ask",
                    tool="write",
                    path_prefix=workspace,
                ),
                PermissionRule(
                    outcome="ask",
                    tool="bash",
                    path_prefix=workspace,
                ),
            ),
        )
    )


async def select_session(
    sdk: AgentSDK,
    workspace: Path,
    session_id: str | None,
) -> SessionSnapshot:
    if session_id is not None:
        return await sdk.sessions.get(session_id)
    return await sdk.sessions.create(workspaces=(workspace.resolve(),))


def define_agent(sdk: AgentSDK, model: str) -> AgentSpec:
    return sdk.agents.define(
        AgentSpec(
            name="quickstart",
            model=model,
            tool_allowlist=("read", "write", "bash"),
        )
    )
