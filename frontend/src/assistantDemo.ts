import type { AssistantOverview, AssistantTask } from './api';

export const demoOverview: AssistantOverview = {
  available: true, control: { state: 'paused', desired_state: 'paused', heartbeat_at: '2026-10-21T09:20:00Z' },
  counts: { received: 6, queued: 1, processing: 1, completed: 2, needs_review: 2 },
  capabilities: { start: true, pause: false, stop: true, multi_user: false, public_visibility: 'b_view_only' },
  binding: { binding_id: 'demo-binding', sender_display_name: '我的抖音账号（演示）', receiver_display_name: 'DouyinMind 助手（演示）',
    knowledge_base_id: 7, knowledge_base_name: '学习与工作', public_reply_allowed: true, allow_import: true, instance_id: '本机电脑（演示）' },
};

export const demoTasks: AssistantTask[] = [
  { id: 'demo-001', kind: 'import', state: 'b_view_visible', video_id: '7000000000000000001', video_title: 'Git 工作流程：四个区域如何协作',
    comment_text: '@助手 请把这个视频加入“学习与工作”知识库。', import_state: 'index_ready', reply_state: 'b_view_visible',
    created_at: '2026-10-21T09:14:00Z', updated_at: '2026-10-21T09:17:00Z', knowledge_base_id: 7,
    actions: { can_reconcile: true, can_resume: false }, usage: { total_tokens: 2480, model_calls: 2, elapsed_ms: 124000 },
    reply: { text: '这条视频已完成入库，可以在“学习与工作”中提问。', state: 'b_view_visible' } },
  { id: 'demo-002', kind: 'qa', state: 'delivery_unknown', video_title: '工作区和暂存区有什么区别？',
    comment_text: '@助手 根据学习与工作知识库，解释工作区与暂存区。', reply_state: 'delivery_unknown',
    created_at: '2026-10-21T09:18:00Z', updated_at: '2026-10-21T09:19:00Z', knowledge_base_id: 7,
    actions: { can_reconcile: true, can_resume: false }, usage: { total_tokens: 1800, model_calls: 2, elapsed_ms: 12000 },
    reply: { text: '工作区用于编辑；暂存区用于选择本次提交的内容。', state: 'delivery_unknown' } },
  { id: 'demo-003', kind: 'import', state: 'executing', video_title: '如何把学习笔记整理成可查找的资料',
    comment_text: '@助手 请把这个视频加入“学习与工作”知识库。', import_state: 'transcribing',
    created_at: '2026-10-21T09:19:00Z', updated_at: '2026-10-21T09:20:00Z', knowledge_base_id: 7,
    actions: { can_reconcile: false, can_resume: false } },
  { id: 'demo-004', kind: 'import', state: 'queued', video_title: '一条等待电脑处理的新视频',
    comment_text: '@助手 请保存到“学习与工作”。', import_state: 'waiting_device',
    created_at: '2026-10-21T09:20:00Z', knowledge_base_id: 7, actions: { can_reconcile: false, can_resume: true } },
  { id: 'demo-005', kind: 'qa', state: 'verification_required', video_title: '回答已准备，等待本人完成核验',
    comment_text: '@助手 总结知识库里的三个要点。', reply_state: 'verification_required',
    created_at: '2026-10-21T09:16:00Z', knowledge_base_id: 7, actions: { can_reconcile: true, can_resume: false } },
  { id: 'demo-006', kind: 'qa', state: 'completed_private', video_title: '只在本机查看的一次问答',
    comment_text: '@助手 根据“学习与工作”回答问题，本次不公开。', reply_state: 'ready',
    created_at: '2026-10-21T09:12:00Z', knowledge_base_id: 7, actions: { can_reconcile: false, can_resume: false },
    reply: { text: '这是保存在自己电脑中的演示回答，没有提交到抖音。', state: 'ready' },
    usage: { total_tokens: 980, model_responses: 2, elapsed_ms: 8300 } },
];

export function demoTaskDetail(id: string): AssistantTask | null {
  const task = demoTasks.find(item => item.id === id);
  if (!task) return null;
  return { ...task, sources: task.kind === 'qa' ? [{ source_id: '资料1', title: 'Git 工作流程（演示来源）', chunk_id: 'demo:1' }] : [],
    timeline: [
      { id: 1, kind: 'received', created_at: task.created_at, label: '收到已绑定账号的指令' },
      { id: 2, kind: 'model_request', created_at: task.created_at, label: '理解本次需求', elapsed_ms: 2600 },
      { id: 3, kind: 'tool_call', created_at: task.updated_at, metadata: { tool_name: task.kind === 'import' ? 'save_current_video' : 'query_knowledge_base' } },
      { id: 4, kind: task.state === 'b_view_visible' ? 'b_view_visible' : 'decision', created_at: task.updated_at,
        label: task.state === 'delivery_unknown' ? '发送结果未确认，停止重发' : task.state === 'b_view_visible' ? '助手账号回读可见' : '记录当前处理状态' },
    ] };
}
