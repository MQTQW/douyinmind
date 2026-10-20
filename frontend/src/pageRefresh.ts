interface VisibilityDocument extends EventTarget {
  readonly visibilityState: string;
}

/** Refresh cached page data once after returning to a visible page. */
export function subscribePageReturn(
  onRefresh: () => void,
  page: EventTarget = window,
  visibilityDocument: VisibilityDocument = document,
): () => void {
  let timer: ReturnType<typeof setTimeout> | undefined;
  let disposed = false;

  const cancelPending = () => {
    if (timer !== undefined) clearTimeout(timer);
    timer = undefined;
  };
  const scheduleRefresh = () => {
    cancelPending();
    if (disposed || visibilityDocument.visibilityState !== 'visible') return;
    timer = setTimeout(() => {
      timer = undefined;
      if (!disposed && visibilityDocument.visibilityState === 'visible') onRefresh();
    }, 150);
  };

  page.addEventListener('focus', scheduleRefresh);
  visibilityDocument.addEventListener('visibilitychange', scheduleRefresh);
  return () => {
    disposed = true;
    cancelPending();
    page.removeEventListener('focus', scheduleRefresh);
    visibilityDocument.removeEventListener('visibilitychange', scheduleRefresh);
  };
}
