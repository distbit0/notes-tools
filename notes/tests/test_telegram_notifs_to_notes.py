import asyncio
from pathlib import Path
from datetime import datetime, timezone
from types import SimpleNamespace
import sys

import pytest
from telethon.tl.types import MessageActionEmpty, MessageService, PeerUser

from private_test_data import PRIVATE_TEST_DATA


REPO_ROOT = Path(__file__).resolve().parents[2]
NOTES_DIR = REPO_ROOT / "notes"

if str(NOTES_DIR) not in sys.path:
    sys.path.insert(0, str(NOTES_DIR))

import telegram_notifs_to_notes  # noqa: E402
import notes_utils  # noqa: E402


PRIVATE_GROUP_DATA = PRIVATE_TEST_DATA["telegramPrivateGroup"]
PRIVATE_GROUP_ID = PRIVATE_GROUP_DATA["id"]
PRIVATE_GROUP_TITLE = PRIVATE_GROUP_DATA["title"]
PRIVATE_GROUP_INVITE_URL = PRIVATE_GROUP_DATA["inviteUrl"]


async def resolve_dialog_url_stub(_client, _entity) -> None:
    return None


def test_collapse_notification_text_handles_empty_and_truncates() -> None:
    assert notes_utils.collapse_notification_text(None) == "<media>"
    assert notes_utils.collapse_notification_text("   ") == "<media>"

    long_text = "word " * 40
    collapsed = notes_utils.collapse_notification_text(long_text)

    assert collapsed.endswith("...")
    assert len(collapsed) <= 120


def test_update_state_cursor_keeps_newest_message() -> None:
    state = telegram_notifs_to_notes.empty_state()

    older = telegram_notifs_to_notes.MessageCursor(
        chat_id=123,
        sender_id=1,
        timestamp_ms=1000,
        message_id=10,
    )
    newer = telegram_notifs_to_notes.MessageCursor(
        chat_id=123,
        sender_id=2,
        timestamp_ms=2000,
        message_id=20,
    )

    telegram_notifs_to_notes.update_state_cursor(state, "dm", newer)
    telegram_notifs_to_notes.update_state_cursor(state, "dm", older)

    assert state["dm"][123] == newer


def test_load_state_returns_empty_when_file_is_missing(tmp_path: Path) -> None:
    state = telegram_notifs_to_notes.load_state(tmp_path / "missing.json")
    assert state == {"dm": {}, "group": {}, "mention": {}}


def test_serialize_state_includes_required_fields() -> None:
    state = telegram_notifs_to_notes.empty_state()
    cursor = telegram_notifs_to_notes.MessageCursor(
        chat_id=77,
        sender_id=88,
        timestamp_ms=999,
        message_id=11,
    )
    telegram_notifs_to_notes.update_state_cursor(state, "mention", cursor)

    serialized = telegram_notifs_to_notes.serialize_state(state)

    assert serialized["mention"]["77"] == {
        "chat_id": 77,
        "sender_id": 88,
        "latest_message_timestamp_ms": 999,
        "latest_message_id": 11,
        "read": True,
    }
    assert serialized["group"] == {}


def test_collect_notifications_uses_latest_unread_window_and_advances_state() -> None:
    dm_old_cursor = telegram_notifs_to_notes.MessageCursor(
        chat_id=111,
        sender_id=111,
        timestamp_ms=1000,
        message_id=1,
    )
    mention_old_cursor = telegram_notifs_to_notes.MessageCursor(
        chat_id=111,
        sender_id=111,
        timestamp_ms=2000,
        message_id=2,
    )

    state = {
        "dm": {111: dm_old_cursor},
        "group": {},
        "mention": {111: mention_old_cursor},
    }

    entity = telegram_notifs_to_notes.User(
        id=111,
        first_name="Alice",
    )
    dialog = SimpleNamespace(
        entity=entity,
        name="Alice",
        is_user=True,
        unread_count=1,
        unread_mentions_count=1,
        unread_mark=False,
    )

    read_dm = SimpleNamespace(
        id=3,
        unread=False,
        out=False,
        mentioned=False,
        sender_id=111,
        raw_text="read dm",
        link=None,
        date=datetime.fromtimestamp(3, tz=timezone.utc),
    )
    unread_dm = SimpleNamespace(
        id=4,
        unread=True,
        out=False,
        mentioned=False,
        sender_id=111,
        raw_text="unread dm",
        link=None,
        date=datetime.fromtimestamp(4, tz=timezone.utc),
    )
    old_dm = SimpleNamespace(
        id=1,
        unread=True,
        out=False,
        mentioned=False,
        sender_id=111,
        raw_text="old dm",
        link=None,
        date=datetime.fromtimestamp(1, tz=timezone.utc),
    )

    read_mention = SimpleNamespace(
        id=5,
        unread=False,
        out=False,
        mentioned=True,
        sender_id=111,
        raw_text="read mention",
        link=None,
        date=datetime.fromtimestamp(5, tz=timezone.utc),
    )
    unread_mention = SimpleNamespace(
        id=6,
        unread=True,
        out=False,
        mentioned=True,
        sender_id=111,
        raw_text="unread mention",
        link=None,
        date=datetime.fromtimestamp(6, tz=timezone.utc),
    )
    old_mention = SimpleNamespace(
        id=2,
        unread=True,
        out=False,
        mentioned=True,
        sender_id=111,
        raw_text="old mention",
        link=None,
        date=datetime.fromtimestamp(2, tz=timezone.utc),
    )

    class FakeClient:
        async def iter_dialogs(self):
            yield dialog

        async def iter_messages(self, _entity, filter=None):
            if filter is None:
                messages = (unread_dm, read_dm, old_dm)
            else:
                messages = (unread_mention, read_mention, old_mention)
            for message in messages:
                yield message

    notifications, next_state = asyncio.run(
        telegram_notifs_to_notes.collect_notifications(
            FakeClient(),
            state,
        )
    )

    assert [notification.label for notification in notifications] == [
        "Alice: unread dm",
        "Alice: unread mention",
    ]
    assert next_state["dm"][111].message_id == 4
    assert next_state["mention"][111].message_id == 6


def test_collect_notifications_treats_chat_unread_mark_as_unread_override() -> None:
    cursor = telegram_notifs_to_notes.MessageCursor(
        chat_id=111,
        sender_id=111,
        timestamp_ms=1000,
        message_id=1,
    )
    state = {
        "dm": {111: cursor},
        "group": {},
        "mention": {111: cursor},
    }

    entity = telegram_notifs_to_notes.User(
        id=111,
        first_name="Alice",
    )
    dialog = SimpleNamespace(
        entity=entity,
        name="Alice",
        is_user=True,
        unread_count=0,
        unread_mentions_count=0,
        unread_mark=True,
    )

    read_dm = SimpleNamespace(
        id=3,
        unread=False,
        out=False,
        mentioned=False,
        sender_id=111,
        raw_text="read dm",
        link=None,
        date=datetime.fromtimestamp(3, tz=timezone.utc),
    )
    old_dm = SimpleNamespace(
        id=1,
        unread=False,
        out=False,
        mentioned=False,
        sender_id=111,
        raw_text="old dm",
        link=None,
        date=datetime.fromtimestamp(1, tz=timezone.utc),
    )
    read_mention = SimpleNamespace(
        id=4,
        unread=False,
        out=False,
        mentioned=True,
        sender_id=111,
        raw_text="read mention",
        link=None,
        date=datetime.fromtimestamp(4, tz=timezone.utc),
    )
    old_mention = SimpleNamespace(
        id=1,
        unread=False,
        out=False,
        mentioned=True,
        sender_id=111,
        raw_text="old mention",
        link=None,
        date=datetime.fromtimestamp(1, tz=timezone.utc),
    )

    class FakeClient:
        async def iter_dialogs(self):
            yield dialog

        async def iter_messages(self, _entity, filter=None):
            if filter is None:
                messages = (read_dm, old_dm)
            else:
                messages = (read_mention, old_mention)
            for message in messages:
                yield message

    notifications, next_state = asyncio.run(
        telegram_notifs_to_notes.collect_notifications(
            FakeClient(),
            state,
        )
    )

    assert [notification.label for notification in notifications] == [
        "Alice: read dm",
        "Alice: read mention",
    ]
    assert next_state["dm"][111].message_id == 3
    assert next_state["mention"][111].message_id == 4


def test_fetch_initial_incoming_messages_skips_service_messages() -> None:
    service_message = MessageService(
        id=9,
        peer_id=PeerUser(user_id=111),
        date=datetime.fromtimestamp(9, tz=timezone.utc),
        out=False,
        action=MessageActionEmpty(),
    )
    unread_message = SimpleNamespace(
        id=10,
        unread=True,
        out=False,
    )

    class FakeClient:
        async def iter_messages(self, _entity):
            yield service_message
            yield unread_message

    result = asyncio.run(
        telegram_notifs_to_notes.fetch_initial_incoming_messages(
            FakeClient(),
            object(),
            unread_count=1,
        )
    )

    assert result == [unread_message]


def test_fetch_initial_incoming_messages_handles_missing_unread_field() -> None:
    incoming_message = SimpleNamespace(
        id=10,
        out=False,
    )

    class FakeClient:
        async def iter_messages(self, _entity):
            yield incoming_message

    result = asyncio.run(
        telegram_notifs_to_notes.fetch_initial_incoming_messages(
            FakeClient(),
            object(),
            unread_count=1,
        )
    )

    assert result == [incoming_message]


def test_build_message_url_for_user_with_username_uses_public_link() -> None:
    entity = telegram_notifs_to_notes.User(id=111, username="alice")
    message = SimpleNamespace(id=12, link=None)

    url = telegram_notifs_to_notes.build_message_url(entity, message)

    assert url == "https://t.me/alice"


def test_build_message_url_for_private_user_without_username_or_phone_returns_none() -> None:
    entity = telegram_notifs_to_notes.User(id=111)
    message = SimpleNamespace(id=12, link=None)

    url = telegram_notifs_to_notes.build_message_url(entity, message)

    assert url is None


def test_build_message_url_prefers_message_link_when_available() -> None:
    entity = telegram_notifs_to_notes.User(id=111)
    message = SimpleNamespace(id=12, link="https://t.me/c/123/12")

    url = telegram_notifs_to_notes.build_message_url(entity, message)

    assert url == "https://t.me/c/123/12"


def test_build_message_url_for_private_group_uses_web_telegram_dialog_url() -> None:
    entity = telegram_notifs_to_notes.Chat(
        id=PRIVATE_GROUP_ID,
        title=PRIVATE_GROUP_TITLE,
        photo=None,
        participants_count=4,
        date=datetime.fromtimestamp(1, tz=timezone.utc),
        version=1,
    )
    message = SimpleNamespace(id=12, link=None)

    url = telegram_notifs_to_notes.build_message_url(entity, message)

    assert url == f"https://web.telegram.org/k/#-{PRIVATE_GROUP_ID}"


def test_resolve_dialog_url_for_private_group_prefers_exported_invite() -> None:
    entity = telegram_notifs_to_notes.Chat(
        id=PRIVATE_GROUP_ID,
        title=PRIVATE_GROUP_TITLE,
        photo=None,
        participants_count=4,
        date=datetime.fromtimestamp(1, tz=timezone.utc),
        version=1,
    )

    class FakeClient:
        async def __call__(self, request):
            assert request.chat_id == PRIVATE_GROUP_ID
            return SimpleNamespace(
                full_chat=SimpleNamespace(
                    exported_invite=SimpleNamespace(link=PRIVATE_GROUP_INVITE_URL)
                )
            )

    url = asyncio.run(
        telegram_notifs_to_notes.resolve_dialog_url(FakeClient(), entity)
    )

    assert url == PRIVATE_GROUP_INVITE_URL


def test_resolve_dialog_url_for_unsupported_entity_returns_none() -> None:
    class FakeClient:
        async def __call__(self, request):
            raise AssertionError("Unsupported entities should not trigger API calls")

    url = asyncio.run(
        telegram_notifs_to_notes.resolve_dialog_url(FakeClient(), SimpleNamespace())
    )

    assert url is None


def test_collect_notifications_includes_small_unmuted_group_unreads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = telegram_notifs_to_notes.empty_state()
    entity = SimpleNamespace(
        id=222,
        title="Study Group",
        participants_count=4,
    )
    dialog = SimpleNamespace(
        entity=entity,
        name="Study Group",
        is_user=False,
        is_group=True,
        unread_count=2,
        unread_mentions_count=0,
        unread_mark=False,
        dialog=SimpleNamespace(
            notify_settings=SimpleNamespace(mute_until=None),
        ),
    )
    older_message = SimpleNamespace(
        id=5,
        unread=True,
        out=False,
        mentioned=False,
        sender_id=1,
        sender=telegram_notifs_to_notes.User(id=1, first_name="Alex"),
        raw_text="older group message",
        link=None,
        date=datetime.fromtimestamp(5, tz=timezone.utc),
    )
    newer_message = SimpleNamespace(
        id=6,
        unread=True,
        out=False,
        mentioned=False,
        sender_id=1,
        sender=telegram_notifs_to_notes.User(id=1, first_name="Alex"),
        raw_text="newer group message",
        link=None,
        date=datetime.fromtimestamp(6, tz=timezone.utc),
    )

    class FakeClient:
        async def iter_dialogs(self):
            yield dialog

        async def iter_messages(self, _entity, filter=None):
            assert filter is None
            for message in (newer_message, older_message):
                yield message

    monkeypatch.setattr(
        telegram_notifs_to_notes,
        "build_message_url",
        lambda _entity, _message, _dialog_url=None: None,
    )
    monkeypatch.setattr(
        telegram_notifs_to_notes,
        "resolve_dialog_url",
        resolve_dialog_url_stub,
    )

    notifications, next_state = asyncio.run(
        telegram_notifs_to_notes.collect_notifications(
            FakeClient(),
            state,
        )
    )

    assert [notification.label for notification in notifications] == [
        "Study Group | Alex: older group message",
        "Study Group | Alex: newer group message",
    ]
    assert next_state["group"][222].message_id == 6


def test_collect_notifications_prefixes_group_mentions_with_group_name_and_at(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = telegram_notifs_to_notes.empty_state()
    entity = SimpleNamespace(
        id=222,
        title="Study Group",
        participants_count=4,
    )
    dialog = SimpleNamespace(
        entity=entity,
        name="Study Group",
        is_user=False,
        is_group=True,
        unread_count=0,
        unread_mentions_count=1,
        unread_mark=False,
        dialog=SimpleNamespace(
            notify_settings=SimpleNamespace(mute_until=None),
        ),
    )
    mentioned_message = SimpleNamespace(
        id=7,
        unread=True,
        out=False,
        mentioned=True,
        sender_id=1,
        sender=telegram_notifs_to_notes.User(id=1, first_name="Alex"),
        raw_text="check this out",
        link=None,
        date=datetime.fromtimestamp(7, tz=timezone.utc),
    )

    class FakeClient:
        async def iter_dialogs(self):
            yield dialog

        async def iter_messages(self, _entity, filter=None):
            assert filter is not None
            yield mentioned_message

    monkeypatch.setattr(
        telegram_notifs_to_notes,
        "build_message_url",
        lambda _entity, _message, _dialog_url=None: None,
    )
    monkeypatch.setattr(
        telegram_notifs_to_notes,
        "resolve_dialog_url",
        resolve_dialog_url_stub,
    )

    notifications, next_state = asyncio.run(
        telegram_notifs_to_notes.collect_notifications(
            FakeClient(),
            state,
        )
    )

    assert [notification.label for notification in notifications] == [
        "Study Group | Alex: @check this out",
    ]
    assert next_state["mention"][222].message_id == 7


def test_collect_notifications_skips_muted_small_group_unreads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = telegram_notifs_to_notes.empty_state()
    entity = SimpleNamespace(
        id=222,
        title="Study Group",
        participants_count=4,
    )
    dialog = SimpleNamespace(
        entity=entity,
        name="Study Group",
        is_user=False,
        is_group=True,
        unread_count=2,
        unread_mentions_count=0,
        unread_mark=False,
        dialog=SimpleNamespace(
            notify_settings=SimpleNamespace(
                mute_until=datetime(2999, 1, 1, tzinfo=timezone.utc)
            ),
        ),
    )

    class FakeClient:
        async def iter_dialogs(self):
            yield dialog

        async def iter_messages(self, _entity, filter=None):
            raise AssertionError("Muted group should not be scanned")

    monkeypatch.setattr(
        telegram_notifs_to_notes,
        "build_message_url",
        lambda _entity, _message, _dialog_url=None: None,
    )
    monkeypatch.setattr(
        telegram_notifs_to_notes,
        "resolve_dialog_url",
        resolve_dialog_url_stub,
    )

    notifications, next_state = asyncio.run(
        telegram_notifs_to_notes.collect_notifications(
            FakeClient(),
            state,
        )
    )

    assert notifications == []
    assert next_state["group"] == {}


def test_collect_notifications_skips_group_with_15_members(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = telegram_notifs_to_notes.empty_state()
    entity = SimpleNamespace(
        id=222,
        title="Study Group",
        participants_count=15,
    )
    dialog = SimpleNamespace(
        entity=entity,
        name="Study Group",
        is_user=False,
        is_group=True,
        unread_count=1,
        unread_mentions_count=0,
        unread_mark=False,
        dialog=SimpleNamespace(
            notify_settings=SimpleNamespace(mute_until=None),
        ),
    )

    class FakeClient:
        async def iter_dialogs(self):
            yield dialog

        async def iter_messages(self, _entity, filter=None):
            raise AssertionError("15-member group should not be scanned")

    monkeypatch.setattr(
        telegram_notifs_to_notes,
        "build_message_url",
        lambda _entity, _message, _dialog_url=None: None,
    )
    monkeypatch.setattr(
        telegram_notifs_to_notes,
        "resolve_dialog_url",
        resolve_dialog_url_stub,
    )

    notifications, next_state = asyncio.run(
        telegram_notifs_to_notes.collect_notifications(
            FakeClient(),
            state,
        )
    )

    assert notifications == []
    assert next_state["group"] == {}


def test_telegram_api_id_requires_positive_integer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_API_ID", "not-an-int")
    with pytest.raises(RuntimeError, match="must be an integer"):
        telegram_notifs_to_notes.telegram_api_id()

    monkeypatch.setenv("TELEGRAM_API_ID", "0")
    with pytest.raises(RuntimeError, match="must be positive"):
        telegram_notifs_to_notes.telegram_api_id()


def test_telegram_auth_command_uses_owning_repo() -> None:
    repo_root = Path(telegram_notifs_to_notes.__file__).resolve().parents[1]

    assert telegram_notifs_to_notes.TELEGRAM_AUTH_COMMAND == (
        f"cd {repo_root} && "
        "uv run --env-file .env python notes/auth_telegram_notifs.py"
    )


def test_telegram_auth_check_allows_authorized_session() -> None:
    class FakeClient:
        async def is_user_authorized(self) -> bool:
            return True

    asyncio.run(
        telegram_notifs_to_notes.ensure_telegram_authorized(
            FakeClient(),
            "main1",
        )
    )


def test_telegram_auth_check_notifies_and_fails_when_unauthorized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    notifications = []

    class FakeClient:
        async def is_user_authorized(self) -> bool:
            return False

    def fake_notify(session_name: str) -> None:
        notifications.append(session_name)

    monkeypatch.setattr(
        telegram_notifs_to_notes,
        "notify_telegram_reauth_needed",
        fake_notify,
    )

    with pytest.raises(RuntimeError, match="auth_telegram_notifs.py"):
        asyncio.run(
            telegram_notifs_to_notes.ensure_telegram_authorized(
                FakeClient(),
                "main1",
            )
        )

    assert notifications == ["main1"]
