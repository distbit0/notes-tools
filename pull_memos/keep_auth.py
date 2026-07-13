import os
import signal
from collections.abc import Callable
from typing import TypeVar
from uuid import getnode as get_mac

import gkeepapi

KEEP_NETWORK_TIMEOUT_SECONDS = 120.0
ResultT = TypeVar("ResultT")


class KeepTimeoutError(TimeoutError):
    pass


def run_with_keep_timeout(
    operation_name: str, operation: Callable[[], ResultT]
) -> ResultT:
    # A hung Keep TLS read can otherwise hold pullTempNotes.py's lock forever.
    def raise_timeout(_signum, _frame):
        raise KeepTimeoutError(
            f"{operation_name} timed out after {KEEP_NETWORK_TIMEOUT_SECONDS:g}s"
        )

    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.getitimer(signal.ITIMER_REAL)
    signal.signal(signal.SIGALRM, raise_timeout)
    signal.setitimer(signal.ITIMER_REAL, KEEP_NETWORK_TIMEOUT_SECONDS)
    try:
        return operation()
    finally:
        signal.setitimer(signal.ITIMER_REAL, *previous_timer)
        signal.signal(signal.SIGALRM, previous_handler)


def resolve_device_id() -> str:
    """Return a stable 16-char hex Android device id for gpsoauth."""
    device_id = os.environ.get("KEEP_DEVICE_ID")
    if device_id:
        return device_id
    # gpsoauth examples typically use 16 hex chars; pad MAC-derived id for shape.
    return f"{get_mac():x}".zfill(16)


def authenticate_keep() -> gkeepapi.Keep:
    username = os.environ.get("username")
    if not username:
        raise RuntimeError("Missing required environment variable: username")

    master_key = os.environ.get("masterKey")
    if not master_key:
        raise RuntimeError("Missing required environment variable: masterKey")

    keep = gkeepapi.Keep()
    try:
        run_with_keep_timeout(
            "Google Keep authentication",
            lambda: keep.authenticate(
                username, master_key, device_id=resolve_device_id()
            ),
        )
    except gkeepapi.exception.LoginException as error:
        raise RuntimeError(
            "Google Keep authentication failed with current masterKey. "
            "Run './.venv/bin/python reauth_keep.py' to refresh it."
        ) from error
    return keep


def sync_keep(keep: gkeepapi.Keep) -> None:
    run_with_keep_timeout("Google Keep sync", keep.sync)
