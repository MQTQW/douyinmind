"""Local, escaped, read-only preview; no import or reply controls."""
from datetime import datetime, timezone
import html
import json
import uuid
from pathlib import Path


LABELS = {"pending": "待核对原评论", "retryable": "等待重试", "received": "已接收，仅预览", "needs_review": "需要人工确认"}


def write_dashboard(root: Path, inbox, *, state: str, message: str, mode: str = "live", health: dict | None = None) -> None:
    root.mkdir(parents=True, exist_ok=True)
    events = [x.to_dict() if hasattr(x, "to_dict") else x for x in inbox.list_events()]
    candidates = [x.to_dict() if hasattr(x, "to_dict") else x for x in inbox.list_candidates()]
    data = {"updated_at_utc": datetime.now(timezone.utc).isoformat(), "state": state,
            "message": message, "mode": mode, "import_enabled": False,
            "events": events, "candidates": candidates, "health": health or {}}
    def esc(v):
        return html.escape(str(v if v is not None else ""), quote=True)
    cards = []
    for event in events:
        reason = event.get("reason") or event.get("review_reason") or ""
        origin = event.get("metadata", {}).get("origin", "unknown")
        origin_label = {"live_normal_page": "正常网页自动接收", "sample_replay": "保存样本回放", "saved_live_ui_replay": "保存样本回放"}.get(origin, "来源未核对")
        cards.append('<article><div class="tag">' + esc(LABELS.get(event.get('status'), event.get('status'))) + '</div><h2>'
            + esc(event.get('target_library_name') or '目标库待确认') + '</h2><p class="command">'
            + esc(event.get('command_text')) + '</p><dl><dt>视频</dt><dd>'
            + esc(event.get('video_title') or event.get('video_id')) + '</dd><dt>处理说明</dt><dd>'
            + esc(reason or '内容已保存为收件预览，未安排导入') + '</dd><dt>来源</dt><dd>'
            + esc(origin_label) + '</dd></dl></article>')
    pending = sum(x.get('status') in ('pending', 'retryable') for x in candidates)
    incomplete = sum(bool(x.get('details', {}).get('incomplete_candidate_quarantine')) for x in candidates)
    incomplete_text = ('<p class="muted">另有 ' + str(incomplete) + ' 条加载不完整的旧记录已留档，停止自动重试；不计作新视频。</p>') if incomplete else ''
    health_text = ""
    if health:
        health_text = '<p class="muted">本次已检查 ' + esc(health.get('cycle_count', 0)) + ' 轮：正常 ' + esc(health.get('success_count', 0)) + '，超时 ' + esc(health.get('timeout_count', 0)) + '，其他错误 ' + esc(health.get('error_count', 0)) + '，暂停或主页等待 ' + esc(health.get('paused_count', 0)) + '。</p>'
    mode_note = ('本次单任务自动回复已显式启用 · 只读查绑定库 · 核验或结果不明时锁定暂停 · 不导入视频'
                 if mode == 'agent_auto_reply' else
                 '已启用 DeepSeek 工具 · 只读查绑定库 · 生成待发送回复；本模式不会自动发评或导入'
                 if mode == 'agent' else
                 '回放验证：处理保存的样本；样本本身的真实性需另核对，不能证明持续在线接收。'
                 if mode != 'live' else '独立助手号 · 只读取最近通知 · 不识别、不入库、不发回复')
    body = f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="refresh" content="10"><title>DouyinMind · 接收测试台</title><style>
body{{margin:0;background:#f2f5ed;color:#293e30;font:16px/1.7 system-ui,"Microsoft YaHei",sans-serif}}main{{max-width:860px;margin:40px auto;padding:0 22px}}h1{{font-size:28px;margin-bottom:6px}}.sub,.muted{{color:#657568}}.status,article{{border:1px solid #d9e3d2;background:#fff;border-radius:16px;padding:22px;margin:18px 0}}.tag{{display:inline-block;background:#e8f0df;padding:3px 10px;border-radius:18px;font-size:13px}}.command{{white-space:pre-wrap;overflow-wrap:anywhere}}h2{{font-size:19px}}dl{{display:grid;grid-template-columns:80px 1fr;gap:9px}}dt{{color:#657568}}dd{{margin:0;overflow-wrap:anywhere}}.note{{font-size:14px;border-left:3px solid #9db88a;padding-left:14px}}@media(max-width:500px){{main{{margin:20px auto}}dl{{grid-template-columns:1fr}}}}
</style><main><div class="tag">DouyinMind · 接收原型</div><h1>评论区 @ 接收测试台</h1><p class="sub">{esc(mode_note)}</p><section class="status"><b>{esc(state)}</b><p>{esc(message)}</p><span class="muted">已保存 {len(events)} 条预览 · {pending} 条等待核对</span>{incomplete_text}{health_text}</section>{''.join(cards) or '<article>暂未保存可核对的指令。登录助手 B 后，保持助手浏览器打开。</article>'}<p class="note">本地指纹用于减少重复展示，尚未取得平台稳定事件号。相同指令和封面可能无法区分；不能保证补齐关闭期间的全部提醒。账号权限绑定和真正入库留待后续验证。</p><p class="muted">停止测试：关闭独立助手浏览器窗口。此页每 10 秒更新，原项目无需重启。</p></main></html>'''
    for name, content in (("status.json", json.dumps(data, ensure_ascii=False, indent=2) + "\n"), ("接收测试台.html", body)):
        path = root / name
        temp = path.with_name(name + "." + uuid.uuid4().hex + ".tmp")
        try:
            with temp.open("x", encoding="utf-8") as stream:
                stream.write(content)
            temp.replace(path)
        finally:
            temp.unlink(missing_ok=True)
