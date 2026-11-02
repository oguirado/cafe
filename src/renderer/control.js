// @ts-check
/** Named renderer actions only. The native parent and Rust authority revalidate
 * every request; queued is never interpreted as committed or complete. */
const MAX_CLOCK = 18446744073709551615n;
const MAX_CAPSULES = 12;
// The authority transport has its own bound; this only bounds the UI projection.
const MAX_SNAPSHOT_BYTES = 65536;
const OPERATOR_OPS = new Set([
  'create_capsule','list_windows','pair_window','choose_folder','choose_ffmpeg',
  'start_recording','stop_recording','send_to_obs','configure_obs','configure_twitch',
  'organizer_plan_confirm','organizer_plan_reject','set_auto_anchor','remove_capsule',
]);
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
/** Optional proposal data never grants an effect or supplies an executable path.
 * @param {unknown} value */
function organizerPlan(value) {
  // The authority publishes expiry as a JSON integer; the UI keeps clocks as decimal strings.
  if (object(value) && Number.isSafeInteger(value.expires_unix_ms) && value.expires_unix_ms > 0) value = {...value, expires_unix_ms: String(value.expires_unix_ms)};
  if (!object(value) || !identifier(value.plan_id) || !text(value.folder_name,255)
      || !clock(value.expires_unix_ms) || !Array.isArray(value.windows)
      || value.windows.length < 1 || value.windows.length > MAX_CAPSULES
      || !value.windows.every(row => object(row) && text(row.title,512))) return null;
  return {plan_id:value.plan_id,folder_name:value.folder_name,
    expires_unix_ms:value.expires_unix_ms,windows:value.windows.map(row => ({title:row.title}))};
}
/** Only closed, authority-supplied recording reasons reach presentation.
 * @param {unknown} value @returns {string|null} */
function recordingReason(value) {
  if (value === undefined || value === null) return null;
  return typeof value === 'string' && DIAGNOSTIC_CODES.has(value) && value.startsWith('recording_')
    ? value : 'recording_reason_unavailable';
}
/** @param {unknown} bridge @param {()=>string} [idFactory] */
export function createControls(bridge, idFactory = () => globalThis.crypto.randomUUID()) {
  /** @type {Record<string,any>|null} */ let current = null;
  /** @type {string|null} */ let epoch = null;
  let connected = false, disposed = false, pending = false;
  /** @type {(()=>void)|null} */ let detach = null;
  /** @type {Set<(view:any)=>void>} */ const listeners = new Set();
  /** @type {Map<string,string>} */ const pairedFeedTokens = new Map();
  /** @type {Map<string,string>} */ const pairedRoleTokens = new Map();
  /** @returns {any} */
  function getState() {
    const empty = { availability: current?.availability || 'unavailable', platform: 'unknown', capsules: [], windows: [], windowPage: {offset:0,nextOffset:null,total:null}, recordings: [], folderConfigured:false,ffmpegConfigured:false,obsReady:false,obsConfigurable:false,chatReady:false,chatConfigurable:false,lastResult:null,organizerPlan:null,autoAnchor:null };
    if (!current || current.availability !== 'available') return empty;
    const state=current.state;
    if (!object(state) || state.runtime !== 'authority_ready' || !object(state.capsules) || !Array.isArray(state.windows)) return {...empty,availability:'unavailable'};
    try {
      const capsules=Object.values(state.capsules).map(row=>{
        if (!object(row)||!identifier(row.capsule_id)||!text(row.person_label,128)||!object(row.feeds)) throw rejected();
        const feeds=Object.values(row.feeds) .map((/** @type {any} */ feed)=>{if(!object(feed)||!identifier(feed.feed_id)||!['game','camera'].includes(feed.role)||!clock(feed.generation)||BigInt(feed.generation)===0n)throw rejected();const winToken=text(feed.window_token,128)?feed.window_token:text(feed.handle,128)?feed.handle:pairedFeedTokens.get(feed.feed_id)||pairedRoleTokens.get(`${row.capsule_id}:${feed.role}`)||null;return {feed_id:feed.feed_id,role:feed.role,generation:feed.generation,window_state:['window_minimized','window_closed'].includes(feed.window_state)?feed.window_state:null,...(winToken?{window_token:winToken}:{})};});
        if(feeds.length>2)throw rejected();return {capsule_id:row.capsule_id,person_label:row.person_label,feeds};
      });
      if(!['windows','unsupported'].includes(state.platform)||capsules.length>MAX_CAPSULES||state.windows.length>3)throw rejected();
      const windows=state.windows.map(row=>{if(!object(row)||!text(row.handle,128)||!clock(row.generation)||!clock(row.layout_revision)||BigInt(row.generation)===0n||BigInt(row.layout_revision)===0n||!object(row.rect)||!unsigned(row.rect.width)||!unsigned(row.rect.height))throw rejected();return {token:row.handle,generation:row.generation,layout_revision:row.layout_revision,title:typeof row.title==='string'?row.title:'',class_name:typeof row.class==='string'?row.class:'',exe:typeof row.executable==='string'?row.executable:'',rect:{width:row.rect.width,height:row.rect.height}};});
      const page=state.window_page;
      if(!object(page)||!unsigned(page.offset)||!unsigned(page.total)||!(page.next_offset===null||unsigned(page.next_offset)))throw rejected();
      const recordings=Array.isArray(state.recordings)?state.recordings.map(row=>({capsule_id:row.capsule_id,state:row.state,reason:recordingReason(row.reason),elapsed_ms:clock(row.elapsed_ms)?row.elapsed_ms:null,feeds:Array.isArray(row.feeds)?row.feeds .map((/** @type {any} */ feed)=>({feed_id:feed.feed_id,state:feed.state,written_frames:clock(feed.written_frames)?feed.written_frames:null,backpressure:clock(feed.backpressure)?feed.backpressure:null})):[]})):[];
      const last=object(state.last_result)&&['observed','committed','rejected','pending','blocked','interrupted','configured'].includes(state.last_result.state)?{state:state.last_result.state,code:DIAGNOSTIC_CODES.has(state.last_result.code)?state.last_result.code:'runtime_'+state.last_result.state,requestId:identifier(state.last_result.request_id)?state.last_result.request_id:null}:null;
      return {availability:'available',platform:state.platform==='windows'?'windows':'unsupported',capsules,windows,windowPage:{offset:page.offset,nextOffset:page.next_offset,total:page.total},recordings,folderConfigured:state.folder_configured===true,ffmpegConfigured:state.recording_configured===true,obsReady:state.obs_available===true,obsConfigurable:state.obs_configurable===true,chatReady:state.chat_available===true,chatConfigurable:state.chat_configurable===true,lastResult:last,organizerPlan:organizerPlan(state.organizer_plan),autoAnchor:typeof state.auto_anchor==='boolean'?state.auto_anchor:null};
    } catch { return {...empty,availability:'unavailable'}; }
  }
  function emit(){const view=getState();for(const listener of listeners){try{listener(view);}catch{ /* presentation listener has no authority effect */ }}}
  /** @param {unknown} snapshot */
  function accept(snapshot){
    if(disposed)return;
    try {
      if(!object(snapshot)||!['starting','available','unavailable'].includes(snapshot.availability)||new TextEncoder().encode(JSON.stringify(snapshot)).length>MAX_SNAPSHOT_BYTES)throw rejected();
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
  /** @param {Record<string,unknown>} body @param {string} [requestId] */
  async function send(body,requestId=nonce()){
    if(disposed||pending||typeof body.op!=='string'||!OPERATOR_OPS.has(body.op)||getState().availability!=='available'||!current||!object(bridge)||typeof bridge.submitIntent!=='function')throw rejected();
    const request={schema:'cafe.operator.v1',kind:'operator_intent',request_id:requestId,authority_epoch:current.authority_epoch,expected_revision:current.revision,body};
    pending=true;
    try{const reply=await bridge.submitIntent(request);if(!object(reply)||!(reply.queued===true||(reply.queued===false&&reply.cancelled===true)))throw rejected();return {queued:reply.queued===true,cancelled:reply.cancelled===true,request_id:requestId};}
    catch{throw rejected();}finally{pending=false;}
  }
  /** @param {string} personLabel */
  async function createCapsule(personLabel){if(!text(personLabel,128)||getState().capsules.length>=MAX_CAPSULES)throw rejected();return send({op:'create_capsule',capsule_id:'cap-'+nonce(),person_label:personLabel});}
  /** @param {number} [offset] */
  async function refreshWindows(offset=0){if(!unsigned(offset)||offset>1000000)throw rejected();const requestId=nonce();const reply=await send({op:'list_windows',offset},requestId);return {queued:reply.queued===true,cancelled:reply.cancelled===true,request_id:requestId};}
  /** @param {any} value */
  async function pairWindow(value){
    if(!object(value)||!identifier(value.capsuleId)||!['game','camera'].includes(value.role)||!text(value.windowToken,128)||!clock(value.windowGeneration)||!clock(value.layoutRevision)||!object(value.crop))throw rejected();
    const crop=value.crop;if(Object.keys(crop).sort().join(',')!=='height,width,x,y'||![crop.x,crop.y,crop.width,crop.height].every(unsigned)||!crop.width||!crop.height||crop.x+crop.width>4294967295||crop.y+crop.height>4294967295)throw rejected();
    const view=getState(),capsule=view.capsules.find((/** @type {any} */ row)=>row.capsule_id===value.capsuleId),window=view.windows.find((/** @type {any} */ row)=>row.token===value.windowToken);
    if(!capsule||!window||window.generation!==value.windowGeneration||window.layout_revision!==value.layoutRevision||capsule.feeds.some((/** @type {any} */ feed)=>feed.role===value.role)||view.capsules.some((/** @type {any} */ row)=>row.feeds.some((/** @type {any} */ feed)=>feed.window_token===value.windowToken)))throw rejected();
    const feedId='feed-'+nonce();
    pairedFeedTokens.set(feedId,window.token);
    pairedRoleTokens.set(`${capsule.capsule_id}:${value.role}`,window.token);
    return send({op:'pair_window',capsule_id:capsule.capsule_id,person_label:capsule.person_label,feed_id:feedId,role:value.role,window_token:window.token,window_generation:window.generation,layout_revision:window.layout_revision,crop:{x:crop.x,y:crop.y,width:crop.width,height:crop.height}});
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
  /** A native operator action submits one current proposal; no observer calls it.
   * @param {string} planId */
  async function confirmOrganizerPlan(planId){
    const plan=getState().organizerPlan;
    if(!identifier(planId)||!plan||plan.plan_id!==planId||BigInt(plan.expires_unix_ms)<=BigInt(Date.now()))throw rejected();
    return send({op:'organizer_plan_confirm',plan_id:planId});
  }
  /** @param {string} planId */
  async function rejectOrganizerPlan(planId){
    const plan=getState().organizerPlan;
    if(!identifier(planId)||!plan||plan.plan_id!==planId)throw rejected();
    return send({op:'organizer_plan_reject',plan_id:planId});
  }
  /** Missing authority capability never becomes a local opt-in.
   * @param {boolean} enabled */
  async function setAutoAnchor(enabled){
    if(typeof enabled!=='boolean'||getState().autoAnchor===null)throw rejected();
    return send({op:'set_auto_anchor',enabled});
  }
  /** Named capsule removal operation validated by the authority.
   * @param {string} capsuleId */
  async function removeCapsule(capsuleId){
    const view=getState();
    if(!identifier(capsuleId)||!view.capsules.some((/** @type {any} */ row)=>row.capsule_id===capsuleId)
      ||view.recordings.some((/** @type {any} */ row)=>row.capsule_id===capsuleId&&row.state==='Running'))throw rejected();
    return send({op:'remove_capsule',capsule_id:capsuleId});
  }
  return Object.freeze({getState,subscribe,connect,dispose,createCapsule,refreshWindows,pairWindow,chooseFolder,chooseFfmpeg,startRecording,stopRecording,sendToObs,configureObs,configureTwitch,confirmOrganizerPlan,rejectOrganizerPlan,setAutoAnchor,removeCapsule});
}

/** Fixed public diagnostic vocabulary. Unknown values never become text output. */
const DIAGNOSTIC_CODES = new Set([
  'pair_window_unavailable', 'pair_window_ambiguous', 'pair_window_stale',
  'pair_platform_unsupported', 'pair_identity_exhausted', 'pair_native_failure',
  'pair_window_invalid', 'pair_person_mismatch', 'pair_crop_invalid', 'pair_crop_stale',
  'pair_capsule_unknown', 'pair_role_already_paired', 'pair_feed_unavailable',
  'pair_authority_rejected', 'pair_generation_exhausted', 'pair_executable_only_forbidden',
  'pair_proposal_rejected', 'pair_not_durable', 'pair_reconfirmation_required',
  'binding_state_unavailable', 'window_token_unknown_or_ambiguous', 'stale_revision',
  'old_authority_epoch', 'request_id_conflict', 'operator_frame_rejected',
  'request_or_capacity_conflict', 'capsule_not_durable', 'capsule_removed',
  'recording_runtime_not_configured', 'organizer_plan_expired', 'organizer_plan_unknown',
  'organizer_plan_confirmed', 'organizer_plan_rejected', 'auto_anchor_configured',
  'window_paired', 'windows_refreshed', 'capsule_created', 'authority_started',
  'native_enumeration_unavailable', 'window_page_invalidated', 'projection_budget_exceeded',
  'ffmpeg_startup_selection_blocked', 'hardware_preflight_required', 'recording_ffmpeg_not_approved', 'recording_hardware_preflight_blocked', 'recording_capture_unavailable',
  'recording_first_frame_timeout', 'recording_capture_unhealthy', 'recording_binding_changed',
  'recording_capture_minimized',
  'recording_capture_frozen',
  'recording_capture_closed',
  'recording_capture_device_lost',
  'recording_capture_resize_required',
  'recording_capture_generation_mismatch',
  'recording_capture_backpressure',
  'recording_capture_border_unavailable',
  'recording_capture_source_failed',
  'recording_capture_clock_regression',
  'recording_crop_invalid', 'recording_recorder_start_failed', 'recording_setup_failed', 'recording_effect_interrupted',
]);
/** @param {unknown} snapshot */
export function buildDiagnosticReport(snapshot) {
  const s=object(snapshot)?snapshot:{};
  const st=object(s.state)?s.state:{};
  const last=object(st.last_result)?st.last_result:{};
  const windows=Array.isArray(st.windows)&&st.windows.length<=3?st.windows:null;
  const caps=object(st.capsules)&&Object.keys(st.capsules).length<=MAX_CAPSULES?Object.values(st.capsules):null;
  const validCaps=caps!==null&&caps.every(c=>object(c)&&object(c.feeds)&&Object.keys(c.feeds).length<=2);
  return Object.freeze({schema:'cafe.diagnostic.v1',
    bridge:['available','starting','unavailable'].includes(s.availability)?s.availability:'unknown',
    result_state:['observed','committed','rejected','pending','blocked','interrupted','configured'].includes(last.state)?last.state:'unknown',
    result_code:DIAGNOSTIC_CODES.has(last.code)?last.code:'unknown_result',
    capsule_count:validCaps?caps.length:null,
    paired_feed_count:validCaps?caps.reduce((n,c)=>n+Object.keys(c.feeds).length,0):null,
    listed_window_count:windows===null?null:windows.length,
    minimized_window_count:windows===null||windows.some(w=>!object(w)||typeof w.minimized!=='boolean')?null:windows.filter(w=>w.minimized===true).length,
    visible_window_count:windows===null||windows.some(w=>!object(w)||typeof w.visible!=='boolean')?null:windows.filter(w=>w.visible===true).length,
  });
}
/** Only the existing read-only bridge query is permitted; never submit an intent.
 * @param {unknown} bridge */
export async function readDiagnostics(bridge) {
  try {
    if(!object(bridge)||typeof bridge.getSnapshot!=='function')throw new Error('DIAGNOSTIC_UNAVAILABLE');
    return buildDiagnosticReport(await bridge.getSnapshot());
  } catch {return buildDiagnosticReport(null);}
}
