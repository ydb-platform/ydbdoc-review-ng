# Documentation review composite action

The action installs `.[runtime]` on Python 3.11 and selects the shipped
`YDBDOC_RUNTIME_FACTORY=ydbdoc_review_ng.runtime:create_runtime`. Version 1.1.0
implements all three modes. Local `doc_translate.yml` and `doc_verify.yml` are
dispatch smoke/templates; `ci.yml` is separate offline CI. The consumer label
workflow is a [deployment template](../../../docs/examples/doc_continue.yml).

| Input | Required for | Meaning |
|---|---|---|
| `mode` | All | Exactly translate, verify or continue. |
| `pr` | All | Source PR for translate, translation PR for verify, either for continue. |
| `source-sha` | Translate, verify | Immutable authoritative source SHA; forbidden for continue. |
| `target-sha` | Verify | Exact current translation head SHA; forbidden otherwise. |
| `budget-rub` | Translate | Nonnegative finite daily budget in RUB; forbidden otherwise. |

Input metadata cannot express conditional requirements: `source-sha` is optional
there, but the first shell step enforces the table before Python setup or install.
Continue invokes exactly `continue --pr N`. Checkpoint SHA values are never inputs.
Final RED returns exit 1 in every mode.

Inputs use environment variables and quoted shell arguments. Secrets stay in
environment references. Workflows forward the model/YDB/project-token secrets
and variables listed in [deployment](../../../docs/deployment.md). They request
contents/PR write and checks read, checkout only the trusted tool with
`persist-credentials: false`, and never checkout source PR code.

Setup/install needs Python/package-index access unless supplied locally. No
separate factory distribution is missing. Credentials, YDB schema provisioning,
permissions in `ydb-platform/ydb`, enabling workflows and attaching checks to
the actual target head are separately authorized deployment operations.

Offline installed smoke runs the shipped composition through all three CLI modes
with only remote I/O replaced. See deployment for the exact smoke files.
This action does not perform cutover or trigger `build-docs` itself.

For continuation, an allowed author posts `/ydbdoc continue` on its own first line
and nonempty context below it before an allowed actor applies `doc_continue`.
The runtime creates audit, then validates the actual label actor and latest eligible
comment from GitHub. It resumes only a live semantic checkpoint, reuses accepted
maps and the saved immutable source/base, and rejects a moved translation head.
The consumer must serialize runs by repository and PR and execute the action from
a reviewed immutable commit without checking out PR code. Deploy that workflow
only after the release gate and explicit schema migration described in deployment.
