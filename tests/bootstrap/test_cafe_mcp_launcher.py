import os
import pathlib
import sys
import tempfile
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'connectors'))

import cafe_mcp  # noqa: E402


class TestLauncherArguments(unittest.TestCase):
    def _session(self, appdata, name, age, chat):
        session = pathlib.Path(appdata) / 'cafe-shell' / 'authority' / name
        (session / 'broker').mkdir(parents=True)
        pipe = session / 'broker' / 'broker.pipe'
        pipe.write_bytes(b'')
        stamp = time.time() - age
        os.utime(pipe, (stamp, stamp))
        if chat:
            (session / 'chat').mkdir()
        return session

    def test_newest_session_chat_is_passed(self):
        with tempfile.TemporaryDirectory() as appdata:
            self._session(appdata, 'session-1', 100, True)
            newest = self._session(appdata, 'session-2', 1, True)
            arguments = cafe_mcp.default_arguments(appdata)
            self.assertEqual(arguments[-2:], ['--data', str(newest / 'chat')])
            self.assertEqual(arguments[0], '--organizer-root')

    def test_no_chat_folder_means_no_data_argument(self):
        with tempfile.TemporaryDirectory() as appdata:
            self._session(appdata, 'session-1', 1, False)
            self.assertNotIn('--data', cafe_mcp.default_arguments(appdata))

    def test_no_session_still_starts_with_organizer_root(self):
        with tempfile.TemporaryDirectory() as appdata:
            (pathlib.Path(appdata) / 'cafe-shell' / 'authority').mkdir(parents=True)
            self.assertEqual(len(cafe_mcp.default_arguments(appdata)), 2)


if __name__ == '__main__':
    unittest.main()
