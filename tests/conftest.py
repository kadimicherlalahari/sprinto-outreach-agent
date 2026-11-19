import socket
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Every test is offline: any outbound socket connection fails the test."""
    def guard(*a, **k):
        raise AssertionError("network access attempted during an offline test")
    monkeypatch.setattr(socket.socket, "connect", guard)
    monkeypatch.setattr(socket, "create_connection", guard)


@pytest.fixture
def icp():
    from sprinto_agent.config import load_icp
    return load_icp()


@pytest.fixture
def store(tmp_path):
    from sprinto_agent.store import open_mock_store
    return open_mock_store(tmp_path / "test.sqlite3")
