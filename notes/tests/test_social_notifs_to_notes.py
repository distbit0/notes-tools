import os
from pathlib import Path
import sys
from unittest.mock import Mock

import pytest
import requests

REPO_ROOT = Path(__file__).resolve().parents[2]
NOTES_DIR = REPO_ROOT / "notes"

if str(NOTES_DIR) not in sys.path:
    sys.path.insert(0, str(NOTES_DIR))

import ethresearch_social_notifs  # noqa: E402
import lesswrong_social_notifs  # noqa: E402
import notes_utils  # noqa: E402
import social_notif_common  # noqa: E402
import x_social_notifs  # noqa: E402


live_social_notifications = pytest.mark.skipif(
    os.environ.get("RUN_LIVE_SOCIAL_NOTIFS_TESTS") != "1",
    reason="set RUN_LIVE_SOCIAL_NOTIFS_TESTS=1 to exercise live browser-auth APIs",
)
CAPTURED_X_INTERNAL_ERROR = (
    '{"errors":[{"message":"Internal error","code":131}]}'
)
CAPTURED_X_DM_URL = "https://x.com/i/api/1.1/dm/inbox_initial_state.json"


def assert_well_formed_notifications(
    notifications: list[social_notif_common.SocialNotification],
) -> None:
    for notification in notifications:
        assert notification.source
        assert notification.kind
        assert notification.label
        assert notification.url.startswith("https://")
        assert notification.cursor.record_key
        assert notification.cursor.timestamp_ms >= 0
        assert notification.cursor.item_id


@live_social_notifications
def test_live_x_collection_reads_current_brave_session() -> None:
    state = social_notif_common.empty_state()
    client = x_social_notifs.XClient.from_brave()

    notifications = (
        x_social_notifs.collect_x_mentions(client, state)
        + x_social_notifs.collect_x_dms(client, state)
    )

    assert_well_formed_notifications(notifications)
    assert len(state["x_reply"]) + len(state["x_dm"]) > 0


@live_social_notifications
def test_live_lesswrong_collection_reads_current_brave_session() -> None:
    state = social_notif_common.empty_state()
    client = lesswrong_social_notifs.LessWrongClient.from_brave()

    notifications = (
        lesswrong_social_notifs.collect_lesswrong_notifications(client, state)
        + lesswrong_social_notifs.collect_lesswrong_dms(client, state)
    )

    assert_well_formed_notifications(notifications)
    assert len(state["lesswrong_notification"]) + len(state["lesswrong_dm"]) > 0


@live_social_notifications
def test_live_ethresearch_collection_reads_current_brave_session() -> None:
    state = social_notif_common.empty_state()

    notifications = ethresearch_social_notifs.collect_ethresearch_all(state)

    assert_well_formed_notifications(notifications)
    assert len(state["ethresearch_notification"]) + len(state["ethresearch_pm"]) > 0


def captured_x_internal_error_response() -> requests.Response:
    response = requests.Response()
    response.status_code = 500
    response._content = CAPTURED_X_INTERNAL_ERROR.encode("utf-8")
    response.headers["Content-Type"] = "application/json"
    return response


def test_request_json_retries_captured_transient_get(monkeypatch) -> None:
    session = Mock(spec=requests.Session)
    session.request.return_value = captured_x_internal_error_response()
    monkeypatch.setattr(social_notif_common.time, "sleep", lambda _seconds: None)

    with pytest.raises(RuntimeError, match="returned HTTP 500"):
        social_notif_common.request_json(
            session,
            "GET",
            CAPTURED_X_DM_URL,
            headers={},
        )

    assert session.request.call_count == 3


def test_request_json_does_not_retry_post(monkeypatch) -> None:
    session = Mock(spec=requests.Session)
    session.request.return_value = captured_x_internal_error_response()
    monkeypatch.setattr(social_notif_common.time, "sleep", lambda _seconds: None)

    with pytest.raises(RuntimeError, match="returned HTTP 500"):
        social_notif_common.request_json(
            session,
            "POST",
            CAPTURED_X_DM_URL,
            headers={},
        )

    assert session.request.call_count == 1


def test_configure_logger_omits_captured_local_values(tmp_path, capsys) -> None:
    notes_utils.configure_logger(tmp_path / "social-notifs.log")

    try:
        captured_response_body = CAPTURED_X_INTERNAL_ERROR
        raise RuntimeError("captured X collection failure")
    except RuntimeError:
        notes_utils.logger.exception("X collection failed")

    output = capsys.readouterr().out
    assert "captured X collection failure" in output
    assert captured_response_body not in output
