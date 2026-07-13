import argparse
import fcntl
import hashlib
import json
import os
import pathlib
import random
import re
import subprocess
import sys
import time as time_module
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, time, timedelta, timezone

from loguru import logger

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
CONFIG_FILE = PROJECT_ROOT / "config.json"
LAST_RUN_FILE = PROJECT_ROOT / ".last_run"
HABIT_TRIGGER_SCHEDULE_FILE = PROJECT_ROOT / ".habit_trigger_schedule"
RUN_LOCK_FILE = PROJECT_ROOT / ".habit_run.lock"
NOTES_FILE = pathlib.Path.home() / "notes/inbox-index.md"
TRIGGER_START = time(6, 0)
TRIGGER_END = time(12, 0)
DUE_OUTPUT_WRITE_TO_MD = "writeToMd"
DUE_OUTPUT_DESKTOP_NOTIFICATION = "desktopNotification"
DUE_OUTPUT_TEXT_TO_SPEECH = "textToSpeech"
HABIT_AUDIO_FILE_FIELD = "audioFile"
ELEVENLABS_API_KEY_ENV = "ELEVENLABS_API_KEY"
PHONE_AUDIO_CONTROL_TRIGGER_URL_ENV = "PHONE_AUDIO_CONTROL_TRIGGER_URL"
DEFAULT_DUE_OUTPUTS = {
    DUE_OUTPUT_WRITE_TO_MD: True,
    DUE_OUTPUT_DESKTOP_NOTIFICATION: False,
    DUE_OUTPUT_TEXT_TO_SPEECH: True,
}
DEFAULT_TEXT_TO_SPEECH_CONFIG = {
    "provider": "elevenlabs",
    "voiceId": "JBFqnCBsd6RMkjVDRZzb",
    "modelId": "eleven_multilingual_v2",
    "outputFormat": "mp3_44100_128",
    "cacheDir": "./.tts_cache",
}
PHONE_AUDIO_CONTROL_STATES = {"pause", "play"}
PHONE_AUDIO_CONTROL_DELAY_SECONDS = 10
AUDIO_PLAYBACK_LEAD_IN_MILLISECONDS = 750
BLUETOOTH_AUDIO_TRANSPORT_BUSY_STATES = {"active", "broadcasting", "pending"}
ACTIVE_HABIT_FIELD_ORDER = (
    "id",
    "name",
    "repeatRule",
    "reminders",
    "targetStartDate",
    "goal",
    "step",
    "unit",
    "dailyTriggerCount",
    "dueOutputs",
    "audioFile",
    "sortOrder",
    "status",
    "archivedTime",
    "iconRes",
    "color",
    "encouragement",
    "totalCheckIns",
    "createdTime",
    "modifiedTime",
    "type",
    "etag",
    "recordEnable",
    "sectionId",
    "targetDays",
    "completedCycles",
    "exDates",
    "style",
)


def ensure_terminal_safe_markdown_path(path):
    path = pathlib.Path(path)
    has_whitespace = any(character.isspace() for character in path.name)
    if path.suffix.lower() == ".md" and has_whitespace:
        collapsed_stem = "-".join(path.stem.split())
        safe_path = path.with_name(f"{collapsed_stem}{path.suffix.lower()}")
        raise ValueError(
            f"Markdown note path contains whitespace: {path}. Use {safe_path} instead."
        )


# Log to stdout + file with rotation
logger.remove()
logger.add(sys.stdout, level="INFO")
logger.add("app.log", rotation="30 KB", retention=5, level="INFO")


def parse_arguments():
    parser = argparse.ArgumentParser(description="Prioritize habits from local JSON")
    parser.add_argument(
        "--test",
        action="store_true",
        help="Run in test mode",
    )
    return parser.parse_args()


def load_config():
    with open(CONFIG_FILE, "r") as config_file:
        config = json.load(config_file)

    if "lookBackDays" not in config:
        raise KeyError("Missing required config key: lookBackDays")
    if "habitsStoreFile" not in config:
        raise KeyError("Missing required config key: habitsStoreFile")
    if "activeHabitsFile" not in config:
        raise KeyError("Missing required config key: activeHabitsFile")
    return config


def acquire_run_lock(lock_path):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = open(lock_path, "w")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_file.close()
        return None
    return lock_file


def get_config_path(config, config_key):
    config_path = pathlib.Path(config[config_key]).expanduser()
    if not config_path.is_absolute():
        config_path = (PROJECT_ROOT / config_path).resolve()
    return config_path


def get_text_to_speech_config(config):
    text_to_speech_config = DEFAULT_TEXT_TO_SPEECH_CONFIG.copy()
    configured_values = config.get("textToSpeech", {})
    if not isinstance(configured_values, dict):
        raise ValueError("Config field 'textToSpeech' must be an object")
    text_to_speech_config.update(configured_values)

    if text_to_speech_config["provider"] != "elevenlabs":
        raise ValueError("Only ElevenLabs text-to-speech is supported")

    for field_name in ("voiceId", "modelId", "outputFormat", "cacheDir"):
        if not isinstance(text_to_speech_config.get(field_name), str):
            raise ValueError(f"textToSpeech.{field_name} must be a string")
        if not text_to_speech_config[field_name].strip():
            raise ValueError(f"textToSpeech.{field_name} must not be empty")

    cache_dir = pathlib.Path(text_to_speech_config["cacheDir"]).expanduser()
    if not cache_dir.is_absolute():
        cache_dir = (PROJECT_ROOT / cache_dir).resolve()
    text_to_speech_config["cacheDir"] = str(cache_dir)
    return text_to_speech_config


def get_phone_audio_control_trigger_url():
    trigger_url = os.environ.get(PHONE_AUDIO_CONTROL_TRIGGER_URL_ENV)
    if trigger_url is None:
        return None
    if not isinstance(trigger_url, str) or not trigger_url.strip():
        raise ValueError(
            f"{PHONE_AUDIO_CONTROL_TRIGGER_URL_ENV} must be a non-empty string"
        )

    parsed_url = urllib.parse.urlsplit(trigger_url)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        raise ValueError(
            f"{PHONE_AUDIO_CONTROL_TRIGGER_URL_ENV} must be an HTTP(S) URL"
        )
    return trigger_url


def load_json_list(json_path, description):
    with open(json_path, "r") as json_file:
        payload = json.load(json_file)

    if not isinstance(payload, list):
        raise ValueError(f"{description} must be a JSON list")
    return payload


def is_archived_habit(habit):
    return bool(habit.get("archivedTime"))


def validate_habits(habits, description):
    for habit in habits:
        if not isinstance(habit, dict):
            raise ValueError(f"Each {description} habit must be a JSON object")
        if "id" not in habit:
            raise ValueError(f"Each {description} habit must include an 'id'")
        if "name" not in habit:
            raise ValueError(f"Each {description} habit must include a 'name'")
        get_habit_due_outputs(habit)
        get_habit_audio_file_path(habit)


def validate_unique_habit_ids(habits):
    seen_habit_ids = set()
    duplicate_habit_ids = set()
    for habit in habits:
        habit_id = str(habit["id"])
        if habit_id in seen_habit_ids:
            duplicate_habit_ids.add(habit_id)
        seen_habit_ids.add(habit_id)

    if duplicate_habit_ids:
        raise ValueError(f"Duplicate habit ids found: {sorted(duplicate_habit_ids)}")


def order_habit_fields(habit):
    ordered_habit = {
        field_name: habit[field_name]
        for field_name in ACTIVE_HABIT_FIELD_ORDER
        if field_name in habit
    }
    ordered_habit.update(
        {
            field_name: field_value
            for field_name, field_value in habit.items()
            if field_name not in ordered_habit
        }
    )
    return ordered_habit


def load_habit_store(store_path, active_habits_path):
    with open(store_path, "r") as store_file:
        store = json.load(store_file)

    if not isinstance(store, dict):
        raise ValueError("Habit store must be a JSON object with habits and checkins")

    habits = store.get("habits")
    checkins = store.get("checkins")
    if not isinstance(habits, list):
        raise ValueError("Habit store field 'habits' must be a list")
    if not isinstance(checkins, dict):
        raise ValueError("Habit store field 'checkins' must be an object")

    active_habits = load_json_list(active_habits_path, "Active habits file")
    validate_habits(active_habits, "active")
    validate_habits(habits, "archived")

    archived_habits_in_active_file = [
        habit["id"] for habit in active_habits if is_archived_habit(habit)
    ]
    active_habits_in_store = [
        habit["id"] for habit in habits if not is_archived_habit(habit)
    ]
    if archived_habits_in_active_file:
        raise ValueError(
            "Active habits file contains archived habits: "
            f"{sorted(archived_habits_in_active_file)}"
        )
    if active_habits_in_store:
        raise ValueError(
            "Habit store contains non-archived habits; move them to active habits file: "
            f"{sorted(active_habits_in_store)}"
        )

    all_habits = active_habits + habits
    validate_unique_habit_ids(all_habits)

    for habit_id, habit_checkins in checkins.items():
        if not isinstance(habit_checkins, list):
            raise ValueError(f"Checkins for habit '{habit_id}' must be a list")

    return {"habits": all_habits, "checkins": checkins}


def save_habit_store(store_path, active_habits_path, habits, checkins):
    active_habits = [
        order_habit_fields(habit) for habit in habits if not is_archived_habit(habit)
    ]
    archived_habits = [
        order_habit_fields(habit) for habit in habits if is_archived_habit(habit)
    ]

    store_path.parent.mkdir(parents=True, exist_ok=True)
    active_habits_path.parent.mkdir(parents=True, exist_ok=True)
    with open(active_habits_path, "w") as active_habits_file:
        json.dump(active_habits, active_habits_file, indent=2)
        active_habits_file.write("\n")
    with open(store_path, "w") as store_file:
        json.dump({"habits": archived_habits, "checkins": checkins}, store_file, indent=2)
        store_file.write("\n")
    logger.info(f"Active habits saved to {active_habits_path}")
    logger.info(f"Archived habit store saved to {store_path}")


def parse_repeat_rule(rrule_str):
    """
    Parses an RRULE string into a dictionary.

    :param rrule_str: RRULE string (e.g., "RRULE:FREQ=DAILY;INTERVAL=20")
    :return: Dictionary of RRULE components.
    """
    rrule = {}
    if rrule_str.startswith("RRULE:"):
        rrule_str = rrule_str[len("RRULE:") :]
    parts = rrule_str.split(";")
    for part in parts:
        if "=" in part:
            key, value = part.split("=", 1)
            rrule[key] = value
    return rrule


def byday_to_weekdays(byday_str):
    """
    Converts BYDAY values to Python weekday numbers.

    :param byday_str: String of BYDAY values (e.g., "SU,MO,TU,WE,TH,FR,SA")
    :return: List of integers representing weekdays (0=Monday, 6=Sunday)
    """
    day_mapping = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}
    days = byday_str.split(",")
    weekdays = [day_mapping[day] for day in days if day in day_mapping]
    return weekdays


def parse_date(date_input):
    """
    Parses a date from various formats to a datetime.date object.

    :param date_input: Integer in YYYYMMDD or string in ISO format.
    :return: datetime.date object.
    """
    if isinstance(date_input, int):
        return datetime.strptime(str(date_input), "%Y%m%d").date()
    if isinstance(date_input, str):
        try:
            return datetime.strptime(date_input, "%Y-%m-%dT%H:%M:%S.%f%z").date()
        except ValueError:
            try:
                return datetime.strptime(date_input, "%Y-%m-%d").date()
            except ValueError as error:
                raise ValueError(f"Unrecognized date format: {date_input}") from error
    raise TypeError(f"Unsupported date input type: {type(date_input)}")


def get_habit_checkins(checkins, habit_id):
    return checkins.get(str(habit_id), [])


def is_habit_due_on_date(habit, date, checkins, strict=False):
    """
    Determines if a habit is due on a specific date.

    :param habit: Habit dictionary.
    :param date: datetime.date object to check.
    :param checkins: Dictionary mapping habit IDs to lists of checkin dictionaries.
    :param strict: Boolean, if True, only return True for DAILY habits on the exact due date.
    :return: Boolean indicating if the habit is due on the given date.
    """
    if habit.get("archivedTime"):
        return False

    repeat_rule = habit.get("repeatRule", "")
    if not repeat_rule:
        return False

    rrule = parse_repeat_rule(repeat_rule)
    freq = rrule.get("FREQ", "").upper()

    if freq == "WEEKLY":
        byday = rrule.get("BYDAY", "")
        if not byday:
            return False
        weekdays = byday_to_weekdays(byday)
        return date.weekday() in weekdays

    if freq == "DAILY":
        interval = int(rrule.get("INTERVAL", "1"))
        habit_checkins = get_habit_checkins(checkins, habit.get("id"))
        latest_checkin_date = None
        for checkin in habit_checkins:
            checkin_stamp = checkin.get("checkinStamp")
            if checkin_stamp is None:
                continue
            try:
                checkin_date = parse_date(checkin_stamp)
            except (ValueError, TypeError):
                continue

            if latest_checkin_date is None or checkin_date > latest_checkin_date:
                latest_checkin_date = checkin_date

        if latest_checkin_date:
            days_since_last_checkin = (date - latest_checkin_date).days
            if strict:
                return (
                    days_since_last_checkin % interval == 0
                    and days_since_last_checkin >= interval
                )
            return days_since_last_checkin >= interval

        target_start = habit.get("targetStartDate")
        if target_start:
            try:
                target_start_date = parse_date(target_start)
            except (ValueError, TypeError):
                return False

            if strict:
                return (
                    target_start_date - date
                ).days % interval == 0 and target_start_date <= date
            return target_start_date <= date
        return True

    return False


def get_habits_due_today(list_of_habits, checkins):
    """
    Returns a list of habits due today based on the provided habits and checkins.

    :param list_of_habits: List of habit dictionaries.
    :param checkins: Dictionary mapping habit IDs to lists of checkin dictionaries.
    :return: List of habit dictionaries that are due today.
    """
    today = datetime.now().astimezone().date()
    return [
        habit
        for habit in list_of_habits
        if is_habit_due_on_date(habit, today, checkins)
    ]


def calculate_completion_rate(habit, checkins, look_back_days):
    """
    Calculates the completion rate for a habit.

    :param habit: Habit dictionary.
    :param checkins: Dictionary mapping habit IDs to lists of checkin dictionaries.
    :param look_back_days: Number of days to look back for completion history.
    :return: Float representing the completion rate (0 to 1).
    """
    habit_checkins = get_habit_checkins(checkins, habit.get("id"))
    if not habit_checkins:
        return 0

    today = datetime.now().astimezone().date()
    start_date = today - timedelta(days=look_back_days)
    recent_checkins = [
        checkin
        for checkin in habit_checkins
        if parse_date(checkin["checkinStamp"]) >= start_date
    ]

    total_days = (today - start_date).days + 1
    scheduled_count = sum(
        1
        for day in range(total_days)
        if is_habit_due_on_date(
            habit, start_date + timedelta(days=day), checkins, strict=False
        )
    )
    completed_count = len(recent_checkins) + 0.1

    completion_rate = completed_count / scheduled_count if scheduled_count > 0 else 0
    logger.info(
        f"Habit: {habit['name'][:10]}, Rate: {completion_rate}, "
        f"Scheduled: {scheduled_count}, Completed: {completed_count}"
    )
    return completion_rate


def sort_habits_by_completion_rate(habits, checkins, look_back_days):
    """
    Sorts habits based on their completion rate.

    :param habits: List of habit dictionaries.
    :param checkins: Dictionary mapping habit IDs to lists of checkin dictionaries.
    :param look_back_days: Number of days to look back for completion history.
    :return: List of habits sorted by completion rate (ascending).
    """

    def get_sort_key(habit):
        name = habit["name"]
        if "@" in name:
            return (-1, 0)
        match = re.search(r"\^(\d*)", name)
        if match:
            number = match.group(1)
            if number == "":
                return (0, 1)
            return (0, int(number))
        return (1, 0)

    sorted_by_completion = sorted(
        habits,
        key=lambda habit: calculate_completion_rate(habit, checkins, look_back_days),
    )
    return sorted(sorted_by_completion, key=get_sort_key)


def update_habit_text(habits):
    always_top_habits = [habit.copy() for habit in habits if "@" in habit["name"]]
    habits_to_number = [habit for habit in habits if "@" not in habit["name"]]

    numbered_habits = []
    for priority, habit in enumerate(habits_to_number, start=1):
        old_name = habit["name"]
        new_name = f"{priority}. {remove_existing_prefix(old_name)}"
        logger.info(f"Updated: {old_name} -> {new_name}")
        habit_update = habit.copy()
        habit_update["name"] = new_name
        numbered_habits.append(habit_update)

    return always_top_habits + numbered_habits


def update_habit_sort_order(habits):
    updated_habits = []
    for habit in habits:
        habit_update = habit.copy()
        priority_match = re.match(r"^(\d+)\.\s", habit["name"])
        if priority_match:
            priority = int(priority_match.group(1))
        else:
            priority = (len(updated_habits) + 1) * 1000000000
        habit_update["sortOrder"] = priority
        updated_habits.append(habit_update)
    return updated_habits


def merge_habit_updates(all_habits, updated_habits):
    updates_by_id = {str(habit["id"]): habit for habit in updated_habits}
    existing_ids = {str(habit["id"]) for habit in all_habits}
    unknown_ids = set(updates_by_id).difference(existing_ids)
    if unknown_ids:
        raise ValueError(f"Cannot update missing habits: {sorted(unknown_ids)}")

    merged_habits = []
    for habit in all_habits:
        habit_id = str(habit["id"])
        if habit_id not in updates_by_id:
            merged_habits.append(habit)
            continue
        merged_habit = habit.copy()
        merged_habit.update(updates_by_id[habit_id])
        merged_habits.append(merged_habit)
    return merged_habits


def remove_existing_prefix(name):
    return re.sub(r"^\d+\.\s*", "", name)


def update_last_run():
    LAST_RUN_FILE.touch()


def format_ticktick_time(timestamp):
    return (
        timestamp.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") + ".000+0000"
    )


def get_habit_daily_trigger_count(habit):
    trigger_count = habit.get("dailyTriggerCount", 1)
    if not isinstance(trigger_count, int) or trigger_count < 1:
        raise ValueError(
            f"Habit '{habit.get('name')}' dailyTriggerCount must be a positive integer"
        )
    return trigger_count


def get_habit_due_outputs(habit):
    habit_due_outputs = habit.get("dueOutputs", DEFAULT_DUE_OUTPUTS)
    if not isinstance(habit_due_outputs, dict):
        raise ValueError(f"Habit '{habit.get('name')}' dueOutputs must be an object")

    unknown_outputs = set(habit_due_outputs).difference(DEFAULT_DUE_OUTPUTS)
    if unknown_outputs:
        raise ValueError(
            f"Habit '{habit.get('name')}' has unknown dueOutputs: "
            f"{sorted(unknown_outputs)}"
        )

    outputs = {}
    for output_name, default_enabled in DEFAULT_DUE_OUTPUTS.items():
        enabled = habit_due_outputs.get(output_name, default_enabled)
        if not isinstance(enabled, bool):
            raise ValueError(
                f"Habit '{habit.get('name')}' dueOutputs.{output_name} must be a boolean"
            )
        outputs[output_name] = enabled
    return outputs


def get_habit_audio_file_path(habit):
    audio_file = habit.get(HABIT_AUDIO_FILE_FIELD)
    if audio_file is None:
        return None

    if not isinstance(audio_file, str) or not audio_file.strip():
        raise ValueError(
            f"Habit '{habit.get('name')}' {HABIT_AUDIO_FILE_FIELD} "
            "must be a non-empty string"
        )

    audio_path = pathlib.Path(audio_file).expanduser()
    if not audio_path.is_absolute():
        audio_path = (PROJECT_ROOT / audio_path).resolve()
    if audio_path.suffix.lower() != ".mp3":
        raise ValueError(
            f"Habit '{habit.get('name')}' {HABIT_AUDIO_FILE_FIELD} "
            "must point to an .mp3 file"
        )
    return audio_path


def is_trigger_delivered(habit, trigger):
    delivered_outputs = trigger.get("deliveredOutputs", {})
    return all(
        delivered_outputs.get(output_name)
        for output_name, enabled in get_habit_due_outputs(habit).items()
        if enabled
    )


def update_trigger_completion(habit, trigger):
    trigger["triggered"] = is_trigger_delivered(habit, trigger)


def normalize_trigger_delivery_state(habit, trigger, now):
    trigger_time = datetime.fromisoformat(trigger["time"])
    due_outputs = get_habit_due_outputs(habit)
    delivered_outputs = trigger.get("deliveredOutputs")

    if trigger.get("triggered"):
        normalized_outputs = {
            output_name: enabled for output_name, enabled in due_outputs.items()
        }
    elif isinstance(delivered_outputs, dict):
        normalized_outputs = {
            output_name: bool(enabled and delivered_outputs.get(output_name))
            for output_name, enabled in due_outputs.items()
        }
    else:
        legacy_ready_trigger = trigger_time <= now
        normalized_outputs = {
            output_name: bool(
                enabled
                and legacy_ready_trigger
                and output_name != DUE_OUTPUT_TEXT_TO_SPEECH
            )
            for output_name, enabled in due_outputs.items()
        }

    trigger["deliveredOutputs"] = normalized_outputs
    update_trigger_completion(habit, trigger)
    return trigger_time


def get_ready_triggers_for_due_output(ready_triggers, output_name):
    return [
        item
        for item in ready_triggers
        if get_habit_due_outputs(item["habit"])[output_name]
        and not item["trigger"].get("deliveredOutputs", {}).get(output_name)
    ]


def mark_triggers_output_delivered(ready_triggers, output_name):
    for item in ready_triggers:
        item["trigger"].setdefault("deliveredOutputs", {})[output_name] = True
        update_trigger_completion(item["habit"], item["trigger"])


def sample_habit_trigger_time(trigger_date, local_timezone):
    trigger_start = datetime.combine(trigger_date, TRIGGER_START, local_timezone)
    trigger_end = datetime.combine(trigger_date, TRIGGER_END, local_timezone)
    trigger_seconds = int((trigger_end - trigger_start).total_seconds())
    return trigger_start + timedelta(seconds=random.randint(0, trigger_seconds))


def load_habit_trigger_schedule(schedule_path, schedule_date):
    if not schedule_path.exists():
        return {"date": schedule_date, "triggers": {}}

    with open(schedule_path, "r") as schedule_file:
        schedule = json.load(schedule_file)

    if schedule.get("date") != schedule_date:
        return {"date": schedule_date, "triggers": {}}
    if not isinstance(schedule.get("triggers"), dict):
        raise ValueError("Habit trigger schedule field 'triggers' must be an object")
    return schedule


def save_habit_trigger_schedule(schedule_path, schedule):
    with open(schedule_path, "w") as schedule_file:
        json.dump(schedule, schedule_file, indent=2)


def get_ready_habit_triggers(due_habits, schedule_path, now):
    schedule_date = now.strftime("%Y%m%d")
    schedule = load_habit_trigger_schedule(schedule_path, schedule_date)
    scheduled_triggers = schedule["triggers"]
    due_habits_by_id = {str(habit["id"]): habit for habit in due_habits}

    for habit_id, habit in due_habits_by_id.items():
        habit_triggers = scheduled_triggers.setdefault(habit_id, [])
        while len(habit_triggers) < get_habit_daily_trigger_count(habit):
            habit_triggers.append(
                {
                    "time": sample_habit_trigger_time(
                        now.date(), now.tzinfo
                    ).isoformat(),
                    "triggered": False,
                    "deliveredOutputs": {
                        output_name: False for output_name in DEFAULT_DUE_OUTPUTS
                    },
                }
            )
        habit_triggers.sort(key=lambda trigger: trigger["time"])

    ready_triggers = []
    for habit_id, habit_triggers in scheduled_triggers.items():
        habit = due_habits_by_id.get(habit_id)
        if habit is None:
            continue
        for trigger in habit_triggers:
            trigger_time = normalize_trigger_delivery_state(habit, trigger, now)
            if trigger.get("triggered"):
                continue
            if trigger_time <= now:
                ready_triggers.append({"habit": habit, "trigger": trigger})

    ready_triggers.sort(key=lambda item: item["trigger"]["time"])
    return ready_triggers, schedule


def get_checkin_entry_for_date(habit_id, checkins, checkin_stamp):
    habit_checkins = get_habit_checkins(checkins, habit_id)
    for checkin in habit_checkins:
        if checkin.get("checkinStamp") == checkin_stamp:
            return checkin
    return None


def build_checkin_payload(
    due_habits, checkins, checkin_stamp, checkin_times_by_habit_id
):
    payload = {"add": [], "update": [], "delete": []}
    for habit in due_habits:
        habit_id = habit.get("id")
        checkin_time = checkin_times_by_habit_id[str(habit_id)]
        habit_goal = habit.get("goal")
        if habit_goal is None:
            logger.error(f"Missing goal for habit: {habit.get('name')} ({habit_id})")
            continue

        try:
            habit_goal_value = float(habit_goal)
        except (TypeError, ValueError):
            logger.error(
                f"Invalid goal for habit: {habit.get('name')} ({habit_id}) -> {habit_goal}"
            )
            continue

        existing_checkin = get_checkin_entry_for_date(habit_id, checkins, checkin_stamp)
        if existing_checkin and existing_checkin.get("status") == 2:
            continue

        entry = {
            "habitId": habit_id,
            "checkinStamp": checkin_stamp,
            "goal": habit_goal_value,
            "value": habit_goal_value,
            "status": 2,
            "checkinTime": checkin_time,
            "opTime": checkin_time,
        }

        if existing_checkin and existing_checkin.get("id"):
            entry["id"] = existing_checkin["id"]
            payload["update"].append(entry)
        else:
            payload["add"].append(entry)

    return payload


def upsert_checkin_entry(habit_checkins, checkin_entry):
    entry_id = checkin_entry.get("id")
    if entry_id:
        for index, existing in enumerate(habit_checkins):
            if existing.get("id") == entry_id:
                merged_entry = existing.copy()
                merged_entry.update(checkin_entry)
                habit_checkins[index] = merged_entry
                return

    entry_stamp = checkin_entry.get("checkinStamp")
    for index, existing in enumerate(habit_checkins):
        if existing.get("checkinStamp") == entry_stamp:
            merged_entry = existing.copy()
            merged_entry.update(checkin_entry)
            habit_checkins[index] = merged_entry
            return

    new_entry = checkin_entry.copy()
    if "id" not in new_entry:
        new_entry["id"] = f"{new_entry['habitId']}-{new_entry['checkinStamp']}"
    habit_checkins.append(new_entry)


def apply_checkin_payload(checkins, payload):
    if not payload["add"] and not payload["update"]:
        return 0

    applied_count = 0
    for checkin_entry in payload["add"] + payload["update"]:
        habit_id = checkin_entry.get("habitId")
        if habit_id is None:
            logger.error(f"Skipping checkin entry without habitId: {checkin_entry}")
            continue

        habit_key = str(habit_id)
        habit_checkins = checkins.setdefault(habit_key, [])
        if not isinstance(habit_checkins, list):
            raise ValueError(f"Checkins bucket for habit '{habit_key}' is not a list")

        upsert_checkin_entry(habit_checkins, checkin_entry)
        applied_count += 1
    return applied_count


def append_ready_habit_triggers(notes_path, ready_triggers):
    if not ready_triggers:
        return 0

    ensure_terminal_safe_markdown_path(notes_path)
    notes_path.parent.mkdir(parents=True, exist_ok=True)

    existing_lines = []
    if notes_path.exists():
        with open(notes_path, "r") as notes_file:
            existing_lines = notes_file.readlines()

    existing_line_set = {line.strip() for line in existing_lines if line.strip()}
    habit_lines = [
        remove_existing_prefix(item["habit"].get("name", "")).strip().lower()
        for item in ready_triggers
    ]
    new_habit_lines = [
        line for line in habit_lines if line and line not in existing_line_set
    ]
    if not new_habit_lines:
        logger.info(f"No new habit lines to append to {notes_path}")
        return 0

    with open(notes_path, "a") as notes_file:
        for habit_line in new_habit_lines:
            notes_file.write(f"\n\n{habit_line}")
        notes_file.write("\n")
    logger.info(f"Appended {len(new_habit_lines)} habit triggers to {notes_path}")
    return len(new_habit_lines)


def create_persistent_desktop_notifications(ready_triggers):
    notification_count = 0
    for item in ready_triggers:
        habit_name = remove_existing_prefix(item["habit"].get("name", "")).strip()
        if not habit_name:
            continue
        subprocess.run(
            [
                "notify-send",
                "--app-name=prioritise_habits",
                "--urgency=critical",
                "--expire-time=0",
                "Habit due",
                habit_name,
            ],
            check=True,
        )
        notification_count += 1

    if notification_count:
        logger.info(f"Created {notification_count} persistent desktop notifications")
    return notification_count


def get_spoken_habit_text(habit):
    habit_name = remove_existing_prefix(habit.get("name", "")).strip()
    habit_name = re.sub(
        r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]",
        lambda match: match.group(2) or match.group(1),
        habit_name,
    )
    return habit_name.replace("`", "").strip()


def is_bluetooth_audio_sink_metadata(wpctl_output):
    return (
        'device.api = "bluez5"' in wpctl_output
        or 'node.name = "bluez_output.' in wpctl_output
        or "api.bluez5." in wpctl_output
    )


def get_bluetooth_address_from_audio_sink_metadata(wpctl_output):
    address_match = re.search(r'api\.bluez5\.address = "([^"]+)"', wpctl_output)
    if address_match:
        return address_match.group(1).upper()

    node_name_match = re.search(
        r'node\.name = "bluez_output\.([0-9A-Fa-f_]{17})\.', wpctl_output
    )
    if node_name_match:
        return node_name_match.group(1).replace("_", ":").upper()
    return None


def is_default_audio_output_bluetooth():
    try:
        result = subprocess.run(
            ["wpctl", "inspect", "@DEFAULT_AUDIO_SINK@"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as error:
        logger.warning(f"Cannot inspect default audio sink for TTS: {error}")
        return False
    return is_bluetooth_audio_sink_metadata(result.stdout)


def get_bluez_media_transport_paths(bluetooth_address):
    device_path = f"/org/bluez/hci\\d+/dev_{bluetooth_address.replace(':', '_')}"
    transport_path_pattern = rf"({device_path}(?:/[^\s]+)*/fd\d+)"
    try:
        result = subprocess.run(
            ["busctl", "tree", "--list", "org.bluez"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as error:
        logger.warning(f"Cannot inspect BlueZ object tree for TTS: {error}")
        return None

    transport_paths = sorted(set(re.findall(transport_path_pattern, result.stdout)))
    if not transport_paths:
        logger.warning(
            "Cannot find BlueZ media transport for default Bluetooth audio sink"
        )
        return None
    return transport_paths


def parse_busctl_string_property(busctl_output):
    value_match = re.fullmatch(r's\s+"([^"]+)"', busctl_output.strip())
    if value_match:
        return value_match.group(1)
    return None


def get_bluez_media_transport_states(bluetooth_address):
    transport_paths = get_bluez_media_transport_paths(bluetooth_address)
    if transport_paths is None:
        return None

    transport_states = []
    for transport_path in transport_paths:
        try:
            result = subprocess.run(
                [
                    "busctl",
                    "get-property",
                    "org.bluez",
                    transport_path,
                    "org.bluez.MediaTransport1",
                    "State",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
        except (FileNotFoundError, subprocess.CalledProcessError) as error:
            logger.warning(
                f"Cannot inspect BlueZ media transport state for TTS: {error}"
            )
            return None

        transport_state = parse_busctl_string_property(result.stdout)
        if transport_state is None:
            logger.warning(
                "Cannot parse BlueZ media transport state for TTS: "
                f"{result.stdout.strip()}"
            )
            return None
        transport_states.append(transport_state)
    return transport_states


def is_default_bluetooth_audio_transport_busy():
    try:
        result = subprocess.run(
            ["wpctl", "inspect", "@DEFAULT_AUDIO_SINK@"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as error:
        logger.warning(f"Cannot inspect default audio sink transport for TTS: {error}")
        return True

    bluetooth_address = get_bluetooth_address_from_audio_sink_metadata(result.stdout)
    if bluetooth_address is None:
        logger.warning("Cannot find Bluetooth address for default audio sink")
        return True

    transport_states = get_bluez_media_transport_states(bluetooth_address)
    if transport_states is None:
        return True

    busy_states = sorted(
        {
            transport_state
            for transport_state in transport_states
            if transport_state in BLUETOOTH_AUDIO_TRANSPORT_BUSY_STATES
        }
    )
    if busy_states:
        logger.warning(
            "Skipping TTS because the default Bluetooth audio transport "
            f"is already streaming: {', '.join(busy_states)}"
        )
        return True
    return False


def get_text_to_speech_audio_path(text_to_speech_config, habit_text):
    cache_payload = {
        "provider": text_to_speech_config["provider"],
        "voiceId": text_to_speech_config["voiceId"],
        "modelId": text_to_speech_config["modelId"],
        "outputFormat": text_to_speech_config["outputFormat"],
        "text": habit_text,
    }
    cache_key = hashlib.sha256(
        json.dumps(cache_payload, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return pathlib.Path(text_to_speech_config["cacheDir"]) / f"{cache_key}.mp3"


def build_elevenlabs_text_to_speech_request(text_to_speech_config, habit_text, api_key):
    encoded_voice_id = urllib.parse.quote(text_to_speech_config["voiceId"], safe="")
    encoded_output_format = urllib.parse.quote(
        text_to_speech_config["outputFormat"], safe=""
    )
    url = (
        f"https://api.elevenlabs.io/v1/text-to-speech/{encoded_voice_id}"
        f"?output_format={encoded_output_format}"
    )
    request_body = json.dumps(
        {"text": habit_text, "model_id": text_to_speech_config["modelId"]}
    ).encode("utf-8")
    return urllib.request.Request(
        url,
        data=request_body,
        headers={
            "Accept": "audio/mpeg",
            "Content-Type": "application/json",
            "xi-api-key": api_key,
        },
        method="POST",
    )


def fetch_elevenlabs_text_to_speech_audio(text_to_speech_config, habit_text, api_key):
    request = build_elevenlabs_text_to_speech_request(
        text_to_speech_config, habit_text, api_key
    )
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            return response.read()
    except urllib.error.HTTPError as error:
        error_body = error.read().decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            f"ElevenLabs TTS failed with HTTP {error.code}: {error_body[:300]}"
        ) from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"ElevenLabs TTS request failed: {error.reason}") from error


def get_or_create_text_to_speech_audio(text_to_speech_config, habit_text):
    audio_path = get_text_to_speech_audio_path(text_to_speech_config, habit_text)
    if audio_path.exists():
        return audio_path

    api_key = os.environ.get(ELEVENLABS_API_KEY_ENV)
    if not api_key:
        raise RuntimeError(
            f"Missing required environment variable: {ELEVENLABS_API_KEY_ENV}"
        )

    audio_path.parent.mkdir(parents=True, exist_ok=True)
    audio_bytes = fetch_elevenlabs_text_to_speech_audio(
        text_to_speech_config, habit_text, api_key
    )
    temporary_path = audio_path.with_suffix(".tmp")
    with open(temporary_path, "wb") as audio_file:
        audio_file.write(audio_bytes)
    temporary_path.replace(audio_path)
    logger.info(f"Cached TTS audio at {audio_path}")
    return audio_path


def get_habit_audio_path(text_to_speech_config, habit):
    custom_audio_path = get_habit_audio_file_path(habit)
    if custom_audio_path is not None:
        if not custom_audio_path.is_file():
            raise RuntimeError(
                f"Custom habit audio file does not exist: {custom_audio_path}"
            )
        return custom_audio_path

    habit_text = get_spoken_habit_text(habit)
    if not habit_text:
        return None
    return get_or_create_text_to_speech_audio(text_to_speech_config, habit_text)


def play_audio_file(audio_path):
    subprocess.run(
        [
            "ffplay",
            "-nodisp",
            "-autoexit",
            "-hide_banner",
            "-loglevel",
            "error",
            "-af",
            f"adelay={AUDIO_PLAYBACK_LEAD_IN_MILLISECONDS}:all=1",
            str(audio_path),
        ],
        check=True,
    )


def build_phone_audio_control_url(trigger_url, state):
    if state not in PHONE_AUDIO_CONTROL_STATES:
        raise ValueError(f"Unsupported phone audio control state: {state}")

    parsed_url = urllib.parse.urlsplit(trigger_url)
    query_parameters = [
        (name, value)
        for name, value in urllib.parse.parse_qsl(
            parsed_url.query, keep_blank_values=True
        )
        if name != "state"
    ]
    query_parameters.append(("state", state))
    return urllib.parse.urlunsplit(
        parsed_url._replace(query=urllib.parse.urlencode(query_parameters))
    )


def send_phone_audio_control(trigger_url, state):
    url = build_phone_audio_control_url(trigger_url, state)
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            response.read()
    except urllib.error.HTTPError as error:
        error_body = error.read().decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            f"Phone audio {state} trigger failed with HTTP {error.code}: "
            f"{error_body[:300]}"
        ) from error
    except urllib.error.URLError as error:
        raise RuntimeError(
            f"Phone audio {state} trigger request failed: {error.reason}"
        ) from error
    logger.info(f"Sent phone audio {state} trigger")


def can_start_audio_habit_batch():
    if not is_default_audio_output_bluetooth():
        logger.warning(
            "Skipping TTS because the default audio output is not a Bluetooth sink"
        )
        return False
    return not is_default_bluetooth_audio_transport_busy()


def speak_ready_habit_triggers(
    text_to_speech_config, ready_triggers, phone_audio_control_trigger_url=None
):
    if not ready_triggers or not can_start_audio_habit_batch():
        return []

    playback_queue = []
    for item in ready_triggers:
        if not is_default_audio_output_bluetooth():
            logger.warning(
                "Skipping TTS because the default audio output is not a Bluetooth sink"
            )
            break
        try:
            audio_path = get_habit_audio_path(text_to_speech_config, item["habit"])
        except (RuntimeError, ValueError) as error:
            logger.error(f"Text-to-speech output failed: {error}")
            break
        if audio_path is None:
            logger.warning("Skipping TTS for habit with empty name")
            continue
        if not is_default_audio_output_bluetooth():
            logger.warning(
                "Skipping TTS playback because the default audio output changed"
            )
            break
        playback_queue.append((item, audio_path))

    if not playback_queue:
        return []

    spoken_triggers = []
    phone_audio_was_paused = False
    try:
        if phone_audio_control_trigger_url:
            send_phone_audio_control(phone_audio_control_trigger_url, "pause")
            phone_audio_was_paused = True
            time_module.sleep(PHONE_AUDIO_CONTROL_DELAY_SECONDS)

        if phone_audio_control_trigger_url and not can_start_audio_habit_batch():
            return []

        for item, audio_path in playback_queue:
            if (
                phone_audio_control_trigger_url
                and not is_default_audio_output_bluetooth()
            ):
                logger.warning(
                    "Skipping TTS because the default audio output is not a Bluetooth sink"
                )
                break
            play_audio_file(audio_path)
            spoken_triggers.append(item)
    except (RuntimeError, ValueError, subprocess.CalledProcessError) as error:
        logger.error(f"Text-to-speech output failed: {error}")
    finally:
        if phone_audio_was_paused:
            time_module.sleep(PHONE_AUDIO_CONTROL_DELAY_SECONDS)
            try:
                send_phone_audio_control(phone_audio_control_trigger_url, "play")
            except RuntimeError as error:
                logger.error(f"Phone audio resume failed after TTS batch: {error}")

    if spoken_triggers:
        logger.info(f"Spoke {len(spoken_triggers)} habit triggers")
    return spoken_triggers


def get_completed_habits_after_ready_triggers(ready_triggers, schedule):
    for item in ready_triggers:
        update_trigger_completion(item["habit"], item["trigger"])

    ready_habits_by_id = {
        str(item["habit"]["id"]): item["habit"] for item in ready_triggers
    }
    completed_habits = []
    checkin_times_by_habit_id = {}
    for habit_id, habit in ready_habits_by_id.items():
        habit_triggers = schedule["triggers"][habit_id]
        if not all(trigger.get("triggered") for trigger in habit_triggers):
            continue
        completed_habits.append(habit)
        latest_trigger_time = max(
            datetime.fromisoformat(trigger["time"]) for trigger in habit_triggers
        )
        checkin_times_by_habit_id[habit_id] = format_ticktick_time(latest_trigger_time)
    return completed_habits, checkin_times_by_habit_id


def main(test_mode=None):
    args = parse_arguments() if test_mode is None else None
    run_in_test_mode = args.test if args else test_mode
    if run_in_test_mode:
        logger.info("Running in test mode.")

    run_lock = acquire_run_lock(RUN_LOCK_FILE)
    if run_lock is None:
        logger.warning("Another prioritise_habits run is already active; skipping.")
        return

    try:
        config = load_config()
        text_to_speech_config = get_text_to_speech_config(config)
        phone_audio_control_trigger_url = get_phone_audio_control_trigger_url()
        store_path = get_config_path(config, "habitsStoreFile")
        active_habits_path = get_config_path(config, "activeHabitsFile")
        store = load_habit_store(store_path, active_habits_path)
        all_habits = store["habits"]
        checkins = store["checkins"]
        due_habits_today = get_habits_due_today(all_habits, checkins)

        if due_habits_today:
            logger.info(f"Found {len(due_habits_today)} long-term habits due today.")

            now = datetime.now().astimezone()
            ready_triggers, habit_trigger_schedule = get_ready_habit_triggers(
                due_habits_today, HABIT_TRIGGER_SCHEDULE_FILE, now
            )
            notes_ready_triggers = get_ready_triggers_for_due_output(
                ready_triggers, DUE_OUTPUT_WRITE_TO_MD
            )
            notification_ready_triggers = get_ready_triggers_for_due_output(
                ready_triggers, DUE_OUTPUT_DESKTOP_NOTIFICATION
            )
            text_to_speech_ready_triggers = get_ready_triggers_for_due_output(
                ready_triggers, DUE_OUTPUT_TEXT_TO_SPEECH
            )
            appended_trigger_count = append_ready_habit_triggers(
                NOTES_FILE, notes_ready_triggers
            )
            mark_triggers_output_delivered(
                notes_ready_triggers, DUE_OUTPUT_WRITE_TO_MD
            )
            save_habit_trigger_schedule(
                HABIT_TRIGGER_SCHEDULE_FILE, habit_trigger_schedule
            )
            notification_count = create_persistent_desktop_notifications(
                notification_ready_triggers
            )
            mark_triggers_output_delivered(
                notification_ready_triggers, DUE_OUTPUT_DESKTOP_NOTIFICATION
            )
            save_habit_trigger_schedule(
                HABIT_TRIGGER_SCHEDULE_FILE, habit_trigger_schedule
            )
            spoken_triggers = speak_ready_habit_triggers(
                text_to_speech_config,
                text_to_speech_ready_triggers,
                phone_audio_control_trigger_url,
            )
            mark_triggers_output_delivered(
                spoken_triggers, DUE_OUTPUT_TEXT_TO_SPEECH
            )
            save_habit_trigger_schedule(
                HABIT_TRIGGER_SCHEDULE_FILE, habit_trigger_schedule
            )
            completed_habits, checkin_times_by_habit_id = (
                get_completed_habits_after_ready_triggers(
                    ready_triggers, habit_trigger_schedule
                )
            )
            save_habit_trigger_schedule(
                HABIT_TRIGGER_SCHEDULE_FILE, habit_trigger_schedule
            )

            today = now.date()
            checkin_stamp = int(today.strftime("%Y%m%d"))
            payload = build_checkin_payload(
                completed_habits, checkins, checkin_stamp, checkin_times_by_habit_id
            )
            checkins_to_apply = [
                entry["habitId"] for entry in payload["add"] + payload["update"]
            ]

            applied_checkins = apply_checkin_payload(checkins, payload)
            logger.info(
                f"Appended {appended_trigger_count} ready habit triggers, "
                f"created {notification_count} desktop notifications, "
                f"spoke {len(spoken_triggers)} habit triggers, "
                f"marked {len(checkins_to_apply)} habits as completed, "
                f"and applied {applied_checkins} checkins."
            )

            sorted_habits = sort_habits_by_completion_rate(
                due_habits_today, checkins, config["lookBackDays"]
            )
            sorted_habits = update_habit_text(sorted_habits)
            sorted_habits = update_habit_sort_order(sorted_habits)
            all_habits = merge_habit_updates(all_habits, sorted_habits)
            save_habit_store(store_path, active_habits_path, all_habits, checkins)

            update_last_run()
            logger.info("Script execution completed and last run time updated.")
        else:
            logger.info("No long-term habits due today.")
    except FileNotFoundError as error:
        logger.error(f"Missing required file: {error}")
    except (json.JSONDecodeError, ValueError, KeyError, TypeError) as error:
        logger.error(f"Failed to process local habit store/config: {error}")
    finally:
        run_lock.close()


if __name__ == "__main__":
    main()
