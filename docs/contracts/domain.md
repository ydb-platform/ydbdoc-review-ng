# Frozen domain contract

This document is the standalone public contract for the T002 domain vocabulary,
wire schema, and ports. Schema version 1 is frozen. An existing enum value,
type tag, record field, field order, or wire key must not change in place. An
incompatible change requires a new schema version and an explicit migration.
The version 1 reader accepts only version 1.

## Enum vocabulary

All enums inherit from both `str` and `Enum`. They have no aliases and no
members beyond this table. Constructing an enum from an unknown value raises
the standard `ValueError`.

| Enum | Member | Wire value |
|---|---|---|
| `Locale` | `RU` | `"ru"` |
| `Locale` | `EN` | `"en"` |
| `Mode` | `DOC_TRANSLATE` | `"doc_translate"` |
| `Mode` | `DOC_VERIFY` | `"doc_verify"` |
| `Mode` | `DOC_CONTINUE` | `"doc_continue"` |
| `ModelRole` | `DIRECTION` | `"direction"` |
| `ModelRole` | `TRANSLATE` | `"translate"` |
| `ModelRole` | `CRITIC` | `"critic"` |
| `ModelRole` | `REPAIR` | `"repair"` |
| `ModelRole` | `FINAL_CRITIC` | `"final_critic"` |
| `Severity` | `GREEN` | `"green"` |
| `Severity` | `YELLOW` | `"yellow"` |
| `Severity` | `RED` | `"red"` |
| `PublicationDecision` | `WITHHOLD_INCOMPLETE` | `"withhold_incomplete"` |
| `PublicationDecision` | `WITHHOLD_UNSAFE` | `"withhold_unsafe"` |
| `PublicationDecision` | `PUBLISH_RED` | `"publish_red"` |
| `PublicationDecision` | `PUBLISH_NORMAL` | `"publish_normal"` |
| `MergeVerdict` | `NOT_APPLICABLE` | `"not_applicable"` |
| `MergeVerdict` | `BLOCKED` | `"blocked"` |
| `MergeVerdict` | `WAITING_CI` | `"waiting_ci"` |
| `MergeVerdict` | `READY` | `"ready"` |

## Scalar values

Each scalar is an independent `@dataclass(frozen=True, slots=True)` with one
field, `value: str`. The constructor requires `type(value) is str`, so a string
subclass is rejected. `str(scalar)` returns the original value. Values are not
case-normalized or Unicode-normalized. On the wire, a scalar is represented by
its string, never by an object containing a `value` key.

| Type | Accepted format |
|---|---|
| `JobId` | Non-empty, no leading or trailing whitespace, and no NUL, CR, or LF character. T002 does not impose any other identifier format. |
| `RepositoryId` | Exactly `owner/name`. Both components are non-empty, neither is `.` or `..`, no whitespace is present, and there is exactly one `/`. |
| `GitSha` | Exactly 40 lowercase hexadecimal digits. This identifies a Git commit snapshot, not a content hash. |
| `ContentHash` | Exactly 64 lowercase hexadecimal digits, representing SHA-256. |
| `RepoPath` | A non-empty relative POSIX path. Leading or trailing `/`, backslash, NUL, empty components, `.` components, and `..` components are forbidden. Locale mapping is outside this type. |

A scalar type or content violation raises `InvariantViolation` and identifies
the scalar type and `value` field without dumping the rejected content.

## Records

Every record is an independent `@dataclass(frozen=True, slots=True)`. The fields
below are listed in constructor and serialization order. Assigning a field
after construction raises `dataclasses.FrozenInstanceError`.

Before checking content invariants, constructors check exact runtime types for
every field. Raw strings cannot replace enums, wrappers, or records. A declared
`str` field requires `type(value) is str`. An optional accepts only `None` or the
exact declared type. An `int` field requires the exact `int` type, so `bool` is
rejected. Subclasses and structurally similar objects are rejected. A type or
content failure raises `InvariantViolation` naming the record type and field.

### `SnapshotRef`

```python
repository: RepositoryId
commit_sha: GitSha
```

It identifies one immutable repository tree. It does not contain a branch,
ref name, HEAD, worktree path, or checkout state. It adds no invariants beyond
the two scalar wrappers.

### `FilePair`

```python
source_locale: Locale
target_locale: Locale
source_path: RepoPath
target_path: RepoPath
```

The locales must differ and the paths must differ. The record does not infer
locale paths or decide scope, renames, tombstones, or file existence.

### `Diagnostic`

```python
severity: Severity
code: str
message: str
action: str | None = None
path: RepoPath | None = None
line_start: int | None = None
line_end: int | None = None
excerpt: str | None = None
```

`code` and `message` must be non-empty and have no leading or trailing
whitespace. `YELLOW` and `RED` require a non-empty `action`; `GREEN` permits
`None`. `line_start` and `line_end` are either both absent or both present.
When present, they are 1-based, satisfy `1 <= line_start <= line_end`, and
require `path`. A present `excerpt` must be non-empty and also requires `path`.
A job-level diagnostic may omit `path`, line fields, and `excerpt`.

### `JobResult`

```python
job_id: JobId
mode: Mode
publication_decision: PublicationDecision | None
merge_verdict: MergeVerdict
checked_snapshot: SnapshotRef | None
diagnostics: tuple[Diagnostic, ...]
```

`diagnostics` must have exact runtime type `tuple`, and every element must have
exact runtime type `Diagnostic`. `READY` and `WAITING_CI` require a
`checked_snapshot`. A `READY` result forbids `PUBLISH_RED`,
`WITHHOLD_INCOMPLETE`, and `WITHHOLD_UNSAFE`.

Publication and merge readiness are independent. `publication_decision=None`
means that publication policy was not applied or publication was not needed.
It is not an alias for `PUBLISH_NORMAL`. The following combinations are
explicitly representable:

- `PUBLISH_RED + BLOCKED`, with or without a checked snapshot.
- `PUBLISH_NORMAL + WAITING_CI`, with a checked snapshot.
- `PUBLISH_NORMAL + READY`, with a checked snapshot.
- `None + NOT_APPLICABLE`, with or without a checked snapshot.
- `None + READY`, with a checked snapshot, for standalone verification.

Only these combinations are forbidden by T002:

- `READY` or `WAITING_CI` without `checked_snapshot`.
- `PUBLISH_RED + READY`.
- `WITHHOLD_INCOMPLETE + READY`.
- `WITHHOLD_UNSAFE + READY`.

T002 intentionally permits every other type-correct combination. Publication
priority, critic outcomes, CI correlation, no-op handling, and exit status are
policy owned by T021 and T024 and are not inferred by this record.

## Wire schema version 1

`DOMAIN_SCHEMA_VERSION` is `1`. `to_wire(value)` returns an exact envelope:

```json
{"schema_version": 1, "type": "snapshot_ref", "data": {}}
```

The envelope keys are exactly `schema_version`, `type`, and `data`. The public
decoder is `from_wire(expected_type, payload)`. It requires a tag matching the
exact requested domain type. It does not supply defaults, coerce primitives,
normalize case, accept member names or aliases, ignore extra keys, or perform a
best-effort migration.

### All type tags

| Python type | Exact tag | `data` kind |
|---|---|---|
| `Locale` | `locale` | JSON string containing its wire value |
| `Mode` | `mode` | JSON string containing its wire value |
| `ModelRole` | `model_role` | JSON string containing its wire value |
| `Severity` | `severity` | JSON string containing its wire value |
| `PublicationDecision` | `publication_decision` | JSON string containing its wire value |
| `MergeVerdict` | `merge_verdict` | JSON string containing its wire value |
| `JobId` | `job_id` | JSON string containing `value` |
| `RepositoryId` | `repository_id` | JSON string containing `value` |
| `GitSha` | `git_sha` | JSON string containing `value` |
| `ContentHash` | `content_hash` | JSON string containing `value` |
| `RepoPath` | `repo_path` | JSON string containing `value` |
| `SnapshotRef` | `snapshot_ref` | Exact record object below |
| `FilePair` | `file_pair` | Exact record object below |
| `Diagnostic` | `diagnostic` | Exact record object below |
| `JobResult` | `job_result` | Exact record object below |

### Exact record objects

`SnapshotRef.data` has exactly two keys:

```json
{
  "repository": "ydb-platform/ydb",
  "commit_sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
}
```

`FilePair.data` has exactly four keys:

```json
{
  "source_locale": "ru",
  "target_locale": "en",
  "source_path": "ydb/docs/ru/index.md",
  "target_path": "ydb/docs/en/index.md"
}
```

`Diagnostic.data` always has all eight keys. Optional values are explicit JSON
nulls:

```json
{
  "severity": "green",
  "code": "ok",
  "message": "No issues",
  "action": null,
  "path": null,
  "line_start": null,
  "line_end": null,
  "excerpt": null
}
```

`JobResult.data` always has all six keys. `diagnostics` is always a JSON array,
including when empty. Its elements are exact `Diagnostic.data` objects, without
a nested envelope. Optional values are explicit JSON nulls. A complete valid
null-optionals object is:

```json
{
  "job_id": "job-no-publication",
  "mode": "doc_verify",
  "publication_decision": null,
  "merge_verdict": "not_applicable",
  "checked_snapshot": null,
  "diagnostics": []
}
```

When present, `checked_snapshot` is an exact `SnapshotRef.data` object. A
non-empty `diagnostics` value has this shape:

```json
[
  {
    "severity": "red",
    "code": "broken_link",
    "message": "Target is missing",
    "action": "Create the target page",
    "path": "ydb/docs/en/index.md",
    "line_start": 3,
    "line_end": 4,
    "excerpt": "[target](missing.md)"
  }
]
```

### Serialization errors and precedence

The error hierarchy is:

```text
ValueError
└── DomainError
    ├── InvariantViolation
    └── SerializationError
        ├── UnsupportedSchemaVersion
        ├── UnknownDomainType
        └── MalformedPayload
```

Public serialization operations never raise `SerializationError` itself.
Classification and decode precedence are exact:

1. `to_wire` given an unsupported exact Python type, or `from_wire` given an
   unsupported `expected_type`, raises `UnknownDomainType`.
2. A non-object envelope raises `MalformedPayload`.
3. A missing `schema_version`, a value other than `1`, or any value whose exact
   runtime type is not `int`, including `bool`, raises
   `UnsupportedSchemaVersion` at `$.schema_version`.
4. A missing `type` or non-string `type` raises `MalformedPayload` at `$.type`.
5. An unknown string tag or a tag that does not match `expected_type` raises
   `UnknownDomainType` at `$.type`.
6. Missing or extra envelope keys, the wrong `data` kind, missing or extra
   record keys, invalid JSON primitive or optional types, invalid nested
   records, invalid arrays or array elements, unknown enum values, and decoded
   constructor-invariant failures raise `MalformedPayload` at the most specific
   path.

Paths use forms such as `$.data.checked_snapshot.commit_sha` and
`$.data.diagnostics[0].code`. Each message states the expected kind and does
not echo the full payload. A decoded unknown enum value preserves the original
`ValueError` as `MalformedPayload.__cause__`. A decoded scalar or record
invariant failure preserves the original `InvariantViolation` as
`MalformedPayload.__cause__`.

## Ports

The complete T002 port surface consists of five synchronous structural
protocols. Generic unary ports use these exact variables:

```python
RequestT_contra = TypeVar("RequestT_contra", contravariant=True)
ResultT_co = TypeVar("ResultT_co", covariant=True)
```

Every parameter before `/`, including `self`, is positional-only. Parameter
names and annotations, return annotations, and methods are exact:

```python
class SnapshotReader(Protocol):
    def read_bytes(
        self, snapshot: SnapshotRef, path: RepoPath, /
    ) -> bytes | None: ...

class ModelClient(Protocol[RequestT_contra, ResultT_co]):
    def invoke(self, request: RequestT_contra, /) -> ResultT_co: ...

class StateStore(Protocol[RequestT_contra, ResultT_co]):
    def execute(self, request: RequestT_contra, /) -> ResultT_co: ...

class GitHubGateway(Protocol[RequestT_contra, ResultT_co]):
    def execute(self, request: RequestT_contra, /) -> ResultT_co: ...

class Clock(Protocol):
    def now(self, /) -> datetime: ...
```

`SnapshotReader` reads only from the explicit immutable snapshot. `None` means
the path is absent; `b""` means it exists and is empty. It has no branch, HEAD,
worktree, list, mutation, checkout, or fallback operation. `Clock.now()` must
return a timezone-aware UTC `datetime`, meaning
`utcoffset() == timedelta(0)`. Moscow calendar-day and TTL calculations are
outside the port. The generic ports carry immutable request and result types
owned by later tasks. They expose no provider, YDB, GitHub transport, token,
pagination, environment, mutable-worktree, or asynchronous API.

## Ownership boundary

T002 does not define direction results, scope manifests, parser plans or
fields, protected references, model requests, responses or attempts, persistent
jobs, checkpoints or pages, GitHub events, mutations or reports, or a
publication-policy evaluator. It does not define adapter schemas, orchestration,
or environment access. Those types and policies remain with their later owning
tasks.

`domain.py` does not import `ports.py`. `ports.py` depends only on standard
library typing and datetime types plus the T002 `RepoPath` and `SnapshotRef`.
The three contract modules do not import environment, process, filesystem,
network, SDK, adapter, HTTP, Git, GitHub, YDB, or CLI modules.
