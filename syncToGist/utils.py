import json
from os import path
import subprocess
import time
import sys


def getAbsPath(relPath):
    basepath = path.dirname(__file__)
    fullPath = path.abspath(path.join(basepath, relPath))
    return fullPath


def getConfig():
    return json.loads(open(getAbsPath("config.json")).read())


sys.path.append(getConfig()["gistWriteDir"])
from writeGist import writeContent


def writeGist(text, name, guid=None, gist_id=None):
    tmpFile = getAbsPath("tmp.txt")
    with open(tmpFile, "w") as f:
        f.write(text)
    gistUrl = "https://gist.github.com/" + gist_id if gist_id else None
    gistUrl = writeContent(gistUrl, guid, name, tmpFile)
    if "https://gist.github.com/" in gistUrl:
        return gistUrl.strip()
    else:
        return None


def convertMdUrlToHtmlUrl(mdUrl):
    # convert a markdown formatted url to an <a href> formatted url string
    name = mdUrl.split("](")[0][1:]
    url = mdUrl.split("](")[1][:-1]
    return f'<a href="{url}">{name}</a>'


def dict_to_markdown(data, indent_level=0):
    markdown = ""
    indent = " " * indent_level
    for key, value in data.items():
        if isinstance(value, str):  # Assuming the URL is a string
            markdown += f"{indent}- [{key}]({value})\n"
        elif isinstance(value, dict):
            markdown += f"{indent}- {key}\n"
            markdown += dict_to_markdown(value, indent_level + 1)
    return markdown


def markdown_list_to_html(markdown):
    markdown = markdown.strip("\n")
    html = ""
    lines = markdown.split("\n")
    current_indent = 0
    for i, line in enumerate(lines):
        nextLine = "-"
        if i + 1 < len(lines):
            nextLine = lines[i + 1]
        stripped_line = line.strip()
        nextLineIndent = nextLine.index("-")
        htmlUrl = stripped_line[2:]
        if nextLineIndent <= current_indent and "](" in stripped_line:
            htmlUrl = convertMdUrlToHtmlUrl(stripped_line[2:])
        if nextLineIndent > current_indent:
            html += "<details><summary>" + stripped_line[1:] + "</summary>\n<ul>\n"
        elif nextLineIndent == current_indent:
            html += "<li>" + htmlUrl + "</li>\n"
        elif nextLineIndent < current_indent:
            html += "<li>" + htmlUrl + "</li>\n"
            html += "</ul>\n</details>\n" * (current_indent - nextLineIndent)
        current_indent = nextLineIndent
    html += "</ul>\n"
    return html
