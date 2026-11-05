import type { SourceItem, TraceData } from './api';

export interface CitationReference { number: number; source: SourceItem }
export interface CitationToken { start: number; end: number; raw: string; reference: CitationReference }
const MARKER = /\[来源\s*[:：]\s*([^\]]+)\]/g;

/** Numbers belong to this message's source list, never a guessed retrieval ordinal. */
export function citationReferences(sources: SourceItem[] = []): CitationReference[] {
  const byId = new Map<string, SourceItem[]>();
  for (const source of sources) {
    if (!source || typeof source.platform_item_id !== 'string' || !/^[\w-]{1,64}$/.test(source.platform_item_id)
      || typeof source.title !== 'string' || !source.title.trim()) continue;
    const group = byId.get(source.platform_item_id) ?? [];
    group.push(source); byId.set(source.platform_item_id, group);
  }
  return [...byId.values()].filter(group => group.every(source => source.title === group[0].title && source.url === group[0].url))
    .map((group, index) => ({ number: index + 1, source: group[0] }));
}

export function resolveCitation(label: string, references: CitationReference[]): CitationReference | undefined {
  // Final sources can be a filtered subset of retrieval results. "资料2" does
  // not necessarily mean sources[1], even when only two source cards are shown.
  if (/^资料\s*\d+$/.test(label.trim())) return undefined;
  const matches = references.filter(reference => reference.source.title.trim() === label.trim());
  return matches.length === 1 ? matches[0] : undefined;
}

export function safeSourceUrl(source: SourceItem): string | undefined {
  try {
    const url = new URL(source.url);
    if (url.protocol !== 'https:' || !['www.douyin.com', 'douyin.com'].includes(url.hostname)
      || url.username || url.password || url.port || url.pathname !== `/video/${source.platform_item_id}`) return undefined;
    return url.href;
  } catch { return undefined; }
}

export function sourcePreviewTitle(title: string): string {
  return title.split(/\r?\n/).find(line => line.trim())?.trim() || title;
}

function bracketEnd(text: string, start: number, opening: string, closing: string): number {
  let depth = 0;
  for (let index = start; index < text.length; index++) {
    if (text[index] === '\\') { index++; continue; }
    if (text[index] === opening) depth++;
    if (text[index] === closing && --depth === 0) return index + 1;
  }
  return -1;
}

/** Conservative literal ranges shared by display and copied Markdown. */
export function citationTokens(content: string, references: CitationReference[]): CitationToken[] {
  const tokens: CitationToken[] = [];
  const quotes: string[] = [];
  const closing: Record<string, string> = { '“': '”', '‘': '’', '「': '」', '『': '』', '《': '》', '"': '"', "'": "'" };
  let fence: { character: string; length: number } | undefined;
  for (let index = 0; index < content.length;) {
    const lineStart = index === 0 || content[index - 1] === '\n';
    if (lineStart) {
      const end = content.indexOf('\n', index);
      const lineEnd = end < 0 ? content.length : end + 1;
      const line = content.slice(index, lineEnd);
      const run = line.match(/^ {0,3}(`{3,}|~{3,})/);
      if (fence) {
        if (run && run[1][0] === fence.character && run[1].length >= fence.length && /^\s*$/.test(line.slice(run[0].length))) fence = undefined;
        index = lineEnd; continue;
      }
      if (run) { fence = { character: run[1][0], length: run[1].length }; index = lineEnd; continue; }
      // Literal code and Markdown block quotations never acquire source authority.
      if (/^(?: {0,3}>| {4}|\t)/.test(line) || /^ {0,3}\[[^\]]+\]:/.test(line)) { index = lineEnd; continue; }
    }
    const character = content[index];
    if (character === '\\') { index += 2; continue; }
    const bareUrl = /[hHwW]/.test(character) ? content.slice(index).match(/^(?:https?:\/\/|www\.)[^\s<>]*/i) : null;
    if (bareUrl && (index === 0 || !/[\p{L}\p{N}_]/u.test(content[index - 1]))) {
      index += bareUrl[0].length; continue;
    }
    if (character === '<') {
      const opening = content.slice(index).match(/^<([a-z][\w-]*)\b[^>]*>/i);
      if (opening) {
        const end = content.toLowerCase().indexOf(`</${opening[1].toLowerCase()}>`, index + opening[0].length);
        index = end < 0 ? index + opening[0].length : end + opening[1].length + 3;
        continue;
      }
    }
    if (character === '`') {
      const run = content.slice(index).match(/^`+/)![0];
      const end = content.indexOf(run, index + run.length);
      if (end < 0) break; // An unfinished streamed code span stays literal.
      index = end + run.length; continue;
    }
    if (character === quotes[quotes.length - 1]) { quotes.pop(); index++; continue; }
    if (closing[character] && !(character === "'" && /[\p{L}\p{N}]/u.test(content[index - 1] || '') && /[\p{L}\p{N}]/u.test(content[index + 1] || ''))) {
      quotes.push(closing[character]); index++; continue;
    }
    if (character === '[') {
      const end = bracketEnd(content, index, '[', ']');
      if (end > 0 && ['(', '['].includes(content[end])) {
        const linkEnd = bracketEnd(content, end, content[end], content[end] === '(' ? ')' : ']');
        if (linkEnd < 0) break;
        index = linkEnd; continue;
      }
      MARKER.lastIndex = index;
      const marker = MARKER.exec(content);
      if (marker?.index === index) {
        const reference = !quotes.length ? resolveCitation(marker[1], references) : undefined;
        if (reference) tokens.push({ start: index, end: MARKER.lastIndex, raw: marker[0], reference });
        index = MARKER.lastIndex; continue;
      }
    }
    index++;
  }
  return tokens;
}

export function compactCitationText(content: string, references: CitationReference[]): string {
  let result = content;
  for (const token of citationTokens(content, references).reverse()) {
    result = result.slice(0, token.start) + `[${token.reference.number}]` + result.slice(token.end);
  }
  return result;
}

interface MarkdownNode {
  type: string; value?: string; children?: MarkdownNode[];
  position?: { start: { offset?: number }; end: { offset?: number } };
  url?: string; data?: { hProperties: Record<string, string | number> };
}

/** Transform real text nodes only; source Markdown links/code/raw HTML stay intact. */
export function citationRemarkPlugin(content: string, references: CitationReference[]) {
  const tokens = citationTokens(content, references);
  return () => (root: unknown) => {
    function walk(node: MarkdownNode) {
      if (['link', 'linkReference', 'image', 'imageReference', 'inlineCode', 'code', 'html', 'blockquote'].includes(node.type) || !node.children) return;
      node.children = node.children.flatMap(child => {
        if (child.type !== 'text' || typeof child.value !== 'string') { walk(child); return [child]; }
        const start = child.position?.start.offset, end = child.position?.end.offset;
        if (start == null || end == null) return [child];
        const rawMatches = [...content.slice(start, end).matchAll(new RegExp(MARKER.source, 'g'))];
        const valueMatches = [...child.value.matchAll(new RegExp(MARKER.source, 'g'))];
        if (rawMatches.length !== valueMatches.length) return [child];
        const pieces: MarkdownNode[] = []; let cursor = 0;
        rawMatches.forEach((match, index) => {
          const token = tokens.find(item => item.start === start + match.index && item.raw === valueMatches[index][0]);
          if (!token) return;
          const valueIndex = valueMatches[index].index!;
          if (valueIndex > cursor) pieces.push({ type: 'text', value: child.value!.slice(cursor, valueIndex) });
          pieces.push({ type: 'link', url: `#answer-citation-${token.reference.number}`,
            data: { hProperties: { 'data-citation-number': token.reference.number } },
            children: [{ type: 'text', value: `[${token.reference.number}]` }] });
          cursor = valueIndex + token.raw.length;
        });
        if (!pieces.length) return [child];
        if (cursor < child.value.length) pieces.push({ type: 'text', value: child.value.slice(cursor) });
        return pieces;
      });
    }
    walk(root as MarkdownNode);
  };
}

export function providedSourceChunks(reference: CitationReference, trace?: TraceData): { chunk_id: string; text: string }[] {
  const seen = new Set<string>();
  return (trace?.chunks ?? []).flatMap(chunk => {
    if (typeof chunk.chunk_id !== 'string' || !chunk.chunk_id.startsWith(reference.source.platform_item_id + ':')
      || chunk.title !== reference.source.title || typeof chunk.full_text !== 'string' || !chunk.full_text.trim()
      || seen.has(chunk.chunk_id)) return [];
    seen.add(chunk.chunk_id); return [{ chunk_id: chunk.chunk_id, text: chunk.full_text }];
  });
}
