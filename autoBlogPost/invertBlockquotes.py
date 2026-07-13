import json
from pathlib import Path

import pysnooper
import utils


def isArticleMostlyConvo(string):
    isMostlyConvo = False
    lines = string.split("\n")
    linesWithBlockquotes = [line for line in lines if line.startswith(">")]
    if len(linesWithBlockquotes) > len(lines) / 2:
        isMostlyConvo = True
    return isMostlyConvo


def getSingleConvoBlockquote(text):
    lines = text.split("\n")
    start_index = None
    end_index = None

    for i, line in enumerate(lines):
        if line.startswith(">"):
            if start_index is None:
                start_index = i
            end_index = i

    if start_index is not None and end_index is not None:
        return "\n".join(lines[start_index : end_index + 1]) + "\n"
    else:
        return ""


def findBlockQuoteConvos(md_string):
    blockquotes = []
    currentBlockquote = ""
    depths = []
    isMostlyConvo = isArticleMostlyConvo(md_string)
    if isMostlyConvo:
        return [
            getSingleConvoBlockquote(md_string)
        ]  # assume entire article is a single convo blockquote even if it contains top level replies with blockquotes
    for line in md_string.split("\n"):
        if line.startswith(">"):
            currentBlockquote += line + "\n"
            depth = len([i for i in line.strip() if i == ">"])
            depths.append(depth)
        else:
            linesInBlockquote = len(currentBlockquote.strip().split("\n"))
            if linesInBlockquote > 5 and len(set(depths)) >= 1:
                blockquotes.append(currentBlockquote)
                currentBlockquote = ""
    return blockquotes


def invertBlockquoteConvos(md_string):
    blockquoteConvos = findBlockQuoteConvos(md_string)
    for blockquoteConvo in blockquoteConvos:
        md_string = md_string.replace(
            blockquoteConvo, invertBlockquoteConvo(blockquoteConvo)
        )

    return md_string


def invertBlockquoteConvo(string):
    convo = convertOriginalToConvo(string)
    string = convertConvoToInverted(convo)
    return string


def convertConvoToInverted(convo):
    def addMsgsToOutput(parent, msgs, depth, outputLines):
        for i in range(len(msgs)):
            message = msgs[i]
            if message["parent"] == parent:
                msgText = ">" * depth + message["message"].replace(
                    "\n", "\n" + ">" * depth
                )
                outputLines.append(msgText)
                outputLines = addMsgsToOutput(
                    message["message"], msgs[i + 1 :], depth + 1, outputLines
                )
        return outputLines

    outputLines = addMsgsToOutput("", convo, 1, [])
    outputText = "\n".join(outputLines) + "\n\n"

    return outputText


def removeBlockquotes(string):
    stringLines = []
    for line in string.split("\n"):
        line = line.split(" ")
        strippedWord = ""
        endOfBlockquote = False
        for char in line[0]:
            if char != ">":
                endOfBlockquote = True
            if endOfBlockquote:
                strippedWord += char
        line[0] = strippedWord
        stringLines.append(" ".join(line))
    string = "\n".join(stringLines)
    return string


def convertOriginalToConvo(string):
    def getDepth(line):
        depth = 0
        for i in line.strip().split(" ")[0]:
            if i == ">":
                depth += 1
            else:
                break
        return depth

    convo = []
    message = []
    lines = string.split("\n")
    lastLineDepth = getDepth(lines[0])
    currentParentsAtDepths = {}
    for line in lines:
        lineDepth = getDepth(line)
        if lineDepth == lastLineDepth:
            message.append(line)
        else:
            message = removeBlockquotes("\n".join(message))
            for depth in list(currentParentsAtDepths):
                if depth <= lastLineDepth:
                    currentParentsAtDepths.pop(depth)

            lowestIndent = (
                min(currentParentsAtDepths.keys()) if currentParentsAtDepths else -1
            )
            parent = currentParentsAtDepths.get(lowestIndent, "")
            if "#draft" not in message and message:
                convo.append({"parent": parent, "message": message})
            currentParentsAtDepths[lastLineDepth] = message
            message = [line]

        lastLineDepth = lineDepth
    with open("convo.json", "w") as f:
        json.dump(convo, f, indent=4)
    return convo


if __name__ == "__main__":
    example_path = Path.home() / "notes/Work/ProdTools/example-blockquote-convo.md"
    md_string = example_path.read_text()
    print(invertBlockquoteConvos(md_string))
