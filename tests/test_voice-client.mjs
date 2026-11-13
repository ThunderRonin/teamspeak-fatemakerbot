import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = fileURLToPath(new URL('../', import.meta.url));

test('a corrupt persistent identity is preserved and never logged or replaced', () => {
  const directory = mkdtempSync(join(tmpdir(), 'fatemaker-identity-'));
  const path = join(directory, 'identity.json');
  const original = JSON.stringify({ identity: 'private-key-canary-that-must-not-be-logged' });
  writeFileSync(path, original, { mode: 0o600 });
  try {
    const env = Object.fromEntries(Object.entries(process.env).filter(([name]) => !name.startsWith('DICEBOT_')));
    const result = spawnSync(process.execPath, ['voice-client.mjs'], {
      cwd: root,
      env: { ...env, DICEBOT_IDENTITY_PATH: path },
      encoding: 'utf8',
      timeout: 5000,
    });
    assert.equal(result.error, undefined);
    assert.equal(result.status, 1);
    assert.match(result.stderr, /identity is corrupt/);
    assert.equal(readFileSync(path, 'utf8'), original);
    assert.doesNotMatch(result.stdout + result.stderr, /private-key-canary/);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});
