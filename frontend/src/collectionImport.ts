export interface CollectionImportSource {
  collection_id: string;
  title: string;
  video_ids: string[];
}

export function collectionLibraryName(source: CollectionImportSource | undefined): string {
  return Array.from(source?.title.trim() ?? '').slice(0, 128).join('');
}

export function isWholeCollectionSelected(
  source: CollectionImportSource | undefined,
  selectedIds: Iterable<string>,
): boolean {
  if (!source || !collectionLibraryName(source)) return false;
  const collectionIds = new Set(source.video_ids);
  if (collectionIds.size === 0) return false;
  const chosenIds = new Set(selectedIds);
  return chosenIds.size === collectionIds.size && [...collectionIds].every(id => chosenIds.has(id));
}
