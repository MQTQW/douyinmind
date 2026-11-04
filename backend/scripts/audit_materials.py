"""Validate by default; explicitly compare a read-only SQLite snapshot with
an existing safe index-metadata export. Never construct a Chroma client, repair
data, invoke a model or delete files. An export is not a live index observation.
"""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import stat
import sys


def _file(path):
    target = path.absolute()
    if target.resolve() != target or target.is_symlink():
        raise ValueError('readonly_input_invalid')
    info = target.lstat()
    if not stat.S_ISREG(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
        raise ValueError('readonly_input_invalid')
    return target


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-readonly', action='store_true')
    parser.add_argument('--database', type=Path, help='Explicit stable SQLite snapshot; no default application storage')
    parser.add_argument('--index-metadata', type=Path, help='Existing list_indexed_video_counts JSON export; no documents/embeddings')
    parser.add_argument('--limit', type=int, default=50)
    parser.add_argument('--offset', type=int, default=0)
    args = parser.parse_args(argv)
    if not 1 <= args.limit <= 200 or args.offset < 0:
        parser.error('limit must be 1..200 and offset must be non-negative')
    if not args.run_readonly:
        print(json.dumps({'mode': 'validate_only', 'executed': False, 'read_only': True,
                          'reason': 'explicit_run_readonly_required', 'repair_supported': False}))
        return 0
    if args.database is None or args.index_metadata is None:
        parser.error('--run-readonly requires explicit --database and --index-metadata paths')
    engine = None
    try:
        database, metadata = _file(args.database), _file(args.index_metadata)
        # Do not silently ignore WAL/journal contents or attempt recovery. The
        # caller supplies a stable snapshot; this tool never makes one itself.
        if any(Path(str(database) + suffix).exists() for suffix in ('-wal', '-journal', '-shm')):
            raise ValueError('sqlite_snapshot_required')
        if metadata.stat().st_size > 16 * 1024 * 1024:
            raise ValueError('index_metadata_export_too_large')
        exported = json.loads(metadata.read_text(encoding='utf-8'))
        backend = Path(__file__).resolve().parents[1]
        if str(backend) not in sys.path:
            sys.path.insert(0, str(backend))
        from sqlalchemy import create_engine
        from sqlalchemy.orm import Session
        from app.services.material_audit import audit_materials, validate_index_snapshot
        snapshot = validate_index_snapshot(exported)
        snapshot['source_kind'] = 'provided_index_metadata_export'
        class ProvidedIndex:
            def list_indexed_video_counts(self, *, verify_legacy_text=False):
                if verify_legacy_text:
                    raise ValueError('provided_export_has_no_local_documents')
                return deepcopy(snapshot)
        def connect():
            connection = sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)
            connection.execute('PRAGMA query_only=ON')
            return connection
        engine = create_engine('sqlite://', creator=connect)
        with Session(engine, autoflush=False) as db:
            result = audit_materials(db, chroma=ProvidedIndex(), limit=args.limit, offset=args.offset)
        result.update(mode='readonly_export_comparison', executed=True, comparison_only=True,
                      live_index_verified=False,
                      scope_note='对照既有索引导出，不代表当前实时索引状态；未执行任何修复。')
        print(json.dumps(result, ensure_ascii=True, allow_nan=False))
        return 0 if result['read_complete'] and not result['summary']['issue_count'] and not result['summary']['review_count'] else 2
    except Exception:
        print(json.dumps({'mode': 'readonly_export_comparison', 'executed': False, 'read_only': True,
                          'read_complete': False, 'summary': None, 'reason': 'readonly_audit_input_or_read_unavailable',
                          'repair_supported': False}))
        return 2
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == '__main__':
    raise SystemExit(main())
