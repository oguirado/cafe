'use strict';
const test=require('node:test'),assert=require('node:assert/strict');
const {mount,button,textOf,squash,findAll,settle}=require('../fixtures/capsule_control.js');
const previousResizeObserver = globalThis.ResizeObserver;
const measurements = [];
globalThis.ResizeObserver = class {
 constructor(callback) { this.callback = callback; measurements.push(this); }
 observe(target) { this.target = target; this.resize(1280, 720); }
 resize(width, height) { this.callback([{ contentRect: { width, height } }]); }
 disconnect() { this.disconnected = true; }
};
test.after(() => {
 if (previousResizeObserver === undefined) delete globalThis.ResizeObserver;
 else globalThis.ResizeObserver = previousResizeObserver;
});
const galleryTiles = m => findAll(m.tree, n => n.props['data-capsule-id'] !== undefined);
const galleryObserver = () => measurements.find(o => !o.disconnected && String(o.target?.props.class).includes('cafe-gallery-viewport'));
const galleryLabels = m => Object.fromEntries(galleryTiles(m).map(n => [n.props['data-capsule-id'], n.props['aria-label']]));
function snapshot(ids=['cap-a'],revision='1') {
 const capsules={};for(const id of ids)capsules[id]={capsule_id:id,person_label:id==='cap-a'?'Synthetic Alpha':'Synthetic Beta',feeds:{game:{feed_id:'feed-game',role:'game',generation:'9007199254740993'}}};
 return {availability:'available',authority_epoch:'9007199254740993',revision,state:{runtime:'authority_ready',platform:'windows',capsules,windows:[],window_page:{offset:0,total:0,next_offset:null},recordings:[],folder_configured:true,recording_configured:true,obs_available:true,obs_configurable:true,chat_available:false,chat_configurable:true}};
}
test('one desktop exposes pairing and help navigation never starts or stops',async()=>{
 const m=await mount(async()=>snapshot());try{
 assert.equal(button(m.tree,'Capsules'),undefined);assert.equal(button(m.tree,'Diagnostics'),undefined);
 assert.equal(button(m.tree,'Nueva cápsula'),undefined);assert.equal(button(m.tree,'New capsule'),undefined);
 for(const label of ['Add window','Record all','Stop all','Pause previews','Settings','Help'])assert.ok(button(m.tree,label),label);
 assert.equal(button(m.tree,'Pause previews').props['aria-pressed'],true);
 assert.ok(button(m.tree,'REC'));assert.ok(button(m.tree,'OBS'));
 const fullText=squash(textOf(m.tree));
 assert.ok(fullText.includes('No preview'));
 assert.ok(fullText.includes('If a preview stops updating, restore the source window and retry. Open Diagnostics if it continues. Keys 1–9 focus a tile; Esc returns to the wall.'));
 assert.equal(fullText.includes('PENDING-LIVE'),false);
 assert.equal(galleryLabels(m)['cap-a'],'slot-1 · Synthetic Alpha');
 assert.equal(findAll(m.tree,n=>n.props.id==='cafe-window-browser').length,0);assert.equal(m.ui.probe.reads,0);
 await m.click('Help');await m.click('Diagnostics');assert.ok(button(m.tree,'Show diagnostics'));
 assert.equal(m.calls.intents,0);assert.equal(m.calls.detached,0);assert.equal(m.ui.probe.reads,0);
 }finally{m.done();}
});
test('slot display aliases remain stable after authoritative reorder',async()=>{
 const m=await mount(async()=>snapshot(['cap-a','cap-b']));try{
 const cards=()=>findAll(m.tree,n=>n.tag==='article').map(n=>squash(textOf(n)));
 assert.ok(cards().find(t=>t.includes('Synthetic Alpha')).includes('slot-1'));assert.ok(cards().find(t=>t.includes('Synthetic Beta')).includes('slot-2'));
 assert.deepEqual(galleryLabels(m),{'cap-a':'slot-1 · Synthetic Alpha','cap-b':'slot-2 · Synthetic Beta'});
 await m.push(snapshot(['cap-b','cap-a'],'2'));
 assert.ok(cards().find(t=>t.includes('Synthetic Alpha')).includes('slot-1'));assert.ok(cards().find(t=>t.includes('Synthetic Beta')).includes('slot-2'));
 assert.deepEqual(galleryLabels(m),{'cap-a':'slot-1 · Synthetic Alpha','cap-b':'slot-2 · Synthetic Beta'});
 assert.equal(m.calls.intents,0);
 }finally{m.done();}
});
test('consumed AI plan keeps pending and correlated result visible without granting new effects',async()=>{
 for(const [action,outcome,code] of [['Confirm','committed','organizer_plan_confirmed'],['Confirm','rejected','operator_frame_rejected'],['Reject','rejected','organizer_plan_rejected'],['Reject','rejected','operator_frame_rejected'],['Reject','committed','organizer_plan_confirmed']]){
  const initial=snapshot();initial.state.organizer_plan={plan_id:'plan-demo',folder_name:'Synthetic session',windows:[{title:'Synthetic Alpha'}],expires_unix_ms:Date.now()+60000};
  const m=await mount(async()=>initial,{submit:async()=>({queued:true})});
  try{
   assert.equal(m.calls.intents,0);
   const pending=button(m.tree,action).props.onClick();await settle(m.ui.nextTick);
   assert.equal(m.calls.intents,1);assert.equal(m.calls.requests[0].body.op,action==='Confirm'?'organizer_plan_confirm':'organizer_plan_reject');
   const consumed=snapshot(['cap-a'],'2');await m.push(consumed);
   assert.equal(button(m.tree,'Confirm'),undefined);
   assert.match(squash(textOf(m.tree)),/Waiting for the authority to confirm the AI plan request/);
   const unrelated=snapshot(['cap-a'],'3');unrelated.state.last_result={state:'committed',request_id:'other-request'};await m.push(unrelated);
   assert.match(squash(textOf(m.tree)),/Waiting for the authority/);
   const completed=snapshot(['cap-a'],'4');completed.state.last_result={state:outcome,code,request_id:m.calls.requests[0].request_id};await m.push(completed);
   await pending;await settle(m.ui.nextTick);
   const rendered=squash(textOf(m.tree));
   if(action==='Reject' && code==='organizer_plan_rejected'){assert.match(rendered,/AI plan rejected/);assert.doesNotMatch(rendered,/Authority refused|Check capsule status for execution results/);}
   else if(action==='Reject' && outcome==='committed'){assert.match(rendered,/AI plan request result could not be confirmed/);assert.doesNotMatch(rendered,/AI plan rejected|Check capsule status for execution results/);}
   else if(action==='Confirm' && outcome==='committed')assert.match(rendered,/AI plan request confirmed by the authority. Check capsule status for execution results/);
   else {assert.match(rendered,/Authority refused the request/);assert.doesNotMatch(rendered,/AI plan request confirmed|AI plan rejected/);}
   assert.equal(m.calls.intents,1);assert.doesNotMatch(rendered,/all stopped|all recorded/i);
  }finally{m.done();}
 }
});
test('REC sends authoritative capsule id and lossless clocks, queued is not Running',async()=>{
 const m=await mount(async()=>snapshot(),{submit:()=>Promise.resolve({queued:true})});try{await m.click('REC');assert.equal(m.calls.intents,1);assert.equal(m.calls.requests[0].body.capsule_id,'cap-a');assert.equal(m.calls.requests[0].body.op,'start_recording');assert.equal(m.calls.requests[0].authority_epoch,'9007199254740993');assert.equal(m.calls.requests[0].expected_revision,'1');assert.doesNotMatch(squash(textOf(m.tree)),/Recording state: Running/);}finally{m.done();}
});
test('stale readiness is revalidated before controller action even before DOM update',async()=>{
 const m=await mount(async()=>snapshot(),{submit:()=>Promise.resolve({queued:true})});try{
 const oldHandler=button(m.tree,'REC').props.onClick;const stale=snapshot(['cap-a'],'2');stale.state.recording_configured=false;
 await m.push(stale,false);oldHandler();await settle(m.ui.nextTick);assert.equal(m.ui.probe.actions.length,0);assert.equal(m.calls.intents,0);
 }finally{m.done();}
});
test('stale removed capsule cannot trigger a controller action',async()=>{
 const m=await mount(async()=>snapshot(),{submit:()=>Promise.resolve({queued:true})});try{const oldHandler=button(m.tree,'REC').props.onClick;await m.push(snapshot([],'2'),false);oldHandler();await settle(m.ui.nextTick);assert.equal(m.ui.probe.actions.length,0);assert.equal(m.calls.intents,0);}finally{m.done();}
});
test('pending REC rejects duplicate clicks, and no completion is invented',async()=>{
 let resolve;const pending=new Promise(done=>{resolve=done;});const m=await mount(async()=>snapshot(),{submit:()=>pending});try{const handler=button(m.tree,'REC').props.onClick;handler();handler();await settle(m.ui.nextTick);assert.equal(m.calls.intents,1);assert.equal(m.ui.probe.actions.length,1);assert.doesNotMatch(squash(textOf(m.tree)),/Recording state: Running/);resolve({queued:true});await settle(m.ui.nextTick);}finally{m.done();}
});
test('unavailable and known empty workspace are distinct and emit no mutation',async()=>{
 const empty=await mount(async()=>snapshot([]));try{assert.match(squash(textOf(empty.tree)),/No capsules/);assert.equal(empty.calls.intents,0);}finally{empty.done();}
 const absent=await mount(async()=>({availability:'unavailable'}));try{assert.match(squash(textOf(absent.tree)),/unavailable/i);assert.equal(button(absent.tree,'REC'),undefined);assert.equal(absent.calls.intents,0);}finally{absent.done();}
});

test('focus and resize preserve recording, preview nodes and authoritative capsules without effects',async()=>{
 const value=snapshot(['cap-a','cap-b','cap-c']);
 value.state.recordings=[{capsule_id:'cap-a',state:'Running',feeds:[]}];
 const m=await mount(async()=>value);try{
 const region=()=>findAll(m.tree,n=>n.props['aria-label']==='Capsules')[0];
 const originalTiles=galleryTiles(m);
 const originalVideos=findAll(m.tree,n=>n.tag==='video');
 assert.equal(originalVideos.length,3);
 const originalArticle=findAll(originalTiles[0],n=>n.tag==='article')[0];
 const observer=galleryObserver();
 const initialWidth=originalTiles[0].props.style.width;
 await m.click('Focus');
 assert.equal(region().props['data-focused-id'],'cap-a');
 assert.ok(parseFloat(originalTiles[0].props.style.width)>parseFloat(initialWidth));
 assert.equal(button(originalTiles[0],'Unfocus').props['aria-pressed'],true);
 assert.ok(button(originalTiles[0],'Stop'));
 assert.equal(button(originalTiles[0],'Remove').props.disabled,true);
 assert.equal(originalTiles[1].props.style.top,originalTiles[2].props.style.top,'others form one strip');
 const focusedWidth=originalTiles[0].props.style.width;
 observer.resize(800,600);await settle(m.ui.nextTick);
 assert.notEqual(originalTiles[0].props.style.width,focusedWidth);
 await m.click('Unfocus');
 assert.equal(region().props['data-focused-id'],'');
 assert.deepEqual(galleryTiles(m),originalTiles);
 assert.deepEqual(findAll(m.tree,n=>n.tag==='video'),originalVideos);
 assert.equal(findAll(originalTiles[0],n=>n.tag==='article')[0],originalArticle);
 assert.ok(button(originalTiles[0],'Stop'));
 assert.match(squash(textOf(button(originalTiles[0],'Stop').parent.parent.parent)),/Recording/);
 assert.equal(m.calls.intents,0);assert.equal(m.ui.probe.actions.length,0);
 m.done();assert.equal(observer.disconnected,true);
 }catch(error){m.done();throw error;}
});

test('measured gallery fits twelve tiles on resize and clears focus when its capsule is removed',async()=>{
 const ids=Array.from({length:12},(_,i)=>`cap-${i}`);
 const m=await mount(async()=>snapshot(ids));try{
 const observer=galleryObserver();
 for(const [width,height] of [[1280,720],[720,900],[640,360],[1920,500]]) {
  observer.resize(width,height);await settle(m.ui.nextTick);
  const boxes=galleryTiles(m).map(tile=>Object.fromEntries(Object.entries(tile.props.style).map(([k,v])=>[k,parseFloat(v)])));
  assert.equal(boxes.length,12);
  for(const box of boxes) {
   assert.ok(box.width>0);assert.ok(box.left>=-1e-8);assert.ok(box.top>=-1e-8);
   assert.ok(box.left+box.width<=width+1e-8,JSON.stringify({box,width,height}));assert.ok(box.top+box.height<=height+1e-8);
   assert.ok(Math.abs(box.width/box.height-16/9)<1e-8);
  }
  for(let i=0;i<boxes.length;i++)for(let j=i+1;j<boxes.length;j++) {
   const a=boxes[i],b=boxes[j];
   assert.ok(a.left+a.width<=b.left+1e-8 || b.left+b.width<=a.left+1e-8 || a.top+a.height<=b.top+1e-8 || b.top+b.height<=a.top+1e-8);
  }
 }
 await m.click('Focus');
 const remaining=galleryTiles(m).slice(1);
 assert.equal(new Set(remaining.map(n=>n.props.style.top)).size,1);
 const reordered=snapshot([...ids].reverse(),'2');await m.push(reordered);
 assert.equal(button(galleryTiles(m).find(n=>n.props['data-capsule-id']===ids[0]),'Unfocus').props['aria-pressed'],true);
 await m.push(snapshot(ids.slice(1),'3'));
 assert.equal(findAll(m.tree,n=>n.props['aria-label']==='Capsules')[0].props['data-focused-id'],'');
 assert.equal(galleryTiles(m).length,11);assert.equal(m.calls.intents,0);
 }finally{m.done();}
});

test('Add window click anchors right away by creating Window N and pairing full-window crop after each authority result',async()=>{
 const base=snapshot([],'1');base.state.folder_configured=false;
 const win={handle:'win-discord-1',generation:'7',layout_revision:'11',title:'Discord Pop-out',class:'Chrome_WidgetWin_1',executable:'Discord.exe',rect:{width:1280,height:720}};
 const m=await mount(async()=>base,{submit:()=>Promise.resolve({queued:true})});
 try{
  assert.ok(squash(textOf(m.tree)).includes('No output folder set'));
  assert.ok(button(m.tree,'Choose folder'));
  await m.click('Add window');
  assert.equal(m.calls.requests.length,1);
  assert.deepEqual(m.calls.requests[0].body,{op:'list_windows',offset:0});
  const page0=snapshot([],'2');page0.state.folder_configured=false;
  page0.state.windows=[win];page0.state.window_page={offset:0,total:1,next_offset:null};
  page0.state.last_result={state:'observed',code:'windows_refreshed',request_id:m.calls.requests[0].request_id};
  await m.push(page0);
  const browser=findAll(m.tree,n=>n.props.id==='cafe-window-browser')[0];
  assert.ok(browser);
  assert.equal(button(browser,'Emparejar ventana'),undefined);
  assert.equal(button(browser,'Copy identifier'),undefined);
  assert.equal(findAll(browser,n=>n.tag==='code').length,0);
  assert.equal(textOf(browser).includes('win-discord-1'),false);
  const details=findAll(browser,n=>n.tag==='details')[0];
  assert.ok(details);assert.equal(details.props.open,undefined);
  assert.ok(squash(textOf(details)).includes('Advanced crop'));

  const row=findAll(browser,n=>n.tag==='button'&&typeof n.props.class==='string'&&n.props.class.includes('cafe-window-row'))[0];
  assert.ok(row);
  const anchorPromise=row.props.onClick();
  await settle(m.ui.nextTick);
  assert.equal(m.calls.requests.length,2);
  assert.equal(m.calls.requests[1].body.op,'create_capsule');
  assert.equal(m.calls.requests[1].body.person_label,'Window 1');
  const createdId=m.calls.requests[1].body.capsule_id;

  const afterCreate=JSON.parse(JSON.stringify(page0));
  afterCreate.revision='3';
  afterCreate.state.capsules={[createdId]:{capsule_id:createdId,person_label:'Window 1',feeds:{}}};
  afterCreate.state.last_result={state:'committed',code:'capsule_created',request_id:m.calls.requests[1].request_id};
  await m.push(afterCreate);
  assert.equal(m.calls.requests.length,3);
  assert.deepEqual(m.calls.requests[2].body,{
   op:'pair_window',
   capsule_id:createdId,
   person_label:'Window 1',
   feed_id:m.calls.requests[2].body.feed_id,
   role:'game',
   window_token:'win-discord-1',
   window_generation:'7',
   layout_revision:'11',
   crop:{x:0,y:0,width:1280,height:720},
  });
  assert.equal(findAll(m.tree,n=>n.props.id==='cafe-window-browser').length,1,'picker remains open until pair_window is committed');

  const afterPair=JSON.parse(JSON.stringify(afterCreate));
  afterPair.revision='4';
  afterPair.state.capsules[createdId].feeds={game:{feed_id:m.calls.requests[2].body.feed_id,role:'game',generation:'1'}};
  afterPair.state.last_result={state:'committed',code:'window_paired',request_id:m.calls.requests[2].request_id};
  await m.push(afterPair);
  await anchorPromise;
  await settle(m.ui.nextTick);
  assert.equal(findAll(m.tree,n=>n.props.id==='cafe-window-browser').length,0);
  assert.equal(galleryLabels(m)[createdId],'slot-1 · Window 1');

  await m.click('Add window');
  const pageReopen=JSON.parse(JSON.stringify(afterPair));
  pageReopen.revision='5';
  pageReopen.state.last_result={state:'observed',code:'windows_refreshed',request_id:m.calls.requests.at(-1).request_id};
  await m.push(pageReopen);
  const browserAgain=findAll(m.tree,n=>n.props.id==='cafe-window-browser')[0];
  const anchoredRow=findAll(browserAgain,n=>n.tag==='button'&&typeof n.props.class==='string'&&n.props.class.includes('cafe-window-row'))[0];
  assert.ok(squash(textOf(anchoredRow)).includes('Anchored to slot-1'));
  assert.equal(anchoredRow.props.disabled,true);
  const beforeCount=m.calls.requests.length;
  await anchoredRow.props.onClick();
  await settle(m.ui.nextTick);
  assert.equal(m.calls.requests.length,beforeCount);
 }finally{m.done();}
});

test('picker auto-pages all window pages, sorts Discord first, hides noise behind Show all windows, and renders untrusted titles as text',async()=>{
 const m=await mount(async()=>snapshot([],'1'),{submit:()=>Promise.resolve({queued:true})});
 try{
  await m.click('Add window');
  assert.equal(m.calls.requests.length,1);
  assert.deepEqual(m.calls.requests[0].body,{op:'list_windows',offset:0});
  const p0=snapshot([],'2');
  p0.state.windows=[
   {handle:'w-game',generation:'1',layout_revision:'1',title:'<b>Game Feed</b>',class:'G',executable:'game.exe',rect:{width:800,height:600}},
   {handle:'w-explorer',generation:'1',layout_revision:'1',title:'Folder View',class:'CabinetWClass',executable:'C:\\Windows\\explorer.exe',rect:{width:800,height:600}},
   {handle:'w-empty',generation:'1',layout_revision:'1',title:'   ',class:'D',executable:'Discord.exe',rect:{width:800,height:600}},
  ];
  p0.state.window_page={offset:0,total:6,next_offset:3};
  p0.state.last_result={state:'observed',code:'windows_refreshed',request_id:m.calls.requests[0].request_id};
  await m.push(p0);
  assert.equal(m.calls.requests.length,2);
  assert.deepEqual(m.calls.requests[1].body,{op:'list_windows',offset:3});
  const p1=snapshot([],'3');
  p1.state.windows=[
   {handle:'w-input',generation:'1',layout_revision:'1',title:'Input Experience',class:'I',executable:'TextInputHost.exe',rect:{width:400,height:300}},
   {handle:'w-pt',generation:'1',layout_revision:'1',title:'PowerToys Run',class:'P',executable:'PowerToys.PowerLauncher.exe',rect:{width:400,height:300}},
   {handle:'w-discord',generation:'1',layout_revision:'1',title:'<em>Guest Call</em>',class:'Chrome_WidgetWin_1',executable:'Discord.exe',rect:{width:1280,height:720}},
  ];
  p1.state.window_page={offset:3,total:6,next_offset:null};
  p1.state.last_result={state:'observed',request_id:m.calls.requests[1].request_id};
  await m.push(p1);

  const browser=findAll(m.tree,n=>n.props.id==='cafe-window-browser')[0];
  const rows=()=>findAll(browser,n=>n.tag==='button'&&typeof n.props.class==='string'&&n.props.class.includes('cafe-window-row'));
  assert.equal(rows().length,2);
  assert.match(squash(textOf(rows()[0])),/<em>Guest Call<\/em> Discord\.exe/);
  assert.match(squash(textOf(rows()[1])),/<b>Game Feed<\/b> game\.exe/);
  assert.equal(findAll(browser,n=>n.tag==='em'||n.tag==='b').length,0,'untrusted window titles render as text only');

  const toggle=findAll(browser,n=>n.tag==='input'&&n.props.type==='checkbox')[0];
  assert.ok(toggle);
  toggle.props['onUpdate:modelValue'](true);
  await settle(m.ui.nextTick);
  assert.equal(rows().length,6);
  assert.ok(squash(textOf(rows()[0])).includes('Discord.exe'));
  assert.ok(squash(textOf(rows()[1])).includes('Discord.exe'));

  await m.click('Close');
  await m.click('Settings');
  const firstPanel=findAll(m.tree,n=>n.tag==='section'&&n.props['aria-labelledby']==='cafe-setup-title')[0];
  const panelText=squash(textOf(firstPanel));
  const folderIdx=panelText.indexOf('Folder');
  const ffmpegIdx=panelText.indexOf('FFmpeg');
  const obsIdx=panelText.indexOf('OBS');
  const twitchIdx=panelText.indexOf('Twitch');
  assert.ok(folderIdx>=0&&ffmpegIdx>folderIdx&&obsIdx>ffmpegIdx&&twitchIdx>obsIdx);
  assert.ok(button(m.tree,'New capsule'));
 }finally{m.done();}
});

test('startup shows Starting Café without Capsules unavailable flash, and empty wall has no card',async()=>{
 let resolveSnap;
 const mPromise=mount(()=>new Promise(resolve=>{resolveSnap=resolve;}));
 await new Promise(resolve=>setImmediate(resolve));
 resolveSnap({availability:'starting'});
 const m=await mPromise;
 try{
  const startText=squash(textOf(m.tree));
  assert.ok(startText.includes('Starting Café...'));
  assert.equal(startText.includes('Capsules unavailable'),false);
  assert.equal(startText.includes('Runtime starting'),false);
  await m.push(snapshot([],'1'));
  assert.equal(findAll(m.tree,n=>typeof n.props.class==='string'&&n.props.class.includes('cafe-capsule-grid-note')).length,0);
  assert.ok(findAll(m.tree,n=>typeof n.props.class==='string'&&n.props.class.includes('cafe-empty-add-button'))[0]);
  assert.ok(findAll(m.tree,n=>typeof n.props.class==='string'&&n.props.class.includes('cafe-empty-instructions'))[0]);
 }finally{m.done();}
});

test('tile hides OBS not ready until clicked, omits Window badge, removes capsule via header, and uses silent success',async()=>{
 const base=snapshot(['cap-a','cap-b'],'1');
 base.state.obs_available=false;
 const m=await mount(async()=>base,{submit:()=>Promise.resolve({queued:true})});
 try{
  const region=findAll(m.tree,n=>n.tag==='section'&&n.props['aria-label']==='Capsules')[0];
  assert.equal(region.props['data-wall-columns'],2);
  assert.equal(region.props['data-wall-rows'],1);
  assert.equal(galleryTiles(m).length,2);
  assert.equal(findAll(m.tree,n=>typeof n.props.class==='string'&&n.props.class.includes('cafe-preview-role')).length,0);

  const tile=findAll(m.tree,n=>n.tag==='article')[0];
  assert.equal(squash(textOf(tile)).includes('OBS not ready'),false);
  const obsBtn=button(tile,'OBS');
  assert.equal(obsBtn.props.disabled,true);
  assert.equal(obsBtn.props.title,'OBS not ready');
  await obsBtn.props.onClick();
  await settle(m.ui.nextTick);
  assert.ok(squash(textOf(tile)).includes('OBS not ready'));

  const removeBtn=button(tile,'Remove');
  assert.ok(removeBtn);
  const removeTask=removeBtn.props.onClick();
  await settle(m.ui.nextTick);
  assert.equal(m.calls.requests.at(-1).body.op,'remove_capsule');
  assert.equal(m.calls.requests.at(-1).body.capsule_id,'cap-a');

  const rejected=snapshot(['cap-a','cap-b'],'2');
  rejected.state.obs_available=false;
  rejected.state.last_result={state:'rejected',code:'operator_frame_rejected',request_id:m.calls.requests.at(-1).request_id};
  await m.push(rejected);
  await removeTask;
  await settle(m.ui.nextTick);
  assert.match(squash(textOf(tile)),/Authority refused the request/);

  const retryRemove=button(tile,'Remove').props.onClick();
  await settle(m.ui.nextTick);
  const committed=snapshot(['cap-b'],'3');
  committed.state.obs_available=false;
  committed.state.last_result={state:'committed',code:'capsule_removed',request_id:m.calls.requests.at(-1).request_id};
  await m.push(committed);
  await retryRemove;
  await settle(m.ui.nextTick);
  assert.equal(squash(textOf(m.tree)).includes('Authority acknowledged'),false);
 }finally{m.done();}
});

test('shared display timer shows mm:ss on Running and resets on stop without authority labels',async()=>{
 const m=await mount(async()=>snapshot(['cap-a','cap-b'],'1'));
 try{
  const elapsedNodes=()=>findAll(m.tree,n=>typeof n.props.class==='string'&&n.props.class.includes('cafe-capsule-elapsed'));
  assert.deepEqual(elapsedNodes().map(n=>squash(textOf(n))),['—','—']);
  assert.ok(elapsedNodes().every(n=>!String(n.props['aria-label']||'').toLowerCase().includes('authorit')));

  const running=snapshot(['cap-a','cap-b'],'2');
  running.state.recordings=[{capsule_id:'cap-a',state:'Running',feeds:[]}];
  await m.push(running);
  assert.match(squash(textOf(elapsedNodes()[0])),/^\d{2}:\d{2}$/);
  assert.equal(squash(textOf(elapsedNodes()[1])),'—');
  assert.ok(elapsedNodes().every(n=>!String(n.props['aria-label']||'').toLowerCase().includes('authorit')));

  const stopped=snapshot(['cap-a','cap-b'],'3');
  stopped.state.recordings=[{capsule_id:'cap-a',state:'Stopped',feeds:[]}];
  await m.push(stopped);
  assert.deepEqual(elapsedNodes().map(n=>squash(textOf(n))),['—','—']);
 }finally{m.done();}
});

test('Settings folder and FFmpeg buttons reflect in-flight picker requests',async()=>{
 let release;
 const m=await mount(async()=>snapshot(),{submit:()=>new Promise(resolve=>{release=resolve;})});
 try{
  await m.click('Settings');
  assert.equal(button(m.tree,'Choose folder').props.disabled,false);
  assert.equal(button(m.tree,'Choose FFmpeg').props.disabled,false);
  await m.click('Choose folder');
  assert.equal(m.calls.intents,1);
  assert.equal(button(m.tree,'Choose folder').props.disabled,true);
  assert.equal(button(m.tree,'Choose FFmpeg').props.disabled,true);
  release({queued:true});await settle(m.ui.nextTick);
  assert.equal(button(m.tree,'Choose folder').props.disabled,false);
  assert.equal(button(m.tree,'Choose FFmpeg').props.disabled,false);
 }finally{release?.({queued:true});m.done();}
});

test('recording failure cards use fixed English reasons without raw codes or unproven occlusion claims',async()=>{
 const cases=[['recording_capture_frozen',/No new frames/],['recording_capture_device_lost',/capture device/],['recording_crop_invalid',/capture area/],['recording_first_frame_timeout',/No new frames/],['recording_UNKNOWN_PRIVATE',/Recording reason unavailable/]];
 for(const [reason,expected] of cases){
  const value=snapshot();value.state.recordings=[{capsule_id:'cap-a',state:'Failed',reason,feeds:[]}];
  const m=await mount(async()=>value);
  try{
   const rendered=squash(textOf(m.tree));assert.match(rendered,expected);
   assert.doesNotMatch(rendered,/recording_[a-zA-Z_]+|Discord stops painting|fully covered/);
   assert.ok(button(m.tree,'Retry'));assert.equal(m.calls.intents,0);
  }finally{m.done();}
 }
});

test('Twitch Settings normalize channel notation and reject names outside the connector contract',async()=>{
 for(const [input,expected] of [['  #MiXeD_Name  ','mixed_name'],['ABC','abc'],['normal_channel','normal_channel'],['a',null],['ab',null],['##abc',null],['a'.repeat(26),null]]){
  const m=await mount(async()=>snapshot(),{submit:async()=>({queued:true})});
  try{
   await m.click('Settings');
   const field=findAll(m.tree,n=>n.props.id==='cafe-chat-channel')[0];
   field.props['onUpdate:modelValue'](input);await settle(m.ui.nextTick);
   await m.click('Configure chat');
   if(expected===null){assert.equal(m.calls.intents,0);assert.match(squash(textOf(m.tree)),/Enter a channel name with 3–25 letters, numbers or underscores/);}
   else {assert.equal(m.calls.intents,1);assert.deepEqual(m.calls.requests[0].body,{op:'configure_twitch',channel:expected});assert.equal(field.value,expected);}
   assert.doesNotMatch(squash(textOf(m.tree)),/Chat: Ready/);
  }finally{m.done();}
 }
});
