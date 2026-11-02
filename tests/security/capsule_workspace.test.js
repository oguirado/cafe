'use strict';
const test=require('node:test'),assert=require('node:assert/strict');
const {mount,button,textOf,squash,findAll,settle}=require('../fixtures/capsule_control.js');
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
 assert.ok(fullText.includes('Keep each pop-out open and not fully covered: Discord stops painting covered windows.'));
 assert.equal(fullText.includes('PENDING-LIVE'),false);
 assert.equal(m.ui.presentations[0].panels[0].title,'slot-1 · Synthetic Alpha');
 assert.equal(findAll(m.tree,n=>n.props.id==='cafe-window-browser').length,0);assert.equal(m.ui.probe.reads,0);
 await m.click('Help');await m.click('Diagnostics');assert.ok(button(m.tree,'Show diagnostics'));
 assert.equal(m.calls.intents,0);assert.equal(m.calls.detached,0);assert.equal(m.ui.probe.reads,0);
 }finally{m.done();}
});
test('slot display aliases remain stable after authoritative reorder',async()=>{
 const m=await mount(async()=>snapshot(['cap-a','cap-b']));try{
 const cards=()=>findAll(m.tree,n=>n.tag==='article').map(n=>squash(textOf(n)));
 assert.ok(cards().find(t=>t.includes('Synthetic Alpha')).includes('slot-1'));assert.ok(cards().find(t=>t.includes('Synthetic Beta')).includes('slot-2'));
 assert.deepEqual(m.ui.presentations[0].panels.map(p=>p.title),['slot-1 · Synthetic Alpha','slot-2 · Synthetic Beta']);
 await m.push(snapshot(['cap-b','cap-a'],'2'));
 assert.ok(cards().find(t=>t.includes('Synthetic Alpha')).includes('slot-1'));assert.ok(cards().find(t=>t.includes('Synthetic Beta')).includes('slot-2'));
 assert.deepEqual(m.ui.presentations[0].panels.map(p=>p.title),['slot-1 · Synthetic Alpha','slot-2 · Synthetic Beta']);
 assert.equal(m.calls.intents,0);
 }finally{m.done();}
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

test('closing a presentation panel restores the same authoritative capsule without effects',async()=>{
 const m=await mount(async()=>snapshot());try{
 assert.equal(m.ui.presentations.length,1);const api=m.ui.presentations[0];
 assert.equal(api.panels.length,1);assert.equal(api.panels[0].params.capsuleId,'cap-a');
 api.removePanel(api.panels[0]);await settle(m.ui.nextTick);
 assert.equal(api.panels.length,1);assert.equal(api.panels[0].params.capsuleId,'cap-a');
 assert.ok(button(m.tree,'REC'));assert.equal(m.calls.intents,0);assert.equal(m.ui.probe.actions.length,0);
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
  assert.equal(m.ui.presentations[0].panels[0].title,'slot-1 · Window 1');

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
  assert.ok(m.ui.presentations[0].groups.every(g=>g.header.hidden===true));
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
