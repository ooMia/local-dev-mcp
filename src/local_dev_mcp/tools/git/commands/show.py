"""Verify an observed commit's signature against the configured key family."""

from .._execution import GitExecution


async def verify_signature(execution: GitExecution, commit: str, fingerprint: str) -> bool:
    result = await execution.run(
        "show",
        "--no-patch",
        "--no-show-signature",
        "--no-notes",
        "--format=%G?%n%GF%n%GP",
        commit,
        operation="Verify observed commit signature",
    )
    lines = result.output.decode("ascii").strip().splitlines()
    expected = fingerprint.removesuffix("!").upper()
    # G/U distinguish owner trust, not cryptographic validity.
    verified = len(lines) == 3 and lines[0] in ("G", "U") and expected in (lines[1].upper(), lines[2].upper())
    if fingerprint.endswith("!"):
        verified = verified and lines[1].upper() == expected
    return verified
