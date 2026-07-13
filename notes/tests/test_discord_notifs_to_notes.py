import os
from pathlib import Path
import sys

import pytest

from private_test_data import PRIVATE_TEST_DATA

REPO_ROOT = Path(__file__).resolve().parents[2]
NOTES_DIR = REPO_ROOT / "notes"

if str(NOTES_DIR) not in sys.path:
    sys.path.insert(0, str(NOTES_DIR))

import discord_notifs_to_notes  # noqa: E402
import discord_auth  # noqa: E402
import notes_utils  # noqa: E402


SELF_NAME = PRIVATE_TEST_DATA["discord"]["selfName"]


def make_message(
    *,
    channel_id: int,
    sender_id: int,
    sender_name: str,
    message_id: int,
    content: str,
    guild_id: int | None = None,
    attachments: tuple[str, ...] = (),
) -> discord_notifs_to_notes.DiscordMessage:
    return discord_notifs_to_notes.DiscordMessage(
        chat_id=channel_id,
        sender_id=sender_id,
        sender_name=sender_name,
        message_id=message_id,
        timestamp_ms=discord_notifs_to_notes.snowflake_timestamp_ms(message_id),
        content=content,
        guild_id=guild_id,
        attachments=attachments,
    )


def test_collapse_notification_text_handles_empty_and_truncates() -> None:
    assert notes_utils.collapse_notification_text(None) == "<media>"
    assert notes_utils.collapse_notification_text("   ") == "<media>"

    long_text = "word " * 40
    collapsed = notes_utils.collapse_notification_text(long_text)

    assert collapsed.endswith("...")
    assert len(collapsed) <= 120


def test_update_state_cursor_keeps_newest_message() -> None:
    state = discord_notifs_to_notes.empty_state()

    older = discord_notifs_to_notes.MessageCursor(
        chat_id=123,
        sender_id=1,
        timestamp_ms=1000,
        message_id=10,
    )
    newer = discord_notifs_to_notes.MessageCursor(
        chat_id=123,
        sender_id=2,
        timestamp_ms=2000,
        message_id=20,
    )

    discord_notifs_to_notes.update_state_cursor(state, "dm", newer)
    discord_notifs_to_notes.update_state_cursor(state, "dm", older)

    assert state["dm"][123] == newer


def test_serialize_state_includes_required_fields() -> None:
    state = discord_notifs_to_notes.empty_state()
    cursor = discord_notifs_to_notes.MessageCursor(
        chat_id=77,
        sender_id=88,
        timestamp_ms=999,
        message_id=11,
    )
    discord_notifs_to_notes.update_state_cursor(state, "mention", cursor)

    serialized = discord_notifs_to_notes.serialize_state(state)

    assert serialized["mention"]["77"] == {
        "chat_id": 77,
        "sender_id": 88,
        "latest_message_timestamp_ms": 999,
        "latest_message_id": 11,
        "read": True,
    }


def test_collect_notifications_bootstrap_notifies_only_unread_messages() -> None:
    dm_channel = discord_notifs_to_notes.DiscordDMChannel(channel_id=10)
    dm_latest = make_message(
        channel_id=10,
        sender_id=200,
        sender_name="Alice",
        message_id=4 << 22,
        content="hello",
    )
    mention_latest = make_message(
        channel_id=20,
        sender_id=300,
        sender_name="Bob",
        message_id=5 << 22,
        content="ping",
        guild_id=999,
    )

    class FakeApi:
        def current_user_id(self) -> int:
            return 100

        def read_state_last_message_ids(self) -> dict[int, int]:
            return {
                10: 3 << 22,
                20: 4 << 22,
            }

        def dm_channels(self) -> list[discord_notifs_to_notes.DiscordDMChannel]:
            return [dm_channel]

        def recent_channel_messages(self, channel_id: int, *, limit: int) -> list[discord_notifs_to_notes.DiscordMessage]:
            raise AssertionError("not expected when unread message exists")

        def channel_messages_after(self, channel_id: int, after_message_id: int) -> list[discord_notifs_to_notes.DiscordMessage]:
            assert channel_id == 10
            assert after_message_id == (3 << 22)
            return [dm_latest]

        def recent_mentions(
            self,
            *,
            before_message_id: int | None,
            limit: int,
        ) -> list[discord_notifs_to_notes.DiscordMessage]:
            if before_message_id is None:
                return [mention_latest]
            return []

        def channel_display_name(self, channel_id: int) -> str:
            assert channel_id == 20
            return "general"

    notifications, next_state = discord_notifs_to_notes.collect_notifications(
        FakeApi(),
        discord_notifs_to_notes.empty_state(),
    )

    assert [notification.label for notification in notifications] == [
        "Alice: hello",
        "general | Bob: @ping",
    ]
    assert next_state["dm"][10].message_id == dm_latest.message_id
    assert next_state["mention"][20].message_id == mention_latest.message_id


def test_collect_notifications_emits_new_dm_and_mention() -> None:
    dm_cursor = discord_notifs_to_notes.MessageCursor(
        chat_id=10,
        sender_id=200,
        timestamp_ms=discord_notifs_to_notes.snowflake_timestamp_ms(1 << 22),
        message_id=1 << 22,
    )
    mention_cursor = discord_notifs_to_notes.MessageCursor(
        chat_id=20,
        sender_id=300,
        timestamp_ms=discord_notifs_to_notes.snowflake_timestamp_ms(2 << 22),
        message_id=2 << 22,
    )
    state = {"dm": {10: dm_cursor}, "mention": {20: mention_cursor}}

    dm_channel = discord_notifs_to_notes.DiscordDMChannel(channel_id=10)
    dm_self = make_message(
        channel_id=10,
        sender_id=100,
        sender_name="Me",
        message_id=3 << 22,
        content="self",
    )
    dm_incoming = make_message(
        channel_id=10,
        sender_id=200,
        sender_name="Alice",
        message_id=4 << 22,
        content="incoming dm",
    )
    mention_new = make_message(
        channel_id=20,
        sender_id=300,
        sender_name="Bob",
        message_id=5 << 22,
        content="incoming mention",
        guild_id=999,
    )

    class FakeApi:
        def current_user_id(self) -> int:
            return 100

        def read_state_last_message_ids(self) -> dict[int, int]:
            return {
                10: 2 << 22,
                20: 3 << 22,
            }

        def dm_channels(self) -> list[discord_notifs_to_notes.DiscordDMChannel]:
            return [dm_channel]

        def recent_channel_messages(self, channel_id: int, *, limit: int) -> list[discord_notifs_to_notes.DiscordMessage]:
            raise AssertionError("not expected when cursor exists")

        def channel_messages_after(self, channel_id: int, after_message_id: int) -> list[discord_notifs_to_notes.DiscordMessage]:
            assert channel_id == 10
            assert after_message_id == (2 << 22)
            return [dm_self, dm_incoming]

        def recent_mentions(
            self,
            *,
            before_message_id: int | None,
            limit: int,
        ) -> list[discord_notifs_to_notes.DiscordMessage]:
            if before_message_id is None:
                return [mention_new]
            return []

        def channel_display_name(self, channel_id: int) -> str:
            assert channel_id == 20
            return "general"

    notifications, next_state = discord_notifs_to_notes.collect_notifications(FakeApi(), state)

    assert [notification.label for notification in notifications] == [
        "Alice: incoming dm",
        "general | Bob: @incoming mention",
    ]
    assert [notification.message.raw_text for notification in notifications] == [
        "incoming dm",
        "incoming mention",
    ]
    assert next_state["dm"][10].message_id == dm_incoming.message_id
    assert next_state["mention"][20].message_id == mention_new.message_id


def test_parse_message_payload_captures_reply_context_and_attachments() -> None:
    raw_message = {
        "id": str(4 << 22),
        "channel_id": "10",
        "author": {"id": "200", "global_name": "Alice"},
        "content": "current reply",
        "attachments": [{"url": "https://cdn.example/current.png"}],
        "referenced_message": {
            "id": str(3 << 22),
            "channel_id": "10",
            "author": {"id": "100", "username": SELF_NAME},
            "content": "original message",
            "attachments": [{"url": "https://cdn.example/original.png"}],
        },
    }

    message = discord_notifs_to_notes.parse_message_payload(raw_message)
    notification = discord_notifs_to_notes.build_notification("dm", message)

    assert notification.label == "Alice: current reply [attachment] https://cdn.example/current.png"
    assert notification.message.raw_text == (
        "current reply\n[attachment] https://cdn.example/current.png"
    )
    assert notification.message.reply is not None
    assert notification.message.reply.sender_name == SELF_NAME
    assert notification.message.reply.raw_text == (
        "original message\n[attachment] https://cdn.example/original.png"
    )


def test_collect_notifications_allows_empty_dm_without_read_state() -> None:
    dm_channel = discord_notifs_to_notes.DiscordDMChannel(
        channel_id=10,
        last_message_id=None,
    )

    class FakeApi:
        def current_user_id(self) -> int:
            return 100

        def read_state_last_message_ids(self) -> dict[int, int]:
            return {}

        def dm_channels(self) -> list[discord_notifs_to_notes.DiscordDMChannel]:
            return [dm_channel]

        def recent_channel_messages(self, channel_id: int, *, limit: int) -> list[discord_notifs_to_notes.DiscordMessage]:
            assert channel_id == 10
            assert limit == 1
            return []

        def channel_messages_after(self, channel_id: int, after_message_id: int) -> list[discord_notifs_to_notes.DiscordMessage]:
            assert channel_id == 10
            assert after_message_id == 0
            return []

        def recent_mentions(
            self,
            *,
            before_message_id: int | None,
            limit: int,
        ) -> list[discord_notifs_to_notes.DiscordMessage]:
            return []

        def channel_display_name(self, channel_id: int) -> str:
            raise AssertionError("not expected when there are no mentions")

    notifications, next_state = discord_notifs_to_notes.collect_notifications(
        FakeApi(),
        discord_notifs_to_notes.empty_state(),
    )

    assert notifications == []
    assert next_state == discord_notifs_to_notes.empty_state()


def test_collect_notifications_raises_for_non_empty_dm_without_read_state() -> None:
    dm_channel = discord_notifs_to_notes.DiscordDMChannel(
        channel_id=10,
        last_message_id=1 << 22,
    )

    class FakeApi:
        def current_user_id(self) -> int:
            return 100

        def read_state_last_message_ids(self) -> dict[int, int]:
            return {}

        def dm_channels(self) -> list[discord_notifs_to_notes.DiscordDMChannel]:
            return [dm_channel]

        def recent_channel_messages(self, channel_id: int, *, limit: int) -> list[discord_notifs_to_notes.DiscordMessage]:
            raise AssertionError("not expected when read_state is missing for non-empty channel")

        def channel_messages_after(self, channel_id: int, after_message_id: int) -> list[discord_notifs_to_notes.DiscordMessage]:
            raise AssertionError("not expected when read_state is missing for non-empty channel")

        def recent_mentions(
            self,
            *,
            before_message_id: int | None,
            limit: int,
        ) -> list[discord_notifs_to_notes.DiscordMessage]:
            return []

        def channel_display_name(self, channel_id: int) -> str:
            raise AssertionError("not expected when read_state is missing for non-empty channel")

    with pytest.raises(RuntimeError, match="Missing Discord read state for non-empty channel 10"):
        discord_notifs_to_notes.collect_notifications(
            FakeApi(),
            discord_notifs_to_notes.empty_state(),
        )


@pytest.mark.skipif(
    os.environ.get("RUN_LIVE_DISCORD_NOTIFS_TESTS") != "1",
    reason="live Discord auth smoke test",
)
def test_brave_discord_auth_token_is_live() -> None:
    token = discord_auth.load_discord_auth_token()

    assert discord_auth.discord_token_is_valid(token)
