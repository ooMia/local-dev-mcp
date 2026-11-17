"""Append-only plan and intent persistence under claim ownership."""

import asyncio
import hashlib
import json
from dataclasses import replace
from typing import Any
from uuid import uuid4

import procrastinate
import psycopg
import pytest
from psycopg import sql

from local_dev_mcp.tools.gh.delivery import (
    ConsumedOutput,
    IntentConflictError,
    IntentHold,
    IntentIntegrityError,
    IntentRecord,
    OperationIntent,
    OperationNode,
    PlanConflictError,
    PlanIntegrityError,
    PlanStructure,
    accept_event,
    inspect_running_claim,
    load_operation_intent,
    load_sealed_plan,
    record_claim_generation,
    record_operation_intent,
    seal_plan_structure,
)
from local_dev_mcp.tools.gh.delivery.schema import (
    CLAIM_GENERATIONS_DDL,
    INTENTS_DDL,
    PLANS_DDL,
    RECEIPTS_DDL,
)
from tests.support.gh_delivery import (
    DIGEST,
    connect,
    connection_options,
    delivery,
    install_procrastinate_schema,
    register_noop,
    requeue_job,
)

pytestmark = pytest.mark.integration


def test_sealed_plan_is_atomic_append_only_and_concurrency_safe() -> None:
    """Every DDL operation is isolated in a test-owned temporary schema."""
    options = connection_options()
    schema = "b3s_sealed_" + uuid4().hex

    async def run() -> None:
        app = procrastinate.App(connector=procrastinate.PsycopgConnector())

        no_op = register_noop(app, name="b3s_sealed_plan_noop")

        plan = PlanStructure(
            (
                OperationNode("read", "github_issue", "read_current", "read:v1"),
                OperationNode("prepare", "github_issue", "prepare_change", "prepare:v1", ("read",)),
            )
        )
        alternative = PlanStructure((OperationNode("different", "github_issue", "read_current", "read:v1"),))

        async with await connect(options) as conn:
            try:
                await conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
                await conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
                await install_procrastinate_schema(conn)
                await conn.execute(RECEIPTS_DDL)
                await conn.execute(PLANS_DDL)

                accepted = await accept_event(connection=conn, delivery=delivery("plan-1"), task=no_op)
                event_id = accepted.event_id
                assert await load_sealed_plan(connection=conn, event_id=event_id) is None

                async with await connect(options) as second:
                    await second.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
                    a, b = await asyncio.gather(
                        seal_plan_structure(connection=conn, event_id=event_id, plan=plan),
                        seal_plan_structure(connection=second, event_id=event_id, plan=plan),
                    )
                    assert {a.disposition, b.disposition} == {"sealed", "existing"}
                    assert a.structure_digest == b.structure_digest == plan.structure_digest
                    assert a.structure_bytes == b.structure_bytes == plan.canonical_bytes()
                    assert (a.event_id, b.event_id) == (event_id, event_id)

                with pytest.raises(PlanConflictError):
                    await seal_plan_structure(connection=conn, event_id=event_id, plan=alternative)
                stored = await conn.execute("SELECT count(*) FROM github_sealed_plans")
                assert await stored.fetchone() == (1,)

                await conn.execute("SET default_transaction_read_only = on")
                read_back = await load_sealed_plan(connection=conn, event_id=event_id)
                assert read_back is not None and read_back.disposition == "existing"
                assert read_back.structure_bytes == plan.canonical_bytes()
                assert read_back.structure_digest == plan.structure_digest
                with pytest.raises(ValueError):
                    await _invalid_nested_seal(conn, event_id, plan)
                await conn.execute("SET default_transaction_read_only = off")

                # A second event can seal a different structure, but the
                # previously sealed plan can never be replaced or deleted.
                other = await accept_event(connection=conn, delivery=delivery("plan-2"), task=no_op)
                sealed_other = await seal_plan_structure(connection=conn, event_id=other.event_id, plan=alternative)
                assert sealed_other.disposition == "sealed"
                for statement in (
                    "UPDATE github_sealed_plans SET structure_digest = 'sha256:wrong' WHERE event_id = %s",
                    "DELETE FROM github_sealed_plans WHERE event_id = %s",
                ):
                    with pytest.raises(psycopg.Error):
                        await conn.execute(statement, (event_id,))
                count = await conn.execute("SELECT count(*) FROM github_sealed_plans")
                assert await count.fetchone() == (2,)

                # Test-only privileged corruption: rehash deliberately invalid
                # structure bytes. The checksum alone must not make them valid.
                await conn.execute("ALTER TABLE github_sealed_plans DISABLE TRIGGER github_sealed_plan_immutable")
                malformed_structures = [b"{}", b"non-json"]
                as_json = json.loads(plan.canonical_bytes())
                as_json["operations"][0]["depends_on"] = ["prepare"]
                malformed_structures.append(json.dumps(as_json, sort_keys=True, separators=(",", ":")).encode("utf-8"))
                for corrupted in malformed_structures:
                    await conn.execute(
                        "UPDATE github_sealed_plans SET structure_bytes = %s, structure_digest = %s "
                        "WHERE event_id = %s",
                        (corrupted, "sha256:" + hashlib.sha256(corrupted).hexdigest(), event_id),
                    )
                    with pytest.raises(PlanIntegrityError):
                        await load_sealed_plan(connection=conn, event_id=event_id)
                    with pytest.raises(PlanIntegrityError):
                        await seal_plan_structure(connection=conn, event_id=event_id, plan=plan)

                await conn.execute(
                    "UPDATE github_sealed_plans SET structure_bytes = %s, structure_digest = %s WHERE event_id = %s",
                    (plan.canonical_bytes(), plan.structure_digest, event_id),
                )
                await conn.execute("ALTER TABLE github_sealed_plans ENABLE TRIGGER github_sealed_plan_immutable")
                restored = await load_sealed_plan(connection=conn, event_id=event_id)
                assert restored is not None and restored.structure_digest == plan.structure_digest

                with pytest.raises(PlanIntegrityError):
                    await seal_plan_structure(connection=conn, event_id=event_id + 100000, plan=plan)
                with pytest.raises(ValueError):
                    await load_sealed_plan(connection=conn, event_id=True)
            finally:
                await conn.execute("SET default_transaction_read_only = off")
                await conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))

    asyncio.run(run())


async def _invalid_nested_seal(
    conn: psycopg.AsyncConnection[tuple[Any, ...]], event_id: int, plan: PlanStructure
) -> None:
    async with conn.transaction():
        await seal_plan_structure(connection=conn, event_id=event_id, plan=plan)


def test_immutable_intent_is_event_scoped_and_claim_gated() -> None:
    """Only a test-owned schema is modified; no Worker or GitHub API starts."""
    options = connection_options()
    schema = "b3s_intent_" + uuid4().hex

    async def run() -> None:
        app = procrastinate.App(connector=procrastinate.PsycopgConnector())

        noop = register_noop(app, name="b3s_intent_noop")

        read = OperationNode("read", "github_issue", "read_current", "read:v1")
        downstream = OperationNode("prepare", "github_issue", "prepare_change", "prepare:v1", ("read",))
        plan = PlanStructure((read, downstream))
        intent = OperationIntent(
            operation=read,
            input_digest=DIGEST,
            effect_digest="sha256:" + "b" * 64,
            preconditions_digest="sha256:" + "c" * 64,
            postconditions_digest="sha256:" + "d" * 64,
            retry_safety_rule="hold_without_probe",
            probe_contract="github_readonly:v1",
        )
        altered = replace(intent, effect_digest="sha256:" + "f" * 64)
        dependent = OperationIntent(
            operation=downstream,
            input_digest=DIGEST,
            effect_digest="sha256:" + "b" * 64,
            preconditions_digest="sha256:" + "c" * 64,
            postconditions_digest="sha256:" + "d" * 64,
            retry_safety_rule="hold_without_probe",
            probe_contract="github_readonly:v1",
            consumed_outputs=(ConsumedOutput("read", DIGEST),),
        )

        async with await connect(options) as conn:
            try:
                await conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
                await conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
                await install_procrastinate_schema(conn)
                await conn.execute(RECEIPTS_DDL)
                await conn.execute(PLANS_DDL)
                await conn.execute(CLAIM_GENERATIONS_DDL)
                await conn.execute(INTENTS_DDL)
                receipt = await accept_event(connection=conn, delivery=delivery("intent-1"), task=noop)
                event_id, job_id = receipt.event_id, receipt.job_id

                worker = await conn.execute("INSERT INTO procrastinate_workers DEFAULT VALUES RETURNING id")
                worker_row = await worker.fetchone()
                assert worker_row is not None
                worker_id = int(worker_row[0])

                assert await load_operation_intent(connection=conn, event_id=event_id, operation_id="read") is None
                with pytest.raises(IntentHold):
                    await record_operation_intent(
                        connection=conn,
                        event_id=event_id,
                        generation=1,
                        job_id=job_id,
                        expected_task_name="b3s_intent_noop",
                        worker_id=worker_id,
                        attempts=0,
                        started_event_id=1,
                        expected_plan=plan,
                        intent=intent,
                    )

                await conn.execute(
                    "UPDATE procrastinate_jobs SET status = 'doing', worker_id = %s WHERE id = %s",
                    (worker_id, job_id),
                )
                observed = await inspect_running_claim(
                    connection=conn,
                    event_id=event_id,
                    job_id=job_id,
                    expected_task_name="b3s_intent_noop",
                    worker_id=worker_id,
                    attempts=0,
                )
                assert observed.status == "observed" and observed.started_event_id is not None
                started_id = observed.started_event_id
                await record_claim_generation(
                    connection=conn,
                    event_id=event_id,
                    job_id=job_id,
                    expected_task_name="b3s_intent_noop",
                    worker_id=worker_id,
                    attempts=0,
                    started_event_id=started_id,
                )

                async def persist(
                    connection: psycopg.AsyncConnection[tuple[Any, ...]],
                    *,
                    expected_intent: OperationIntent = intent,
                    claim_generation: int = 1,
                    active_worker: int = worker_id,
                    active_start: int = started_id,
                    expected_structure: PlanStructure = plan,
                ) -> IntentRecord:
                    return await record_operation_intent(
                        connection=connection,
                        event_id=event_id,
                        generation=claim_generation,
                        job_id=job_id,
                        expected_task_name="b3s_intent_noop",
                        worker_id=active_worker,
                        attempts=0,
                        started_event_id=active_start,
                        expected_plan=expected_structure,
                        intent=expected_intent,
                    )

                with pytest.raises(IntentHold, match="sealed"):
                    await persist(conn)
                await seal_plan_structure(connection=conn, event_id=event_id, plan=plan)
                with pytest.raises(IntentHold, match="Checkpoint"):
                    await persist(conn, expected_intent=dependent)
                with pytest.raises(IntentHold):
                    await persist(conn, expected_structure=PlanStructure((read,)))

                async with await connect(options) as other:
                    await other.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
                    a, b = await asyncio.gather(persist(conn), persist(other))
                    assert {a.disposition, b.disposition} == {"recorded", "existing"}
                    assert a.intent_digest == b.intent_digest == intent.intent_digest
                    assert a.created_generation == b.created_generation == 1
                    assert (await persist(other)).disposition == "existing"

                with pytest.raises(IntentConflictError):
                    await persist(conn, expected_intent=altered)
                with pytest.raises(IntentHold):
                    await persist(conn, claim_generation=2)

                await conn.execute("SET default_transaction_read_only = on")
                loaded = await load_operation_intent(connection=conn, event_id=event_id, operation_id="read")
                assert loaded is not None
                assert loaded.intent_bytes == intent.canonical_bytes()
                assert loaded.structure_digest == plan.structure_digest
                assert loaded.created_generation == 1 and loaded.disposition == "existing"
                with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
                    await persist(conn)
                await conn.execute("SET default_transaction_read_only = off")

                # Procrastinate's state change creates a new start event.
                await requeue_job(conn, job_id)
                with pytest.raises(IntentHold):
                    await persist(conn)
                await conn.execute(
                    "UPDATE procrastinate_jobs SET status = 'doing', worker_id = %s WHERE id = %s",
                    (worker_id, job_id),
                )
                next_observation = await inspect_running_claim(
                    connection=conn,
                    event_id=event_id,
                    job_id=job_id,
                    expected_task_name="b3s_intent_noop",
                    worker_id=worker_id,
                    attempts=0,
                )
                assert next_observation.started_event_id is not None
                newer_start = next_observation.started_event_id
                assert newer_start > started_id
                with pytest.raises(IntentHold):
                    await persist(conn, active_start=newer_start)
                generation2 = await record_claim_generation(
                    connection=conn,
                    event_id=event_id,
                    job_id=job_id,
                    expected_task_name="b3s_intent_noop",
                    worker_id=worker_id,
                    attempts=0,
                    started_event_id=newer_start,
                )
                assert generation2.generation == 2
                existing = await persist(conn, claim_generation=2, active_start=newer_start)
                assert existing.disposition == "existing" and existing.created_generation == 1
                with pytest.raises(IntentConflictError):
                    await persist(conn, claim_generation=2, active_start=newer_start, expected_intent=altered)

                count = await conn.execute("SELECT count(*) FROM github_operation_intents")
                assert await count.fetchone() == (1,)
                for statement in (
                    "UPDATE github_operation_intents SET created_generation = 2 WHERE event_id = %s",
                    "DELETE FROM github_operation_intents WHERE event_id = %s",
                ):
                    with pytest.raises(psycopg.Error):
                        await conn.execute(statement, (event_id,))

                # Test-only privileged corruption. A self-consistent Intent
                # with a mismatched Plan link or Operation must still fail
                # the read-only integrity check.
                await conn.execute(
                    "ALTER TABLE github_operation_intents DISABLE TRIGGER github_operation_intent_immutable"
                )
                await conn.execute(
                    "UPDATE github_operation_intents SET structure_digest = %s WHERE event_id = %s",
                    ("sha256:" + "f" * 64, event_id),
                )
                with pytest.raises(IntentIntegrityError, match="sealed Plan"):
                    await load_operation_intent(connection=conn, event_id=event_id, operation_id="read")
                await conn.execute(
                    "UPDATE github_operation_intents SET structure_digest = %s WHERE event_id = %s",
                    (plan.structure_digest, event_id),
                )

                rogue = replace(intent, operation=replace(read, operation_type="other_read"))
                await conn.execute(
                    "UPDATE github_operation_intents SET intent_bytes = %s, intent_digest = %s WHERE event_id = %s",
                    (rogue.canonical_bytes(), rogue.intent_digest, event_id),
                )
                with pytest.raises(IntentIntegrityError, match="absent"):
                    await load_operation_intent(connection=conn, event_id=event_id, operation_id="read")

                # Even valid SHA-256 of malformed canonical evidence is rejected.
                invalid = b"{}"
                await conn.execute(
                    "UPDATE github_operation_intents SET intent_bytes = %s, intent_digest = %s WHERE event_id = %s",
                    (invalid, "sha256:" + hashlib.sha256(invalid).hexdigest(), event_id),
                )
                with pytest.raises(IntentIntegrityError):
                    await load_operation_intent(connection=conn, event_id=event_id, operation_id="read")
                with pytest.raises(IntentIntegrityError):
                    await persist(conn, claim_generation=2, active_start=newer_start)
            finally:
                await conn.execute("SET default_transaction_read_only = off")
                await conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))

    asyncio.run(run())
