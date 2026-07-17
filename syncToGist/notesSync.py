import fcntl
import os
import re
import traceback
from contextlib import contextmanager
from pathlib import Path

import frontmatter

from teleportWikilinks import create_backlinks
from utils import delete_gist, getConfig, writeGist


SHARE_TOKEN_PATTERN = re.compile(r"(?<![\w-])#share(?![\w-])")
BLOCK_TOKEN_PATTERN = re.compile(r"(?<![\w-])#block(?![\w-])")


@contextmanager
def notes_repository_lock(directory):
    lock_path = Path(directory) / ".git/git_auto_commit.lock"
    with lock_path.open("a") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        yield


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


def _iter_top_level_markdown_paths(directory: str):
    for filename in os.listdir(directory):
        if filename.endswith(".md"):
            yield os.path.join(directory, filename)


def find_files_by_regex_on_content(root_folder, regexPattern):
    matching_files = []

    # Compile the regex pattern outside of the loop for efficiency
    compiled_pattern = re.compile(regexPattern)

    # Only scan top-level notes (do not touch subfolders under notesFolder).
    for filepath in _iter_top_level_markdown_paths(root_folder):
        with open(filepath, "r", encoding="utf-8", errors="ignore") as file:
            for line in file:
                if compiled_pattern.search(line):
                    matching_files.append(filepath)
                    break

    return matching_files


def has_share_token(content):
    return SHARE_TOKEN_PATTERN.search(content) is not None


def has_block_token(content):
    return BLOCK_TOKEN_PATTERN.search(content) is not None


def note_body_text(raw_content):
    if not raw_content.startswith("---"):
        return raw_content

    lines = raw_content.splitlines(keepends=True)
    for line_index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return "".join(lines[line_index + 1 :])
    return raw_content


def strip_share_token(content):
    lines = []
    for line in content.split("\n"):
        if not has_share_token(line):
            lines.append(line)
            continue
        stripped_line = SHARE_TOKEN_PATTERN.sub("", line)
        stripped_line = re.sub(r"[ \t]{2,}", " ", stripped_line).strip()
        if stripped_line:
            lines.append(stripped_line)
    return "\n".join(lines)


def load_note(file_path):
    return frontmatter.load(file_path)


def getAllIndexNotes(directory):
    file_info_dict = {}

    for file_path in _iter_top_level_markdown_paths(directory):
        file_name = os.path.basename(file_path)
        with open(file_path, "r", encoding="utf-8", errors="ignore") as file:
            raw_content = file.read()
        if has_block_token(raw_content) or not has_share_token(
            note_body_text(raw_content)
        ):
            continue
        post = load_note(file_path)
        print(file_name)
        gist_url = post.metadata.get("gist_url", None)
        file_info_dict[file_name] = {
            "file_path": file_path,
            "gist_link": gist_url,
            "text": strip_share_token(post.content),
        }
    return file_info_dict


def getAllNotesLinkedFromIndexNotes(indexNotes, directory):
    file_info_dict = {}
    wikilink_pattern = r"\[\[(?P<filename>[^|\]]+?)(?:\|(?P<linkText>[^]]+))?\]\]"

    top_level_md_by_stem_lower = {}
    for path in _iter_top_level_markdown_paths(directory):
        file_name = os.path.basename(path)
        top_level_md_by_stem_lower[note_slug(file_name)] = path

    for indexNoteName, file_info in indexNotes.items():
        text = file_info["text"]
        wikilinks = re.findall(wikilink_pattern, text)

        for wikilink in wikilinks:
            matching_file = top_level_md_by_stem_lower.get(note_slug(wikilink[0]))

            if matching_file:
                with open(
                    matching_file, "r", encoding="utf-8", errors="ignore"
                ) as file:
                    raw_content = file.read()
                if has_block_token(raw_content):
                    continue
                file_name = os.path.basename(matching_file)
                post = frontmatter.loads(raw_content)
                gist_url = post.metadata.get("gist_url", None)
                file_info_dict[file_name] = {
                    "file_path": matching_file,
                    "gist_link": gist_url,
                    "text": strip_share_token(post.content),
                }
            # else:
            # print(
            #     f"No matching file found for wikilink: [[{wikilink}]], {indexNoteName}"
            # )

    return file_info_dict


def delete_blocked_note_gists(directory):
    for file_path in _iter_top_level_markdown_paths(directory):
        with open(file_path, "r", encoding="utf-8", errors="ignore") as file:
            raw_content = file.read()
        if not has_block_token(raw_content):
            continue

        post = frontmatter.loads(raw_content)
        gist_url = post.metadata.get("gist_url")
        if gist_url:
            delete_gist(gist_url)

        metadata_changed = "gist_url" in post.metadata or "live" in post.metadata
        post.metadata.pop("gist_url", None)
        post.metadata.pop("live", None)
        if post.metadata and metadata_changed:
            frontmatter.dump(post, file_path)
        elif not post.metadata and (metadata_changed or raw_content.startswith("---")):
            Path(file_path).write_text(post.content, encoding="utf-8")


def setLiveFlag(allGistNotes, directory):
    gist_urls = {
        info["gist_link"] for info in allGistNotes.values() if info["gist_link"]
    }

    # Only update top-level notes (do not touch subfolders under notesFolder).
    for file_path in _iter_top_level_markdown_paths(directory):
        fileIsGist = open(file_path).read().strip().startswith("---")
        if not fileIsGist:
            continue
        post = frontmatter.load(file_path)
        gist_url = post.metadata.get("gist_url")
        new_live_value = gist_url in gist_urls
        if gist_url:
            if post.metadata.get("live") != new_live_value:
                post.metadata["live"] = new_live_value
                frontmatter.dump(post, file_path)
        else:
            if post.metadata.get("live", None) != None:
                del post.metadata["live"]
                frontmatter.dump(post, file_path)


def process_markdown_files(directory):
    delete_blocked_note_gists(directory)
    indexNotes = getAllIndexNotes(directory)
    notesLinkedFromIndexNotes = getAllNotesLinkedFromIndexNotes(indexNotes, directory)
    allGistNotes = indexNotes | notesLinkedFromIndexNotes
    for fileName in allGistNotes:
        try:
            allGistNotes = process_single_file(fileName, allGistNotes)
        except Exception as e:
            print(
                "\n\n\n\nexception message and details: ", e, "\n", e.args, "\n\n\n\n"
            )
            traceback.print_exc()

    setLiveFlag(allGistNotes, directory)


def wikilinksToGistLinks(md_string, allFileNames):
    # Regex pattern to match the markdown links
    pattern = r"\[\[(?P<filename>[^|\]]+)(?:\|(?P<linkText>[^]]+))?\]\]"
    notes_by_slug = {note_slug(file_name): info for file_name, info in allFileNames.items()}

    # Function to replace matched markdown links
    def replace_func(match):
        # Extract the filename and title from the matched object
        filename = match.group("filename").split("/")[-1]  # Only take the file name
        linkText = match.group("linkText") if match.group("linkText") else filename
        linkText = linkText.replace(".md", "")
        file_info = notes_by_slug.get(note_slug(filename))
        if not file_info or "gist_link" not in file_info:
            return linkText
        else:
            fileUrl = file_info["gist_link"]

        # Return the replacement link format
        replacementLink = f"[{linkText}]({fileUrl})"
        return replacementLink

    # Use re.sub to replace the markdown links
    return re.sub(pattern, replace_func, md_string)


def addGistLinkToFrontmatter(path, gistId):
    gistLink = "https://gist.github.com/" + gistId
    with open(path, "r") as f:
        content = f.read()
    fileObj = frontmatter.loads(content)
    fileObj.metadata["gist_url"] = gistLink
    frontmatter.dump(fileObj, path)


def process_single_file(fileName, gistFiles):
    file_info = gistFiles[fileName]
    fileText = file_info["text"]
    if not fileText:
        return gistFiles
    
    # Check if the file contains the SCRATCHPAD heading
    scratchpad_marker = "# -- SCRATCHPAD"
    
    # Split by the scratchpad marker and take only the content before it if it exists
    if scratchpad_marker in fileText:
        fileText = fileText.split(scratchpad_marker)[0].rstrip()
    else:
        # If the scratchpad marker doesn't exist, add it to the original file
        with open(file_info["file_path"], "a") as f:
            f.write(f"\n\n\n\n\n\n\n\n\n\n{scratchpad_marker}\n\n")
    
    fileText = wikilinksToGistLinks(fileText, gistFiles).replace("\n", "  \n")
    gistId = file_info["gist_link"].split("/")[-1] if file_info["gist_link"] else None
    # Create or update the gist
    new_gist_id = writeGist(fileText, fileName.replace(".md", ""), None, gistId).split(
        "/"
    )[-1]
    if file_info["gist_link"] is None:
        file_info["gist_link"] = "https://gist.github.com/" + new_gist_id
        addGistLinkToFrontmatter(file_info["file_path"], new_gist_id)
    return gistFiles


def main():
    config = getConfig()
    directory_to_scan = config.get("notesFolder")
    with notes_repository_lock(directory_to_scan):
        create_backlinks(directory_to_scan)
        process_markdown_files(directory_to_scan)


if __name__ == "__main__":
    main()
