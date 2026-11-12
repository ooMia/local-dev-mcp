"""Read-only MCP elicitation diagnostics; never authorizes local Git writes."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal, TypedDict, cast

from fastmcp import Context
from mcp.types import ElicitRequest, ElicitRequestFormParams, ElicitResult, InputRequiredResult, ToolAnnotations

from ..mcp.registry import ToolDefinition

if TYPE_CHECKING:
    from ..app.runtime import ServerSettings


class ElicitationCapabilities(TypedDict):
    protocol_version: str | None
    request_meta_present: bool
    request_form: bool
    request_url: bool
    request_capabilities_kind: str
    request_elicitation_kind: str
    request_elicitation_fields: list[str]
    session_form: bool | None
    session_available: bool
    request_form_usable: bool
    next_action: str


class ElicitationProbe(TypedDict):
    status: Literal["blocked", "responded"]
    action: Literal["not_requested", "accept", "decline", "cancel"]
    confirmed: bool
    operation_performed: bool
    reason: Literal[
        "none", "form_not_declared", "legacy_request_unsupported", "mrtr_state_invalid", "mrtr_response_missing"
    ]


@dataclass(frozen=True)
class DiagnosticConfirmation:
    confirmed: bool


def _as_record(value: object | None) -> dict[str, object] | None:
    if isinstance(value, Mapping):
        typed = cast(Mapping[object, object], value)
        return {str(key): entry for key, entry in typed.items()}
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        record = dump(exclude_none=True)
        if isinstance(record, dict):
            return cast(dict[str, object], record)
    return None


def classify_elicitation(
    sdk_caps: object | None, request_meta: object | None, protocol: str | None
) -> ElicitationCapabilities:
    """Per-call metadata is authoritative; legacy empty elicitation means form."""
    meta = _as_record(request_meta)
    cap_record = meta.get("io.modelcontextprotocol/clientCapabilities") if meta is not None else None
    capabilities = _as_record(cap_record)
    raw_elicitation = capabilities.get("elicitation") if capabilities is not None else None
    declaration = _as_record(raw_elicitation)
    form = declaration is not None and (not declaration or _as_record(declaration.get("form")) is not None)
    url = declaration is not None and _as_record(declaration.get("url")) is not None
    sdk_form: bool | None = None
    if sdk_caps is not None:
        sdk_elicitation = getattr(sdk_caps, "elicitation", None)
        if isinstance(sdk_caps, dict):
            record = cast(dict[str, object], sdk_caps)
            sdk_elicitation = record.get("elicitation")
        sdk_form = sdk_elicitation is not None
    legacy_supported = protocol in {"2025-11-25", "2025-06-18", "2025-03-26"}
    usable = form and legacy_supported
    return {
        "protocol_version": protocol,
        "request_meta_present": cap_record is not None,
        "request_form": form,
        "request_url": url,
        "request_capabilities_kind": type(cap_record).__name__,
        "request_elicitation_kind": type(raw_elicitation).__name__,
        "request_elicitation_fields": [
            key for key in ("form", "url") if declaration is not None and key in declaration
        ],
        "session_form": sdk_form,
        "session_available": sdk_caps is not None,
        "request_form_usable": usable,
        "next_action": (
            (
                "Form declared but this protocol requires InputRequiredResult multi-round requests; "
                "legacy ctx.elicit is blocked."
            )
            if form and not usable
            else (
                "Form declared on a legacy protocol; an optional no-op server-initiated probe is possible."
                if usable
                else "Client did not declare per-request Form elicitation; do not send elicitation/create."
            )
        ),
    }


def _observe(ctx: Context) -> ElicitationCapabilities:
    session = ctx.session
    params = getattr(session, "client_params", None)
    sdk = getattr(session, "client_capabilities", None)
    if sdk is None and params is not None:
        sdk = getattr(params, "capabilities", None)
    version = getattr(params, "protocol_version", None)
    request = ctx.request_context
    meta = request.meta if request is not None else None
    return classify_elicitation(sdk, meta, version if isinstance(version, str) else None)


@dataclass(frozen=True)
class DiagnoseClientElicitationTool:
    settings: ServerSettings

    async def execute(self, ctx: Context) -> ElicitationCapabilities:
        return _observe(ctx)


def diagnostic_action(action: Literal["accept", "decline", "cancel"], confirmed: bool = False) -> ElicitationProbe:
    """Interpret an elicitation answer without ever producing approval authority."""
    return {
        "status": "responded",
        "action": action,
        "confirmed": action == "accept" and confirmed,
        "operation_performed": False,
        "reason": "none",
    }


@dataclass(frozen=True)
class ProbeClientElicitationTool:
    settings: ServerSettings

    async def execute(self, ctx: Context) -> ElicitationProbe:
        capability = _observe(ctx)
        if not capability["request_form_usable"]:
            return {
                "status": "blocked",
                "action": "not_requested",
                "confirmed": False,
                "operation_performed": False,
                "reason": "legacy_request_unsupported" if capability["request_form"] else "form_not_declared",
            }
        result = await ctx.elicit(
            "진단 전용: 이 확인 응답은 어떤 Git 작업이나 승인도 실행하지 않습니다.",
            response_type=DiagnosticConfirmation,
        )
        action = result.action
        if action == "accept":
            data: object = getattr(result, "data", None)
            return diagnostic_action("accept", isinstance(data, DiagnosticConfirmation) and data.confirmed)
        if action in ("decline", "cancel"):
            return diagnostic_action(action)
        return {
            "status": "blocked",
            "action": "not_requested",
            "confirmed": False,
            "operation_performed": False,
            "reason": "legacy_request_unsupported",
        }


DIAGNOSE_CLIENT_ELICITATION: Final = ToolDefinition(
    name="diagnose_client_elicitation",
    description=(
        "Read-only comparison of per-request MCP elicitation Form/URL metadata and negotiated SDK "
        "client capabilities. Returns no raw metadata and never requests user input or performs writes."
    ),
    annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False),
    required_commands=(),
    create_runtime=DiagnoseClientElicitationTool,
)

PROBE_CLIENT_ELICITATION: Final = ToolDefinition(
    name="probe_client_elicitation",
    description=(
        "Diagnostic-only no-op Form elicitation on legacy protocol when per-request Form is declared. "
        "Records accept/decline/cancel without running Git, issuing merge approval, or storing secrets. "
        "2026-07-28 InputRequiredResult mode is not implemented here and is blocked before requesting input."
    ),
    annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False),
    required_commands=(),
    create_runtime=ProbeClientElicitationTool,
)


# Modern MCP (2026-07-28): each round is an independent tools/call.
# This probe is intentionally incapable of authorizing any Git operation.
_MRTR_DIAGNOSTIC_STATE = "mrtr-diagnostic-v1"


def modern_diagnostic_request() -> InputRequiredResult:
    return InputRequiredResult(
        result_type="input_required",
        input_requests={
            "diagnostic": ElicitRequest(
                method="elicitation/create",
                params=ElicitRequestFormParams(
                    message="진단 전용: 이 응답은 Git 작업 실행이나 병합 승인을 의미하지 않습니다.",
                    requested_schema={
                        "type": "object",
                        "properties": {"confirmed": {"type": "boolean", "title": "진단 응답"}},
                        "required": ["confirmed"],
                    },
                ),
            )
        },
        request_state=_MRTR_DIAGNOSTIC_STATE,
    )


def modern_diagnostic_response(responses: Mapping[str, object] | None, state: str | None) -> ElicitationProbe:
    """Read an echoed round; never treat a client response as Git authority."""
    if state != _MRTR_DIAGNOSTIC_STATE:
        return {
            "status": "blocked",
            "action": "not_requested",
            "confirmed": False,
            "operation_performed": False,
            "reason": "mrtr_state_invalid",
        }
    answer = responses.get("diagnostic") if responses is not None else None
    if not isinstance(answer, ElicitResult):
        return {
            "status": "blocked",
            "action": "not_requested",
            "confirmed": False,
            "operation_performed": False,
            "reason": "mrtr_response_missing",
        }
    if answer.action == "accept":
        data = answer.content
        return diagnostic_action("accept", isinstance(data, dict) and data.get("confirmed") is True)
    if answer.action in ("decline", "cancel"):
        return diagnostic_action(answer.action)
    return {
        "status": "blocked",
        "action": "not_requested",
        "confirmed": False,
        "operation_performed": False,
        "reason": "mrtr_response_missing",
    }


@dataclass(frozen=True)
class ProbeClientElicitationMrtrTool:
    settings: ServerSettings

    async def execute(self, ctx: Context) -> ElicitationProbe | InputRequiredResult:
        capability = _observe(ctx)
        if capability["protocol_version"] != "2026-07-28":
            return {
                "status": "blocked",
                "action": "not_requested",
                "confirmed": False,
                "operation_performed": False,
                "reason": "legacy_request_unsupported",
            }
        if not capability["request_form"]:
            return {
                "status": "blocked",
                "action": "not_requested",
                "confirmed": False,
                "operation_performed": False,
                "reason": "form_not_declared",
            }
        if ctx.input_responses is None:
            return modern_diagnostic_request()
        return modern_diagnostic_response(ctx.input_responses, ctx.request_state)


PROBE_CLIENT_ELICITATION_MRTR: Final = ToolDefinition(
    name="probe_client_elicitation_mrtr",
    description=(
        "No-op 2026-07-28 Form Elicitation MRTR probe. Uses InputRequiredResult only "
        "when the calling request declares Form support; validates echoed round state. "
        "Accept, decline and cancel never grant Git approval or perform any write."
    ),
    annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False),
    required_commands=(),
    create_runtime=ProbeClientElicitationMrtrTool,
)
