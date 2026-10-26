"""Explicit operator commands. No background startup or automatic public send."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import uuid
from app.assistant.legacy_policy import legacy_testing_blocked, MESSAGE as MIGRATED_MESSAGE

from .contracts import Binding, canonical_json
from .intake import make_envelope
from .service import AgentService, configured_database, open_store, read_binding


STATE = Path(__file__).resolve().parents[1] / "storage/mention_agent"


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def save_new(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # A successful send reservation is never overwritten by a retry.
    with path.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description="DouyinMind单用户DeepSeek工具代理")
    parser.add_argument("--binding", type=Path, default=STATE / "local-binding.json")
    parser.add_argument("--state", type=Path, default=STATE / "agent.sqlite3")
    sub = parser.add_subparsers(dest="command", required=True)
    bind = sub.add_parser("bind", help="在本机明确绑定A、B和一个已有库；不是多人OAuth认证")
    bind.add_argument("--sender-profile", required=True)
    bind.add_argument("--receiver-profile", required=True)
    bind.add_argument("--library-id", type=int, required=True)
    bind.add_argument("--allow-public-replies", action="store_true")
    run = sub.add_parser("process", help="处理经过核对的公开通知/详情快照，生成待发送回复")
    run.add_argument("snapshot", type=Path)
    run.add_argument("--source-mode", choices=["live_ui", "replay", "isolated"], default="replay")
    run.add_argument("--output", type=Path)
    status = sub.add_parser("status")
    status.add_argument("--task-id")
    revise = sub.add_parser("revise-unsent", help="操作员显式修订一次尚未预留发送的草稿，旧稿完整留档")
    revise.add_argument("task_id")
    revise.add_argument("--reason", required=True)
    reserve = sub.add_parser("reserve-send", help="供受控账号执行器在当前原评论核对后保留一次发送")
    reserve.add_argument("task_id")
    reserve.add_argument("verification", type=Path)
    reserve.add_argument("--output", type=Path, required=True)
    finish = sub.add_parser("record-send", help="记录真实账号执行结果；不进行网页发送")
    finish.add_argument("reservation", type=Path)
    finish.add_argument("outcome", choices=["b_view_visible", "delivery_unknown", "verification_required", "failed_no_submit"])
    finish.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command!='status' and legacy_testing_blocked():
        print(MIGRATED_MESSAGE)
        return 2
    database = configured_database()
    store = open_store(args.state, database)
    if args.command == "bind":
        from .knowledge import LocalKnowledgeTools
        description = LocalKnowledgeTools(database, args.library_id).describe()
        value = Binding.from_dict({"binding_id": "local-" + uuid.uuid4().hex,
            "instance_id": uuid.uuid4().hex, "sender_profile_url": args.sender_profile,
            "receiver_profile_url": args.receiver_profile, "knowledge_base_id": args.library_id,
            "public_reply_allowed": args.allow_public_replies, "active": True})
        if args.binding.exists():
            old = read_binding(args.binding)
            if any(getattr(old, key) != getattr(value, key) for key in
                   ("sender_profile_url", "receiver_profile_url", "knowledge_base_id", "public_reply_allowed", "active")):
                raise ValueError("现有绑定不同，请保留旧台账并显式选择新的配置文件")
            value = old
        else:
            save_new(args.binding, value.to_dict())
        store.put_binding(value.to_dict())
        print(canonical_json({"status": "bound", "binding_id": value.binding_id, "library": description,
                              "scope": "single-owner local operator binding; not multi-user OAuth"}))
        return 0
    binding = read_binding(args.binding)
    store.put_binding(binding.to_dict())
    if args.command == "process":
        snapshot = read_json(args.snapshot)
        envelope = make_envelope(snapshot["notice"], snapshot["detail"], binding, args.source_mode)
        result = AgentService(store, binding, database=database,
            scope_authorizer=lambda _:not legacy_testing_blocked()).process(envelope)
        if args.output:
            save_new(args.output, result)
        print(canonical_json(result))
    elif args.command == "status":
        service = AgentService(store, binding, database=database)
        if args.task_id:
            result = service._result(args.task_id)
            result["steps"] = [{"kind": row["kind"], "metadata": row["metadata"]} for row in store.list_steps(args.task_id)]
        else:
            result = [{"task_id": row["task_id"], "state": row["state"], "reason_code": row.get("reason_code")}
                      for row in store.list_tasks(binding.binding_id)]
        print(canonical_json(result))
    elif args.command == "reserve-send":
        output = args.output.resolve()
        if output.exists():
            raise ValueError("发送保留文件已经存在，不能再次保留")
        task = store.get_task(args.task_id)
        if not task or task["binding_id"] != binding.binding_id:
            raise ValueError("任务不属于当前绑定")
        reservation = store.reserve_send(args.task_id, read_json(args.verification))
        if reservation is None:
            raise ValueError("原评论核对、公开授权或发送状态未通过；未保留发送")
        try:
            save_new(output, {"task_id": args.task_id, "send_token": reservation["send_token"],
                "text": reservation["text"], "target": reservation["target"],
                "reserved_at_utc": datetime.now(timezone.utc).isoformat()})
        except Exception:
            # A durable reservation already exists: never retry the post.
            print(canonical_json({"status": "reservation_saved_but_export_failed", "task_id": args.task_id}))
            return 2
        print(canonical_json({"status": "send_reserved", "task_id": args.task_id,
                              "text": reservation["text"], "reservation_file": str(output)}))
    elif args.command == "revise-unsent":
        task = store.get_task(args.task_id)
        if not task or task["binding_id"] != binding.binding_id:
            raise ValueError("任务不属于当前绑定")
        revised = store.requeue_unsent(args.task_id, args.reason)
        print(canonical_json({"task_id": revised["task_id"], "status": revised["state"],
                              "old_proposals_archived": len(store.list_reply_revisions(args.task_id))}))
    elif args.command == "record-send":
        reservation = read_json(args.reservation)
        task = store.get_task(reservation["task_id"])
        if not task or task["binding_id"] != binding.binding_id:
            raise ValueError("任务不属于当前绑定")
        result = store.finish_send(reservation["task_id"], reservation["send_token"], args.outcome, read_json(args.evidence))
        print(canonical_json({"status": result["state"], "task_id": reservation["task_id"]}))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        # Do not print provider exception strings or arbitrary tool input.
        print(canonical_json({"status": "failed", "error_type": type(error).__name__,
                              "message": "操作未完成；请查看独立任务状态，勿自动重发。"}))
        raise SystemExit(1)
