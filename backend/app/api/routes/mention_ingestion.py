"""Loopback-only ingestion requests for the one locally configured binding."""
import ipaddress
import threading

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from app.services.mention_ingestion_service import MentionIngestionError, build_default_service


def require_loopback(request: Request):
    try:
        address = ipaddress.ip_address(request.client.host)
        local = address.is_loopback or bool(getattr(address, "ipv4_mapped", None) and address.ipv4_mapped.is_loopback)
    except (AttributeError, ValueError):
        local = False
    if not local:
        raise HTTPException(status_code=403, detail="loopback_required")


router = APIRouter(prefix="/mention-ingestion", tags=["助手入库"], dependencies=[Depends(require_loopback)])


_service = None
_service_lock = threading.Lock()


def get_service():
    global _service
    with _service_lock:
        if _service is None:
            _service = build_default_service()
        return _service


class ImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(min_length=1, max_length=128)
    binding_id: str = Field(min_length=1, max_length=128)
    instance_id: str = Field(min_length=1, max_length=128)
    video_id: str = Field(pattern=r"^\d{5,30}$", strict=True)
    knowledge_base_id: int = Field(gt=0, strict=True)
    title: str | None = Field(default=None, max_length=512)


@router.post("")
def submit(body: ImportRequest, service=Depends(get_service)):
    try:
        return service.submit(body.model_dump())
    except MentionIngestionError as error:
        raise HTTPException(status_code=error.status_code, detail=error.code) from None
    except Exception:
        raise HTTPException(status_code=503, detail="ingestion_status_unavailable") from None


@router.get("/{request_id}")
def status(request_id: str, service=Depends(get_service)):
    try:
        return service.get(request_id)
    except MentionIngestionError as error:
        raise HTTPException(status_code=error.status_code, detail=error.code) from None
    except Exception:
        raise HTTPException(status_code=503, detail="ingestion_status_unavailable") from None
