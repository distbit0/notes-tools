import shlex
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
NETWORK_READY_DROP_IN = (
    REPO_ROOT
    / "automation/systemd/scheduled-message-replies.service.d/10-network-ready.conf"
)


def network_readiness_commands() -> list[list[str]]:
    return [
        shlex.split(line.removeprefix("ExecStartPre="))
        for line in NETWORK_READY_DROP_IN.read_text(encoding="utf-8").splitlines()
        if line.startswith("ExecStartPre=")
    ]


def test_message_pulls_wait_for_networkmanager_startup_and_connectivity() -> None:
    assert network_readiness_commands() == [
        [
            "/usr/bin/nm-online",
            "--wait-for-startup",
            "--quiet",
            "--timeout=60",
        ],
        ["/usr/bin/nm-online", "--quiet", "--timeout=60"],
    ]
