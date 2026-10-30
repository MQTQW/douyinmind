import { ChevronRight } from 'lucide-react';
import { assistantTime } from '../assistantView';
import type { AssistantOverview } from '../api';

export default function NativeIntakeNotice({ summary }: { summary?: AssistantOverview['native_intake'] }) {
  if (!summary) return null;
  if (!summary.available) return <p className="assistant-native-intake-status" role="status">收件明细暂时无法读取，已有记录保留。</p>;
  if (!summary.unresolved_count) return summary.pending_count > 0
    ? <p className="assistant-native-intake-status" role="status">{summary.pending_count} 条指令正在核对受理条件。</p> : null;
  const recent = summary.recent ?? [];
  return <details className="assistant-intake-issues assistant-native-intake">
    <summary>{summary.unresolved_count} 条收件记录需核对<ChevronRight size={14} /></summary>
    <p>通知已登记，但尚未确认进入任务队列。请查看具体原因，避免重复发送。</p>
    {summary.pending_count > 0 && <p>另有 {summary.pending_count} 条指令正在核对受理条件。</p>}
    {recent.map(issue => <div key={issue.id}>
      <strong>{issue.message || '原评论或受理条件需要核对。'}</strong>
      {issue.comment_excerpt && <span>{issue.comment_excerpt}</span>}
      <small>首次记录：{assistantTime(issue.created_at)}</small>
    </div>)}
    {summary.unresolved_count > recent.length && <p>另有 {summary.unresolved_count - recent.length} 条记录需核对。</p>}
    {summary.observed_at && <p>最近核对：{assistantTime(summary.observed_at)}</p>}
  </details>;
}
