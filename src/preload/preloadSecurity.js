// @ts-check
'use strict';
const { createCafeBridge } = require('./cafeBridge');
const GET = 'cafe:getSnapshot';
const SUBMIT = 'cafe:submitIntent';
const SNAPSHOT = 'cafe:snapshot';

/** @param {import('electron').IpcRenderer} ipcRenderer */
function closedTransport(ipcRenderer) {
  return Object.freeze({
    /** @param {string} channel @param {unknown} [payload] @returns {Promise<unknown>} */
    async invoke(channel, payload) {
      if (channel !== GET && channel !== SUBMIT) throw new Error('IPC_CHANNEL_DENIED');
      if (channel === GET && payload !== undefined) throw new Error('IPC_ARGUMENTS_DENIED');
      try { return channel === GET ? await ipcRenderer.invoke(channel) : await ipcRenderer.invoke(channel, payload); }
      catch { throw new Error('IPC_REQUEST_REJECTED'); }
    },
    /** @param {string} channel @param {(value: unknown) => void} listener @returns {() => void} */
    subscribe(channel, listener) {
      if (channel !== SNAPSHOT || typeof listener !== 'function') throw new Error('IPC_CHANNEL_DENIED');
      /** @param {unknown} _event @param {unknown} payload */
      const receive = (_event, payload) => listener(payload);
      ipcRenderer.on(SNAPSHOT, receive);
      let removed = false;
      return () => {
        if (!removed) { removed = true; ipcRenderer.removeListener(SNAPSHOT, receive); }
      };
    },
  });
}

/** @param {{contextBridge: import('electron').ContextBridge, ipcRenderer: import('electron').IpcRenderer}} electron */
function exposeClosedBridge(electron) {
  const facade = createCafeBridge(closedTransport(electron.ipcRenderer));
  electron.contextBridge.exposeInMainWorld('cafe', facade);
  return facade;
}

exports.closedTransport = closedTransport;
exports.exposeClosedBridge = exposeClosedBridge;
