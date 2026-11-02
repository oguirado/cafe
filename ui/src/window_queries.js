// @ts-check
/** Wait for the exact native query observation, never the enqueue acknowledgement.
 * This module schedules a bounded presentation timeout; it owns no native effect.
 * @param {any} controller @param {number} offset @param {() => boolean} isCurrent
 * @returns {Promise<any>}
 */
export function observeWindowPage(controller, offset, isCurrent) {
  return new Promise((resolve, reject) => {
    let issued = false, requestId = '', completed = false;
    /** @type {any[]} */ const waiting = [];
    let off = () => {};
    const finish = (value, error) => {
      if (completed) return; completed = true; clearTimeout(timer); off();
      if (error) reject(new Error('window_query_failed')); else resolve(value);
    };
    const matches = value => value.windowPage?.offset === offset &&
      value.lastResult?.state === 'observed' && value.lastResult?.requestId === requestId;
    const collect = value => {
      if (!issued || completed) return;
      if (!isCurrent() || value.availability !== 'available') { finish(null, true); return; }
      if (requestId) { if (matches(value)) finish(value, false); }
      else { if (waiting.length >= 8) { finish(null, true); return; } waiting.push(value); }
    };
    const timer = setTimeout(() => finish(null, true), 3000);
    off = controller.subscribe(collect);
    issued = true;
    controller.refreshWindows(offset).then(reply => {
      if (!reply || reply.queued !== true || typeof reply.request_id !== 'string') { finish(null, true); return; }
      requestId = reply.request_id;
      const exact = waiting.find(matches); if (exact) finish(exact, false);
      waiting.length = 0;
    }, () => finish(null, true));
  });
}
