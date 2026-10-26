#!/usr/bin/env python3
"""Stdlib JSON-Schema subset validator (draft 2020-12 keywords used by the v2 contracts)."""
import json, re


def _type_ok(v, t):
    if t == "null":
        return v is None
    if t == "boolean":
        return isinstance(v, bool)
    if t == "integer":
        return isinstance(v, int) and not isinstance(v, bool)
    if t == "number":
        return isinstance(v, (int, float)) and not isinstance(v, bool)
    if t == "string":
        return isinstance(v, str)
    if t == "array":
        return isinstance(v, list)
    if t == "object":
        return isinstance(v, dict)
    return False


def validate(v, s, path="$"):
    """Return a list of error strings (empty = valid)."""
    e = []
    if "const" in s and not (v == s["const"] and type(v) == type(s["const"])):
        e.append("%s: const" % path)
    if "enum" in s and not any(v == x and type(v) == type(x) for x in s["enum"]):
        e.append("%s: enum" % path)
    if "type" in s:
        ts = s["type"] if isinstance(s["type"], list) else [s["type"]]
        if not any(_type_ok(v, t) for t in ts):
            return e + ["%s: type %s" % (path, ts)]
    if isinstance(v, str):
        if "minLength" in s and len(v) < s["minLength"]:
            e.append("%s: minLength" % path)
        if "maxLength" in s and len(v) > s["maxLength"]:
            e.append("%s: maxLength" % path)
        if "pattern" in s and not re.search(s["pattern"], v):
            e.append("%s: pattern" % path)
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        if "minimum" in s and v < s["minimum"]:
            e.append("%s: minimum" % path)
        if "maximum" in s and v > s["maximum"]:
            e.append("%s: maximum" % path)
    if isinstance(v, list):
        if "minItems" in s and len(v) < s["minItems"]:
            e.append("%s: minItems" % path)
        if "maxItems" in s and len(v) > s["maxItems"]:
            e.append("%s: maxItems" % path)
        if s.get("uniqueItems") and len({json.dumps(x, sort_keys=True) for x in v}) != len(v):
            e.append("%s: uniqueItems" % path)
        pi = s.get("prefixItems", [])
        for i, x in enumerate(v):
            if i < len(pi):
                e += validate(x, pi[i], "%s[%d]" % (path, i))
            elif s.get("items") is False:
                e.append("%s[%d]: items false" % (path, i))
            elif isinstance(s.get("items"), dict):
                e += validate(x, s["items"], "%s[%d]" % (path, i))
    if isinstance(v, dict):
        for r in s.get("required", []):
            if r not in v:
                e.append("%s: required %s" % (path, r))
        props = s.get("properties", {})
        for k, x in v.items():
            if k in props:
                e += validate(x, props[k], "%s.%s" % (path, k))
            elif s.get("additionalProperties") is False:
                e.append("%s: additional %s" % (path, k))
            elif isinstance(s.get("additionalProperties"), dict):
                e += validate(x, s["additionalProperties"], "%s.%s" % (path, k))
    return e
