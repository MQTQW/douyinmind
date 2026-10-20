export interface ServerSentEvent {
  event: string;
  data: unknown;
}

/** Read complete SSE events, regardless of network or UTF-8 chunk boundaries. */
export async function* readSSE(body: ReadableStream<Uint8Array>): AsyncGenerator<ServerSentEvent> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let event = '';
  let data: string[] = [];
  let finished = false;

  try {
    while (!finished) {
      const result = await reader.read();
      finished = result.done;
      buffer += finished ? decoder.decode() : decoder.decode(result.value, { stream: true });

      while (true) {
        const end = buffer.search(/[\r\n]/);
        if (end < 0) break;
        // A CR at the end may be the first half of a split CRLF separator.
        if (buffer[end] === '\r' && end === buffer.length - 1 && !finished) break;
        const line = buffer.slice(0, end);
        const width = buffer[end] === '\r' && buffer[end + 1] === '\n' ? 2 : 1;
        buffer = buffer.slice(end + width);

        if (line === '') {
          if (data.length > 0) {
            const payload = data.join('\n');
            const eventName = event || 'message';
            event = '';
            data = [];
            // Malformed data must surface as an error instead of silently losing text.
            yield { event: eventName, data: JSON.parse(payload) };
          } else {
            event = '';
          }
          continue;
        }
        if (line.startsWith(':')) continue;
        const colon = line.indexOf(':');
        const field = colon < 0 ? line : line.slice(0, colon);
        let value = colon < 0 ? '' : line.slice(colon + 1);
        if (value.startsWith(' ')) value = value.slice(1);
        if (field === 'event') event = value;
        if (field === 'data') data.push(value);
      }
    }
    // An event without its final blank line is incomplete and is not dispatched.
  } finally {
    try {
      if (!finished) await reader.cancel();
    } finally {
      reader.releaseLock();
    }
  }
}
