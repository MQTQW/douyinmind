"""Freeze public Python code only, for isolated tests during parallel changes.

The origin may change after creation. A passing snapshot is evidence for its
recorded files, not a claim that a later working tree has passed validation.
No .env, storage, database, media, browser profile or dependency is copied.
Only app/test Python and an explicit allowlist of published scripts are included;
ignored local journal acceptance tests are excluded.
"""
import argparse
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--label', required=True)
    parser.add_argument('--include-launcher-source', action='store_true',
                        help='Include byte-identical public launcher as an AST-only test fixture')
    args = parser.parse_args()
    source = Path(os.path.abspath(args.source))
    output = Path(os.path.abspath(args.output))
    if (source.resolve() != source or output.resolve() != output
            or output == source or source in output.parents
            or re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', args.label) is None):
        parser.error('Use a plain source path and a private output outside that source')
    runner = source/'tools/portable_guarded_tests.py'
    spec = importlib.util.spec_from_file_location('public_code_inventory', runner)
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    helper.safe_directory(source)
    helper.safe_file(runner)
    output.mkdir(parents=True, exist_ok=True)
    helper.safe_directory(output)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    target = output/(args.label+'-'+stamp)
    target.mkdir()
    backend = source/'backend'
    for attempt in range(3):
        paths = helper.inventory(backend)
        before = helper.manifest(backend, paths)
        for path in paths:
            destination = target/'backend'/path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes((backend/path).read_bytes())
        after = helper.manifest(backend, helper.inventory(backend))
        if before == after:
            break
    else:
        raise RuntimeError('Source changed repeatedly while copying; incomplete snapshot retained')
    # Do not leave stale files if a developer removed a source during a retry.
    copied = set(helper.inventory(target/'backend'))
    if copied != set(paths):
        raise RuntimeError('Inventory changed during copy; incomplete snapshot retained')
    if helper.manifest(target/'backend', paths) != before:
        raise RuntimeError('Frozen bytes differ from origin inventory')
    extras = {}
    if args.include_launcher_source:
        launcher = source/'start-local.py'
        helper.safe_file(launcher)
        raw = launcher.read_bytes()
        relative = Path('app/_launcher_shutdown_source.py')
        (target/'backend'/relative).write_bytes(raw)
        if launcher.read_bytes() != raw:
            raise RuntimeError('Public launcher changed during snapshot creation')
        extras['start-local.py'] = {'copied_as': relative.as_posix(), 'sha256': helper.sha(raw)}
    (target/'tools').mkdir()
    shutil.copyfile(runner, target/'tools/portable_guarded_tests.py')
    report = {'origin':str(source), 'snapshot':str(target), 'public_files':before,
              'extra_public_sources':extras,
              'private_data_copied':False, 'runner_sha256':helper.sha(runner.read_bytes()),
              'scope':'Only recorded snapshot; original may continue changing'}
    (target/'snapshot.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps({'snapshot':str(target),'public_file_count':len(paths)+len(extras),'private_data_copied':False}))


if __name__ == '__main__':
    main()
