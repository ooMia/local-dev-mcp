"""Opt-in GitHub Issue add-label Consumer with explicit remote write dependencies.

Q77=A: success means the desired label is observed, not that our earlier
request caused it. No default write transport, runtime authority, Worker,
automatic retry, or production concurrency guarantee is provided.
"""

import asyncio
import http.client
import json
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Literal, Protocol, cast

from local_dev_mcp.tools.gh.webhook.verify import VerifiedTarget

from ._canonical import checked_digest, digest_bytes
from .issue_read_consumer import (
    API_VERSION,
    IssueReadHold,
    decode_issue_read_checkpoint,
    issue_read_input_digest,
    make_issue_read_intent,
)
from .operation_checkpoint import CheckpointField, OperationCheckpoint
from .operation_contracts import ConcurrencyProtection, ConsumerOperationContract
from .operation_intent import ConsumedOutput, OperationIntent
from .operation_lineage import LineageBinding, OperationLineage
from .operation_plan import OperationNode

CONSUMER_ID = "github_issue"
OPERATION_TYPE = "add_label"
CONTRACT_VERSION = "add_label:v2"
RETRY_RULE = "hold_without_probe"
PROBE_CONTRACT = "github_issue_label_goal:v1"
OUTPUT_VERSION = "github_issue_label_goal:v1"
CONCURRENCY_LEVEL = "consumer_scoped_durable_attempt:v2"
BINDING_VERSION = "issue_read_labels_to_goal:v1"

IssueObserve = Callable[[VerifiedTarget], Awaitable[bytes]]
IssueAddLabel = Callable[[VerifiedTarget, str], Awaitable[object]]


class AttemptPermit(Protocol):
    async def commit_attempt(self) -> None:
        """Durably commit the immutable pre-POST Attempt, or reject the POST."""


WriteScope = Callable[[OperationIntent], AbstractAsyncContextManager[AttemptPermit]]


class IssueLabelHold(RuntimeError):
    """The requested label goal or authority is not sufficiently verified."""


@dataclass(frozen=True, slots=True)
class IssueLabelProbe:
    status: Literal["goal_satisfied", "inconclusive"]
    observation_digest: str
    mutation_attributed: Literal[False] = False


def validate_issue_label(value: str) -> str:
    if type(value) is not str or not 0 < len(value) <= 100 or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("Expected one nonempty, bounded GitHub Label name.")
    return value


@dataclass(frozen=True, slots=True, repr=False)
class GitHubIssueLabelHttpTransport:
    """Explicit POST-only transport; Caller must supply a guarded write scope.

    Token contents never appear in repr. An error after a request may mean
    GitHub applied the change: no fallback or automatic retry is allowed.
    """

    token: str

    def __post_init__(self) -> None:
        if (
            type(self.token) is not str
            or not 0 < len(self.token) <= 8192
            or any(ord(c) in (0, 10, 13) for c in self.token)
        ):
            raise ValueError("A valid explicit write token is required.")

    async def __call__(self, target: VerifiedTarget, label: str) -> object:
        issue_read_input_digest(target)
        validate_issue_label(label)

        def post() -> None:
            conn = http.client.HTTPSConnection("api.github.com", timeout=10)
            try:
                body = json.dumps({"labels": [label]}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                conn.request(
                    "POST",
                    "/repos/" + target.repository + "/issues/" + str(target.number) + "/labels",
                    body=body,
                    headers={
                        "Accept": "application/vnd.github+json",
                        "Content-Type": "application/json",
                        "X-GitHub-Api-Version": API_VERSION,
                        "User-Agent": "local-dev-mcp",
                        "Authorization": "Bearer " + self.token,
                    },
                )
                response = conn.getresponse()
                if response.status != 200:
                    raise IssueLabelHold("add_label_http_status_unknown")
            except (OSError, http.client.HTTPException) as error:
                raise IssueLabelHold("add_label_http_outcome_unknown") from error
            finally:
                conn.close()

        request = asyncio.create_task(asyncio.to_thread(post))
        try:
            await asyncio.shield(request)
        except asyncio.CancelledError:
            # Cancellation of the awaiting coroutine must NOT release the
            # owning write_scope while the synchronous POST is still in flight.
            # Drain the bounded HTTP request, then propagate cancellation as
            # an unknown remote outcome. Never retry here.
            while not request.done():
                try:
                    await asyncio.shield(request)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if not request.cancelled():
                request.exception()  # Consume any error; cancellation wins.
            raise
        return None


def _bindings(
    *,
    operation: OperationNode,
    target: VerifiedTarget,
    label: str,
    source_intent: OperationIntent,
    source_checkpoint: OperationCheckpoint,
) -> tuple[OperationIntent, OperationLineage]:
    try:
        target_digest = issue_read_input_digest(target)
    except IssueReadHold as error:
        raise IssueLabelHold("add_label_target_invalid") from error
    validate_issue_label(label)
    if (
        type(operation) is not OperationNode
        or (operation.consumer_id, operation.operation_type, operation.contract_version)
        != (CONSUMER_ID, OPERATION_TYPE, CONTRACT_VERSION)
        or type(source_intent) is not OperationIntent
        or type(source_checkpoint) is not OperationCheckpoint
        or operation.depends_on != (source_intent.operation.operation_id,)
        or source_intent != make_issue_read_intent(operation=source_intent.operation, target=target)
        or source_checkpoint.operation_id != source_intent.operation.operation_id
        or source_checkpoint.intent_digest != source_intent.intent_digest
        or source_checkpoint.output_schema_version != "github_issue_read:v1"
        or source_checkpoint.verification_contract != "github_readonly:v1"
    ):
        raise IssueLabelHold("add_label_source_or_operation_mismatch")
    fields = {f.field_id: f.value_digest for f in source_checkpoint.fields}
    if set(fields) != {"issue_id", "number", "state", "updated_at", "labels"}:
        raise IssueLabelHold("add_label_source_checkpoint_invalid")

    intent = OperationIntent(
        operation=operation,
        input_digest=digest_bytes(
            json.dumps(
                {
                    "target_digest": target_digest,
                    "label": label,
                    "source_checkpoint_digest": source_checkpoint.checkpoint_digest,
                },
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        ),
        effect_digest=digest_bytes(
            json.dumps(
                {"effect": "ensure_label_present", "label": label},
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        ),
        preconditions_digest=digest_bytes(source_checkpoint.checkpoint_digest.encode("ascii")),
        postconditions_digest=digest_bytes(
            json.dumps(
                {"postcondition": "label_present", "label": label},
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        ),
        retry_safety_rule=RETRY_RULE,
        probe_contract=PROBE_CONTRACT,
        consumed_outputs=(ConsumedOutput(source_intent.operation.operation_id, source_checkpoint.checkpoint_digest),),
    )
    lineage = OperationLineage(
        (
            LineageBinding(
                source_operation_id=source_intent.operation.operation_id,
                source_checkpoint_digest=source_checkpoint.checkpoint_digest,
                source_field_id="labels",
                source_field_digest=fields["labels"],
                source_schema_version=source_checkpoint.output_schema_version,
                target_field_id="observed_labels",
                binding_version=BINDING_VERSION,
            ),
        )
    )
    return intent, lineage


def make_issue_add_label_intent(
    *,
    operation: OperationNode,
    target: VerifiedTarget,
    label: str,
    source_intent: OperationIntent,
    source_checkpoint: OperationCheckpoint,
) -> tuple[OperationIntent, OperationLineage]:
    """Bind an additive goal to the immutable read Checkpoint and field lineage."""
    return _bindings(
        operation=operation,
        target=target,
        label=label,
        source_intent=source_intent,
        source_checkpoint=source_checkpoint,
    )


def _observed_labels(
    raw: bytes, target: VerifiedTarget, source_intent: OperationIntent, source_checkpoint: OperationCheckpoint
) -> tuple[str, ...]:
    try:
        observed = decode_issue_read_checkpoint(raw=raw, target=target, intent=source_intent)
    except (IssueReadHold, ValueError, TypeError) as error:
        raise IssueLabelHold("unverified_issue_observation") from error
    prior_id = next(field.value_digest for field in source_checkpoint.fields if field.field_id == "issue_id")
    observed_id = next(field.value_digest for field in observed.fields if field.field_id == "issue_id")
    if observed_id != prior_id:
        raise IssueLabelHold("source_issue_identity_changed")
    # Identity and label element shapes have already been validated by the
    # read Consumer decoder, including the bounded payload and duplicated keys.
    parsed = cast(dict[str, object], json.loads(raw))
    labels = cast(list[dict[str, object]], parsed["labels"])
    return tuple(cast(str, item["name"]) for item in labels)


def bind_issue_add_label_contract(
    *,
    intent: OperationIntent,
    target: VerifiedTarget,
    label: str,
    source_intent: OperationIntent,
    source_checkpoint: OperationCheckpoint,
    observe: IssueObserve,
    add_label: IssueAddLabel,
    write_scope: WriteScope,
    test_evidence_digest: str,
) -> ConsumerOperationContract:
    """Provide fake-injectable add-only hooks; never register a default writer.

    write_scope MUST provide the caller's target-wide concurrency protection
    across the whole read→POST→read interval. A local lock, shape-only
    registration, or a test digest is NOT proof of remote fencing.
    """
    if type(intent) is not OperationIntent:
        raise IssueLabelHold("add_label_intent_mismatch")
    expected, _ = _bindings(
        operation=intent.operation,
        target=target,
        label=label,
        source_intent=source_intent,
        source_checkpoint=source_checkpoint,
    )
    if intent != expected:
        raise IssueLabelHold("add_label_intent_mismatch")
    if not callable(observe) or not callable(add_label) or not callable(write_scope):
        raise TypeError("Explicit observe/add-label/scope implementations are required.")
    checked_digest(test_evidence_digest, "test_evidence_digest")

    attempted = False
    issue_id = next(field.value_digest for field in source_checkpoint.fields if field.field_id == "issue_id")
    label_digest = digest_bytes(label.encode("utf-8"))

    def require_intent(value: OperationIntent) -> None:
        if value != intent:
            raise IssueLabelHold("add_label_intent_mismatch")

    async def guarded(value: OperationIntent) -> object:
        require_intent(value)
        # A separate preflight check never holds a lease into execute.
        # Execute itself MUST reacquire and hold the scoped protection.
        async with write_scope(value):
            return None

    def goal(raw: bytes) -> bool:
        names = _observed_labels(raw, target, source_intent, source_checkpoint)
        return label.casefold() in {name.casefold() for name in names}

    async def probe(value: OperationIntent) -> object:
        require_intent(value)
        raw = await observe(target)  # Probe is read-only, including after an unknown POST.
        return IssueLabelProbe("goal_satisfied" if goal(raw) else "inconclusive", digest_bytes(raw))

    async def execute(value: OperationIntent) -> object:
        nonlocal attempted
        require_intent(value)
        if attempted:
            raise IssueLabelHold("add_label_execution_already_attempted")
        attempted = True  # An in-memory defense, NOT a durable retry fence.
        async with write_scope(value) as permit:
            before = await observe(target)
            if goal(before):
                return before  # Already satisfied; no remote POST or Attempt needed.
            # Commit must return after a top-level DB transaction. A failure
            # here never permits the remote mutation to start.
            await permit.commit_attempt()
            try:
                await add_label(target, label)
            except Exception as error:
                raise IssueLabelHold("add_label_remote_outcome_unknown") from error
            after = await observe(target)
            if not goal(after):
                raise IssueLabelHold("add_label_postcondition_unverified")
            return after

    def decode(raw: bytes) -> OperationCheckpoint:
        if not goal(raw):
            raise IssueLabelHold("add_label_goal_not_observed")
        return OperationCheckpoint(
            operation_id=intent.operation.operation_id,
            intent_digest=intent.intent_digest,
            output_schema_version=OUTPUT_VERSION,
            fields=(
                CheckpointField("issue_id", issue_id),
                CheckpointField("goal_label", label_digest),
                CheckpointField("source_checkpoint", digest_bytes(source_checkpoint.checkpoint_digest.encode("ascii"))),
            ),
            verification_contract=PROBE_CONTRACT,
            verification_evidence_digest=digest_bytes(raw),
        )

    async def verify(value: OperationIntent, checkpoint: OperationCheckpoint) -> bool:
        require_intent(value)
        if (
            type(checkpoint) is not OperationCheckpoint
            or checkpoint.operation_id != intent.operation.operation_id
            or checkpoint.intent_digest != intent.intent_digest
            or checkpoint.output_schema_version != OUTPUT_VERSION
            or checkpoint.verification_contract != PROBE_CONTRACT
            or {field.field_id: field.value_digest for field in checkpoint.fields}
            != {
                "issue_id": issue_id,
                "goal_label": label_digest,
                "source_checkpoint": digest_bytes(source_checkpoint.checkpoint_digest.encode("ascii")),
            }
        ):
            return False
        fresh = await observe(target)
        return goal(fresh)

    return ConsumerOperationContract(
        consumer_id=CONSUMER_ID,
        operation_type=OPERATION_TYPE,
        contract_version=CONTRACT_VERSION,
        retry_safety_rule=RETRY_RULE,
        probe_contract=PROBE_CONTRACT,
        concurrency=ConcurrencyProtection(CONCURRENCY_LEVEL, CONCURRENCY_LEVEL, test_evidence_digest, guarded),
        execute=execute,
        probe=probe,
        decode=decode,
        verify=verify,
    )
