import Markdown from 'react-markdown';
import remarkGfm from 'remark-gfm';

/** Model output is text: never enable raw HTML or load remote images from it. */
export default function MessageContent({ content }: { content: string }) {
  return <div className="message-content">
    <Markdown remarkPlugins={[remarkGfm]} components={{
      a: ({ node: _node, ...props }) => <a {...props} target="_blank" rel="noopener noreferrer" />,
      img: ({ alt }) => <span className="text-[var(--color-ink-muted)]">{alt ? `[图片：${alt}]` : '[图片]'}</span>,
      table: ({ node: _node, ...props }) => <div className="message-table"><table {...props} /></div>,
    }}>{content}</Markdown>
  </div>;
}
