import { useEffect, useState } from 'react';
import * as api from '../api';

export default function AssistantUsageSettings({ demo, admin }: { demo: boolean; admin: api.AssistantAdminSession | null }) {
  const [usage, setUsage] = useState<api.AssistantBudgetSummary | null>(null);
  const [taskLimit, setTaskLimit] = useState('');
  const [dayLimit, setDayLimit] = useState('');
  const [revision, setRevision] = useState<number | null>(null);
  const [editing, setEditing] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  useEffect(() => {
    if (demo) return;
    const controller = new AbortController();
    let polling = false;
    const load = async () => {
      if (polling || document.visibilityState === 'hidden') return;
      polling = true;
      try {
        const value = await api.getAssistantUsage(controller.signal);
        if (!controller.signal.aborted) {
          setUsage(value); setError('');
          if (!editing && value.policy) {
            setTaskLimit(value.policy.per_task_tokens?.toString() ?? '');
            setDayLimit(value.policy.daily_tokens?.toString() ?? '');
            setRevision(value.policy.revision);
          }
        }
      } catch (reason) {
        if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : '用量记录暂不可读。');
      } finally { polling = false; }
    };
    void load();
    const timer = window.setInterval(() => void load(), 15000);
    return () => { controller.abort(); clearInterval(timer); };
  }, [demo, editing, refresh]);

  const save = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!admin || busy || revision == null || !usage?.initialized) return;
    const amount = (value: string): number | null => {
      if (!value.trim()) return null;
      if (!/^\d+$/.test(value) || !Number.isSafeInteger(Number(value)) || Number(value) > 1_000_000_000_000) throw new Error('额度请输入0至1000000000000的整数，或留空。');
      return Number(value);
    };
    setBusy(true); setError(''); setNotice('');
    try {
      const result = await api.setAssistantUsageLimits(admin.admin_token, {
        per_task_tokens: amount(taskLimit), daily_tokens: amount(dayLimit), expected_revision: revision,
      });
      setUsage(result); setEditing(false); setRefresh(value => value + 1);
      setNotice('额度已保存。不会重跑旧任务或释放未核清用量；已发出的请求不受此操作撤回。');
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '额度未保存。');
    } finally { setBusy(false); }
  };
  const number = (value?: number) => value == null ? '未记录' : value.toLocaleString('zh-CN');
  return <section className="assistant-usage-settings assistant-user-card" aria-label="助手用量与额度">
    <h4>助手用量与额度</h4>
    {demo ? <p>演示模式不读取或修改真实用量。</p> : <>
      {error && <p role="alert">{error}</p>}
      {!usage?.initialized ? <p>尚未读取到已初始化的用量台账。正常开启新版助手后会登记原用量；读取页面不会新建空台账。</p> : <>
        <div className="assistant-usage"><div><span>今日已确认用量<strong>{number(usage.daily?.actual_tokens)} tokens</strong></span></div><div><span>待核对的估算预留<strong>{number(usage.daily?.held_estimated_tokens)} tokens</strong></span></div><div><span>待核对请求<strong>{number(usage.daily?.unsettled_requests)} 次</strong></span></div></div>
        <p>tokens（模型处理文字的计量单位）。今日按北京时间 {usage.day_key} 的请求准入日统计，包含本助手原失败调用；不包含网页聊天、声音识别和向量检索服务。人民币费用未取得完整账单，暂不估填。</p>
        {Boolean(usage.daily?.unknown_unbounded_requests) && <p role="status">有 {usage.daily?.unknown_unbounded_requests} 次历史请求缺少真实用量，不能当零；设置每日额度后会先停止新的模型准入，等待核对。</p>}
        <form onSubmit={event => void save(event)}>
          <div className="assistant-version-grid"><label>每个任务的文字用量额度<input type="text" inputMode="numeric" aria-label="每任务tokens额度" value={taskLimit} disabled={!admin || busy} onChange={event => { setTaskLimit(event.target.value); setEditing(true); }} placeholder="留空：未设置tokens额度" /></label><label>本机助手每日文字用量额度<input type="text" inputMode="numeric" aria-label="每日tokens额度" value={dayLimit} disabled={!admin || busy} onChange={event => { setDayLimit(event.target.value); setEditing(true); }} placeholder="留空：未设置tokens额度" /></label></div>
          <p>0表示不准入新的模型请求。留空保留既有模型轮数和工具次数限制。每任务额度跨原任务重试累计；每日额度由本机助手共享。</p>
          <p>请求前按完整文字和输出预算作保守估算预留，完成后换为服务实际返回用量。估算可能偏高或被实际用量超出，额度是准入门槛，不是服务商账单的绝对上限；超时或用量缺失不会自动释放预留。</p>
          <button className="ui-button" type="submit" disabled={!admin || busy || !editing || revision == null}>{busy ? '正在保存…' : '保存用量额度'}</button>
          {editing && <button className="text-button" type="button" disabled={busy} onClick={() => { setEditing(false); setRefresh(value => value + 1); }}>重新读取当前额度</button>}
          {!admin && <p>更改额度前，请先完成上方“本机管理”确认。</p>}
        </form>
      </>}
      {notice && <p role="status">{notice}</p>}
    </>}
  </section>;
}
