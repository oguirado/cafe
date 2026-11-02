"""Start Café's MCP server for the current Windows user; register this file in an MCP client."""
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cafe.mcp_organizer import current_broker  # noqa: E402
from cafe.mcp_server import main as _main  # noqa: E402


def default_arguments(appdata):
    """Organizer root, plus the chat data of the newest session when it exists."""
    root = Path(appdata) / 'cafe-shell' / 'authority'
    arguments = ['--organizer-root', str(root)]
    try:
        chat = current_broker(root).parent / 'chat'
    except Exception:
        return arguments
    return arguments + ['--data', str(chat)] if chat.is_dir() else arguments


def main(argv=None):
    appdata = os.environ.get('APPDATA')
    if not appdata:
        return 2
    return _main(default_arguments(appdata) if argv is None else argv)


if __name__ == '__main__':
    raise SystemExit(main())
