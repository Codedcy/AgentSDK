from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime

from agent_sdk.errors import AgentSDKError, ErrorCode
from agent_sdk.events.models import EventEnvelope
from agent_sdk.ids import new_id
from agent_sdk.runtime.idempotency import _idempotency_public_error
from agent_sdk.runtime.models import RunSnapshot, RunStatus
from agent_sdk.storage.base import (
    CommitBatch,
    SnapshotPrecondition,
    SnapshotPreconditionError,
    SnapshotWrite,
    StateStore,
)
from agent_sdk.storage.idempotency import (
    IdempotencyError,
    IdempotencyReplay,
    IdempotencyWrite,
    fingerprint_command,
    validate_replay,
)
from agent_sdk.subagents.models import (
    AgentMessage,
    MailboxCursorSnapshot,
    MailboxSnapshot,
)

_MAX_COMMIT_ATTEMPTS = 8


@dataclass(frozen=True)
class MailboxRead:
    mailbox: MailboxSnapshot
    cursor: MailboxCursorSnapshot
    messages: tuple[AgentMessage, ...]

    def advanced_cursor(self) -> MailboxCursorSnapshot | None:
        if not self.messages:
            return None
        return self.cursor.model_copy(
            update={
                "last_consumed_sequence": self.messages[-1].sequence,
                "version": self.cursor.version + 1,
            }
        )


class MailboxService:
    def __init__(self, store: StateStore) -> None:
        self._store = store

    async def send(
        self,
        sender_run_id: str,
        recipient_run_id: str,
        content: str,
        *,
        idempotency_key: str | None = None,
    ) -> AgentMessage:
        if not isinstance(content, str) or not 1 <= len(content) <= 32_768:
            raise AgentSDKError(
                ErrorCode.INVALID_STATE,
                "message content must contain 1..32768 characters",
                retryable=False,
            )
        scope = f"run/{sender_run_id}/message.send"
        fingerprint = fingerprint_command(
            "agent.message.send",
            {
                "sender_run_id": sender_run_id,
                "recipient_run_id": recipient_run_id,
                "content": content,
            },
        )
        if idempotency_key is not None:
            try:
                validate_replay(
                    IdempotencyReplay(scope, idempotency_key, fingerprint)
                )
            except IdempotencyError as error:
                raise _idempotency_public_error(error) from None

        for attempt in range(_MAX_COMMIT_ATTEMPTS):
            sender = await self._load_run(sender_run_id)
            recipient = await self._load_run(recipient_run_id)
            self._validate_relation(sender, recipient)
            try:
                mailbox = await self._ensure_mailbox(recipient)
            except SnapshotPreconditionError:
                if attempt + 1 < _MAX_COMMIT_ATTEMPTS:
                    await asyncio.sleep(0)
                    continue
                break
            has_replay = False
            if idempotency_key is not None:
                try:
                    has_replay = (
                        await self._store.get_idempotency(scope, idempotency_key)
                        is not None
                    )
                except IdempotencyError as error:
                    raise _idempotency_public_error(error) from None
            if has_replay:
                assert idempotency_key is not None
                try:
                    result = await self._store.commit(
                        CommitBatch(
                            events=(),
                            idempotency=IdempotencyReplay(
                                scope,
                                idempotency_key,
                                fingerprint,
                            ),
                            replay_preconditions=(
                                self._exact_run(sender),
                                self._exact_run(recipient),
                                self._exact_mailbox(mailbox),
                            ),
                        )
                    )
                except SnapshotPreconditionError:
                    if attempt + 1 < _MAX_COMMIT_ATTEMPTS:
                        await asyncio.sleep(0)
                        continue
                    break
                except IdempotencyError as error:
                    raise _idempotency_public_error(error) from None
                return self._validated_replay(
                    result=result.idempotency,
                    mailbox=mailbox,
                    sender=sender,
                    recipient=recipient,
                    content=content,
                )
            sequence = (
                mailbox.messages[-1].sequence + 1
                if mailbox.messages
                else 1
            )
            message = AgentMessage(
                message_id=new_id("msg"),
                session_id=recipient.session_id,
                sender_run_id=sender.run_id,
                recipient_run_id=recipient.run_id,
                sequence=sequence,
                content=content,
                created_at=datetime.now(UTC),
            )
            updated = mailbox.model_copy(
                update={
                    "version": mailbox.version + 1,
                    "messages": (*mailbox.messages, message),
                }
            )
            idempotency = None
            if idempotency_key is not None:
                idempotency = IdempotencyWrite(
                    scope=scope,
                    key=idempotency_key,
                    request_fingerprint=fingerprint,
                    session_id=sender.session_id,
                    result=message.model_dump(mode="json"),
                )
            try:
                result = await self._store.commit(
                    CommitBatch(
                        events=(
                            EventEnvelope.new(
                                type="agent.message.sent",
                                session_id=message.session_id,
                                run_id=f"mailbox:{recipient.run_id}",
                                sequence=message.sequence,
                                payload=message.model_dump(mode="json"),
                            ),
                        ),
                        snapshots=(self._mailbox_write(updated),),
                        preconditions=(
                            self._exact_run(sender),
                            self._exact_run(recipient),
                            self._exact_mailbox(mailbox),
                        ),
                        idempotency=idempotency,
                        replay_preconditions=(
                            self._exact_run(sender),
                            self._exact_run(recipient),
                            self._exact_mailbox(updated),
                        ) if idempotency is not None else (),
                    )
                )
            except SnapshotPreconditionError:
                if attempt + 1 < _MAX_COMMIT_ATTEMPTS:
                    await asyncio.sleep(0)
                    continue
                break
            except IdempotencyError as error:
                raise _idempotency_public_error(error) from None
            if result.applied:
                return message
            return self._validated_replay(
                result=result.idempotency,
                mailbox=updated,
                sender=sender,
                recipient=recipient,
                content=content,
            )
        raise AgentSDKError(
            ErrorCode.CONFLICT,
            "mailbox state changed concurrently",
            retryable=True,
        )

    async def unread(self, recipient_run_id: str) -> tuple[AgentMessage, ...]:
        return (await self.read(recipient_run_id)).messages

    async def read(
        self,
        recipient_run_id: str,
        *,
        session_id: str | None = None,
    ) -> MailboxRead:
        for attempt in range(_MAX_COMMIT_ATTEMPTS):
            recipient = await self._load_run(recipient_run_id)
            if session_id is not None and recipient.session_id != session_id:
                raise AgentSDKError(
                    ErrorCode.NOT_FOUND,
                    "run not found",
                    retryable=False,
                )
            if recipient.status in {RunStatus.COMPLETED, RunStatus.FAILED}:
                raise AgentSDKError(
                    ErrorCode.INVALID_STATE,
                    "terminal run cannot receive messages",
                    retryable=False,
                )
            try:
                mailbox = await self._ensure_mailbox(recipient)
                cursor = await self._ensure_cursor(recipient)
            except SnapshotPreconditionError:
                if attempt + 1 < _MAX_COMMIT_ATTEMPTS:
                    await asyncio.sleep(0)
                    continue
                break
            if (
                cursor.recipient_run_id != mailbox.recipient_run_id
                or cursor.session_id != mailbox.session_id
                or cursor.last_consumed_sequence > len(mailbox.messages)
            ):
                raise self._invalid_mailbox()
            messages = tuple(
                message
                for message in mailbox.messages
                if message.sequence > cursor.last_consumed_sequence
            )
            return MailboxRead(
                mailbox=mailbox,
                cursor=cursor,
                messages=messages,
            )
        raise AgentSDKError(
            ErrorCode.CONFLICT,
            "mailbox state changed concurrently",
            retryable=True,
        )

    async def _load_run(self, run_id: str) -> RunSnapshot:
        try:
            data = await self._store.get_snapshot("run", run_id)
            if data is None:
                raise AgentSDKError(
                    ErrorCode.NOT_FOUND,
                    "run not found",
                    retryable=False,
                )
            return RunSnapshot.model_validate(data)
        except AgentSDKError:
            raise
        except Exception:
            raise AgentSDKError(
                ErrorCode.INTERNAL,
                "stored run is invalid",
                retryable=False,
            ) from None

    @staticmethod
    def _validate_relation(sender: RunSnapshot, recipient: RunSnapshot) -> None:
        direct = (
            sender.run_id != recipient.run_id
            and (
                recipient.parent_run_id == sender.run_id
                or sender.parent_run_id == recipient.run_id
            )
        )
        if sender.session_id != recipient.session_id or not direct:
            raise AgentSDKError(
                ErrorCode.INVALID_STATE,
                "target must be a direct parent or child in the same session",
                retryable=False,
            )
        if sender.status in {RunStatus.COMPLETED, RunStatus.FAILED} or (
            recipient.status in {RunStatus.COMPLETED, RunStatus.FAILED}
        ):
            raise AgentSDKError(
                ErrorCode.INVALID_STATE,
                "terminal run cannot send or receive messages",
                retryable=False,
            )

    async def _ensure_mailbox(self, recipient: RunSnapshot) -> MailboxSnapshot:
        data = await self._store.get_snapshot("mailbox", recipient.run_id)
        if data is not None:
            return self._validated_mailbox(data, recipient)
        mailbox = MailboxSnapshot(
            recipient_run_id=recipient.run_id,
            session_id=recipient.session_id,
        )
        result = await self._store.commit(
            CommitBatch(
                events=(),
                snapshots=(self._mailbox_write(mailbox),),
                preconditions=(self._exact_run(recipient),),
                idempotency=self._bootstrap_write(
                    kind="mailbox",
                    recipient=recipient,
                    result=mailbox.model_dump(mode="json"),
                ),
                replay_preconditions=(
                    self._exact_run(recipient),
                    self._exact_mailbox(mailbox),
                ),
            )
        )
        if result.applied:
            return mailbox
        record = result.idempotency
        if record is None:
            raise self._invalid_mailbox()
        return self._validated_mailbox(dict(record.result), recipient)

    async def _ensure_cursor(
        self,
        recipient: RunSnapshot,
    ) -> MailboxCursorSnapshot:
        data = await self._store.get_snapshot("mailbox_cursor", recipient.run_id)
        if data is not None:
            return self._validated_cursor(data, recipient)
        cursor = MailboxCursorSnapshot(
            recipient_run_id=recipient.run_id,
            session_id=recipient.session_id,
        )
        result = await self._store.commit(
            CommitBatch(
                events=(),
                snapshots=(self._cursor_write(cursor),),
                preconditions=(self._exact_run(recipient),),
                idempotency=self._bootstrap_write(
                    kind="mailbox_cursor",
                    recipient=recipient,
                    result=cursor.model_dump(mode="json"),
                ),
                replay_preconditions=(
                    self._exact_run(recipient),
                    self.exact_cursor(cursor),
                ),
            )
        )
        if result.applied:
            return cursor
        record = result.idempotency
        if record is None:
            raise self._invalid_mailbox()
        return self._validated_cursor(dict(record.result), recipient)

    @classmethod
    def _validated_mailbox(
        cls,
        data: object,
        recipient: RunSnapshot,
    ) -> MailboxSnapshot:
        try:
            mailbox = MailboxSnapshot.model_validate(data)
            if (
                mailbox.recipient_run_id != recipient.run_id
                or mailbox.session_id != recipient.session_id
            ):
                raise ValueError("mailbox owner mismatch")
            return mailbox
        except Exception:
            raise cls._invalid_mailbox() from None

    @classmethod
    def _validated_cursor(
        cls,
        data: object,
        recipient: RunSnapshot,
    ) -> MailboxCursorSnapshot:
        try:
            cursor = MailboxCursorSnapshot.model_validate(data)
            if (
                cursor.recipient_run_id != recipient.run_id
                or cursor.session_id != recipient.session_id
            ):
                raise ValueError("mailbox cursor owner mismatch")
            return cursor
        except Exception:
            raise cls._invalid_mailbox() from None

    @staticmethod
    def _invalid_mailbox() -> AgentSDKError:
        return AgentSDKError(
            ErrorCode.INTERNAL,
            "stored mailbox is invalid",
            retryable=False,
        )

    @staticmethod
    def _bootstrap_write(
        *,
        kind: str,
        recipient: RunSnapshot,
        result: dict[str, object],
    ) -> IdempotencyWrite:
        command = f"{kind}.bootstrap"
        return IdempotencyWrite(
            scope=f"run/{recipient.run_id}/{command}",
            key="v1",
            request_fingerprint=fingerprint_command(
                command,
                {
                    "recipient_run_id": recipient.run_id,
                    "session_id": recipient.session_id,
                },
            ),
            session_id=recipient.session_id,
            result=result,
        )

    @staticmethod
    def _exact_run(run: RunSnapshot) -> SnapshotPrecondition:
        return SnapshotPrecondition(
            "run",
            run.run_id,
            run.version,
            run.session_id,
            run.model_dump(mode="json"),
        )

    @staticmethod
    def _exact_mailbox(mailbox: MailboxSnapshot) -> SnapshotPrecondition:
        return SnapshotPrecondition(
            "mailbox",
            mailbox.recipient_run_id,
            mailbox.version,
            mailbox.session_id,
            mailbox.model_dump(mode="json"),
        )

    @staticmethod
    def exact_cursor(cursor: MailboxCursorSnapshot) -> SnapshotPrecondition:
        return SnapshotPrecondition(
            "mailbox_cursor",
            cursor.recipient_run_id,
            cursor.version,
            cursor.session_id,
            cursor.model_dump(mode="json"),
        )

    @staticmethod
    def _validated_replay(
        *,
        result: object,
        mailbox: MailboxSnapshot,
        sender: RunSnapshot,
        recipient: RunSnapshot,
        content: str,
    ) -> AgentMessage:
        try:
            if result is None:
                raise ValueError("missing idempotency result")
            payload = getattr(result, "result")
            message = AgentMessage.model_validate(dict(payload))
            if (
                message.session_id != sender.session_id
                or message.sender_run_id != sender.run_id
                or message.recipient_run_id != recipient.run_id
                or message.content != content
                or message not in mailbox.messages
            ):
                raise ValueError("invalid idempotency result")
            return message
        except Exception:
            raise AgentSDKError(
                ErrorCode.INTERNAL,
                "stored mailbox command result is invalid",
                retryable=False,
            ) from None

    @staticmethod
    def _mailbox_write(mailbox: MailboxSnapshot) -> SnapshotWrite:
        return SnapshotWrite(
            "mailbox",
            mailbox.recipient_run_id,
            mailbox.session_id,
            mailbox.version,
            mailbox.model_dump(mode="json"),
        )

    @staticmethod
    def _cursor_write(cursor: MailboxCursorSnapshot) -> SnapshotWrite:
        return SnapshotWrite(
            "mailbox_cursor",
            cursor.recipient_run_id,
            cursor.session_id,
            cursor.version,
            cursor.model_dump(mode="json"),
        )
