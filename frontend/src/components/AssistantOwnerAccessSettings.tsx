import { useEffect, useState } from 'react';
import { LoaderCircle, ShieldCheck } from 'lucide-react';
import type { AssistantLocalOwnerAccess, AssistantLocalOwnerRequest } from '../api';

export interface OwnerAccessDraft { query_all_local: boolean; catalog_read_allowed: boolean; public_reply_allowed: boolean; grant_current_libraries: boolean; }

export function ownerAccessDraft(access?: AssistantLocalOwnerAccess): OwnerAccessDraft {
  const active = access?.configured === true && access.active === true;
  return { query_all_local: active && access.query_all_local === true,
    catalog_read_allowed: active && access.catalog_read_allowed === true,
    public_reply_allowed: active && access.public_reply_allowed === true, grant_current_libraries: false };
}

export function ownerAccessSupported(access?: AssistantLocalOwnerAccess): boolean {
  return typeof access?.configured === 'boolean' && typeof access?.active === 'boolean'
    && typeof access?.query_all_local === 'boolean' && typeof access?.catalog_read_allowed === 'boolean'
    && typeof access?.public_reply_allowed === 'boolean';
}

export function ownerAccessRequest(draft: OwnerAccessDraft): AssistantLocalOwnerRequest {
  if (draft.grant_current_libraries && !draft.catalog_read_allowed) throw new Error('请先允许读取本人本机目录，再登记当前库的保存权限。');
  if (draft.public_reply_allowed && !draft.query_all_local && !draft.catalog_read_allowed) throw new Error('请先选择全部资料或本机目录范围，再开启公开许可。');
  return { confirm: true, ...draft };
}

export default function AssistantOwnerAccessSettings({ access, local, verified, canManage, catalogCount, catalogKey, unavailable, demo = false, onSave }:
  { access?: AssistantLocalOwnerAccess; local: boolean; verified: boolean; canManage: boolean; catalogCount?: number; catalogKey?: string; unavailable?: boolean; demo?: boolean;
    onSave: (body: AssistantLocalOwnerRequest) => Promise<void> }) {
  const [draft, setDraft] = useState(() => ownerAccessDraft(access));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const supported = ownerAccessSupported(access);
  // Reading saved flags only updates this form. No API or grant happens here.
  useEffect(() => { setDraft(ownerAccessDraft(access)); }, [access?.configured, access?.version, access?.active,
    access?.query_all_local, access?.catalog_read_allowed, access?.public_reply_allowed]);
  useEffect(() => { if (!canManage) { setDraft(ownerAccessDraft(access)); setNotice(''); } }, [canManage]);
  useEffect(() => { setDraft(current => ({ ...current, grant_current_libraries: false })); }, [catalogKey, catalogCount, unavailable]);
  const countKnown = typeof catalogCount === 'number' && Number.isSafeInteger(catalogCount) && catalogCount >= 0;
  const locked = !supported || !local || !verified || !canManage || unavailable === true || !countKnown || saving;
  const change = (key: keyof OwnerAccessDraft, value: boolean) => { setDraft(current => ({ ...current, [key]: value,
    ...(key === 'catalog_read_allowed' && !value ? { grant_current_libraries: false } : {}) })); setError(''); setNotice(''); };
  const submit = async () => {
    if (locked) return;
    setError(''); setNotice('');
    let body: AssistantLocalOwnerRequest;
    try { body = ownerAccessRequest(draft); } catch (reason) { setError(reason instanceof Error ? reason.message : '请核对授权范围。'); return; }
    setSaving(true);
    try { await onSave(body); setNotice(demo ? '演示：没有更改真实账号、资料或授权。' : '本次设置已提交，请以重新读取的权限状态为准。'); }
    catch (reason) { setError(reason instanceof Error ? reason.message : '本次设置未能确认，原有授权保留，请重新读取。'); }
    finally { setDraft(current => ({ ...current, grant_current_libraries: false })); setSaving(false); }
  };
  return <section className="assistant-owner-settings" aria-label="本人本机全部资料权限">
    <div className="assistant-owner-heading"><ShieldCheck size={17} /><div><h5>本人本机全部资料</h5><small>{!supported ? '设置尚未加载' : access?.configured && access.active ? '已有独立登记' : '尚未开启 · 不继承其他账号权限'}</small></div></div>
    <p>仅用于这个账号和这台已确认的本机。新账号与另一台电脑的权限分别登记。</p>
    <form onSubmit={event => { event.preventDefault(); void submit(); }}>
      <label className="assistant-checkbox"><input type="checkbox" checked={draft.query_all_local} disabled={locked} onChange={event => change('query_all_local', event.target.checked)} /><span>允许省略库名查询“全部入库资料”<small>包含未归入知识库的已入库视频；写明库名时仍只查该库。</small></span></label>
      <label className="assistant-checkbox"><input type="checkbox" checked={draft.catalog_read_allowed} disabled={locked} onChange={event => change('catalog_read_allowed', event.target.checked)} /><span>允许选库时展示本人本机目录<small>显示知识库数量与名称；公开展示还需要同时允许下方公开回复。</small></span></label>
      <label className="assistant-checkbox"><input type="checkbox" checked={draft.public_reply_allowed} disabled={locked} onChange={event => change('public_reply_allowed', event.target.checked)} /><span>允许在原评论公开全资料答案及已允许的目录<small>查询资料和公开回复分别控制；仅限当前账号、设备与已选择的范围。</small></span></label>
      <div className="assistant-owner-once"><label className="assistant-checkbox"><input type="checkbox" checked={draft.grant_current_libraries} disabled={locked || !draft.catalog_read_allowed} onChange={event => change('grant_current_libraries', event.target.checked)} /><span>本次授予当前 {countKnown ? catalogCount : '未读取'} 个知识库查询和保存权限<small>各库的公开回复许可按本次上方选择设置；未来新建的库不会自动获得保存权限。</small></span></label><p>这是一项本次操作，提交后会清空勾选。已有各库授权仍可在下方分别管理；关闭全资料开关不会自动移除各库授权。</p></div>
      {!supported ? <p role="status">后台尚未加载此设置，更新并重启项目后再配置。</p> : !local ? <p>另一台电脑尚未接通，不能在此授予它本机全部资料权限。</p> : !verified ? <p>请先分别在本机确认账号和设备，再调整全部资料权限。</p> : !canManage ? <p>更改前请先完成上方有效期 15 分钟的本机管理确认。</p> : unavailable || !countKnown ? <p>目录或设置尚未可靠读取，暂不更改授权。</p> : null}
      {error && <p className="assistant-owner-error" role="alert">{error}</p>}{notice && <p role="status">{notice}</p>}
      <button type="submit" className="ui-button" disabled={locked}>{saving ? <LoaderCircle size={14} className="spin" /> : <ShieldCheck size={14} />}保存本人本机权限</button>
    </form>
  </section>;
}
