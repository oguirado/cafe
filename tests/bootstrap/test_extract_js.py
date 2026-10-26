"""Independent manually transcribed JS surface and fail-closed extraction oracles."""
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
FILES = ['src/main/backendSupervisor.js', 'src/preload/cafeBridge.js']
# Authored from numbered P07 source before extracting; no generated inventory.
GOLD = {
    FILES[0]: [
        ('SupervisorCaller', 'typedef::SupervisorCaller', 12),
        ('SupervisorStatus', 'typedef::SupervisorStatus', 19),
        ('QueuedReceipt', 'typedef::QueuedReceipt', 30),
        ('EventSource', 'typedef::EventSource', 37),
        ('StdinAdapter', 'typedef::StdinAdapter', 43),
        ('ChildAdapter', 'typedef::ChildAdapter', 51),
        ('SpawnPipeOptions', 'typedef::SpawnPipeOptions', 61),
        ('SpawnFn', 'typedef::SpawnFn', 67),
        ('SupervisorOptions', 'typedef::SupervisorOptions', 71),
        ('getStatus', 'createBackendSupervisor::getStatus', 280),
        ('handle', 'createBackendSupervisor::handle', 356),
        ('subscribe', 'createBackendSupervisor::subscribe', 448),
        ('start', 'createBackendSupervisor::start', 474),
        ('dispose', 'createBackendSupervisor::dispose', 518),
        ('createBackendSupervisor', 'createBackendSupervisor', 804),
    ],
    FILES[1]: [
        ('BridgeTransport', 'typedef::BridgeTransport', 12),
        ('BridgeStatus', 'typedef::BridgeStatus', 18),
        ('CafeBridge', 'typedef::CafeBridge', 29),
        ('getSnapshot', 'createCafeBridge::getSnapshot', 201),
        ('submitIntent', 'createCafeBridge::submitIntent', 210),
        ('onSnapshot', 'createCafeBridge::onSnapshot', 218),
        ('createCafeBridge', 'createCafeBridge', 230),
    ],
}
PREFIX = {FILES[0]: 'cafe_shell::main::backendSupervisor::',
          FILES[1]: 'cafe_shell::preload::cafeBridge::'}
SPEC = importlib.util.spec_from_file_location('cafe_js_extractor', ROOT/'tools/extract_js.py')
x = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(x)
QSPEC = importlib.util.spec_from_file_location('cafe_js_qualifier', ROOT/'tools/qualify_cafe_router.py')
q = importlib.util.module_from_spec(QSPEC)
sys.modules[QSPEC.name] = q
QSPEC.loader.exec_module(q)


def expected_inventory():
    return {path: [{'path': path, 'symbol': PREFIX[path]+suffix, 'name': name,
                    'line': line, 'process': path.split('/')[1]}
                   for name, suffix, line in pairs] for path, pairs in GOLD.items()}


class TestJavaScriptExtraction(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.path = 'src/main/sample.js'
        (self.root/'src/main').mkdir(parents=True)

    def source(self, text, path=None):
        path = path or self.path
        p = self.root/path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding='utf-8')
        return path

    def extract(self, text):
        self.source(text)
        return x.extract(self.root, [self.path], [self.path])['files'][0]['symbols']

    def refusal(self, text, code=None):
        with self.assertRaises(x.ExtractionError) as raised:
            self.extract(text)
        if code: self.assertEqual(raised.exception.code, code)
        return raised.exception.code

    def test_handwritten_current_public_inventory(self):
        inventory, _ = q.load_inventory(ROOT, os.environ.get('CAFE_ROUTER_INVENTORY'))
        report = x.extract(ROOT, FILES, inventory)
        self.assertEqual(report['schema'], 'cafe.js-surface.v1')
        self.assertEqual(report['parser'], {'name':'typescript','version':'5.9.3'})
        self.assertEqual({f['path']: f['symbols'] for f in report['files']}, expected_inventory())
        self.assertEqual(sum(len(f['symbols']) for f in report['files']), 22)
        for f in report['files']:
            self.assertEqual(f['source_sha256'], hashlib.sha256((ROOT/f['path']).read_bytes()).hexdigest())
            for row in f['symbols']:
                self.assertIn(row['name'], (ROOT/f['path']).read_text().splitlines()[row['line']-1])

    def test_removed_export_breaks_manual_inventory(self):
        for path in FILES:
            p=self.root/path;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes((ROOT/path).read_bytes())
        p=self.root/FILES[1];s=p.read_text();self.assertEqual(s.count('module.exports = { createCafeBridge };'),1)
        p.write_text(s.replace('module.exports = { createCafeBridge };','void { createCafeBridge };'))
        result=x.extract(self.root,FILES,FILES)
        with self.assertRaises(AssertionError):
            self.assertEqual({f['path']:f['symbols'] for f in result['files']},expected_inventory())
        self.assertEqual([r['name'] for r in result['files'][1]['symbols']],['BridgeTransport','BridgeStatus','CafeBridge'])

    def test_comment_string_and_template_lookalikes_not_extracted(self):
        source='''// export function lineGhost() {}
/* export function blockGhost() {} */
const a="export function doubleGhost() {}";
const b='export function singleGhost() {}';
const c=`export function templateGhost() {} /** @typedef {object} Fake */`;
const d=`${"export function interpolatedGhost() {}"}`;
const e=/export function regexGhost/;
export function real() {}
'''
        self.assertEqual(self.extract(source),[{'path':self.path,'symbol':'cafe_shell::main::sample::real',
                                              'name':'real','line':8,'process':'main'}])

    def test_named_default_and_alias_exports(self):
        source='''const local = 1;
export function alpha() { return 2; }
export const bravo = 3;
export class Charlie {}
export { local as alias };
export default function named() { return 4; }
'''
        self.assertEqual([(r['name'],r['line']) for r in self.extract(source)],
                         [('alpha',2),('bravo',3),('Charlie',4),('alias',5),('default',6)])
        self.assertEqual([(r['name'],r['line']) for r in self.extract('export default class {}')],[('default',1)])
        self.assertEqual([(r['name'],r['line']) for r in self.extract('export default 3;')],[('default',1)])

    def test_commonjs_named_and_static_aggregate_exports(self):
        self.assertEqual([(r['name'],r['line']) for r in self.extract('exports.alpha=1;\nmodule.exports.bravo=()=>2;')],
                         [('alpha',1),('bravo',2)])
        self.assertEqual([(r['name'],r['line']) for r in self.extract('function local(){}\nmodule.exports={ local, alias:local };')],
                         [('alias',2),('local',2)])
        self.assertEqual([r['name'] for r in self.extract('module.exports = function () { return 1; };')],['default'])

    def test_static_facade_coordinates_and_ambiguity(self):
        rows=self.extract('''export function factory() {
  function first(){}
  return Object.freeze({first, second(){return 1;}});
}''')
        self.assertEqual([(r['name'],r['line']) for r in rows],[('factory',1),('first',2),('second',3)])
        for source in [
            'export function f(b){if(b)return {x(){}};return {y(){}};}',
            'export function f(b){if(b)return {x(){}};}',
            'export function f(){return {...unknown};}',
            'export function f(Object){return Object.freeze({x(){}});}',
            'const {Object}=unknown;export function f(){return Object.freeze({x(){}});}',
            'Object.freeze=unknown;export function f(){return Object.freeze({x(){}});}',
            'export async function f(){return {x(){}};}',
            'export function* f(){return {x(){}};}',
        ]:
            with self.subTest(source=source):self.refusal(source)

    def test_public_jsdoc_consecutive_blocks_and_private_exclusion(self):
        rows=self.extract('''/** @typedef {object} First */
/** @typedef {object} Second */
/** @private @typedef {object} Secret */
/** @internal @typedef {object} Internal */
const marker=1;
function nested(){ /** @typedef {object} Local */ const value=1; }
export const real=1;
''')
        self.assertEqual([(r['name'],r['line']) for r in rows],[('First',1),('Second',2),('real',7)])
        self.refusal('/** @typedef {object} Same */\n/** @typedef {object} Same */\nconst marker=1;','duplicate_public_typedef')
        self.refusal('function f(){\n/** @public @typedef {object} Visible */\nconst marker=1;\n}','unsupported_nested_public_typedef')

    def test_explicit_reexport_graph_and_local_process_boundary(self):
        a=self.source('export const original=1;','src/contracts/types.js')
        b=self.source("export { original as copied } from '../contracts/types.js';")
        report=x.extract(self.root,sorted([a,b]),sorted([a,b]))
        self.assertEqual([(f['symbols'][0]['name'],f['symbols'][0]['process'],f['symbols'][0]['line']) for f in report['files']],
                         [('original','contracts',1),('copied','main',1)])

    def test_reexport_ambiguities_abstain(self):
        self.refusal("export * from './x.js';",'export_star_or_namespace_unsupported')
        self.refusal("export * as ns from './x.js';",'export_star_or_namespace_unsupported')
        self.refusal("export { x } from './missing.js';",'unlisted_or_unresolved_reexport')
        self.refusal("export { x } from 'external';",'ambiguous_reexport')
        a=self.source("export { name } from './b.js';",'src/main/a.js')
        b=self.source("export { name } from './a.js';",'src/main/b.js')
        with self.assertRaises(x.ExtractionError) as error:x.extract(self.root,[a,b],[a,b])
        self.assertEqual(error.exception.code,'cyclic_reexport')

    def test_computed_and_unsupported_export_syntax_abstains(self):
        cases=["module.exports[name]=1;", "exports['x']=1;", 'module.exports={...other};', 'module.exports={__proto__:unknown};',
               'module.exports=unknown;', 'module["exports"].x=1;', 'const m=module;m.exports.x=1;', 'this.x=1;', 'if (condition) exports.x=1;',
               'function f(){ module.exports.x=1; }','const alias=exports;alias.x=1;',
               'exports.x=1;exports.x=2;', 'module.exports={};exports.x=1;',
               'export const x=1;module.exports.x=2;', 'export const {x}=other;',
               'export interface Shape {}', 'function nested(){export const x=1;}','export const x: number=1;', 'export const x = <View/>;',
               'export function broken( {', 'export { missing as name };']
        for source in cases:
            with self.subTest(source=source):self.refusal(source)

    def test_each_process_boundary(self):
        paths=[self.source('export const same=1;',f'src/{process}/sample.js') for process in ['contracts','main','preload','renderer']]
        report=x.extract(self.root,paths,paths)
        self.assertEqual([f['symbols'][0]['process'] for f in report['files']],['contracts','main','preload','renderer'])
        self.assertEqual(len({f['symbols'][0]['symbol'] for f in report['files']}),4)
        self.source('export const wrong=1;','other/sample.js')
        with self.assertRaises(x.ExtractionError) as error:x.extract(self.root,['other/sample.js'],['other/sample.js'])
        self.assertEqual(error.exception.code,'unknown_process_boundary')

    def test_unlisted_source_is_not_read_by_python_or_ast_process(self):
        self.source("import './hidden.js';\nexport function real() {}")
        hidden=self.source("module.exports[name]=unknown;throw Error('must not execute');",'src/main/hidden.js')
        original=Path.read_bytes
        def guarded(path):
            self.assertNotEqual(path,self.root/hidden)
            return original(path)
        # Instrument the AST process itself, not just the Python byte reader.
        guard='''const fsGuard=require('node:fs'), pathGuard=require('node:path');
const originalRead=fsGuard.readFileSync;
fsGuard.readFileSync=function(path,...args){
 if(typeof path==='string'&&pathGuard.resolve(path)===%s)throw Error('unlisted source read');
 return originalRead.call(this,path,...args);
};\n''' % json.dumps(str(self.root/hidden))
        with mock.patch.object(Path,'read_bytes',guarded),mock.patch.object(x,'AST_DRIVER',guard+x.AST_DRIVER):
            result=x.extract(self.root,[self.path],sorted([hidden,self.path]))
        self.assertEqual([r['name'] for r in result['files'][0]['symbols']],['real'])
        self.source("export { missing } from './hidden.js';")
        with mock.patch.object(Path,'read_bytes',guarded),mock.patch.object(x,'AST_DRIVER',guard+x.AST_DRIVER),self.assertRaises(x.ExtractionError) as error:
            x.extract(self.root,[self.path],sorted([hidden,self.path]))
        self.assertEqual(error.exception.code,'unlisted_or_unresolved_reexport')

    def test_sources_are_parsed_never_evaluated(self):
        self.assertEqual([r['name'] for r in self.extract('throw new Error("source must never run");\nexport const known=1;')],['known'])

    def test_byte_hashes_before_decode_and_crlf_coordinates(self):
        self.source('placeholder')
        raw=b'// comment\r\nexport const real=1;\r\n'
        (self.root/self.path).write_bytes(raw)
        report=x.extract(self.root,[self.path],[self.path])['files'][0]
        self.assertEqual(report['source_sha256'],hashlib.sha256(raw).hexdigest())
        self.assertEqual(report['symbols'][0]['line'],2)
        for separator in ['\r','\u2028','\u2029']:
            (self.root/self.path).write_bytes(('// comment'+separator+'export const real=1;').encode())
            self.assertEqual(x.extract(self.root,[self.path],[self.path])['files'][0]['symbols'][0]['line'],2)
        observed=[];original=hashlib.sha256
        class BytesEvidence(bytes):
            def decode(data,*args,**kwargs):
                self.assertEqual(observed,['hashed']);return super().decode(*args,**kwargs)
        def digest(data):observed.append('hashed');return original(data)
        with mock.patch.object(Path,'read_bytes',return_value=BytesEvidence(b'\xff')),mock.patch.object(x.hashlib,'sha256',side_effect=digest):
            with self.assertRaises(x.ExtractionError) as error:x.extract(self.root,[self.path],[self.path])
        self.assertEqual(error.exception.code,'invalid_utf8')

    def test_paths_version_inventory_and_symlinks_reject(self):
        self.source('export const real=1;')
        for paths,inventory in [([], [self.path]),([self.path,self.path],[self.path]),
                                ([self.path],[]),([self.path],['src/main/other.js']),
                                (['src/main/../sample.js'],[self.path]),(['/absolute.js'],[self.path]),
                                (['src/main/sample.ts'],['src/main/sample.ts'])]:
            with self.subTest(paths=paths),self.assertRaises(x.ExtractionError):x.extract(self.root,paths,inventory)
        other=self.source('export const other=1;','src/main/other.js')
        with self.assertRaises(x.ExtractionError):x.extract(self.root,[self.path,other],sorted([self.path,other]))
        alias=self.root/'src/main/alias.js';alias.symlink_to(self.root/self.path)
        with self.assertRaises(x.ExtractionError):x.extract(self.root,['src/main/alias.js'],['src/main/alias.js'])

    def test_missing_runtime_and_wrong_parser_refuse(self):
        self.source('export const real=1;')
        with mock.patch.dict(os.environ,{'ROUTER_NODE':''}),mock.patch.object(x.shutil,'which',return_value=None):
            with self.assertRaises(x.ExtractionError) as error:x.extract(self.root,[self.path],[self.path])
        self.assertEqual(error.exception.code,'node24_unavailable')
        with mock.patch.object(x.subprocess,'run',return_value=subprocess.CompletedProcess([],0,'v18.0.0\n','')):
            with self.assertRaises(x.ExtractionError) as error:x.extract(self.root,[self.path],[self.path])
        self.assertEqual(error.exception.code,'unsupported_node_runtime')
        fake=self.root/'node_modules/typescript/lib/typescript.js';fake.parent.mkdir(parents=True);fake.write_text('module.exports={version:"0.0.0"};')
        with mock.patch.object(x,'ROOT',self.root),self.assertRaises(x.ExtractionError) as error:x.extract(self.root,[self.path],[self.path])
        self.assertEqual(error.exception.code,'unsupported_parser_version')

    def test_cli_determinism_and_hashed_explicit_sources(self):
        command=[sys.executable,'-B','tools/extract_js.py','--files',*FILES]
        if os.environ.get('CAFE_ROUTER_INVENTORY'):command+=['--inventory',os.environ['CAFE_ROUTER_INVENTORY']]
        runs=[subprocess.run(command,cwd=ROOT,capture_output=True) for _ in range(2)]
        self.assertEqual([v.returncode for v in runs],[0,0],runs[0].stderr.decode())
        self.assertEqual(runs[0].stdout,runs[1].stdout);self.assertEqual(runs[0].stderr,b'')
        self.assertTrue(runs[0].stdout.endswith(b'\n'))
        data=json.loads(runs[0].stdout)
        self.assertEqual({f['path']:f['symbols'] for f in data['files']},expected_inventory())

    def test_js_qualification_requires_executed_passing_nonempty_test(self):
        selection='tests/bootstrap/test_extract_js.py'
        for check in [q.Check(selection,True,0,1),q.Check(selection,False,1,1),q.Check(selection,False,0,0)]:
            def run(root,directory,pattern):return check if pattern=='test_extract_js.py' else q.Check(pattern,True,0,1)
            with self.subTest(check=check),mock.patch.object(q,'run_selection',side_effect=run) as called, \
                    mock.patch.object(q,'load_inventory',return_value=(['fixture'],'fixture')), \
                    mock.patch.object(q,'check_ownership',return_value=q.Check('ownership',True)):
                legs,source=q.collect(self.root);report=q.summarize(legs,source);q.validate_report(report,legs)
                self.assertTrue(report['legs']['extractor_js']['implemented'])
                self.assertEqual(report['legs']['extractor_js']['status'],'pass' if check.passed else 'fail')
                self.assertEqual(report['verdict'],'router available (uncalibrated)' if check.passed else 'router unavailable');self.assertEqual(q.exit_code(legs),0 if check.passed else 1)
                called.assert_any_call(self.root,'tests/bootstrap','test_extract_js.py')


if __name__=='__main__':unittest.main()
