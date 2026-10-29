// Actual production shell/controller/authority in private Xvfb; native picker is
// injected test input. Linux capture remains unsupported, no Windows-ready claim.
'use strict';
const fs=require('node:fs'),path=require('node:path'),os=require('node:os'),crypto=require('node:crypto'),{spawnSync}=require('node:child_process');
async function child(){
 const electron=require('electron'),repo=process.env.CAFE_UI_REPO,out=process.env.CAFE_UI_OUT,appRoot=process.env.CAFE_UI_APP;
 if(!repo||!out||!appRoot||process.versions.electron!=='44.9.2')throw Error('UI_SMOKE_ENV_REJECTED');
 electron.app.setPath('userData',path.join(appRoot,'user-data'));electron.app.disableHardwareAcceleration();
 const selected=path.join(appRoot,'selected');fs.mkdirSync(selected,{mode:0o700});
 electron.dialog.showOpenDialog=async(_window,options)=>options.properties.includes('openDirectory')?{canceled:false,filePaths:[selected]}:{canceled:true,filePaths:[]};
 const runtime=require(path.join(repo,'src/main/runtimeBackend.js')),realFactory=runtime.createPackagedBackend;let backend,starts=0;
 runtime.createPackagedBackend=options=>{starts++;backend=realFactory(options);return backend;};
 const created=new Promise(resolve=>electron.app.once('browser-window-created',(_event,win)=>resolve(win)));
 require(path.join(repo,'src/main/index.js'));const win=await created;
 async function wait(script){for(let n=0;n<300;n++){try{if(await win.webContents.executeJavaScript(script))return;}catch{}await new Promise(resolve=>setTimeout(resolve,30));}throw Error('UI_SMOKE_STATE_TIMEOUT');}
 await wait("window.cafe && window.cafe.getSnapshot().then(s=>s.availability==='available' && s.state.platform==='unsupported')");win.setSize(1280,900);win.show();
 const pid=backend.getStatus().pid;if(!(pid>1&&pid!==process.pid))throw Error('UI_AUTHORITY_PID_INVALID');
 async function create(label){await win.webContents.executeJavaScript(`(()=>{const field=document.getElementById('cafe-person-input');field.value=${JSON.stringify(label)};field.dispatchEvent(new Event('input',{bubbles:true}));})()`);await win.webContents.executeJavaScript("[...document.querySelectorAll('button')].find(b=>b.textContent.trim()==='New capsule').click()");await wait('document.body.innerText.includes('+JSON.stringify(label)+')');}
 await create('Synthetic guest one');await wait("window.cafe.getSnapshot().then(s=>s.revision==='1')");
 await win.webContents.executeJavaScript("[...document.querySelectorAll('button')].find(b=>b.textContent.trim()==='Choose folder').click()");await wait("window.cafe.getSnapshot().then(s=>s.revision==='3'&&s.state.folder_configured===true)");await wait("document.body.innerText.includes('Output folder: Configured')");
 win.webContents.reload();await wait("window.cafe && window.cafe.getSnapshot().then(s=>s.revision==='3')");await wait("document.body.innerText.includes('Synthetic guest one')");
 await create('Synthetic guest two');await wait("window.cafe.getSnapshot().then(s=>s.revision==='4')");
 if(starts!==1||backend.getStatus().pid!==pid)throw Error('UI_DUPLICATED_AUTHORITY');
 fs.mkdirSync(out,{recursive:true});const screens=[];
 for(const theme of ['dark','light']){
  if(theme==='light')await win.webContents.executeJavaScript("[...document.querySelectorAll('button')].find(b=>b.textContent.includes('Switch to')).click()");
  await win.webContents.executeJavaScript('new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))');
  const actual=await win.webContents.executeJavaScript(`(()=>({theme:document.documentElement.getAttribute('data-cafe-theme')||'dark',background:getComputedStyle(document.body).backgroundColor,font:getComputedStyle(document.body).fontFamily,text:document.body.innerText,bridge:Object.keys(window.cafe).sort(),start:[...document.querySelectorAll('button')].filter(b=>b.textContent.includes('Start and check hardware')).map(b=>b.disabled)}))()`);
  if(actual.theme!==theme||!actual.text.includes('Synthetic guest two')||!actual.text.includes('Configured')||actual.bridge.join(',')!=='getSnapshot,onSnapshot,submitIntent'||/OpenAI|MetaViewer|\bTerminal\b/.test(actual.text)||/S[oö]hne|Signifier|\bInter\b/i.test(actual.font)||actual.start.some(value=>!value))throw Error('UI_RENDERER_CONTRACT_REJECTED');
  const image=await win.webContents.capturePage();if(image.isEmpty())throw Error('UI_SCREEN_EMPTY');fs.writeFileSync(path.join(out,theme+'.png'),image.toPNG());fs.writeFileSync(path.join(out,theme+'.json'),JSON.stringify(actual,null,2)+'\n');screens.push({theme,background:actual.background,size:image.getSize()});
 }
 if(screens[0].background===screens[1].background)throw Error('UI_THEME_UNCHANGED');
 fs.writeFileSync(path.join(out,'gate.json'),JSON.stringify({status:'pass',actual_authority:true,actual_production_factory:true,renderer_reload_same_pid:true,capsules_created:2,chosen_directory_committed:true,manual_revisions:['0','1','3','4'],picker:'injected test-only directory result',native_capture:'unsupported_linux',native_windows:'pending',screens},null,2)+'\n');
 win.close();electron.app.exit(0);
}
function parent(){
 const args=process.argv.slice(2);if(args.length!==6||args[0]!=='--electron'||args[2]!=='--authority'||args[4]!=='--out')throw Error('UI_SMOKE_USAGE');const repo=path.resolve(__dirname,'..'),electronBin=path.resolve(args[1]),authority=path.resolve(args[3]),out=path.resolve(args[5]);for(const file of [electronBin,authority])if(!fs.statSync(file).isFile())throw Error('UI_SMOKE_TOOL_ABSENT');
 // Chromium uses Unix sockets in its temporary/user-data directories. Keep
 // this Linux-only fixture canonical and short even when a judge has a long
 // TMPDIR. This is never an authority storage path supplied by the renderer.
 const requested=fs.realpathSync(os.tmpdir());
 const temporaryRoot=process.platform==='linux'&&Buffer.byteLength(requested)>48?fs.realpathSync('/tmp'):requested;
 if(!fs.statSync(temporaryRoot).isDirectory()||process.platform==='linux'&&Buffer.byteLength(temporaryRoot)>48)throw Error('UI_SMOKE_TEMP_ROOT_REJECTED');
 const temp=fs.mkdtempSync(path.join(temporaryRoot,'cafe-ui-'));fs.chmodSync(temp,0o700);
 try{const runtime=path.join(temp,'electron'),app=path.join(temp,'app');fs.cpSync(path.dirname(electronBin),runtime,{recursive:true});fs.mkdirSync(path.join(app,'dist'),{recursive:true});for(const name of ['renderer','preload'])fs.cpSync(path.join(repo,'dist',name),path.join(app,'dist',name),{recursive:true});
  const resources=path.join(runtime,'resources');fs.mkdirSync(path.join(resources,'backend'),{recursive:true});const binary=path.join(resources,'backend/cafe-core.exe');fs.copyFileSync(authority,binary);fs.chmodSync(binary,0o700);fs.writeFileSync(path.join(resources,'cafe-build.json'),JSON.stringify({schema:'cafe.unsigned-windows-app.v1',authority_sha256:crypto.createHash('sha256').update(fs.readFileSync(binary)).digest('hex')}));
  fs.writeFileSync(path.join(app,'package.json'),JSON.stringify({name:'cafe-ui-smoke',version:'0.1.0-rc.1',main:__filename}));fs.mkdirSync(out,{recursive:true});
  const env={...process.env,TMPDIR:temp,TMP:temp,TEMP:temp,CAFE_UI_REPO:repo,CAFE_UI_OUT:out,CAFE_UI_APP:app};delete env.ELECTRON_RUN_AS_NODE;delete env.NODE_OPTIONS;
  const result=spawnSync('xvfb-run',['-a',path.join(runtime,path.basename(electronBin)),app],{env,encoding:'utf8',timeout:120000,maxBuffer:1024*1024});fs.writeFileSync(path.join(out,'process.json'),JSON.stringify({status:result.status,signal:result.signal,error:result.error?.code||null})+'\n');fs.writeFileSync(path.join(out,'stderr'),result.stderr||'');fs.writeFileSync(path.join(out,'stdout'),result.stdout||'');if(result.status!==0)throw Error('UI_SMOKE_FAILED');console.log(JSON.stringify({status:'pass',screens:2,actual_authority:true,native_windows:'pending'}));
 }finally{fs.rmSync(temp,{recursive:true,force:true});}
}
if(process.versions.electron){child().catch(()=>{console.error('UI_CHILD_SMOKE_FAILED');require('electron').app.exit(1);});}else{try{parent();}catch(error){console.error(error.message);process.exitCode=1;}}
