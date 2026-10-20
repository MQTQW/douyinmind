import type { SourceItem } from './api';

export function answerClipboardText(content: string, sources: SourceItem[] = []): string {
  return sources.length ? `${content}\n\n参考来源：\n${sources.map(source => `- ${source.title}\n  ${source.url}`).join('\n')}` : content;
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
