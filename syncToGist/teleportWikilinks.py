import os
from utils import *
import re


def note_slug(value):
    stem = os.path.basename(value.strip())
    if stem.lower().endswith(".md"):
        stem = stem[:-3]
    if "#" in stem:
        stem = stem.split("#", 1)[0]

    slug_chars = []
    previous_was_separator = False
    for character in stem.lower():
        if character.isalnum():
            slug_chars.append(character)
            previous_was_separator = False
        elif not previous_was_separator:
            slug_chars.append("-")
            previous_was_separator = True
    return "".join(slug_chars).strip("-")


def note_filename(value):
    return note_slug(value) + ".md"


def readable_note_title(value):
    return os.path.splitext(os.path.basename(value))[0].replace("-", " ")


def find_file_by_name(directory, file_name):
    if file_name == None:
        return None
    target_slug = note_slug(file_name)
    matches = []
    for file in os.listdir(directory):
        file_path = os.path.join(directory, file)
        if (
            os.path.isfile(file_path)
            and file.endswith(".md")
            and note_slug(file) == target_slug
        ):
            matches.append(file_path)
    if len(matches) > 1:
        raise RuntimeError(f"Ambiguous note filename for {file_name}: {matches}")
    return matches[0] if matches else None


def get_all_markdown_files(directory):
    markdown_files = []
    for file in os.listdir(directory):
        file_path = os.path.join(directory, file)
        if os.path.isfile(file_path) and file.endswith(".md"):
            markdown_files.append(file_path)
    return markdown_files


def process_wikilink(lineOfMatch, linkedNoteName, wikilinkText, directory):
    teleportFlag = "t"
    if (
        (f" {teleportFlag} " not in lineOfMatch)
        and (not lineOfMatch.startswith(f"{teleportFlag} "))
        and (not lineOfMatch.endswith(f" {teleportFlag}"))
    ):
        return False
    print("wikilink", wikilinkText, "in", lineOfMatch)
    lineWithoutLinks = (
        lineOfMatch.replace(wikilinkText, "")
        .replace(",", "")
        .replace(".", "")
        .replace(teleportFlag, "")
        .strip()
    )
    if lineWithoutLinks != "":
        print(
            f"Skipping wikilink: {lineWithoutLinks} due to containing additional text"
        )
        return False

    linked_note_path = find_file_by_name(directory, linkedNoteName)

    if not linked_note_path:
        linked_note_path = os.path.join(directory, note_filename(linkedNoteName))
        if not os.path.exists(linked_note_path):
            print(f"Creating new note: {linked_note_path}")
            with open(linked_note_path, "w", encoding="utf-8") as f:
                f.write("#share\n")

    return True


def add_backlink(linkedNoteFileName, backlink, directory):
    linked_note_path = find_file_by_name(directory, linkedNoteFileName)
    if os.path.exists(linked_note_path):
        currentTextOfNote = open(linked_note_path, "r", encoding="utf-8").read()
        if backlink not in currentTextOfNote:
            print(f"Adding backlink: {backlink} to {linked_note_path}")
            with open(linked_note_path, "a", encoding="utf-8") as f:
                f.write(f"\n\n{backlink}")


def remove_wikilink(file_path, content, lineOfMatch):
    if content.endswith(lineOfMatch):
        content = content.replace(lineOfMatch, "")
    else:
        content = content.replace(lineOfMatch + "\n", "")
    print(f"Updating content in {file_path}:\n DELETED: {lineOfMatch}\n")
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(content)


def create_backlinks(directory):
    wikilink_pattern = re.compile(
        r"^(?P<lineOfMatch>.*\[\[(?P<filename>[^|\]]+)(?:\|(?P<linkText>[^]]+))?\]\].*)$",
        re.MULTILINE,
    )
    markdown_files = get_all_markdown_files(directory)
    for file_path in markdown_files:
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
        matches = wikilink_pattern.finditer(content)
        alreadyRemovedLines = []
        for match in matches:
            linkedNoteName = match.group("filename").strip()
            lineOfMatch = match.group("lineOfMatch")
            wikilinkText = match.group("filename")
            if match.group("linkText"):
                wikilinkText += "|" + match.group("linkText")
            wikilinkText = f"[[{wikilinkText}]]"
            linkedNoteFileName = note_filename(linkedNoteName)
            isLinkToTeleport = process_wikilink(
                lineOfMatch, linkedNoteFileName, wikilinkText, directory
            )

            if not isLinkToTeleport:
                continue

            backlink = f"[[{readable_note_title(file_path)}]]"
            add_backlink(linkedNoteFileName, backlink, directory)

            if lineOfMatch not in alreadyRemovedLines:
                remove_wikilink(file_path, content, lineOfMatch)


if __name__ == "__main__":
    config = getConfig()
    directory_to_scan = config.get("notesFolder")
    create_backlinks(directory_to_scan)
