"""Capture the real frontend with invented data, never a logged-in profile.

Run docs-preview.mjs first, then use the backend venv Python to run this file.
Every API request is fulfilled in the fresh browser context; other origins are
blocked. No request may reach a backend, model, account, import or comment sender.
"""
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from urllib.parse import urlparse

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[2]
REPORT = ROOT / 'backend/app/storage/docs-capture'
ASSETS = REPORT / 'inputs'
BASE = 'http://127.0.0.1:5197'


def fixtures():
    stamp = datetime.now(timezone.utc).isoformat()
    titles = ['把收藏整理成主题笔记', '笔记复盘的三个步骤', '资料命名与主题分类',
              '阅读笔记：从问题到行动', '给自己的灵感清单留一点空白']
    videos = [dict(platform_item_id=f'demo-video-{i+1:02d}', title=title,
        author='演示内容团队', author_name='演示内容团队', duration=380+i*47,
        duration_seconds=380+i*47, status='done' if i < 3 else 'pending',
        cover_url='', video_url=f'https://example.invalid/video/demo-{i+1:02d}',
        url=f'https://example.invalid/video/demo-{i+1:02d}', created_at=stamp) for i,title in enumerate(titles)]
    libraries = [dict(id=11,name='方法与实践',video_count=3,ready_count=3,created_at=stamp,updated_at=stamp),
        dict(id=12,name='阅读与表达',video_count=2,ready_count=2,created_at=stamp,updated_at=stamp),
        dict(id=13,name='生活灵感',video_count=0,ready_count=0,created_at=stamp,updated_at=stamp)]
    source = dict(source_id='资料1',platform_item_id='demo-video-01',chunk_id='demo-video-01:1',
        title=titles[0],url='https://example.invalid/video/demo-01',source_type='audio',
        time_range='0:20–1:10',text='先写出需要解决的问题，再按主题归类。复盘时回到原文，补充一个可执行的下一步。')
    messages = [dict(id=101,role='user',content='如何把收藏的视频整理成能用的笔记？',created_at=stamp),
        dict(id=102,role='assistant',content='可以先做这三步：\n\n1. **写下问题**：先确定这次想解决什么，避免只是堆积收藏。\n2. **按主题归类**：把相关视频放进少量主题库，保留完整文字稿。\n3. **复盘并行动**：回到引用的原文核对，再写一个自己可以尝试的下一步。\n\n这几条演示资料都强调，笔记应当帮助再次找到依据，而不是只保存一个结论。',sources=[source],created_at=stamp,
            latency_ms=1820,trace=None)]
    transcript = '这是一份为文档截图编写的合成文字稿。\n\n整理收藏时，可以先把目标写成一个具体的问题。比如，我们希望找到某种方法的步骤，就应当把问题放在笔记的开头。\n\n随后，使用少量主题给资料归类。一个视频可以与不同主题有关，但不必为每条收藏单独新建知识库。完整文字稿和时间信息应当保留下来，方便以后回到原视频。\n\n最后，在复盘中核对原文，补充自己的理解，并写下一个可执行的下一步。不要把自动生成的摘要直接当作原文。'
    content = dict(success=True,platform_item_id='demo-video-01',title=titles[0],status='done',
        transcript_text=transcript,visual_analysis_text='',analysis_mode='audio',analysis_model='演示声音模型',
        duration_seconds=380,transcript_segments=[dict(start_seconds=20,end_seconds=70,text=source['text']),
            dict(start_seconds=70,end_seconds=135,text='保留完整文字稿和时间，方便回到原视频核对。')],
        visual_segments=[],warnings=['合成演示内容，仅说明界面与操作位置。'],error_message='',
        processing_metrics=dict(status='succeeded',started_at=stamp,ended_at=stamp,total_processing_seconds=42.6,
            stages=dict(download=dict(seconds=4.2,status='succeeded'),recognition=dict(seconds=35.1,status='succeeded'),indexing=dict(seconds=3.3,status='succeeded'))))
    def task(key, kind, title, text, state, **extra):
        return dict(id=key,kind=kind,state=state,video_title=title,video_id='demo-video-01',comment_text=text,
            created_at=stamp,updated_at=stamp,knowledge_base_id=11,query_scope='library',scope_label='方法与实践',
            actions=dict(can_reconcile=state=='delivery_unknown',can_resume=False,can_retry_query=False),
            stages=dict(intake='accepted',processing='done',reply='b_view_visible' if state=='b_view_visible' else 'needs_review'),**extra)
    tasks = [task('demo-query','query',titles[0],'请根据方法与实践里的资料，概括做笔记的步骤。','b_view_visible',
        reply_state='b_view_visible',details=dict(library_name='方法与实践',reply_text='可以先写下问题，再按主题归类，最后回到原文复盘，并补充一个可执行的下一步。',sources=[source],
            steps=[dict(kind='decision',created_at=0,metadata=dict(kind='semantic_planning',name='理解指令',status='complete'))],timings=dict(execution_ms=1820,model_ms=1510,retrieval_ms=210))),
        task('demo-save','save',titles[1],'请把这条视频放进方法与实践。','b_view_visible',reply_state='b_view_visible',import_state='succeeded',
            details=dict(library_name='方法与实践',reply_text='这个视频已加入“方法与实践”知识库，文字稿和检索资料已就绪。',sources=[],steps=[])),
        task('demo-unknown','query',titles[2],'请解释资料中提到的分类方法。','delivery_unknown',reply_state='delivery_unknown',
            reason_code='publication_result_unconfirmed',details=dict(library_name='方法与实践',reply_text='演示回答：先确定问题，再按主题归类。',sources=[source],steps=[]))]
    overview = dict(available=True,control=dict(state='running',desired_state='running',process_alive=True,receiver_state='ready',worker_state='idle',
        reason_code=None,heartbeat_at=stamp,last_capture_at=stamp,capture_cycles=12,last_capture_unavailable_details=0,
        manual_verification=dict(active=False,available=False,state='idle'),native_session_status=dict(state='ready',retry_after_seconds=0)),
        binding=dict(binding_id='demo-binding',sender_display_name='演示提问账号 A',receiver_display_name='演示助手 B',
            receiver_public_number='演示账号 B',knowledge_base_id=11,knowledge_base_name='方法与实践'),
        counts=dict(b_view_visible=2,needs_review=0,delivery_unknown=1,queued=0,executing=0,publishing=0),
        configuration=dict(state='ready',reason_code=None),intake_issues=dict(unresolved_count=0,recent=[]),
        capabilities=dict(start=True,pause=True,stop=True,multi_user=False,public_visibility='b_view_only'),
        diagnostics=dict(notices=[],disk_source_version='demo-build',
            backend_build=dict(source_version='demo-build',matches_disk=True),
            helper_build=dict(source_version='demo-build',matches_disk=True),
            capture_age_seconds=3,heartbeat_age_seconds=1,unverified_notice_deferrals=0,
            intake_scope=dict(poll_seconds=45,visible_candidates_per_batch=3,all_notifications_confirmed=False),
            delivery=dict(b_view_visible=2,requires_review=1,recipient_visibility='not_automatically_verified')))
    return videos,libraries,source,messages,content,tasks,overview,stamp


def run():
    videos,libraries,source,messages,content,tasks,overview,stamp=fixtures()
    ASSETS.mkdir(parents=True,exist_ok=True)
    calls,blocked,errors,unhandled=[],[],[],[]
    def handle(route):
        req=route.request;url=urlparse(req.url);path=url.path
        if url.netloc!='127.0.0.1:5197':
            blocked.append(req.url);route.abort();return
        if not path.startswith('/api/'):
            route.continue_();return
        calls.append(dict(method=req.method,path=path))
        if req.method!='GET':
            unhandled.append(dict(method=req.method,path=path));route.fulfill(status=405,body='Synthetic capture: mutations disabled');return
        data=None
        if path=='/api/auth/douyin/login/status':data=dict(status='logged_in',message='合成演示 · 未连接抖音')
        elif path=='/api/knowledge-bases':data=dict(success=True,items=libraries,total=len(libraries),capabilities=dict(delete_knowledge_base=True))
        elif path=='/api/knowledge/stats':data=dict(success=True,video_cache=dict(done=3,pending=2,failed=0,downloading=0,transcribing=0,embedding=0),chromadb=dict(total_videos=3,total_chunks=12),capabilities=dict(guarded_video_delete=True))
        elif path=='/api/favorites/collections':data=dict(success=True,items=[dict(id=0,collection_id='all',title='全部收藏',video_count=5),dict(id=1,collection_id='demo-notes',title='整理与笔记',video_count=3),dict(id=2,collection_id='demo-reading',title='阅读灵感',video_count=2)],total=3)
        elif re.fullmatch(r'/api/favorites/collections/[^/]+/videos',path):
            items=videos if '/all/' in path else videos[:3] if '/demo-notes/' in path else videos[3:]
            data=dict(success=True,items=items,total=len(items))
        elif path=='/api/knowledge/videos' or re.fullmatch(r'/api/knowledge-bases/\d+/videos',path):data=dict(success=True,items=videos[:3],total=3)
        elif re.fullmatch(r'/api/knowledge/videos/[^/]+/content',path):data=content
        elif path=='/api/chat/sessions':data=dict(success=True,items=[dict(id=501,title='把收藏整理成可用的笔记',knowledge_base_id=11,collection_id='all',message_count=2,created_at=stamp,updated_at=stamp)],total=1)
        elif path=='/api/chat/sessions/501/messages':data=dict(success=True,title='把收藏整理成可用的笔记',items=messages)
        elif path=='/api/assistant/overview':data=overview
        elif path=='/api/assistant/tasks':data=dict(items=tasks,total=3,page=1,page_size=10)
        elif path.startswith('/api/assistant/tasks/'):
            data=next((item for item in tasks if item['id']==path.rsplit('/',1)[1]),None)
        elif path=='/api/assistant/registry':data=dict(configured=True,users=[],local_catalog=libraries,local_instance_id='demo-local',cross_device_status='local_protocol_only_not_remote_verified')
        elif path=='/api/assistant/usage':data=dict(available=False,reason_code='usage_not_configured')
        if data is None:
            unhandled.append(dict(method=req.method,path=path));route.fulfill(status=404,content_type='application/json',body='{"code":"synthetic_endpoint_not_defined"}');return
        route.fulfill(content_type='application/json; charset=utf-8',body=json.dumps(data,ensure_ascii=False))

    def annotate(page, marks):
        page.locator('.doc-marker,.doc-demo-label').evaluate_all('(nodes)=>nodes.forEach(n=>n.remove())')
        page.evaluate('''() => {const tag=document.createElement('div');tag.className='doc-demo-label';tag.textContent='合成演示 · 不含真实账号或资料';tag.style.cssText='position:fixed;right:18px;bottom:12px;padding:7px 12px;border-radius:6px;background:#fffdf7;color:#725122;border:1px solid #d8c7a5;font:12px system-ui;z-index:100000;pointer-events:none';document.body.append(tag)}''')
        for marker in marks:
            number,locator=marker[:2]
            placement=marker[2] if len(marker)>2 else 'left'
            box=locator.bounding_box()
            assert box, f'callout {number} target invisible'
            x=box['x']-32 if placement=='left' else box['x']-10
            y=box['y']+4 if placement=='left' else box['y']-(28 if placement=='above' else 12)
            page.evaluate('''({n,x,y})=>{const tag=document.createElement('div');tag.className='doc-marker';tag.textContent=n;tag.style.cssText=`position:fixed;left:${x}px;top:${y}px;width:25px;height:25px;border:2px solid white;border-radius:50%;display:grid;place-items:center;background:#a84c31;color:white;font:700 13px system-ui;box-shadow:0 1px 4px #0002;z-index:100001;pointer-events:none`;document.body.append(tag)}''',dict(n=str(number),x=max(5,x),y=max(5,y)))

    def capture(page,name,marks):
        annotate(page,marks);page.screenshot(path=str(ASSETS/name),animations='disabled')

    with sync_playwright() as p:
        browser=p.chromium.launch(channel='msedge',headless=True)
        context=browser.new_context(viewport=dict(width=1280,height=900),locale='zh-CN',device_scale_factor=1)
        context.route('**/*',handle);context.set_default_timeout(12000)
        page=context.new_page();page.on('pageerror',lambda error:errors.append(str(error)))
        page.goto(BASE,wait_until='networkidle')
        page.locator('.library-open').filter(has_text='方法与实践').click()
        # Opening existing, invented history exercises the actual scope/history UI.
        page.get_by_role('button',name='把收藏整理成可用的笔记',exact=False).first.click()
        expect(page.locator('.chat-room')).to_contain_text('先确定这次想解决什么')
        page.get_by_label('输入问题',exact=True).fill('能把第二步展开说说吗？')
        capture(page,'guide-chat.png',[(1,page.locator('.workspace-title')),(2,page.locator('.chat-room').get_by_text('把收藏整理成主题笔记',exact=False).first),(3,page.get_by_label('输入问题',exact=True))])
        page.get_by_role('button',name='从收藏夹导入',exact=True).click()
        search=page.get_by_label('搜索收藏视频标题',exact=True)
        search.fill('笔记')
        checks=page.locator('main input[type=checkbox]')
        checks.nth(1).check();checks.nth(3).check()
        capture(page,'guide-import.png',[(1,search,'above'),(2,checks.nth(1)),(3,page.get_by_role('button',name=re.compile('导入所选视频')),'corner')])
        page.get_by_role('button',name=re.compile('导入所选视频')).click()
        dialog=page.get_by_role('dialog');expect(dialog).to_be_visible()
        capture(page,'guide-import-confirm.png',[(1,dialog.get_by_text('导入到知识库',exact=True)),(2,dialog.get_by_role('button',name=re.compile('确认导入')),'corner')])
        dialog.get_by_role('button',name='取消',exact=True).click()
        page.get_by_role('button',name='返回知识库',exact=True).click()
        page.get_by_role('button',name='视频资料',exact=True).click()
        material_checks=page.locator('main input[type=checkbox]')
        material_checks.nth(1).check();material_checks.nth(2).check()
        capture(page,'guide-materials.png',[(1,page.locator('.workspace-title')),(2,material_checks.nth(1)),(3,page.get_by_role('button',name='批量移出',exact=True))])
        page.locator('.resource-title').first.click()
        dialog=page.get_by_role('dialog');expect(dialog).to_contain_text('这是一份为文档截图编写的合成文字稿')
        dialog.get_by_text('查看带时间的逐段文字',exact=True).click()
        capture(page,'guide-transcript.png',[(1,dialog.get_by_role('tab',name='完整文字稿',exact=True)),(2,dialog.get_by_role('link',name='导出文字稿与时间信息',exact=True)),(3,dialog.get_by_text('查看带时间的逐段文字',exact=True))])
        dialog.get_by_role('button',name='关闭识别结果',exact=True).click()
        page.locator('.assistant-sidebar-shortcuts .task-shortcut').filter(has_text='抖音助手').click()
        page.set_viewport_size(dict(width=1280,height=1080))
        expect(page.locator('.assistant-control-card')).to_be_visible()
        capture(page,'guide-assistant.png',[(1,page.locator('.assistant-control-card')),(2,page.get_by_role('button',name='任务队列',exact=True))])
        context.close();browser.close()
    assert not blocked, blocked
    assert not errors, errors
    assert not unhandled, unhandled
    report=dict(synthetic_only=True,fresh_browser_profile=True,api_proxy=False,all_api_intercepted=True,
        actual_backend_model_account_import_comment_calls=0,api_calls=calls,blocked=blocked,errors=errors,
        unhandled=unhandled,images=[p.name for p in ASSETS.glob('guide-*.png')])
    (REPORT/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'images':report['images'],'synthetic_api_gets':len(calls),'runtime_errors':len(errors),'real_effects':0},ensure_ascii=False))


if __name__=='__main__':run()
