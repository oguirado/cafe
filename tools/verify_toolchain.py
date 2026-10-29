"""Fail before a certificate if the observed toolchain differs from its profile."""
import argparse
import json
import re
import subprocess


def verify(expected, runner=subprocess.run):
    if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', expected):
        raise ValueError('invalid_exact_toolchain_version')
    records = []
    for program in ('cargo', 'rustc'):
        result = runner([program, '--version'], capture_output=True, text=True,
                        timeout=15, check=False)
        match = re.fullmatch(re.escape(program) + r' ([0-9]+\.[0-9]+\.[0-9]+)(?: \([^\n]+\))?\s*', result.stdout)
        observed = match[1] if result.returncode == 0 and match else None
        records.append({'program': program, 'expected': expected, 'observed': observed})
    return {'schema': 'cafe.toolchain-check.v1',
            'status': 'pass' if all(r['observed'] == expected for r in records) else 'blocked',
            'records': records}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--expected', required=True)
    args = parser.parse_args(argv)
    try:
        report = verify(args.expected)
    except (OSError, ValueError, subprocess.SubprocessError):
        report = {'schema': 'cafe.toolchain-check.v1', 'status': 'blocked',
                  'reason': 'exact_toolchain_unavailable'}
    print(json.dumps(report, sort_keys=True))
    return 0 if report['status'] == 'pass' else 1


if __name__ == '__main__':
    raise SystemExit(main())
