// @ts-check
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const crypto = require('node:crypto');
const { spawnSync } = require('node:child_process');
const { applyShellFuses } = require('./apply_shell_fuses');

const ELECTRON_SHA256 = '1a9ed7f4e70291c95ed9479d7fcd384a2d9cf062e5fb7ce846b119c7c63036c3';
const APP_FILES = ['package.json', 'src/main/index.js', 'src/main/shellSecurity.js',
  'src/main/backendSupervisor.js', 'src/main/runtimeBackend.js', 'src/main/rendererAssets.js', 'src/main/ffmpegSelection.js', 'dist/preload/index.cjs',
  'dist/renderer/manifest.json', 'dist/renderer/renderer.js', 'dist/renderer/renderer.css', 'LICENSE', 'NOTICE',
  'ui/src/design/vendor/open-design-openai/LICENSE', 'ui/src/design/vendor/open-design-openai/PROVENANCE.md', 'docs/ui/PROVENANCE.md'];

/** @param {string} file */
function digest(file) {
  const fd = fs.openSync(file, 'r'), buffer = Buffer.alloc(1024 * 1024), hash = crypto.createHash('sha256');
  try { for (;;) { const n = fs.readSync(fd, buffer, 0, buffer.length, null); if (!n) break; hash.update(buffer.subarray(0,n)); } }
  finally { fs.closeSync(fd); }
  return hash.digest('hex');
}
/** @param {string} file */
function regular(file) {
  if (!path.isAbsolute(file) || fs.realpathSync.native(file) !== file || !fs.lstatSync(file).isFile() || fs.lstatSync(file).isSymbolicLink()) throw new Error('PACKAGE_INPUT_REJECTED');
  return file;
}
/** @param {string} name */
function relative(name) {
  if (!/^[A-Za-z0-9_@./-]+$/.test(name) || name.startsWith('/') || name.split('/').some(p => !p || p === '.' || p === '..')) throw new Error('PACKAGE_SOURCE_REJECTED');
  return name;
}
/** @param {string} python @param {string[]} argv @param {string} root */
function runPython(python, argv, root) {
  const result = spawnSync(regular(python), argv, { cwd: root, encoding: 'utf8', timeout: 900000, maxBuffer: 2 * 1024 * 1024, windowsHide: true });
  if (result.error || result.status !== 0) throw new Error('PACKAGE_PYTHON_GATE_BLOCKED');
  return result.stdout;
}

/** @param {Record<string, unknown>} request */
async function buildCheckedWindowsApp(request) {
  const keys = ['source','output','electronZipDir','pythonTool','pythonArchive','websocketsWheel','authorityExe','authoritySha256','inventory'];
  if (Object.keys(request).sort().join(',') !== [...keys].sort().join(',') || keys.filter(k => k !== 'inventory').some(k => typeof request[k] !== 'string')) throw new Error('PACKAGE_REQUEST_REJECTED');
  const root = /** @type {string} */ (request.source), output = /** @type {string} */ (request.output);
  if (!path.isAbsolute(root) || fs.realpathSync.native(root) !== root || !fs.statSync(root).isDirectory() || !path.isAbsolute(output) || fs.existsSync(output) || fs.realpathSync.native(path.dirname(output)) !== path.dirname(output)) throw new Error('PACKAGE_REQUEST_REJECTED');
  const inventory = request.inventory;
  if (!Array.isArray(inventory) || inventory.length > 4096 || inventory.some(n => typeof n !== 'string') || new Set(inventory).size !== inventory.length) throw new Error('PACKAGE_INVENTORY_REJECTED');
  for (const name of inventory) relative(name);
  const python = /** @type {string} */ (request.pythonTool);
  const archive = path.join(/** @type {string} */ (request.electronZipDir), 'electron-v44.9.2-win32-x64.zip');
  if (digest(regular(archive)) !== ELECTRON_SHA256) throw new Error('ELECTRON_PIN_REJECTED');
  const pyModule = regular(path.join(root, 'tools/package_windows.py'));
  const authority = regular(/** @type {string} */ (request.authorityExe));
  if (!/^[0-9a-f]{64}$/.test(/** @type {string} */ (request.authoritySha256)) || digest(authority) !== request.authoritySha256) throw new Error('AUTHORITY_PIN_REJECTED');
  const validateAuthority = "import importlib.util,sys; s=importlib.util.spec_from_file_location('p',sys.argv[1]);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);m.authority_input(sys.argv[2],sys.argv[3])";
  runPython(python, ['-B','-c',validateAuthority,pyModule,authority,/** @type {string} */ (request.authoritySha256)], root);
  const work = fs.mkdtempSync(path.join(os.tmpdir(), 'cafe-windows-package-'));
  /** @type {{dev:bigint,ino:bigint}|null} */
  let published = null;
  let phase = "source";
  try {
    const source = path.join(work,'source');fs.mkdirSync(source);
    const sourceHashes = {};
    for (const name of APP_FILES) {
      if (name !== 'package.json' && !name.startsWith('dist/') && !inventory.includes(name)) throw new Error('PACKAGE_INVENTORY_REJECTED');
      const file=regular(path.join(root,relative(name))), target=path.join(source,name);fs.mkdirSync(path.dirname(target),{recursive:true});fs.copyFileSync(file,target);
      if (digest(file)!==digest(target)) throw new Error('PACKAGE_SOURCE_CHANGED');sourceHashes[name]=digest(target);
    }
    const manifest=JSON.parse(fs.readFileSync(path.join(source,'package.json'),'utf8'));
    if (manifest.name!=='cafe-shell' || manifest.version!=='0.1.0' || manifest.main!=='src/main/index.js') throw new Error('PACKAGE_VERSION_REJECTED');
    phase = 'private_runtime';
    const runtime=path.join(work,'private-runtime');
    runPython(python,['-B',pyModule,'--python-archive',/** @type {string} */ (request.pythonArchive),'--websockets-wheel',/** @type {string} */ (request.websocketsWheel),'--out',runtime],root);
    phase = 'asar';
    const asar=await import('@electron/asar');const packed=path.join(work,'app.asar');await asar.createPackage(source,packed);
    phase = 'electron';
    const {packager}=await import('@electron/packager');
    const builds=await packager({dir:source,name:'Cafe',executableName:'Cafe',appVersion:'0.1.0-rc.1',electronVersion:'44.9.2',electronZipDir:/** @type {string} */ (request.electronZipDir),platform:'win32',arch:'x64',prebuiltAsar:packed,out:path.join(work,'package'),prune:false,quiet:true});
    if (builds.length!==1) throw new Error('PACKAGE_LAYOUT_REJECTED');
    const stage=builds[0], resources=path.join(stage,'resources');
    phase = 'notices';
    const lock=JSON.parse(fs.readFileSync(path.join(root,'package-lock.json'),'utf8'));
    const modules=fs.realpathSync.native(path.join(root,'node_modules'));
    const notices=path.join(resources,'third-party-notices');fs.mkdirSync(notices);
    for (const [key,row] of Object.entries(lock.packages)) {
      if (!key) continue;
      relative(key);
      if (!fs.existsSync(path.join(root,key))) {
        if (row.optional===true) continue;
        throw new Error('NOTICE_PACKAGE_ABSENT');
      }
      const moduleRoot=fs.realpathSync.native(path.join(root,key));
      if (!moduleRoot.startsWith(modules+path.sep)) throw new Error('NOTICE_SOURCE_REJECTED');
      const installed=JSON.parse(fs.readFileSync(path.join(moduleRoot,'package.json'),'utf8'));
      if (installed.version!==row.version) throw new Error('NOTICE_PACKAGE_REJECTED');
      const target=path.join(notices,key.replaceAll('/', '_'));fs.mkdirSync(target);
      fs.writeFileSync(path.join(target,'package.json'),JSON.stringify({name:installed.name,version:installed.version,license:installed.license},null,2)+'\n');
      for (const name of ['LICENSE','LICENSE.md','LICENSE.txt','license','license.md','LICENSE-MIT','NOTICE','NOTICE.txt']) {
        const file=path.join(moduleRoot,name);
        if (!fs.existsSync(file)) continue;
        const source=regular(file);
        if (fs.statSync(source).size>4*1024*1024) throw new Error('NOTICE_SIZE_REJECTED');
        fs.copyFileSync(source,path.join(target,name));
      }
    }
    phase = 'runtime_copy';
    fs.cpSync(path.join(runtime,'python'),path.join(resources,'python'),{recursive:true,errorOnExist:true});
    fs.copyFileSync(path.join(runtime,'runtime-manifest.json'),path.join(resources,'runtime-manifest.json'));
    fs.mkdirSync(path.join(resources,'backend'));fs.copyFileSync(authority,path.join(resources,'backend/cafe-core.exe'));
    if (digest(path.join(resources,'backend/cafe-core.exe'))!==request.authoritySha256) throw new Error('AUTHORITY_CHANGED_DURING_COPY');
    phase = 'connectors';
    const connectors=inventory.filter(name => name.startsWith('connectors/') && name.endsWith('.py'));
    for (const required of ['connectors/cafe/mcp_server.py','connectors/cafe/mcp_queries.py','connectors/cafe/twitch_chat.py','connectors/cafe/twitch_storage.py','connectors/cafe/obs_bridge.py']) if (!connectors.includes(required)) throw new Error('MVP_CONNECTOR_ABSENT');
    for (const name of connectors) {
      const file=regular(path.join(root,name)), target=path.join(resources,name);fs.mkdirSync(path.dirname(target),{recursive:true});fs.copyFileSync(file,target);
      if (digest(file)!==digest(target)) throw new Error('PACKAGE_SOURCE_CHANGED');sourceHashes[name]=digest(target);
    }
    phase = 'fuses';
    const fuses=await applyShellFuses(path.join(stage,'Cafe.exe'));
    const receipt={schema:'cafe.unsigned-windows-app.v1',version:'0.1.0-rc.1',signed:false,ffmpeg:'external_user_path_and_hash_only',ffmpeg_config:{ffmpeg_binary:null},authority_sha256:request.authoritySha256,source_files:sourceHashes,fuses,windows_verification:'pending',product_runtime_binding:'pending'};
    fs.writeFileSync(path.join(resources,'cafe-build.json'),JSON.stringify(receipt,null,2)+'\n');
    phase = 'zip';
    const zipCode="import importlib.util,json,sys; s=importlib.util.spec_from_file_location('p',sys.argv[1]);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);print(json.dumps(m.write_archive(sys.argv[2],sys.argv[3])))";
    const temporaryZip=path.join(work,'verified-package.zip');
    const result=JSON.parse(runPython(python,['-B','-c',zipCode,pyModule,stage,temporaryZip],root));
    phase = 'archive_security';
    const inspect=JSON.parse(runPython(python,['-B',path.join(root,'tools/check_shell_archive.py'),temporaryZip,'--exe','Cafe.exe','--node',process.execPath],root));
    if (inspect.status!=='pass') throw new Error('PACKAGED_FUSES_REJECTED');
    phase = 'publish';
    const owned=fs.statSync(temporaryZip,{bigint:true});
    fs.linkSync(temporaryZip,output);
    published={dev:owned.dev,ino:owned.ino};
    return {schema:receipt.schema,status:'built_unsigned_native_pending',version:receipt.version,...result,windows_verification:'pending',product_runtime_binding:'pending',ffmpeg_bundled:false,archive_security:inspect};
  } catch {
    process.stderr.write(JSON.stringify({code:'WINDOWS_PACKAGE_BLOCKED',phase})+'\n');
    if (published && fs.existsSync(output)) {
      const current=fs.lstatSync(output,{bigint:true});
      if (!current.isSymbolicLink() && current.dev===published.dev && current.ino===published.ino) fs.unlinkSync(output);
    }
    throw new Error('WINDOWS_PACKAGE_BLOCKED');
  } finally { fs.rmSync(work,{recursive:true,force:true}); }
}

/** @param {Record<string, unknown>} request */
async function buildWindowsApp(request) {
  try { return await buildCheckedWindowsApp(request); }
  catch { throw new Error('WINDOWS_PACKAGE_BLOCKED'); }
}

async function main(args) {
  try {
    if (args.length!==1) throw new Error();const input=regular(args[0]);
    if (fs.statSync(input).size>64*1024) throw new Error();
    const request=JSON.parse(fs.readFileSync(input,'utf8'));const result=await buildWindowsApp(request);
    process.stdout.write(JSON.stringify(result)+'\n');return 0;
  } catch { process.stdout.write(JSON.stringify({schema:'cafe.unsigned-windows-app.v1',status:'blocked',code:'WINDOWS_PACKAGE_BLOCKED'})+'\n');return 1; }
}
exports.buildWindowsApp=buildWindowsApp;
exports.main=main;
if (require.main===module) main(process.argv.slice(2)).then(rc=>{process.exitCode=rc;});
