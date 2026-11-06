"""Atomic checkpoint/lineage persistence and corruption rejection."""

import asyncio
import hashlib
from dataclasses import replace
from typing import Any
from uuid import uuid4

import procrastinate
import psycopg
import pytest
from psycopg import sql

from local_dev_mcp.tools.gh.delivery import (
    CheckpointConflictError,
    CheckpointField,
    CheckpointHold,
    CheckpointIntegrityError,
    ConcurrencyProtection,
    ConsumedOutput,
    ConsumerOperationContract,
    ContractCatalog,
    IntentHold,
    IntentIntegrityError,
    LineageBinding,
    LineageConflictError,
    LineageIntegrityError,
    OperationCheckpoint,
    OperationIntent,
    OperationLineage,
    OperationNode,
    PlanStructure,
    accept_event,
    inspect_operation_recovery_evidence,
    inspect_persisted_delivery_contracts,
    inspect_running_claim,
    load_operation_checkpoint,
    load_operation_intent,
    load_operation_lineage,
    record_claim_generation,
    record_operation_checkpoint,
    record_operation_intent,
    seal_plan_structure,
)
from local_dev_mcp.tools.gh.delivery.schema import (
    CHECKPOINTS_DDL,
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


def test_checkpoint_output_and_dependent_lineage_are_immutable_and_atomic() -> None:
    """PostgreSQL evidence-only Q71/Q73 exercise; no GitHub API effect."""
    options = connection_options()
    schema = "b3s_checkpoint_" + uuid4().hex

    async def run() -> None:
        app = procrastinate.App(connector=procrastinate.PsycopgConnector())

        noop = register_noop(app, name="b3s_checkpoint_noop")

        root = OperationNode("read", "github_issue", "read_current", "read:v1")
        dependent = OperationNode("prepare", "github_issue", "prepare", "prepare:v1", ("read",))
        downstream = OperationNode("finish", "github_issue", "finish", "finish:v1", ("prepare",))
        plan = PlanStructure((root, dependent, downstream))
        root_intent = OperationIntent(
            operation=root,
            input_digest=DIGEST,
            effect_digest="sha256:" + "b" * 64,
            preconditions_digest="sha256:" + "c" * 64,
            postconditions_digest="sha256:" + "d" * 64,
            retry_safety_rule="hold_without_probe",
            probe_contract="github_readonly:v1",
        )
        output = OperationCheckpoint(
            operation_id="read",
            intent_digest=root_intent.intent_digest,
            output_schema_version="github_issue_read:v1",
            fields=(CheckpointField("status", "sha256:" + "e" * 64),),
            verification_contract="github_readonly:v1",
            verification_evidence_digest="sha256:" + "f" * 64,
        )
        dependent_intent = OperationIntent(
            operation=dependent,
            input_digest=DIGEST,
            effect_digest="sha256:" + "b" * 64,
            preconditions_digest="sha256:" + "c" * 64,
            postconditions_digest="sha256:" + "d" * 64,
            retry_safety_rule="hold_without_probe",
            probe_contract="github_readonly:v1",
            consumed_outputs=(ConsumedOutput("read", output.checkpoint_digest),),
        )
        dependent_output = OperationCheckpoint(
            operation_id="prepare",
            intent_digest=dependent_intent.intent_digest,
            output_schema_version="github_issue_prepare:v1",
            fields=(CheckpointField("prepared", DIGEST),),
            verification_contract="github_readonly:v1",
            verification_evidence_digest="sha256:" + "f" * 64,
        )
        binding = LineageBinding(
            source_operation_id="read",
            source_checkpoint_digest=output.checkpoint_digest,
            source_field_id="status",
            source_field_digest="sha256:" + "e" * 64,
            source_schema_version="github_issue_read:v1",
            target_field_id="desired_status",
            binding_version="status_binding:v1",
        )
        lineage = OperationLineage((binding,))
        downstream_intent = OperationIntent(
            operation=downstream,
            input_digest=DIGEST,
            effect_digest="sha256:" + "b" * 64,
            preconditions_digest="sha256:" + "c" * 64,
            postconditions_digest="sha256:" + "d" * 64,
            retry_safety_rule="hold_without_probe",
            probe_contract="github_readonly:v1",
            consumed_outputs=(ConsumedOutput("prepare", dependent_output.checkpoint_digest),),
        )
        downstream_lineage = OperationLineage(
            (
                LineageBinding(
                    source_operation_id="prepare",
                    source_checkpoint_digest=dependent_output.checkpoint_digest,
                    source_field_id="prepared",
                    source_field_digest=DIGEST,
                    source_schema_version="github_issue_prepare:v1",
                    target_field_id="ready",
                    binding_version="prepared_binding:v1",
                ),
            )
        )

        # Static deployment compatibility must never invoke Consumer hooks.
        async def never_execute(_: OperationIntent) -> object:
            raise AssertionError("Deployment preflight invoked execute")

        async def never_probe(_: OperationIntent) -> object:
            raise AssertionError("Deployment preflight invoked probe")

        def never_decode(_: bytes) -> OperationCheckpoint:
            raise AssertionError("Deployment preflight invoked decode")

        async def never_verify(_: OperationIntent, __: OperationCheckpoint) -> bool:
            raise AssertionError("Deployment preflight invoked verify")

        async def never_guard(_: OperationIntent) -> object:
            raise AssertionError("Deployment preflight invoked concurrency guard")

        catalog = ContractCatalog(
            tuple(
                ConsumerOperationContract(
                    consumer_id=node.consumer_id,
                    operation_type=node.operation_type,
                    contract_version=node.contract_version,
                    retry_safety_rule="hold_without_probe",
                    probe_contract="github_readonly:v1",
                    concurrency=ConcurrencyProtection(
                        "conditional_remote:v1",
                        "conditional_remote:v1",
                        DIGEST,
                        never_guard,
                    ),
                    execute=never_execute,
                    probe=never_probe,
                    decode=never_decode,
                    verify=never_verify,
                )
                for node in plan.operations
            )
        )

        async with await connect(options) as conn:
            try:
                await conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
                await conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
                await install_procrastinate_schema(conn)
                for ddl in (RECEIPTS_DDL, PLANS_DDL, CLAIM_GENERATIONS_DDL, INTENTS_DDL, CHECKPOINTS_DDL):
                    await conn.execute(ddl)
                receipt = await accept_event(connection=conn, delivery=delivery("checkpoint-1"), task=noop)
                event_id, job_id = receipt.event_id, receipt.job_id
                worker = await conn.execute("INSERT INTO procrastinate_workers DEFAULT VALUES RETURNING id")
                worker_row = await worker.fetchone()
                assert worker_row is not None
                worker_id = int(worker_row[0])
                await conn.execute(
                    "UPDATE procrastinate_jobs SET status = 'doing', worker_id = %s WHERE id = %s",
                    (worker_id, job_id),
                )
                observed = await inspect_running_claim(
                    connection=conn,
                    event_id=event_id,
                    job_id=job_id,
                    expected_task_name="b3s_checkpoint_noop",
                    worker_id=worker_id,
                    attempts=0,
                )
                assert observed.started_event_id is not None
                started_id = observed.started_event_id
                await record_claim_generation(
                    connection=conn,
                    event_id=event_id,
                    job_id=job_id,
                    expected_task_name="b3s_checkpoint_noop",
                    worker_id=worker_id,
                    attempts=0,
                    started_event_id=started_id,
                )
                await seal_plan_structure(connection=conn, event_id=event_id, plan=plan)

                async def persist_checkpoint(
                    connection: psycopg.AsyncConnection[tuple[Any, ...]],
                    *,
                    value: OperationCheckpoint = output,
                    claim_generation: int = 1,
                    active_start: int = started_id,
                ) -> Any:
                    return await record_operation_checkpoint(
                        connection=connection,
                        event_id=event_id,
                        generation=claim_generation,
                        job_id=job_id,
                        expected_task_name="b3s_checkpoint_noop",
                        worker_id=worker_id,
                        attempts=0,
                        started_event_id=active_start,
                        expected_plan=plan,
                        checkpoint=value,
                    )

                async def persist_dependent(
                    connection: psycopg.AsyncConnection[tuple[Any, ...]],
                    *,
                    bindings: OperationLineage = lineage,
                    claim_generation: int = 1,
                    active_start: int = started_id,
                ) -> Any:
                    return await record_operation_intent(
                        connection=connection,
                        event_id=event_id,
                        generation=claim_generation,
                        job_id=job_id,
                        expected_task_name="b3s_checkpoint_noop",
                        worker_id=worker_id,
                        attempts=0,
                        started_event_id=active_start,
                        expected_plan=plan,
                        intent=dependent_intent,
                        lineage=bindings,
                    )

                assert await load_operation_checkpoint(connection=conn, event_id=event_id, operation_id="read") is None
                with pytest.raises(CheckpointHold, match="Intent"):
                    await persist_checkpoint(conn)

                # Root Operations must reject even an empty explicit lineage;
                # the writer must not create evidence that preflight will HOLD.
                async def persist_root_with_lineage() -> Any:
                    return await record_operation_intent(
                        connection=conn,
                        event_id=event_id,
                        generation=1,
                        job_id=job_id,
                        expected_task_name="b3s_checkpoint_noop",
                        worker_id=worker_id,
                        attempts=0,
                        started_event_id=started_id,
                        expected_plan=plan,
                        intent=root_intent,
                        lineage=OperationLineage(()),
                    )

                with pytest.raises(IntentHold, match="Root Operation"):
                    await persist_root_with_lineage()
                rows = await conn.execute(
                    "SELECT count(*) FROM github_operation_intents WHERE event_id = %s", (event_id,)
                )
                assert await rows.fetchone() == (0,)

                await record_operation_intent(
                    connection=conn,
                    event_id=event_id,
                    generation=1,
                    job_id=job_id,
                    expected_task_name="b3s_checkpoint_noop",
                    worker_id=worker_id,
                    attempts=0,
                    started_event_id=started_id,
                    expected_plan=plan,
                    intent=root_intent,
                )
                with pytest.raises(IntentHold, match="Root Operation"):
                    await persist_root_with_lineage()
                root_lineages = await conn.execute(
                    "SELECT count(*) FROM github_operation_lineages WHERE event_id = %s", (event_id,)
                )
                assert await root_lineages.fetchone() == (0,)
                with pytest.raises(IntentHold, match="lineage"):
                    await record_operation_intent(
                        connection=conn,
                        event_id=event_id,
                        generation=1,
                        job_id=job_id,
                        expected_task_name="b3s_checkpoint_noop",
                        worker_id=worker_id,
                        attempts=0,
                        started_event_id=started_id,
                        expected_plan=plan,
                        intent=dependent_intent,
                    )
                with pytest.raises(IntentHold, match="Checkpoint"):
                    await persist_dependent(conn)

                async with await connect(options) as other:
                    await other.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
                    a, b = await asyncio.gather(persist_checkpoint(conn), persist_checkpoint(other))
                    assert {a.disposition, b.disposition} == {"recorded", "existing"}
                    assert a.checkpoint_digest == b.checkpoint_digest == output.checkpoint_digest
                    assert a.recorded_generation == b.recorded_generation == 1

                with pytest.raises(CheckpointConflictError):
                    await persist_checkpoint(conn, value=replace(output, verification_evidence_digest=DIGEST))
                mismatched = OperationLineage((replace(binding, source_field_id="missing"),))
                with pytest.raises(IntentHold, match="field"):
                    await persist_dependent(conn, bindings=mismatched)
                absent = await conn.execute(
                    "SELECT count(*) FROM github_operation_intents WHERE event_id = %s AND operation_id = 'prepare'",
                    (event_id,),
                )
                assert await absent.fetchone() == (0,)
                assert (await persist_dependent(conn)).disposition == "recorded"
                assert (await persist_dependent(conn)).disposition == "existing"
                assert (
                    await load_operation_lineage(connection=conn, event_id=event_id, operation_id="prepare") is not None
                )
                with pytest.raises(LineageConflictError):
                    await persist_dependent(
                        conn, bindings=OperationLineage((replace(binding, target_field_id="alternate_status"),))
                    )
                count = await conn.execute(
                    "SELECT count(*) FROM github_operation_lineages WHERE event_id = %s", (event_id,)
                )
                assert await count.fetchone() == (1,)
                assert (
                    await load_operation_intent(connection=conn, event_id=event_id, operation_id="prepare")
                ) is not None

                # A dependent Intent and its lineage are atomic evidence:
                # test-only deletion cannot make an incomplete Intent loadable.
                await conn.execute(
                    "ALTER TABLE github_operation_lineages DISABLE TRIGGER github_operation_lineage_immutable"
                )
                await conn.execute(
                    "DELETE FROM github_operation_lineages WHERE event_id = %s AND operation_id = 'prepare'",
                    (event_id,),
                )
                with pytest.raises(IntentIntegrityError, match="no atomic lineage"):
                    await load_operation_intent(connection=conn, event_id=event_id, operation_id="prepare")
                with pytest.raises(CheckpointHold, match="no atomic lineage"):
                    await persist_checkpoint(conn, value=dependent_output)
                missing_checkpoint = await conn.execute(
                    "SELECT count(*) FROM github_operation_checkpoints "
                    "WHERE event_id = %s AND operation_id = 'prepare'",
                    (event_id,),
                )
                assert await missing_checkpoint.fetchone() == (0,)
                await conn.execute(
                    """
                    INSERT INTO github_operation_lineages
                        (event_id, operation_id, lineage_version, lineage_digest, lineage_bytes)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (event_id, "prepare", lineage.lineage_version, lineage.lineage_digest, lineage.canonical_bytes()),
                )
                await conn.execute(
                    "ALTER TABLE github_operation_lineages ENABLE TRIGGER github_operation_lineage_immutable"
                )
                assert (
                    await load_operation_intent(connection=conn, event_id=event_id, operation_id="prepare")
                ) is not None
                assert (await persist_checkpoint(conn, value=dependent_output)).disposition == "recorded"
                assert (await persist_checkpoint(conn, value=dependent_output)).disposition == "existing"
                assert (
                    await load_operation_checkpoint(connection=conn, event_id=event_id, operation_id="prepare")
                ) is not None

                async def persist_downstream() -> Any:
                    return await record_operation_intent(
                        connection=conn,
                        event_id=event_id,
                        generation=1,
                        job_id=job_id,
                        expected_task_name="b3s_checkpoint_noop",
                        worker_id=worker_id,
                        attempts=0,
                        started_event_id=started_id,
                        expected_plan=plan,
                        intent=downstream_intent,
                        lineage=downstream_lineage,
                    )

                assert (await persist_downstream()).disposition == "recorded"
                assert (
                    await load_operation_lineage(connection=conn, event_id=event_id, operation_id="finish")
                ) is not None

                # A Checkpoint must not remain loadable if the atomically
                # recorded dependent lineage is subsequently lost.
                await conn.execute(
                    "ALTER TABLE github_operation_lineages DISABLE TRIGGER github_operation_lineage_immutable"
                )
                await conn.execute(
                    "DELETE FROM github_operation_lineages WHERE event_id = %s AND operation_id = 'prepare'",
                    (event_id,),
                )
                with pytest.raises(CheckpointIntegrityError, match="lineage"):
                    await load_operation_checkpoint(connection=conn, event_id=event_id, operation_id="prepare")
                # The direct source Checkpoint still exists, but its own
                # lineage is missing. The grandchild must fail closed too.
                with pytest.raises(LineageIntegrityError, match="source evidence"):
                    await load_operation_lineage(connection=conn, event_id=event_id, operation_id="finish")
                with pytest.raises(IntentHold, match="lineage"):
                    await persist_downstream()
                await conn.execute(
                    """
                    INSERT INTO github_operation_lineages
                        (event_id, operation_id, lineage_version, lineage_digest, lineage_bytes)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (event_id, "prepare", lineage.lineage_version, lineage.lineage_digest, lineage.canonical_bytes()),
                )
                await conn.execute(
                    "ALTER TABLE github_operation_lineages ENABLE TRIGGER github_operation_lineage_immutable"
                )
                assert (
                    await load_operation_checkpoint(connection=conn, event_id=event_id, operation_id="prepare")
                ) is not None

                compatible = await inspect_persisted_delivery_contracts(
                    connection=conn,
                    catalog=catalog,
                    event_id=event_id,
                )
                assert compatible.status == "compatible" and compatible.reason is None

                # The lineage reader must validate the dependent Intent's
                # persisted Plan digest, not merely its canonical bytes.
                await conn.execute(
                    "ALTER TABLE github_operation_intents DISABLE TRIGGER github_operation_intent_immutable"
                )
                await conn.execute(
                    "UPDATE github_operation_intents SET structure_digest = %s "
                    "WHERE event_id = %s AND operation_id = 'prepare'",
                    ("sha256:" + "f" * 64, event_id),
                )
                with pytest.raises(LineageIntegrityError, match="sealed Plan"):
                    await load_operation_lineage(connection=conn, event_id=event_id, operation_id="prepare")
                await conn.execute(
                    "UPDATE github_operation_intents SET structure_digest = %s "
                    "WHERE event_id = %s AND operation_id = 'prepare'",
                    (plan.structure_digest, event_id),
                )
                await conn.execute(
                    "ALTER TABLE github_operation_intents ENABLE TRIGGER github_operation_intent_immutable"
                )
                assert (
                    await load_operation_lineage(connection=conn, event_id=event_id, operation_id="prepare")
                ) is not None

                # Only dependent operations may persist lineage. A root
                # lineage row is inconsistent even when its digest is valid.
                rogue_root = OperationLineage(())
                await conn.execute(
                    """
                    INSERT INTO github_operation_lineages
                        (event_id, operation_id, lineage_version, lineage_digest, lineage_bytes)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (
                        event_id,
                        "read",
                        rogue_root.lineage_version,
                        rogue_root.lineage_digest,
                        rogue_root.canonical_bytes(),
                    ),
                )
                root_invalid = await inspect_persisted_delivery_contracts(
                    connection=conn,
                    catalog=catalog,
                    event_id=event_id,
                )
                assert root_invalid.status == "hold" and root_invalid.reason == "lineage_invalid"
                with pytest.raises(CheckpointHold, match="Root Intent"):
                    await persist_checkpoint(conn)
                with pytest.raises(IntentIntegrityError, match="unexpected lineage"):
                    await load_operation_intent(connection=conn, event_id=event_id, operation_id="read")
                with pytest.raises(CheckpointIntegrityError, match="lineage"):
                    await load_operation_checkpoint(connection=conn, event_id=event_id, operation_id="read")
                with pytest.raises(LineageIntegrityError, match="dependent Intent"):
                    await load_operation_lineage(connection=conn, event_id=event_id, operation_id="read")
                assert (
                    await inspect_operation_recovery_evidence(
                        connection=conn, catalog=catalog, event_id=event_id, operation_id="read"
                    )
                ).reason == "lineage_invalid"
                await conn.execute(
                    "ALTER TABLE github_operation_lineages DISABLE TRIGGER github_operation_lineage_immutable"
                )
                await conn.execute(
                    "DELETE FROM github_operation_lineages WHERE event_id = %s AND operation_id = %s",
                    (event_id, "read"),
                )
                await conn.execute(
                    "ALTER TABLE github_operation_lineages ENABLE TRIGGER github_operation_lineage_immutable"
                )
                assert (
                    await inspect_persisted_delivery_contracts(connection=conn, catalog=catalog, event_id=event_id)
                ).status == "compatible"

                # The append-only lineage must be checked against the source
                # checkpoint before considering a persisted Plan compatible.
                await conn.execute(
                    "ALTER TABLE github_operation_lineages DISABLE TRIGGER github_operation_lineage_immutable"
                )
                damaged_lineage = b"{}"
                await conn.execute(
                    "UPDATE github_operation_lineages SET lineage_bytes = %s, lineage_digest = %s "
                    "WHERE event_id = %s AND operation_id = 'prepare'",
                    (
                        damaged_lineage,
                        "sha256:" + hashlib.sha256(damaged_lineage).hexdigest(),
                        event_id,
                    ),
                )
                damaged_inspection = await inspect_persisted_delivery_contracts(
                    connection=conn,
                    catalog=catalog,
                    event_id=event_id,
                )
                assert damaged_inspection.status == "hold" and damaged_inspection.reason == "lineage_invalid"
                with pytest.raises(CheckpointHold, match="lineage"):
                    await persist_checkpoint(conn, value=dependent_output)
                with pytest.raises(CheckpointIntegrityError, match="lineage"):
                    await load_operation_checkpoint(connection=conn, event_id=event_id, operation_id="prepare")
                with pytest.raises(LineageIntegrityError, match="source evidence"):
                    await load_operation_lineage(connection=conn, event_id=event_id, operation_id="finish")
                with pytest.raises(IntentHold, match="lineage"):
                    await persist_downstream()
                await conn.execute(
                    "UPDATE github_operation_lineages SET lineage_bytes = %s, lineage_digest = %s "
                    "WHERE event_id = %s AND operation_id = 'prepare'",
                    (lineage.canonical_bytes(), lineage.lineage_digest, event_id),
                )
                assert (
                    await inspect_persisted_delivery_contracts(
                        connection=conn,
                        catalog=catalog,
                        event_id=event_id,
                    )
                ).status == "compatible"
                await conn.execute(
                    "ALTER TABLE github_operation_lineages ENABLE TRIGGER github_operation_lineage_immutable"
                )

                await conn.execute("SET default_transaction_read_only = on")
                loaded_checkpoint = await load_operation_checkpoint(
                    connection=conn, event_id=event_id, operation_id="read"
                )
                assert loaded_checkpoint is not None
                assert loaded_checkpoint.checkpoint_bytes == output.canonical_bytes()
                assert loaded_checkpoint.recorded_generation == 1
                await conn.execute("SET default_transaction_read_only = off")

                for table in ("github_operation_checkpoints", "github_operation_lineages"):
                    for sql_statement in (
                        f"UPDATE {table} SET event_id = event_id WHERE event_id = %s",
                        f"DELETE FROM {table} WHERE event_id = %s",
                    ):
                        with pytest.raises(psycopg.Error):
                            await conn.execute(sql_statement, (event_id,))

                # A new Generation can read the old immutable Output but never
                # silently replace it or change the dependent binding.
                await requeue_job(conn, job_id)
                with pytest.raises(CheckpointHold):
                    await persist_checkpoint(conn)
                await conn.execute(
                    "UPDATE procrastinate_jobs SET status = 'doing', worker_id = %s WHERE id = %s",
                    (worker_id, job_id),
                )
                next_observation = await inspect_running_claim(
                    connection=conn,
                    event_id=event_id,
                    job_id=job_id,
                    expected_task_name="b3s_checkpoint_noop",
                    worker_id=worker_id,
                    attempts=0,
                )
                assert next_observation.started_event_id is not None
                newer_start = next_observation.started_event_id
                with pytest.raises(CheckpointHold):
                    await persist_checkpoint(conn, active_start=newer_start)
                new_generation = await record_claim_generation(
                    connection=conn,
                    event_id=event_id,
                    job_id=job_id,
                    expected_task_name="b3s_checkpoint_noop",
                    worker_id=worker_id,
                    attempts=0,
                    started_event_id=newer_start,
                )
                assert new_generation.generation == 2
                assert (
                    await persist_checkpoint(conn, claim_generation=2, active_start=newer_start)
                ).disposition == "existing"
                assert (
                    await persist_dependent(conn, claim_generation=2, active_start=newer_start)
                ).disposition == "existing"

                # A Checkpoint cannot predate the immutable Intent it records.
                # Both generations exist, so a plain FK check would not catch
                # this privileged test-only corruption of the Intent origin.
                await conn.execute(
                    "ALTER TABLE github_operation_intents DISABLE TRIGGER github_operation_intent_immutable"
                )
                await conn.execute(
                    "UPDATE github_operation_intents SET created_generation = 2 "
                    "WHERE event_id = %s AND operation_id = 'read'",
                    (event_id,),
                )
                invalid_order = await inspect_persisted_delivery_contracts(
                    connection=conn,
                    catalog=catalog,
                    event_id=event_id,
                )
                assert invalid_order.status == "hold" and invalid_order.reason == "checkpoint_invalid"
                with pytest.raises(CheckpointIntegrityError):
                    await load_operation_checkpoint(connection=conn, event_id=event_id, operation_id="read")
                with pytest.raises(CheckpointIntegrityError, match="predates"):
                    await persist_checkpoint(conn, claim_generation=2, active_start=newer_start)
                with pytest.raises(LineageIntegrityError):
                    await load_operation_lineage(connection=conn, event_id=event_id, operation_id="prepare")
                with pytest.raises(IntentHold):
                    await persist_dependent(conn, claim_generation=2, active_start=newer_start)
                await conn.execute(
                    "UPDATE github_operation_intents SET created_generation = 1 "
                    "WHERE event_id = %s AND operation_id = 'read'",
                    (event_id,),
                )
                await conn.execute(
                    "ALTER TABLE github_operation_intents ENABLE TRIGGER github_operation_intent_immutable"
                )
                assert (
                    await inspect_persisted_delivery_contracts(
                        connection=conn,
                        catalog=catalog,
                        event_id=event_id,
                    )
                ).status == "compatible"

                # In test-only disposable schema, corrupt canonical bytes and
                # ensure both readers and downstream writes reject stale output.
                await conn.execute(
                    "ALTER TABLE github_operation_checkpoints DISABLE TRIGGER github_operation_checkpoint_immutable"
                )
                corrupted = b"{}"
                await conn.execute(
                    "UPDATE github_operation_checkpoints "
                    "SET checkpoint_bytes = %s, checkpoint_digest = %s "
                    "WHERE event_id = %s",
                    (corrupted, "sha256:" + hashlib.sha256(corrupted).hexdigest(), event_id),
                )
                with pytest.raises(CheckpointIntegrityError):
                    await load_operation_checkpoint(connection=conn, event_id=event_id, operation_id="read")
                with pytest.raises(LineageIntegrityError):
                    await load_operation_lineage(connection=conn, event_id=event_id, operation_id="prepare")
                source_corrupted = await inspect_persisted_delivery_contracts(
                    connection=conn,
                    catalog=catalog,
                    event_id=event_id,
                )
                assert source_corrupted.status == "hold" and source_corrupted.reason == "lineage_invalid"
                with pytest.raises(IntentHold):
                    await persist_dependent(conn, claim_generation=2, active_start=newer_start)
            finally:
                await conn.execute("SET default_transaction_read_only = off")
                await conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))

    asyncio.run(run())
