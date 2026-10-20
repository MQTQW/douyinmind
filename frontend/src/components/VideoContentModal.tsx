import { useEffect, useState } from 'react';
import * as api from '../api';

const time = (seconds: number) => {
  const value = Math.max(0, Math.floor(seconds));
  return `${Math.floor(value / 60)}:${String(value % 60).padStart(2, '0')}`;
};
const elapsed = (seconds?: number | null) => {
  if (seconds == null || !Number.isFinite(seconds)) return '未记录';
  if (seconds < 60) return `${seconds.toFixed(1)} 秒`;
  return `${Math.floor(seconds / 60)} 分 ${(seconds % 60).toFixed(1)} 秒`;
};

export default function VideoContentModal({ itemId, onClose }: { itemId: string; onClose: () => void }) {
  const [data, setData] = useState<api.VideoContent | null>(null);
  const [error, setError] = useState('');
  const [tab, setTab] = useState<'speech' | 'visual'>('speech');
  const hasVisualAnalysis = Boolean(data?.visual_analysis_text?.trim());
  const hasTranscript = Boolean(data?.transcript_text?.trim());
  const hasSavedContent = hasTranscript || hasVisualAnalysis;
  const isFailed = data?.status === 'failed';
  const failureMessage = data?.error_message || '';
  const hasFreshCookiesError = /fresh cookies/i.test(failureMessage);
  const isDownloadFailure = /^(音频|视频)下载失败/.test(failureMessage.trimStart()) || hasFreshCookiesError;
  const isRecognitionFailure = /文字.*不完整|文字格式|时间.*范围|模型未完整返回|自动重试/.test(failureMessage);
  const hasNoSpeech = /没有可识别的人声/.test(failureMessage);
  useEffect(() => {
    let active = true;
    setData(null);
    setError('');
    setTab('speech');
    api.getVideoContent(itemId).then(result => {
      if (!active) return;
      if (!result.success) setError(result.message || '未找到识别结果');
      else setData(result);
    }).catch(() => { if (active) setError('结果加载失败，请稍后重试。'); });
    return () => { active = false; };
  }, [itemId]);
  useEffect(() => {
    const close = (event: KeyboardEvent) => { if (event.key === 'Escape') onClose(); };
    window.addEventListener('keydown', close);
    return () => window.removeEventListener('keydown', close);
  }, [onClose]);

  return (
    <div className="fixed inset-0 z-50 bg-black/30 flex items-center justify-center p-4" onClick={onClose}>
      <section role="dialog" aria-modal="true" aria-labelledby="video-result-title" className="w-full max-w-3xl max-h-[85vh] bg-[var(--color-panel)] rounded-2xl shadow-xl flex flex-col" onClick={event => event.stopPropagation()}>
        <header className="flex items-start gap-4 px-6 py-5 border-b border-[var(--color-border)]">
          <div className="min-w-0 flex-1">
            <h2 id="video-result-title" className="font-display text-lg">{data?.title || (hasVisualAnalysis ? '文字稿与画面分析' : '完整文字稿')}</h2>
            <p className="mt-1 text-xs text-[var(--color-ink-muted)]">{isFailed && !hasSavedContent ? '查看失败原因，处理问题后可重新勾选入库。' : hasVisualAnalysis ? '完整文字稿和画面分析分别保存，时间对应原视频。' : '完整文字稿和逐段时间信息，时间对应原视频。'}</p>
          </div>
          <button onClick={onClose} aria-label="关闭识别结果" className="text-lg px-2">×</button>
        </header>
        {data && hasSavedContent && <div className="flex gap-2 px-6 pt-4" role="tablist" aria-label="视频识别结果">
          <button role="tab" aria-selected={tab === 'speech'} onClick={() => setTab('speech')} className={`px-4 py-2 rounded-xl text-sm ${tab === 'speech' ? 'bg-accent-light text-accent' : 'text-[var(--color-ink-muted)]'}`}>完整文字稿</button>
          {hasVisualAnalysis && <button role="tab" aria-selected={tab === 'visual'} onClick={() => setTab('visual')} className={`px-4 py-2 rounded-xl text-sm ${tab === 'visual' ? 'bg-accent-light text-accent' : 'text-[var(--color-ink-muted)]'}`}>画面分析</button>}
          <a className="ml-auto px-3 py-2 text-xs text-accent" href={`/api/knowledge/export/${encodeURIComponent(itemId)}?mode=original`}>{hasVisualAnalysis ? '导出全部结果' : '导出文字稿与时间信息'}</a>
        </div>}
        <div className="px-6 py-5 overflow-y-auto text-sm leading-relaxed">
          {error ? <p role="alert" className="text-red-600">{error}</p> : !data ? <p>正在加载识别结果…</p> : <>
            {data.processing_metrics ? <details className="mb-4 rounded-xl border border-[var(--color-border)] p-3 text-xs">
              <summary className="cursor-pointer font-medium">本次处理耗时：{elapsed(data.processing_metrics.total_processing_seconds)}</summary>
              <dl className="mt-3 grid grid-cols-2 gap-2">
                <div><dt className="text-[var(--color-ink-muted)]">下载与提取音频</dt><dd>{elapsed(data.processing_metrics.stages.download?.seconds)}</dd></div>
                <div><dt className="text-[var(--color-ink-muted)]">分段与声音转文字</dt><dd>{elapsed(data.processing_metrics.stages.recognition?.seconds)}</dd></div>
                <div><dt className="text-[var(--color-ink-muted)]">整理与资料入库</dt><dd>{elapsed(data.processing_metrics.stages.indexing?.seconds)}</dd></div>
              </dl>
              <p className="mt-2 text-[var(--color-ink-muted)]">仅统计这次处理，不含排队和之前的重试。{!data.processing_metrics.ended_at ? '记录尚未结束，暂不能作为完整耗时。' : data.processing_metrics.status === 'failed' ? '这次处理失败，已完成的阶段耗时仍保留。' : ''}</p>
            </details> : <p className="mb-3 text-xs text-[var(--color-ink-muted)]">此视频暂无处理耗时记录。</p>}
            {isFailed && <div role="alert" className="mb-4 rounded-xl border border-red-200 bg-red-50 p-4">
              <h3 className="font-semibold text-red-700">{hasSavedContent ? '入库未完成，已保存的识别结果仍可查看' : '入库失败，尚未生成完整文字稿'}</h3>
              <p className="mt-2 text-xs text-red-700">{isDownloadFailure ? '失败发生在下载视频或音频这一步，本次还没有开始 AI 识别。' : hasNoSpeech ? '当前只分析声音，这个视频没有识别到可用人声，因此没有文字稿可入库。' : isRecognitionFailure ? '视频已下载，但声音识别返回的文字或时间标记未通过检查，没有将不完整内容加入知识库。重试时会复用已保存并通过检查的分段。' : failureMessage ? '本次处理未完成，具体原因请查看下方技术详情。' : '暂未记录具体失败信息。'}</p>
              {hasFreshCookiesError && <p className="mt-2 text-xs text-red-700">下载工具没有取得抖音视频资料，这个提示不一定表示你未登录。</p>}
              {failureMessage && <details className="mt-3 text-xs text-red-700">
                <summary className="cursor-pointer">技术详情</summary>
                <p className="mt-2 whitespace-pre-wrap break-words">{failureMessage}</p>
              </details>}
              <p className="mt-3 text-xs text-[var(--color-ink-soft)]">关闭此窗口后，可以在侧边栏勾选这个失败的视频，再点击“入库所选视频”重试。</p>
            </div>}
            {data.warnings.length > 0 && <div className="mb-4 p-3 rounded-xl bg-amber-light text-amber text-xs">{data.warnings.map((warning, i) => <p key={i}>{warning}</p>)}</div>}
            {(!isFailed || hasSavedContent) && (tab === 'speech' || !hasVisualAnalysis ? <>
              <p className="whitespace-pre-wrap break-words">{data.transcript_text || (data.visual_analysis_text ? '本视频没有可识别的语音。' : '尚未获得文字稿。')}</p>
              {data.transcript_segments.length > 0 && <details className="mt-5 border-t border-[var(--color-border)] pt-3">
                <summary className="cursor-pointer text-xs text-[var(--color-ink-muted)]">查看带时间的逐段文字</summary>
                <div className="mt-3 space-y-3">{data.transcript_segments.map((segment, i) => <div key={i}><span className="text-xs text-accent">{time(segment.start_seconds)}–{time(segment.end_seconds)}</span><p className="whitespace-pre-wrap">{segment.text}</p></div>)}</div>
              </details>}
            </> : <p className="whitespace-pre-wrap break-words">{data.visual_analysis_text || '尚未获得画面分析。'}</p>)}
            {!isFailed && data.error_message && <p className="mt-4 text-xs text-red-600">处理状态：{data.error_message}</p>}
          </>}
        </div>
      </section>
    </div>
  );
}
