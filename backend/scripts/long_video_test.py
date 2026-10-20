"""Prepare and inspect long-video tests without making model calls.

The user explicitly starts the inspection after importing. Listening annotations
and answer keys must be checked before compiling an executable retrieval exam.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import html
import json
from pathlib import Path
import re
import subprocess
import sys
import uuid
import wave

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


def write_json(path: Path, value: dict) -> None:
    if path.exists():
        raise ValueError(f"文件已存在，请保留旧记录并使用新名称：{path.name}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("记录必须是 JSON 对象")
    return value


def collect_videos(db) -> list[dict]:
    from sqlalchemy import select
    from app.models.entities import VideoCache, KnowledgeBase, KnowledgeBaseVideo
    names = dict(db.execute(select(KnowledgeBase.id, KnowledgeBase.name)).all())
    memberships: dict[str, list[dict]] = {}
    for base_id, video_id in db.execute(select(KnowledgeBaseVideo.knowledge_base_id, KnowledgeBaseVideo.platform_item_id)):
        memberships.setdefault(video_id, []).append({"knowledge_base_id": base_id, "label": names[base_id]})
    rows = []
    for row in db.scalars(select(VideoCache).order_by(VideoCache.id)):
        try:
            analysis = json.loads(row.analysis_json or "{}")
        except ValueError:
            analysis = {}
        rows.append({"video_id": row.platform_item_id, "title": row.title, "status": row.status,
                     "duration_seconds": row.analysis_duration_seconds,
                     "transcript_text": row.transcript_text, "analysis": analysis if isinstance(analysis, dict) else {},
                     "transcript_sha256": hashlib.sha256((row.transcript_text or "").encode()).hexdigest(),
                     "knowledge_bases": memberships.get(row.platform_item_id, [])})
    return rows


def initialize(run_dir: Path, videos: list[dict]) -> dict:
    baseline = {"schema_version": 1, "prepared_at": now(), "start_policy": "导入后由用户发消息再检查，不安排定时运行",
        "duration_slots_minutes": [7, 15, 30], "baseline_videos": [
            {k: row[k] for k in ("video_id", "title", "status", "duration_seconds", "transcript_sha256")}
            for row in videos],
        "workflow": ["逐条导入约7、15、30分钟视频，等待入库完成", "核对下载/转写/入库耗时及失败重试记录",
                     "核对音频分段请求覆盖和文字时间结构", "回听开头、中间、结尾、每分钟交界及可疑无文字时段",
                     "按回听核实的原话编写开头/中间/结尾/跨段问题及无答案对照", "固定考卷，先测检索，再完整生成回答并逐点核对"],
        "limits": ["结构检查通过不等于听写没有遗漏；静音或音乐不应误判为漏听",
                   "旧视频没有准确阶段计时，不用创建/修改时间倒算耗时",
                   "只分析声音，画面里的字与操作不在本轮转写范围",
                   "检索命中和最终回答准确性分开评，测试对话使用资料副本"]}
    write_json(run_dir / "baseline.json", baseline)
    (run_dir / "长视频测试流程.html").write_text(render_report(baseline, []), encoding="utf-8")
    return baseline


def question_sheet(videos: list[dict]) -> dict:
    categories = [("BEGIN", "开头事实", "从开头回听片段选一个能精确定位的事实"),
                  ("MIDDLE", "中间步骤", "从中间回听片段选择步骤或原因，保留所有必要项"),
                  ("END", "结尾结论", "从最后部分选结论或限制条件，确认末尾没有漏听"),
                  ("BOUNDARY", "跨分段交界", "选至少两个跨分钟交界的原话片段共同支持答案"),
                  ("CROSS", "跨多个章节", "问题需结合不同部分，至少两条相隔较远的原文依据"),
                  ("TERM", "名称或参数", "选择原声中能确认的名称、数字、条件，不能猜画面里的字"),
                  ("NOANSWER", "当前库无答案", "回看当前知识库全文，挑一个确实未给答案的问题"),
                  ("SCOPE", "不同知识库对照", "换到不含该依据的知识库，同一问题应承认缺少依据")]
    cases = []
    for video in videos:
        for suffix, category, instruction in categories:
            cases.append({"id": f"LV-{video['video_id']}-{suffix}", "video_id": video["video_id"],
                          "category": category, "instruction": instruction, "question": "", "expected_answer": "",
                          "scope": {"knowledge_base_id": None, "label": "待选择知识库"},
                          "suggested_scopes": video["knowledge_bases"], "answerable": suffix not in {"NOANSWER", "SCOPE"},
                          "expected_route": "vector", "evidence": [], "source_audio_verified": False,
                          "scope_absence_verified": False, "status": "pending_annotation"})
    return {"schema_version": 1, "note": "这是待标注模板，不是已验证考卷。先回听再填写原话、时间和预期答案。", "cases": cases}


def inspect_videos(run_dir: Path, videos: list[dict], selected_ids: list[str] | None = None) -> tuple[dict, Path]:
    from app.core.config import settings
    from app.services.ingestion_metrics import list_ingestion_runs
    from app.services.long_video_audit import audit_transcript
    baseline = read_json(run_dir / "baseline.json")
    old = {row["video_id"]: row for row in baseline["baseline_videos"]}
    target_path = run_dir / "selected-targets.json"
    targets = set(read_json(target_path).get("video_ids", [])) if target_path.is_file() else set()
    observed_path = run_dir / "observed-timings.json"
    observed = read_json(observed_path).get("videos", {}) if observed_path.is_file() else {}
    if selected_ids and set(selected_ids) - {row["video_id"] for row in videos}:
        raise ValueError("指定视频不存在")
    candidates = [row for row in videos if row["video_id"] in selected_ids] if selected_ids else [
        row for row in videos if row["video_id"] in targets or row["video_id"] not in old
        or row["status"] != old[row["video_id"]]["status"]
        or row["transcript_sha256"] != old[row["video_id"]]["transcript_sha256"]]
    inspected = []
    for row in candidates:
        item = {k: row[k] for k in ("video_id", "title", "status", "duration_seconds", "transcript_sha256", "knowledge_bases")}
        item["in_target_duration"] = 360 <= row["duration_seconds"] <= 1860
        item["audit"] = audit_transcript(row["duration_seconds"], row["analysis"], row["transcript_text"])
        item["audio_processing"] = row["analysis"].get("audio_processing")
        runs = list_ingestion_runs(row["video_id"])
        item["processing_runs"] = runs
        item["polling_observation"] = observed.get(row["video_id"])
        item["latest_processing"] = runs[-1] if runs else None
        item["known_attempt_seconds"] = sum(run["total_processing_seconds"] for run in runs
                                           if isinstance(run.get("total_processing_seconds"), (int, float))) if runs else None
        item["local_audio_available"] = (Path(settings.audio_cache_dir) / f"{row['video_id']}.wav").is_file()
        inspected.append(item)
    result = {"schema_version": 1, "inspected_at": now(), "baseline_prepared_at": baseline["prepared_at"],
              "selection": "explicit" if selected_ids else "new_changed_or_selected_since_baseline", "videos": inspected,
              "quality_status": "pending_audio_listening_and_gold_questions",
              "model_calls": 0, "chat_writes": 0}
    folder = run_dir / ("inspection-" + uuid.uuid4().hex[:10])
    write_json(folder / "inspection.json", result)
    write_json(folder / "question-worksheet.json", question_sheet(candidates))
    (folder / "长视频检查报告.html").write_text(render_report(baseline, inspected), encoding="utf-8")
    return result, folder


def compile_exam(db, worksheet: dict, output: Path) -> dict:
    from scripts.evaluate_retrieval import evaluate_dataset
    rows = worksheet.get("cases")
    if not isinstance(rows, list) or not rows:
        raise ValueError("待标注考卷没有题目")
    cases = []
    for row in rows:
        if row.get("status") != "ready":
            raise ValueError(f"{row.get('id')} 尚未标注完成")
        if row.get("answerable") and row.get("source_audio_verified") is not True:
            raise ValueError(f"{row.get('id')} 尚未对照原声确认")
        if row.get("answerable") is False and row.get("scope_absence_verified") is not True:
            raise ValueError(f"{row.get('id')} 尚未核实本库无答案")
        if type(row.get("scope", {}).get("knowledge_base_id")) is not int:
            raise ValueError(f"{row.get('id')} 必须明确选择知识库")
        if row.get("category") in {"跨分段交界", "跨多个章节"}:
            spans = {(item.get('video_id'), item.get('start_seconds'), item.get('end_seconds'))
                     for item in row.get('evidence', [])}
            if len(spans) < 2:
                raise ValueError(f"{row.get('id')} 至少需要两条不同时间的依据")
        cases.append({key: row.get(key) for key in ("id", "question", "scope", "answerable", "expected_route", "evidence", "expected_answer", "category")})
    dataset = {"schema_version": 1, "cases": cases}
    validation = evaluate_dataset(db, dataset, run=False)
    if validation["summary"]["status_counts"].get("invalid"):
        raise ValueError("题目原话、时间或知识库范围校验失败，不能执行评测")
    write_json(output, dataset)
    return validation["summary"]


def extract_windows(source: Path, windows: list[dict], folder: Path) -> list[dict]:
    """Copy real PCM samples for listening; no new speech recognition is run."""
    folder.mkdir(parents=True, exist_ok=True)
    clips = []
    with wave.open(str(source), "rb") as audio:
        params = audio.getparams()
        if params.comptype != "NONE":
            raise ValueError("抽样需要未压缩的 WAV 音频")
        duration = audio.getnframes() / params.framerate
        for index, window in enumerate(windows):
            start, end = window["start_seconds"], window["end_seconds"]
            if not (type(start) in (int, float) and type(end) in (int, float)
                    and 0 <= start < end <= duration + .01 and end - start <= 60):
                raise ValueError("回听窗口时间无效")
            first = min(params.nframes, int(start * params.framerate))
            last = min(params.nframes, int(end * params.framerate))
            audio.setpos(first)
            data = audio.readframes(last - first)
            if len(data) != (last - first) * params.nchannels * params.sampwidth:
                raise ValueError("原音频不完整，不能当作可靠回听样本")
            target = folder / f"clip-{index + 1:03d}.wav"
            if target.exists():
                raise ValueError("回听样本已存在，请使用新目录")
            with wave.open(str(target), "wb") as output:
                output.setparams(params._replace(nframes=0))
                output.writeframes(data)
            clips.append({**window, "file": target.name, "actual_seconds": (last - first) / params.framerate})
    write_json(folder / "listening-checklist.json", {"clips": clips, "review_status": "pending"})
    body = ''.join(f'<section><h3>{esc(c["kind"])} {stamp(c["start_seconds"])}–{stamp(c["end_seconds"])}</h3><audio controls preload="none" src="{esc(c["file"])}"></audio><p>待回听：记录漏句、重复、名词或数字错误；静音和音乐不算漏听。</p></section>' for c in clips)
    (folder / "回听样本.html").write_text(page("原声回听样本（尚未核验）", body), encoding="utf-8")
    return clips


def listening_source(video_id: str, inspection_dir: Path) -> Path:
    """Use a retained WAV or locally restore an earlier cached video soundtrack."""
    from app.core.config import settings
    from app.services.media_service import _resolve_ffmpeg_path
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', video_id):
        raise ValueError('视频编号无效')
    source = Path(settings.audio_cache_dir) / f'{video_id}.wav'
    if source.is_file():
        return source
    original = Path(settings.audio_cache_dir) / 'source_media' / f'{video_id}.mp4'
    if not original.is_file():
        raise ValueError('本地原声和原视频均不存在；需回到原视频核对，不自动下载或调用模型')
    restored = inspection_dir / 'restored-audio'
    restored.mkdir(parents=True, exist_ok=True)
    source = restored / f'{video_id}.wav'
    if source.is_file():
        return source
    temporary = source.with_suffix('.partial.wav')
    command = [_resolve_ffmpeg_path(),'-hide_banner','-loglevel','error','-xerror','-y',
               '-i',str(original),'-map','0:a:0','-vn','-ac','1','-ar','16000',
               '-c:a','pcm_s16le',str(temporary)]
    try:
        completed = subprocess.run(command,capture_output=True,timeout=300)
        if completed.returncode:
            raise ValueError('缓存视频音轨提取失败，不能生成可靠回听样本')
        with wave.open(str(temporary)) as audio:
            if audio.getnframes()<=0:
                raise ValueError('缓存视频没有可用音轨')
        temporary.replace(source)
    except subprocess.TimeoutExpired:
        raise ValueError('本地音轨提取超时，未生成回听样本') from None
    finally:
        temporary.unlink(missing_ok=True)
    return source


def esc(value) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def stamp(seconds) -> str:
    return f"{int(seconds) // 60:02d}:{int(seconds) % 60:02d}"


def elapsed(value) -> str:
    return "未记录" if value is None else f"{float(value):.2f} 秒"


def page(title: str, body: str) -> str:
    return '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>'+esc(title)+'</title><style>body{font:16px/1.8 system-ui,"Microsoft YaHei",sans-serif;background:#f7f8f5;color:#26382b;margin:0}main{max-width:1000px;margin:auto;padding:32px 22px}section{background:white;padding:20px;border-radius:12px;margin:18px 0;border:1px solid #dce5d8}table{border-collapse:collapse;width:100%;overflow-wrap:anywhere}td,th{text-align:left;vertical-align:top;padding:8px;border-bottom:1px solid #e0e6dc}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f3f5f0;padding:14px}audio{max-width:100%}.note{color:#665029;background:#fff2d6;padding:14px;border-radius:8px}summary{cursor:pointer}a{color:#25633d}</style><main><h1>'+esc(title)+'</h1>'+body+'</main></html>'


def render_report(plan: dict, videos: list[dict]) -> str:
    intro = '<section><h2>执行顺序</h2><ol>'+''.join('<li>'+esc(s)+'</li>' for s in plan['workflow'])+'</ol><p>后续由你发“导入好了”开始，不定时运行、不自动调用模型。</p></section>'
    intro += '<p class="note">'+esc('；'.join(plan['limits']))+'。</p>'
    intro += '<section><h2>建议测试样本</h2><p>约 7 分钟、15 分钟、30 分钟各一条。先逐条导入，方便比较首次处理与重试。</p><p>阶段计时为本次实际处理，不含排队；多次尝试逐条保存，不覆盖为一次成功时间。</p></section>'
    intro += '<section><h2>每个视频的考题类型</h2><ul><li>开头、中间、结尾分别定位原话</li><li>跨分钟分段交界的问题</li><li>结合多个章节才能答全的问题</li><li>名称、数字或条件核对</li><li>当前库没有答案的问题</li><li>换到不含相关内容的知识库做对照</li></ul><p>每片先准备6道有依据题和2道无答案/范围对照题。先对照原声填写参考答案、原话和时间，再冻结题目；不能只把自动文字稿当作听写正确性的证明。</p></section>'
    intro += '<section><h2>记录与判定</h2><p>分别记录：下载/提取音频、分段/声音转文字、整理入库、每次尝试总处理，以及提问到首段文字出现、整段回答完成的时间。已完成资料直接复用时，不冒充一次新转写。</p><p>检索看是否找到正确视频与支持答案的原话；最终回答看必要点覆盖、无依据扩写和引用。发现听写错误应先单列，不静默改成正确稿来提高检索成绩。</p></section>'
    if plan.get('target_videos'):
        intro += '<section><h2>本批待测名单（'+str(len(plan['target_videos']))+' 个）</h2><ul>'+''.join('<li>'+esc(v['title'])+'</li>' for v in plan['target_videos'])+'</ul><p>导入已在准备期间开始，质量检查仍等你发“导入好了”再做。</p></section>'
    if plan.get('deployment_note'):
        intro += '<p class="note">'+esc(plan['deployment_note'])+'</p>'
    if not videos:
        return page('长视频测试流程：等待你确认导入完成', intro+'<section><h2>尚未开始质量评测</h2><p>已记录现有视频作为基线与本批待测名单。后续检查新增、状态改变或已指定的视频，不会误把之前的22分钟视频当作新样本。</p></section>')
    for video in videos:
        audit = video['audit']; timing = video['latest_processing']; stages = (timing or {}).get('stages', {})
        intro += '<section><h2>'+esc(video['title'])+'</h2><p>视频编号：'+esc(video['video_id'])+' · 状态：'+esc(video['status'])+' · 时长：'+stamp(video['duration_seconds'])+'</p>'
        if not video['in_target_duration']:
            intro += '<p class="note">时长尚未确认，或明显超出约7–30分钟范围（允许6–31分钟边界）；仍保留状态，比较时应单独说明。</p>'
        intro += '<table><tr><th>本次总处理</th><th>下载/提取</th><th>分段/听写</th><th>整理/入库</th></tr><tr>'+''.join('<td>'+elapsed(x)+'</td>' for x in [(timing or {}).get('total_processing_seconds'),stages.get('download',{}).get('seconds'),stages.get('recognition',{}).get('seconds'),stages.get('indexing',{}).get('seconds')])+'</tr></table>'
        intro += '<p>已保存尝试 '+str(len(video['processing_runs']))+' 次；已记录尝试累计：'+elapsed(video['known_attempt_seconds'])+'。若记录未结束，不能当作完整耗时。</p>'
        if video.get('polling_observation'):
            intro += '<details><summary>旧服务当前批次的观察计时（非精确阶段计时）</summary><p class="note">本批导入在新计时功能加载前已开始。轮询只提供观察估计，起点缺失的阶段不能补成完整耗时。</p><pre>'+esc(json.dumps(video['polling_observation'],ensure_ascii=False,indent=2))+'</pre></details>'
        coverage = audit['request_coverage']
        intro += '<p>分段请求时间覆盖：'+(f'{coverage["ratio"]*100:.2f}%' if coverage else '未记录/无法核对')+'。这不是听写无遗漏的证明。</p>'
        intro += '<p>结构检查：'+esc(audit['structural_status'])+'；听写遗漏判断：'+esc(audit['omission_verdict'])+'</p>'
        intro += '<ul>'+''.join('<li>'+esc(issue)+'</li>' for issue in audit['issues'])+'</ul>'
        intro += '<p>原声缓存：'+('已保留，可生成回听片段' if video['local_audio_available'] else '当前没有本地音频，需回到原视频核对')+'</p>'
        intro += '<details><summary>回听开头、中间、结尾与分段交界</summary><table><tr><th>位置</th><th>原视频时间</th><th>状态</th></tr>'+''.join('<tr><td>'+esc(w['kind'])+'</td><td>'+stamp(w['start_seconds'])+'–'+stamp(w['end_seconds'])+'</td><td>待回听</td></tr>' for w in audit['listening_windows'])+'</table></details>'
        intro += '<details><summary>疑似需回听的长间隔、重复及完整结构记录</summary><p>'+esc(audit['listening_note'])+'</p><pre>'+esc(json.dumps(audit,ensure_ascii=False,indent=2))+'</pre></details>'
        intro += '<p>下一步：回听核实原话，再填写同目录 question-worksheet.json。题目尚未填写时，检索与回答成绩均为“未测”。</p></section>'
    return page('长视频检查记录（质量结论待回听与答题）', intro)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['init', 'inspect', 'clips', 'compile'])
    parser.add_argument('--run-dir', type=Path, required=True)
    parser.add_argument('--video-id', action='append')
    parser.add_argument('--inspection', type=Path)
    parser.add_argument('--worksheet', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    run_dir = args.run_dir.resolve()
    inspection = args.inspection.resolve() if args.inspection else None
    worksheet = args.worksheet.resolve() if args.worksheet else None
    output = args.output.resolve() if args.output else None
    if args.video_id and any(not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', v) for v in args.video_id):
        parser.error('视频编号格式无效')
    import os
    os.chdir(BACKEND_DIR)
    from app.core.config import settings
    from scripts.evaluate_retrieval import readonly_database
    try:
        if args.action == 'clips':
            if inspection is None or not args.video_id or len(args.video_id) != 1:
                raise ValueError('clips 需要 --inspection 和一个 --video-id')
            row = next((r for r in read_json(inspection)['videos'] if r['video_id'] == args.video_id[0]), None)
            if row is None:
                raise ValueError('该检查记录中没有这个视频')
            source = listening_source(row['video_id'], inspection.parent)
            if not row['audit']['listening_windows']:
                raise ValueError('当前记录尚无有效时长和回听窗口，请先确认入库状态')
            target = output or inspection.parent / 'clips' / row['video_id']
            clips = extract_windows(source, row['audit']['listening_windows'], target)
            print(json.dumps({'clips':len(clips),'output':str(target)},ensure_ascii=False))
            return 0
        with readonly_database(settings.database_url) as db:
            if args.action == 'init':
                plan = initialize(run_dir, collect_videos(db))
                print(json.dumps({'prepared':True,'baseline_videos':len(plan['baseline_videos']),'output':str(run_dir)},ensure_ascii=False))
            elif args.action == 'inspect':
                data, target = inspect_videos(run_dir, collect_videos(db), args.video_id)
                print(json.dumps({'videos':len(data['videos']),'output':str(target),'quality':'待回听和标注','model_calls':0},ensure_ascii=False))
            else:
                if worksheet is None:
                    raise ValueError('compile 需要 --worksheet')
                result = compile_exam(db, read_json(worksheet), output or run_dir / 'long-video-exam.json')
                print(json.dumps(result,ensure_ascii=False))
        return 0
    except (OSError, ValueError, KeyError, TypeError, wave.Error) as error:
        print('测试准备未完成：'+str(error),file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
