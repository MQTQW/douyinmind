"""Explicit foreground launcher for one bounded, already-bound agent test.

Reading the binding and library description does not call a model or retrieve
private chunks. The receiver is invoked only after the operator starts this
module; it is never imported by application startup.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
from app.assistant.legacy_policy import legacy_testing_blocked, MESSAGE as MIGRATED_MESSAGE
import secrets

from .contracts import Binding


BACKEND = Path(__file__).resolve().parents[2]
STATE = BACKEND / "app" / "storage" / "mention_agent"


class LauncherError(ValueError):
    """A fixed message suitable for the foreground console."""


def generate_marker(*, allow_import=False) -> str:
    moment = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    mode = "IMPORT" if allow_import else "AUTOQA"
    return f"DM-{moment}-{mode}-{secrets.token_hex(4).upper()}"


def _read_binding(path: Path) -> Binding:
    if not path.is_file():
        raise LauncherError("未找到已有本机绑定文件，请先按说明完成绑定；本入口不会自动创建绑定。")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        binding = Binding.from_dict(value)
    except (OSError, UnicodeError, ValueError):
        raise LauncherError("已有绑定文件无法读取或格式不完整，请核对本机公开账号与知识库配置。") from None
    if not binding.active:
        raise LauncherError("当前本机绑定已停用，本入口不会自动启用或更换账号。")
    if not binding.public_reply_allowed:
        raise LauncherError("当前绑定尚未允许公开回复，请先按说明明确授权；本入口不会修改授权设置。")
    return binding


def _describe_library(binding: Binding) -> dict:
    # describe opens the business SQLite file read-only and does not initialize
    # the retrieval provider or make a model/embedding request.
    from app.core.config import settings
    from app.mention_receiver.catalog import configured_database
    from .knowledge import LocalKnowledgeTools

    database = configured_database(settings.database_url, BACKEND)
    return LocalKnowledgeTools(database, binding.knowledge_base_id).describe()


def _receiver_main(arguments: list[str]) -> int:
    from app.mention_receiver.__main__ import main as receive
    return receive(arguments)


def launch(binding_path: Path, state_path: Path, *, allow_import=False) -> int:
    if type(allow_import) is not bool:
        raise LauncherError("视频入库开关必须明确设置为布尔值。")
    binding_path, state_path = Path(binding_path).resolve(), Path(state_path).resolve()
    binding = _read_binding(binding_path)
    description = _describe_library(binding)
    if (not isinstance(description, dict) or type(description.get("id")) is not int
            or description["id"] != binding.knowledge_base_id
            or not isinstance(description.get("name"), str)
            or not description["name"].strip() or len(description["name"]) > 128):
        raise LauncherError("未能核对已绑定的知识库，请检查本机库是否仍然存在。")
    if not allow_import and (type(description.get("ready_count")) is not int or description["ready_count"] <= 0):
        raise LauncherError("绑定知识库当前没有可检索的已完成资料，请先准备资料后再测试问答。")
    # The explicit save grammar compares the actual stored library name, so an
    # import instruction must not collapse its internal spaces or rename it.
    name = description["name"] if allow_import else " ".join(description["name"].split())
    marker = generate_marker(allow_import=True) if allow_import else generate_marker()
    question = (f"请把这个视频加入“{name}”知识库。测试编号 {marker}。" if allow_import else
                "请根据你绑定的知识库，介绍其中一个主题，给出两条有原文依据的要点，并标明资料来源。"
                f"测试编号 {marker}。")
    print("DouyinMind · 本次单账号、单库视频入库测试" if allow_import else
          "DouyinMind · 本次单账号、单库自动问答测试")
    print(f"已绑定知识库：{name}（编号 {binding.knowledge_base_id}）")
    print("本次限定 20 分钟、最多 1 个智能任务和 1 次发布流程；指令理解、声音识别和资料处理可能产生 API 费用。" if allow_import else
          "本次限定 20 分钟、最多 1 个智能任务和 1 次发布流程；模型与检索可能产生 API 费用。")
    print("请保持控制台打开，并在助手窗口核对 B 已登录。用绑定账号 A 在视频评论区从 @ 列表选中 B。")
    print("选中 B 后粘贴以下正文，不要只手动输入昵称：")
    print()
    print(question)
    print()
    print(f"本次标记：{marker}")
    print("出现核验或结果不明会锁定暂停，不自动重发；关闭助手窗口或使用正常停止命令可结束。")
    print("正在启动本次独立助手；不创建绑定、不代登录、不修改设置，也不启动原后台。", flush=True)
    arguments = [
        "--agent-binding", str(binding_path), "--agent-state", str(state_path),
        "--agent-marker", marker, "--agent-max-tasks", "1", "--agent-auto-reply",
        "--duration-minutes", "20",
    ]
    if allow_import:
        arguments.append("--agent-allow-import")
    return _receiver_main(arguments)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="手动启动本机已绑定知识库的一次问答或视频入库测试")
    parser.add_argument("--binding", type=Path, default=STATE / "local-binding.json")
    parser.add_argument("--state", type=Path, default=STATE / "agent.sqlite3")
    parser.add_argument("--allow-import", action="store_true", help="显式测试当前视频入库；默认保持只读知识库问答")
    args = parser.parse_args(argv)
    if legacy_testing_blocked():
        print(MIGRATED_MESSAGE)
        return 2
    try:
        result = launch(args.binding, args.state, **({"allow_import": True} if args.allow_import else {}))
        if result != 0:
            print("本次测试未正常完成，请核对上方状态和本机环境；不会自动重启或重发。")
        return result
    except LauncherError as error:
        print(str(error))
    except KeyboardInterrupt:
        print("本人已停止本次测试，现有绑定与任务资料保留。")
        return 130
    except Exception as error:
        # Never print provider/configuration exceptions: they can contain
        # private paths, request content, URLs or credentials.
        label = "视频入库" if args.allow_import else "智能问答"
        print(f"本次{label}测试未能正常启动或已停止（{type(error).__name__}），请检查现有环境和本机状态。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
