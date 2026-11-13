// Keep complete lines together when possible and bound each message by UTF-8 bytes.
export function chunkMessage(message, limit = 400) {
  const chunks = [];
  let current = '';
  for (const line of message.split('\n')) {
    const combined = current ? `${current}\n${line}` : line;
    if (Buffer.byteLength(combined, 'utf8') <= limit) {
      current = combined;
      continue;
    }
    if (current) chunks.push(current);
    current = '';
    for (const character of line) {
      if (Buffer.byteLength(current + character, 'utf8') > limit) {
        chunks.push(current);
        current = '';
      }
      current += character;
    }
  }
  if (current) chunks.push(current);
  return chunks;
}
