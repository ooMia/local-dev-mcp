"""Read-only deployment and recovery classification of persisted evidence."""

import asyncio
import hashlib
from uuid import uuid4

import procrastinate
import pytest
from psycopg import sql

from local_dev_mcp.tools.gh.delivery import (
    CheckpointField,
    ConcurrencyProtection,
    ConsumerOperationContract,
    ContractCatalog,
    OperationCheckpoint,
    OperationIntent,
    OperationNode,
    PlanStructure,
    accept_event,
    inspect_operation_recovery_evidence,
    inspect_pending_deployment_contracts,
    inspect_persisted_delivery_contracts,
    inspect_running_claim,
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


def test_deployment_preflight_reads_persisted_contract_versions_without_effects() -> None:
    """Q76: read-only preflight never invokes Consumers or changes Job state."""
    options = connection_options()
    schema = "b3s_preflight_" + uuid4().hex

    async def run() -> None:
        app = procrastinate.App(connector=procrastinate.PsycopgConnector())

        noop = register_noop(app, name="b3s_preflight_noop")

        async def no_execute(_: OperationIntent) -> object:
            raise AssertionError("Preflight invoked execute")

        async def no_probe(_: OperationIntent) -> object:
            raise AssertionError("Preflight invoked probe")

        def no_decode(_: bytes) -> OperationCheckpoint:
            raise AssertionError("Preflight invoked decode")

        async def no_verify(_: OperationIntent, __: OperationCheckpoint) -> bool:
            raise AssertionError("Preflight invoked verify")

        async def no_guard(_: OperationIntent) -> object:
            raise AssertionError("Preflight invoked concurrency guard")

        def code(version: str) -> ConsumerOperationContract:
            return ConsumerOperationContract(
                consumer_id="github_issue",
                operation_type="read_current",
                contract_version=version,
                retry_safety_rule="hold_without_probe",
                probe_contract="github_readonly:v1",
                concurrency=ConcurrencyProtection(
                    "conditional_remote:v1",
                    "conditional_remote:v1",
                    DIGEST,
                    no_guard,
                ),
                execute=no_execute,
                probe=no_probe,
                decode=no_decode,
                verify=no_verify,
            )

        old_node = OperationNode("read", "github_issue", "read_current", "read:v1")
        new_node = OperationNode("read", "github_issue", "read_current", "read:v2")
        old_plan = PlanStructure((old_node,))
        new_plan = PlanStructure((new_node,))
        old_intent = OperationIntent(
            operation=old_node,
            input_digest=DIGEST,
            effect_digest="sha256:" + "b" * 64,
            preconditions_digest="sha256:" + "c" * 64,
            postconditions_digest="sha256:" + "d" * 64,
            retry_safety_rule="hold_without_probe",
            probe_contract="github_readonly:v1",
        )

        async with await connect(options) as conn:
            try:
                await conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
                await conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
                await install_procrastinate_schema(conn)
                for ddl in (RECEIPTS_DDL, PLANS_DDL, CLAIM_GENERATIONS_DDL, INTENTS_DDL, CHECKPOINTS_DDL):
                    await conn.execute(ddl)

                old_receipt = await accept_event(
                    connection=conn,
                    delivery=delivery("preflight-old"),
                    task=noop,
                )
                new_receipt = await accept_event(
                    connection=conn,
                    delivery=delivery("preflight-new"),
                    task=noop,
                )
                missing_receipt = await accept_event(
                    connection=conn,
                    delivery=delivery("preflight-unsealed"),
                    task=noop,
                )
                await seal_plan_structure(connection=conn, event_id=old_receipt.event_id, plan=old_plan)
                await seal_plan_structure(connection=conn, event_id=new_receipt.event_id, plan=new_plan)

                worker = await conn.execute("INSERT INTO procrastinate_workers DEFAULT VALUES RETURNING id")
                worker_row = await worker.fetchone()
                assert worker_row is not None
                worker_id = int(worker_row[0])
                await conn.execute(
                    "UPDATE procrastinate_jobs SET status = 'doing', worker_id = %s WHERE id = %s",
                    (worker_id, old_receipt.job_id),
                )
                observation = await inspect_running_claim(
                    connection=conn,
                    event_id=old_receipt.event_id,
                    job_id=old_receipt.job_id,
                    expected_task_name="b3s_preflight_noop",
                    worker_id=worker_id,
                    attempts=0,
                )
                assert observation.started_event_id is not None
                started_id = observation.started_event_id
                await record_claim_generation(
                    connection=conn,
                    event_id=old_receipt.event_id,
                    job_id=old_receipt.job_id,
                    expected_task_name="b3s_preflight_noop",
                    worker_id=worker_id,
                    attempts=0,
                    started_event_id=started_id,
                )
                await record_operation_intent(
                    connection=conn,
                    event_id=old_receipt.event_id,
                    generation=1,
                    job_id=old_receipt.job_id,
                    expected_task_name="b3s_preflight_noop",
                    worker_id=worker_id,
                    attempts=0,
                    started_event_id=started_id,
                    expected_plan=old_plan,
                    intent=old_intent,
                )

                latest_only = ContractCatalog((code("read:v2"),))
                old = await inspect_persisted_delivery_contracts(
                    connection=conn,
                    catalog=latest_only,
                    event_id=old_receipt.event_id,
                )
                assert old.status == "hold" and old.reason == "unsupported_contract"
                assert len(old.operations) == 1
                assert old.operations[0].contract_version == "read:v1"

                current = await inspect_persisted_delivery_contracts(
                    connection=conn,
                    catalog=latest_only,
                    event_id=new_receipt.event_id,
                )
                assert current.status == "compatible" and current.reason is None
                assert current.operations[0].contract_version == "read:v2"

                missing = await inspect_persisted_delivery_contracts(
                    connection=conn,
                    catalog=latest_only,
                    event_id=missing_receipt.event_id,
                )
                assert missing.status == "hold" and missing.reason == "plan_missing"
                nonexistent = await inspect_persisted_delivery_contracts(
                    connection=conn,
                    catalog=latest_only,
                    event_id=old_receipt.event_id + 10000,
                )
                assert nonexistent.reason == "receipt_missing"
                empty = await inspect_persisted_delivery_contracts(
                    connection=conn,
                    catalog=ContractCatalog(),
                    event_id=new_receipt.event_id,
                )
                assert empty.reason == "unsupported_contract"

                with pytest.raises(ValueError):
                    await inspect_pending_deployment_contracts(
                        connection=conn,
                        catalog=latest_only,
                        max_events=0,
                    )
                bounded = await inspect_pending_deployment_contracts(
                    connection=conn,
                    catalog=latest_only,
                    max_events=2,
                )
                assert bounded.status == "hold" and bounded.reason == "scope_incomplete"
                assert bounded.total_inspected == 0 and bounded.inspected == ()
                batch = await inspect_pending_deployment_contracts(
                    connection=conn,
                    catalog=latest_only,
                    max_events=3,
                )
                assert batch.status == "hold" and batch.reason is None
                assert batch.total_inspected == 3
                assert [x.reason for x in batch.inspected] == [
                    "unsupported_contract",
                    None,
                    "plan_missing",
                ]

                both = ContractCatalog((code("read:v1"), code("read:v2")))
                assert (
                    await inspect_persisted_delivery_contracts(
                        connection=conn,
                        catalog=both,
                        event_id=old_receipt.event_id,
                    )
                ).status == "compatible"
                assert (
                    await inspect_pending_deployment_contracts(
                        connection=conn,
                        catalog=both,
                        max_events=3,
                    )
                ).status == "hold"  # Missing Plan cannot be silently skipped.

                # Claim Generation has a local FK to Receipt, but its Job/start
                # references have no DB FK. Test-only corruption must fail
                # both immutable-delivery inspection and recovery classification.
                await conn.execute(
                    "ALTER TABLE github_delivery_claim_generations DISABLE TRIGGER github_claim_generation_immutable"
                )
                for column, invalid, original in (
                    ("job_id", new_receipt.job_id, old_receipt.job_id),
                    ("started_event_id", started_id + 1000000, started_id),
                ):
                    await conn.execute(
                        sql.SQL("UPDATE github_delivery_claim_generations SET {} = %s WHERE event_id = %s").format(
                            sql.Identifier(column)
                        ),
                        (invalid, old_receipt.event_id),
                    )
                    broken_claim = await inspect_persisted_delivery_contracts(
                        connection=conn,
                        catalog=both,
                        event_id=old_receipt.event_id,
                    )
                    assert broken_claim.status == "hold"
                    assert broken_claim.reason == "claim_evidence_invalid"
                    broken_recovery = await inspect_operation_recovery_evidence(
                        connection=conn,
                        catalog=both,
                        event_id=old_receipt.event_id,
                        operation_id="read",
                    )
                    assert broken_recovery.status == "hold"
                    assert broken_recovery.reason == "claim_evidence_invalid"
                    await conn.execute(
                        sql.SQL("UPDATE github_delivery_claim_generations SET {} = %s WHERE event_id = %s").format(
                            sql.Identifier(column)
                        ),
                        (original, old_receipt.event_id),
                    )
                await conn.execute(
                    "ALTER TABLE github_delivery_claim_generations ENABLE TRIGGER github_claim_generation_immutable"
                )
                assert (
                    await inspect_persisted_delivery_contracts(
                        connection=conn,
                        catalog=both,
                        event_id=old_receipt.event_id,
                    )
                ).status == "compatible"

                # A stored Receipt with missing required target fields must
                # never pass pre-deployment or recovery compatibility checks.
                target_row = await conn.execute(
                    "SELECT repository, target_updated_at FROM github_event_receipts WHERE event_id = %s",
                    (new_receipt.event_id,),
                )
                original_target = await target_row.fetchone()
                assert original_target is not None
                for column, original in zip(("repository", "target_updated_at"), original_target, strict=True):
                    await conn.execute(
                        sql.SQL("UPDATE github_event_receipts SET {} = '' WHERE event_id = %s").format(
                            sql.Identifier(column)
                        ),
                        (new_receipt.event_id,),
                    )
                    invalid_target = await inspect_persisted_delivery_contracts(
                        connection=conn,
                        catalog=both,
                        event_id=new_receipt.event_id,
                    )
                    assert invalid_target.status == "hold" and invalid_target.reason == "unsupported_contract"
                    recovery_invalid = await inspect_operation_recovery_evidence(
                        connection=conn,
                        catalog=both,
                        event_id=new_receipt.event_id,
                        operation_id="read",
                    )
                    assert recovery_invalid.status == "hold" and recovery_invalid.reason == "unsupported_contract"
                    await conn.execute(
                        sql.SQL("UPDATE github_event_receipts SET {} = %s WHERE event_id = %s").format(
                            sql.Identifier(column)
                        ),
                        (original, new_receipt.event_id),
                    )

                # Q65: never confuse an unstarted queued Job with an already
                # persisted Intent whose external outcome is unknown.
                pending = await inspect_operation_recovery_evidence(
                    connection=conn,
                    catalog=both,
                    event_id=new_receipt.event_id,
                    operation_id="read",
                )
                assert pending.status == "pending_observed"
                assert pending.intent_digest is None and pending.checkpoint_digest is None

                # Even without started events or a recorded Claim, a pending
                # Job may preserve retry/ownership evidence. Never
                # mistake such rows for a genuinely unstarted local Job.
                for column, suspicious, original in (
                    ("attempts", 1, 0),
                    ("worker_id", worker_id, None),
                ):
                    await conn.execute(
                        sql.SQL("UPDATE procrastinate_jobs SET {} = %s WHERE id = %s").format(sql.Identifier(column)),
                        (suspicious, new_receipt.job_id),
                    )
                    anomalous = await inspect_operation_recovery_evidence(
                        connection=conn,
                        catalog=both,
                        event_id=new_receipt.event_id,
                        operation_id="read",
                    )
                    assert anomalous.status == "hold"
                    assert anomalous.reason == "job_history_unresolved"
                    await conn.execute(
                        sql.SQL("UPDATE procrastinate_jobs SET {} = %s WHERE id = %s").format(sql.Identifier(column)),
                        (original, new_receipt.job_id),
                    )
                assert (
                    await inspect_operation_recovery_evidence(
                        connection=conn,
                        catalog=both,
                        event_id=new_receipt.event_id,
                        operation_id="read",
                    )
                ).status == "pending_observed"

                # Test-only history gap: durable Claim Generation survives,
                # but the linked started Job event is absent. This must never
                # be mistaken for an operation that has not been attempted.
                orphaned = await accept_event(
                    connection=conn,
                    delivery=delivery("preflight-orphaned-claim"),
                    task=noop,
                )
                await seal_plan_structure(connection=conn, event_id=orphaned.event_id, plan=new_plan)
                await conn.execute(
                    """
                    INSERT INTO github_delivery_claim_generations
                        (event_id, generation, job_id, started_event_id, worker_id, attempts)
                    VALUES (%s, 1, %s, %s, %s, 0)
                    """,
                    (orphaned.event_id, orphaned.job_id, started_id, worker_id),
                )
                claim_gap = await inspect_operation_recovery_evidence(
                    connection=conn,
                    catalog=both,
                    event_id=orphaned.event_id,
                    operation_id="read",
                )
                assert claim_gap.status == "hold"
                assert claim_gap.reason == "claim_history_unresolved"
                assert claim_gap.intent_digest is None
                # This is local evidence classification, not remote verification.
                assert (
                    await inspect_persisted_delivery_contracts(
                        connection=conn,
                        catalog=both,
                        event_id=orphaned.event_id,
                    )
                ).status == "compatible"

                unresolved = await inspect_operation_recovery_evidence(
                    connection=conn,
                    catalog=both,
                    event_id=old_receipt.event_id,
                    operation_id="read",
                )
                assert unresolved.status == "hold" and unresolved.reason == "outcome_unknown"
                assert unresolved.intent_digest == old_intent.intent_digest
                unsupported = await inspect_operation_recovery_evidence(
                    connection=conn,
                    catalog=latest_only,
                    event_id=old_receipt.event_id,
                    operation_id="read",
                )
                assert unsupported.reason == "unsupported_contract"
                assert (
                    await inspect_operation_recovery_evidence(
                        connection=conn,
                        catalog=both,
                        event_id=old_receipt.event_id,
                        operation_id="unknown",
                    )
                ).reason == "operation_missing"
                assert (
                    await inspect_operation_recovery_evidence(
                        connection=conn,
                        catalog=both,
                        event_id=missing_receipt.event_id,
                        operation_id="read",
                    )
                ).reason == "plan_missing"

                # A started Job without an Intent is never treated as new
                # pending work, including after a queued transition.
                await conn.execute(
                    "UPDATE procrastinate_jobs SET status = 'doing', worker_id = %s WHERE id = %s",
                    (worker_id, new_receipt.job_id),
                )
                active = await inspect_operation_recovery_evidence(
                    connection=conn,
                    catalog=both,
                    event_id=new_receipt.event_id,
                    operation_id="read",
                )
                assert active.status == "hold" and active.reason == "active_unresolved"
                await requeue_job(conn, new_receipt.job_id)
                interrupted = await inspect_operation_recovery_evidence(
                    connection=conn,
                    catalog=both,
                    event_id=new_receipt.event_id,
                    operation_id="read",
                )
                assert interrupted.status == "hold" and interrupted.reason == "historical_start_unresolved"

                # Storage is evidence only; no GitHub effect or success is
                # simulated. A Checkpoint is observed, not a remote permit.
                old_checkpoint = OperationCheckpoint(
                    operation_id="read",
                    intent_digest=old_intent.intent_digest,
                    output_schema_version="issue:v1",
                    fields=(CheckpointField("status", DIGEST),),
                    verification_contract="github_readonly:v1",
                    verification_evidence_digest="sha256:" + "e" * 64,
                )
                await record_operation_checkpoint(
                    connection=conn,
                    event_id=old_receipt.event_id,
                    generation=1,
                    job_id=old_receipt.job_id,
                    expected_task_name="b3s_preflight_noop",
                    worker_id=worker_id,
                    attempts=0,
                    started_event_id=started_id,
                    expected_plan=old_plan,
                    checkpoint=old_checkpoint,
                )
                recorded = await inspect_operation_recovery_evidence(
                    connection=conn,
                    catalog=both,
                    event_id=old_receipt.event_id,
                    operation_id="read",
                )
                assert recorded.status == "checkpoint_observed" and recorded.reason is None
                assert recorded.checkpoint_digest == old_checkpoint.checkpoint_digest
                assert recorded.intent_digest == old_intent.intent_digest

                # Corruption with a recomputed digest is still rejected.
                await conn.execute(
                    "ALTER TABLE github_operation_checkpoints DISABLE TRIGGER github_operation_checkpoint_immutable"
                )
                corrupted_checkpoint = b"{}"
                await conn.execute(
                    "UPDATE github_operation_checkpoints SET checkpoint_bytes = %s, "
                    "checkpoint_digest = %s WHERE event_id = %s",
                    (
                        corrupted_checkpoint,
                        "sha256:" + hashlib.sha256(corrupted_checkpoint).hexdigest(),
                        old_receipt.event_id,
                    ),
                )
                assert (
                    await inspect_operation_recovery_evidence(
                        connection=conn,
                        catalog=both,
                        event_id=old_receipt.event_id,
                        operation_id="read",
                    )
                ).reason == "checkpoint_invalid"
                corrupted_preflight = await inspect_persisted_delivery_contracts(
                    connection=conn,
                    catalog=both,
                    event_id=old_receipt.event_id,
                )
                assert corrupted_preflight.status == "hold"
                assert corrupted_preflight.reason == "checkpoint_invalid"
                await conn.execute(
                    "UPDATE github_operation_checkpoints SET checkpoint_bytes = %s, "
                    "checkpoint_digest = %s WHERE event_id = %s",
                    (old_checkpoint.canonical_bytes(), old_checkpoint.checkpoint_digest, old_receipt.event_id),
                )

                await conn.execute("SET default_transaction_read_only = on")
                assert (
                    await inspect_persisted_delivery_contracts(
                        connection=conn,
                        catalog=both,
                        event_id=old_receipt.event_id,
                    )
                ).status == "compatible"
                assert (
                    await inspect_operation_recovery_evidence(
                        connection=conn,
                        catalog=both,
                        event_id=old_receipt.event_id,
                        operation_id="read",
                    )
                ).status == "checkpoint_observed"
                await conn.execute("SET default_transaction_read_only = off")
                old_job = await conn.execute(
                    "SELECT status::text FROM procrastinate_jobs WHERE id = %s",
                    (old_receipt.job_id,),
                )
                assert await old_job.fetchone() == ("doing",)

                # Test-only corruption of one persisted Intent's full canonical
                # representation is rejected even with a matching SHA-256.
                await conn.execute(
                    "ALTER TABLE github_operation_intents DISABLE TRIGGER github_operation_intent_immutable"
                )
                invalid = b"{}"
                await conn.execute(
                    "UPDATE github_operation_intents SET intent_bytes = %s, intent_digest = %s WHERE event_id = %s",
                    (invalid, "sha256:" + hashlib.sha256(invalid).hexdigest(), old_receipt.event_id),
                )
                damaged = await inspect_persisted_delivery_contracts(
                    connection=conn,
                    catalog=both,
                    event_id=old_receipt.event_id,
                )
                assert damaged.status == "hold" and damaged.reason == "intent_invalid"
            finally:
                await conn.execute("SET default_transaction_read_only = off")
                await conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))

    asyncio.run(run())
