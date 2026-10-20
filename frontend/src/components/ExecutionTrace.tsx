import { useState } from 'react';
import type { TraceData } from '../api';

const ROUTE_LABELS: Record<string, string> = {
  direct: '直接回复',
  db_list: '列表查询',
  db_content: '内容概览',
  vector: '语义检索',
};

const RETRIEVAL_STAGES = [
  ['query_embedding', '把问题转换成检索用的数字'],
  ['vector_search', '按含义查找原文'],
  ['scope_documents', '读取当前范围原文'],
  ['keyword_fusion', '合并含义与关键词匹配结果'],
  ['neighbor_expansion', '补齐相邻原句'],
] as const;

const validNumber = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value) && value >= 0;
const STEP_LABELS: Record<string, string> = {
  '路由判断': '判断问题类型',
  '语义与关键词检索': '按含义与关键词找原文',
  '关键词查找（语义服务暂不可用）': '关键词查找（后备）',
  '上下文构建': '整理回答所需资料',
  'LLM 生成': 'AI 生成回答',
};

export default function ExecutionTrace({ trace, latency_ms }: { trace: TraceData; latency_ms?: number }) {
  const [open, setOpen] = useState(false);

  if (!trace || !trace.steps) return null;

  const hasChunks = trace.chunks && trace.chunks.length > 0;
  const lexicalFallback = trace.retrieval_mode === 'lexical_fallback';
  const details = trace.retrieval_details;
  const context = trace.context_stats;
  const routeLabel = trace.route === 'vector' && lexicalFallback
    ? '关键词查找（后备）'
    : trace.route === 'vector' && trace.retrieval_mode === 'hybrid'
      ? '组合查找'
      : ROUTE_LABELS[trace.route] || trace.route;

  return (
    <div className="mt-2 border border-[var(--color-border)] rounded-xl overflow-hidden text-xs">
      {/* Header */}
      <button
        onClick={() => setOpen(!open)}
        className="w-full flex flex-wrap items-center gap-x-2 gap-y-1 px-3 py-2 text-left hover:bg-black/[0.02] transition-colors"
      >
        <span className={`w-2 h-2 rounded-full ${open ? 'bg-accent' : 'bg-success'}`} />
        <span className="font-semibold text-[var(--color-ink-soft)]">
          查找过程 · {routeLabel}
        </span>
        {hasChunks && (
          <span className="text-[var(--color-ink-muted)] ml-auto">
            {trace.chunks.length} 段资料
          </span>
        )}
        <svg className={`w-3 h-3 text-[var(--color-ink-muted)] transition-transform ${open ? 'rotate-180' : ''}`} fill="none" viewBox="0 0 24 24" stroke="currentColor">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
        </svg>
      </button>
      {lexicalFallback && (
        <p role="status" className="px-3 pb-2 text-[var(--color-ink-muted)]">
          语义服务暂时不可用，本次使用关键词查找
        </p>
      )}

      {/* Body */}
      {open && (
        <div className="border-t border-dashed border-[var(--color-border)] px-3 py-2 space-y-2">
          {validNumber(latency_ms) && <p className="text-[var(--color-ink-muted)]">本次回答耗时 {latency_ms}ms</p>}
          {/* Steps */}
          <div className="space-y-1">
            {trace.steps.map((step, i) => (
              <div key={i} className="flex items-center gap-2">
                <span className="w-4 h-4 rounded-full bg-accent-light text-accent flex items-center justify-center font-mono text-[10px] font-bold">{i + 1}</span>
                <span className="flex-1 text-[var(--color-ink-soft)]">{STEP_LABELS[step.name] || step.name}</span>
                <span className="font-mono text-[var(--color-ink-muted)]">{step.time_ms}ms</span>
              </div>
            ))}
          </div>

          {details && (
            <div className="rounded-lg bg-black/[0.02] px-3 py-2 text-[var(--color-ink-muted)] space-y-1">
              <p className="font-semibold text-[var(--color-ink-soft)]">查找过程明细</p>
              {RETRIEVAL_STAGES.map(([key, label]) => {
                const ms = details.stages_ms?.[key];
                const stageLabel = key === 'keyword_fusion' && lexicalFallback ? '按关键词匹配并排序' : label;
                return validNumber(ms) ? <div key={key} className="flex gap-3 justify-between"><span>{stageLabel}</span><span className="font-mono">{ms}ms</span></div> : null;
              })}
              {validNumber(details.counts?.scope_records) && <p>当前范围可供匹配的原文：{details.counts.scope_records} 段</p>}
              {validNumber(details.counts?.dense_candidates) && <p>按含义找到的候选：{details.counts.dense_candidates} 段</p>}
              {validNumber(context?.provided_chunks) && validNumber(context?.provided_chars) && <p>交给 AI 回答的资料：{context.provided_chunks} 段，共 {context.provided_chars} 字符</p>}
              <p className="text-[10px]">以上是查找步骤的细分耗时，已计入上方查找阶段。</p>
            </div>
          )}

          {/* Chunks */}
          {hasChunks && (
            <div className="pt-2 border-t border-dashed border-[var(--color-border)]">
              <p className="text-[10px] font-bold text-[var(--color-ink-muted)] mb-1">提供给 AI 的原文</p>
              <p className="text-[10px] text-[var(--color-ink-muted)] mb-1.5">已查到并提供，不代表全部被回答引用。</p>
              <div className="space-y-1.5">
                {trace.chunks.map((chunk, i) => {
                  const scoreLabel = chunk.score_kind === 'lexical' || lexicalFallback
                    ? '关键词匹配'
                    : typeof chunk.score === 'number' && Number.isFinite(chunk.score)
                      ? `${(chunk.score * 100).toFixed(0)}%`
                      : '';
                  return (
                  <div key={i} className="p-2 rounded-lg bg-white/60 hover:bg-white/90 transition-colors cursor-default">
                    <div className="flex items-center justify-between gap-2">
                      <span className="font-semibold text-[var(--color-ink)] truncate">{chunk.title}</span>
                      {scoreLabel && <span className="font-mono text-[var(--color-accent)] flex-shrink-0">{scoreLabel}</span>}
                    </div>
                    <p className="text-[var(--color-ink-muted)] mt-1 line-clamp-2 leading-relaxed">{chunk.text}</p>
                    {typeof chunk.full_text === 'string' && chunk.full_text.length > 0 && (
                      <details className="mt-2">
                        <summary className="cursor-pointer text-accent focus-visible:outline-accent">查看本次提供的原文</summary>
                        <div className="mt-2 max-h-64 overflow-y-auto whitespace-pre-wrap break-words select-text leading-relaxed text-[var(--color-ink-soft)]">
                          {chunk.full_text}
                        </div>
                      </details>
                    )}
                  </div>
                  );
                })}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
