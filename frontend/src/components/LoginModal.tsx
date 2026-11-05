import { useEffect, useId, useRef } from 'react';
import { CircleAlert, CircleCheck, LoaderCircle, ScanLine } from 'lucide-react';
import type { CollectionConnection } from '../collectionConnection';

interface Props {
  onClose: () => void;
  onRetry: () => void;
  connection: CollectionConnection;
  requestBusy?: boolean;
  error?: string;
}

export default function LoginModal({ onClose, onRetry, connection, requestBusy = false, error = '' }: Props) {
  const dialog = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  const failed = Boolean(error) || connection.phase === 'failed';
  const reading = connection.phase === 'scraping';
  const title = failed ? '收藏读取未完成' : reading ? '正在读取收藏夹' : connection.ready ? '收藏读取完成' : '登录抖音';

  useEffect(() => {
    const element = dialog.current;
    element?.showModal();
    return () => element?.close();
  }, []);

  return <dialog ref={dialog} className="collection-login-dialog" aria-labelledby={titleId}
    onCancel={event => { event.preventDefault(); onClose(); }}
    onClick={event => {
      if (event.target !== event.currentTarget) return;
      const bounds = event.currentTarget.getBoundingClientRect();
      if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) onClose();
    }}>
    <div className="flex flex-col items-center gap-5">
      <h2 id={titleId} className="font-display text-xl text-[var(--color-ink)]">{title}</h2>
      <div className="w-48 h-48 rounded-xl bg-black/3 flex items-center justify-center" aria-hidden="true">
        <span className="text-accent">{failed ? <CircleAlert size={52} /> : reading ? <LoaderCircle size={52} className="animate-spin motion-reduce:animate-none" /> : connection.ready ? <CircleCheck size={52} /> : <ScanLine size={52} />}</span>
      </div>
      <p role={failed ? 'alert' : 'status'} className="text-sm text-[var(--color-ink-soft)] text-center">{error || connection.message}</p>
      <p className="text-xs text-[var(--color-ink-soft)] text-center">在浏览器中完成本人登录，等待读取完成，再到收藏页点击“同步收藏夹”更新列表。</p>
      <div className="flex flex-wrap justify-center gap-4">
        {failed && <button className="ui-button ui-button-primary" onClick={onRetry} disabled={requestBusy || connection.busy}>重试读取收藏</button>}
        <button className="text-button" onClick={onClose}>关闭</button>
      </div>
    </div>
  </dialog>;
}
