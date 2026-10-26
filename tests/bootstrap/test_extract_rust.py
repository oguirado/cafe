"""Handwritten P05 Rust inventory and bounded-extractor negative oracles."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
CAPSULE = 'core/src/capsule_registry.rs'
FILES = sorted(['core/src/authority_journal.rs', CAPSULE, 'core/src/journal_replay.rs', 'core/src/main.rs', 'core/src/windows_window_registry.rs', 'core/src/capsule_pairing.rs', 'core/src/capture_health.rs', 'core/src/capture_frame_pool.rs', 'core/src/windows_graphics_capture.rs', 'core/src/windows_journal_policy.rs'])
# Independently transcribed from reviewed P05 declarations, before extractor execution.
GOLD = [
    ('SCHEMA', 14), ('MAX_ENVELOPE_BYTES', 17), ('Clock', 25), ('Role', 102),
    ('Envelope', 110), ('Command', 122), ('DecodeError', 150), ('decode_envelope', 293),
    ('Feed', 306), ('Capsule', 315), ('Proposal', 325), ('Snapshot', 336),
    ('Outcome', 348), ('Receipt', 356), ('Reply', 364), ('Registry', 374),
    ('Registry::new', 385), ('Registry::snapshot', 397),
    ('Registry::decode_and_apply', 409), ('Registry::apply', 416),
]
JOURNAL_GOLD = [
    ('JOURNAL_SCHEMA', 29), ('MAX_RECORD_BYTES', 32), ('JournalError', 46),
    ('AppendState', 73), ('Record', 81), ('Pending', 95), ('Pending::state', 101),
    ('Committed', 109), ('Committed::state', 116), ('Committed::sequence', 120),
    ('Committed::event_id', 124), ('Committed::record_digest', 128),
    ('Journal', 172), ('canonical_bytes', 191), ('Journal::open', 573),
    ('Journal::stage_control', 622), ('Journal::commit', 676),
    ('Journal::append_control', 719), ('Journal::append_media_intent', 729),
    ('Journal::publish_media', 770), ('Journal::records', 852),
    ('Journal::recovery_status', 857), ('Journal::interrupted_bytes', 862),
]
REPLAY_GOLD = [('RecoveryStatus', 21), ('MediaState', 29), ('Recovery', 40), ('recover', 68)]
WINDOW_GOLD = [('Rect',7),('WindowHandle',11),('WindowView',14),('FixtureWindow',22),('WindowError',29),('WindowRegistry',43),('WindowRegistry::new',46),('WindowRegistry::refresh_fixtures',47),('WindowRegistry::snapshots',85),('WindowRegistry::resolve',86),('WindowRegistry::invalidate_layout',94),('NativeAdapter',159),('NativeAdapter::new',165),('NativeAdapter::enumerate',169)]
PAIRING_GOLD = [('Crop',7),('PairRequest',16),('Binding',31),('PairingBook',43),('PairingBook::new',48),('PairingBook::pair',54),('PairingBook::bindings',63),('PairingBook::current',67),('PairingBook::pair_by_executable',80),('PairError',87)]
HEALTH_GOLD = [('HealthPolicy',5),('BorderStatus',10),('CaptureState',13),('CaptureEvent',19),('HealthError',25),('CaptureHealth',31),('CaptureHealth::new',44),('CaptureHealth::event',61),('CaptureHealth::admit_frame',84),('CaptureHealth::status',118)]
POOL_GOLD = [('PoolLimits',18),('BorrowedFrame',28),('PoolError',39),('FramePool',51),('FramePool::new',59),('FramePool::copy_from',89),('FramePool::available',140),('FramePool::in_use',145),('FramePool::capacity',150),('OwnedFrame',170),('OwnedFrame::pixels',182),('OwnedFrame::width',186),('OwnedFrame::height',190),('OwnedFrame::source_stride',195),('OwnedFrame::packed_stride',200),('OwnedFrame::source_time',205)]
CAPTURE_GOLD = [('NativeSupport',27),('CaptureConfig',36),('CaptureError',48)]
CAPTURE_PORTABLE_GOLD = [('native_support',80),('NativeCapture',85),('NativeCapture::start',91),('NativeCapture::take_latest',101),('NativeCapture::poll_state',105),('NativeCapture::border_status',109),('NativeCapture::stop',113)]
SOURCE_HASHES = {'core/src/authority_journal.rs': '6bee8dfcc7fbb537146d13bed0c5ce9d68b91e26fa243ca93c96ade7734f852c', 'core/src/capsule_registry.rs': '4e05d8fe4cb5abf357b30816ed2a1aca54299e1b264f7d0629acf2dfb987fd7d', 'core/src/journal_replay.rs': '16a92bb9817ae399055470bc762be738ea5997a6bc5e2130e2eea96d8ecbf720', 'core/src/main.rs': '0a2b0b727c28fd999848936819ea1cc4a3ead1aea178b1d24d067654ced36db7', 'core/src/capsule_pairing.rs': 'e6055d47ba364809b8c22591dd47d23f5f74d8c1ba991831686c8d0eb7196835', 'core/src/windows_window_registry.rs': '84239abed2aa581d1d3b9559ea50c8ee5a0c259b5dd6241f708b71bf5af5da38', 'core/src/capture_health.rs': 'e8c64656ce7a26c86605eccf7f14581c9ce78b7a480858bac2e08636804a3058', 'core/src/capture_frame_pool.rs': '37471684096c063bf2aca5d3b6d7bd660521851176240bcebc029d7772a4d76d', 'core/src/windows_graphics_capture.rs': '39c92d83acb1cd6a815d17182af2a2e8497bb444ec6aea5260f0e16b6a76202e', 'core/src/windows_journal_policy.rs': '3e6087d78cc62d14c1d2586aefe90a44de70cc386cd47f366c2b2d2fb8f80ad9'}
SPEC = importlib.util.spec_from_file_location('cafe_rust_extractor', ROOT / 'tools/extract_rust.py')
x = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = x
SPEC.loader.exec_module(x)
QSPEC = importlib.util.spec_from_file_location('cafe_rust_qualification', ROOT / 'tools/qualify_cafe_router.py')
q = importlib.util.module_from_spec(QSPEC)
sys.modules[QSPEC.name] = q
QSPEC.loader.exec_module(q)


def expected_gold():
    return [{'path': CAPSULE, 'symbol': 'cafe_core::capsule_registry::' + name,
             'name': name.split('::')[-1], 'line': line, 'cfg': []} for name, line in GOLD]


def expected_inventory():
    # All rows originate in manually transcribed tuples, never parsed source output.
    def rows(path, module, pairs):
        return [{'path': path, 'symbol': 'cafe_core::' + module + '::' + name,
                 'name': name.split('::')[-1], 'line': line, 'cfg': []} for name, line in pairs]
    return {'core/src/windows_journal_policy.rs': [],
            'core/src/authority_journal.rs': rows('core/src/authority_journal.rs', 'authority_journal', JOURNAL_GOLD),
            CAPSULE: expected_gold(),
            'core/src/journal_replay.rs': rows('core/src/journal_replay.rs', 'journal_replay', REPLAY_GOLD),
            'core/src/main.rs': [],
            'core/src/capture_health.rs': rows('core/src/capture_health.rs','capture_health',HEALTH_GOLD),
            'core/src/capture_frame_pool.rs': rows('core/src/capture_frame_pool.rs','capture_frame_pool',POOL_GOLD),
            'core/src/windows_graphics_capture.rs': rows('core/src/windows_graphics_capture.rs','windows_graphics_capture',CAPTURE_GOLD) + [dict(row,cfg=['not(windows)']) for row in rows('core/src/windows_graphics_capture.rs','windows_graphics_capture',CAPTURE_PORTABLE_GOLD)],
            'core/src/capsule_pairing.rs': rows('core/src/capsule_pairing.rs','capsule_pairing',PAIRING_GOLD),
            'core/src/windows_window_registry.rs': [dict(row,cfg=['not(windows)'] if 'NativeAdapter' in row['symbol'] else []) for row in rows('core/src/windows_window_registry.rs','windows_window_registry',WINDOW_GOLD)]}


class TestRustExtraction(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'core/src').mkdir(parents=True)
        self.inventory = ['core/src/main.rs']

    def source(self, text):
        (self.root / 'core/src/main.rs').write_text(text, encoding='utf-8')

    def extract(self, text, profile=None):
        self.source(text)
        return x.extract(self.root, self.inventory, self.inventory, profile)['files'][0]['symbols']

    def test_handwritten_current_public_inventory(self):
        inventory, _source = q.load_inventory(ROOT, os.environ.get('CAFE_ROUTER_INVENTORY'))
        report = x.extract(ROOT, FILES, inventory, {'windows':False,'unix':True,'test':False})
        self.assertEqual(report['schema'], 'cafe.rust-surface.v1')
        self.assertEqual(report['crate'], 'cafe_core')
        self.assertEqual(report['cfg_profile'], {'windows':False,'unix':True,'test':False})
        self.assertEqual({v['path']: v['symbols'] for v in report['files']}, expected_inventory())
        self.assertEqual({v['path']: v['source_sha256'] for v in report['files']}, SOURCE_HASHES)
        for row in [row for file in report['files'] for row in file['symbols']]:
            line = (ROOT / row['path']).read_text().splitlines()[row['line'] - 1]
            self.assertIn(row['name'], line)

    def test_removed_public_function_breaks_manual_gold(self):
        for rel in FILES:
            (self.root / rel).write_bytes((ROOT / rel).read_bytes())
        p = self.root / CAPSULE
        before = p.read_text()
        self.assertEqual(before.count('pub fn decode_envelope('), 1)
        p.write_text(before.replace('pub fn decode_envelope(', 'fn decode_envelope(', 1))
        report = x.extract(self.root, FILES, FILES, {'windows':False,'unix':True,'test':False})
        with self.assertRaises(AssertionError):
            self.assertEqual(next(v for v in report['files'] if v['path'] == CAPSULE)['symbols'], expected_gold())
        self.assertNotEqual(next(v for v in report['files'] if v['path'] == CAPSULE)['source_sha256'], SOURCE_HASHES[CAPSULE])

    def test_comment_and_string_lookalikes_not_extracted(self):
        text = '''// pub fn line_fake() {}
/* pub struct BlockFake; /* pub enum NestedFake {} */ */
const TEXT: &str = "pub fn string_fake() { \\\" }";
const RAW: &str = r###"pub fn raw_fake() { /* } */"###;
const BYTES: &[u8] = br##"pub mod byte_fake {}"##;
const CSTR: &std::ffi::CStr = c"pub trait Cfake {}";
const LETTER: char = '{';
pub fn real<'a>(value: &'a str) { let _ = 'x'; }
'''
        rows = self.extract(text)
        self.assertEqual(rows, [{'path': self.inventory[0], 'symbol': 'cafe_core::real',
                                'name': 'real', 'line': 8, 'cfg': []}])

    def test_macro_generated_public_surface_abstains(self):
        for text in ['macro_rules! expose { () => { pub fn hidden() {} } } expose!();',
                     'fn private() { macro_rules! expose { () => { pub struct Hidden; } } }',
                     'some_expander! { pub struct Hidden; }']:
            with self.subTest(text=text), self.assertRaises(x.ExtractionError):
                self.extract(text)

    def test_glob_and_named_reexports_abstain(self):
        for text in ['pub use x::*;', 'pub use x::{Thing, *};', 'pub use x::Thing;']:
            with self.subTest(text=text), self.assertRaises(x.ExtractionError):
                self.extract(text)

    def test_nonlisted_source_is_never_read(self):
        self.source('pub fn allowed() {}')
        rogue = self.root / 'core/src/unlisted.rs'
        rogue.write_bytes(b'\xffmacro_rules! rogue { () => { pub fn hidden() {} } }')
        original = Path.read_bytes
        reads = []
        def guarded(path):
            reads.append(path.relative_to(self.root).as_posix())
            if path == rogue:
                raise AssertionError('nonlisted source read')
            return original(path)
        with mock.patch.object(Path, 'read_bytes', guarded):
            rows = x.extract(self.root, self.inventory, self.inventory + ['core/src/unlisted.rs'])
        self.assertEqual(reads, self.inventory)
        self.assertEqual([v['name'] for v in rows['files'][0]['symbols']], ['allowed'])
        self.source('mod unlisted;')
        with mock.patch.object(Path, 'read_bytes', guarded), self.assertRaises(x.ExtractionError):
            x.extract(self.root, self.inventory, self.inventory + ['core/src/unlisted.rs'])
        self.assertNotIn('core/src/unlisted.rs', reads)

    def test_sorted_explicit_safe_versioned_list_required(self):
        self.source('pub fn ok() {}')
        bad = [[], [self.inventory[0]] * 2, ['../escape.rs'], ['/absolute.rs'],
               ['core/src/./main.rs'], ['core/src//main.rs'], ['core/src/main.txt'],
               ['core/src/main.rs', 'core/src/a.rs']]
        for paths in bad:
            with self.subTest(paths=paths), self.assertRaises(x.ExtractionError):
                x.extract(self.root, paths, self.inventory)
        with self.assertRaises(x.ExtractionError):
            x.extract(self.root, self.inventory, [])
        target = self.root / 'core/src/alias.rs'
        target.symlink_to(self.root / self.inventory[0])
        with self.assertRaises(x.ExtractionError):
            x.extract(self.root, ['core/src/alias.rs', 'core/src/main.rs'],
                      ['core/src/alias.rs', 'core/src/main.rs'])

    def test_source_bytes_hashed_before_strict_decode(self):
        p = self.root / self.inventory[0]
        p.write_bytes(b'\xffpub fn bad() {}')
        real = hashlib.sha256
        with mock.patch.object(x.hashlib, 'sha256', wraps=real) as hashed:
            with self.assertRaises(x.ExtractionError) as error:
                x.extract(self.root, self.inventory, self.inventory)
            self.assertEqual(error.exception.code, 'invalid_utf8')
            hashed.assert_called_once_with(p.read_bytes())
        raw = b'// bytes\r\npub fn fine() {}\r\n'
        p.write_bytes(raw)
        report = x.extract(self.root, self.inventory, self.inventory)
        self.assertEqual(report['files'][0]['source_sha256'], real(raw).hexdigest())
        self.assertEqual(report['files'][0]['symbols'][0]['line'], 2)

    def test_cfg_evaluated_only_with_explicit_profile(self):
        text = '#[cfg(all(unix, not(test), target_os = "linux"))]\npub fn active() {}\n#[cfg(test)]\npub fn inactive() {}\n'
        rows = self.extract(text, {'unix': True, 'test': False, 'target_os': 'linux'})
        self.assertEqual([v['name'] for v in rows], ['active'])
        self.assertEqual(rows[0]['cfg'], ['all(unix,not(test),target_os="linux")'])
        for source, profile in [(text, {}), ('#[cfg(unknown)] pub fn nope() {}', {}),
                                ('#[cfg(any(unix, mystery))] pub fn nope() {}', {'unix': True}),
                                ('#[cfg_attr(test, path="other.rs")] mod child;', {'test': False}),
                                ('#[cfg(unsupported(test))] pub fn nope() {}', {'test': True})]:
            with self.subTest(source=source), self.assertRaises(x.ExtractionError):
                self.extract(source, profile)
        rows = self.extract('#[cfg(feature="a")] pub const VALUE: u8 = 1;', {'feature': ['a', 'b']})
        self.assertEqual(rows[0]['name'], 'VALUE')

    def test_qualified_inline_modules_impl_and_item_kinds(self):
        text = '''pub mod outer {
 pub struct Thing;
 pub enum Choice { A }
 pub trait Interface { fn implicit(); }
 pub type Alias = Thing;
 pub const VALUE: u8 = 1;
 impl Thing { pub fn new() -> Self { Self } fn private() {} }
 pub(crate) fn restricted() {}
}
'''
        rows = self.extract(text)
        self.assertEqual([(v['symbol'], v['line']) for v in rows], [
            ('cafe_core::outer', 1), ('cafe_core::outer::Thing', 2),
            ('cafe_core::outer::Choice', 3), ('cafe_core::outer::Interface', 4),
            ('cafe_core::outer::Alias', 5), ('cafe_core::outer::VALUE', 6),
            ('cafe_core::outer::Thing::new', 7)])

    def test_private_static_is_not_public_and_unsupported_static_refuses(self):
        rows = self.extract('static NEXT: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(1);\npub fn visible() {}')
        self.assertEqual([row['symbol'] for row in rows], ['cafe_core::visible'])
        for source in ['pub static EXPORTED: u8 = 1;', 'static mut MUTABLE: u8 = 1;',
                       'static BLOCK: u8 = { 1 };']:
            with self.subTest(source=source), self.assertRaises(x.ExtractionError):
                self.extract(source)

    def test_module_graph_and_inherited_cfg(self):
        self.source('#[cfg(enabled)] mod child;')
        child = 'core/src/child.rs'
        (self.root / child).write_text('pub fn leaf() {}')
        paths = [child, self.inventory[0]]
        rows = x.extract(self.root, paths, paths, {'enabled': True})['files'][0]['symbols']
        self.assertEqual(rows, [{'path': child, 'symbol': 'cafe_core::child::leaf',
                                'name': 'leaf', 'line': 1, 'cfg': ['enabled']}])
        self.assertEqual(x.extract(self.root, paths, paths, {'enabled': False})['files'][0]['symbols'], [])
        self.source('fn root() {}')
        with self.assertRaises(x.ExtractionError):
            x.extract(self.root, paths, paths)
        self.source('mod child;')
        other = 'core/src/child/mod.rs'
        (self.root / other).parent.mkdir()
        (self.root / other).write_text('')
        with self.assertRaises(x.ExtractionError):
            x.extract(self.root, sorted(paths + [other]), sorted(paths + [other]))

    def test_unsupported_and_malformed_syntax_refuses(self):
        for text in ['#[path="elsewhere.rs"] mod child;', '#[expand] pub struct Hidden;',
                     '#[derive(Custom)] pub struct Hidden;', 'impl<T> Generic<T> { pub fn f() {} }',
                     'pub fn broken() {', '/* unterminated', 'pub const S: &str = "unterminated',
                     'pub static VALUE: u8 = 1;', 'pub union U { x: u8 }']:
            with self.subTest(text=text), self.assertRaises(x.ExtractionError):
                self.extract(text)

    def test_disabled_private_extern_is_bounded_and_active_or_unknown_refuses(self):
        text = '#[cfg(windows)] unsafe extern "system" fn callback() { let s="pub fn fake() {}"; }\npub fn actual() {}'
        rows = self.extract(text, {'windows': False})
        self.assertEqual([r['name'] for r in rows], ['actual'])
        for profile in [{}, {'windows': True}]:
            with self.subTest(profile=profile), self.assertRaises(x.ExtractionError): self.extract(text, profile)
        with self.assertRaises(x.ExtractionError): self.extract('#[cfg(windows)] pub extern "system" fn hidden() {}', {'windows': False})

    def test_cli_determinism_and_source_hashes(self):
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
        cfg = self.root / 'cfg.json'; cfg.write_text('{"windows":false,"unix":true,"test":false}')
        command = [sys.executable, '-B', 'tools/extract_rust.py', '--cfg-profile', str(cfg), '--files', *FILES]
        snapshot = os.environ.get('CAFE_ROUTER_INVENTORY')
        if snapshot:
            command += ['--inventory', snapshot]
        runs = [subprocess.run(command, cwd=ROOT, env=env, capture_output=True) for _ in range(2)]
        self.assertEqual([v.returncode for v in runs], [0, 0], runs[0].stderr.decode())
        self.assertEqual(runs[0].stdout, runs[1].stdout)
        self.assertEqual(runs[0].stderr, b'')
        report = json.loads(runs[0].stdout)
        self.assertEqual({v['path']: v['source_sha256'] for v in report['files']}, SOURCE_HASHES)
        self.assertTrue(runs[0].stdout.endswith(b'\n'))

    def test_rust_qualification_requires_actual_passing_nonempty_test(self):
        selection = 'tests/bootstrap/test_extract_rust.py'
        for check in [q.Check(selection, True, 0, 1), q.Check(selection, False, 1, 1),
                      q.Check(selection, False, 0, 0)]:
            def run(root, directory, pattern):
                return check if pattern == 'test_extract_rust.py' else q.Check(pattern, True, 0, 1)
            with self.subTest(check=check), mock.patch.object(q, 'run_selection', side_effect=run) as invoked, \
                    mock.patch.object(q, 'load_inventory', return_value=(['fixture'], 'fixture')), \
                    mock.patch.object(q, 'check_ownership', return_value=q.Check('ownership', True)):
                legs, source = q.collect(self.root)
                report = q.summarize(legs, source)
                q.validate_report(report, legs)
                row = report['legs']['extractor_rust']
                self.assertTrue(row['implemented'])
                self.assertEqual(row['status'], 'pass' if check.passed else 'fail')
                self.assertEqual(report['verdict'], 'router available (uncalibrated)' if check.passed else 'router unavailable')
                self.assertEqual(q.exit_code(legs), 0 if check.passed else 1)
                invoked.assert_any_call(self.root, 'tests/bootstrap', 'test_extract_rust.py')


if __name__ == '__main__':
    unittest.main()
