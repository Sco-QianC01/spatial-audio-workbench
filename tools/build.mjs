import assert from 'node:assert/strict';
import {cp, mkdir, readFile, rm, writeFile} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';

const root = new URL('../', import.meta.url);
const output = new URL('dist/', root);
await rm(output, {recursive: true, force: true});
await mkdir(output, {recursive: true});
for (const directory of ['spatial-audio', 'public', 'docs']) {
  await cp(new URL(`${directory}/`, root), new URL(`${directory}/`, output), {recursive: true});
}
for (const file of ['README.md', 'LICENSE']) {
  await cp(new URL(file, root), new URL(file, output));
}
await writeFile(new URL('index.html', output), `<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Spatial Audio Workbench</title><meta http-equiv="refresh" content="0;url=./spatial-audio/"></head><body><a href="./spatial-audio/">打开空间音频工作台</a></body></html>
`);
const entry = await readFile(new URL('spatial-audio/index.html', output), 'utf8');
assert.ok(entry.includes('../public/spatial-icon.svg'));
assert.ok(!entry.includes('music-prompt'));
for (const path of ['public/spatial-icon.svg', 'public/vendor/three/three.module.min.js', 'spatial-audio/assets/listener-head.json']) {
  assert.ok((await readFile(new URL(path, output))).length > 0, `Missing build asset: ${path}`);
}
console.log(`Built static workbench: ${fileURLToPath(output)}`);
