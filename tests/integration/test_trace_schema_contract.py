"""Real sink process must accept the pinned contract and reject changed bytes."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'connectors'))
from cafe import runtime_logging as trace


def host_start():
    return {'schema': 'cafe.runtime_trace.v1', 'seq': 1, 'elapsed_ms': 0,
            'event': 'host_start', 'op': 'none', 'bridge': 'unknown', 'reason': 'none',
            'result_state': 'unknown', 'result_code': 'unknown_result', 'stage': 'none',
            'capsule_count': None, 'paired_feed_count': None, 'listed_window_count': None,
            'minimized_window_count': None, 'visible_window_count': None,
            'recorded_frames': None, 'recording_state': 'unknown',
            'base_commit': None, 'authority_sha256': 'a' * 64}


class TraceSchemaContract(unittest.TestCase):
    def run_sink(self, script, directory):
        return subprocess.run([sys.executable, '-I', '-B', str(script),
                               '--root', str(directory)],
                              input=json.dumps(host_start()).encode() + b'\n',
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)

    def test_current_packaged_contract_is_accepted_by_real_sink_and_reader(self):
        with tempfile.TemporaryDirectory(prefix='cafe-trace-contract-') as temporary:
            directory = Path(temporary).resolve() / 'diagnostics'
            result = self.run_sink(ROOT / 'connectors/cafe/logging_storage.py', directory)
            self.assertEqual(result.returncode, 0, 'current contract must admit the host trace')
            self.assertEqual(result.stdout, b'')
            self.assertEqual(result.stderr, b'')
            self.assertEqual(len(list(directory.glob('trace-*.jsonl'))), 1)
            self.assertEqual(trace.recent(directory)['rows'], [host_start()])

    def test_changed_packaged_contract_is_rejected_before_trace_creation(self):
        with tempfile.TemporaryDirectory(prefix='cafe-trace-contract-') as temporary:
            package = Path(temporary).resolve() / 'package'
            connectors = package / 'connectors/cafe'
            contracts = package / 'src/contracts'
            connectors.mkdir(parents=True)
            contracts.mkdir(parents=True)
            for name in ('logging_storage.py', 'runtime_logging.py'):
                shutil.copyfile(ROOT / 'connectors/cafe' / name, connectors / name)
            # Even harmless byte drift must not silently bypass the schema pin.
            original = (ROOT / 'src/contracts/runtime_logging.json').read_bytes()
            (contracts / 'runtime_logging.json').write_bytes(original + b'\n')
            directory = Path(temporary).resolve() / 'diagnostics'
            result = self.run_sink(connectors / 'logging_storage.py', directory)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stdout, b'')
            self.assertEqual(result.stderr, b'')
            self.assertFalse(directory.exists())


if __name__ == '__main__':
    unittest.main()
