#!/usr/bin/env node
const fs = require('fs');
const execa = require('execa');
const { join } = require('path');

async function main() {
  const outDir = join(__dirname, 'dist');

  // Start fresh
  try {
    fs.rmSync(outDir, { recursive: true, force: true });
  } catch (err) {
    // noop

  }

  await execa(
    'ncc',
    [
      'build',
      join(__dirname, 'src/index.ts'),
      '-e',
      '@vercel/build-utils',
      '-o',
      outDir,
    ],
    { stdio: 'inherit' }
  );
}

main().catch(err => {
  console.error(err);
  process.exit(1);
});
