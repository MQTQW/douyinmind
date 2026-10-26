import type { AssistantDiagnostics } from '../api';
import { assistantTime } from '../assistantView';

function buildLabel(build: AssistantDiagnostics['backend_build'] | undefined): string {
  if (!build?.source_version) return '未记录启动版本';
  return `${build.source_version.slice(0, 8)} · ${build.matches_disk === true ? '与磁盘一致' : build.matches_disk === false ? '有更新待加载' : '尚未核对'}`;
}

export default function AssistantOperations({ diagnostics, demo, heartbeatAt, captureCycles }: { diagnostics?: AssistantDiagnostics; demo: boolean; heartbeatAt?: string | number | null; captureCycles?: string }) {
  if (demo) return null;
  return <details className="assistant-operations assistant-intake-issues">
    <summary>运行版本与收件范围</summary>
    <p>助手最近报告（心跳）：{assistantTime(heartbeatAt)}{captureCycles && <><br />{captureCycles}</>}</p>
    <p>暂停处理时新指令继续排队；停止后不再收件。后台已受理的入库会继续，任务记录保留。</p>
    {!diagnostics ? <p>后台尚未提供本轮版本信息。网页可打开，不代表后台与助手已加载最新代码。</p> : <>
      <dl className="assistant-version-grid">
        <div><dt>后台启动版本</dt><dd>{buildLabel(diagnostics.backend_build)}</dd></div>
        <div><dt>助手启动版本</dt><dd>{buildLabel(diagnostics.helper_build)}</dd></div>
      </dl>
      <p>版本号是源码指纹（用于区分启动时的代码版本），不能代替实际功能验证。</p>
      <p>约每 {diagnostics.intake_scope.poll_seconds} 秒核对一轮，每批最多检查 {diagnostics.intake_scope.visible_candidates_per_batch} 条可见通知。可见范围之外和延迟出现的提醒仍可能漏收。</p>
      {Boolean(diagnostics.unverified_notice_deferrals) && <p>有 {diagnostics.unverified_notice_deferrals} 条提醒原评论暂未核对，正在等待重查；尚未计作收到指令。</p>}
      <p>B端回读可见 {diagnostics.delivery.b_view_visible} 条；回复结果待核对 {diagnostics.delivery.requires_review} 条。B能看到不代表提问者或所有人都能看到。</p>
      {diagnostics.notices.map(notice => <p key={notice.code} role="status">{notice.message}</p>)}
    </>}
  </details>;
}
