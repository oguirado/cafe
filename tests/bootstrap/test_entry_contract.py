"""Portable entry and backend browse contracts; runtime resolver remains R8."""
import hashlib
from collections import Counter
from html.parser import HTMLParser
from urllib.parse import unquote, urlsplit
import importlib.util
import json
import os
from pathlib import Path
import posixpath
import re
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROOT_REAL = os.path.realpath(ROOT)

TIKTOKEN_VERSION = "0.13.0"
TIKTOKEN_ENCODING = "o200k_base"
VOCABULARY_FINGERPRINT_SHA256 = "1c0eeb095c06be7701778b7fcdaec4c868c4d2859bfae0f1cecc0f14ffd48a1a"

ENTRY_TOKEN_CAPS = {
    "llms.txt": 150,
    "catalogue/protocol.json": 600,
    "AGENTS.md": 1500,
    "README.md": 1000,
}
ENTRY_FILES = ["llms.txt", "catalogue/protocol.json", "AGENTS.md", "README.md"]

PROTOCOL_PATH = "catalogue/protocol.json"
ROUTES_PATH = "catalogue/routes.tsv"
SCHEMA_PATH = os.path.join("router", "standards", "schemas", "ctx.protocol.v2.schema.json")
VALIDATOR_PATH = os.path.join("router", "tools", "jsonschema_lite.py")

PATH_RULE = "lookup:catalogue/routes.tsv"
EXPECTED_PARAMS = "catalogue/router/params.json"
R2_CORPUS = "cafe"
CORPUS_PATTERN = r"^[a-z][a-z0-9_]{0,15}$"
ID_PATTERN = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"
# Reviewed bound (ROUTER_BOOTSTRAP section 4): len(corpus)+1+len(id) <= 32
# UTF-8 bytes with a colon separator; never clip. It also rejects overlong IDs.
MAX_NAMESPACED_BYTES = 32



SUBJECTS_PATH = "catalogue/subjects.json"
BACKEND_INDEX = "subjects/backend/index.json"
BACKEND_PAGE = "subjects/backend/page.md"
BACKEND_ID = "backend-book"
CARD_TOKEN_CAP = 4000
BACKEND_ANCHORS = ["backend-overview", "backend-boundaries", "backend-journal",
                   "backend-oracle", "backend-negative"]
FUTURE_IDENTITIES = ["Rust authority", "Python connectors + MCP", "JS shell",
                     "recorder", "OBS adapter", "Twitch data layer"]
LINK_RE = re.compile(r"(?<!!)\[[^\]\n]*\]\(([^\s)]+)\)")
ANCHOR_PATTERN = r"[a-z][a-z0-9-]*"


class _Anchors(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.ids = []

    def handle_starttag(self, tag, attrs):
        # Count every HTML id, including repeated attributes and other tags:
        # a duplicate heading id must not shadow a public <a id> target.
        for key, value in attrs:
            if key == "id":
                self.ids.append((tag, value))

    handle_startendtag = handle_starttag



def _navigation_html(text):
    # Fenced examples do not create navigable HTML anchors.
    lines = []
    fence = None
    for line in text.splitlines():
        opener = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if fence is None and opener:
            fence = opener.group(1)
            continue
        if fence is not None:
            if re.fullmatch(r" {0,3}" + re.escape(fence[0]) +
                            "{" + str(len(fence)) + r",}\s*", line):
                fence = None
            continue
        lines.append(line)
    return "\n".join(lines)


def _public_file(test, rel):
    test.assertTrue(_is_safe_relative(rel), "unsafe browse target: %r" % rel)
    test.assertTrue(rel.startswith(("catalogue/", "subjects/", "docs/")) or
                    rel in ENTRY_FILES, "browse target outside governed public files: %r" % rel)
    full = _root_path(*rel.split("/"))
    test.assertTrue(os.path.isfile(full), "missing browse target: %s" % rel)
    # A navigation target is a repository file, never a symlink into the host.
    test.assertEqual(os.path.realpath(full), os.path.abspath(full),
                     "symlink in browse target: %s" % rel)
    test.assertTrue(os.path.realpath(full).startswith(ROOT_REAL + os.sep),
                    "browse target escapes repository: %s" % rel)
    return full


def _browse_page(test):
    _public_file(test, "llms.txt")
    llms = _read_text(test, "llms.txt")
    test.assertIn(PROTOCOL_PATH, LINK_RE.findall(llms), "llms lacks protocol link")
    _public_file(test, PROTOCOL_PATH)
    # ctx.protocol.v2 has no subjects field. The local, explicit sibling
    # convention is carried in llms rather than inventing a schema property.
    convention = "catalogue/protocol.json -> catalogue/subjects.json"
    test.assertIn(convention, llms, "missing protocol-to-subjects sibling convention")
    subjects_path = posixpath.join(posixpath.dirname(PROTOCOL_PATH), "subjects.json")
    test.assertEqual(subjects_path, SUBJECTS_PATH)
    test.assertIn(subjects_path, LINK_RE.findall(llms), "llms lacks subject catalogue link")
    _public_file(test, subjects_path)
    subjects = json.loads(_read_text(test, subjects_path))
    test.assertEqual(subjects, {"subjects": [{"id": "backend", "index": BACKEND_INDEX}]},
                     "backend subject contract mismatch")
    index_path = subjects["subjects"][0]["index"]
    _public_file(test, index_path)
    index = json.loads(_read_text(test, index_path))
    test.assertIsInstance(index, dict, "subject index must be an object")
    test.assertEqual(set(index), {"subject", "pages"}, "unknown/missing index fields")
    test.assertEqual(index["subject"], "backend", "wrong subject identity")
    test.assertIsInstance(index["pages"], list, "pages must be an array")
    test.assertEqual(len(index["pages"]), 1, "backend must have one page")
    page = index["pages"][0]
    test.assertIsInstance(page, dict, "page must be an object")
    test.assertEqual(set(page), {"id", "path", "anchors", "token_limit"},
                     "unknown/missing page fields")
    test.assertEqual(page["id"], BACKEND_ID, "unexpected page id")
    _public_file(test, page["path"])
    test.assertEqual(page["path"], BACKEND_PAGE, "unexpected page path")
    test.assertIs(type(page["token_limit"]), int, "token limit must be integer")
    test.assertEqual(page["token_limit"], CARD_TOKEN_CAP, "card cap may not be raised")
    test.assertEqual(page["anchors"], BACKEND_ANCHORS, "advertised anchors changed")
    test.assertEqual(len(page["anchors"]), len(set(page["anchors"])), "duplicate advertised anchors")
    test.assertLessEqual(len(page["anchors"]), 16, "too many advertised anchors")
    for anchor in page["anchors"]:
        test.assertIsNotNone(re.fullmatch(ANCHOR_PATTERN, anchor), "invalid anchor id")
        test.assertLessEqual(len(anchor.encode("utf-8")), 64, "anchor exceeds UTF-8 bound")
    _public_file(test, page["path"])
    return page


class _CacheMiss(Exception):
    pass


def _root_path(*parts):
    return os.path.join(ROOT, *parts)


def _read_text(test, rel):
    path = _root_path(*rel.split("/"))
    test.assertTrue(os.path.isfile(path), "missing entry input: %s" % rel)
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _load_validator(test):
    path = _root_path(VALIDATOR_PATH)
    test.assertTrue(os.path.isfile(path), "missing vendored validator: %s" % VALIDATOR_PATH)
    spec = importlib.util.spec_from_file_location("r2_jsonschema_lite", path)
    test.assertIsNotNone(spec, "cannot load validator spec: %s" % VALIDATOR_PATH)
    test.assertIsNotNone(spec.loader, "validator has no loader: %s" % VALIDATOR_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _offline_encoding(test):
    cache = Path(_root_path('tools','assets','tokenizer'))
    asset = cache / 'fb374d419588a4632f3f557e76b4b70aebbca790'
    test.assertTrue(asset.is_file(), 'published tokenizer vocabulary absent')
    test.assertEqual(hashlib.sha256(asset.read_bytes()).hexdigest(), '446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d', 'published tokenizer vocabulary drift')
    try:
        import tiktoken
        import tiktoken.load
    except ImportError as exc:
        test.fail("tiktoken unavailable: %s" % exc)
    with mock.patch.dict(os.environ, {"TIKTOKEN_CACHE_DIR": str(cache)}), mock.patch.object(tiktoken.load, "read_file", side_effect=_CacheMiss("offline")):
        try:
            return tiktoken.get_encoding(TIKTOKEN_ENCODING)
        except _CacheMiss:
            test.fail("missing local tiktoken cache for %s; network fetch disabled" % TIKTOKEN_ENCODING)


def _vocabulary_fingerprint(test, enc):
    ranks = getattr(enc, "_mergeable_ranks", None)
    test.assertIsNotNone(ranks, "encoding exposes no _mergeable_ranks")
    special = getattr(enc, "_special_tokens", None)
    test.assertIsNotNone(special, "encoding exposes no _special_tokens")
    digest = hashlib.sha256()
    for key, rank in sorted(ranks.items()):
        digest.update(len(key).to_bytes(4, "big"))
        digest.update(key)
        digest.update(rank.to_bytes(4, "big"))
    for name, token in sorted(special.items()):
        digest.update(name.encode("utf-8"))
        digest.update(token.to_bytes(4, "big"))
    return digest.hexdigest()


def _stem_id(filename):
    if filename.endswith(".md"):
        stem = filename[: -len(".md")]
    else:
        stem = os.path.splitext(filename)[0]
    return re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-")


def _is_safe_relative(path):
    if not isinstance(path, str) or not path:
        return False
    if "\\" in path:
        return False
    if path.startswith("/") or re.match(r"^[A-Za-z]:", path):
        return False
    if posixpath.normpath(path) != path:
        return False
    parts = path.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return False
    return True


# Closed volatility scanner for the final R2 list. One helper reused by the
# entry scan and by the persistent regression tests below.
_IDENTITY_EXACT = "247666607+oguirado@users.noreply.github.com"
_IDENTITY_TOKEN_RE = re.compile(
    r"(?<![^\W_])" + re.escape(_IDENTITY_EXACT) + r"(?![^\W_])")
_ISO_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)?(?!\d)")
_ISO_TIME_RE = re.compile(r"\b\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?(?!\d)")
_PID_RE = re.compile(r"\bpid\b(?:\s*[:=]\s*|\s+)\d+", re.IGNORECASE)
# POSIX home roots are joined at runtime so no absolute example appears here.
_POSIX_ROOT_A = "/" + "hom" + "e" + "/"
_POSIX_ROOT_B = "/" + "User" + "s" + "/"
_POSIX_HOME_RE = re.compile(r"(?<![\w])(?:" + re.escape(_POSIX_ROOT_A) + r"[^\s]*|" + re.escape(_POSIX_ROOT_B) + r"[^\s]*)")
_WINDOWS_DRIVE_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/][^\s]*")
_UNC_RE = re.compile(r"(?<![A-Za-z0-9])\\\\[^\\\s]+[\\/][^\s]*")
_HEX_FULL_RE = re.compile(r"[0-9A-Fa-f]+")
_HEX_DIGIT_RE = re.compile(r"[0-9]")
_TOKEN_RE = re.compile(r"[^\W_]+")


def scan_volatility(text):
    findings = []
    for match in _ISO_DATE_RE.finditer(text):
        findings.append(("iso-date", match.group(0)))
    for match in _ISO_TIME_RE.finditer(text):
        findings.append(("iso-time", match.group(0)))
    for match in _PID_RE.finditer(text):
        findings.append(("labeled-pid", match.group(0)))
    for match in _POSIX_HOME_RE.finditer(text):
        findings.append(("posix-home", match.group(0)))
    for match in _WINDOWS_DRIVE_RE.finditer(text):
        findings.append(("windows-path", match.group(0)))
    for match in _UNC_RE.finditer(text):
        findings.append(("unc-path", match.group(0)))
    masked = _IDENTITY_TOKEN_RE.sub(lambda match: " " * len(match.group(0)), text)
    for token in _TOKEN_RE.findall(masked):
        if len(token) >= 7 and _HEX_FULL_RE.fullmatch(token) and _HEX_DIGIT_RE.search(token):
            findings.append(("hex", token))
    return findings


class TestEntryContract(unittest.TestCase):
    def test_tokenizer_pin(self):
        try:
            import tiktoken
        except ImportError as exc:
            self.fail("tiktoken unavailable: %s" % exc)
        self.assertEqual(tiktoken.__version__, TIKTOKEN_VERSION,
                         "tiktoken version drift: %s" % tiktoken.__version__)
        enc = _offline_encoding(self)
        observed = _vocabulary_fingerprint(self, enc)
        self.assertEqual(observed, VOCABULARY_FINGERPRINT_SHA256,
                         "o200k_base vocabulary fingerprint drift")

    def test_entry_token_caps(self):
        enc = _offline_encoding(self)
        for rel, cap in ENTRY_TOKEN_CAPS.items():
            with self.subTest(entry=rel):
                text = _read_text(self, rel)
                count = len(enc.encode(text))
                self.assertLessEqual(count, cap, "%s uses %d o200k_base tokens, cap %d" % (rel, count, cap))

    def test_protocol_schema(self):
        schema = json.loads(_read_text(self, SCHEMA_PATH))
        protocol = json.loads(_read_text(self, PROTOCOL_PATH))
        js = _load_validator(self)
        errors = js.validate(protocol, schema)
        self.assertEqual(errors, [], "protocol violates its schema: %s" % (errors,))
        unknown = dict(protocol)
        unknown["unexpected"] = True
        self.assertTrue(js.validate(unknown, schema), "unknown top-level protocol property accepted")
        nested = json.loads(json.dumps(protocol))
        nested["router"]["unexpected"] = True
        self.assertTrue(js.validate(nested, schema), "unknown router property accepted")

    def test_mount_priorities_and_single_corpus(self):
        protocol = json.loads(_read_text(self, PROTOCOL_PATH))
        self.assertIn("mounts", protocol, "protocol declares no mounts")
        mounts = protocol["mounts"]
        self.assertIsInstance(mounts, list, "mounts is not an array")
        priorities = [mount["priority"] for mount in mounts]
        self.assertEqual(len(priorities), len(set(priorities)),
                         "duplicate mount priorities: %s" % (priorities,))
        corpora = [mount["corpus"] for mount in mounts]
        self.assertEqual(len(corpora), len(set(corpora)), "duplicate mount corpora: %s" % (corpora,))
        self.assertEqual(len(mounts), 1, "R2 requires exactly one mount, found %d" % len(mounts))
        self.assertEqual(corpora, [R2_CORPUS],
                         "R2 requires the single cafe corpus, found %s" % (corpora,))
        self.assertIn("router", protocol, "protocol declares no router")
        self.assertEqual(protocol["router"]["path_rule"], PATH_RULE,
                         "path rule must be exactly %s" % PATH_RULE)
        self.assertEqual(protocol["router"]["params"], EXPECTED_PARAMS,
                         "params target must be exactly %s" % EXPECTED_PARAMS)
        for rel in [protocol["router"]["params"]] + [mount["path"] for mount in mounts]:
            self.assertTrue(_is_safe_relative(rel), "unsafe metadata path: %r" % (rel,))

    def test_routes_cover_docs(self):
        docs_root = _root_path("docs")
        self.assertTrue(os.path.isdir(docs_root), "missing docs/ directory")
        census = {}
        for dirpath, _dirnames, filenames in os.walk(docs_root):
            for filename in sorted(filenames):
                if not filename.endswith(".md"):
                    continue
                rel = os.path.relpath(os.path.join(dirpath, filename), ROOT).replace(os.sep, "/")
                doc_id = _stem_id(filename)
                self.assertTrue(doc_id, "empty inventory id for %s" % rel)
                self.assertNotIn(doc_id, census,
                                 "inventory id collision: %s (%s and %s)" % (doc_id, census.get(doc_id), rel))
                census[doc_id] = rel
        self.assertTrue(census, "docs-only census is empty")
        rows = [line for line in _read_text(self, ROUTES_PATH).splitlines() if line != ""]
        self.assertTrue(rows, "routes file is empty")
        self.assertEqual(rows[0], "id\tpath", "routes header must be exactly id<TAB>path")
        lookup = {}
        for row in rows[1:]:
            fields = row.split("\t")
            self.assertEqual(len(fields), 2, "malformed route row: %r" % (row,))
            lookup[fields[0]] = fields[1]
        page = _browse_page(self)
        self.assertNotIn(page["id"], census, "page id collides with document id")
        census[page["id"]] = page["path"]
        for ident, path in census.items():
            self.assertEqual(lookup.get(ident), path, "document primary id missing from generic lookup")
        nodes = [json.loads(line) for line in _read_text(self, "catalogue/nodes.jsonl").splitlines()]
        self.assertEqual(set(lookup), {node["id"] for node in nodes}, "lookup must cover every node and shard")

    def test_routes_are_unique_safe_existing(self):
        self.assertRegex(R2_CORPUS, CORPUS_PATTERN, "R2 corpus violates the namespace pattern")
        raw = _read_text(self, ROUTES_PATH)
        self.assertTrue(raw.endswith("\n"), "routes file lacks a terminal newline")
        self.assertNotIn("\r", raw, "routes file contains carriage returns")
        lines = raw.split("\n")
        self.assertEqual(lines[-1], "", "routes file lacks a terminal newline")
        body = lines[:-1]
        self.assertTrue(body, "routes file is empty")
        self.assertEqual(body[0], "id\tpath", "routes header must be exactly id<TAB>path")
        rows = body[1:]
        self.assertTrue(rows, "routes file has no rows")
        ids = []
        paths = []
        for row in rows:
            fields = row.split("\t")
            self.assertEqual(len(fields), 2, "malformed route row (want id<TAB>path): %r" % (row,))
            doc_id, rel = fields
            self.assertTrue(doc_id, "empty route id in row: %r" % (row,))
            self.assertTrue(rel, "empty route path in row: %r" % (rel,))
            self.assertRegex(doc_id, ID_PATTERN, "route id outside stem-rule shape: %r" % (doc_id,))
            namespaced = R2_CORPUS + ":" + doc_id
            self.assertLessEqual(len(namespaced.encode("utf-8")), MAX_NAMESPACED_BYTES,
                                 "namespaced route exceeds %d bytes: %r" % (MAX_NAMESPACED_BYTES, namespaced))
            self.assertNotIn("\\", rel, "route path contains a backslash: %r" % (rel,))
            self.assertFalse(rel.startswith("/") or re.match(r"^[A-Za-z]:", rel),
                             "absolute route path: %r" % (rel,))
            self.assertEqual(posixpath.normpath(rel), rel, "unnormalized route path: %r" % (rel,))
            self.assertTrue(rel.startswith(("docs/", "subjects/", "core/", "src/", "connectors/")),
                            "route path outside declared source profile: %r" % (rel,))
            full = _root_path(*rel.split("/"))
            self.assertTrue(os.path.isfile(full), "route path is not an existing file: %r" % (rel,))
            resolved = os.path.realpath(full)
            self.assertTrue(resolved == ROOT_REAL or resolved.startswith(ROOT_REAL + os.sep),
                            "route path escapes the repository: %r" % (rel,))
            ids.append(doc_id)
            paths.append(rel)
        self.assertEqual(len(ids), len(set(ids)), "duplicate route ids: %s" % (ids,))
        coordinates = [json.loads(line) for line in _read_text(self, "catalogue/coordinates.jsonl").splitlines()]
        mapping = {row["id"]: row["path"] for row in coordinates}
        self.assertEqual(dict(zip(ids, paths)), mapping, "each node must consume exactly one lookup path")
        nodes = [json.loads(line) for line in _read_text(self, "catalogue/nodes.jsonl").splitlines()]
        grouped = {}
        for node in nodes:
            grouped.setdefault(mapping[node["id"]], set()).add(node.get("family", node["id"]))
        self.assertTrue(all(len(families) == 1 for families in grouped.values()),
                        "shared source paths must be shards of one declared family")


    def test_backend_browse_hops(self):
        _browse_page(self)

    def test_backend_anchors_and_links(self):
        page = _browse_page(self)
        text = _read_text(self, page["path"])
        parser = _Anchors()
        html = _navigation_html(text)
        parser.feed(html)
        parser.close()
        counts = Counter(value for _tag, value in parser.ids)
        self.assertTrue(all(n == 1 for n in counts.values()), "duplicate HTML anchors: %s" % counts)
        self.assertEqual(set(counts), set(page["anchors"]), "missing or unadvertised anchors")
        for anchor in page["anchors"]:
            self.assertEqual(sum(line.strip() == '<a id="%s"></a>' % anchor
                                 for line in html.splitlines()), 1,
                             "anchor must be a standalone HTML target: %s" % anchor)
        self.assertTrue(all(tag == "a" for tag, _value in parser.ids), "public anchors must be <a id>")
        links = LINK_RE.findall(text)
        local = [unquote(link[1:]) for link in links if link.startswith("#")]
        self.assertEqual(Counter(local), Counter(page["anchors"]), "reading map differs from index")
        for link in links:
            with self.subTest(link=link):
                parts = urlsplit(link)
                self.assertFalse(parts.scheme or parts.netloc or parts.query,
                                 "browse links must be local file/anchor targets")
                path = unquote(parts.path)
                self.assertNotIn("\\", path, "backslash in link")
                self.assertFalse(path.startswith("/"), "absolute link")
                target = (posixpath.normpath(posixpath.join(posixpath.dirname(page["path"]), path))
                          if path else page["path"])
                _public_file(self, target)
                if parts.fragment:
                    target_parser = _Anchors()
                    target_parser.feed(_navigation_html(_read_text(self, target)))
                    self.assertEqual(sum(value == unquote(parts.fragment)
                                         for _tag, value in target_parser.ids), 1,
                                     "dangling or ambiguous link: %s" % link)

    def test_backend_index_routes(self):
        page = _browse_page(self)
        rows = [line.split("\t") for line in _read_text(self, ROUTES_PATH).splitlines()[1:]]
        self.assertIn([page["id"], page["path"]], rows, "index id missing from routes")
        self.assertEqual(sum(row[0] == page["id"] for row in rows if row), 1,
                         "index id must occur exactly once in routes")

    def test_backend_card_tokens(self):
        page = _browse_page(self)
        enc = _offline_encoding(self)
        count = len(enc.encode(_read_text(self, page["path"])))
        self.assertLessEqual(count, CARD_TOKEN_CAP,
                             "backend card uses %d o200k_base tokens, cap %d" % (count, CARD_TOKEN_CAP))

    def test_backend_no_volatile_data(self):
        page = _browse_page(self)
        for rel in [SUBJECTS_PATH, BACKEND_INDEX, page["path"]]:
            with self.subTest(entry=rel):
                self.assertEqual(scan_volatility(_read_text(self, rel)), [],
                                 "volatile data in backend navigation: %s" % rel)

    def test_backend_future_identities(self):
        page = _browse_page(self)
        text = _read_text(self, page["path"])
        self.assertIn("All component identities below are future targets; no implementation location is asserted.",
                      text, "missing future identity disclaimer")
        self.assertIn("| Future identity [TARGET] | Responsibility [TARGET] |", text)
        table_rows = [line for line in text.splitlines() if line.startswith("|")]
        identities = []
        for line in table_rows:
            cells = [cell.strip() for cell in line.strip("|").split("|")]
            identity = cells[0].removesuffix(" [TARGET]")
            if identity in FUTURE_IDENTITIES:
                identities.append(identity)
                self.assertTrue(all(cell.endswith(" [TARGET]") for cell in cells),
                                "future component row lacks TARGET classification")
                self.assertEqual(len(cells), 2, "component table must not advertise code paths")
                self.assertNotRegex(line, r"(?:[/\\]|\.(?:rs|py|js)\b|\]\()",
                                    "future component row asserts a location")
        self.assertEqual(Counter(identities), Counter(FUTURE_IDENTITIES), "future identity table incomplete")
        self.assertIn("router available (uncalibrated)", text)
        for evidence in ["MEASURED", "CITED", "TARGET"]:
            self.assertIn(evidence, text, "missing evidence classification")
        self.assertIn("python3 -B -m unittest discover -s tests/bootstrap -p 'test_entry_contract.py'", text,
                      "missing executable documentation oracle")

    def test_entry_no_volatile_data(self):
        findings = []
        for rel in ENTRY_FILES:
            text = _read_text(self, rel)
            for category, match in scan_volatility(text):
                findings.append("%s [%s]: %r" % (rel, category, match))
        self.assertEqual(findings, [], "volatile data in entry files: %s" % (findings,))

    def test_router_uncalibrated_honesty(self):
        readme = _read_text(self, "README.md")
        self.assertIn("**router available (uncalibrated)**", readme)
        self.assertIn("built with OpenAI Codex (Sol 6.1)", readme)
        self.assertIn("lookup is consumed by `tools/resolve_cafe.py`", readme)
        self.assertIn("Uncalibrated warning", readme)
        self.assertIn("no Café parameters have been fitted", readme)
        self.assertIn("--require-qualified", readme)
        self.assertIn("fails even when structural checks pass", readme)
        self.assertIn("DEV/TEST calibration is after the demo", readme)
        self.assertIn("uncalibrated", _read_text(self, "llms.txt"))

    def test_volatility_closed_list_negatives(self):
        posix_a = "Path: " + "/" + "hom" + "e" + "/" + "fixture" + "/file"
        posix_b = "Path: " + "/" + "User" + "s" + "/" + "Example" + "/file"
        windows = "Path: " + "C" + ":" + "\\" + "User" + "s" + "\\" + "Example" + "\\" + "file.txt"
        cases = [
            ("iso-date", "Build: 2001-02-03T04:05:06Z"),
            ("iso-date", "Date: 2001-02-03"),
            ("iso-time", "Time: 04:05:06Z"),
            ("labeled-pid", "pId:42"),
            ("labeled-pid", "PID=42"),
            ("labeled-pid", "PiD 42"),
            ("posix-home", posix_a),
            ("posix-home", posix_b),
            ("windows-path", windows),
            ("unc-path", 'Archive: ' + chr(92) * 2 + 'fixture' + chr(92) + 'share'),
            ("unc-path", 'Archive: `' + chr(92) * 2 + 'fixture' + chr(92) + 'share`'),
            ("unc-path", 'Archive: "' + chr(92) * 2 + 'fixture' + chr(92) + 'share"'),
            ("unc-path", 'Archive: (' + chr(92) * 2 + 'fixture' + chr(92) + 'share)'),
            ("hex", "Revision: abc1234"),
            ("hex", "Revision: ABC1234"),
            ("hex", "Counter: 1234567"),
        ]
        for expected, text in cases:
            with self.subTest(expected=expected, text=text):
                findings = scan_volatility(text)
                categories = [category for category, _ in findings]
                self.assertIn(expected, categories,
                              "expected %s in %s for %r" % (expected, findings, text))

    def test_volatility_hex_identity_boundary(self):
        exact = _IDENTITY_EXACT
        altered = _IDENTITY_EXACT.replace("247666607", "247666608")
        with self.subTest(case="exact-passes"):
            self.assertEqual(scan_volatility("Identity: " + exact), [],
                             "exact identity must pass")
        with self.subTest(case="altered-rejects"):
            findings = scan_volatility("Identity: " + altered)
            self.assertIn("hex", [category for category, _ in findings],
                          "altered identity must reject via hex: %s" % (findings,))
        with self.subTest(case="loose-hash-rejects"):
            findings = scan_volatility("Revision: abc1234")
            self.assertIn("hex", [category for category, _ in findings],
                          "loose hash must reject: %s" % (findings,))
        with self.subTest(case="exact-plus-hash-rejects"):
            findings = scan_volatility("Identity: " + exact + " abc1234")
            self.assertIn("hex", [category for category, _ in findings],
                          "exact identity plus hash must reject: %s" % (findings,))
        with self.subTest(case="pure-numeric-rejects"):
            findings = scan_volatility("Counter: 1234567")
            self.assertIn("hex", [category for category, _ in findings],
                          "pure numeric token must reject: %s" % (findings,))
        with self.subTest(case="longer-nonhex-passes"):
            self.assertEqual(scan_volatility("Token: zzzabc1234"), [],
                             "longer nonhex word must pass")
        with self.subTest(case="short-hex-passes"):
            self.assertEqual(scan_volatility("Token: abc123"), [],
                             "short hex token must pass")
        with self.subTest(case="nondigit-hex-passes"):
            self.assertEqual(scan_volatility("Token: abcdefab"), [],
                             "nondigit hex word must pass")

    def test_identity_prefix_suffix_reject_readme(self):
        read_text = _read_text
        for payload in ("Identity: a" + _IDENTITY_EXACT,
                        "Identity: " + _IDENTITY_EXACT + "a"):
            with self.subTest(payload=payload):
                def injected(test, rel):
                    content = read_text(test, rel)
                    return content + "\n" + payload if rel == "README.md" else content
                with mock.patch(__name__ + "._read_text", side_effect=injected):
                    with self.assertRaisesRegex(AssertionError, r"README\.md \[hex\]"):
                        self.test_entry_no_volatile_data()

    def test_volatility_allowlist_passes(self):
        deferred_posix = "Path: " + "/" + "tmp" + "/" + "fixture" + "/file"
        cases = [
            "See https://example.com/docs/page for details",
            "Read docs/architecture/file.md for rules",
            "Config lives at ~/.config/app/settings.json",
            "The PID controller was tuned",
            "PID",
            "Meeting on 3 Feb 2001 was noted",
            deferred_posix,
            "Token: abc123",
            "Token: abcdefab",
            "Token: zzzabc1234",
        ]
        for text in cases:
            with self.subTest(text=text):
                findings = scan_volatility(text)
                self.assertEqual(findings, [], "allowlist must pass: %r got %s" % (text, findings))


if __name__ == "__main__":
    unittest.main()
