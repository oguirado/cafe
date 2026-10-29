// @ts-check
'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { createHash } = require('node:crypto');
const { spawnSync } = require('node:child_process');
const { applyShellFuses } = require('../../tools/apply_shell_fuses');
const { REQUIRED } = require('../../tools/check_shell_fuses');
const ROOT = path.resolve(__dirname, '../..');
// Measured from the official Electron44.9.2 release SHASUMS256 document.
const ELECTRON_ARCHIVE_SHA256 = '1a9ed7f4e70291c95ed9479d7fcd384a2d9cf062e5fb7ce846b119c7c63036c3';
const FIXTURE_EXE = 'cafe-shell-fixture.exe';
let work, python, archives;
function sha(file) { return createHash('sha256').update(fs.readFileSync(file)).digest('hex'); }
const ZIP = String.raw`
from pathlib import Path
import sys,zipfile
root=Path(sys.argv[1]);target=Path(sys.argv[2])
with zipfile.ZipFile(target,'x',compression=zipfile.ZIP_DEFLATED,compresslevel=4) as archive:
 for source in sorted(root.rglob('*')):
  if source.is_symlink():raise ValueError('fixture_symlink')
  if source.is_file():archive.write(source,source.relative_to(root).as_posix())
`;
function runPython(args) {
  const result = spawnSync(python, args, { cwd: ROOT, encoding: 'utf8', timeout: 180000 });
  if (result.error) throw new Error('FIXTURE_PYTHON_REJECTED');
  return result;
}
function inspect(file) {
  const result = runPython(['-B','tools/check_shell_archive.py',file,'--exe',FIXTURE_EXE,'--node',process.execPath]);
  assert.equal(result.stderr, '');
  return { rc: result.status, data: JSON.parse(result.stdout) };
}

test.before(async () => {
  const directory = process.env.CAFE_ELECTRON_ZIP_DIR;
  python = process.env.CAFE_TEST_PYTHON;
  assert.ok(directory && path.isAbsolute(directory), 'BLOCKED: approved Electron archive cache absent');
  assert.ok(python && path.isAbsolute(python), 'BLOCKED: explicit test Python interpreter absent');
  const original = path.join(directory,'electron-v44.9.2-win32-x64.zip');
  assert.ok(fs.existsSync(original) && fs.statSync(original).isFile(), 'BLOCKED: approved Electron archive absent');
  assert.equal(sha(original), ELECTRON_ARCHIVE_SHA256, 'BLOCKED: approved Electron archive checksum mismatch');
  work = fs.mkdtempSync(path.join(os.tmpdir(),'cafe-shell-fuses-'));
  try {
    const source = path.join(work,'source');fs.mkdirSync(source);
    for (const name of ['package.json','src/main/index.js','src/main/shellSecurity.js','src/main/backendSupervisor.js','src/main/rendererAssets.js','dist/preload/index.cjs','dist/renderer/manifest.json','dist/renderer/renderer.js','dist/renderer/renderer.css']) {
      const target=path.join(source,name);fs.mkdirSync(path.dirname(target),{recursive:true});fs.copyFileSync(path.join(ROOT,name),target);
    }
    const asar = await import('@electron/asar');
    const prebuilt = path.join(work,'app.asar');await asar.createPackage(source,prebuilt);
    const {packager}=await import('@electron/packager');
    const built=await packager({dir:source,name:'cafe-shell-fixture',appVersion:'0.0.0',
      electronVersion:'44.9.2',electronZipDir:directory,platform:'win32',arch:'x64',
      prebuiltAsar:prebuilt,out:path.join(work,'package'),prune:false,quiet:true});
    // The version above labels this test fixture; it does not select a product release version.
    assert.equal(built.length,1);
    const unfused=built[0],fused=path.join(work,'fused');fs.cpSync(unfused,fused,{recursive:true,errorOnExist:true});
    await applyShellFuses(path.join(fused,FIXTURE_EXE));
    assert.equal(sha(path.join(unfused,'resources/app.asar')),sha(path.join(fused,'resources/app.asar')));
    archives={fused:path.join(work,'fused.zip'),unfused:path.join(work,'unfused.zip')};
    for (const [variant,folder] of [['fused',fused],['unfused',unfused]]) assert.equal(runPython(['-B','-c',ZIP,folder,archives[variant]]).status,0);
  } catch (error) { fs.rmSync(work,{recursive:true,force:true});work=null;throw error; }
});
test.after(() => { if(work)fs.rmSync(work,{recursive:true,force:true}); });

test('explicit operator five and reference file-protocol fuse policy stay closed', () => {
  assert.deepEqual(REQUIRED,{RunAsNode:false,EnableNodeOptionsEnvironmentVariable:false,
    EnableNodeCliInspectArguments:false,EnableEmbeddedAsarIntegrityValidation:true,
    OnlyLoadAppFromAsar:true,GrantFileProtocolExtraPrivileges:false});
});
test('actual ZIP-extracted Windows executable has required fuses and embedded packed-ASAR integrity', () => {
  const result=inspect(archives.fused);assert.equal(result.rc,0);assert.equal(result.data.status,'pass');
  assert.equal(result.data.archive_executable_extracted,true);assert.equal(result.data.structure,'pass');
  assert.equal(result.data.embedded_asar_integrity,'verified');assert.equal(result.data.asar.unpacked_main_fallback,false);
  for(const check of result.data.checks)assert.equal(check.pass,true);
});
test('same valid package UNFUSED fails specifically on real fuse policy', () => {
  const result=inspect(archives.unfused);assert.equal(result.rc,1);assert.equal(result.data.status,'fail');
  assert.equal(result.data.code,'FUSE_POLICY_REJECTED');assert.equal(result.data.structure,'pass');
  assert.equal(result.data.embedded_asar_integrity,'verified');assert.equal(result.data.archive_executable_extracted,true);
  assert.ok(result.data.checks.some((check)=>check.pass===false));
});
