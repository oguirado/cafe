// @ts-check
'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { noticeFiles } = require('../../tools/build_windows_app');

/** @param {(root:string)=>void} body */
function fixture(body) {
  const root = fs.realpathSync.native(fs.mkdtempSync(path.join(os.tmpdir(), 'cafe-notices-')));
  try { body(root); } finally { fs.rmSync(root, { recursive:true, force:true }); }
}
test('actual LICENSE is returned once using its canonical spelling', () => fixture(root => {
  fs.writeFileSync(path.join(root,'LICENSE'),'license text');
  const files = noticeFiles(root);
  assert.deepEqual(files,[fs.realpathSync.native(path.join(root,'LICENSE'))]);
  assert.equal(files.length,1);
}));
test('mixed-case names use actual spelling and stable ordering', () => fixture(root => {
  for (const name of ['Notice.txt','LiCeNsE.Md','README.md']) fs.writeFileSync(path.join(root,name),'text');
  assert.deepEqual(noticeFiles(root).map(file=>path.basename(file)),['LiCeNsE.Md','Notice.txt']);
  assert.deepEqual(noticeFiles(root),noticeFiles(root));
}));
test('a directory named LICENSE is rejected as a package input', () => fixture(root => {
  fs.mkdirSync(path.join(root,'LICENSE'));
  assert.throws(()=>noticeFiles(root),/PACKAGE_INPUT_REJECTED/);
}));
test('unapproved filenames are never selected', () => fixture(root => {
  for (const name of ['LICENSE.exe','NOTICE.json','other']) fs.writeFileSync(path.join(root,name),'text');
  assert.deepEqual(noticeFiles(root),[]);
}));
