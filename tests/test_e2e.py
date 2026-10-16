import pytest

from local_dev_mcp.core import server


@pytest.fixture
def args():
    return server.ServerArgs()


def test_base_success(args: server.ServerArgs):
    assert server.base(args) == 0
