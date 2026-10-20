import { useEffect, useId, useRef, useState, type FormEvent } from 'react';
import * as api from '../api';

interface Props {
  knowledgeBase?: api.KnowledgeBaseItem;
  onClose: () => void;
  onSaved: (base: api.KnowledgeBaseItem) => void;
}

export default function KnowledgeBaseCreateModal({ knowledgeBase, onClose, onSaved }: Props) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const titleId = useId();
  const descriptionId = useId();
  const [name, setName] = useState(knowledgeBase?.name ?? '');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const renaming = Boolean(knowledgeBase);
  const validName = name.trim().length > 0 && name.trim().length <= 128;

  useEffect(() => {
    const dialog = dialogRef.current;
    dialog?.showModal();
    inputRef.current?.focus();
    if (knowledgeBase) inputRef.current?.select();
    return () => { dialog?.close(); };
  }, [knowledgeBase]);

  const handleSubmit = async (event: FormEvent) => {
    event.preventDefault();
    if (saving || !validName) return;
    setSaving(true);
    setError('');
    try {
      const result = knowledgeBase
        ? await api.renameKnowledgeBase(knowledgeBase.id, name.trim())
        : await api.createKnowledgeBase(name.trim());
      if (!result.success || !result.item) throw new Error('保存失败');
      onSaved(result.item);
      onClose();
    } catch (cause) {
      setError(cause instanceof Error && cause.message.includes('409')
        ? '已有同名知识库，请换一个名称。'
        : `${renaming ? '重命名' : '创建'}失败，请稍后重试。`);
      setSaving(false);
    }
  };

  return (
    <dialog ref={dialogRef} aria-labelledby={titleId} aria-describedby={descriptionId}
      onCancel={event => { event.preventDefault(); if (!saving) onClose(); }}
      className="fixed inset-0 m-auto w-[calc(100%-24px)] max-w-[420px] max-h-[90vh] overflow-y-auto rounded-2xl border border-[var(--color-border)] bg-[var(--color-panel)] p-5 sm:p-6 text-[var(--color-ink)] shadow-2xl backdrop:bg-black/30">
      <form onSubmit={handleSubmit}>
        <h2 id={titleId} className="font-display text-xl">{renaming ? '重命名知识库' : '创建知识库'}</h2>
        <p id={descriptionId} className="mt-2 text-sm leading-relaxed text-[var(--color-ink-soft)]">
          {renaming ? '修改名称后，库里的视频和文字稿会保留。' : '给视频资料分个主题，例如“编程学习”或“健身”。之后入库时可以选择这个知识库。'}
        </p>
        <label className="mt-4 block text-xs text-[var(--color-ink-muted)]">
          知识库名称
          <input ref={inputRef} value={name} onChange={event => setName(event.target.value)} maxLength={128}
            disabled={saving} placeholder="例如：编程学习" required
            className="mt-2 h-11 w-full rounded-xl border border-[var(--color-border)] bg-white px-3 text-sm text-[var(--color-ink)] outline-none focus:border-accent" />
        </label>
        {error && <p role="alert" className="mt-3 text-xs text-red-600">{error}</p>}
        <div className="mt-5 flex justify-end flex-wrap gap-3">
          <button type="button" onClick={onClose} disabled={saving}
            className="rounded-full border border-[var(--color-border)] px-4 py-2 text-sm disabled:opacity-40">取消</button>
          <button type="submit" disabled={saving || !validName}
            className="rounded-full bg-accent px-4 py-2 text-sm font-semibold text-white hover:bg-accent-hover disabled:opacity-40">
            {saving ? '正在保存…' : renaming ? '保存名称' : '创建知识库'}
          </button>
        </div>
      </form>
    </dialog>
  );
}
