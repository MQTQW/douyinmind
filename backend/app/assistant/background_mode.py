"""Trusted daily browser policy: headless, with no automatic desktop fallback.

No environment/HTTP payload, profile, process or human-verification operation
is read or created here. Interactive recovery requires a separate explicit
workflow; a challenge cannot silently turn daily mode into a desktop window.
"""
from __future__ import annotations

PRODUCT_BROWSER_MODE = 'background'
_RECEIVER_MESSAGES = {
    'starting': '后台助手已启动，正在准备抖音会话。',
    'ready': '后台抖音会话已就绪。',
    'verification_required': '后台抖音会话需要本人验证，等待人工处理。',
    'login_required': '后台抖音会话需要本人登录，等待人工处理。',
    'page_unavailable': '后台抖音页面暂不可用，任务记录已保留。',
}


def product_browser_headless(*, mode=PRODUCT_BROWSER_MODE):
    """Only a trusted backend background option is permitted; never launches."""
    if type(mode) is not str or mode != PRODUCT_BROWSER_MODE:
        raise ValueError('assistant_background_mode_required')
    return True


def background_receiver_message(receiver_state):
    """Fixed text; this does not solve a challenge or change control state."""
    if type(receiver_state) is not str or receiver_state not in _RECEIVER_MESSAGES:
        raise ValueError('invalid_receiver_state')
    return _RECEIVER_MESSAGES[receiver_state]


def background_finished_message(reason_code):
    """Retain the old close code; do not render an arbitrary reason string."""
    if reason_code in ('assistant_window_closed', 'assistant_session_closed'):
        return '后台抖音会话已关闭，任务记录已保留。'
    if reason_code == 'runtime_start_failed':
        return '后台助手未能启动，任务记录已保留。'
    return '后台助手已停止，任务记录已保留。'
