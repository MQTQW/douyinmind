import type { CollectionLoginStatus } from './api';

export interface CollectionConnection {
  phase: 'idle' | 'logging_in' | 'scraping' | 'ready' | 'failed';
  ready: boolean;
  busy: boolean;
  message: string;
  shortLabel: string;
}

export const INITIAL_COLLECTION_STATUS: CollectionLoginStatus = { status: 'idle', message: '' };

/** A detected cookie does not complete the collection snapshot. */
export function collectionConnection(status: CollectionLoginStatus): CollectionConnection {
  const message = typeof status.message === 'string' ? status.message : '';
  const hasSnapshotState = status.phase !== undefined || status.snapshot_ready !== undefined;
  let phase: CollectionConnection['phase'] = 'idle';

  if (hasSnapshotState) {
    if (status.status === 'logged_in' && status.phase === 'ready' && status.snapshot_ready === true) phase = 'ready';
    else if (status.phase === 'failed' || status.status === 'failed') phase = 'failed';
    else if (status.phase === 'scraping') phase = 'scraping';
    else if (status.phase === 'logging_in' || status.status === 'pending' || status.busy === true) phase = 'logging_in';
  } else if (status.status === 'failed' || /抓取失败|读取失败|未成功抓取|未生成可同步快照|抓取尚未完整/.test(message)) {
    phase = 'failed';
  } else if (/正在(?:同步|读取|抓取)收藏/.test(message)) {
    phase = 'scraping';
  } else if (status.status === 'logged_in') {
    phase = 'ready';
  } else if (status.status === 'pending') {
    phase = 'logging_in';
  }

  const descriptions = {
    idle: { message: '收藏未连接 · 本地资料可用', shortLabel: '收藏未连接' },
    logging_in: { message: '请在打开的浏览器中登录抖音', shortLabel: '正在连接收藏' },
    scraping: { message: '已登录，正在读取收藏夹…', shortLabel: '正在读取收藏' },
    ready: { message: '收藏读取完成，可同步收藏夹', shortLabel: '收藏已连接' },
    failed: { message: '收藏读取未完成，请重试读取收藏', shortLabel: '收藏读取失败' },
  };
  return {
    phase, ready: phase === 'ready',
    busy: status.busy === true || phase === 'logging_in' || phase === 'scraping',
    message: message || descriptions[phase].message,
    shortLabel: descriptions[phase].shortLabel,
  };
}
