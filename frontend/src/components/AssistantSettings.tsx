import { useCallback, useEffect, useRef, useState } from 'react';
import { BookOpen, Check, ChevronDown, Laptop, LoaderCircle, Plus, RefreshCw, ShieldCheck, Smartphone, Users, X } from 'lucide-react';
import * as api from '../api';
import { assistantTime, assistantLibraryGrantActive, assistantSetupContext, nativeChannelStage } from '../assistantView';
import AssistantUsageSettings from './AssistantUsageSettings';
import AssistantOwnerAccessSettings from './AssistantOwnerAccessSettings';

const sampleRegistry: api.AssistantRegistry = {
  configured: true, local_instance_id: 'demo-local', cross_device_status: 'local_protocol_only_not_remote_verified',
  local_catalog: [{ id: 7, name: '学习与工作' }, { id: 8, name: '生活灵感' }],
  users: [{ user_id: 'demo-person', label: '我的账号（演示）', sender_profile_url: 'https://www.douyin.com/user/demo-person',
    receiver_profile_url: 'https://www.douyin.com/user/demo-assistant', active: true, verification_status: 'verified', default_device_id: 'demo-device',
    devices: [{ device_id: 'demo-device', label: '我的电脑（演示）', instance_id: 'demo-local', enabled: true, revoked: false, is_local: true,
      verification_status: 'verified', online: true, local_owner_access: { configured: false, active: false, query_all_local: false, catalog_read_allowed: false, public_reply_allowed: false }, libraries: [{ grant_id: 'demo-grant', knowledge_base_id: 7, name: '学习与工作', aliases: ['学习库'],
        is_default: true, can_query: true, can_import: true, public_reply_allowed: true }] }] }],
};

type Form = { kind: 'user' } | { kind: 'device'; user: api.AssistantRegistryUser } | { kind: 'local-device'; user: api.AssistantRegistryUser } | { kind: 'library'; user: api.AssistantRegistryUser; device: api.AssistantDevice; grant?: api.AssistantLibraryGrant };
const encoded = encodeURIComponent;

export default function AssistantSettings({ demo, binding, setup, nativeChannel, nativeProvision, onChanged }: { demo: boolean; binding?: api.AssistantBindingSummary | null; setup?: api.AssistantSetup | null; nativeChannel?: api.AssistantNativeChannel | null; nativeProvision?: api.AssistantNativeProvision | null; onChanged: () => void }) {
  const [registry, setRegistry] = useState<api.AssistantRegistry | null>(null);
  const [admin, setAdmin] = useState<api.AssistantAdminSession | null>(null);
  const [confirmLocal, setConfirmLocal] = useState(false);
  const [allowLegacyImport, setAllowLegacyImport] = useState(false);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const [registryReadError, setRegistryReadError] = useState('');
  const [notice, setNotice] = useState('');
  const [form, setForm] = useState<Form | null>(null);
  const [revision, setRevision] = useState(0);
  const [revoke, setRevoke] = useState<{ path: string; label: string } | null>(null);
  const [receiverForm, setReceiverForm] = useState(false);
  const [showRevoked, setShowRevoked] = useState(false);
  const [nativeBusy, setNativeBusy] = useState(false);
  const refresh = useCallback(() => setRevision(value => value + 1), []);
  const adminActive = Boolean(admin && Date.parse(admin.expires_at_utc) > Date.now());
  const setupCtx = assistantSetupContext(setup, registry?.users, registry?.local_instance_id);
  const scrollTo = (id: string) => { if (typeof document !== 'undefined') document.getElementById(id)?.scrollIntoView({ behavior: 'smooth', block: 'center' }); };

  useEffect(() => {
    setAdmin(null); setConfirmLocal(false); setAllowLegacyImport(false); setRegistry(null); setRegistryReadError(''); setForm(null); setNotice(''); setShowRevoked(false);
  }, [demo]);

  useEffect(() => {
    let active = true;
    let polling = false;
    const controller = new AbortController();
    setLoading(true); setError('');
    if (demo) { setRegistry(current => current ?? structuredClone(sampleRegistry)); setLoading(false); return; }
    const load = async () => {
      if (polling || document.visibilityState === 'hidden') return;
      polling = true;
      try {
        const value = await api.getAssistantRegistry(controller.signal);
        if (!Array.isArray(value.users) || !Array.isArray(value.local_catalog)) throw new Error('账号管理数据不完整，请重新加载。');
        if (active) { setRegistry(value); setError(''); setRegistryReadError(''); }
      } catch (reason) { if (active && !controller.signal.aborted) { const message = reason instanceof Error ? reason.message : '账号管理暂时无法连接。'; setError(message); setRegistryReadError(message); } }
      finally { polling = false; if (active) setLoading(false); }
    };
    void load();
    const timer = window.setInterval(() => void load(), 15000);
    const visible = () => { if (document.visibilityState === 'visible') void load(); };
    document.addEventListener('visibilitychange', visible);
    return () => { active = false; controller.abort(); clearInterval(timer); document.removeEventListener('visibilitychange', visible); };
  }, [demo, revision]);

  useEffect(() => {
    if (!admin) return;
    const delay = Math.max(0, Date.parse(admin.expires_at_utc) - Date.now());
    const timer = window.setTimeout(() => { setAdmin(null); setConfirmLocal(false); setAllowLegacyImport(false); setNotice('本机管理确认已过期，请重新确认后更改设置。'); }, delay);
    return () => clearTimeout(timer);
  }, [admin]);

  const confirmAdmin = async () => {
    if (!confirmLocal || busy) return;
    setBusy('admin'); setError('');
    try {
      const value = demo ? { admin_token: 'demo-memory-only', expires_at_utc: new Date(Date.now() + 900000).toISOString() } : await api.confirmAssistantAdmin();
      setAdmin(value); setAllowLegacyImport(false); setNotice(demo ? '演示：本机管理状态已开启，没有建立真实管理会话。' : '已开启本次本机管理，15 分钟内有效。');
    } catch (reason) { setError(reason instanceof Error ? reason.message : '未能确认本机管理身份。'); }
    finally { setBusy(''); }
  };

  const mutate = async (path: string, body: Record<string, unknown> = {}, method: 'POST' | 'PUT' | 'DELETE' = 'POST') => {
    if (!adminActive || !admin) throw new Error('请先确认本机管理身份。');
    if (error || registryReadError || loading || !registry) throw new Error('请先重新读取设置，核对原授权状态。');
    if (demo) { setNotice('演示：已记录本页设置操作，没有更改真实账号或授权。'); return; }
    try { await api.updateAssistantRegistry(path, admin.admin_token, body, method); }
    catch (reason) {
      if (reason instanceof api.AssistantRequestError && reason.status === 401) { setAdmin(null); setConfirmLocal(false); setAllowLegacyImport(false); }
      throw reason;
    }
    refresh(); onChanged();
  };

  const action = async (key: string, path: string, body: Record<string, unknown> = {}, method: 'POST' | 'PUT' | 'DELETE' = 'POST') => {
    if (busy) return;
    setBusy(key); setError('');
    try { await mutate(path, body, method); setRevoke(null); if (!demo) setNotice('设置已保存，后续任务会按当前授权重新核对。'); }
    catch (reason) { setError(reason instanceof Error ? reason.message : '设置未能保存。'); }
    finally { setBusy(''); }
  };

  const nativeIntake = async (mode: 'enable' | 'disable') => {
    if (nativeBusy) return;
    setNativeBusy(true); setError('');
    try {
      const result = mode === 'enable' ? await api.enableAssistantNativeIntake() : await api.disableAssistantNativeIntake();
      setNotice(result.message || (mode === 'enable' ? '正在启用接口收件，助手会短暂重启。' : '正在关闭接口收件并恢复网页收件。'));
      onChanged();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '接口收件操作未完成；原网页收件不受影响。');
    } finally { setNativeBusy(false); }
  };
  const nativeStage = nativeChannelStage(nativeChannel, nativeProvision);

  const saveForm = async (body: Record<string, unknown>) => {
    if (!form) return;
    setBusy('form');
    try {
      if (form.kind === 'user') await mutate('/users', body);
      else if (form.kind === 'device' || form.kind === 'local-device') await mutate(`/users/${encoded(form.user.user_id)}/devices`, body);
      else await mutate(`/users/${encoded(form.user.user_id)}/devices/${encoded(form.device.device_id)}/libraries/${body.knowledge_base_id}`, {
        aliases: body.aliases, can_query: body.can_query, can_import: body.can_import, public_reply_allowed: body.public_reply_allowed, is_default: body.is_default,
      }, 'PUT');
      if (demo && registry) {
        const next = structuredClone(registry);
        if (form.kind === 'user') next.users.push({ user_id: `demo-new-${next.users.length}`, label: String(body.label), sender_profile_url: String(body.sender_profile_url), receiver_profile_url: String(body.receiver_profile_url), active: true, verification_status: 'unverified', devices: [] });
        else if (form.kind === 'device' || form.kind === 'local-device') next.users.find(user => user.user_id === form.user.user_id)?.devices.push({ device_id: `demo-new-device-${Date.now()}`, instance_id: String(body.instance_id || next.local_instance_id), label: String(body.label), enabled: true, revoked: false, is_local: !body.instance_id || body.instance_id === next.local_instance_id, verification_status: 'unverified', online: false, libraries: [], local_owner_access: { configured: false, active: false, query_all_local: false, catalog_read_allowed: false, public_reply_allowed: false } });
        else {
          const device = next.users.find(user => user.user_id === form.user.user_id)?.devices.find(device => device.device_id === form.device.device_id);
          if (device) { const id = Number(body.knowledge_base_id); device.libraries = [...device.libraries.filter(grant => grant.knowledge_base_id !== id), { grant_id: `demo-grant-${id}`, knowledge_base_id: id, name: next.local_catalog.find(item => item.id === id)?.name || '演示知识库', aliases: body.aliases as string[], can_query: body.can_query === true, can_import: body.can_import === true, public_reply_allowed: body.public_reply_allowed === true, is_default: body.is_default === true }]; }
        }
        setRegistry(next);
      }
      setForm(null); if (!demo) setNotice('设置已保存。新登记账号和设备仍需要分别在本机确认。');
    } finally { setBusy(''); }
  };

  return <div className="assistant-registry">
    {!demo && setup && !setup.ready && <section className="assistant-setup-card" aria-label="首次使用设置"><header><div><h4>首次使用设置</h4><p>按顺序完成以下步骤后即可开启助手；助手不会自动启动。</p></div><span className="assistant-setup-progress">{setup.steps.filter(step => step.state === 'ready').length}/{setup.steps.length}</span></header><ol className="assistant-setup-steps">{setup.steps.map(step => {
        const actionNode = step.state === 'ready' ? null
          : step.key === 'receiver' ? (setup.receiver?.state === 'missing'
              ? <button className="ui-button ui-button-primary" disabled={Boolean(busy)} onClick={() => setReceiverForm(true)}><ShieldCheck size={15} />填写助手 B 的公开信息</button>
              : <small className="assistant-muted">请先核对原有接收器配置，不会自动覆盖。</small>)
          : step.key === 'accounts' ? (setupCtx.receiverReady
              ? <button className="text-button" disabled={!adminActive || Boolean(busy)} onClick={() => scrollTo('assistant-accounts')}>去登记账号</button>
              : <button className="text-button" disabled={Boolean(busy)} onClick={() => setReceiverForm(true)}>先填写助手 B 信息</button>)
          : step.key === 'device' ? (!setupCtx.receiverReady
              ? <small className="assistant-muted">填写助手 B 信息后继续。</small>
              : setupCtx.matchedUser ? (setupCtx.localDevice
                  ? (setupCtx.localDevice.verification_status !== 'verified'
                      ? <button className="text-button" disabled={!adminActive || Boolean(busy)} onClick={() => void action(`verify-device:${setupCtx.localDevice!.device_id}`, `/devices/${encoded(setupCtx.localDevice!.device_id)}/verify`, { confirm: true })}>在本机确认这台电脑</button>
                      : null)
                  : <button className="ui-button" disabled={!adminActive || Boolean(busy)} onClick={() => setForm({ kind: 'local-device', user: setupCtx.matchedUser! })}><Plus size={13} />登记这台电脑</button>)
                : <button className="text-button" disabled={!adminActive || Boolean(busy)} onClick={() => setForm({ kind: 'user' })}>先登记账号</button>)
          : step.key === 'scope' ? (setupCtx.verifiedDevice
              ? <button className="text-button" disabled={!adminActive || Boolean(busy)} onClick={() => scrollTo(`assistant-device-${setupCtx.verifiedDevice!.device_id}`)}>去设置资料范围</button>
              : <small className="assistant-muted">完成设备确认后即可设置。</small>)
          : null;
        return <li key={step.key} className={`is-${step.state}`}><span className="assistant-setup-marker">{step.state === 'ready' ? <Check size={14} /> : step.state === 'blocked' ? <X size={14} /> : <span className="assistant-setup-dot" />}</span><div><strong>{step.title}</strong><small>{step.message}</small>{actionNode && <div className="assistant-setup-step-action">{actionNode}</div>}</div></li>;
      })}</ol>{setup.receiver?.state === 'ready' ? <p className="assistant-muted">助手 B 已固定：公开抖音号 {setup.receiver.receiver_public_number} · {setup.receiver.receiver_profile_url}</p> : setup.receiver?.state !== 'missing' ? <p className="assistant-feedback attention" role="alert">{setup.receiver?.message || '接收器配置需要核对，不会自动覆盖原有记录。'}</p> : null}<p className="assistant-muted">步骤按钮会直接打开对应设置；完成后回到上方点「开启助手」，首次开启需要由本人用手机扫码登录助手 B。</p></section>}
    <div className="assistant-admin-card"><ShieldCheck size={22} /><div><h4>{adminActive ? '本次本机管理已确认' : '由本人管理账号与资料授权'}</h4><p>{adminActive ? `有效至 ${assistantTime(admin?.expires_at_utc)}，关闭页面后需要重新确认。` : '更改设置前，先确认你正在本人的电脑上管理这些账号。此确认不代表平台实名验证。'}</p>{!adminActive && <label className="assistant-checkbox"><input type="checkbox" checked={confirmLocal} onChange={event => setConfirmLocal(event.target.checked)} />我确认由本人管理这台电脑上的助手账号和资料授权</label>}</div>{!adminActive ? <button className="ui-button" disabled={!confirmLocal || Boolean(busy)} onClick={() => void confirmAdmin()}>{busy === 'admin' ? <LoaderCircle className="spin" size={15} /> : <Check size={15} />}确认本机管理</button> : <button className="text-button" onClick={() => { setAdmin(null); setConfirmLocal(false); setAllowLegacyImport(false); setNotice('本次本机管理已结束。再次更改设置需要重新确认。'); }}>结束管理</button>}</div>
    {error && <div className="assistant-feedback attention" role="alert"><p>{error}</p></div>}
    {registryReadError && registryReadError !== error && <div className="assistant-feedback attention" role="alert"><p>{registryReadError}</p></div>}
    {registry?.catalog_error && <div className="assistant-feedback attention" role="alert"><p>本机知识库清单暂时无法读取，未使用空清单覆盖已有授权。请重新加载后再调整资料范围。</p></div>}
    {notice && <p className="assistant-notice" role="status"><Check size={15} />{notice}</p>}
    <section className="assistant-admin-card" aria-label="接口收件设置">
      <ShieldCheck size={22} />
      <div>
        <h4>接口收件（原生通知接口，实验性）<span className={`assistant-stage tone-${nativeStage.tone}`}><i />{nativeStage.label}</span></h4>
        <p>通过抖音通知接口读取 @ 消息，定位更精确、收件更及时。启用或关闭时助手会短暂重启；当前 B 会话必须有效。这是非官方接口，可能受平台策略影响，可随时关闭并回退网页收件；关闭后已有任务记录保留。</p>
        {nativeChannel?.enabled === true && <p className="assistant-muted">当前已启用。若抖音侧登录失效，助手会暂停并提示本人重新登录，不会静默改回网页收件。</p>}
        {nativeProvision?.active && <p role="status"><LoaderCircle className="spin" size={15} /> {nativeProvision.message || '正在切换接口收件…'}</p>}
        {!nativeProvision?.active && nativeStage.tone === 'danger' && <p className="assistant-feedback attention" role="alert">{nativeProvision?.message || '上次切换未完成；原网页收件保持可用，请重试或查看记录。'}</p>}
        <div className="assistant-device-actions">
          {nativeChannel?.enabled !== true
            ? <button className="ui-button ui-button-primary" disabled={demo || nativeBusy || Boolean(nativeProvision?.active) || setup?.ready !== true} onClick={() => void nativeIntake('enable')}>
                {nativeBusy ? <LoaderCircle className="spin" size={15} /> : <ShieldCheck size={15} />}启用接口收件
              </button>
            : <button className="text-button" disabled={demo || nativeBusy || Boolean(nativeProvision?.active)} onClick={() => void nativeIntake('disable')}>
                {nativeBusy ? <LoaderCircle className="spin" size={15} /> : null}关闭并回退网页收件
              </button>}
          {demo && <small className="assistant-muted">演示模式不会访问真实平台会话。</small>}
          {setup?.ready !== true && <small className="assistant-muted">请先完成首次设置，再启用接口收件。</small>}
        </div>
      </div>
    </section>
    <AssistantUsageSettings demo={demo} admin={adminActive ? admin : null} />
    <div className="assistant-registry-heading" id="assistant-accounts"><div><h4>提问账号与助手账号</h4><p>每个提问账号拥有独立的设备、知识库范围和公开回复选择。</p></div><div><button className="icon-button" aria-label="刷新账号设置" onClick={refresh} disabled={loading}><RefreshCw size={15} /></button><button className="ui-button ui-button-primary" disabled={!adminActive || Boolean(busy) || Boolean(error) || Boolean(registryReadError) || loading} onClick={() => setForm({ kind: 'user' })}><Plus size={15} />添加账号</button></div></div>
    {loading && !registry ? <p className="empty-hint">正在读取账号设置…</p> : registry?.users.length ? <div className="assistant-users">{registry.users.map(user => { const visibleDevices = user.devices.filter(device => showRevoked || !device.revoked); return <section className={`assistant-user-card ${!user.active ? 'is-inactive' : ''}`} key={user.user_id}>
      <header><span className="assistant-user-avatar"><Users size={20} /></span><div><h4>{user.label || '提问账号'}</h4><small>{!user.active ? '已停用 · 保留历史记录' : user.verification_status === 'verified' ? '已在本机确认' : '待本人在本机确认'}</small></div><div className="assistant-user-actions">{user.active && user.verification_status !== 'verified' && <button className="ui-button" disabled={!adminActive || Boolean(busy)} onClick={() => void action(`verify:${user.user_id}`, `/users/${encoded(user.user_id)}/verify`, { confirm: true })}>在本机确认账号</button>}{user.active && <button className="text-button" disabled={!adminActive || Boolean(busy)} onClick={() => setRevoke({ path: `/users/${encoded(user.user_id)}/revoke`, label: `停用“${user.label || '此账号'}”` })}>停用</button>}</div></header>
      <div className="assistant-profile-pair"><div><Smartphone size={15} /><span>提问账号 A<strong>{user.sender_profile_url}</strong></span></div><div><ShieldCheck size={15} /><span>助手账号 B<strong>{user.receiver_profile_url}</strong></span></div></div>
      <div className="assistant-device-heading"><h5><Laptop size={15} />处理设备</h5><div>{user.devices.some(device => device.revoked) && <button className="text-button" onClick={() => setShowRevoked(value => !value)}>{showRevoked ? '隐藏已撤销设备' : `显示已撤销设备（${user.devices.filter(device => device.revoked).length}）`}</button>}<button className="text-button" disabled={!adminActive || !user.active || Boolean(busy)} onClick={() => setForm({ kind: 'device', user })}><Plus size={13} />登记另一台电脑</button></div></div>
      {visibleDevices.length ? visibleDevices.map(device => {
        const local = device.is_local === true || device.instance_id === registry.local_instance_id;
        const usable = user.active && device.enabled && !device.revoked;
        const panelAvailable = local && usable;
        return <div className="assistant-registry-device" key={device.device_id} id={`assistant-device-${device.device_id}`}><div className="assistant-device-top"><Laptop size={21} /><div><strong>{device.label || '处理电脑'}</strong><small>{local ? '本机' : '另一台电脑 · 连接尚待验证'} · {device.revoked ? '已撤销' : device.online === true ? '已报告在线' : device.online === false ? '当前离线' : '在线状态未知'}{usable && user.default_device_id === device.device_id ? ' · 默认设备' : ''}</small></div><div className="assistant-device-actions">{usable && local && device.verification_status !== 'verified' && <button className="text-button" disabled={!adminActive || Boolean(busy)} onClick={() => void action(`verify-device:${device.device_id}`, `/devices/${encoded(device.device_id)}/verify`, { confirm: true })}>在本机确认设备</button>}{usable && user.default_device_id !== device.device_id && <button className="text-button" disabled={!adminActive || Boolean(busy)} onClick={() => void action(`default-device:${device.device_id}`, `/users/${encoded(user.user_id)}/default-device`, { device_id: device.device_id }, 'PUT')}>设为默认</button>}{usable && <button className="text-button" disabled={!adminActive || Boolean(busy)} onClick={() => setRevoke({ path: `/devices/${encoded(device.device_id)}/revoke`, label: `撤销“${device.label || '此设备'}”` })}>撤销</button>}</div></div>
          {panelAvailable ? <>
            <AssistantOwnerAccessSettings key={`${user.user_id}:${device.device_id}:${adminActive ? admin?.admin_id || 'demo-admin' : 'locked'}`}
              access={device.local_owner_access} local={device.is_local === true && device.instance_id === registry.local_instance_id}
              verified={usable && user.verification_status === 'verified' && device.verification_status === 'verified'}
              canManage={adminActive} demo={demo} catalogCount={registry.catalog_error ? undefined : registry.local_catalog.length}
              catalogKey={JSON.stringify(registry.local_catalog.map(item => [item.id, item.name]))}
              unavailable={Boolean(registryReadError) || Boolean(error) || loading || Boolean(busy)}
              onSave={async body => { setBusy(`owner:${device.device_id}`); try {
                await mutate(`/users/${encoded(user.user_id)}/devices/${encoded(device.device_id)}/local-owner-access`, { ...body }, 'PUT');
              } finally { setBusy(''); } }} />
            <div className="assistant-grants">{device.libraries.map(grant => <AssistantLibraryGrantRow key={grant.grant_id ?? grant.knowledge_base_id} grant={grant}
              editDisabled={!adminActive || !usable || !local || Boolean(busy) || Boolean(registry.catalog_error)}
              revokeDisabled={!adminActive || !usable || Boolean(busy)}
              onEdit={() => setForm({ kind: 'library', user, device, grant })}
              onRevoke={() => setRevoke({ path: `/grants/${encoded(grant.grant_id!)}`, label: `撤销“${grant.name}”授权` })} />)}</div>
            <button className="assistant-add-grant" disabled={!adminActive || !usable || Boolean(busy) || Boolean(registry.catalog_error)} onClick={() => setForm({ kind: 'library', user, device })}><Plus size={14} />允许使用另一个知识库</button>
          </> : <p className="assistant-muted">{device.revoked ? '这台设备已撤销，历史记录保留。请在「处理设备」重新登记这台电脑，再做本机确认。' : '这是另一台电脑，不能在这里授予“本人本机全部资料”或本机知识库权限；请在本机登记设备。'}</p>}
          {device.last_seen_at && <p className="assistant-device-seen">最近报告 {assistantTime(device.last_seen_at)}</p>}
        </div>;
      }) : <p className="assistant-muted">{user.devices.length ? '已撤销的设备已折叠，可点上方按钮查看历史。' : '尚未登记处理设备。请点上方「登记这台电脑」；登记不会自动登录账号或建立跨电脑连接。'}</p>}
    </section>; })}</div> : registry && !error && !registryReadError && !loading ? <div className="assistant-empty assistant-registry-empty"><Users size={30} /><h4>让本人账号与自己的资料库建立联系</h4><p>先登记提问账号和助手账号，再选择电脑与允许使用的知识库。新账号的公开回复默认关闭。</p><label className="assistant-checkbox"><input type="checkbox" checked={allowLegacyImport} disabled={!adminActive || Boolean(busy)} onChange={event => setAllowLegacyImport(event.target.checked)} />允许向原知识库保存视频</label><button className="ui-button" disabled={!adminActive || Boolean(busy)} onClick={() => void action('migrate', '/migrate-legacy', { confirm: true, allow_import: allowLegacyImport })}>登记原有本机绑定（如果已有）</button><p>后台会核对原绑定是否存在。原绑定的公开回复选择沿用；保存视频由本次勾选决定，不会授权给新增用户。</p></div> : <div className="assistant-empty assistant-registry-empty"><Users size={30} /><h4>账号设置尚未读取</h4><p>连接后显示原有设置；当前不会视为首次使用。</p><button className="ui-button" onClick={refresh}>重新读取设置</button></div>}
    <p className="assistant-boundary-note"><ShieldCheck size={14} />资料留在各自电脑；设备离线时等待。跨电脑连接尚需单独验证，本机确认不代表抖音平台所有权证明。</p>
    {revoke && <div className="assistant-revoke-confirm" role="alert"><p><strong>{revoke.label}？</strong>历史记录会保留，后续任务将按撤销后的权限核对，已发生的操作不会被撤回。</p><div><button className="ui-button" onClick={() => setRevoke(null)} disabled={Boolean(busy)}>取消</button><button className="ui-button" onClick={() => void action('revoke', revoke.path, { confirm: true }, revoke.path.startsWith('/grants/') ? 'DELETE' : 'POST')} disabled={Boolean(busy)}>确认{revoke.label.startsWith('停用') ? '停用' : '撤销'}</button></div></div>}
    {form && registry && <RegistryForm form={form} catalog={registry.local_catalog} receiver={binding?.receiver_profile_url || ''} busy={Boolean(busy)} active={adminActive} demo={demo} onClose={() => setForm(null)} onSave={saveForm} />}
    {receiverForm && !demo && <ReceiverSetupDialog busy={Boolean(busy)} defaultProfile={registry?.users.find(user => user.active && user.receiver_profile_url)?.receiver_profile_url || binding?.receiver_profile_url || ''} onClose={() => setReceiverForm(false)} onSaved={async () => { refresh(); onChanged(); }} />}
  </div>;
}

function ReceiverSetupDialog({ defaultProfile, busy, onClose, onSaved }: { defaultProfile: string; busy: boolean; onClose: () => void; onSaved: () => Promise<void> }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [number, setNumber] = useState('');
  const [profile, setProfile] = useState(defaultProfile);
  const [error, setError] = useState('');
  const [saving, setSaving] = useState(false);
  useEffect(() => { const node = dialog.current; node?.showModal(); return () => { if (node?.open) node.close(); }; }, []);
  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (saving || busy) return;
    const digits = number.trim();
    if (!/^[0-9]{5,30}$/.test(digits)) { setError('请输入 5–30 位数字的助手 B 公开抖音号。'); return; }
    setSaving(true); setError('');
    try { await api.saveAssistantReceiver({ receiver_public_number: digits, receiver_profile_url: profile.trim() }); await onSaved(); onClose(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : '助手 B 的公开信息未能保存，请核对后重试。'); }
    finally { setSaving(false); }
  };
  return <dialog ref={dialog} className="assistant-form-dialog" aria-labelledby="assistant-receiver-title" onCancel={event => { event.preventDefault(); if (!saving) onClose(); }}><form onSubmit={event => void submit(event)}><header><div><p className="eyebrow">首次设置 · 仅保存公开信息</p><h3 id="assistant-receiver-title">填写助手 B 的公开信息</h3></div><button className="icon-button" type="button" aria-label="关闭助手 B 设置" disabled={saving} onClick={onClose}><X size={18} /></button></header>
    <p className="assistant-form-note">这里只保存公开的抖音号与主页，用来固定这台电脑接收 @ 的助手账号；不保存密码、Cookie 或登录凭证。配置写入后不会自动被覆盖。</p>
    <label>助手 B 的公开抖音号<input autoFocus value={number} inputMode="numeric" maxLength={30} placeholder="例如：123456789" onChange={event => setNumber(event.target.value)} /></label>
    <label>助手 B 的公开主页<input type="url" value={profile} maxLength={512} placeholder="https://www.douyin.com/user/…" onChange={event => setProfile(event.target.value)} /></label>
    {error && <p className="assistant-form-error" role="alert">{error}</p>}
    <footer><button className="ui-button" type="button" disabled={saving} onClick={onClose}>取消</button><button className="ui-button ui-button-primary" type="submit" disabled={saving || busy}>{saving ? <LoaderCircle size={15} className="spin" /> : <Check size={15} />}保存公开信息</button></footer>
  </form></dialog>;
}

export function AssistantLibraryGrantRow({ grant, editDisabled, revokeDisabled, onEdit, onRevoke }:
  { grant: api.AssistantLibraryGrant; editDisabled: boolean; revokeDisabled: boolean; onEdit: () => void; onRevoke: () => void }) {
  const active = assistantLibraryGrantActive(grant);
  return <div className={`assistant-grant${active ? '' : ' is-inactive'}`}><BookOpen size={16} /><div>
    <strong>{grant.name || `知识库 ${grant.knowledge_base_id}`} {active && grant.is_default && <em>默认</em>}{!active && <em>已撤销</em>}{grant.library_exists === false && <em>资料库不可用</em>}</strong>
    <small>{active ? <>{grant.aliases.length ? `也可以叫：${grant.aliases.join('、')} · ` : ''}{grant.can_query ? '允许提问' : '不允许提问'} · {grant.can_import ? '允许入库' : '不允许入库'} · {grant.public_reply_allowed ? '允许公开回复' : '不公开回复'}</> : '这项单库授权已撤销，历史任务保留；全部资料权限另行管理。'}</small>
  </div><button type="button" className="text-button" disabled={editDisabled} onClick={onEdit}>{active ? '调整' : '重新授权'}</button>
    {active && grant.grant_id && <button type="button" className="text-button" disabled={revokeDisabled} onClick={onRevoke}>移除授权</button>}
  </div>;
}

function RegistryForm({ form, catalog, receiver, busy, active, demo, onClose, onSave }: { form: Form; catalog: { id: number; name: string }[]; receiver: string; busy: boolean; active: boolean; demo: boolean; onClose: () => void; onSave: (body: Record<string, unknown>) => Promise<void> }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [label, setLabel] = useState(form.kind === 'local-device' ? '这台电脑' : '');
  const [sender, setSender] = useState('');
  const [assistant, setAssistant] = useState(receiver);
  const [instance, setInstance] = useState('');
  const grant = form.kind === 'library' ? form.grant : undefined;
  const [library, setLibrary] = useState(grant ? String(grant.knowledge_base_id) : '');
  const [aliases, setAliases] = useState(grant?.aliases.join('，') || '');
  const [canQuery, setCanQuery] = useState(grant?.can_query ?? true);
  const [canImport, setCanImport] = useState(grant?.can_import ?? false);
  const [publicReply, setPublicReply] = useState(grant?.public_reply_allowed ?? false);
  const [isDefault, setIsDefault] = useState(grant?.is_default ?? false);
  const [error, setError] = useState('');
  const title = form.kind === 'user' ? '登记提问账号与助手' : form.kind === 'local-device' ? '登记这台电脑' : form.kind === 'device' ? '登记另一台电脑' : grant ? '调整此知识库的授权' : '允许使用一个知识库';
  useEffect(() => { const node = dialog.current; node?.showModal(); return () => { if (node?.open) node.close(); }; }, []);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault(); if (busy || !active) return;
    setError('');
    try {
      let body: Record<string, unknown>;
      if (form.kind === 'user') {
        if (!label.trim()) throw new Error('请填写便于辨认的账号名称。');
        const profile = (value: string) => {
          try { const url = new URL(value.trim()); if (url.protocol !== 'https:' || !['www.douyin.com', 'douyin.com'].includes(url.hostname) || url.username || url.password || url.port || !/^\/user\/[^/?#]+\/?$/.test(url.pathname) || url.pathname.replace(/\/$/, '') === '/user/self') throw new Error(); return `https://www.douyin.com${url.pathname.replace(/\/$/, '')}`; }
          catch { throw new Error('请填写固定的抖音公开个人主页链接，不能使用“我”的动态主页。'); }
        };
        const a = profile(sender), b = profile(assistant);
        if (a === b) throw new Error('提问账号和助手账号应是两个不同的账号。');
        body = { label: label.trim(), sender_profile_url: a, receiver_profile_url: b };
      } else if (form.kind === 'device' || form.kind === 'local-device') {
        if (!label.trim()) throw new Error('请填写设备名称。');
        body = { label: label.trim(), ...(form.kind === 'device' && instance.trim() ? { instance_id: instance.trim() } : {}) };
      } else {
        const id = Number(library);
        if (!Number.isSafeInteger(id) || !catalog.some(item => item.id === id)) throw new Error('请选择当前电脑中的知识库。');
        body = { knowledge_base_id: id, aliases: [...new Set(aliases.split(/[，,、;；\n]/).map(value => value.trim()).filter(Boolean))],
          can_query: canQuery, can_import: canImport, public_reply_allowed: publicReply, is_default: isDefault };
      }
      await onSave(body);
    } catch (reason) { setError(reason instanceof Error ? reason.message : '设置未能保存。'); }
  };

  return <dialog ref={dialog} className="assistant-form-dialog" aria-labelledby="assistant-form-title" onCancel={event => { event.preventDefault(); if (!busy) onClose(); }}><form onSubmit={event => void submit(event)}><header><div><p className="eyebrow">{demo ? '演示设置 · 未更改真实账号' : '本人授权 · 仅此账号与设备'}</p><h3 id="assistant-form-title">{title}</h3></div><button className="icon-button" type="button" aria-label="关闭设置表单" disabled={busy} onClick={onClose}><X size={18} /></button></header>
    {form.kind === 'user' ? <><label>账号名称<input autoFocus value={label} onChange={event => setLabel(event.target.value)} maxLength={128} placeholder="例如：我的学习账号" /></label><label>提问账号 A 的公开主页<input value={sender} onChange={event => setSender(event.target.value)} type="url" placeholder="https://www.douyin.com/user/…" /></label><label>助手账号 B 的公开主页<input value={assistant} onChange={event => setAssistant(event.target.value)} type="url" placeholder="https://www.douyin.com/user/…" /></label><p className="assistant-form-note">新账号先登记为待确认，不会继承现有账号的知识库或公开回复许可。请在登记后分别确认本人账号与设备。</p></> : form.kind === 'local-device' ? <><label>设备名称<input autoFocus value={label} onChange={event => setLabel(event.target.value)} maxLength={128} placeholder="例如：这台电脑" /></label><p className="assistant-form-note">只登记这台电脑；不会自动登录账号或建立跨电脑连接。保存后请在设备卡片点「在本机确认设备」，再设置资料范围。</p></> : form.kind === 'device' ? <><label>设备名称<input autoFocus value={label} onChange={event => setLabel(event.target.value)} maxLength={128} placeholder="例如：我的笔记本" /></label><label>设备实例编号 <span>选填</span><input value={instance} onChange={event => setInstance(event.target.value)} maxLength={128} placeholder="留空使用本机编号" /></label><p className="assistant-form-note">登记另一台电脑只保存管理信息，不会自动建立连接。该电脑的资料目录仍需要单独核对。</p></> : <><label>当前电脑中的知识库<select autoFocus value={library} disabled={Boolean(grant)} onChange={event => setLibrary(event.target.value)}><option value="">选择一个知识库</option>{catalog.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label><label>这个库的别名<textarea value={aliases} onChange={event => setAliases(event.target.value)} rows={2} placeholder="例如：学习库，工作笔记（逗号或换行分开）" /></label><fieldset className="assistant-permissions"><legend>允许此账号在这台设备上</legend><label className="assistant-checkbox"><input type="checkbox" checked={canQuery} onChange={event => setCanQuery(event.target.checked)} />根据此知识库回答问题</label><label className="assistant-checkbox"><input type="checkbox" checked={canImport} onChange={event => setCanImport(event.target.checked)} />把当前视频导入此知识库</label><label className="assistant-checkbox assistant-public-permission"><input type="checkbox" checked={publicReply} onChange={event => setPublicReply(event.target.checked)} />允许把此库的回答公开回复到原评论区</label><p>只对本次账号、设备和知识库授权生效。新授权默认不公开回复。</p><label className="assistant-checkbox"><input type="checkbox" checked={isDefault} onChange={event => setIsDefault(event.target.checked)} />设为此设备的默认知识库</label></fieldset></>}
    {!active && <p className="assistant-form-error" role="alert">本机管理确认已过期，请关闭表单并重新确认。</p>}{error && <p className="assistant-form-error" role="alert">{error}</p>}<footer><button className="ui-button" type="button" disabled={busy} onClick={onClose}>取消</button><button className="ui-button ui-button-primary" type="submit" disabled={busy || !active}>{busy ? <LoaderCircle size={15} className="spin" /> : <Check size={15} />}保存设置</button></footer>
  </form></dialog>;
}
