// Documentation capture server: frontend assets only, with no API proxy.
// Send "close" on stdin to stop this owned preview without touching services.
import { createServer } from 'vite';
import react from '@vitejs/plugin-react';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const root = fileURLToPath(new URL('../', import.meta.url));
const server = await createServer({
  configFile: false,
  root,
  plugins: [react()],
  cacheDir: path.join(root, 'node_modules/.cache/docs-preview'),
  server: { host: '127.0.0.1', port: 5197, strictPort: true, hmr: false, proxy: {} },
});
await server.listen();
console.log('Synthetic documentation frontend: http://127.0.0.1:5197 (no API proxy)');
process.stdin.setEncoding('utf8');
process.stdin.on('data', async input => {
  if (input.trim() === 'close') { await server.close(); process.exit(0); }
});
for (const signal of ['SIGINT', 'SIGTERM']) process.on(signal, async () => {
  await server.close(); process.exit(0);
});
