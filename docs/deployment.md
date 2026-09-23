# Runtime and deployment boundary

Install `ydbdoc-review-ng[runtime]` and set
`YDBDOC_RUNTIME_FACTORY=ydbdoc_review_ng.runtime:create_runtime`. This shipped
factory returns real `LinearWorkflows`. Construction performs no I/O; each
instance is one job. Tests replace only `github_transport`, `model_transport`
and `ydb_executor`. No separate deployment Python module must be authored.

## Environment

| Setting | Meaning |
|---|---|
| `GITHUB_ACTOR` | Authenticated runner identity for translate/verify. Continue authorizes the actual label actor from issue events and the comment author. |
| `YDBDOC_ALLOWED_ACTORS` | Comma/whitespace separated exact logins. Empty denies all. |
| `YDB_GH_TOKEN`, fallback `GH_TOKEN` | Project token takes precedence. Contents/PR write and checks read in `ydb-platform/ydb`. |
| `YANDEX_API_KEY`, `YANDEX_FOLDER_ID` | Native Yandex model credentials. |
| `YDBDOC_MODEL` | Optional native model name, default `yandexgpt-5.1/latest`. |
| `YDB_ENDPOINT`, `YDB_DATABASE`, `YDB_TOKEN` | Optional YDB endpoint, database path and access token. Connection is lazy. |
| `YDB_SA_KEY` | Existing inline Yandex Cloud service-account JSON. Used when `YDB_TOKEN` is absent; endpoint/database default to the deployed documentation database and remain overridable by `YDB_ENDPOINT`/`YDB_DATABASE`. |
| `YDBDOC_DAILY_BUDGET_RUB` | Passed only to translate as `--budget-rub`; verify and continue have no gate. |
| `YDBDOC_MAX_DEPENDENCY_FILES_PER_ARTICLE`, `YDBDOC_MAX_SOURCE_CHARACTERS` | Scope bounds, defaults 100 dependency files and 200000 source characters. |
| `YDBDOC_TRUSTED_RUNTIME_SHA` | Required only by the bundled dispatch workflows: reviewed lowercase 40-hex commit of this repository to checkout before invoking the local action. External consumers pin their `uses:` action reference directly. |

Job start precedes authorization. Denied actors receive terminal failure audit
without GitHub mutations or model calls. Translate/verify deny before GitHub
reads; continue reads issue events/comments to establish the actual actor.
Credentials/transcripts are not
printed or included in comments. Every model attempt is persisted. Explicit
billable cost is retained; unavailable cost is NULL, not fabricated zero.
Current-job cost is unknown if any attempt cost is unknown. The daily budget
uses all known costs across all three modes and all roles.
Translate, critic, final-critic and repair attempts also retain their article
`target_path`; direction remains PR-wide. The QA comment reads cumulative costs
for the pinned `source_sha`, breaks them down by article and role, and lists old
rows without a path as unattributed. Unknown and not-called costs are never
rendered as zero.

## Pinned content and publication

PR/base metadata resolves immutable SHAs; a changing PR inventory is rejected.
Old merged PRs translate the pinned current base. No checkout, hooks or source
repository code runs. Branches are `translation/pr-N` in `ydb-platform/ydb`
using the source PR base. PR body markers bind source PR and source SHA.
Verify requires those markers and the exact current translation head.

Continue accepts only `continue --pr N`, with no source/target SHA or budget
arguments. An allowed author must post a comment whose first line is
`/ydbdoc continue` and whose remaining lines contain nonempty operator context,
then an allowed actor applies label `doc_continue`. The latest eligible comment
must precede the current label event; later edits also cannot supply context.
The runtime resumes only a live checkpoint for direction, pending translation,
or RED review. It validates the saved source/base, exact translation head,
scope digest and field IDs, then rebuilds from authoritative source. Accepted
maps are reused and review covers only unresolved paths. GREEN closes the
checkpoint; another semantic stop keeps the original 14-day expiry. Transport
and persistence failures require a new run. Context remains private to new
model calls and audit, never the report. RED exits 1 in every CLI mode.

The runtime composes pair discovery, one mixed-direction decision, dependency
scope/preflight, parser, strict translation, protected-fragment checks, critic
and at most one repair per job. Validated bytes become Git Data blobs/tree/
commit and a non-force ref update. Byte-identical output cannot create a PR or
comment. Final QA follows the last critic; checks are read for the exact head,
and branch movement blocks stale reporting.

Metadata production is narrow: PyYAML's SafeLoader compose API validates the
complete node structure of pinned source root/changed TOCs before model calls.
Block and flow mappings and nested `items` lists are supported; malformed
YAML, unsupported tags/shapes, duplicate keys, aliases and includes fail with
fixed non-echoing errors, never silently count as unreachable. It does not
construct Python objects or follow external includes. The target TOC gets a
root item inserted by exact node spans, preserving existing text. Rename
replaces the old target href and adds one direct locale-relative `from`/`to` entry in
`ydb/docs/{ru,en}/redirects.yaml`. Multiple edits accumulate into one candidate;
redirect chains/conflicts fail. Accepted redirect shape: `redirects:` followed
by `- from:`/`to:` pairs. Unsupported syntax/preimages fail closed. This is not
a YAML formatter, navigation graph, anchor resolver or general TOC migration.
No per-PR metadata environment blob needs to be authored manually. PyYAML is
an explicit package dependency, installed by the action with the runtime extra.

GitHub collections are single-page bounded. A next-page Link, incomplete PR
file inventory or truncated checks fails closed. No pagination subsystem exists.

## Schema setup, separately authorized

Provision the database and grant access before enabling workflows. The runtime
needs three tables under `ydbdoc_review/`. Run the applicable commands below
from the installed runtime environment after deployment authorization. For a
new database without the tables:

```bash
python - <<'PY'
import os
from ydbdoc_review_ng.persistence import YdbPersistence
from ydbdoc_review_ng.runtime_ydb import DEFAULT_YDB_DATABASE, DEFAULT_YDB_ENDPOINT, SDKExecutor

YdbPersistence(SDKExecutor(
    os.environ.get("YDB_ENDPOINT", DEFAULT_YDB_ENDPOINT),
    os.environ.get("YDB_DATABASE", DEFAULT_YDB_DATABASE),
    os.environ.get("YDB_TOKEN", ""),
    os.environ.get("YDB_SA_KEY", ""),
)).install_schema()
PY
```

For an existing v1.0.x database with `jobs` and `attempts`, run the original
one-time migration first:

```bash
python - <<'PY'
import os
from ydbdoc_review_ng.persistence import YdbPersistence
from ydbdoc_review_ng.runtime_ydb import DEFAULT_YDB_DATABASE, DEFAULT_YDB_ENDPOINT, SDKExecutor

YdbPersistence(SDKExecutor(
    os.environ.get("YDB_ENDPOINT", DEFAULT_YDB_ENDPOINT),
    os.environ.get("YDB_DATABASE", DEFAULT_YDB_DATABASE),
    os.environ.get("YDB_TOKEN", ""),
    os.environ.get("YDB_SA_KEY", ""),
)).migrate_schema()
PY
```

For every existing database, including one already upgraded for continuation,
then run the separate one-time cost-attribution migration:

```bash
python - <<'PY'
import os
from ydbdoc_review_ng.persistence import YdbPersistence
from ydbdoc_review_ng.runtime_ydb import DEFAULT_YDB_DATABASE, DEFAULT_YDB_ENDPOINT, SDKExecutor

YdbPersistence(SDKExecutor(
    os.environ.get("YDB_ENDPOINT", DEFAULT_YDB_ENDPOINT),
    os.environ.get("YDB_DATABASE", DEFAULT_YDB_DATABASE),
    os.environ.get("YDB_TOKEN", ""),
    os.environ.get("YDB_SA_KEY", ""),
)).migrate_cost_schema()
PY
```

Do not run either migration after a fresh install. DDL is nontransactional and
not idempotent: inspect applied statements before retrying a partially failed
upgrade. No schema operation runs automatically in `create_runtime`.

The migration executes these statements in order. The fresh install creates
`jobs` with nullable `source_sha` and `attempts` with nullable `job_id` directly,
then creates the same `continuations` table:

```sql
ALTER TABLE `ydbdoc_review/attempts` ADD COLUMN job_id Utf8;
ALTER TABLE `ydbdoc_review/jobs` ALTER COLUMN source_sha DROP NOT NULL;
ALTER TABLE `ydbdoc_review/attempts` ADD COLUMN target_path Utf8;
CREATE TABLE `ydbdoc_review/continuations` (
    continuation_id Utf8 NOT NULL,
    job_id Utf8 NOT NULL,
    source_pr Uint64 NOT NULL,
    trigger_pr Uint64 NOT NULL,
    source_sha Utf8 NOT NULL,
    base_sha Utf8 NOT NULL,
    translation_branch Utf8 NOT NULL,
    target_sha Utf8,
    stage Utf8 NOT NULL,
    source_inventory String NOT NULL,
    scope_target_paths String NOT NULL,
    state String NOT NULL,
    status Utf8 NOT NULL,
    consumed_by_job_id Utf8,
    created_at Timestamp NOT NULL,
    PRIMARY KEY (continuation_id)
) WITH (TTL = Interval("P14D") ON created_at);
```

`jobs` and `attempts` retain TTL `Interval("P14D") ON started_at`; old attempts
may have NULL `job_id` or `target_path`. A new continuation audit starts with
NULL `source_sha`
until admission binds the saved source. Checkpoint `state` is strict JSON v1;
`source_inventory` and `scope_target_paths` freeze replay inputs. A replacement
checkpoint inherits `created_at`, so retrying never extends its TTL. Runtime
also rejects expired rows before physical TTL deletion. `status` and
`consumed_by_job_id` protect semantic handoff and consumption. No schema changes
were executed against live YDB during local acceptance.

## Offline installed smoke and external cutover

Version 1.1.0 implements continuation; publishing `v1.1.0` and cutting over the
consumer are separate operations after the final release gate. A trusted consumer
uses the action subdirectory pinned to a reviewed immutable 40-hex commit:

```yaml
- uses: ydb-platform/ydbdoc-review-ng/.github/actions/doc-review@REVIEWED_1_1_0_COMMIT_SHA
  with:
    mode: continue
    pr: ${{ github.event.pull_request.number }}
```

Action устанавливает runtime из собственного checkout независимо от текущего
workspace вызывающего workflow. `doc_translate` передаёт immutable source SHA.
`doc_verify` передаёт source SHA из provenance marker translation PR и точный
текущий target head SHA. `doc_continue` передаёт только PR. Action metadata
оставляет `source-sha` optional, а первый shell step строго проверяет mode:
translate требует source и finite nonnegative budget, verify требует source
и target без budget, continue запрещает любые непустые source/target/budget.
Проверка выполняется до Python setup/install и runtime.

The complete [consumer template](examples/doc_continue.yml) belongs in
`ydb-platform/ydb/.github/workflows/doc_continue.yml` after replacing its explicit
SHA placeholder. It handles `pull_request_target: labeled`, filters the
`doc_continue` label, uses existing credentials and serializes by repository+PR
with cancellation disabled. There is no PR checkout. The trusted action performs
authorization and records denied jobs. An `issues: labeled` variant must also
guard `github.event.issue.pull_request` and pass the issue's PR number; do not
run ordinary issues as PRs. The bundled tool-repository workflow_dispatch files
are smoke/templates and cannot receive labels from the consumer repository.
No consumer workflow, tag or external cutover PR was changed by this implementation.

Copy `tests/integration/smoke_installed_runtime.py` and `_runtime_services.py`
beside one another outside the checkout. Run the smoke with a fresh venv Python
containing the built wheel. It imports the shipped factory via the same CLI
environment as the action and replaces only remote I/O. Translate, RED verify,
successful continue, one current comment and checkpoint close execute with network
denied. Do not set `PYTHONPATH=src`: the script asserts the imported runtime is
inside the installed venv. With a wheelhouse containing the runtime dependencies:

```bash
python -m pip wheel --no-index --no-deps --no-build-isolation --wheel-dir dist .
smoke_dir=$(mktemp -d)
python -m venv "$smoke_dir/venv"
"$smoke_dir/venv/bin/python" -m pip install --no-index --find-links "$WHEELHOUSE" \
  'dist/ydbdoc_review_ng-1.1.0-py3-none-any.whl[runtime]'
cp tests/integration/smoke_installed_runtime.py tests/integration/_runtime_services.py "$smoke_dir/"
(cd "$smoke_dir" && env -u PYTHONPATH "$smoke_dir/venv/bin/python" smoke_installed_runtime.py)
```

Credentials, schema provisioning, token grants, target-head CI wiring, enabling
workflows and replacing the prototype require separate deployment authorization
and integration verification. Dispatching this tool's workflow on another ref
is not evidence for successful `doc_verify`/`build-docs` on the translation SHA.
Local tests never perform those changes or claim a live service is enabled.
