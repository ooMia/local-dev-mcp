"""Known-value diagnostics stay private across encoding and collection boundaries."""

import base64
import json
from urllib.parse import quote, quote_plus

import pytest

from local_dev_mcp.redaction import (
    MAX_SECRET_BYTES,
    MAX_SECRET_VALUES,
    REDACTED,
    WITHHELD,
    BoundedRedactedOutput,
    Redactor,
)

pytestmark = pytest.mark.unit


def _collect(redactor: Redactor, chunks: list[bytes], limit: int = 65536) -> BoundedRedactedOutput:
    output = BoundedRedactedOutput(redactor, limit)
    for chunk in chunks:
        output.feed(chunk)
    output.finish()
    return output


def test_known_values_and_supported_encodings_are_hidden_without_per_test_variable_lists() -> None:
    secret = '한글 token+/>="\nnext'
    redactor = Redactor.from_values({"NEW_DATABASE_VALUE": secret})
    encoded = secret.encode()
    forms = {
        secret,
        json.dumps(secret)[1:-1],
        json.dumps(secret, ensure_ascii=False)[1:-1],
        quote(secret, safe=""),
        quote_plus(secret, safe=""),
        base64.b64encode(encoded).decode(),
        base64.urlsafe_b64encode(encoded).decode(),
        base64.b64encode(encoded).decode().rstrip("="),
        base64.urlsafe_b64encode(encoded).decode().rstrip("="),
    }
    assert redactor.safe
    for form in forms:
        assert redactor.sanitize("before:" + form + ":after") == "before:" + REDACTED + ":after"


@pytest.mark.parametrize("secret", ["password123", "비밀 token\nsecond line", ">>>???"])
def test_every_byte_split_preserves_unicode_and_redacts_complete_values(secret: str) -> None:
    redactor = Redactor.from_values({"SECRET": secret})
    forms = [secret, json.dumps(secret)[1:-1], quote(secret, safe=""), base64.b64encode(secret.encode()).decode()]
    for form in forms:
        payload = ("시작:" + form + ":끝").encode()
        for split in range(len(payload) + 1):
            output = _collect(redactor, [payload[:split], payload[split:]])
            assert output.text == "시작:" + REDACTED + ":끝"
            assert output.complete
            assert output.reason is None


def test_overlapping_secrets_redact_the_union_across_all_chunk_boundaries() -> None:
    redactor = Redactor.from_values({"FIRST": "abcdefg", "SECOND": "efghijk"})
    payload = b"start:abcdefghijk:end"
    for split in range(len(payload) + 1):
        output = _collect(redactor, [payload[:split], payload[split:]])
        assert output.text == "start:" + REDACTED + ":end"
        assert output.complete


def test_continuously_overlapping_matches_do_not_require_an_unbounded_raw_tail() -> None:
    redactor = Redactor.from_values({"SECRET": "aaaa"})
    output = _collect(redactor, [b"a" * 1024 for _ in range(80)], limit=len(REDACTED))
    assert output.text == REDACTED
    assert output.complete


def test_output_cap_is_applied_after_redaction_instead_of_clipping_the_secret_first() -> None:
    secret = "private-" * 100
    output = _collect(Redactor.from_values({"SECRET": secret}), [("x" + secret + "y").encode()], limit=12)
    assert output.text == "x" + REDACTED + "y"
    assert output.complete


def test_a_secret_crossing_the_output_cap_never_exposes_its_prefix() -> None:
    redactor = Redactor.from_values({"SECRET": "private-password"})
    for split in range(21):
        payload = b"ok private-password!"
        output = _collect(redactor, [payload[:split], payload[split:]], limit=8)
        assert output.text == "ok "
        assert not output.complete
        assert output.reason == "output_limit"


def test_utf8_output_limit_preserves_valid_complete_characters() -> None:
    output = _collect(Redactor.from_values({}), ["가나다".encode()], limit=4)
    assert output.text == "가"
    assert not output.complete
    assert output.reason == "output_limit"


def test_private_values_win_when_an_equal_value_is_explicitly_public_elsewhere() -> None:
    redactor = Redactor.from_values({"PUBLIC": "shared-value", "PRIVATE": "shared-value"}, ("PUBLIC",))
    assert redactor.safe
    assert redactor.sanitize("shared-value") == REDACTED


def test_url_percent_hex_case_is_supported_without_lowercasing_unescaped_text() -> None:
    redactor = Redactor.from_values({"SECRET": "abc/def XYZ"})
    output = _collect(redactor, [b"abc%2fdef+XYZ abc%2fdef%20XYZ"])
    assert output.text == REDACTED + " " + REDACTED
    assert output.complete


def test_public_operational_flags_need_no_short_secret_withholding() -> None:
    redactor = Redactor.from_values({"FLAG": "1", "ABSENT": None, "EMPTY": ""}, ("FLAG",))
    output = _collect(redactor, [b"worker count=1"])
    assert redactor.safe
    assert output.text == "worker count=1"
    assert output.complete


@pytest.mark.parametrize("secret", ["1", "no", "yes"])
def test_short_private_values_withhold_the_channel_instead_of_being_silently_ignored(secret: str) -> None:
    redactor = Redactor.from_values({"UNKNOWN": secret})
    assert not redactor.safe
    assert redactor.reason == "private_value_too_short"
    assert redactor.sanitize("arbitrary metadata " + secret) == WITHHELD
    output = _collect(redactor, [("raw " + secret).encode()])
    assert output.text == WITHHELD
    assert not output.complete
    assert output.reason == "private_value_too_short"


@pytest.mark.parametrize("payload", [b"safe prefix\xff", b"safe prefix\xe2\x82"])
def test_invalid_or_unfinished_utf8_withholds_the_entire_channel(payload: bytes) -> None:
    output = _collect(Redactor.from_values({}), [payload[:11], payload[11:]])
    assert output.text == WITHHELD
    assert not output.complete
    assert output.reason == "invalid_utf8"


def test_invalid_utf8_after_truncation_still_withholds_the_channel() -> None:
    output = _collect(Redactor.from_values({}), [b"safe output" * 100, b"\xff"], limit=30)
    assert output.text == WITHHELD
    assert output.reason == "invalid_utf8"


def test_invalid_unicode_metadata_is_withheld_before_result_serialization() -> None:
    assert Redactor.from_values({}).sanitize("broken\ud800metadata") == WITHHELD


def test_catalog_limits_fail_closed_without_preserving_any_diagnostic_source() -> None:
    cases = [
        ({"SECRET": "x" * (MAX_SECRET_BYTES + 1)}, "private_value_limit"),
        ({str(index): f"secret-{index:04d}" for index in range(MAX_SECRET_VALUES + 1)}, "private_value_count_limit"),
        ({str(index): f"{index:03d}" + "Ω" * 4094 for index in range(32)}, "redaction_catalog_limit"),
        ({"SECRET": "bad\ud800value"}, "invalid_private_value"),
    ]
    for values, reason in cases:
        redactor = Redactor.from_values(values)
        assert not redactor.safe
        assert redactor.reason == reason
        output = _collect(redactor, [b"potentially sensitive diagnostic"])
        assert output.text == WITHHELD
        assert output.reason == reason


def test_unknown_runtime_values_are_not_claimed_to_be_discovered() -> None:
    output = _collect(Redactor.from_values({"KNOWN": "known-password"}), [b"dynamic-token"])
    assert output.text == "dynamic-token"
    assert output.complete


def test_output_is_bounded_for_large_chunks_and_finish_is_idempotent() -> None:
    output = _collect(Redactor.from_values({}), [b"safe " * 30000], limit=32)
    output.finish()
    assert output.text == "safe " * 6 + "sa"
    assert output.reason == "output_limit"
    assert not output.complete
    with pytest.raises(ValueError, match="already finished"):
        output.feed(b"more")


def test_zero_budget_returns_no_content_and_negative_budget_is_invalid() -> None:
    output = _collect(Redactor.from_values({}), [b"content"], limit=0)
    assert output.text == ""
    assert output.reason == "output_limit"
    with pytest.raises(ValueError, match="nonnegative"):
        BoundedRedactedOutput(Redactor.from_values({}), limit=-1)
