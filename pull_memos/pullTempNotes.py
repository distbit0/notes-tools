import json
from dataclasses import dataclass

from loguru import logger
from pydub.utils import mediainfo
import fcntl
import os
import re
import glob
import datetime
import time
from openai import OpenAI
import hashlib
import subprocess
from pathlib import Path
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
class KeepSyncPlan:
    temp_text: str
    writing_text: str
    friends_text: str
    url_actions: list[KeepUrlAction]
    notes_to_trash: list[object]


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


def commit_keep_sync_batch(keep, file_writes, notes_to_trash):
    if not any(text for _, text in file_writes) and not notes_to_trash:
        return

    for file_path, text in file_writes:
        if not text:
            continue
        writeToFile(file_path, text)
    for gnote in notes_to_trash:
        gnote.trash()
    sync_keep(keep)


def build_keep_sync_plan(keep):
    gnotes = list(keep.find(archived=False, trashed=False))
    gnotes = sorted(gnotes, key=lambda x: x.timestamps.edited.timestamp())
    textToAddToFile = ""
    writing_text = ""
    friends_text = ""
    url_actions = []
    notes_to_trash = []
    url_pattern = re.compile(r"https?://\S+")

    for gnote in gnotes:
        isWatchNote = gnote.title in ["Questions", "Statements", "Notes"]
        isEmpty = (gnote.text + gnote.title).strip() == ""
        if isEmpty:
            notes_to_trash.append(gnote)
        if isEmpty or isWatchNote:
            continue
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
            writing_text = append_keep_note_text(writing_text, question_text)
            notes_to_trash.append(gnote)
            continue

        friend_text = format_prefixed_keep_note(
            noteTitle, noteText, FRIENDS_PREFIX_PATTERN
        )
        if friend_text is not None:
            friends_text = append_keep_note_text(friends_text, friend_text)
            notes_to_trash.append(gnote)
            continue

        infolio_text = strip_prefix(noteText, INFOLIO_PREFIX_PATTERN)
        note_text_for_url_only_check = (
            infolio_text if infolio_text is not None else noteText
        ).rstrip()
        has_infolio_marker = infolio_text is not None
        urls = [
            raw_url.rstrip(".,!?)]")
            for raw_url in url_pattern.findall(noteText.strip())
        ]
        is_url_only_note = (
            urls and url_pattern.sub("", note_text_for_url_only_check).strip() == ""
        )
        slack_urls = [url for url in urls if "slack.com" in url]
        non_slack_urls = [url for url in urls if "slack.com" not in url]
        should_add_urls_to_infolio = is_url_only_note and has_infolio_marker
        if should_add_urls_to_infolio:
            url_actions.append(
                KeepUrlAction(
                    note=gnote,
                    note_title=noteTitle,
                    raw_text=noteText,
                    lineate_urls=urls,
                    output_dest="infolio",
                )
            )
            continue
        if is_url_only_note:
            if non_slack_urls:
                url_actions.append(
                    KeepUrlAction(
                        note=gnote,
                        note_title=noteTitle,
                        raw_text=noteText,
                        lineate_urls=non_slack_urls,
                        success_text=(
                            ("\n\n" + "\n".join(slack_urls)) if slack_urls else ""
                        ),
                    )
                )
                continue
            if slack_urls:
                textToAddToFile += "\n\n" + "\n".join(slack_urls)
            notes_to_trash.append(gnote)
            continue
        logger.debug(
            f"Text from keep note {noteTitle if noteTitle else '[untitled]'}: {noteText}"
        )
        if noteText or noteTitle:
            textToAddToFile += "\n"
        noteTitle = ""
        if "http" not in noteText:
            textToAddToFile += "\n" + noteTitle if noteTitle else ""
            textToAddToFile += ":" if noteText and noteTitle else ""
        textToAddToFile += "\n" + noteText if noteText else ""
        notes_to_trash.append(gnote)

    return KeepSyncPlan(
        textToAddToFile, writing_text, friends_text, url_actions, notes_to_trash
    )


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
            file.write(f"{timestamp} {url}\n")


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


def processMp3File(mp3FileName):
    # Get the duration of the audio file
    info = mediainfo(mp3FileName)

    if "duration" not in info:
        logger.error(f"Could not determine duration for {mp3FileName}")
        return "DURATION UNKNOWN", False

    duration = float(info["duration"])
    temp_file = None
    if duration > 1100:
        logger.info(f"Cropping {mp3FileName} to 1100 seconds")
        from pydub import AudioSegment

        audio = AudioSegment.from_file(mp3FileName)
        audio = audio[: 1100 * 1000]  # pydub works in milliseconds
        temp_file = mp3FileName + ".temp.mp3"
        audio.export(temp_file, format="mp3")
        mp3FileName = temp_file

    try:
        apiKey = os.environ["OPENAI_API_KEY"]
        client = OpenAI(api_key=apiKey)
        with open(mp3FileName, "rb") as audio_file:
            api_response = client.audio.transcriptions.create(
                model="gpt-4o-transcribe",
                file=audio_file,
                response_format="text",
            )
        transcribed_text = api_response
        success = True
        logger.info(f"Transcription completed for {mp3FileName}")
    except Exception as error:
        transcribed_text = f"transcription api error {time.time()}"
        success = False
        logger.exception(f"Transcription failed for {mp3FileName}: {error}")

    if temp_file:
        try:
            os.remove(temp_file)
            logger.debug(f"Removed temporary audio file {temp_file}")
        except Exception as error:
            logger.exception(f"Error removing temporary file {temp_file}: {error}")

    return transcribed_text, success


def sort_key(filename):
    match = re.search(r"(\d+)", os.path.basename(filename))
    if match:
        return int(match.group(1))
    return 0


def saveNotesFromMp3s():
    mp3FolderPath = load_config()["mp3CaptureFolder"]
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
            }
            continue

        logger.info(f"Processing {fileName}")
        raw_text, transcription_successful = processMp3File(mp3File)
        formatted_text = formatIncomingText(raw_text, True) if raw_text else ""

        if transcription_successful:
            if formatted_text:
                textToAddToFile += "\n\n" + formatted_text
                logger.debug(f"Transcription output for {fileName}: {formatted_text}")

            # Record the processed file hash
            processed_hashes.add_hash(
                file_hash,
                {
                    "filename": fileName,
                    "processed_date": str(datetime.datetime.now()),
                },
            )
        else:
            logger.info(
                f"Transcription failed for {fileName}; leaving file in source folder for retry"
            )

        processedMp3s[fileName] = {
            "raw_text": raw_text,
            "formatted_text": formatted_text,
            "transcription_successful": transcription_successful,
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
    keep_plan = build_keep_sync_plan(keep)
    # Plain Keep text should land promptly even when downstream URL conversion is slow.
    commit_keep_sync_batch(
        keep,
        [
            (temp_file_path, keep_plan.temp_text),
            (writing_file_path, keep_plan.writing_text),
            (friends_file_path, keep_plan.friends_text),
        ],
        keep_plan.notes_to_trash,
    )

    keep_url_retry_counts = load_keep_url_retry_counts()
    deferred_keep_text = ""
    deferred_notes_to_trash = []

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
                    deferred_notes_to_trash.append(url_action.note)
                continue
            if url_action.output_dest == "browser":
                append_opened_urls(url_action.lineate_urls, opened_urls_path)

        clear_keep_url_retry_count(url_action, keep_url_retry_counts)
        deferred_keep_text += url_action.success_text
        deferred_notes_to_trash.append(url_action.note)

    commit_keep_sync_batch(
        keep, [(temp_file_path, deferred_keep_text)], deferred_notes_to_trash
    )


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
    # folder reflects only audio that has not landed in temp notes yet.
    writeToFile(temp_file_path, text_to_add_to_file)
    delete_processed_mp3s(processed_mp3s, mp3_folder_path)


def main():
    lock_handle = acquire_script_lock()
    try:
        logger.info("Authenticating with Google Keep")
        keep = authenticate_keep()
        logger.info("Authenticated with Google Keep")
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
