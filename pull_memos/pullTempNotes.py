import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from loguru import logger
import fcntl
import os
import re
import glob
import datetime
import time
from openai import AuthenticationError, OpenAI
import hashlib
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import unquote_plus, urlsplit, urlunsplit
from dotenv import load_dotenv
from send2trash import send2trash
from processed_hashes import ProcessedHashes
from keep_auth import authenticate_keep, sync_keep

load_dotenv()

CONFIG_FILE = Path(__file__).with_name("config.json")


def load_config():
    with open(CONFIG_FILE, "r") as file:
        return json.load(file)


def terminal_safe_markdown_path(file_path):
    path = Path(file_path)
    has_whitespace = any(character.isspace() for character in path.name)
    if path.suffix.lower() != ".md" or not has_whitespace:
        return path

    collapsed_stem = "-".join(path.stem.split())
    return path.with_name(f"{collapsed_stem}{path.suffix.lower()}")


def ensure_terminal_safe_markdown_path(file_path):
    path = Path(file_path)
    safe_path = terminal_safe_markdown_path(path)
    if safe_path != path:
        raise ValueError(
            f"Markdown note path contains whitespace: {path}. Use {safe_path} instead."
        )


# Initialize the hash tracker
HASH_FILE = os.path.join(os.path.dirname(__file__), "audio_hashes.json")
processed_hashes = ProcessedHashes(HASH_FILE)

LOG_DIR = os.path.join(os.path.dirname(__file__), "logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "pullTempNotes.log")
LOCK_FILE = os.path.join(LOG_DIR, "pullTempNotes.lock")
KEEP_URL_RETRY_COUNTS_FILE = os.path.join(LOG_DIR, "keep_url_retry_counts.json")
KEEP_CAPTURE_LEDGER_FILE = os.path.join(LOG_DIR, "keep_capture_ledger.json")
AUDIO_TRANSCRIPTS_DIR = os.path.join(LOG_DIR, "audio_transcripts")
# Known to fit one gpt-4o-transcribe request; OpenAI only publishes the 25 MB limit.
TRANSCRIPTION_CHUNK_SECONDS = 1100
TRANSCRIPTION_WORKERS = 20
MAX_KEEP_URL_CONVERSION_ATTEMPTS = 3


def keep_route_prefix_pattern(prefix):
    return re.compile(rf"^\s*{re.escape(prefix)}\s+", re.IGNORECASE)


QUESTION_PREFIX_PATTERN = keep_route_prefix_pattern("qq")
FRIENDS_PREFIX_PATTERN = keep_route_prefix_pattern("ff")
INFOLIO_PREFIX_PATTERN = keep_route_prefix_pattern("ii")
logger.add(
    LOG_FILE,
    rotation="10 MB",
    retention="10 days",
    enqueue=False,
    backtrace=True,
    diagnose=True,
)


@dataclass
class KeepUrlAction:
    note: object
    note_title: str
    raw_text: str
    lineate_urls: list[str]
    output_dest: str = "browser"
    success_text: str = ""


@dataclass
class KeepNoteRoute:
    destination: str
    text: str
    url_action: KeepUrlAction | None = None


@dataclass
class KeepSyncPlan:
    temp_text: str
    writing_text: str
    friends_text: str
    url_actions: list[KeepUrlAction]
    saved_texts: dict[str, str]


def formatIncomingText(text, isTranscription):
    pattern = re.compile(r"(^\d+\.\s*)http")
    modified_lines = []

    for line in text.split("\n"):
        line = pattern.sub("http", line)
        line = " ".join(
            word.lower() if isTranscription else word for word in line.split()
        )  # only convert transcribed text to lowercase. otherwise could clobber case sensitive text such as urls from gkeep
        if line:
            line = line[0].lower() + line[1:]  # lowercase first caps letter
        line = line.strip(".!?") if isTranscription else line
        modified_lines.append(line)

    modified_text = "\n".join(modified_lines)
    return modified_text


def remove_keep_text_fragment_url_lines(note_text):
    kept_lines = []
    for line in note_text.split("\n"):
        stripped_line = line.lstrip()
        if stripped_line.startswith("http") and "#:~:text=" in stripped_line:
            continue
        kept_lines.append(line)
    return "\n".join(kept_lines)


def calculate_file_hash(file_path, hash_algo="sha256"):
    """Calculate the hash of a chunk from the middle of a file."""
    hash_func = getattr(hashlib, hash_algo)()
    file_size = os.path.getsize(file_path)
    chunk_size = min(8192, file_size)
    middle_pos = file_size // 2

    with open(file_path, "rb") as file:
        file.seek(middle_pos - chunk_size // 2)
        chunk = file.read(chunk_size)
        hash_func.update(chunk)

    return hash_func.hexdigest()


def delete_duplicate_files(directory):
    """Delete files with matching hashes in a directory, sending them to trash."""
    if not os.path.isdir(directory):
        raise ValueError(f"The provided path {directory} is not a directory.")

    hashes = {}
    for root, _, files in os.walk(directory):
        for filename in files:
            file_path = os.path.join(root, filename)
            file_hash = calculate_file_hash(file_path)
            if file_hash in hashes:
                # This file is a duplicate; send it to the trash
                send2trash(file_path)
                logger.info(f"Moved to trash due to duplicate hash: {file_path}")
            else:
                # Record the hash of this unique file
                hashes[file_hash] = file_path


def load_keep_url_retry_counts():
    if not os.path.exists(KEEP_URL_RETRY_COUNTS_FILE):
        return {}

    with open(KEEP_URL_RETRY_COUNTS_FILE, "r") as file:
        retry_counts = json.load(file)

    if not isinstance(retry_counts, dict):
        raise RuntimeError(
            f"Expected retry counts dict in {KEEP_URL_RETRY_COUNTS_FILE}"
        )

    return {str(key): int(value) for key, value in retry_counts.items()}


def save_keep_url_retry_counts(retry_counts):
    with open(KEEP_URL_RETRY_COUNTS_FILE, "w") as file:
        json.dump(retry_counts, file, indent=2, sort_keys=True)


def get_keep_url_retry_key(note, note_title, note_text):
    note_id = getattr(note, "id", None)
    if note_id:
        return str(note_id)
    note_signature = f"{note_title}\n{note_text}".encode("utf-8")
    return hashlib.sha256(note_signature).hexdigest()


def record_keep_url_conversion_failure(url_action, retry_counts):
    retry_key = get_keep_url_retry_key(
        url_action.note, url_action.note_title, url_action.raw_text
    )
    failure_count = retry_counts.get(retry_key, 0) + 1

    should_fallback_to_raw_text = failure_count >= MAX_KEEP_URL_CONVERSION_ATTEMPTS
    if should_fallback_to_raw_text:
        retry_counts.pop(retry_key, None)
    else:
        retry_counts[retry_key] = failure_count

    save_keep_url_retry_counts(retry_counts)
    return failure_count, should_fallback_to_raw_text


def clear_keep_url_retry_count(url_action, retry_counts):
    retry_key = get_keep_url_retry_key(
        url_action.note, url_action.note_title, url_action.raw_text
    )
    if retry_key not in retry_counts:
        return
    retry_counts.pop(retry_key, None)
    save_keep_url_retry_counts(retry_counts)


def append_keep_note_text(existing_text, note_text):
    if not note_text:
        return existing_text
    return existing_text + "\n\n" + note_text


def strip_prefix(text, prefix_pattern):
    match = prefix_pattern.match(text)
    if not match:
        return None
    return text[match.end() :].strip()


def format_prefixed_keep_note(note_title, note_text, prefix_pattern):
    stripped_title = strip_prefix(note_title, prefix_pattern)
    if stripped_title is not None:
        parts = [stripped_title, note_text.strip()]
        return "\n".join(part for part in parts if part)

    stripped_text = strip_prefix(note_text, prefix_pattern)
    if stripped_text is None:
        return None
    parts = [note_title.strip(), stripped_text]
    return "\n".join(part for part in parts if part)


def load_keep_capture_ledger():
    if not os.path.exists(KEEP_CAPTURE_LEDGER_FILE):
        return {}

    with open(KEEP_CAPTURE_LEDGER_FILE, "r") as file:
        return json.load(file)


def save_keep_capture_ledger(capture_ledger):
    # A truncated ledger would stop every later run, so replace it atomically.
    temporary_path = f"{KEEP_CAPTURE_LEDGER_FILE}.tmp"
    with open(temporary_path, "w") as file:
        json.dump(capture_ledger, file, indent=2, sort_keys=True)
    os.replace(temporary_path, KEEP_CAPTURE_LEDGER_FILE)


def route_keep_note(gnote):
    url_pattern = re.compile(r"https?://\S+")
    noteText, noteTitle = (
        formatIncomingText(
            remove_keep_text_fragment_url_lines(gnote.text.strip()), False
        ),
        gnote.title.strip(),
    )
    question_text = format_prefixed_keep_note(
        noteTitle, noteText, QUESTION_PREFIX_PATTERN
    )
    if question_text is not None:
        return KeepNoteRoute("writing", question_text)

    friend_text = format_prefixed_keep_note(noteTitle, noteText, FRIENDS_PREFIX_PATTERN)
    if friend_text is not None:
        return KeepNoteRoute("friends", friend_text)

    infolio_text = strip_prefix(noteText, INFOLIO_PREFIX_PATTERN)
    note_text_for_url_only_check = (
        infolio_text if infolio_text is not None else noteText
    ).rstrip()
    has_infolio_marker = infolio_text is not None
    urls = [
        raw_url.rstrip(".,!?)]") for raw_url in url_pattern.findall(noteText.strip())
    ]
    is_url_only_note = (
        urls and url_pattern.sub("", note_text_for_url_only_check).strip() == ""
    )
    slack_urls = [url for url in urls if "slack.com" in url]
    non_slack_urls = [url for url in urls if "slack.com" not in url]
    if is_url_only_note and has_infolio_marker:
        return KeepNoteRoute(
            "temp",
            noteText,
            KeepUrlAction(
                note=gnote,
                note_title=noteTitle,
                raw_text=noteText,
                lineate_urls=urls,
                output_dest="infolio",
            ),
        )
    if is_url_only_note and non_slack_urls:
        return KeepNoteRoute(
            "temp",
            noteText,
            KeepUrlAction(
                note=gnote,
                note_title=noteTitle,
                raw_text=noteText,
                lineate_urls=non_slack_urls,
                success_text=(("\n\n" + "\n".join(slack_urls)) if slack_urls else ""),
            ),
        )
    if is_url_only_note:
        return KeepNoteRoute("temp", "\n".join(slack_urls))
    return KeepNoteRoute("temp", noteText)


def build_keep_sync_plan(keep, capture_ledger):
    gnotes = list(keep.find(archived=False, trashed=False))
    gnotes = sorted(gnotes, key=lambda x: x.timestamps.edited.timestamp())
    text_by_destination = {"temp": "", "writing": "", "friends": ""}
    url_actions = []
    saved_texts = {}

    for gnote in gnotes:
        isWatchNote = gnote.title in ["Questions", "Statements", "Notes"]
        isEmpty = (gnote.text + gnote.title).strip() == ""
        if isEmpty or isWatchNote or gnote.id in capture_ledger:
            continue
        route = route_keep_note(gnote)
        if route.url_action:
            url_actions.append(route.url_action)
            continue
        logger.debug(
            f"Text from keep note {gnote.title.strip() or '[untitled]'}: {route.text}"
        )
        text_by_destination[route.destination] = append_keep_note_text(
            text_by_destination[route.destination], route.text
        )
        saved_texts[gnote.id] = route.text

    return KeepSyncPlan(
        text_by_destination["temp"],
        text_by_destination["writing"],
        text_by_destination["friends"],
        url_actions,
        saved_texts,
    )


def replace_saved_keep_text(file_path, saved_text, current_text):
    with open(file_path, "r") as file:
        file_text = file.read()
    # Whole lines only, so a short note cannot match inside a longer one.
    saved_text_pattern = re.compile(rf"^{re.escape(saved_text)}$", re.MULTILINE)
    updated_text, replacement_count = saved_text_pattern.subn(
        lambda _: current_text, file_text, count=1
    )
    if replacement_count:
        with open(file_path, "w") as file:
            file.write(updated_text)
    return replacement_count > 0


def apply_keep_note_edits(keep, capture_ledger, file_paths):
    for note_id, saved_text in list(capture_ledger.items()):
        gnote = keep.get(note_id)
        if gnote is None:
            # Keep purged it from the trash, so it can no longer change.
            del capture_ledger[note_id]
            continue
        route = route_keep_note(gnote)
        if not route.text or route.text == saved_text:
            continue
        file_path = file_paths[route.destination]
        if saved_text and replace_saved_keep_text(file_path, saved_text, route.text):
            logger.info(f"Updated Keep note {note_id} in {file_path} with its edits")
        else:
            logger.warning(
                f"Saved text of Keep note {note_id} is not in {file_path}; "
                "appending its edited text"
            )
            writeToFile(file_path, "\n\n" + route.text)
        capture_ledger[note_id] = route.text


def trash_settled_keep_notes(keep, capture_ledger):
    quiet_period_seconds = load_config()["keepNoteQuietPeriodSeconds"]
    settled_notes = [
        gnote
        for gnote in keep.find(archived=False, trashed=False)
        if time.time() - gnote.timestamps.updated.timestamp() >= quiet_period_seconds
        and (gnote.id in capture_ledger or (gnote.text + gnote.title).strip() == "")
    ]
    for gnote in settled_notes:
        gnote.trash()
    if settled_notes:
        sync_keep(keep)


def run_lineate_for_urls(urls, output_dest="browser"):
    if not urls:
        return
    if output_dest not in ("browser", "infolio"):
        raise ValueError("Lineate output destination must be browser or infolio.")
    urls_text = " ".join(urls)
    command = [
        str(Path.home() / "dev/lineate/run.sh"),
        "--force-convert-all",
        "--summarise",
        "--output-dest",
        output_dest,
        urls_text,
    ]
    logger.info(f"Running lineate for {len(urls)} urls with output dest {output_dest}")
    env = os.environ.copy()
    env["DISPLAY"] = ":0"
    subprocess.run(command, check=True, env=env)


def url_for_history(url):
    """Keep the resource address without persisting query-string credentials."""
    parsed = urlsplit(url)
    credential_parameters = {
        "token", "access_token", "refresh_token", "id_token",
        "api_key", "apikey", "client_secret", "password",
        "signature", "x-amz-signature", "x-goog-signature",
    }
    query_parts = parsed.query.split("&")
    retained_parts = [
        part for part in query_parts
        if unquote_plus(part.partition("=")[0]).lower() not in credential_parameters
    ]
    if retained_parts == query_parts:
        return url
    return urlunsplit(parsed._replace(query="&".join(retained_parts)))


def append_opened_urls(urls, file_path):
    if not urls:
        return
    ensure_terminal_safe_markdown_path(file_path)
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(file_path, "a+") as file:
        file.seek(0, os.SEEK_END)
        if file.tell() > 0:
            file.seek(file.tell() - 1)
            if file.read(1) != "\n":
                file.write("\n")
        for url in urls:
            file.write(f"{timestamp} {url_for_history(url)}\n")


def tryDeleteFile(path, fileText):
    fileExt = path.split(".")[-1]
    oldFileName = path.split("/")[-1]
    newFileName = (
        "".join([char for char in fileText if char.isalnum() or char == " "][:120])
        + "."
        + fileExt
    )
    newFilePath = path.replace(oldFileName, newFileName)
    os.rename(path, newFilePath)
    try:
        send2trash(newFilePath)
        logger.info(f"Moved {newFilePath} to trash after successful transcription")
    except Exception as error:
        logger.exception(f"Failed moving {newFilePath} to trash: {error}")


def split_audio_into_chunks(audio_path, chunk_dir):
    # Speech-grade mono keeps every chunk far below the 25 MB upload limit.
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-loglevel",
            "error",
            "-i",
            audio_path,
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-b:a",
            "64k",
            "-f",
            "segment",
            "-segment_time",
            str(TRANSCRIPTION_CHUNK_SECONDS),
            "-reset_timestamps",
            "1",
            str(Path(chunk_dir) / "chunk-%05d.mp3"),
        ],
        check=True,
    )
    return sorted(Path(chunk_dir).glob("chunk-*.mp3"))


def transcribe_audio_chunk(client, chunk_path, transcript_path):
    if transcript_path.exists():
        return transcript_path.read_text()
    with open(chunk_path, "rb") as audio_file:
        transcript = client.audio.transcriptions.create(
            model="gpt-4o-transcribe",
            file=audio_file,
            response_format="text",
        )
    transcript_path.write_text(transcript)
    return transcript


def processMp3File(mp3FileName, transcript_dir):
    try:
        client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
        transcript_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory() as chunk_dir:
            chunk_paths = split_audio_into_chunks(mp3FileName, chunk_dir)
            logger.info(f"Transcribing {len(chunk_paths)} chunks of {mp3FileName}")
            with ThreadPoolExecutor(TRANSCRIPTION_WORKERS) as executor:
                chunk_transcripts = list(
                    executor.map(
                        lambda chunk_path: transcribe_audio_chunk(
                            client, chunk_path, transcript_dir / f"{chunk_path.stem}.txt"
                        ),
                        chunk_paths,
                    )
                )
    except AuthenticationError:
        raise
    except Exception as error:
        logger.exception(f"Transcription failed for {mp3FileName}: {error}")
        return "", False

    logger.info(f"Transcription completed for {mp3FileName}")
    return "\n\n".join(chunk_transcripts), True


def sort_key(filename):
    match = re.search(r"(\d+)", os.path.basename(filename))
    if match:
        return int(match.group(1))
    return 0


def transcript_inbox_text(transcript, mp3File, config):
    if len(transcript) <= config["maxInboxTranscriptChars"]:
        return transcript

    # The inbox is shared to a gist and read whole by agents, and a long recording
    # may hold other people's speech, so only a link goes there.
    recording_date = datetime.date.fromtimestamp(os.path.getmtime(mp3File)).isoformat()
    recording_name = "-".join(Path(mp3File).stem.lower().split())
    note_path = Path(config["tempNotesPath"]).with_name(
        f"voice-transcript-{recording_date}-{recording_name}.md"
    )
    note_path.write_text(transcript + "\n")
    return f"voice transcript: [[{note_path.stem}]]"


def saveNotesFromMp3s():
    config = load_config()
    mp3FolderPath = config["mp3CaptureFolder"]
    textToAddToFile = ""
    processedMp3s = {}

    musicFiles = sorted(
        glob.glob(mp3FolderPath + "/*.mp3") + glob.glob(mp3FolderPath + "/*.m4a"),
        key=sort_key,
    )
    for mp3File in musicFiles:
        fileName = mp3File.split("/")[-1]
        file_hash = calculate_file_hash(mp3File)

        # Check if we've already processed this file hash
        if processed_hashes.is_hash_processed(file_hash):
            logger.info(f"Skipping already processed file: {fileName}")
            processedMp3s[fileName] = {
                "raw_text": "ALREADY PROCESSED",
                "formatted_text": "ALREADY PROCESSED",
                "transcription_successful": True,
                "new_hash": None,
            }
            continue

        logger.info(f"Processing {fileName}")
        try:
            raw_text, transcription_successful = processMp3File(
                mp3File, Path(AUDIO_TRANSCRIPTS_DIR) / file_hash
            )
        except AuthenticationError as error:
            # Every remaining file would fail the same way after its own slow split.
            logger.error(f"OpenAI rejected the API key; leaving audio to retry: {error}")
            break
        formatted_text = formatIncomingText(raw_text, True) if raw_text else ""

        if transcription_successful:
            if formatted_text:
                textToAddToFile += "\n\n" + transcript_inbox_text(
                    formatted_text, mp3File, config
                )
                logger.debug(f"Transcription output for {fileName}: {formatted_text}")
        else:
            logger.info(
                f"Transcription failed for {fileName}; leaving file in source folder for retry"
            )

        processedMp3s[fileName] = {
            "raw_text": raw_text,
            "formatted_text": formatted_text,
            "transcription_successful": transcription_successful,
            "new_hash": file_hash if transcription_successful else None,
        }

    return textToAddToFile, processedMp3s


def remove_duplicate_beginnings(text):
    lines = text.split("\n")
    stripped_lines = [line.strip() for line in lines]
    result_lines = []
    allowedDuplicates = ["---", ""]

    for i, line in enumerate(lines):
        is_duplicate = False
        for j, other_line in enumerate(stripped_lines):
            differentIndex = i != j
            sharedPrefix = other_line.startswith(line.strip())
            # only check earlier lines for exact matches, to avoid deleting all instances of a string present on multiple lines:
            exactMatchOnLaterLine = other_line.lower() == line.lower() and j > i
            if differentIndex and sharedPrefix and not exactMatchOnLaterLine:
                is_duplicate = True
                break

        if not is_duplicate or line.strip() in allowedDuplicates:
            result_lines.append(line)

    return "\n".join(result_lines)


def writeToFile(filePath, textToAddToFile):
    if not textToAddToFile.strip():
        return
    ensure_terminal_safe_markdown_path(filePath)
    with open(filePath, "r") as f:
        text = f.read()
    existingText = text.strip()
    if existingText.split("\n")[-1][0] == "#":
        existingText += "\n"
    existingText += textToAddToFile
    existingText = remove_duplicate_beginnings(existingText)
    if existingText[-1] != "\n":
        existingText += "\n"
    if existingText.strip().split("\n")[-1][0] == "#":
        existingText += "\n"
    existingText = existingText.replace("\n\n\n", "\n\n")
    with open(filePath, "w") as f:
        f.write(existingText)


def acquire_script_lock():
    lock_handle = open(LOCK_FILE, "a+")
    try:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        logger.info("Another pullTempNotes.py run is active; exiting without work")
        lock_handle.close()
        raise SystemExit(0)
    return lock_handle


def sync_keep_notes(
    keep, temp_file_path, writing_file_path, friends_file_path, opened_urls_path
):
    capture_ledger = load_keep_capture_ledger()
    apply_keep_note_edits(
        keep,
        capture_ledger,
        {
            "temp": temp_file_path,
            "writing": writing_file_path,
            "friends": friends_file_path,
        },
    )
    keep_plan = build_keep_sync_plan(keep, capture_ledger)
    # Plain Keep text should land promptly even when downstream URL conversion is slow.
    writeToFile(temp_file_path, keep_plan.temp_text)
    writeToFile(writing_file_path, keep_plan.writing_text)
    writeToFile(friends_file_path, keep_plan.friends_text)
    capture_ledger.update(keep_plan.saved_texts)
    save_keep_capture_ledger(capture_ledger)

    keep_url_retry_counts = load_keep_url_retry_counts()
    deferred_keep_text = ""

    for url_action in keep_plan.url_actions:
        if url_action.lineate_urls:
            try:
                run_lineate_for_urls(url_action.lineate_urls, url_action.output_dest)
            except subprocess.CalledProcessError:
                failure_count, should_fallback_to_raw_text = (
                    record_keep_url_conversion_failure(
                        url_action, keep_url_retry_counts
                    )
                )
                logger.warning(
                    f"Lineate failed for Keep note {url_action.note_title if url_action.note_title else '[untitled]'} "
                    f"({failure_count}/{MAX_KEEP_URL_CONVERSION_ATTEMPTS})"
                )
                if should_fallback_to_raw_text:
                    deferred_keep_text = append_keep_note_text(
                        deferred_keep_text, url_action.raw_text
                    )
                    capture_ledger[url_action.note.id] = url_action.raw_text
                continue
            if url_action.output_dest == "browser":
                append_opened_urls(url_action.lineate_urls, opened_urls_path)

        clear_keep_url_retry_count(url_action, keep_url_retry_counts)
        deferred_keep_text += url_action.success_text
        capture_ledger[url_action.note.id] = url_action.raw_text

    writeToFile(temp_file_path, deferred_keep_text)
    save_keep_capture_ledger(capture_ledger)
    trash_settled_keep_notes(keep, capture_ledger)


def record_processed_mp3s(processed_mp3s):
    for mp3File, mp3Outcome in processed_mp3s.items():
        if not mp3Outcome["new_hash"]:
            continue
        processed_hashes.add_hash(
            mp3Outcome["new_hash"],
            {
                "filename": mp3File,
                "processed_date": str(datetime.datetime.now()),
            },
        )
        shutil.rmtree(Path(AUDIO_TRANSCRIPTS_DIR) / mp3Outcome["new_hash"])


def delete_processed_mp3s(processed_mp3s, mp3_folder_path):
    for mp3File, mp3Outcome in processed_mp3s.items():
        if not mp3Outcome["transcription_successful"]:
            logger.info(
                f"Skipping deletion for {mp3File} because transcription failed; leaving for retry"
            )
            continue

        fileText = (
            mp3Outcome["formatted_text"]
            or mp3Outcome["raw_text"]
            or os.path.splitext(mp3File)[0]
        )
        logger.info(f"Deleting {mp3File}")
        tryDeleteFile(os.path.join(mp3_folder_path, mp3File), fileText)


def commit_processed_mp3_batch(
    temp_file_path, text_to_add_to_file, processed_mp3s, mp3_folder_path
):
    # Keep the temp-note write and source-file trashing adjacent so the capture
    # folder reflects only audio that has not landed in temp notes yet. A hash
    # recorded before the write would get unsaved audio trashed after a crash.
    writeToFile(temp_file_path, text_to_add_to_file)
    record_processed_mp3s(processed_mp3s)
    delete_processed_mp3s(processed_mp3s, mp3_folder_path)


def main():
    lock_handle = acquire_script_lock()
    try:
        config = load_config()
        tempFilePath, writingFilePath, mp3FolderPath = (
            config["tempNotesPath"],
            config["writingNotesPath"],
            config["mp3CaptureFolder"],
        )
        friendsFilePath = config["friendsNotesPath"]
        delete_duplicate_files(mp3FolderPath)

        textToAddToFile, processedMp3s = saveNotesFromMp3s()
        commit_processed_mp3_batch(
            tempFilePath, textToAddToFile, processedMp3s, mp3FolderPath
        )

        # Authenticating is the only Keep pull. Audio can take minutes, so pulling
        # before it would save and trash notes from a snapshot older than their edits.
        logger.info("Authenticating with Google Keep")
        keep = authenticate_keep()
        logger.info("Authenticated with Google Keep")
        sync_keep_notes(
            keep,
            tempFilePath,
            writingFilePath,
            friendsFilePath,
            str(Path.home() / "notes/opened-urls.md"),
        )
    except Exception as error:
        logger.exception(f"pullTempNotes.py failed: {error}")
        raise
    finally:
        lock_handle.close()


if __name__ == "__main__":
    main()
