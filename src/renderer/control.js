// @ts-check
/** Named renderer actions only. The native parent and Rust authority revalidate
 * every request; queued is never interpreted as committed or complete. */
const MAX_CLOCK = 18446744073709551615n;
/** @param {unknown} value @returns {value is Record<string, any>} */
function object(value) { return value !== null && typeof value === 'object' && !Array.isArray(value); }
/** @param {unknown} value @returns {value is string} */
function clock(value) { return typeof value === 'string' && /^(0|[1-9][0-9]{0,19})$/.test(value) && BigInt(value) <= MAX_CLOCK; }
/** @param {unknown} value @returns {value is string} */
function identifier(value) { return typeof value === 'string' && /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}$/.test(value); }
/** @param {unknown} value @param {number} max @returns {value is string} */
function text(value, max) { return typeof value === 'string' && value.length > 0 && new TextEncoder().encode(value).length <= max && !value.includes('\0'); }
/** @param {unknown} value @returns {value is number} */
function unsigned(value) { return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 && value <= 4294967295; }
function rejected() { return new Error('Request rejected; refresh and try again'); }
/** @param {unknown} bridge @param {()=>string} [idFactory] */
export function createControls(bridge, idFactory = () => globalThis.crypto.randomUUID()) {
  /** @type {Record<string,any>|null} */ let current = null;
  /** @type {string|null} */ let epoch = null;
  let connected = false, disposed = false, pending = false;
  /** @type {(()=>void)|null} */ let detach = null;
  /** @type {Set<(view:any)=>void>} */ const listeners = new Set();
  /** @returns {any} */
  function getState() {
    const empty = { availability: current?.availability || 'unavailable', platform: 'unknown', capsules: [], windows: [], windowPage: {offset:0,nextOffset:null,total:null}, recordings: [], folderConfigured:false,ffmpegConfigured:false,obsReady:false,obsConfigurable:false,chatReady:false,chatConfigurable:false,lastResult:null };
    if (!current || current.availability !== 'available') return empty;
    const state=current.state;
    if (!object(state) || state.runtime !== 'authority_ready' || !object(state.capsules) || !Array.isArray(state.windows)) return {...empty,availability:'unavailable'};
    try {
      const capsules=Object.values(state.capsules).map(row=>{
        if (!object(row)||!identifier(row.capsule_id)||!text(row.person_label,128)||!object(row.feeds)) throw rejected();
        const feeds=Object.values(row.feeds) .map((/** @type {any} */ feed)=>{if(!object(feed)||!identifier(feed.feed_id)||!['game','camera'].includes(feed.role)||!clock(feed.generation)||BigInt(feed.generation)===0n)throw rejected();return {feed_id:feed.feed_id,role:feed.role,generation:feed.generation};});
        if(feeds.length>2)throw rejected();return {capsule_id:row.capsule_id,person_label:row.person_label,feeds};
      });
      if(!['windows','unsupported'].includes(state.platform)||capsules.length>2||state.windows.length>3)throw rejected();
      const windows=state.windows.map(row=>{if(!object(row)||!text(row.handle,128)||!clock(row.generation)||!clock(row.layout_revision)||BigInt(row.generation)===0n||BigInt(row.layout_revision)===0n||!object(row.rect)||!unsigned(row.rect.width)||!unsigned(row.rect.height))throw rejected();return {token:row.handle,generation:row.generation,layout_revision:row.layout_revision,title:typeof row.title==='string'?row.title:'',class_name:typeof row.class==='string'?row.class:'',exe:typeof row.executable==='string'?row.executable:'',rect:{width:row.rect.width,height:row.rect.height}};});
      const page=state.window_page;
      if(!object(page)||!unsigned(page.offset)||!unsigned(page.total)||!(page.next_offset===null||unsigned(page.next_offset)))throw rejected();
      const recordings=Array.isArray(state.recordings)?state.recordings.map(row=>({capsule_id:row.capsule_id,state:row.state,feeds:Array.isArray(row.feeds)?row.feeds .map((/** @type {any} */ feed)=>({feed_id:feed.feed_id,state:feed.state,written_frames:clock(feed.written_frames)?feed.written_frames:null,backpressure:clock(feed.backpressure)?feed.backpressure:null})):[]})):[];
      const last=object(state.last_result)&&['observed','committed','rejected','pending','blocked','interrupted','configured'].includes(state.last_result.state)?{state:state.last_result.state,code:'runtime_'+state.last_result.state}:null;
      return {availability:'available',platform:state.platform==='windows'?'windows':'unsupported',capsules,windows,windowPage:{offset:page.offset,nextOffset:page.next_offset,total:page.total},recordings,folderConfigured:state.folder_configured===true,ffmpegConfigured:state.recording_configured===true,obsReady:state.obs_available===true,obsConfigurable:state.obs_configurable===true,chatReady:state.chat_available===true,chatConfigurable:state.chat_configurable===true,lastResult:last};
    } catch { return {...empty,availability:'unavailable'}; }
  }
  function emit(){const view=getState();for(const listener of listeners){try{listener(view);}catch{ /* presentation listener has no authority effect */ }}}
  /** @param {unknown} snapshot */
  function accept(snapshot){
    if(disposed)return;
    try {
      if(!object(snapshot)||!['starting','available','unavailable'].includes(snapshot.availability)||new TextEncoder().encode(JSON.stringify(snapshot)).length>8192)throw rejected();
      if(snapshot.availability==='available'){
        if(!clock(snapshot.authority_epoch)||BigInt(snapshot.authority_epoch)===0n||!clock(snapshot.revision))throw rejected();
        if(epoch!==null&&snapshot.authority_epoch!==epoch)throw rejected();
        if(current?.availability==='available'&&BigInt(snapshot.revision)<BigInt(current.revision))return;
        epoch=snapshot.authority_epoch;
      }
      current=JSON.parse(JSON.stringify(snapshot));emit();
    }catch{current={availability:'unavailable'};emit();}
  }
  async function connect(){
    if(disposed)throw rejected();if(connected)return;connected=true;
    if(!object(bridge)||typeof bridge.getSnapshot!=='function'||typeof bridge.onSnapshot!=='function'||typeof bridge.submitIntent!=='function'){accept({availability:'unavailable'});return;}
    try { const off=bridge.onSnapshot(accept);if(typeof off!=='function')throw rejected();detach=off;accept(await bridge.getSnapshot()); }
    catch { accept({availability:'unavailable'}); }
  }
  /** @param {(view:any)=>void} listener */
  function subscribe(listener){if(disposed||typeof listener!=='function'||listeners.size>=16)throw rejected();listeners.add(listener);listener(getState());return ()=>{listeners.delete(listener);};}
  function dispose(){if(disposed)return;disposed=true;listeners.clear();if(detach){try{detach();}catch{}detach=null;} /* never stop authority or recording */}
  function nonce(){const value=idFactory();if(!identifier(value))throw rejected();return value;}
  /** @param {Record<string,unknown>} body */
  async function send(body){
    if(disposed||pending||getState().availability!=='available'||!current||!object(bridge)||typeof bridge.submitIntent!=='function')throw rejected();
    const request={schema:'cafe.operator.v1',kind:'operator_intent',request_id:nonce(),authority_epoch:current.authority_epoch,expected_revision:current.revision,body};
    pending=true;
    try{const reply=await bridge.submitIntent(request);if(!object(reply)||!(reply.queued===true||(reply.queued===false&&reply.cancelled===true)))throw rejected();return reply;}
    catch{throw rejected();}finally{pending=false;}
  }
  /** @param {string} personLabel */
  async function createCapsule(personLabel){if(!text(personLabel,128)||getState().capsules.length>=2)throw rejected();return send({op:'create_capsule',capsule_id:'cap-'+nonce(),person_label:personLabel});}
  /** @param {number} [offset] */
  async function refreshWindows(offset=0){if(!unsigned(offset)||offset>1000000)throw rejected();return send({op:'list_windows',offset});}
  /** @param {any} value */
  async function pairWindow(value){
    if(!object(value)||!identifier(value.capsuleId)||!['game','camera'].includes(value.role)||!text(value.windowToken,128)||!clock(value.windowGeneration)||!clock(value.layoutRevision)||!object(value.crop))throw rejected();
    const crop=value.crop;if(Object.keys(crop).sort().join(',')!=='height,width,x,y'||![crop.x,crop.y,crop.width,crop.height].every(unsigned)||!crop.width||!crop.height||crop.x+crop.width>4294967295||crop.y+crop.height>4294967295)throw rejected();
    const view=getState(),capsule=view.capsules.find((/** @type {any} */ row)=>row.capsule_id===value.capsuleId),window=view.windows.find((/** @type {any} */ row)=>row.token===value.windowToken);
    if(!capsule||!window||window.generation!==value.windowGeneration||window.layout_revision!==value.layoutRevision||capsule.feeds.some((/** @type {any} */ feed)=>feed.role===value.role))throw rejected();
    return send({op:'pair_window',capsule_id:capsule.capsule_id,person_label:capsule.person_label,feed_id:'feed-'+nonce(),role:value.role,window_token:window.token,window_generation:window.generation,layout_revision:window.layout_revision,crop:{x:crop.x,y:crop.y,width:crop.width,height:crop.height}});
  }
  async function chooseFolder(){return send({op:'choose_folder'});}
  async function chooseFfmpeg(){return send({op:'choose_ffmpeg'});}
  /** @param {string} capsuleId */
  async function startRecording(capsuleId){const view=getState();const capsule=view.capsules.find((/** @type {any} */ row)=>row.capsule_id===capsuleId);if(view.platform!=='windows'||!view.folderConfigured||!view.ffmpegConfigured||!capsule?.feeds.length)throw rejected();return send({op:'start_recording',capsule_id:capsuleId});}
  /** @param {string} capsuleId */
  async function stopRecording(capsuleId){if(!getState().recordings.some((/** @type {any} */ row)=>row.capsule_id===capsuleId&&row.state==='Running'))throw rejected();return send({op:'stop_recording',capsule_id:capsuleId});}
  /** @param {string} capsuleId */
  async function sendToObs(capsuleId){const view=getState();if(!view.obsReady||!view.capsules.some((/** @type {any} */ row)=>row.capsule_id===capsuleId))throw rejected();return send({op:'send_to_obs',capsule_id:capsuleId});}
  /** @param {number} port @param {string} password */
  async function configureObs(port,password){if(!getState().obsConfigurable||!Number.isInteger(port)||port<1||port>65535||typeof password!=='string'||new TextEncoder().encode(password).length>1024||password.includes('\0'))throw rejected();return send({op:'configure_obs',port,password:password===''?{mode:'absent'}:{mode:'provided',value:password}});}
  /** @param {string} channel */
  async function configureTwitch(channel){if(!getState().chatConfigurable||typeof channel!=='string'||!(/^[a-z0-9_]{1,25}$/).test(channel))throw rejected();return send({op:'configure_twitch',channel});}
  return Object.freeze({getState,subscribe,connect,dispose,createCapsule,refreshWindows,pairWindow,chooseFolder,chooseFfmpeg,startRecording,stopRecording,sendToObs,configureObs,configureTwitch});
}
