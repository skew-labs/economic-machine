import {build} from 'esbuild';
import {fileURLToPath} from 'node:url';
import path from 'node:path';
const here = path.dirname(fileURLToPath(import.meta.url));
await build({entryPoints: {entry: path.join(here, 'privy.jsx')},
  outdir: path.resolve(here, '../../web/privy'), bundle: true, splitting: true,
  format: 'esm', platform: 'browser', target: ['es2022'], minify: true,
  define: {'process.env.NODE_ENV': '"production"'}, legalComments: 'linked',
  chunkNames: 'chunk-[hash]', assetNames: 'asset-[hash]'});
