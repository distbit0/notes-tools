import os
from pathlib import Path
import sys

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
NOTES_DIR = REPO_ROOT / "notes"

if str(NOTES_DIR) not in sys.path:
    sys.path.insert(0, str(NOTES_DIR))

import ethresearch_social_notifs  # noqa: E402
import lesswrong_social_notifs  # noqa: E402
import social_notif_common  # noqa: E402
import x_social_notifs  # noqa: E402


pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_LIVE_SOCIAL_NOTIFS_TESTS") != "1",
    reason="set RUN_LIVE_SOCIAL_NOTIFS_TESTS=1 to exercise live browser-auth APIs",
)


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


def test_live_x_collection_reads_current_brave_session() -> None:
    state = social_notif_common.empty_state()
    client = x_social_notifs.XClient.from_brave()

    notifications = (
        x_social_notifs.collect_x_mentions(client, state)
        + x_social_notifs.collect_x_dms(client, state)
    )

    assert_well_formed_notifications(notifications)
    assert len(state["x_reply"]) + len(state["x_dm"]) > 0


def test_live_lesswrong_collection_reads_current_brave_session() -> None:
    state = social_notif_common.empty_state()
    client = lesswrong_social_notifs.LessWrongClient.from_brave()

    notifications = (
        lesswrong_social_notifs.collect_lesswrong_notifications(client, state)
        + lesswrong_social_notifs.collect_lesswrong_dms(client, state)
    )

    assert_well_formed_notifications(notifications)
    assert len(state["lesswrong_notification"]) + len(state["lesswrong_dm"]) > 0


def test_live_ethresearch_collection_reads_current_brave_session() -> None:
    state = social_notif_common.empty_state()

    notifications = ethresearch_social_notifs.collect_ethresearch_all(state)

    assert_well_formed_notifications(notifications)
    assert len(state["ethresearch_notification"]) + len(state["ethresearch_pm"]) > 0
