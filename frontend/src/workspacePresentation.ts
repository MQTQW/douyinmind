/** Keep server order and an older active conversation without changing the full list. */
export function visibleHistory<T extends { id: number }>(sessions: T[], activeId: number | null, expanded: boolean): T[] {
  if (expanded) return sessions;
  const recent = sessions.slice(0, 6);
  const active = sessions.find(session => session.id === activeId);
  return active && !recent.some(session => session.id === active.id) ? [...recent, active] : recent;
}
