"""Keep the task snapshot and output catalog separate from server credentials."""

import pytest

from local_dev_mcp.app import _task_environment
from local_dev_mcp.redaction import Redactor

pytestmark = pytest.mark.unit


def test_common_project_values_do_not_require_per_task_variable_lists(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_task_environment, "base_environment", lambda: {"PATH": "selected-tool-path"})
    values = {
        "PROJECT_DATABASE_URL": "synthetic-database-private",
        "NEW_TEST_TOKEN": "synthetic-new-test-token",
        "EMPTY_SETTING": "",
        "UNASSIGNED": None,
        "CONTROL_PLANE_API_KEY": "synthetic-server-credential",
        "GIT_USER_NAME": "Public Writer",
    }
    environment = _task_environment.task_environment(values)
    assert environment == {
        "PATH": "selected-tool-path",
        "PROJECT_DATABASE_URL": "synthetic-database-private",
        "NEW_TEST_TOKEN": "synthetic-new-test-token",
        "EMPTY_SETTING": "",
    }
    public = _task_environment.public_environment_names(values, ("EMPTY_SETTING", "CONTROL_PLANE_API_KEY"))
    redactor = Redactor.from_values(values, public)
    assert redactor.sanitize("synthetic-server-credential synthetic-new-test-token Public Writer") == (
        "[REDACTED] [REDACTED] Public Writer"
    )


def test_public_operational_classification_does_not_publish_server_credentials() -> None:
    values = {"DEBUG": "1", "PORT": "80", "CONTROL_PLANE_TUNNEL_ID": "synthetic-private-tunnel"}
    public = _task_environment.public_environment_names(values, ("DEBUG", "PORT", "CONTROL_PLANE_TUNNEL_ID"))
    redactor = Redactor.from_values(values, public)
    assert redactor.safe
    assert redactor.sanitize("DEBUG=1 PORT=80 synthetic-private-tunnel") == "DEBUG=1 PORT=80 [REDACTED]"


def test_invalid_server_metadata_is_not_implicitly_public() -> None:
    values = {"GIT_COMMIT_SIGN": "unexpected-private-intent", "GIT_SIGNING_KEY": "synthetic-unvalidated-key"}
    redactor = Redactor.from_values(values, _task_environment.public_environment_names(values, ()))
    assert redactor.sanitize("unexpected-private-intent synthetic-unvalidated-key") == "[REDACTED] [REDACTED]"
