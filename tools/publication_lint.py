"""Read-only public-tree privacy lint; findings contain hashes, never matched text.

The optional external policy contains reviewed SHA256 token phrases. Public
exceptions are exact whole-line expressions, never directories. Filename policy
requires portable ASCII English project identifiers, with reviewed word tokens.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import unicodedata

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = 'cafe.publication-lint.v1'
DECLARED_IDENTITY = '247666607+oguirado@users.noreply.github.com'
HOME = re.compile('/' + '(?:home|Users)' + r'/[^\s/"\x27`<>]+|[A-Za-z]:[\\/]+Users[\\/]+[^\s\\/"\x27`<>]+|(?<!\\)\\\\[A-Za-z0-9_.-]+\\[A-Za-z0-9_.$-]+', re.I)
EMAIL = re.compile(r'(?<![A-Za-z0-9.!#$%&*+/=?^_{|}~@-])[A-Za-z0-9.!#$%&*+/=?^_{|}~-][A-Za-z0-9.!#$%&\x27*+/=?^_`{|}~-]*@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+(?![A-Za-z0-9@-])')
CREDENTIAL = re.compile(r'-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----|\bgh[pousr]_[A-Za-z0-9_]{20,}\b|\bgithub_pat_[A-Za-z0-9_]{20,}\b|\bsecret_key\s*[:=]\s*["\x27]?[a-fA-F0-9]{64}(?![a-fA-F0-9])', re.I)
IDENTITY_FIELD = re.compile(r'(?<![A-Za-z0-9_])(?:["\x27](?:user_name|username|host_name|hostname|user|host)["\x27]\s*[:=]|(?:user_name|username|host_name|hostname)["\x27]?\s*[:=]|(?:user|host)\s*=)\s*["\x27]?([A-Za-z][A-Za-z0-9_.-]{1,})', re.I)
EPOCH_SUFFIX = re.compile(r'(?<![A-Za-z0-9])(?:\d{10}|\d{13})[A-Za-z]{1,3}(?![A-Za-z0-9])')
# Reviewed English M0/F1 vocabulary; no path or directory exception.
NAME_WORDS = frozenset('acceptance admission agents allowlist anchor answer app apply architecture archive assets auth authority backend backendsupervisor base baseline bench blocks bootstrap bridge bug build cafe cafebridge calibration capped capsule capture cargo cases catalogue census changes channel chat check ci claims clean clone code codeowners cols commands common companions completion components conduct connector connectors contract contracts contributing control controls coordinates core cspdialog css ctl ctx dependabot design designtokens dev digest docs edges electronshell empty entry eval event export extract families ffmpeg fixture fixtures float frame freeze frozen fuses gate gates gen gitattributes github gitignore gitkeep gitleaks graphics grid health hooks host id index install integration intent inventory issue journal js jsconfig json jsonl jsonschema justification kit lane lanes layers license lifecycle link lint lite llms local lock logging logs main make manifest mcp md memory merge mounts multimount muse node nodes notice null obs open openai overlap ownership pacer package page pages pairing papers params path pfx phr pm policy pool post pre precedence preload preloadsecurity prepared privacy private probe profile protocol provenance publication pull push py pyproject python qualification qualify queries queue readme recorder recovery registry renderer rendererassets renderercheck rendererreplacement replay repo request requirements resolve route router routes rs rule run rust scan schema schemas se secret security server sh sha shell shellfuses shellsecurity shortlist smoke source sources sprint src stages standards statusbar storage subjects sums supervisor support sw symbols synthetic template test tests thes thesaurus timestamp tokenizer tokens toml toolchain tools topbar tsv tune twitch txt uibutton uitextinput unanswerable unit usage vendor verify vocabulary vue wgc window windows workflow workflows workspace worktree yaml yml'.split())
# Reviewed English runtime identifiers; exact tokens, no directory bypass.
NAME_WORDS = NAME_WORDS | frozenset({'pipeline', 'intents', 'operator', 'effects', 'runtime', 'recording', 'runtimebackend', 'service', 'services', 'owner', 'crop'})
FORBIDDEN_NAMES = {'operator.token', 'launch.token', 'auth.json'}


def digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def words(value):
    value = unicodedata.normalize('NFKC', value)
    value = ''.join(c for c in value if unicodedata.category(c) != 'Cf')
    return re.findall(r'[^\W_]+', value.casefold())




def load_policy(path):
    if path is None:
        return []
    candidate = Path(path)
    if any(part in FORBIDDEN_NAMES or part.endswith('.key') for part in candidate.parts + candidate.resolve().parts):
        raise ValueError('sensitive_policy_not_opened')
    data = json.loads(candidate.read_text(encoding='utf-8'))
    if set(data) != {'schema', 'reviewed', 'deny'} or data['schema'] != 'pm.release-policy.v1' or data['reviewed'] is not True or not isinstance(data['deny'], list) or not data['deny']:
        raise ValueError('invalid_private_policy')
    for rule in data['deny']:
        if set(rule) - {'case_sensitive'} != {'category', 'words', 'first_sha256', 'phrase_sha256'} or rule['category'] not in {'private_user', 'private_host', 'private_tool', 'private_corpus', 'private_project'} or type(rule['words']) is not int or not 1 <= rule['words'] <= 12 or rule.get('case_sensitive', False) not in [True, False] or any(not re.fullmatch(r'[a-f0-9]{64}', rule[k]) for k in ['first_sha256', 'phrase_sha256']):
            raise ValueError('invalid_private_policy_rule')
    return data['deny']


def private_hit(value, rules):
    tokens = words(value)
    for i, token in enumerate(tokens):
        first = digest(token)
        for rule in rules:
            if first == rule['first_sha256'] and digest(' '.join(tokens[i:i+rule['words']])) == rule['phrase_sha256']:
                return True
    return False


def load_allowlist(path):
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    if set(data) != {'schema', 'lines'} or data['schema'] != 'cafe.publication-allowlist.v1' or not isinstance(data['lines'], list):
        raise ValueError('invalid_line_allowlist')
    result = []
    for row in data['lines']:
        if set(row) != {'pattern', 'reason'} or not isinstance(row['reason'], str) or not row['reason'] or not isinstance(row['pattern'], str):
            raise ValueError('invalid_line_allowlist_rule')
        pattern = row['pattern']
        if not pattern.startswith('^') or not pattern.endswith('$') or any(s in pattern for s in ['.*', '.+', '(?s', '(?m', '\\n', '\\r']):
            raise ValueError('unanchored_or_broad_allowlist')
        result.append(re.compile(pattern))
    return result


def safe_name(name):
    return isinstance(name, str) and bool(name) and '\\' not in name and '\0' not in name and not name.startswith('/') and ':' not in name and str(PurePosixPath(name)) == name and all(p not in ['', '.', '..'] for p in name.split('/'))


def filename_ok(name):
    if not name.isascii() or not re.fullmatch(r'[A-Za-z0-9_./-]+', name):
        return False
    for part in name.split('/'):
        # Canonical content-addressed asset identifiers are language-neutral, not directory exceptions.
        if re.fullmatch(r'(?=[a-f0-9]*[0-9])[a-f0-9]{32,64}',part):
            continue
        for token in re.findall(r'[A-Za-z]+',part):
            if len(token)>2 and token.casefold() not in NAME_WORDS:
                return False
    return True


def scan_text(name, text, rules=(), allowlist=()):
    findings = []
    def finding(rule, line):
        record = {'path_sha256': digest(name), 'line': line, 'rule': rule}
        if record not in findings:
            findings.append(record)
    if private_hit(name, rules):
        finding('private_identifier', 0)
    if not filename_ok(name):
        finding('non_english_or_unreviewed_filename', 0)
    decoded = re.sub(r'\\u([0-9a-fA-F]{4})', lambda match: chr(int(match[1],16)), text).replace('\\/', '/')
    for view in dict.fromkeys([text, decoded]):
        # Full-text whitespace handling also closes quoted multi-line JSON key/value gaps.
        for match in IDENTITY_FIELD.finditer(view):
            line_number = view.count('\n',0,match.start())+1
            if not any(regex.fullmatch(view.splitlines()[line_number-1]) for regex in allowlist):
                finding('user_or_host_identifier',line_number)
        for n, line in enumerate(view.splitlines(), 1):
            # A private identifier is never suppressible by a public fixture exception.
            if private_hit(line, rules):
                finding('private_identifier', n)
            if any(regex.fullmatch(line) for regex in allowlist):
                continue
            if HOME.search(line):
                finding('absolute_home_or_unc_path', n)
            if any(m[0] != DECLARED_IDENTITY for m in EMAIL.finditer(line)):
                finding('undeclared_email', n)
            if CREDENTIAL.search(line):
                finding('credential', n)
            if IDENTITY_FIELD.search(line):
                finding('user_or_host_identifier', n)
            if EPOCH_SUFFIX.search(line):
                finding('epoch_with_letter_suffix', n)
    return findings


def inventory(root, snapshot=None):
    if (root / '.git').exists():
        result = subprocess.run(['git', '-C', str(root), 'ls-files', '--cached', '--others', '--exclude-standard', '-z'], check=True, capture_output=True)
        names = sorted(set(result.stdout.decode().split('\0')) - {''})
        if snapshot and json.loads(Path(snapshot).read_text()) != names:
            raise ValueError('stale_inventory_snapshot')
    elif snapshot:
        names = json.loads(Path(snapshot).read_text())
    elif os.environ.get('CAFE_PREPARED_INVENTORY_JSON') is not None:
        names = json.loads(os.environ['CAFE_PREPARED_INVENTORY_JSON'])
        from ci_support import validate_inventory
        validate_inventory(root,names)
    else:
        raise ValueError('versioned_inventory_unavailable')
    if not isinstance(names, list) or not names or names != sorted(set(names)) or any(not safe_name(n) for n in names):
        raise ValueError('invalid_or_empty_inventory')
    return names


def scan(root, names, rules, allowlist):
    findings = []
    for name in names:
        path = root / name
        if any(p in FORBIDDEN_NAMES or p.startswith('.env') or p.endswith('.key') for p in PurePosixPath(name).parts):
            findings.append({'path_sha256': digest(name), 'line': 0, 'rule': 'sensitive_file_not_opened'})
            continue
        if path.resolve() != path or not path.is_file():
            raise ValueError('unsafe_or_missing_public_file')
        data = path.read_bytes()
        if b'\0' in data:
            raise ValueError('unsupported_binary_public_file')
        findings.extend(scan_text(name, data.decode('utf-8-sig'), rules, allowlist))
    return findings


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('root', nargs='?', default=str(ROOT))
    p.add_argument('--inventory')
    p.add_argument('--policy')
    p.add_argument('--stdin', action='store_true')
    p.add_argument('--name', default='privacy.txt')
    args = p.parse_args(argv)
    try:
        root = Path(args.root).resolve()
        configured = args.policy or os.environ.get('CAFE_PRIVATE_POLICY')
        local = root / 'local/private_denylist.json'
        rules = load_policy(configured if configured else local if local.is_file() else None)
        allowed = load_allowlist(root / 'tools/publication_allowlist.json')
        if args.stdin:
            if not safe_name(args.name):
                raise ValueError('invalid_stdin_name')
            names = [args.name]
            findings = scan_text(args.name, sys.stdin.buffer.read().decode('utf-8'), rules, allowed)
        else:
            names = inventory(root, args.inventory)
            findings = scan(root, names, rules, allowed)
        result = {'schema': SCHEMA, 'status': 'fail' if findings else 'pass', 'files': len(names), 'findings': findings, 'count': len(findings), 'policy_loaded': bool(rules)}
        rc = 1 if findings else 0
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError, re.error):
        result = {'schema': SCHEMA, 'status': 'fail', 'error': 'publication_inputs_unavailable_or_invalid'}
        rc = 2
    print(json.dumps(result, sort_keys=True))
    return rc


if __name__ == '__main__':
    raise SystemExit(main())
