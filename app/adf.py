"""Minimal Atlassian Document Format (ADF) helpers.

Jira Cloud REST API v3 uses ADF for rich-text fields (description, comments).
These helpers convert plain text <-> ADF so the rest of the platform can stay text-based.
"""

from typing import Any


def text_to_adf(text: str | None) -> dict | None:
    if text is None:
        return None
    paragraphs = []
    for block in text.replace("\r\n", "\n").split("\n\n"):
        content: list[dict] = []
        for i, line in enumerate(block.split("\n")):
            if i > 0:
                content.append({"type": "hardBreak"})
            if line:
                content.append({"type": "text", "text": line})
        paragraphs.append({"type": "paragraph", "content": content})
    return {"type": "doc", "version": 1, "content": paragraphs}


_BLOCK_TYPES = {"paragraph", "heading", "blockquote", "codeBlock", "listItem", "panel", "rule", "tableRow"}


def adf_to_text(node: Any) -> str:
    if node is None:
        return ""
    if isinstance(node, str):  # some fields may already be plain text
        return node
    if isinstance(node, list):
        return "".join(adf_to_text(n) for n in node)

    node_type = node.get("type")
    if node_type == "text":
        return node.get("text", "")
    if node_type == "hardBreak":
        return "\n"
    if node_type == "mention":
        return node.get("attrs", {}).get("text", "")
    if node_type in ("emoji",):
        return node.get("attrs", {}).get("text", "") or node.get("attrs", {}).get("shortName", "")
    if node_type in ("inlineCard", "blockCard"):
        return node.get("attrs", {}).get("url", "")

    inner = adf_to_text(node.get("content", []))
    if node_type == "doc":
        return inner.strip("\n")
    if node_type == "listItem":
        return "- " + inner.strip("\n") + "\n"
    if node_type in _BLOCK_TYPES:
        return inner.rstrip("\n") + "\n\n"
    return inner
