import type { SourceItem } from './api';
import { citationReferences, compactCitationText, safeSourceUrl } from './citations';

export function answerClipboardText(content: string, sources: SourceItem[] = []): string {
  const references = citationReferences(sources);
  if (!references.length) return content;
  return `${compactCitationText(content, references)}\n\n参考来源：\n${references.map(reference =>
    `[${reference.number}] ${reference.source.title}\n  ${safeSourceUrl(reference.source) ?? '原视频链接暂不可用'}`).join('\n')}`;
}

export async function copyText(text: string): Promise<void> {
  try {
    if (navigator.clipboard?.writeText) { await navigator.clipboard.writeText(text); return; }
  } catch { /* Fall back for clipboard permissions or older browsers. */ }
  const previous = document.activeElement;
  const input = document.createElement('textarea');
  input.value = text;
  input.readOnly = true;
  input.style.cssText = 'position:fixed;opacity:0;pointer-events:none;left:0;top:0';
  document.body.append(input);
  try {
    input.select();
    if (!document.execCommand('copy')) throw new Error('无法复制，请选中回答文字手动复制。');
  } finally {
    input.remove();
    if (previous instanceof HTMLElement) previous.focus();
  }
}
