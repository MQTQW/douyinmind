"""Actual owned Python child, temporary queue, no browser/model/network.

The child only registers, heartbeats, observes pause/resume and exits upon its
own stop-file request. There is deliberately no terminate/kill fallback.
"""
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from app.assistant.control import AssistantControl, process_alive
from app.assistant.queue import AssistantQueue


CHILD_SOURCE = r'''
import json,os,sys,time
from pathlib import Path
sys.path.insert(0,os.getcwd())
from app.assistant.control import AssistantControl
from app.assistant.queue import AssistantQueue
directory=Path(sys.argv[1]).resolve()
queue=AssistantQueue(directory/'queue.sqlite3')
control=AssistantControl(queue,directory)
initial=queue.get_control()
if initial.get('state')!='starting':
    print(json.dumps({'event':'startup_not_registered','pid':os.getpid()}),flush=True)
    raise SystemExit(2)
run_id=initial['run_id']
control.register(run_id)
print(json.dumps({'event':'registered','pid':os.getpid(),'parent_pid':os.getppid()}),flush=True)
paused=False
ticks=0
while not control.should_stop(run_id):
    observed=control.heartbeat(run_id)
    paused=paused or observed['desired_state']=='paused'
    ticks+=1
    time.sleep(.05)
control.finish(run_id,'isolated_stop_requested')
print(json.dumps({'event':'stopped','pid':os.getpid(),'pause_observed':paused,'heartbeat_cycles':ticks}),flush=True)
'''


def _until(predicate, *, seconds=12):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(.05)
    raise TimeoutError("isolated_control_deadline")


def exercise_windows_process_control(directory):
    """Return a machine-readable report; all paths belong to this fixture."""
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    child = directory / "owned_control_child.py"
    child.write_text(CHILD_SOURCE, encoding="utf-8")
    queue = AssistantQueue(directory / "queue.sqlite3")
    processes = []
    actual_pid = launcher_pid = None
    error = None
    started = time.monotonic()
    result = {"scope": "isolated owned Python child and temporary assistant queue; no browser/model/network",
              "child_source_sha256": hashlib.sha256(CHILD_SOURCE.encode()).hexdigest(), "real_browser_started": False,
              "model_calls": 0, "http_calls": 0, "production_database_opened": False,
              "terminate_or_kill_called": False, "spawn_count": 0, "passed": False}
    with ExitStack() as stack:
        stdout = stack.enter_context((directory / "child.stdout.txt").open("x", encoding="utf-8"))
        stderr = stack.enter_context((directory / "child.stderr.txt").open("x", encoding="utf-8"))
        def spawn(command, **kwargs):
            kwargs.update(stdout=stdout, stderr=stderr)
            process = subprocess.Popen(command, **kwargs)
            processes.append(process)
            return process
        control = AssistantControl(queue, directory,
            command_factory=lambda: [sys.executable, str(child), str(directory)], popen_factory=spawn)
        try:
            pending = control.start()
            launcher_pid = processes[0].pid
            active = _until(lambda: (value if value.get("state") == "running" and value.get("pid") and value.get("process_alive")
                                    else None) if (value := control.snapshot()) else None)
            actual_pid = active["pid"]
            assert active["run_id"] == pending["run_id"]
            assert process_alive(actual_pid)
            repeated = control.start()
            assert repeated["pid"] == actual_pid and len(processes) == 1
            control.pause()
            paused = _until(lambda: (value if value.get("state") == "paused" else None)
                            if (value := control.snapshot()) else None)
            assert paused["pid"] == actual_pid
            control.start()
            resumed = _until(lambda: (value if value.get("state") == "running" else None)
                             if (value := control.snapshot()) else None)
            assert resumed["pid"] == actual_pid and len(processes) == 1
            stop = control.stop()
            stop_path = control.stop_path(pending["run_id"])
            assert stop["desired_state"] == "stopped" and stop_path.is_file()
            processes[0].wait(timeout=12)
            _until(lambda: not process_alive(actual_pid), seconds=5)
            _until(lambda: not process_alive(launcher_pid), seconds=5)
            final = control.snapshot()
            assert final["state"] == "stopped" and final["pid"] is None
            assert processes[0].returncode == 0
            result.update(passed=True, run_id=pending["run_id"], pause_observed_in_control=True,
                          resumed_same_actual_pid=True, stop_file_present=stop_path.is_file(), final_state=final["state"])
        except Exception as exc:
            error = type(exc).__name__
        finally:
            # Only a stop request to this test's registered run; never signal or
            # kill a PID. Even failure leaves the durable stop file for the stub.
            if any(process.poll() is None for process in processes):
                try:
                    control.stop()
                except Exception:
                    pass
                for process in processes:
                    try:
                        process.wait(timeout=12)
                    except subprocess.TimeoutExpired:
                        pass
            result.update(spawn_count=len(processes), launcher_pid=launcher_pid, actual_python_pid=actual_pid,
                actual_pid_differs_from_launcher=actual_pid is not None and actual_pid != launcher_pid,
                launcher_pid_exited=launcher_pid is not None and not process_alive(launcher_pid),
                actual_python_pid_exited=actual_pid is not None and not process_alive(actual_pid),
                process_returncodes=[process.poll() for process in processes], elapsed_seconds=round(time.monotonic()-started, 3),
                error_type=error)
    lines = (directory / "child.stdout.txt").read_text(encoding="utf-8").splitlines()
    try:
        result["stdout_events"] = [json.loads(line) for line in lines]
    except (ValueError, TypeError):
        result["stdout_events"] = []
        result["passed"] = False
    result["stderr_empty"] = not (directory / "child.stderr.txt").read_text(encoding="utf-8").strip()
    result["passed"] = bool(result["passed"] and result["launcher_pid_exited"] and result["actual_python_pid_exited"]
                            and result["stderr_empty"] and any(event.get("pause_observed") is True for event in result["stdout_events"]))
    return result


def test_actual_owned_child_registers_real_pid_and_exits_by_stop_file(tmp_path):
    report = exercise_windows_process_control(tmp_path / "isolated-state")
    assert report["passed"], report
    assert report["spawn_count"] == 1
    assert report["process_returncodes"] == [0]
