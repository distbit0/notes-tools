import os
import json
from utils import *


# Recursive function to find bookmark folders with "SHARE" bookmark
def find_folders(bookmarks, share_folders):
    # Iterate through the children of the current bookmark object
    for child in bookmarks["children"]:
        # If the child is a folder, check if it has a bookmark called "SHARE"
        if child["type"] == "folder":
            # Check if the folder has a bookmark called "SHARE"
            has_share = any(
                bookmark["name"] == "SHARE" for bookmark in child["children"]
            )
            if has_share:
                # If the folder has a "SHARE" bookmark, append it to the share_folders list
                share_folders.append(child)
            # Recursively search the children of the current folder for more folders with "SHARE" bookmark
            find_folders(child, share_folders)


def bookmarks_to_dict(children):
    def process_node(node):
        if isinstance(node, dict):  # Check if the input is a dictionary (a single node)
            if node["type"] == "folder":
                folder_contents = {}
                for child in node["children"]:
                    folder_contents.update(process_node(child))
                return {node["name"]: folder_contents}
            elif node["type"] == "url":
                if node["name"].strip():
                    return {node["name"]: node["url"]}
                else:
                    return {node["url"]: node["url"]}
        elif isinstance(node, list):  # Check if the input is a list of nodes
            contents = {}
            for child in node:
                childContent = process_node(child)
                if list(childContent.keys()) != ["SHARE"]:
                    contents.update(childContent)
            return contents

    return process_node(children)


def updateAllSHAREGists(bookmarks_file):
    gists = {}
    with open(bookmarks_file, "r") as f:
        bookmarks = json.load(f)

    # Find all bookmark folders with "SHARE" bookmark
    share_folders = []
    find_folders(bookmarks["roots"]["bookmark_bar"], share_folders)

    # Iterate through the share folders
    for folder in share_folders:
        gistFileName = folder["name"]
        # Find the "SHARE" bookmark in the folder
        share_bookmark = next(
            bookmark for bookmark in folder["children"] if bookmark["name"] == "SHARE"
        )
        bookmarksAsPlainDict = bookmarks_to_dict(folder["children"])
        mdAsText = dict_to_markdown(bookmarksAsPlainDict)
        htmlFromMd = markdown_list_to_html(mdAsText)
        bookmarkGuid = share_bookmark["guid"]
        gistUrl = writeGist(htmlFromMd, gistFileName, bookmarkGuid)
        gists[gistFileName] = gistUrl

    return gists


if __name__ == "__main__":
    # Read the bookmarks file
    ##concatonate home folder with bookmarks file path
    bookmarks_file = os.path.expanduser(getConfig()["bookmarksFilePath"])
    gists = updateAllSHAREGists(bookmarks_file)
    linksToGistsMd = dict_to_markdown(gists)
    linksToGistsHtml = markdown_list_to_html(linksToGistsMd)
    gistUrl = writeGist(linksToGistsHtml, "Bookmark Gists", "TAGlistOfGists")
    print(gistUrl)
