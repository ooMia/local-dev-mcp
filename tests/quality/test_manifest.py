"""Shared quality profile schema and registered-recipe contract."""

import pytest

from local_dev_mcp.quality import parse_quality_manifest
from local_dev_mcp.settings import JobDefinition

pytestmark = pytest.mark.unit


def _text(checks: str, *, clean: str = "true") -> str:
    return f"schema_version = 1\n[profiles.pre_merge]\nchecks = {checks}\nrequire_clean = {clean}\n"


def test_profile_selects_existing_tasks_in_order_and_retains_digest() -> None:
    manifest = parse_quality_manifest(_text('["lint", "unit"]'))
    jobs = (
        JobDefinition("unit", "test", "task.unit", ("python", "-m", "pytest"), "unit", ("tests",)),
        JobDefinition("lint", "lint", "task.lint", ("python", "-m", "ruff")),
    )
    profile, selected = manifest.select("pre_merge", jobs)
    assert profile.checks == ("lint", "unit")
    assert tuple(job.name for job in selected) == profile.checks
    assert len(manifest.digest) == 64
    assert manifest.digest == parse_quality_manifest(_text('["lint", "unit"]')).digest


@pytest.mark.parametrize(
    "content",
    [
        _text('["lint", "lint"]'),
        _text("[]"),
        _text('["lint"]', clean='"yes"'),
        'schema_version = 2\n[profiles.pre_merge]\nchecks = ["lint"]\nrequire_clean = true\n',
        _text('["lint"]') + "unexpected = true\n",
        'schema_version = 1\n[profiles.pre_merge]\nchecks = ["Lint"]\nrequire_clean = true\n',
        'schema_version = 1\n[profiles.pre_merge]\nchecks = ["lint"]\n',
    ],
)
def test_invalid_manifest_is_rejected(content: str) -> None:
    with pytest.raises(ValueError):
        parse_quality_manifest(content)


def test_missing_or_modifying_recipe_is_rejected() -> None:
    manifest = parse_quality_manifest(_text('["check"]'))
    with pytest.raises(ValueError, match="not a registered"):
        manifest.select("pre_merge", ())
    modifying = JobDefinition("check", "format", "task.check", ("python", "-m", "black"))
    with pytest.raises(ValueError, match="modifies"):
        manifest.select("pre_merge", (modifying,))


def test_profile_does_not_accept_implicit_fallback() -> None:
    manifest = parse_quality_manifest(_text('["lint"]'))
    with pytest.raises(ValueError, match="Unknown quality profile"):
        manifest.select("legacy", ())
