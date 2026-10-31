// Renders preview stills at chosen frames, bundling only once.
// Usage: node scripts/stills.mjs 150 300 900 ...
import {bundle} from '@remotion/bundler';
import {renderStill, selectComposition} from '@remotion/renderer';
import path from 'node:path';
import fs from 'node:fs';

const frames = process.argv.slice(2).map(Number);
const serveUrl = await bundle({entryPoint: path.resolve('src/index.ts')});
const composition = await selectComposition({serveUrl, id: 'NabdPromo'});
fs.mkdirSync('out/stills', {recursive: true});
console.log(`Composition: ${composition.durationInFrames} frames`);
for (const frame of frames) {
  const output = path.resolve(`out/stills/f${String(frame).padStart(4, '0')}.png`);
  await renderStill({composition, serveUrl, frame, output, scale: 0.5});
  console.log('wrote', output);
}
