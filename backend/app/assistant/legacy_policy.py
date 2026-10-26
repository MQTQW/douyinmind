"""A migrated project has one authorization authority for assistant actions."""
from pathlib import Path

STATE=Path(__file__).resolve().parents[1]/'storage/assistant'
MESSAGE='检测到日常“抖音助手”状态或需要核对的残留，请从项目中的助手入口操作；旧单次测试入口不再执行或保留发送。'


def legacy_testing_blocked(root=None):
    root=Path(root) if root is not None else STATE
    # A damaged/absent registry with its migration marker is a failure, not a
    # reason to revive the old local-binding.json permissions.
    main=root/'registry.sqlite3'
    paths=(main,root/'registry-required.json',*(Path(str(main)+suffix) for suffix in ('-journal','-wal','-shm')))
    return any(path.exists() or path.is_symlink() for path in paths)
