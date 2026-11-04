"""Loopback-only observations; never initialize storage, repair or delete."""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.assistant.routes import local_request
from app.db.session import session_factory
from app.services import chroma_service
from app.services.material_audit import audit_materials, validate_index_snapshot


router = APIRouter(prefix='/maintenance', tags=['本机只读维护'], dependencies=[Depends(local_request)])
_HEADERS = {'Cache-Control': 'no-store'}


def readonly_db():
    """A fresh, non-autoflushing read session with no success-path commit."""
    db = session_factory(autoflush=False)
    try:
        yield db
    finally:
        db.close()


def _unknown(code, *, changed=None, result=None):
    payload = {'success': False, 'code': code,
        'message': ('核查期间资料或索引发生变化，未形成一致性结论；请稍后重新核查。' if changed else
                    '本机核查暂时无法完整读取，未知内容未记为零；没有执行修复或删除。'),
        'read_only': True, 'repair_supported': False, 'read_complete': False,
        'content_verified': None, 'complete': False, 'snapshot_changed': changed,
        'summary': None, 'issues': [], 'total_issues': None, 'next_offset': None}
    if result is not None:
        payload.update(result)
        payload['success'] = False
    return JSONResponse(status_code=503, content=payload, headers=_HEADERS)


@router.get('/material-audit')
def get_material_audit(
    verify_legacy_text: bool = False,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(readonly_db),
):
    # The maintenance view may observe an already-ready index. Merely opening
    # this endpoint must never create PersistentClient/collection/default EF.
    index = chroma_service._chroma_service
    if index is None:
        return _unknown('MATERIAL_INDEX_NOT_INITIALIZED')
    try:
        result = audit_materials(db, chroma=index, limit=limit, offset=offset,
                                verify_legacy_text=verify_legacy_text)
    except Exception:
        return _unknown('MATERIAL_AUDIT_UNAVAILABLE')
    if not result['read_complete']:
        changed = result.get('snapshot_changed')
        return _unknown('MATERIAL_AUDIT_SNAPSHOT_CHANGED' if changed else 'MATERIAL_AUDIT_UNAVAILABLE',
                        changed=changed, result=result)
    return JSONResponse({'success': True, **result}, headers=_HEADERS)


@router.get('/index-snapshot')
def get_index_snapshot():
    index = chroma_service._chroma_service
    if index is None:
        return _unknown('MATERIAL_INDEX_NOT_INITIALIZED')
    try:
        # Project through the safe census schema twice. Unknown SDK fields,
        # documents, embeddings and provider diagnostics never cross this API.
        first = validate_index_snapshot(index.list_indexed_video_counts(verify_legacy_text=False))
        second = validate_index_snapshot(index.list_indexed_video_counts(verify_legacy_text=False))
        if first != second:
            return _unknown('MATERIAL_INDEX_SNAPSHOT_CHANGED', changed=True)
    except Exception as error:
        changed = getattr(error, 'reason', None) == 'index_snapshot_changed'
        return _unknown('MATERIAL_INDEX_SNAPSHOT_CHANGED' if changed else 'MATERIAL_INDEX_SNAPSHOT_UNAVAILABLE',
                        changed=True if changed else None)
    return JSONResponse({'success': True, 'read_only': True, 'repair_supported': False, **first,
                         'captured_at': datetime.now(timezone.utc).isoformat()}, headers=_HEADERS)
