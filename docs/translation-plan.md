# Translation plan contract

## Why the plan exists

The source pull-request inventory is the boundary of the translation job. Every
inventory row must receive one explicit disposition before a model is called or
a candidate is published. Filtering the inventory by extension is forbidden:
an unhandled localized file is an error, not an implicit no-op.

The plan has two different concepts which must not be conflated:

- an intended operation, such as translating a document or synchronizing a
  TOC;
- an execution result, which may be a file mutation or a proven semantic no-op.

The absence of a target file from the final Git diff is not proof that an input
was forgotten. It is valid only when the executor records why the requested
state was already satisfied.

## Required phases

1. Freeze the complete PR inventory and the immutable source base/head
   snapshots.
2. Classify every path as Markdown, TOC, redirects, asset, other localized
   metadata, target-side counterpart, or outside the locale trees.
3. Select a direction from the classified inventory. A one-locale metadata-only
   PR has a deterministic direction and must not depend on a Markdown model
   request.
4. Build an immutable plan. Every input has exactly one action, target path and
   reason. Renames contain both the old and new target paths.
5. Expand implicit dependencies (linked Markdown, TOCs, redirects and assets)
   as separate plan entries with provenance back to the input which introduced
   them.
6. Execute only plan entries. Execution returns a mutation or a checked no-op
   for every entry.
7. Reconcile the execution result against the plan, then build one closed full
   PRReviewContext with complete inventory, source/candidate text files including
   index/TOC, allowed paths/operations, exact snapshot/candidate digests, both
   complete pinned RU/EN glossaries and technical validation data. One editor
   returns all editable target text paths as complete files. Apply atomically,
   reconcile/validate and send the complete final candidate to one independent
   full PR arbiter. RED findings go directly to the report and are never
   automatically repaired or passed onward. Build/CI do not participate in
   the semantic verdict.
8. Persist and hash the canonical plan. `doc_continue` and `doc_verify` replay
   that exact plan; they do not rediscover scope using the same mutable rules.

## File policy matrix

| Source change | Required planned behavior |
|---|---|
| Markdown add/modify | Translate the complete file; synchronize its navigation dependencies. |
| Markdown delete | Delete the target file and remove its target TOC references. |
| Markdown rename | Move/delete the old target, translate when content changed, update TOCs, and add a target redirect when policy requires it. |
| Source tombstone/redirect | Delete or retire the old target document and synchronize the equivalent target redirect; preserving an orphan article is not an implicit no-op. |
| TOC change | Give the model the complete source-head TOC with YAML structure protected. Compute the source base-to-head YAML delta only for planning and deterministic merge, then take changed prose translations from the complete-file result. A target which already satisfies every structural and prose postcondition records a checked no-op. |
| Redirects change | Apply and validate the source base-to-head redirect delta in the target locale. |
| Asset change | Record an explicit policy outcome: copy, preserve a localized counterpart, delete, or reject. Existing target bytes must never make the input disappear silently. |
| Both locale counterparts changed | Record an explicit complete-pair/no-translation disposition and validate collisions. |
| Outside locale trees | Record an ignored disposition with the path and reason. |
| Unknown localized file | Fail closed before model calls. |

## TOC correctness

TOCs are structured localized documents, not incidental metadata of a Markdown
article. Correct synchronization needs the source PR base and head. Comparing
only the current source TOC with the current target is insufficient because the
two locales may legitimately contain different pre-existing entries.

For each changed TOC, the planner must identify additions, removals, moves,
renames, `include` changes and prose-scalar changes. `href`, `include`, mapping
shape and ordering are structural data. Human-readable labels are translated
prose. A label must not be replaced with the target article H1 merely because it
contains Cyrillic: navigation labels may intentionally differ from article
titles.

For source PR `#50839`, the RU delta adds two entries. The current EN TOC already
contains both hrefs, but href equality proves only structural idempotency. The
label `BlobDepot decommit` must still be compared with the whole-file model
translation of `Декомиссия BlobDepot`. A mismatch is a target TOC mutation; only
equality of both structure and translated prose is a checked no-op. Replacing a
navigation label with the target article H1 is not a valid general proof because
navigation labels and article titles may intentionally differ.

## Publication invariants

- Inventory coverage is exactly 100%; no localized input is silently dropped.
- Every plan entry has exactly one terminal result: mutation or checked no-op.
- Candidate mutations are a subset of planned outputs.
- Every required planned mutation is present in the candidate.
- Deletions and old rename paths are checked as outputs, not only created files.
- A semantic GREEN is impossible if plan reconciliation failed.
- `doc_verify` independently checks the persisted plan against the published
  candidate and pinned snapshots.

## Current implementation boundary

The full PR review replacement approved on 2026-09-30 is not implemented or
provider-validated at baseline `2a1c268`. Whole-file/source-only translation
remains. Source deltas only plan operations; old target prose is not a model
source for translation or repair. Complete inventory includes deletions, renames
and binary manifests/digests; these are not editable text-map outputs.

`translation_plan.py` is the fail-closed inventory boundary. Classification
preflight runs before direction/model calls. After direction selection, the
inventory intent is classified before metadata planning; exact metadata
postconditions are then frozen before document-model calls. Fixed/final
candidate reconciliation requires the concrete TOC/delete/rename/document
outputs claimed by the completed plan. Unsupported localized inputs and
unsupported statuses stop the job rather than publish an incomplete translation.

For the currently supported TOC migration path, metadata planning freezes the
SHA-256 of the complete expected target TOC before document-model calls. It
also reads the pinned source PR base and head and proves that the complete YAML
delta consists only of byte-preserving appended `name + href` Markdown entries. Every
added href must resolve to a document already present in the selected plan.
Edits, removals, moves, include/group changes, comments-only changes and an
unplanned second entry fail closed. Both fixed-output and final-candidate
reconciliation require the exact target digest; a non-empty but stale or
unrelated TOC cannot satisfy the plan. The source base/head digests and target
postcondition are part of the canonical plan hash checked by `doc_continue`.
PR #50839 has a full runtime golden built from the recorded source base/head
and complete EN file and proves that target-only EN navigation is preserved
while `BlobDepot decommit` becomes `Group Decommissioning`. This historical
golden proves planner/output shape, not semantic correctness of that label.

This is still a deliberately narrow supported subset. Markdown lifecycle and
the target-H1-backed TOC entry-addition migration associated with selected
Markdown are executable. Direct asset/redirect work, metadata-only direction,
general TOC prose translation and TOC edit/remove/rename/copy remain
fail-closed. They must be
implemented with source-base snapshots and per-entry results before support is
advertised. The legacy TOC title repair still uses an established target H1 and
is therefore limited to the current supported migration cases; the complete-file
TOC prose contract above is the required replacement for general support.

## Approved editor/reconciliation boundary

Editor output covers exactly all allowed editable text paths as complete files.
Missing, unknown, duplicate, malformed, partial, binary, deletion, traversal and
out-of-scope entries fail closed. Applying the whole map is atomic in memory.
Source-owned technical values and deterministic operation constraints remain
mandatory. TOC name corrections are limited to explicitly planned text changes:
structure, href, order and valid target-only navigation stay unchanged.
A planner-owned API must recompute the validated artifact digest while preserving
the original plan and correction provenance. Dropping the expected digest or
allowing arbitrary YAML overwrite is forbidden; unsupported corrections stop
with a typed blocked correction/RED.

The arbiter sees the complete final candidate with full glossary after runtime
checks and applies critic corrections; GREEN requires valid full output, empty
findings, complete nonempty coverage and matching digest. Empty responses,
missing arbiter, editor changes or a checked-noop plan do not imply GREEN.
Review checks cross-file terminology/entity identity without requiring an exact
glossary mapping, links/anchors, H1/index/TOC and complete PR operations.
Valid target-only navigation means index and TOC path sets need not be equal.

Review context is indivisible. Verified input/context/output admission reserves
the exact serialized request and complete-file response; unknown capability or
oversize stops before publication without lossy fallback or partial GREEN.
The pinned witness glossaries alone contain 225432 UTF-8 bytes, so historical
48000-character limits and excerpt probes are insufficient.
Continue translates pending documents only, but every semantic review includes
the full PR/glossary. Checkpoints bind a new contract version plus
source/candidate/glossary manifests/digests; incompatible chunk-review states
fail closed. Real-provider full-context probe and independent content review
are required before claiming semantic acceptance.

## Independent review of the legacy flow

The following findings are ordered by correctness risk, not by implementation
effort.

1. **Blocker — inventory filtering.** `prepare_source()` turns only changed
   Markdown paths into scope inputs. Every other file kind used to disappear
   before direction selection and publication.
2. **Resolved for the supported subset — source-delta snapshot.** The runtime
   pins and reads the source PR base and head. The plan accepts only provable
   simple entry additions and hashes both snapshots. Unsupported delta shapes
   stop before model calls.
3. **Blocker — metadata is a side effect.** TOC and redirect edits are emitted
   while iterating selected Markdown entries. A TOC-only or redirects-only PR
   has no document entry which can trigger that code.
4. **Blocker — path-level coverage is too weak.** Seeing one generated change
   for a target TOC path does not prove that every source TOC delta was applied;
   an unrelated entry edit can mask a missed removal, reorder, include or label
   change in the same file.
5. **Blocker — deletion is incomplete.** `DELETE_TARGET` deletes the Markdown
   file, but metadata production is skipped for that operation. A target TOC
   reference can remain stale. A source tombstone currently wins over missing
   source handling and may preserve an old target article instead of retiring
   it and synchronizing a redirect.
6. **High — assets are discovery-only.** Assets are found only through selected
   Markdown links and are copied only when the target asset is absent. Direct
   asset changes, deletions, renames and changed bytes with an existing target
   have no explicit policy.
7. **High — TOC labels use an unsafe proxy.** The legacy repair takes the target
   article H1 when a changed source navigation label contains Cyrillic. A
   navigation label is allowed to differ from the article title, so this can
   silently undo an intentional label change.
8. **High — verify shares discovery blind spots.** `doc_verify` reconstructs
   scope with the same rules. It can confirm consistency with the generator
   without proving that the original PR inventory was completely handled.
9. **Resolved — continuation plan identity.** Checkpoints bind the canonical
   cross-file plan hash, including source TOC base/head and target
   postconditions. Continue reconstructs the plan from pinned inputs and rejects
   any mismatch before model calls.
10. **Medium — no user-visible plan.** Reports do not show which source files
   translated, deleted, synchronized, proved no-op, or were rejected. This made
   a correct no-op and a forgotten file indistinguishable to a reviewer.

The current boundary closes silent filtering, old/new rename classification,
status/action mismatch, target collision detection and missing terminal output
for the supported subset. The remaining findings stay explicit fail-closed
implementation work; no GREEN may reinterpret an unsupported case as a no-op.
