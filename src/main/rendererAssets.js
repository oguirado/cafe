// @ts-check
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const { createHash } = require('node:crypto');
const JAVASCRIPT_URL = 'cafe://app/renderer.js';
const STYLESHEET_URL = 'cafe://app/renderer.css';
const HTML = '<!doctype html><html lang="en" data-cafe-theme="dark"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Café</title><link rel="stylesheet" href="cafe://app/renderer.css"></head><body><div id="app"></div><script defer src="cafe://app/renderer.js"></script></body></html>';

/** Immutable host-owned bytes for the two reviewed renderer resources. */
class RendererBundle {
  /** @param {Buffer} javascript @param {Buffer} stylesheet */
  constructor(javascript, stylesheet) {
    this.javascript = javascript;
    this.stylesheet = stylesheet;
    Object.freeze(this);
  }
}
/** Fixed packaged filenames only; no URL or renderer-selected filesystem input.
 * @param {string} appRoot @returns {RendererBundle|undefined} */
function loadRendererBundle(appRoot) {
  try {
    if (!path.isAbsolute(appRoot) || path.resolve(appRoot) !== appRoot) throw new Error();
    const root = path.join(appRoot, 'dist', 'renderer');
    for (const directory of [path.join(appRoot, 'dist'), root]) {
      if (fs.existsSync(directory)) {
        const info = fs.lstatSync(directory);
        if (!info.isDirectory() || info.isSymbolicLink()) throw new Error();
      }
    }
    const manifestPath = path.join(root, 'manifest.json');
    if (!fs.existsSync(manifestPath)) {
      if (fs.existsSync(path.join(root, 'renderer.js')) || fs.existsSync(path.join(root, 'renderer.css'))) throw new Error();
      return undefined;
    }
    const metadata = fs.lstatSync(manifestPath);
    if (!metadata.isFile() || metadata.isSymbolicLink() || metadata.size > 65536) throw new Error();
    const manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf8'));
    if (!manifest || Object.keys(manifest).sort().join(',') !== 'files,schema' || manifest.schema !== 'cafe.renderer-assets.v1' || !Array.isArray(manifest.files) || manifest.files.length !== 2) throw new Error();
    const names = new Set();
    const buffers = new Map();
    for (const record of manifest.files) {
      if (!record || Object.keys(record).sort().join(',') !== 'bytes,path,sha256' || !['renderer.js', 'renderer.css'].includes(record.path) || names.has(record.path) || !/^[a-f0-9]{64}$/.test(record.sha256) || !Number.isSafeInteger(record.bytes) || record.bytes < 1 || record.bytes > 8 * 1024 * 1024) throw new Error();
      names.add(record.path);
      const location = path.join(root, record.path);
      const info = fs.lstatSync(location);
      if (!info.isFile() || info.isSymbolicLink() || info.size !== record.bytes) throw new Error();
      const bytes = fs.readFileSync(location);
      if (bytes.length !== record.bytes || createHash('sha256').update(bytes).digest('hex') !== record.sha256) throw new Error();
      buffers.set(record.path, bytes);
    }
    return new RendererBundle(buffers.get('renderer.js'), buffers.get('renderer.css'));
  } catch { throw new Error('RENDERER_ASSET_REJECTED'); }
}

/** Resource admission is an exact finite set, separate from frame navigation.
 * @param {string} url @param {RendererBundle|undefined} bundle */
function admitsResource(url, bundle) { return !!bundle && (url === JAVASCRIPT_URL || url === STYLESHEET_URL); }

/** @param {string} url @param {RendererBundle} bundle */
function rendererResponse(url, bundle) {
  if (url === JAVASCRIPT_URL) return new Response(new Uint8Array(bundle.javascript), { headers: { 'Content-Type': 'application/javascript; charset=utf-8', 'X-Content-Type-Options': 'nosniff' } });
  if (url === STYLESHEET_URL) return new Response(new Uint8Array(bundle.stylesheet), { headers: { 'Content-Type': 'text/css; charset=utf-8', 'X-Content-Type-Options': 'nosniff' } });
  throw new Error('RENDERER_RESOURCE_REJECTED');
}
exports.loadRendererBundle = loadRendererBundle;
exports.admitsResource = admitsResource;
exports.rendererResponse = rendererResponse;
exports.HTML = HTML;

exports.RendererBundle = RendererBundle;
