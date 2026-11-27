The public [YDB PR 60931](https://github.com/ydb-platform/ydb/pull/60931) fixture contains exact HEAD text and reversible changed lines for 18 files at `28662ec25a9e7c08ea4ed2f2fcf57b4cb16d221f`. The offline test verifies Git blob IDs and reconstructs the diff. Historical file bodies stay in the test fixture.

The four files in writing-policy come from [the approved writing-policy revision](https://github.com/ydb-platform/ydb/tree/a3c9e632e194a3a9f65bed5b70d2d6734ccbeaba/ydb/docs/.ruler). The manifest identifies the exact bytes. Runtime reads policy from the configured revision using GitHub.
