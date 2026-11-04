# CI scope

`ci.yml` runs frontend dependency installation, tests and production build on Node.js 22 and 24. The backend job uses Windows, Python 3.12 and `uv sync --locked --extra dev` against `backend/uv.lock`.

Pushes to `master` and `codex/**` branches, pull requests and manual dispatch can trigger checks. Feature-branch checks can run before any default-branch merge; the workflow has no publishing or deployment step.

Backend checks use `tools/portable_guarded_tests.py`. It copies public Python code into a temporary snapshot, removes sensitive environment values and redirects test writes to temporary files. No real model keys, `.env`, account sessions, media, production application database or Chroma index are provided. This workflow does not install browser binaries or ffmpeg, and the selected tests do not invoke them.

The explicit backend collection covers retry/accounting, per-call observation, query-embedding contracts, lexical preparation, material deletion/audit, retrieval, typed persistent jobs, shutdown deadlines, durable notes, conversation scopes, clarification storage/recovery and versioned menus. A separate ASGI group includes the notes API and permits only the runner's verified CPython internal socketpair boundary; application networking remains denied. Another separate invocation allows hardlinks only between writable temporary fixture files and checks that stores reject linked database/sidecar paths. Symlink creation, private/source/runtime links and application sockets remain prohibited. Report failures are not bypassed or converted into success.

This is a bounded regression selection, not the entire backend suite or a browser/platform acceptance run. Broader DOM/media, native browser, account, provider and full-suite fixtures need a separately reviewed environment. Cross-computer execution and real comment visibility are not tested here. The three selected backend run blocks were also executed locally against frozen public source and locked dependencies. This is not evidence of a GitHub-hosted run.

The test invocations explicitly disable HTTPX's optional console entry point to avoid initializing Click's native Windows console during imports. Real HTTP clients and SDK classes remain intact; the optional HTTPX CLI itself is not tested. The public snapshot includes a byte-identical launcher source fixture for AST-only shutdown checks.

Action revisions were checked against official sources on 2026-11-04 and pinned to their full commit IDs:

- [actions/checkout v7.0.1](https://github.com/actions/checkout/releases/tag/v7.0.1): `3d3c42e5aac5ba805825da76410c181273ba90b1`
- [actions/setup-node v7.1.0](https://github.com/actions/setup-node/releases/tag/v7.1.0): `949feb2413d6458794dcd2491c4babbbce0c15c1`
- [actions/setup-python v7.1.0](https://github.com/actions/setup-python/releases/tag/v7.1.0): `6d4fd22b20465ccc0f7354476588e8df7c9ca0e0`
- [astral-sh/setup-uv v10.3.3](https://github.com/astral-sh/setup-uv/releases/tag/v10.3.3): `68559e99cc807c1eb7e9238fd43a15bba460eec2`; the installed tool is pinned to [uv 0.13.7](https://github.com/astral-sh/uv/releases/tag/0.13.7).

The commands follow the [official uv GitHub Actions integration guide](https://docs.astral.sh/uv/guides/integration/github/). Checkout does not retain credentials, workflow permissions are `contents: read`, and no publishing/deployment step is defined.
