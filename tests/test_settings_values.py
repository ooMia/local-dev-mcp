from pathlib import Path

import pytest

from local_dev_mcp.errors import ConfigurationError
from local_dev_mcp.settings import JobDefinition, Policy, PolicyValues, parse_dotenv, parse_policy

pytestmark = pytest.mark.unit


def test_policy_text_preserves_declared_paths_and_commands() -> None:
    assert parse_policy('[filesystem]\nallowed_paths=[".", "../shared"]\n[system]\nallowed_commands=["git"]') == (
        PolicyValues((".", "../shared"), ("git",))
    )


@pytest.mark.parametrize(
    "text",
    [
        "[",
        "",
        "[filesystem]\nallowed_paths=[]",
        '[filesystem]\nallowed_paths="."\n[system]\nallowed_commands=[]',
        "[filesystem]\nallowed_paths=[1]\n[system]\nallowed_commands=[]",
        '[filesystem]\nallowed_paths=[" "]\n[system]\nallowed_commands=[]',
        "filesystem=1\n[system]\nallowed_commands=[]",
    ],
)
def test_policy_text_rejects_invalid_structure_without_path_access(text: str) -> None:
    with pytest.raises(ConfigurationError):
        parse_policy(text)


def test_empty_allowlists_remain_empty() -> None:
    values = parse_policy("[filesystem]\nallowed_paths=[]\n[system]\nallowed_commands=[]")
    assert values == PolicyValues((), ())


def test_policy_text_preserves_read_only_and_denied_scopes() -> None:
    values = parse_policy(
        '[filesystem]\nallowed_paths=["src", "docs"]\nread_only_paths=["docs"]\n'
        'denied_paths=["src/credentials.json"]\n[system]\nallowed_commands=[]'
    )

    assert values == PolicyValues(("src", "docs"), (), ("docs",), ("src/credentials.json",))


@pytest.mark.parametrize("setting", ["read_only_paths", "denied_paths"])
@pytest.mark.parametrize("value", ['"private-marker"', "[1]", '[" "]', "false", "{}"])
def test_optional_path_rules_reject_invalid_explicit_values(setting: str, value: str) -> None:
    text = f'[filesystem]\nallowed_paths=["."]\n{setting}={value}\n[system]\nallowed_commands=[]'

    with pytest.raises(ConfigurationError) as caught:
        parse_policy(text)

    assert caught.value.target == f"filesystem.{setting}"
    assert "private-marker" not in caught.value.diagnostic()


def test_empty_optional_path_rules_grant_nothing() -> None:
    assert parse_policy(
        "[filesystem]\nallowed_paths=[]\nread_only_paths=[]\ndenied_paths=[]\n[system]\nallowed_commands=[]"
    ) == PolicyValues((), ())


@pytest.mark.parametrize(
    ("text", "target"),
    [
        ('[filesystem]\n[system]\nallowed_commands=["git"]', "filesystem.allowed_paths"),
        ('[filesystem]\nallowed_paths=["."]\n[system]', "system.allowed_commands"),
    ],
)
def test_policy_text_reports_each_missing_required_setting(text: str, target: str) -> None:
    with pytest.raises(ConfigurationError) as caught:
        parse_policy(text)

    assert caught.value.target == target
    assert caught.value.reason == "Required setting is missing."


def test_dotenv_text_keeps_literal_interpolation_and_valueless_declarations() -> None:
    assert parse_dotenv('VALUE="${USER}"\nEMPTY=\nBARE\n') == {"VALUE": "${USER}", "EMPTY": "", "BARE": None}


def test_policy_permission_is_a_value_relation() -> None:
    root = Path("/synthetic/workspace")
    policy = Policy(root, (Path("/synthetic"),), ("git",))
    policy.require_project_access()
    policy.require_command("git")
    with pytest.raises(PermissionError):
        policy.require_command("gpg")
    with pytest.raises(PermissionError):
        Policy(root, (), ()).require_project_access()


BASE_POLICY = '[filesystem]\nallowed_paths=["."]\n[system]\nallowed_commands=["checks.unit"]\n'
UNIT_JOB = (
    'kind="test"\npermission="checks.unit"\nargv=["pytest", "-n", "0"]\nlevel="unit"\ndefault_targets=["tests"]\n'
)


def test_named_jobs_preserve_argv_and_explicit_suite_targets() -> None:
    values = parse_policy(
        BASE_POLICY
        + "[jobs.unit]\n"
        + UNIT_JOB
        + '[jobs.format]\nkind="format"\npermission="checks.format"\nargv=["ruff", "format", "."]\n'
        + 'timeout_seconds=90\n[jobs_environment]\npublic_names=["APP_MODE", "LANG"]\n'
    )

    assert values.jobs == (
        JobDefinition("unit", "test", "checks.unit", ("pytest", "-n", "0"), "unit", ("tests",)),
        JobDefinition("format", "format", "checks.format", ("ruff", "format", "."), timeout_seconds=90),
    )
    assert values.public_environment == ("APP_MODE", "LANG")
    assert parse_policy(BASE_POLICY).jobs == ()
    assert parse_policy(BASE_POLICY + "[jobs_environment]\n").public_environment == ()


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ('kind="test"', 'kind="private-marker"'),
        ('permission="checks.unit"', 'permission="uv"'),
        ('permission="checks.unit"', 'permission="checks.*"'),
        ('argv=["pytest", "-n", "0"]', "argv=[]"),
        ('argv=["pytest", "-n", "0"]', 'argv=["pytest", 0]'),
        ('argv=["pytest", "-n", "0"]', 'argv=["pytest", "private-marker\\u0000"]'),
        ('level="unit"\n', ""),
        ('level="unit"', 'level="private-marker"'),
        ('default_targets=["tests"]\n', ""),
        ('default_targets=["tests"]', "default_targets=[]"),
        ('default_targets=["tests"]', 'default_targets=[" "]'),
    ],
)
def test_job_schema_rejects_invalid_values_without_echoing_them(field: str, invalid: str) -> None:
    with pytest.raises(ConfigurationError) as caught:
        parse_policy(BASE_POLICY + "[jobs.unit]\n" + UNIT_JOB.replace(field, invalid))

    assert caught.value.target.startswith("jobs.unit")
    assert "private-marker" not in caught.value.diagnostic()


@pytest.mark.parametrize("timeout", ["0", "3601", "true", '"300"'])
def test_job_timeout_requires_a_bounded_integer(timeout: str) -> None:
    with pytest.raises(ConfigurationError) as caught:
        parse_policy(BASE_POLICY + "[jobs.unit]\n" + UNIT_JOB + f"timeout_seconds={timeout}\n")

    assert caught.value.target == "jobs.unit.timeout_seconds"


@pytest.mark.parametrize(
    "extra",
    [
        '[jobs."private-marker"]\n' + UNIT_JOB,
        "[jobs.unit]\n" + UNIT_JOB + 'unknown="private-marker"\n',
        '[jobs.unit]\nkind="lint"\npermission="checks.lint"\nargv=["ruff"]\nlevel="unit"\n',
        '[jobs.unit]\nkind="format"\npermission="checks.format"\nargv=["ruff"]\ndefault_targets=["."]\n',
        'jobs="private-marker"\n',
        '[jobs_environment]\npublic_names=["private-marker"]\n',
        '[jobs_environment]\npublic_names=["APP_MODE", "APP_MODE"]\n',
        '[jobs_environment]\npublic_names="private-marker"\n',
        '[jobs_environment]\nunknown="private-marker"\n',
    ],
)
def test_jobs_and_environment_reject_unknown_or_incompatible_structure(extra: str) -> None:
    # A top-level scalar must precede the policy's table headers.
    text = extra + BASE_POLICY if extra.startswith("jobs=") else BASE_POLICY + extra
    with pytest.raises(ConfigurationError) as caught:
        parse_policy(text)

    assert "private-marker" not in caught.value.diagnostic()


@pytest.mark.parametrize("overflow", ["name", "argument", "argument_count", "combined_arguments"])
def test_job_configuration_size_limits_fail_before_runtime_preparation(overflow: str) -> None:
    name = "u" * 65 if overflow == "name" else "unit"
    arguments = ['"' + "x" * 4097 + '"'] if overflow == "argument" else ['"pytest"']
    if overflow == "argument_count":
        arguments = ['"x"'] * 129
    if overflow == "combined_arguments":
        arguments = ['"' + "x" * 4096 + '"'] * 9
    declaration = UNIT_JOB.replace('argv=["pytest", "-n", "0"]', "argv=[" + ",".join(arguments) + "]")

    with pytest.raises(ConfigurationError):
        parse_policy(BASE_POLICY + f"[jobs.{name}]\n" + declaration)


def test_recipe_cwd_defaults_to_root_and_accepts_relative_subproject() -> None:
    legacy = parse_policy(BASE_POLICY + "[jobs.unit]\n" + UNIT_JOB)
    nested = parse_policy(BASE_POLICY + "[jobs.unit]\n" + UNIT_JOB + 'cwd="apps/server"\n')

    assert legacy.jobs[0].cwd == "."
    assert nested.jobs[0].cwd == "apps/server"


@pytest.mark.parametrize(
    "value",
    ['""', '"../outside"', '"/absolute"', '"C:/project"', '"apps\\\\server"', '"apps/server/../../escape"', "1"],
)
def test_job_cwd_rejects_escape_and_invalid_forms(value: str) -> None:
    with pytest.raises(ConfigurationError) as caught:
        parse_policy(BASE_POLICY + "[jobs.unit]\n" + UNIT_JOB + f"cwd={value}\n")
    assert caught.value.target == "jobs.unit.cwd"
