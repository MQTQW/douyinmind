import type { CitationReference } from '../citations';
import type { TraceData } from '../api';
import { providedSourceChunks, safeSourceUrl, sourcePreviewTitle } from '../citations';
import { ExternalLink } from 'lucide-react';

export default function CitationSources({ references, trace, register, onEscape }: {
  references: CitationReference[]; trace?: TraceData;
  register: (number: number, element: HTMLDetailsElement | null) => void;
  onEscape: (number: number) => void;
}) {
  if (!references.length) return null;
  return <div className="answer-sources" aria-label="已引用的视频来源">
    <p className="answer-sources-label">已引用的视频来源</p>
    {references.map(reference => {
      const chunks = providedSourceChunks(reference, trace);
      const url = safeSourceUrl(reference.source);
      return <details className="answer-source-detail" key={reference.source.platform_item_id}
        ref={element => register(reference.number, element)}
        onKeyDown={event => { if (event.key === 'Escape') { event.preventDefault(); onEscape(reference.number); } }}>
        <summary><span className="source-number">[{reference.number}]</span>
          <span className="source-preview-title">{sourcePreviewTitle(reference.source.title)}</span>
          <span className="source-disclosure-label"><span className="source-open-label">查看来源</span><span className="source-close-label">收起</span></span></summary>
        <div className="source-detail-body">
          <h4>完整视频标题</h4><div className="source-full-title" tabIndex={0} role="region"
            aria-label={`来源 ${reference.number} 的完整标题`}>{reference.source.title}</div>
          {url ? <a className="source-original-link" href={url} target="_blank" rel="noopener noreferrer">打开原视频<ExternalLink size={14} aria-hidden="true" /></a>
            : <p className="source-detail-note">原视频链接暂不可用。</p>}
          {chunks.length ? <>
            <h4>本次提供的同视频原文</h4>
            <p className="source-detail-note">这些片段交给了 AI，可对照回答核查具体依据。</p>
            {chunks.map((chunk, index) => <div className="source-original-text" key={chunk.chunk_id}
              tabIndex={0} role="region" aria-label={`来源 ${reference.number} 的原文片段 ${index + 1}`}>{chunk.text}</div>)}
          </> : <p className="source-detail-note">这条回答未保存可定位的原文片段，可打开原视频核查。</p>}
        </div>
      </details>;
    })}
  </div>;
}
