import { useState, useEffect, useCallback } from 'react';
import * as api from '../api';
import { CircleAlert, CircleCheck, ScanLine } from 'lucide-react';

interface Props { onClose: () => void; onSuccess: () => void; }

export default function LoginModal({ onClose, onSuccess }: Props) {
  const [status, setStatus] = useState('pending');
  const [message, setMessage] = useState('请在打开的浏览器中扫码登录');

  useEffect(() => {
    const poll = setInterval(async () => {
      try {
        const s = await api.loginStatus();
        setMessage(s.message);
        if (s.status === 'logged_in') {
          setStatus('success');
          setTimeout(onSuccess, 800);
          clearInterval(poll);
        } else if (s.status === 'failed') {
          setStatus('failed');
          clearInterval(poll);
        }
      } catch { /* ignore */ }
    }, 1500);
    return () => clearInterval(poll);
  }, [onSuccess]);

  return (
    <div className="fixed inset-0 bg-black/30 flex items-center justify-center z-50" onClick={onClose}>
      <div
        className="bg-[var(--color-panel)] rounded-2xl p-8 w-[400px] flex flex-col items-center gap-5 shadow-2xl"
        onClick={e => e.stopPropagation()}
      >
        <h2 className="font-display text-xl text-[var(--color-ink)]">
          {status === 'success' ? '登录成功' : status === 'failed' ? '登录失败' : '登录抖音'}
        </h2>

        <div className="w-48 h-48 rounded-xl bg-black/3 flex items-center justify-center">
          <span className="text-accent">{status === 'pending' ? <ScanLine size={52} /> : status === 'success' ? <CircleCheck size={52} /> : <CircleAlert size={52} />}</span>
        </div>

        <p className="text-sm text-[var(--color-ink-soft)] text-center">{message}</p>

        <div className="w-full flex flex-col gap-2 text-xs text-[var(--color-ink-soft)]">
          <div className="flex gap-2"><span className="text-accent font-bold">1.</span>点击「扫码登录」后浏览器自动打开</div>
          <div className="flex gap-2"><span className="text-accent font-bold">2.</span>在浏览器中扫描抖音二维码</div>
          <div className="flex gap-2"><span className="text-accent font-bold">3.</span>登录成功后自动开始同步收藏夹</div>
        </div>

        <button onClick={onClose} className="text-sm text-[var(--color-ink-muted)] hover:text-[var(--color-ink)] transition-colors">
          取消
        </button>
      </div>
    </div>
  );
}
