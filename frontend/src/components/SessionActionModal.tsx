import { useEffect, useRef, useState, FormEvent } from 'react';
import * as api from '../api';

interface Props {
  session: api.SessionItem;
  action: 'rename' | 'delete';
  onClose: () => void;
  onRenamed: (id: number, title: string) => void;
  onDeleted: (id: number) => void;
}

export default function SessionActionModal({ session, action, onClose, onRenamed, onDeleted }: Props) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const [title, setTitle] = useState(session.title);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const renaming = action === 'rename';

  useEffect(() => {
    const dialog = dialogRef.current;
    dialog?.showModal();
    if (renaming) { inputRef.current?.focus(); inputRef.current?.select(); }
    return () => { dialog?.close(); };
  }, [renaming]);

  const handleSubmit = async (event: FormEvent) => {
    event.preventDefault();
    if (saving || (renaming && (!title.trim() || title.trim().length > 128))) return;
    setSaving(true);
    setError('');
    try {
      if (renaming) {
        const result = await api.renameSession(session.id, title);
        if (!result.success) throw new Error('重命名失败');
        onRenamed(session.id, result.title);
      } else {
        const result = await api.deleteSession(session.id);
        if (!result.success) throw new Error('删除失败');
        onDeleted(session.id);
      }
      onClose();
    } catch (error) {
      setError(error instanceof Error && error.message.includes('404')
        ? '这段对话已不存在，请关闭窗口后重新加载历史列表。'
        : `${renaming ? '重命名' : '删除'}失败，请稍后重试。`);
      setSaving(false);
    }
  };

  return (
    <dialog
      ref={dialogRef}
      aria-labelledby="session-action-title"
      aria-describedby="session-action-description"
      onCancel={event => { event.preventDefault(); if (!saving) onClose(); }}
      className="fixed inset-0 m-auto w-[calc(100%-24px)] max-w-[420px] max-h-[90vh] overflow-y-auto rounded-2xl border border-[var(--color-border)] bg-[var(--color-panel)] p-5 sm:p-6 text-[var(--color-ink)] shadow-2xl backdrop:bg-black/30"
    >
      <form onSubmit={handleSubmit}>
        <h2 id="session-action-title" className="font-display text-xl">{renaming ? '重命名对话' : '删除这段对话？'}</h2>
        <p id="session-action-description" className="mt-2 text-sm leading-relaxed text-[var(--color-ink-soft)]">
          {renaming ? '给这段对话起个方便查找的名字。' : '删除后，这段对话及其中的全部消息将无法恢复。'}
        </p>
        {renaming ? (
          <label className="mt-4 block text-xs text-[var(--color-ink-muted)]">
            对话名称
            <input ref={inputRef} value={title} onChange={event => setTitle(event.target.value)} maxLength={128} disabled={saving}
              className="mt-2 h-11 w-full rounded-xl border border-[var(--color-border)] bg-white px-3 text-sm text-[var(--color-ink)] outline-none focus:border-accent" />
          </label>
        ) : (
          <p className="mt-4 rounded-xl border border-[var(--color-border)] bg-white/70 p-3 text-sm break-words">{session.title}</p>
        )}
        {error && <p role="alert" className="mt-3 text-xs text-red-600">{error}</p>}
        <div className="mt-5 flex justify-end gap-3">
          <button type="button" onClick={onClose} disabled={saving} autoFocus={!renaming}
            className="rounded-full border border-[var(--color-border)] px-4 py-2 text-sm disabled:opacity-40">取消</button>
          <button type="submit" disabled={saving || (renaming && !title.trim())}
            className={`rounded-full px-4 py-2 text-sm font-semibold text-white disabled:opacity-40 ${renaming ? 'bg-accent hover:bg-accent-hover' : 'bg-red-600 hover:bg-red-700'}`}>
            {saving ? '正在保存…' : renaming ? '保存名称' : '确认删除'}
          </button>
        </div>
      </form>
    </dialog>
  );
}
