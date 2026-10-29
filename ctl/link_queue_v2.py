"""Second immutable link profile: include exact generated resolver coordinates."""
import hashlib
import importlib.util
from pathlib import Path

BASE = Path(__file__).with_name('link_queue.py')
EXPECTED_BASE_SHA256 = '4e97f38ac6120c0d51f4d2fddc7bf05d2d8f24bbfa6d4faeb2a9da1669753c10'
if hashlib.sha256(BASE.read_bytes()).hexdigest() != EXPECTED_BASE_SHA256:
    raise RuntimeError('base_link_tool_version_changed')
SPEC = importlib.util.spec_from_file_location('_cafe_link_queue_v2_base', BASE)
queue = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(queue)
ORIGINAL_SHARED = queue.SHARED_GENERATED
queue.SHARED_GENERATED = ORIGINAL_SHARED | frozenset({'catalogue/coordinates.jsonl'})


def main(argv=None):
    return queue.main(argv)


if __name__ == '__main__':
    raise SystemExit(main())
