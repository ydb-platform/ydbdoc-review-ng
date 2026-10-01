# Third runtime replay (fix verification)

Дата: 2026-10-01. Base: `0477139`. After fixes: see `git log -1` on `public/main`.

## Witness results after fixes

| Residual | Scenario | Result |
|---|---|---|
| 1 | partial A ok / B missing + critic 503 → RED; continue | RED then continue GREEN; roles critic/arbiter |
| 2 | PNG-only + arbiter `finish_reason=length` | RED QA; checkpoint open; not GREEN |
| 3 | critic `# Corrected\nText\n` spacing | published branch `# Corrected\nText\n`; GREEN path with 2 commits |
| 4 | table 2→3 soft-publish RED; continue | continue GREEN; models ran |
| 5 | malformed YAML frontmatter validate_plan | no raise (unit) |
| 6 | TOC title delta + empty strings + critic 503 | RED; `target_sha=null`; TOC in `review_paths` |
| 7 | delete `API` group with Extra child | Extra preserved for both `API` and `Target API` titles |
| 8 | empty target TOC + Parent/New | string_ids include Parent and New |
| 9 | 960-char clean EN; noncontinuous RU fragments | ~0.0003s; no false echo |

## Method

Public `ContinueServices` / `doc_translate` / `doc_continue` with transport fakes only.
Plus unit harnesses for TOC delta and `validate_translated_prose`.

## Status

Residuals **1–9 CLOSED**. Prior PARTIAL findings W3/W4/W6a/W6b/W10/W15/W20 from the
discovery matrix are addressed. Soft-publish / diagnostics≠gate and continue for
`target_sha=null` / missing soft-publish targets hold on these witnesses.
