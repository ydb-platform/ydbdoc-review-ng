# Documentation review composite action

The action installs `.[runtime]` on Python 3.11 and selects the shipped
`YDBDOC_RUNTIME_FACTORY=ydbdoc_review_ng.runtime:create_runtime`. Both
`doc_translate.yml` and `doc_verify.yml` use it; `ci.yml` is separate offline CI.

| Input | Required for | Meaning |
|---|---|---|
| `mode` | Both | Exactly translate or verify. |
| `pr` | Both | Source PR for translate, translation PR for verify. |
| `source-sha` | Both | Immutable authoritative source SHA. |
| `target-sha` | Verify | Exact current translation head SHA. |
| `budget-rub` | Translate | Nonnegative finite daily budget in RUB. |

Inputs use environment variables and quoted shell arguments. Secrets stay in
environment references. Workflows forward the model/YDB/project-token secrets
and variables listed in [deployment](../../../docs/deployment.md). They request
contents/PR write and checks read, checkout only the trusted tool with
`persist-credentials: false`, and never checkout source PR code.

Setup/install needs Python/package-index access unless supplied locally. No
separate factory distribution is missing. Credentials, YDB schema provisioning,
permissions in `ydb-platform/ydb`, enabling workflows and attaching checks to
the actual target head are separately authorized deployment operations.

Offline installed smoke runs the shipped composition through both CLI modes
with only remote I/O replaced. See deployment for the exact smoke files.
This action does not perform cutover or trigger `build-docs` itself.
