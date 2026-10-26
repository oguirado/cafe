#!/usr/bin/env python3
"""PM block parser (V2-01). Pure functions, no cross-file caching."""
import json, re

BLOCK_RE = re.compile(r"<!-- PM:BEGIN (pm\.[a-z_]+\.v\d+) -->\s*```json\s*(.*?)```\s*<!-- PM:END \1 -->", re.S)
ANCHOR_RE = re.compile(r'<a id="([^"]+)"')


class BlockError(Exception):
    def __init__(self, code, path, detail=""):
        super().__init__("%s %s %s" % (code, path, detail))
        self.code, self.path = code, path


def _text(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def raw_from_text(text):
    return {m.group(1): m.group(2) for m in BLOCK_RE.finditer(text)}


def blocks_from_text(text, path="<text>"):
    out = {}
    for name, raw in raw_from_text(text).items():
        try:
            out[name] = json.loads(raw)
        except ValueError as e:
            raise BlockError("E_BLOCK_JSON", path, name + ": " + str(e))
    return out


def read_raw(path):
    """{schema: raw JSON text}, anchors set, in one read."""
    t = _text(path)
    return raw_from_text(t), set(ANCHOR_RE.findall(t))


def read_blocks(path):
    return blocks_from_text(_text(path), path)


def anchors(path):
    return set(ANCHOR_RE.findall(_text(path)))


def block_bytes(path, schema):
    """Raw JSON bytes of one block (for source hashing); None if absent."""
    for m in BLOCK_RE.finditer(_text(path)):
        if m.group(1) == schema:
            return m.group(2).encode("utf-8")
    return None
