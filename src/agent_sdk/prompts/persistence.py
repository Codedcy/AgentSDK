from __future__ import annotations

from agent_sdk.errors import AgentSDKError, ErrorCode
from agent_sdk.events.models import EventEnvelope
from agent_sdk.prompts.models import PromptManifest
from agent_sdk.storage.base import (
    CommitBatch,
    SnapshotPrecondition,
    SnapshotPreconditionError,
    SnapshotWrite,
    StateStore,
)


class PromptManifestPersistence:
    def __init__(self, store: StateStore) -> None:
        self._store = store

    async def persist(
        self,
        manifest: PromptManifest,
        *,
        session_id: str,
    ) -> None:
        event = EventEnvelope.new(
            type="prompt.manifest.created",
            session_id=session_id,
            run_id=manifest.manifest_id,
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
                    snapshots=(
                        SnapshotWrite(
                            "prompt_manifest",
                            manifest.manifest_id,
                            session_id,
                            1,
                            manifest.model_dump(mode="json"),
                        ),
                    ),
                    preconditions=(
                        SnapshotPrecondition("session", session_id),
                        SnapshotPrecondition(
                            "context_view",
                            manifest.context_view_id,
                            session_id=session_id,
                        ),
                    ),
                )
            )
        except SnapshotPreconditionError as error:
            raise AgentSDKError(
                ErrorCode.NOT_FOUND,
                "prompt manifest owner no longer exists",
                retryable=False,
            ) from error
        except AgentSDKError:
            raise
        except Exception as error:
            raise AgentSDKError(
                ErrorCode.INTERNAL,
                "prompt manifest persistence failed",
                retryable=False,
            ) from error


__all__ = ["PromptManifestPersistence"]
