import Markdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { useMemo, useRef } from 'react';
import type { SourceItem, TraceData } from '../api';
import { citationReferences, citationRemarkPlugin } from '../citations';
import CitationSources from './CitationSources';

const EMPTY_SOURCES: SourceItem[] = [];

/** Model output is text: never enable raw HTML or load remote images from it. */
export default function MessageContent({ content, sources = EMPTY_SOURCES, trace }: { content: string; sources?: SourceItem[]; trace?: TraceData }) {
  const references = useMemo(() => citationReferences(sources), [sources]);
  const plugin = useMemo(() => citationRemarkPlugin(content, references), [content, references]);
  const details = useRef(new Map<number, HTMLDetailsElement>());
  const origin = useRef<{ number: number; element: HTMLButtonElement } | null>(null);
  const reveal = (number: number, trigger: HTMLButtonElement) => {
    const panel = details.current.get(number);
    if (!panel) return;
    origin.current = { number, element: trigger };
    panel.open = true;
    panel.querySelector('summary')?.focus({ preventScroll: true });
    panel.scrollIntoView({ block: 'nearest', behavior: 'auto' });
  };
  return <>
    <div className="message-content"><Markdown remarkPlugins={[remarkGfm, plugin]} components={{
      a: ({ node, ...props }) => {
        const number = node?.properties?.['data-citation-number'];
        if (typeof number === 'number' && references.some(reference => reference.number === number)
          && props.href === `#answer-citation-${number}`) {
          return <button className="answer-citation" type="button" aria-label={`查看来源 ${number}`}
            onClick={event => reveal(number, event.currentTarget)}>[{number}]</button>;
        }
        return <a {...props} target="_blank" rel="noopener noreferrer" />;
      },
      img: ({ alt }) => <span className="text-[var(--color-ink-muted)]">{alt ? `[图片：${alt}]` : '[图片]'}</span>,
      table: ({ node: _node, ...props }) => <div className="message-table"><table {...props} /></div>,
    }}>{content}</Markdown></div>
    <CitationSources references={references} trace={trace}
      register={(number, element) => { if (element) details.current.set(number, element); else details.current.delete(number); }}
      onEscape={number => {
        const panel = details.current.get(number);
        if (panel) panel.open = false;
        if (origin.current?.number === number && origin.current.element.isConnected) origin.current.element.focus();
        else panel?.querySelector('summary')?.focus();
      }} />
  </>;
}
