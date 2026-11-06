"""Pinned persisted evidence vectors; serialization changes require an explicit version decision."""

import hashlib

import pytest

from local_dev_mcp.tools.gh.delivery import (
    CheckpointField,
    LineageBinding,
    OperationAttempt,
    OperationCheckpoint,
    OperationIntent,
    OperationLineage,
    OperationNode,
    PlanStructure,
)

pytestmark = pytest.mark.unit
D = "sha256:" + "a" * 64
E = "sha256:" + "b" * 64
type Evidence = PlanStructure | OperationIntent | OperationCheckpoint | OperationLineage | OperationAttempt


@pytest.mark.parametrize(
    "value,raw,digest",
    [
        (
            PlanStructure((OperationNode("read", "github_issue", "read_current", "read:v1"),)),
            (
                b'{"operations":[{"consumer_id":"github_issue","contract_version":"read:v1","depen'
                b'ds_on":[],"operation_id":"read","operation_type":"read_current"}],"structure_ver'
                b'sion":"b3s-plan:1"}'
            ),
            "sha256:0e329ac8f397c5f450e5aae557c4ea05aad887301527ca81ab304bbe6dc7126d",
        ),
        (
            OperationIntent(
                OperationNode("read", "github_issue", "read_current", "read:v1"),
                D,
                E,
                D,
                E,
                "hold_without_probe",
                "github_readonly:v1",
            ),
            (
                b'{"consumed_outputs":[],"effect_digest":"sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'
                b'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","input_digest":"sha256:aaaaaaaaaaaaaaaaaaaaaaaa'
                b'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","intent_version":"b3s-intent:1","opera'
                b'tion":{"consumer_id":"github_issue","contract_version":"read:v1","depends_on":[]'
                b',"operation_id":"read","operation_type":"read_current"},"postconditions_digest":'
                b'"sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","preco'
                b'nditions_digest":"sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
                b'aaaaaaaaa","probe_contract":"github_readonly:v1","retry_safety_rule":"hold_witho'
                b'ut_probe"}'
            ),
            "sha256:a615361c8fbb07c651afbb6197fa5baf5276226d3747266869cae5368d269ee7",
        ),
        (
            OperationCheckpoint(
                "read",
                D,
                "issue_read:v1",
                (CheckpointField("status", E),),
                "github_get_issue:v1",
                D,
            ),
            (
                b'{"checkpoint_version":"b3s-checkpoint:1","fields":[{"field_id":"status","value_d'
                b'igest":"sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"'
                b'}],"intent_digest":"sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
                b'aaaaaaaaaaa","operation_id":"read","output_schema_version":"issue_read:v1","veri'
                b'fication_contract":"github_get_issue:v1","verification_evidence_digest":"sha256:'
                b'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}'
            ),
            "sha256:a774af14139c57521464b65ec29791f6e4277f733a3016144a6068108ad82254",
        ),
        (
            OperationLineage(
                (
                    LineageBinding(
                        "read",
                        D,
                        "status",
                        E,
                        "issue_read:v1",
                        "desired_status",
                        "issue_status:v1",
                    ),
                )
            ),
            (
                b'{"bindings":[{"binding_version":"issue_status:v1","source_checkpoint_digest":"sh'
                b'a256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","source_f'
                b'ield_digest":"sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'
                b'bbbbb","source_field_id":"status","source_operation_id":"read","source_schema_ve'
                b'rsion":"issue_read:v1","target_field_id":"desired_status"}],"lineage_version":"b'
                b'3s-lineage:1"}'
            ),
            "sha256:fd6b73be1587a8a590e4e46941cf1d5cf5aae0a20a7747ef9d7dbcae8c8c98c4",
        ),
        (
            OperationAttempt(1, "label", 2, D),
            (
                b'{"event_id":1,"intent_digest":"sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
                b'aaaaaaaaaaaaaaaaaaaaaa","operation_id":"label","origin_generation":2,"version":"'
                b'b3s-attempt:1"}'
            ),
            "sha256:705fa847fb64c1935485c4ae006ed95e185b972ef1963fdc9df71e3051c17723",
        ),
    ],
)
def test_persisted_canonical_vectors(value: Evidence, raw: bytes, digest: str) -> None:
    assert value.canonical_bytes() == raw
    assert type(value).from_canonical_bytes(raw) == value
    assert "sha256:" + hashlib.sha256(raw).hexdigest() == digest
    if isinstance(value, PlanStructure):
        assert value.structure_digest == digest
    elif isinstance(value, OperationIntent):
        assert value.intent_digest == digest
    elif isinstance(value, OperationCheckpoint):
        assert value.checkpoint_digest == digest
    elif isinstance(value, OperationLineage):
        assert value.lineage_digest == digest
    else:
        assert value.attempt_digest == digest


@pytest.mark.parametrize(
    "kind,duplicate,nonfinite",
    [
        (PlanStructure, "Duplicate JSON key in plan.", "Nonfinite value in plan."),
        (OperationIntent, "Duplicate key in Intent.", "Nonfinite value in Intent."),
        (
            OperationCheckpoint,
            "Duplicate canonical evidence field.",
            "Nonfinite JSON literal.",
        ),
        (
            OperationLineage,
            "Duplicate canonical evidence field.",
            "Nonfinite JSON literal.",
        ),
    ],
)
def test_canonical_decoders_keep_strict_json_failures(
    kind: type[PlanStructure] | type[OperationIntent] | type[OperationCheckpoint] | type[OperationLineage],
    duplicate: str,
    nonfinite: str,
) -> None:
    for raw, expected in (
        (b'{"x":{"a":1,"a":2}}', duplicate),
        (b'{"x":NaN}', nonfinite),
        (b'{"x":Infinity}', nonfinite),
    ):
        with pytest.raises(ValueError) as caught:
            kind.from_canonical_bytes(raw)
        assert str(caught.value) == expected
