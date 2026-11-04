"""Loopback-only explicit paid note submission and pure saved-note reads."""
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool
from sqlalchemy.orm import Session

from app.assistant.routes import local_request
from app.db.session import get_db
from app.services.notes_service import NotesError, notes_service


def no_store(response: Response):
    response.headers['Cache-Control'] = 'no-store'


router = APIRouter(prefix='/notes', tags=['本地笔记'], dependencies=[Depends(local_request), Depends(no_store)])


class GenerateNotesRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: UUID
    confirm: StrictBool
    expected_source_fingerprint: str = Field(pattern=r'^[a-f0-9]{64}$')
    redo: StrictBool = False
    recovery_of: UUID | None = None


def failure(error):
    if isinstance(error, NotesError):
        return JSONResponse(status_code=error.status, content={'success': False, 'code': error.code,
            'message': error.message, 'needs_review': error.needs_review,
            **({'request_registered': error.request_registered} if error.request_registered is not None else {})},
            headers={'Cache-Control': 'no-store'})
    return JSONResponse(status_code=503, content={'success': False, 'code': 'NOTES_READ_UNAVAILABLE',
        'message': '笔记记录暂时无法读取，请保留原请求标识并先只读核查。'}, headers={'Cache-Control': 'no-store'})


@router.get('/capabilities')
def capabilities():
    try:
        return notes_service.capabilities()
    except Exception as error:
        return failure(error)


@router.get('/requests/{request_id}')
def request_result(request_id: UUID, db: Session = Depends(get_db)):
    try:
        return notes_service.lookup(db, str(request_id))
    except Exception as error:
        return failure(error)


@router.get('/videos/{platform_item_id}')
def video_notes(platform_item_id: str, limit: int = Query(10, ge=1, le=100),
                offset: int = Query(0, ge=0), db: Session = Depends(get_db)):
    try:
        return notes_service.history(db, platform_item_id, limit=limit, offset=offset)
    except Exception as error:
        return failure(error)


@router.post('/videos/{platform_item_id}')
def generate_notes(platform_item_id: str, body: GenerateNotesRequest, db: Session = Depends(get_db)):
    try:
        return notes_service.submit(db, platform_item_id, request_id=str(body.request_id), confirm=body.confirm,
            expected_source_fingerprint=body.expected_source_fingerprint, redo=body.redo,
            recovery_of=str(body.recovery_of) if body.recovery_of is not None else None)
    except Exception as error:
        if isinstance(error, NotesError):
            return failure(error)
        return failure(NotesError('NOTES_SUBMISSION_UNCONFIRMED', status=503, needs_review=True))


@router.get('/artifacts/{note_id}.md')
def download_notes(note_id: UUID, db: Session = Depends(get_db)):
    try:
        content = notes_service.artifact(db, str(note_id))
        return Response(content=content, media_type='text/markdown; charset=utf-8', headers={
            'Cache-Control': 'no-store', 'Content-Disposition': f'attachment; filename=notes-{note_id}.md',
            'X-Content-Type-Options': 'nosniff'})
    except Exception as error:
        return failure(error)
