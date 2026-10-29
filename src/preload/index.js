// @ts-check
'use strict';
const { exposeClosedBridge } = require('./preloadSecurity');

/**
 * Installs the closed Cafe bridge into the isolated world. Host-testable:
 * plain Node unit tests call this with a mock electronApi and never touch
 * the native Electron module.
 * @param {{ contextBridge: import('electron').ContextBridge, ipcRenderer: import('electron').IpcRenderer }} electronApi
 */
function installPreload(electronApi) {
  return exposeClosedBridge(electronApi);
}

if (typeof process !== 'undefined' && process !== null && process.type === 'renderer') {
  installPreload(require('electron'));
}

exports.installPreload = installPreload;
