"""Missing optional evidence stores keep recovery in Hold."""

import asyncio
from uuid import uuid4

import procrastinate
import pytest
from psycopg import sql

from local_dev_mcp.tools.gh.delivery import (
    ConcurrencyProtection,
    ConsumerOperationContract,
    ContractCatalog,
    OperationCheckpoint,
    OperationIntent,
    OperationNode,
    PlanStructure,
    accept_event,
    inspect_operation_recovery_evidence,
    inspect_persisted_delivery_contracts,
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
)

pytestmark = pytest.mark.integration


def test_recovery_holds_when_optional_evidence_store_is_not_installed() -> None:
    """A root-only Delivery must fail closed, not raise an undefined-table SQL error."""
    options = connection_options()
    schema = "b3s_recovery_optional_" + uuid4().hex

    async def run() -> None:
        app = procrastinate.App(connector=procrastinate.PsycopgConnector())

        noop = register_noop(app, name="b3s_recovery_optional_noop")

        async def never_call(_: OperationIntent) -> object:
            raise AssertionError("Recovery inspection invoked a Consumer hook")

        async def never_verify(_: OperationIntent, __: OperationCheckpoint) -> bool:
            raise AssertionError("Recovery inspection invoked verify")

        def never_decode(_: bytes) -> OperationCheckpoint:
            raise AssertionError("Recovery inspection invoked decode")

        root = OperationNode("read", "github_issue", "read_current", "read:v1")
        plan = PlanStructure((root,))
        catalog = ContractCatalog(
            (
                ConsumerOperationContract(
                    consumer_id="github_issue",
                    operation_type="read_current",
                    contract_version="read:v1",
                    retry_safety_rule="hold_without_probe",
                    probe_contract="github_readonly:v1",
                    concurrency=ConcurrencyProtection(
                        "conditional_remote:v1",
                        "conditional_remote:v1",
                        DIGEST,
                        never_call,
                    ),
                    execute=never_call,
                    probe=never_call,
                    decode=never_decode,
                    verify=never_verify,
                ),
            )
        )

        async with await connect(options) as conn:
            try:
                await conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
                await conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
                await install_procrastinate_schema(conn)
                for ddl in (RECEIPTS_DDL, PLANS_DDL, CLAIM_GENERATIONS_DDL, INTENTS_DDL):
                    await conn.execute(ddl)
                receipt = await accept_event(connection=conn, delivery=delivery("recovery-optional-store"), task=noop)
                await seal_plan_structure(connection=conn, event_id=receipt.event_id, plan=plan)

                preflight = await inspect_persisted_delivery_contracts(
                    connection=conn, catalog=catalog, event_id=receipt.event_id
                )
                assert preflight.status == "compatible"
                await conn.execute("SET default_transaction_read_only = on")
                missing = await inspect_operation_recovery_evidence(
                    connection=conn, catalog=catalog, event_id=receipt.event_id, operation_id="read"
                )
                assert missing.status == "hold"
                assert missing.reason == "evidence_store_unavailable"
                assert missing.structure_digest == plan.structure_digest
                await conn.execute("SET default_transaction_read_only = off")

                # DDL remains an explicit owner action, never an inspector effect.
                await conn.execute(CHECKPOINTS_DDL)
                fresh = await inspect_operation_recovery_evidence(
                    connection=conn, catalog=catalog, event_id=receipt.event_id, operation_id="read"
                )
                assert fresh.status == "pending_observed"
                assert fresh.reason is None

                # Simulate a partial installation in this disposable schema.
                await conn.execute("DROP TABLE github_operation_lineages")
                partial = await inspect_operation_recovery_evidence(
                    connection=conn, catalog=catalog, event_id=receipt.event_id, operation_id="read"
                )
                assert partial.status == "hold"
                assert partial.reason == "evidence_store_unavailable"
                assert (
                    await inspect_persisted_delivery_contracts(
                        connection=conn, catalog=catalog, event_id=receipt.event_id
                    )
                ).status == "compatible"  # Root-only static compatibility is unchanged.
            finally:
                await conn.execute("SET default_transaction_read_only = off")
                await conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))

    asyncio.run(run())
