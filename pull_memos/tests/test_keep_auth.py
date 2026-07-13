import time

import pytest

import keep_auth


def test_run_with_keep_timeout_raises_when_operation_hangs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(keep_auth, "KEEP_NETWORK_TIMEOUT_SECONDS", 0.05)

    with pytest.raises(
        keep_auth.KeepTimeoutError,
        match=r"slow keep op timed out after 0\.05s",
    ):
        keep_auth.run_with_keep_timeout("slow keep op", lambda: time.sleep(0.2))


def test_authenticate_keep_wraps_authentication_in_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeKeep:
        def __init__(self) -> None:
            self.authenticate_calls: list[tuple[str, str, str]] = []

        def authenticate(
            self, username: str, master_key: str, *, device_id: str
        ) -> None:
            self.authenticate_calls.append((username, master_key, device_id))

    fake_keep = FakeKeep()
    timeout_operations: list[str] = []

    monkeypatch.setenv("username", "user@example.com")
    monkeypatch.setenv("masterKey", "master-token")
    monkeypatch.setattr(keep_auth.gkeepapi, "Keep", lambda: fake_keep)
    monkeypatch.setattr(keep_auth, "resolve_device_id", lambda: "device-1234")
    monkeypatch.setattr(
        keep_auth,
        "run_with_keep_timeout",
        lambda operation_name, operation: timeout_operations.append(operation_name)
        or operation(),
    )

    keep = keep_auth.authenticate_keep()

    assert keep is fake_keep
    assert timeout_operations == ["Google Keep authentication"]
    assert fake_keep.authenticate_calls == [
        ("user@example.com", "master-token", "device-1234")
    ]


def test_sync_keep_wraps_sync_in_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeKeep:
        def __init__(self) -> None:
            self.sync_calls = 0

        def sync(self) -> None:
            self.sync_calls += 1

    fake_keep = FakeKeep()
    timeout_operations: list[str] = []
    monkeypatch.setattr(
        keep_auth,
        "run_with_keep_timeout",
        lambda operation_name, operation: timeout_operations.append(operation_name)
        or operation(),
    )

    keep_auth.sync_keep(fake_keep)

    assert fake_keep.sync_calls == 1
    assert timeout_operations == ["Google Keep sync"]
