"""Fixed loopback accounting API; only confirmed local management changes caps."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.storage_guard import assert_no_orphan_sqlite_files
from .registry_routes import admin_request
from .routes import local_request
from .usage_budget import UsageBudget, UsageBudgetError, DEFAULT_PATH, MARKER_NAME

BUDGET_PATH = DEFAULT_PATH
router = APIRouter(prefix='/assistant/usage', tags=['助手用量与额度'], dependencies=[Depends(local_request)])


def budget_error(error):
    code = error.code if isinstance(error, UsageBudgetError) else 'usage_budget_unavailable'
    allowed = {'usage_budget_missing', 'usage_budget_marker_missing', 'invalid_usage_budget_marker',
               'unsafe_usage_budget_marker', 'foreign_usage_budget', 'usage_policy_changed'}
    if code not in allowed: code = 'usage_budget_unavailable'
    message = '额度已被其他操作修改，请重新读取后再保存。' if code == 'usage_policy_changed' else '用量台账暂不可用，原记录保留。请核对本机助手，不会自动重建或把未知费用按零计算。'
    return HTTPException(409 if code == 'usage_policy_changed' else 503, detail={'code': code, 'message': message})


def read_usage():
    marker = BUDGET_PATH.parent / MARKER_NAME
    if not BUDGET_PATH.exists() and not BUDGET_PATH.is_symlink() and not marker.exists() and not marker.is_symlink():
        if BUDGET_PATH.parent.is_dir(): assert_no_orphan_sqlite_files(BUDGET_PATH)
        return {'initialized': False, 'currency_cost': None,
                'scope': 'assistant_answer_model_provider_tokens', 'excluded_services': ['embedding', 'asr']}
    return {'initialized': True, **UsageBudget(BUDGET_PATH).summary()}


@router.get('')
def usage():
    try: return read_usage()
    except Exception as error: raise budget_error(error) from None


class LimitsBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    per_task_tokens: int | None = Field(strict=True, ge=0, le=1_000_000_000_000)
    daily_tokens: int | None = Field(strict=True, ge=0, le=1_000_000_000_000)
    expected_revision: int = Field(strict=True, ge=1)


@router.put('/limits', dependencies=[Depends(admin_request)])
def limits(body: LimitsBody):
    try:
        budget = UsageBudget(BUDGET_PATH)
        budget.set_limits(per_task_tokens=body.per_task_tokens, daily_tokens=body.daily_tokens,
                          expected_revision=body.expected_revision)
        return {'success': True, 'initialized': True, **budget.summary()}
    except Exception as error: raise budget_error(error) from None
