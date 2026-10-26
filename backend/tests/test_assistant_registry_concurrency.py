"""Windows-compatible Registry lifecycle contention, owned temporary files only."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from app.assistant.registry import Registry, RegistryError
from app.storage_guard import MAGIC


CATALOG = [{"id": 6, "name": "isolated-library"}]
RECEIVER = "https://www.douyin.com/user/registry-contention-B"


def test_two_instances_initialize_authorize_and_write_on_shared_file(tmp_path):
    path = tmp_path / "registry.sqlite3"
    ready = threading.Barrier(2)

    def run(tag):
        ready.wait(timeout=5)
        registry = Registry(path, catalog_reader=lambda: CATALOG)
        result = []
        for number in range(8):
            user = registry.register_user(f"https://www.douyin.com/user/thread-{tag}-{number}", RECEIVER)
            registry.verify_user(user["user_id"], verified_by="isolated-local-admin")
            device = registry.register_device(user["user_id"], is_default=True)
            registry.verify_device(device["device_id"], verified_by="isolated-local-admin")
            grant = registry.grant_library(user["user_id"], device["device_id"], 6, is_default=True)
            frozen = registry.authorization_snapshot(grant["binding_id"], device["instance_id"], 6, "query")
            assert frozen is not None
            assert registry.authorize_snapshot(frozen).sender_profile_url == user["sender_profile_url"]
            result.append(user["user_id"])
        return result

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(run, tag) for tag in ("a", "b")]
        ids = [value for future in futures for value in future.result(timeout=30)]
    snapshot = Registry(path, catalog_reader=lambda: CATALOG, allow_create=False).snapshot()
    assert len(ids) == len(set(ids)) == len(snapshot["users"]) == 16
    assert all(user["verification_status"] == "verified" for user in snapshot["users"])
    assert Path(str(path) + ".guard.lock").read_bytes() == MAGIC


CHILD = r'''
import json,os,sys
from pathlib import Path
from app.assistant.registry import Registry
path=Path(sys.argv[1]); tag=sys.argv[2]
registry=Registry(path,catalog_reader=lambda:[{'id':6,'name':'isolated-library'}])
ids=[]
for number in range(16):
    user=registry.register_user('https://www.douyin.com/user/process-'+tag+'-'+str(number),
                               'https://www.douyin.com/user/registry-contention-B')
    registry.verify_user(user['user_id'],verified_by='isolated-local-admin')
    ids.append(user['user_id'])
print(json.dumps({'pid':os.getpid(),'ids':ids}),flush=True)
'''


def test_two_real_python_processes_share_initialization_and_full_sql_lifecycle(tmp_path):
    path = tmp_path / "registry.sqlite3"
    processes = [subprocess.Popen([sys.executable, "-B", "-c", CHILD, str(path), tag],
        cwd=Path(__file__).resolve().parents[1], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)) for tag in ("a", "b")]
    results = []
    try:
        for process in processes:
            stdout, stderr = process.communicate(timeout=25)
            assert process.returncode == 0, stderr
            assert not stderr
            results.append(json.loads(stdout))
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
                process.communicate(timeout=5)
    snapshot = Registry(path, catalog_reader=lambda: CATALOG, allow_create=False).snapshot()
    ids = [value for result in results for value in result["ids"]]
    assert len(ids) == len(set(ids)) == len(snapshot["users"]) == 32
    assert all(user["verification_status"] == "verified" for user in snapshot["users"])
    assert all(process.poll() == 0 for process in processes)
    assert Path(str(path) + ".guard.lock").read_bytes() == MAGIC


def test_lifecycle_lock_keeps_main_hardlink_and_protected_path_rejection(tmp_path):
    path = tmp_path / "registry.sqlite3"
    registry = Registry(path, catalog_reader=lambda: CATALOG)
    original = path.read_bytes()
    os.link(path, tmp_path / "must-preserve-copy.sqlite3")
    with pytest.raises(RegistryError, match="unsafe_registry_file"):
        registry.snapshot()
    assert path.read_bytes() == original
    protected = tmp_path / "protected.sqlite3"
    protected.write_bytes(b"protected-fixture")
    with pytest.raises(RegistryError, match="protected_registry_path"):
        Registry(protected, protected_paths=(protected,))
    assert protected.read_bytes() == b"protected-fixture"
    assert not Path(str(protected) + ".guard.lock").exists()


def test_lifecycle_lock_does_not_relax_sidecar_hardlink_rejection(tmp_path):
    path = tmp_path / "registry.sqlite3"
    registry = Registry(path, catalog_reader=lambda: CATALOG)
    journal = Path(str(path) + "-journal")
    journal.write_bytes(b"foreign-journal-fixture")
    os.link(journal, tmp_path / "must-preserve-journal")
    with pytest.raises(RegistryError, match="unsafe_registry_file"):
        registry.snapshot()
    assert journal.read_bytes() == b"foreign-journal-fixture"
