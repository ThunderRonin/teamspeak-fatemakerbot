import test from 'node:test';
import assert from 'node:assert/strict';
import { chunkMessage } from '../message-chunks.mjs';

test('long initiative lists are delivered completely at line boundaries', () => {
  const lines = ['[b]Initiative[/b]:', ...Array.from({length: 18}, (_, i) => `${i + 1}. [b]Hero${i}[/b] [color=lime]14[/color] [i](fixed)[/i]`)];
  const message = lines.join('\n');
  const chunks = chunkMessage(message);
  assert(chunks.length > 1);
  assert.equal(chunks.join('\n'), message);
  for (const chunk of chunks) assert(Buffer.byteLength(chunk, 'utf8') <= 400);
  for (const line of lines) assert(chunks.some(chunk => chunk.split('\n').includes(line)));
});

test('unicode and a line longer than the limit are not dropped', () => {
  const input = '火'.repeat(450);
  const chunks = chunkMessage(input);
  assert.equal(chunks.join(''), input);
  for (const chunk of chunks) assert(Buffer.byteLength(chunk, 'utf8') <= 400);
});
