import { BookOpen, ScanLine } from 'lucide-react';

interface Props { onStartLogin: () => void; onTryChat: () => void; busy: boolean; }

export default function LandingPage({ onStartLogin, onTryChat, busy }: Props) {
  return (
    <div className="min-h-screen flex flex-col bg-[var(--color-bg)]">
      {/* Top Bar */}
      <header className="flex items-center justify-between px-8 py-4 bg-white/50 backdrop-blur">
        <div className="flex items-center gap-3">
          <div className="brand-mark"><BookOpen size={20} /></div>
          <span className="font-display text-lg text-[var(--color-ink)]">DouyinMind</span>
        </div>
      </header>

      {/* Hero */}
      <main className="flex-1 flex flex-col items-center justify-center text-center px-6 gap-10">
        <span className="px-5 py-2 rounded-full bg-accent-light text-accent text-xs tracking-[0.15em] uppercase">
          让你的抖音收藏夹不再吃灰
        </span>

        <h1 className="font-display text-5xl leading-tight text-[var(--color-ink)] max-w-lg">
          把"收藏"变成<br />真正可用的知识
        </h1>

        <p className="text-[var(--color-ink-soft)] text-base max-w-md leading-relaxed">
          收藏了很多好内容却从不回顾？这里把碎片化视频接入 AI：
          自动转写、语义检索、对话式回顾，让收藏真正为你所用。
        </p>

        <button
          onClick={onStartLogin}
          disabled={busy}
          className="inline-flex items-center gap-3 px-8 py-3.5 rounded-xl bg-accent text-white font-medium text-base hover:bg-accent-hover transition-colors disabled:opacity-60 disabled:cursor-wait"
        >
          <ScanLine size={20} />{busy ? '启动中...' : '扫码登录 · 开始构建'}
        </button>

        <div className="-mt-5">
          <button onClick={onTryChat} className="text-sm text-accent underline underline-offset-4">先体验 AI 对话</button>
          <p className="mt-2 text-xs text-[var(--color-ink-muted)]">无需登录抖音；根据收藏视频回答需要先同步并入库</p>
        </div>

        {/* Pipeline */}
        <div className="flex flex-wrap justify-center gap-5 md:gap-10 mt-6">
          {[['1', '同步', '接入收藏夹'], ['2', '提炼', 'AI 转写整理'], ['3', '检索', '语义搜索'], ['4', '对话', '问答回顾']].map(([n, label, desc]) => (
            <div key={n} className="flex flex-col items-center gap-2 w-20">
              <div className="w-9 h-9 rounded-full bg-accent-light flex items-center justify-center text-accent font-bold text-sm">{n}</div>
              <span className="text-sm font-semibold text-[var(--color-ink)]">{label}</span>
              <span className="text-xs text-[var(--color-ink-muted)]">{desc}</span>
            </div>
          ))}
        </div>
      </main>

      {/* Footer */}
      <footer className="py-3 text-center text-xs text-[var(--color-ink-muted)] bg-white/30">
        DouyinMind © 2026 · 基于抖音收藏夹构建 · AI 驱动
      </footer>
    </div>
  );
}
