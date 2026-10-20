"""Record polling observations for an already-running, uninstrumented import.

These are approximate observations, never replacements for program stage timers.
No transcript/model inspection, restart, download or model call is performed.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
import sys
import time
import urllib.request

BACKEND_DIR=Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:sys.path.insert(0,str(BACKEND_DIR))


def update_observations(state:dict, rows:list[dict], elapsed:float, stamp:str) -> None:
    terminal={'done','failed'}
    active={'downloading','transcribing','embedding'}
    for row in rows:
        video_id,status=row['video_id'],row['status']
        saved=state['videos'].get(video_id)
        if saved is None:
            saved={'video_id':video_id,'initial_status':status,'current_status':status,
                   'started_before_observation':status in active or status in terminal,
                   'total_observed_seconds':None,'events':[],'stage_observations':[]}
            state['videos'][video_id]=saved
        if saved['events'] and saved['current_status']==status:continue
        previous=saved['events'][-1] if saved['events'] else None
        if previous and previous['status'] in active:
            saved['stage_observations'].append({'status':previous['status'],
                'observed_seconds':round(max(0,elapsed-previous['elapsed_seconds']),3),
                'partial_start':len(saved['events'])==1 and saved['started_before_observation'],
                'meaning':'轮询观察估计，包含采样与请求延迟误差'})
        saved['events'].append({'status':status,'observed_at':stamp,'elapsed_seconds':elapsed})
        saved['current_status']=status
        if status in terminal and not saved['started_before_observation']:
            first=next((event for event in saved['events'] if event['status'] in active),None)
            if first:saved['total_observed_seconds']=round(max(0,elapsed-first['elapsed_seconds']),3)


def main() -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir',required=True,type=Path)
    parser.add_argument('--task-id',required=True)
    parser.add_argument('--poll-seconds',type=float,default=2)
    parser.add_argument('--max-seconds',type=float,default=3600)
    args=parser.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',args.task_id):parser.error('任务编号无效')
    if not 1<=args.poll_seconds<=30 or not 1<=args.max_seconds<=14400:parser.error('观察间隔或总时长无效')
    folder=args.run_dir.resolve();target=folder/'observed-timings.json'
    if target.exists():parser.error('已有观察记录，不能覆盖；请换测试目录')
    import os
    os.chdir(BACKEND_DIR)
    from app.core.config import settings
    from scripts.evaluate_answers import database_path,atomic_json
    ids=json.loads((folder/'selected-targets.json').read_text(encoding='utf-8'))['video_ids']
    if not ids or any(not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',item) for item in ids):parser.error('测试视频编号无效')
    state={'schema_version':1,'task_id':args.task_id,'status':'observing','videos':{},
           'poll_seconds':args.poll_seconds,'started_at':datetime.now(timezone.utc).isoformat(),
           'note':'本批任务在新计时功能加载前已开始；仅记录观察时刻，不伪装精确耗时，先前已完成或已在处理的起点未知。'}
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    started=time.perf_counter();last_state=None
    try:
        while time.perf_counter()-started<args.max_seconds:
            with closing(sqlite3.connect(database_path(settings.database_url).as_uri()+'?mode=ro',uri=True)) as db:
                rows=[{'video_id':row[0],'status':row[1]} for row in db.execute(
                    'SELECT platform_item_id,status FROM video_cache WHERE platform_item_id IN ('+','.join('?' for _ in ids)+')',ids)]
            elapsed=round(time.perf_counter()-started,3)
            stamp=datetime.now(timezone.utc).isoformat()
            update_observations(state,rows,elapsed,stamp)
            state['last_observed_at']=stamp
            with opener.open('http://127.0.0.1:8000/api/knowledge/sync/'+args.task_id,timeout=8) as response:progress=json.load(response)
            status=progress.get('status') if progress.get('success') else 'task_not_found'
            if status in {'done','failed','task_not_found'}:state['status']=status
            atomic_json(target,state)
            signature=tuple((row['video_id'],row['status']) for row in rows)
            if signature!=last_state:
                print(json.dumps({'observation_only':True,'elapsed_seconds':elapsed,'statuses':rows},ensure_ascii=True),flush=True)
                last_state=signature
            if state['status']!='observing':break
            time.sleep(args.poll_seconds)
        else:state['status']='observation_timeout'
    except Exception as error:
        state.update(status='observation_stopped',error_type=type(error).__name__)
    state['ended_at']=datetime.now(timezone.utc).isoformat();atomic_json(target,state)
    print(json.dumps({'observation_status':state['status'],'output':str(target)},ensure_ascii=True),flush=True)
    return 0


if __name__=='__main__':raise SystemExit(main())
