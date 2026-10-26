// @ts-check
'use strict';
/**
 * Consumer fixture only: replacing this facade has no authority lifecycle access.
 * @param {ReturnType<typeof import('../../src/preload/cafeBridge.js').createCafeBridge>} bridge
 */
function replaceRenderer(bridge) {
 return Object.freeze({
  getSnapshot: () => bridge.getSnapshot(),
  submitIntent: bridge.submitIntent,
  onSnapshot: bridge.onSnapshot
 });
}
module.exports = { replaceRenderer };
