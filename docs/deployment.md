# Runtime and deployment boundary

Install `ydbdoc-review-ng[runtime]` and set
`YDBDOC_RUNTIME_FACTORY=ydbdoc_review_ng.runtime:create_runtime`. This shipped
factory returns real `LinearWorkflows`. Construction performs no I/O; each
instance is one job. Tests replace only `github_transport`, `model_transport`
and `ydb_executor`. No separate deployment Python module must be authored.

## Environment

| Setting | Meaning |
|---|---|
| `GITHUB_ACTOR` | Authenticated runner identity, never an untrusted CLI argument. |
| `YDBDOC_ALLOWED_ACTORS` | Comma/whitespace separated exact logins. Empty denies all. |
| `YDB_GH_TOKEN`, fallback `GH_TOKEN` | Project token takes precedence. Contents/PR write and checks read in `ydb-platform/ydb`. |
| `YANDEX_API_KEY`, `YANDEX_FOLDER_ID` | Native Yandex model credentials. |
| `YDBDOC_MODEL` | Optional native model name, default `yandexgpt-5.1/latest`. |
| `YDB_ENDPOINT`, `YDB_DATABASE`, `YDB_TOKEN` | YDB endpoint, database path and access token. Connection is lazy. |
| `YDBDOC_DAILY_BUDGET_RUB` | Passed to translate as `--budget-rub`; verify has no gate. |
| `YDBDOC_MAX_DEPENDENCY_FILES_PER_ARTICLE`, `YDBDOC_MAX_SOURCE_CHARACTERS` | Scope bounds, defaults 100 dependency files and 200000 source characters. |
| `YDBDOC_TRUSTED_RUNTIME_SHA` | Required only by the bundled dispatch workflows: reviewed lowercase 40-hex commit of this repository to checkout before invoking the local action. External `uses: ...@v1.0.0` consumers do not use this variable. |

Job start precedes authorization. Denied actors receive terminal failure audit
without GitHub reads/mutations or model calls. Credentials/transcripts are not
printed or included in comments. Every model attempt is persisted. Explicit
billable cost is retained; unavailable cost is NULL, not fabricated zero.
Current-job cost is unknown if any attempt cost is unknown. The daily budget
uses all known costs across both modes and all roles.

## Pinned content and publication

PR/base metadata resolves immutable SHAs; a changing PR inventory is rejected.
Old merged PRs translate the pinned current base. No checkout, hooks or source
repository code runs. Branches are `translation/pr-N` in `ydb-platform/ydb`
using the source PR base. PR body markers bind source PR and source SHA.
Verify requires those markers and the exact current translation head.

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

Provision the database and grant access before enabling workflows. With explicit
authorization and the environment above, supplied executor/schema code is:

```python
import os
from ydbdoc_review_ng.persistence import YdbPersistence
from ydbdoc_review_ng.runtime_ydb import SDKExecutor

YdbPersistence(SDKExecutor(os.environ["YDB_ENDPOINT"], os.environ["YDB_DATABASE"],
                          os.environ["YDB_TOKEN"])).install_schema()
```

This creates two audit tables with 14-day TTL. It is a one-time operation, not
a third CLI mode or automatic factory side effect. No schema creation was
performed during local readiness.

## Offline installed smoke and external cutover

Релизный composite action подключается из подпапки репозитория:

```yaml
- uses: ydb-platform/ydbdoc-review-ng/.github/actions/doc-review@v1.0.0
```

Action устанавливает runtime из собственного checkout независимо от текущего
workspace вызывающего workflow. `doc_translate` передаёт immutable source SHA.
`doc_verify` передаёт source SHA из provenance marker translation PR и точный
текущий target head SHA. `doc_continue` в `v1.0.0` отсутствует и до выполнения
зафиксированного TODO остаётся на прежнем action.

Copy `tests/integration/smoke_installed_runtime.py` and `_runtime_services.py`
beside one another outside the checkout. Run the smoke with a fresh venv Python
containing the built wheel. It imports the shipped factory via the same CLI
environment as the action and replaces only remote I/O. Both modes, real
translation/reporting, one current comment and audit execute with network denied.

Credentials, schema provisioning, token grants, target-head CI wiring, enabling
workflows and replacing the prototype require separate deployment authorization
and integration verification. Dispatching this tool's workflow on another ref
is not evidence for successful `doc_verify`/`build-docs` on the translation SHA.
Local tests never perform those changes or claim a live service is enabled.
