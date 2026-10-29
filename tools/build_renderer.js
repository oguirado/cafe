// @ts-check
'use strict';
const path = require('node:path');
const fs = require('node:fs');
const { createHash } = require('node:crypto');

/** Compile the reviewed Vue sources into two closed, offline renderer assets.
 * @param {string} [root] */
async function buildRenderer(root = path.resolve(__dirname, '..')) {
  if (Number(process.versions.node.split('.')[0]) !== 24) throw new Error('NODE_RUNTIME_REJECTED');
  const { build } = await import('vite');
  const { default: vue } = await import('@vitejs/plugin-vue');
  const result = await build({
    root, configFile: false, publicDir: false, logLevel: 'silent', plugins: [vue()],
    define: { 'process.env.NODE_ENV': JSON.stringify('production'), __VUE_OPTIONS_API__: 'true', __VUE_PROD_DEVTOOLS__: 'false', __VUE_PROD_HYDRATION_MISMATCH_DETAILS__: 'false' },
    build: {
      write: false, emptyOutDir: false, minify: false, sourcemap: false,
      lib: { entry: path.join(root, 'ui', 'src', 'main.js'), formats: ['iife'], name: 'CafeRenderer', fileName: () => 'renderer.js', cssFileName: 'renderer' },
    },
  });
  const outputs = (Array.isArray(result) ? result : [result]).flatMap((entry) => 'output' in entry ? entry.output : []);
  if (outputs.length !== 2 || outputs.some((entry) => !['renderer.js', 'renderer.css'].includes(entry.fileName))) throw new Error('RENDERER_OUTPUT_REJECTED');
  const out = path.join(root, 'dist', 'renderer');
  fs.mkdirSync(out, { recursive: true });
  const files = [];
  for (const entry of outputs) {
    const bytes = Buffer.from(entry.type === 'chunk' ? entry.code : entry.source);
    if (!bytes.length || bytes.length > 8 * 1024 * 1024) throw new Error('RENDERER_SIZE_REJECTED');
    fs.writeFileSync(path.join(out, entry.fileName), bytes);
    files.push({ path: entry.fileName, bytes: bytes.length, sha256: createHash('sha256').update(bytes).digest('hex') });
  }
  files.sort((a, b) => a.path.localeCompare(b.path));
  const manifest = { schema: 'cafe.renderer-assets.v1', files };
  fs.writeFileSync(path.join(out, 'manifest.json'), JSON.stringify(manifest) + '\n');
  return { status: 'pass', ...manifest };
}
exports.buildRenderer = buildRenderer;
if (require.main === module) buildRenderer().then((value) => console.log(JSON.stringify(value))).catch(() => {
  console.log(JSON.stringify({ status: 'fail', code: 'RENDERER_BUILD_REJECTED' })); process.exitCode = 1;
});
