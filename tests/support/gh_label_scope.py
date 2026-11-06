"""Explicit isolated read-to-label setup and shared boundary assertions; no fault dispatch."""

import json
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from typing import Any
from uuid import uuid4

import procrastinate
import psycopg
import pytest
from psycopg import sql

from local_dev_mcp.tools.gh.delivery import (
    ClaimGenerationRecord,
    ConsumerOperationContract,
    ContractCatalog,
    DeliveryReceipt,
    IssueLabelHold,
    OperationCheckpoint,
    OperationIntent,
    OperationNode,
    PlanStructure,
    PostgresIssueLabelWriteScope,
    accept_event,
    bind_issue_add_label_contract,
    bind_issue_read_contract,
    inspect_operation_recovery_evidence,
    inspect_running_claim,
    issue_label_lock_key,
    load_operation_attempt,
    load_operation_checkpoint,
    load_operation_lineage,
    load_target,
    make_issue_add_label_intent,
    make_issue_read_intent,
    record_claim_generation,
    record_operation_checkpoint,
    record_operation_intent,
    seal_plan_structure,
)
from local_dev_mcp.tools.gh.delivery.schema import (
    ATTEMPTS_DDL,
    CHECKPOINTS_DDL,
    CLAIM_GENERATIONS_DDL,
    INTENTS_DDL,
    PLANS_DDL,
    RECEIPTS_DDL,
)
from local_dev_mcp.tools.gh.webhook import VerifiedTarget
from tests.support.gh_delivery import (
    DIGEST,
    DbOptions,
    connect,
    connection_options,
    delivery,
    install_procrastinate_schema,
    register_noop,
)


@dataclass
class FakeIssue:
    get_calls: int = 0
    post_calls: int = 0
    observed_labels: set[str] = field(default_factory=lambda: {"bug"})


@dataclass(frozen=True)
class LabelScopeCase:
    options: DbOptions
    schema: str
    conn: psycopg.AsyncConnection[tuple[Any, ...]]
    attempt_conn: psycopg.AsyncConnection[tuple[Any, ...]]
    plan: PlanStructure
    receipt: DeliveryReceipt
    worker_id: int
    started: int
    claim: ClaimGenerationRecord
    target: VerifiedTarget
    intent: OperationIntent
    checkpoint: OperationCheckpoint
    write_intent: OperationIntent
    scope: PostgresIssueLabelWriteScope
    writer: ConsumerOperationContract
    catalog: ContractCatalog
    bound: ConsumerOperationContract
    fake_get: Callable[[VerifiedTarget], Awaitable[bytes]]
    fake_post: Callable[[VerifiedTarget, str], Awaitable[object]]
    api: FakeIssue


@asynccontextmanager
async def prepared_label_scope() -> AsyncGenerator[LabelScopeCase]:
    options = connection_options()
    schema = "b3s_issue_read_" + uuid4().hex
    app = procrastinate.App(connector=procrastinate.PsycopgConnector())

    noop = register_noop(app, name="b3s_issue_read_noop")

    read = OperationNode("read", "github_issue", "read_current", "read:v1")
    add = OperationNode("label", "github_issue", "add_label", "add_label:v2", ("read",))
    plan = PlanStructure((read, add))
    # PostgreSQL advisory locks are database-wide, not schema-scoped.
    # Derive a valid, per-test Issue number from the isolated schema ID
    # to avoid cross-worker contention on the fixed (42, 123..132) keys.
    issue_number = 1000 + int(schema[-12:], 16) % (2**31 - 1000)
    api = FakeIssue()
    attempt_conn: psycopg.AsyncConnection[tuple[Any, ...]] | None = None

    async def fake_get(target: VerifiedTarget) -> bytes:
        api.get_calls += 1
        assert (target.kind, target.repository, target.repository_id, target.number) == (
            "issue",
            "acme/widget",
            42,
            issue_number,
        )
        return json.dumps(
            {
                "id": 9800,
                "number": issue_number,
                "repository_url": "https://api.github.com/repos/acme/widget",
                "state": "open",
                "updated_at": "2026-11-03T10:00:00Z",
                "labels": [{"name": name} for name in sorted(api.observed_labels)],
            },
            sort_keys=True,
        ).encode("utf-8")

    async def fake_post(target: VerifiedTarget, label: str) -> object:
        assert target.repository == "acme/widget" and label == "triaged"
        async with await connect(options) as contender:
            await contender.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
            lock = await contender.execute("SELECT pg_try_advisory_xact_lock(%s)", (issue_label_lock_key(target),))
            assert await lock.fetchone() == (False,), "Another session must not acquire the Issue lock"
            attempt_row = await contender.execute(
                "SELECT attempt_digest FROM github_operation_attempts WHERE operation_id = %s",
                ("label",),
            )
            assert await attempt_row.fetchone() is not None, "Attempt must already be committed before POST"
        api.post_calls += 1
        api.observed_labels.add(label)
        return None

    async with await connect(options) as conn:
        try:
            await conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
            await conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
            await install_procrastinate_schema(conn)
            for ddl in (RECEIPTS_DDL, PLANS_DDL, CLAIM_GENERATIONS_DDL, INTENTS_DDL, CHECKPOINTS_DDL, ATTEMPTS_DDL):
                await conn.execute(ddl)

            receipt = await accept_event(
                connection=conn, delivery=delivery("read-consumer", number=issue_number), task=noop
            )
            worker = await conn.execute("INSERT INTO procrastinate_workers DEFAULT VALUES RETURNING id")
            worker_row = await worker.fetchone()
            assert worker_row is not None
            worker_id = int(worker_row[0])
            await conn.execute(
                "UPDATE procrastinate_jobs SET status = 'doing', worker_id = %s WHERE id = %s",
                (worker_id, receipt.job_id),
            )
            observed = await inspect_running_claim(
                connection=conn,
                event_id=receipt.event_id,
                job_id=receipt.job_id,
                expected_task_name="b3s_issue_read_noop",
                worker_id=worker_id,
                attempts=0,
            )
            assert observed.started_event_id is not None
            started = observed.started_event_id
            claim = await record_claim_generation(
                connection=conn,
                event_id=receipt.event_id,
                job_id=receipt.job_id,
                expected_task_name="b3s_issue_read_noop",
                worker_id=worker_id,
                attempts=0,
                started_event_id=started,
            )
            await seal_plan_structure(connection=conn, event_id=receipt.event_id, plan=plan)
            target = await load_target(connection=conn, event_id=receipt.event_id)
            assert target is not None
            intent = make_issue_read_intent(operation=read, target=target)
            assert (
                await record_operation_intent(
                    connection=conn,
                    event_id=receipt.event_id,
                    generation=claim.generation,
                    job_id=receipt.job_id,
                    expected_task_name="b3s_issue_read_noop",
                    worker_id=worker_id,
                    attempts=0,
                    started_event_id=started,
                    expected_plan=plan,
                    intent=intent,
                )
            ).disposition == "recorded"

            bound = bind_issue_read_contract(
                intent=intent, target=target, transport=fake_get, test_evidence_digest=DIGEST
            )
            assert ContractCatalog((bound,)).inspect(read, intent=intent).compatible
            assert api.get_calls == 0
            assert bound.concurrency.guard is not None
            assert bound.execute is not None
            assert bound.decode is not None
            assert bound.verify is not None
            await bound.concurrency.guard(intent)
            raw = await bound.execute(intent)
            assert type(raw) is bytes
            checkpoint = bound.decode(raw)
            assert await bound.verify(intent, checkpoint)
            assert api.get_calls == 2

            persisted = await record_operation_checkpoint(
                connection=conn,
                event_id=receipt.event_id,
                generation=claim.generation,
                job_id=receipt.job_id,
                expected_task_name="b3s_issue_read_noop",
                worker_id=worker_id,
                attempts=0,
                started_event_id=started,
                expected_plan=plan,
                checkpoint=checkpoint,
            )
            assert persisted.disposition == "recorded"
            assert persisted.checkpoint_digest == checkpoint.checkpoint_digest
            assert (
                await load_operation_checkpoint(connection=conn, event_id=receipt.event_id, operation_id="read")
            ) is not None
            write_intent, write_lineage = make_issue_add_label_intent(
                operation=add,
                target=target,
                label="triaged",
                source_intent=intent,
                source_checkpoint=checkpoint,
            )
            assert (
                await record_operation_intent(
                    connection=conn,
                    event_id=receipt.event_id,
                    generation=claim.generation,
                    job_id=receipt.job_id,
                    expected_task_name="b3s_issue_read_noop",
                    worker_id=worker_id,
                    attempts=0,
                    started_event_id=started,
                    expected_plan=plan,
                    intent=write_intent,
                    lineage=write_lineage,
                )
            ).disposition == "recorded"
            assert (
                await load_operation_lineage(connection=conn, event_id=receipt.event_id, operation_id="label")
            ) is not None

            attempt_conn = await connect(options)
            await attempt_conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
            scope = PostgresIssueLabelWriteScope(
                connection=conn,
                attempt_connection=attempt_conn,
                target=target,
                event_id=receipt.event_id,
                generation=claim.generation,
                job_id=receipt.job_id,
                expected_task_name="b3s_issue_read_noop",
                worker_id=worker_id,
                attempts=0,
                started_event_id=started,
                expected_plan=plan,
            )
            writer = bind_issue_add_label_contract(
                intent=write_intent,
                target=target,
                label="triaged",
                source_intent=intent,
                source_checkpoint=checkpoint,
                observe=fake_get,
                add_label=fake_post,
                write_scope=scope,
                test_evidence_digest=DIGEST,
            )
            catalog = ContractCatalog((bound, writer))
            assert catalog.inspect_plan(plan, intents=(intent, write_intent)).compatible
            assert writer.concurrency.guard is not None
            assert writer.execute is not None
            assert writer.decode is not None
            assert writer.verify is not None
            with pytest.raises(IssueLabelHold, match="claim_or_plan_invalid"):
                async with replace(scope, expected_task_name="invalid")(write_intent):
                    pytest.fail("A wrong active task must not enter the write interval")
            with pytest.raises(IssueLabelHold, match="receipt_target_mismatch"):
                async with replace(scope, target=replace(target, number=target.number + 1))(write_intent):
                    pytest.fail("An unrelated Issue must not enter the write interval")

            assert attempt_conn is not None
            yield LabelScopeCase(
                options=options,
                schema=schema,
                conn=conn,
                attempt_conn=attempt_conn,
                plan=plan,
                receipt=receipt,
                worker_id=worker_id,
                started=started,
                claim=claim,
                target=target,
                intent=intent,
                checkpoint=checkpoint,
                write_intent=write_intent,
                scope=scope,
                writer=writer,
                catalog=catalog,
                bound=bound,
                fake_get=fake_get,
                fake_post=fake_post,
                api=api,
            )
        finally:
            if attempt_conn is not None:
                await attempt_conn.close()
            await conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))


async def assert_attempt_failure_is_atomic(case: LabelScopeCase) -> tuple[ConsumerOperationContract, ContractCatalog]:
    """Exercise the shared failed-INSERT boundary before each later fault."""
    conn = case.conn
    receipt = case.receipt
    write_intent = case.write_intent
    scope = case.scope
    target = case.target
    intent = case.intent
    checkpoint = case.checkpoint
    fake_get = case.fake_get
    fake_post = case.fake_post
    bound = case.bound
    catalog = case.catalog
    writer = case.writer
    api = case.api
    assert writer.concurrency.guard is not None and writer.execute is not None
    await conn.execute("ALTER TABLE github_operation_attempts RENAME TO hidden_operation_attempts")
    with pytest.raises(IssueLabelHold, match="attempt_store_unavailable"):
        await writer.concurrency.guard(write_intent)
    missing = await inspect_operation_recovery_evidence(
        connection=conn, catalog=catalog, event_id=receipt.event_id, operation_id="label"
    )
    assert missing.status == "hold" and missing.reason == "evidence_store_unavailable"
    await conn.execute("ALTER TABLE hidden_operation_attempts RENAME TO github_operation_attempts")
    await writer.concurrency.guard(write_intent)
    assert api.post_calls == 0
    assert await load_operation_attempt(connection=conn, event_id=receipt.event_id, operation_id="label") is None
    # The second connection's INSERT must roll back and block POST
    # when PostgreSQL rejects the Attempt. No retry on the same
    # binding; a fresh binding can only proceed after correction.
    await conn.execute(
        "ALTER TABLE github_operation_attempts ADD CONSTRAINT test_reject_attempt CHECK (false) NOT VALID"
    )
    with pytest.raises(IssueLabelHold, match="write_scope_attempt_commit_failed"):
        assert writer.execute is not None
        await writer.execute(write_intent)
    assert api.post_calls == 0
    assert await load_operation_attempt(connection=conn, event_id=receipt.event_id, operation_id="label") is None
    await conn.execute("ALTER TABLE github_operation_attempts DROP CONSTRAINT test_reject_attempt")
    writer = bind_issue_add_label_contract(
        intent=write_intent,
        target=target,
        label="triaged",
        source_intent=intent,
        source_checkpoint=checkpoint,
        observe=fake_get,
        add_label=fake_post,
        write_scope=scope,
        test_evidence_digest=DIGEST,
    )
    catalog = ContractCatalog((bound, writer))
    assert writer.execute is not None and writer.decode is not None and writer.verify is not None
    return writer, catalog
