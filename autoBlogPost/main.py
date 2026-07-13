import os
import frontmatter
from datetime import datetime, timedelta
import shutil
import utils
from pathlib import Path
import glob
import re
import shlex
import subprocess
import invertBlockquotes


def note_slug(value):
    stem = Path(value).name
    if stem.lower().endswith(".md"):
        stem = stem[:-3]

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


# function to check if a file has valid front matter
def has_valid_frontmatter(file_path):
    try:
        blogFM = frontmatter.load(file_path)
        if "headerImage" in blogFM:
            return True
        else:
            return False
    except:
        return False


def generateTitle(file_path):
    title = Path(file_path).stem.strip()
    title = re.sub(r"[-_]+", " ", title)
    title = title[0].upper() + title[1:] if title else ""

    return title


# function to add front matter to a file
def add_frontmatter(file_path, date=None, description="", articleUrl="", isHidden=True):
    filename = file_path.split("/")[-1]
    # get the current date
    yesterday = datetime.now() - timedelta(1)
    date = yesterday.strftime("%Y-%m-%d") if date is None else date
    # get the title from the file name

    postObject = frontmatter.load(file_path)
    hashtags = remove_hashtags(postObject.content)[1]

    title = generateTitle(file_path)

    if articleUrl == "":
        articleUrl = (
            utils.getConfig()["blogUrl"] + "/" + title.replace(" ", "-").lower()
        )
    category = "blog" if not isHidden else ""
    frontMatterObject = {
        "title": title,
        "layout": "post",
        "date": date + " 00:00",
        "headerImage": False,
        "category": category,
        "author": utils.getConfig()["author"],
        "description": description,
        "articleUrl": articleUrl,
        "tag": hashtags,
    }

    for key, value in frontMatterObject.items():
        postObject[key] = value

    # save file
    outputText = frontmatter.dumps(postObject)
    currentText = open(file_path).read()
    if currentText == outputText:
        return
    frontmatter.dump(postObject, file_path)


def addNewLinesBeforeBlockQuoteReply(md_string):
    # necessary to this for the reply to be displayed as a separate blockquote
    modified_md_string = []
    indentOfLastLine = 0
    for line in md_string.split("\n"):
        try:
            indent = line.strip().split(" ")[0].count(">")
        except:
            indent = 0
        if line.strip().startswith(">"):
            lineNotBlank = line.strip().replace(">", "").strip() != ""
            if indent < indentOfLastLine and lineNotBlank:
                modified_md_string.append(">" * indent)
        modified_md_string.append(line)
        indentOfLastLine = indent

    return "\n".join(modified_md_string)


def handle_scratchpad(source_file_path):
    """Add SCRATCHPAD heading to source file if it doesn't exist"""
    scratchpad_marker = "# -- SCRATCHPAD"
    
    with open(source_file_path, 'r', encoding='utf-8', errors='ignore') as file:
        content = file.read()
    
    if scratchpad_marker not in content:
        # Add the marker to the end of the file
        with open(source_file_path, 'a', encoding='utf-8') as file:
            file.write(f"\n\n{scratchpad_marker}\n")

def remove_scratchpad_content(content):
    """Remove content after the SCRATCHPAD marker"""
    scratchpad_marker = "# -- SCRATCHPAD"
    
    if scratchpad_marker in content:
        # Split at the marker and only keep the content before it
        parts = content.split(scratchpad_marker, 1)
        return parts[0].rstrip()
    
    return content


def uses_explicit_tex_syntax(math_body):
    return any(marker in math_body for marker in ("\\", "{", "}", "_", "^"))


def convert_tex_dollar_delimiters(md_string):
    display_dollar_span = re.compile(r"(?<!\\)\$\$(.+?)(?<!\\)\$\$", re.DOTALL)
    inline_dollar_span = re.compile(r"(?<!\\)\$(?!\$)([^$\n]+?)(?<!\\)\$(?!\$)")

    md_string = display_dollar_span.sub(
        lambda match: rf"\\[{match.group(1)}\\]",
        md_string,
    )

    def replace_explicit_tex(match):
        math_body = match.group(1)
        if not uses_explicit_tex_syntax(math_body):
            return match.group(0)
        return rf"\\({math_body}\\)"

    return inline_dollar_span.sub(replace_explicit_tex, md_string)


def formatPostContents(file_path, allFileNames):
    post = frontmatter.load(file_path)
    content = post.content
    
    # Remove any scratchpad content
    content = remove_scratchpad_content(content)
    
    content = remove_link_only_lines(content)
    content = convert_md_links(content, allFileNames)
    content += "\n\n" + contactInfo
    content = content.replace(postPostfix, "")
    content = content.replace(hiddenPostPostfix, "")
    content = remove_hashtags(content)[0]
    content = convert_tex_dollar_delimiters(content)
    contentWithDoubleSpaces = ""
    for line in content.split("\n"):
        contentWithDoubleSpaces += line
        if not line.endswith("  ") and line.strip() != "":
            contentWithDoubleSpaces += "  "  # markdown requires two spaces after a line for it to be recognised as such
        contentWithDoubleSpaces += "\n"
    content = str(contentWithDoubleSpaces)
    content = invertBlockquotes.invertBlockquoteConvos(content)
    content = addNewLinesBeforeBlockQuoteReply(content)
    post.content = content
    frontmatter.dump(post, file_path)


def convert_md_links(md_string, allFileNames):
    # Regex pattern to match the markdown links
    pattern = r"\[\[(?P<filename>[^|\]]+)(?:\|(?P<linkText>[^]]+))?\]\]"
    note_keys = {note_slug(file_name) for file_name in allFileNames}

    # Function to replace matched markdown links
    def replace_func(match):
        # Extract the filename and title from the matched object
        filename = match.group("filename").split("/")[-1]  # Only take the file name
        linkText = match.group("linkText") if match.group("linkText") else filename

        linkText = linkText.replace(".md", "")
        target_slug = note_slug(filename)
        if target_slug not in note_keys:
            return linkText

        filename = "/" + target_slug
        # Return the replacement link format
        return f"[{linkText}]({filename})"

    # Use re.sub to replace the markdown links
    return re.sub(pattern, replace_func, md_string)


def remove_link_only_lines(md_string):
    # Regex pattern to match lines with only markdown links and whitespaces
    pattern = r"^\s*(\[\[[^|\]]+\|?[^\]]*\]\]\s*)+$"

    # Filter out lines that match the pattern
    lines = md_string.split("\n")
    filtered_lines = [
        line
        for line in lines
        if not re.match(
            pattern, line.replace(",", "").replace(".", "").replace("+", "")
        )
    ]

    # Join and return the filtered lines
    return "\n".join(filtered_lines)


def find_files_containing_string(root_folder, target_string):
    matching_files = []

    for filename in os.listdir(root_folder):
        if not filename.endswith(".md"):
            continue
        filepath = os.path.join(root_folder, filename)
        if not os.path.isfile(filepath):
            continue
        with open(filepath, "r", encoding="utf-8", errors="ignore") as file:
            for line in file:
                if target_string in line:
                    matching_files.append(filepath)
                    break

    return matching_files


def remove_hashtags(md_string):
    # Regular expression pattern to detect #hashtags
    hashtag_pattern = r"\s(#[\w-]+)"

    md_string = "\n" + md_string
    # Find all hashtags in the markdown string
    hashtags = re.findall(hashtag_pattern, md_string)

    # Remove all found hashtags from the markdown string
    cleaned_md = re.sub(hashtag_pattern, "", md_string)

    hashtags = [
        tag.strip("#")
        for tag in hashtags
        if tag != postPostfix and tag != hiddenPostPostfix
    ]

    return cleaned_md, hashtags


def generateBlogPostFileName(title, date):
    fileName = date + "-" + note_slug(title) + ".md"

    return fileName


def run_git_auto_commit(git_auto_path, git_folder):
    subprocess.run(shlex.split(git_auto_path) + [git_folder], check=True)


def main():
    files = glob.glob(blog_folder + "/*")
    # find all files in the notes folder
    publishedFilePaths = find_files_containing_string(notes_folder, postPostfix)
    hiddenFilePaths = find_files_containing_string(notes_folder, hiddenPostPostfix)
    allPaths = publishedFilePaths + hiddenFilePaths
    allFileNames = [str(file_path).split("/")[-1] for file_path in allPaths]

    updatedBlogPostFiles = []
    for file_path in allPaths:
        file_path = str(file_path)
        isHidden = file_path in hiddenFilePaths
        filename = file_path.split("/")[-1]
        if has_valid_frontmatter(file_path):
            # extract the date from the front matter
            post = frontmatter.load(file_path)
            description = post["description"] if "description" in post else ""
            articleUrl = post["articleUrl"] if "articleUrl" in post else ""
            filename = articleUrl.split("/")[-1] if "articleUrl" in post else filename
            date = (
                datetime.strptime(post["date"], "%Y-%m-%d  %H:%M")
                .date()
                .strftime("%Y-%m-%d")
            )
            add_frontmatter(
                file_path,
                date=date,
                description=description,
                articleUrl=articleUrl,
                isHidden=isHidden,
            )
        else:
            print("not valid frontmatter")
            # add front matter to the file
            add_frontmatter(file_path, isHidden=isHidden)
            yesterday = datetime.now() - timedelta(1)
            date = yesterday.strftime("%Y-%m-%d")
        # copy the file to the blog folder with the new name

        blogPostFileName = generateBlogPostFileName(filename, date)
        new_file_path = os.path.join(blog_folder, blogPostFileName)
        updatedBlogPostFiles.append(new_file_path)
        print("copied", file_path, "\nto", new_file_path, "\n\n")
        shutil.copy(file_path, new_file_path)
        # Check and add the SCRATCHPAD marker to the source file if it doesn't exist
        handle_scratchpad(file_path)
        
        # Format the content for the blog post (with SCRATCHPAD content removed)
        formatPostContents(new_file_path, allFileNames)

    for f in files:
        if f not in updatedBlogPostFiles:
            print("removing", f)
            os.remove(f)


if __name__ == "__main__":
    # path of the folder containing the notes
    notes_folder = utils.getConfig()["notesFolderPath"]
    # path of the folder containing the blog posts
    blog_folder = utils.getConfig()["blogFolderPath"]
    postPostfix = utils.getConfig()["blogPostIdentifierPostfix"]
    hiddenPostPostfix = utils.getConfig()["hiddenPostPostfix"]
    contactInfo = utils.getConfig()["contactInfo"]
    gitAutoPath = utils.getConfig()["gitAutoPath"]
    main()
    gitFolder = "/".join(blog_folder.split("/")[:-1])
    run_git_auto_commit(gitAutoPath, gitFolder)
