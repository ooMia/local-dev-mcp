"""Explicitly bound, read-only GitHub Issue Consumer (no worker or retry runner).

The caller must supply an authenticated Receipt target and a transport. The
Intent alone contains digests and cannot reconstruct API arguments or tokens.
No GitHub write, automatic retry, or checkpoint persistence occurs here.
"""

import asyncio
import http.client
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import cast

from local_dev_mcp.tools.gh.webhook.verify import VerifiedTarget, supports_target_action

from ._canonical import checked_digest, decode_json, digest_bytes
from .operation_checkpoint import CheckpointField, OperationCheckpoint
from .operation_contracts import ConcurrencyProtection, ConsumerOperationContract
from .operation_intent import OperationIntent
from .operation_plan import OperationNode

CONSUMER_ID = "github_issue"
OPERATION_TYPE = "read_current"
CONTRACT_VERSION = "read:v1"
RETRY_RULE = "hold_without_probe"
PROBE_CONTRACT = "github_readonly:v1"
OUTPUT_VERSION = "github_issue_read:v1"
CONCURRENCY_LEVEL = "read_only_get:v1"
API_VERSION = "2026-03-10"
MAX_RESPONSE_BYTES = 1024 * 1024
_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
_TIMESTAMP = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ\Z")
_READ_EFFECT = digest_bytes(b"github_issue.read_current:read_only:v1")
_READ_PRECONDITION = digest_bytes(b"github_issue.read_current:verified_receipt_target:v1")
_READ_POSTCONDITION = digest_bytes(b"github_issue.read_current:verified_snapshot:v1")

IssueReadTransport = Callable[[VerifiedTarget], Awaitable[bytes]]


class IssueReadHold(ValueError):
    """A target, HTTP response, or observation cannot be trusted."""


def _target(target: VerifiedTarget) -> None:
    if (
        type(target) is not VerifiedTarget
        or target.kind != "issue"
        or type(target.repository) is not str
        or _REPOSITORY.fullmatch(target.repository) is None
        or type(target.repository_id) is not int
        or not 0 < target.repository_id < 2**63
        or type(target.number) is not int
        or not 0 < target.number < 2**31
        or not supports_target_action(target.kind, target.action)
        or type(target.updated_at) is not str
        or not 0 < len(target.updated_at) <= 64
    ):
        raise IssueReadHold("invalid_issue_target")


def _operation(operation: OperationNode) -> None:
    if (
        type(operation) is not OperationNode
        or (operation.consumer_id, operation.operation_type, operation.contract_version)
        != (CONSUMER_ID, OPERATION_TYPE, CONTRACT_VERSION)
        or operation.depends_on
    ):
        raise IssueReadHold("unsupported_issue_read_operation")


def issue_read_input_digest(target: VerifiedTarget) -> str:
    """Bind the entire verified minimal Envelope, not just the Issue number."""
    _target(target)
    canonical = json.dumps(
        {
            "repository": target.repository,
            "repository_id": target.repository_id,
            "number": target.number,
            "action": target.action,
            "updated_at": target.updated_at,
            "kind": target.kind,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return digest_bytes(canonical)


def make_issue_read_intent(*, operation: OperationNode, target: VerifiedTarget) -> OperationIntent:
    """Construct a Root Intent matching exactly one previously verified target."""
    _operation(operation)
    return OperationIntent(
        operation=operation,
        input_digest=issue_read_input_digest(target),
        effect_digest=_READ_EFFECT,
        preconditions_digest=_READ_PRECONDITION,
        postconditions_digest=_READ_POSTCONDITION,
        retry_safety_rule=RETRY_RULE,
        probe_contract=PROBE_CONTRACT,
    )


def decode_issue_read_checkpoint(*, raw: bytes, target: VerifiedTarget, intent: OperationIntent) -> OperationCheckpoint:
    """Validate a bounded response and hash only minimal fields into a Checkpoint.

    The raw GitHub response is never part of the resulting durable value.
    A digest does not independently authenticate a server response.
    """
    if type(intent) is not OperationIntent or intent != make_issue_read_intent(
        operation=intent.operation, target=target
    ):
        raise IssueReadHold("issue_read_intent_mismatch")
    if type(raw) is not bytes or not 0 < len(raw) <= MAX_RESPONSE_BYTES:
        raise IssueReadHold("issue_response_limit")
    try:
        value: object = decode_json(
            raw,
            duplicate_message="Duplicate GitHub response field.",
            constant_message="Invalid GitHub response number.",
        )
    except (ValueError, UnicodeError, RecursionError) as error:
        raise IssueReadHold("invalid_issue_response") from error
    if type(value) is not dict:
        raise IssueReadHold("invalid_issue_response")
    issue = cast(dict[str, object], value)
    issue_id = issue.get("id")
    number = issue.get("number")
    state = issue.get("state")
    updated_at = issue.get("updated_at")
    labels = issue.get("labels")
    repository_url = issue.get("repository_url")
    if (
        type(issue_id) is not int
        or issue_id <= 0
        or type(number) is not int
        or number != target.number
        or state not in ("open", "closed")
        or type(state) is not str
        or type(updated_at) is not str
        or _TIMESTAMP.fullmatch(updated_at) is None
        or type(labels) is not list
        or type(repository_url) is not str
        or repository_url.casefold() != ("https://api.github.com/repos/" + target.repository).casefold()
        or "pull_request" in issue
    ):
        raise IssueReadHold("issue_identity_or_shape_mismatch")
    try:
        datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
    except ValueError as error:
        raise IssueReadHold("invalid_issue_timestamp") from error

    names: list[str] = []
    for element in cast(list[object], labels):
        if type(element) is not dict:
            raise IssueReadHold("invalid_issue_label")
        name = cast(dict[str, object], element).get("name")
        if type(name) is not str or not 0 < len(name) <= 100 or any(c in name for c in "\r\n\x00"):
            raise IssueReadHold("invalid_issue_label")
        names.append(name)
    if len({name.casefold() for name in names}) != len(names):
        raise IssueReadHold("duplicate_issue_label")

    label_bytes = json.dumps(sorted(names, key=str.casefold), separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return OperationCheckpoint(
        operation_id=intent.operation.operation_id,
        intent_digest=intent.intent_digest,
        output_schema_version=OUTPUT_VERSION,
        fields=(
            CheckpointField("issue_id", digest_bytes(str(issue_id).encode("ascii"))),
            CheckpointField("number", digest_bytes(str(number).encode("ascii"))),
            CheckpointField("state", digest_bytes(state.encode("utf-8"))),
            CheckpointField("updated_at", digest_bytes(updated_at.encode("ascii"))),
            CheckpointField("labels", digest_bytes(label_bytes)),
        ),
        verification_contract=PROBE_CONTRACT,
        verification_evidence_digest=digest_bytes(raw),
    )


@dataclass(frozen=True, slots=True, repr=False)
class GitHubIssueHttpTransport:
    """Opt-in GET-only transport using the standard library and fixed GitHub host."""

    token: str | None = None

    def __post_init__(self) -> None:
        if self.token is not None and (
            type(self.token) is not str or not 0 < len(self.token) <= 8192 or any(c in self.token for c in "\r\n\x00")
        ):
            raise ValueError("Invalid GitHub access token configuration.")

    async def __call__(self, target: VerifiedTarget) -> bytes:
        _target(target)

        def fetch() -> bytes:
            connection = http.client.HTTPSConnection("api.github.com", timeout=10)
            try:
                headers = {
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": API_VERSION,
                    "User-Agent": "local-dev-mcp",
                }
                if self.token is not None:
                    headers["Authorization"] = "Bearer " + self.token
                connection.request(
                    "GET", "/repos/" + target.repository + "/issues/" + str(target.number), headers=headers
                )
                response = connection.getresponse()
                if response.status != 200:
                    raise IssueReadHold("issue_http_status_unaccepted")
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise IssueReadHold("issue_response_limit")
                return raw
            except (OSError, http.client.HTTPException) as error:
                raise IssueReadHold("issue_transport_failed") from error
            finally:
                connection.close()

        return await asyncio.to_thread(fetch)


def bind_issue_read_contract(
    *,
    intent: OperationIntent,
    target: VerifiedTarget,
    transport: IssueReadTransport,
    test_evidence_digest: str,
) -> ConsumerOperationContract:
    """Bind explicit per-Operation context without rewriting the B3-S hooks.

    Neither the Catalog nor these hooks execute a Job. The caller must
    durably commit the Intent first and separately gate any invocation.
    """
    if type(intent) is not OperationIntent or intent != make_issue_read_intent(
        operation=intent.operation, target=target
    ):
        raise IssueReadHold("issue_read_intent_mismatch")
    if not callable(transport):
        raise TypeError("Issue transport must be an async callable.")
    checked_digest(test_evidence_digest, "test_evidence_digest")

    def require_intent(value: OperationIntent) -> None:
        if value != intent:
            raise IssueReadHold("issue_read_intent_mismatch")

    async def guard(value: OperationIntent) -> object:
        require_intent(value)
        return None

    async def execute(value: OperationIntent) -> object:
        require_intent(value)
        return await transport(target)

    async def probe(value: OperationIntent) -> object:
        require_intent(value)
        return await transport(target)

    def decode(raw: bytes) -> OperationCheckpoint:
        return decode_issue_read_checkpoint(raw=raw, target=target, intent=intent)

    async def verify(value: OperationIntent, checkpoint: OperationCheckpoint) -> bool:
        require_intent(value)
        if type(checkpoint) is not OperationCheckpoint:
            return False
        fresh = decode_issue_read_checkpoint(raw=await transport(target), target=target, intent=intent)
        return fresh == checkpoint

    return ConsumerOperationContract(
        consumer_id=CONSUMER_ID,
        operation_type=OPERATION_TYPE,
        contract_version=CONTRACT_VERSION,
        retry_safety_rule=RETRY_RULE,
        probe_contract=PROBE_CONTRACT,
        concurrency=ConcurrencyProtection(CONCURRENCY_LEVEL, CONCURRENCY_LEVEL, test_evidence_digest, guard),
        execute=execute,
        probe=probe,
        decode=decode,
        verify=verify,
    )
