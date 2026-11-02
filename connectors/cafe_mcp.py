"""Start Café's MCP server for the current Windows user; register this file in an MCP client."""
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cafe.mcp_server import main as _main  # noqa: E402


def main(argv=None):
    appdata = os.environ.get('APPDATA')
    if not appdata:
        return 2
    return _main(['--organizer-root', str(Path(appdata) / 'cafe-shell' / 'authority')] if argv is None else argv)


if __name__ == '__main__':
    raise SystemExit(main())
