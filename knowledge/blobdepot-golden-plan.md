# P2 stub: BlobDepot golden harness

Offline golden for the CI translation redesign. Not implemented in the P0+P1
pass; land after the gate/atoms/prompts are green on main.

## Goal

Freeze a small RU BlobDepot article (+ optional old EN) and assert deterministic
prep/restore/presentation outcomes without live models:

- `BS_CONTROLLER` / `CREATE_FAILED` / `POOL_NAME` remain identifier atoms
- no `page_CONTROLLER` / split-underscore prose after prepare+restore
- when old EN has backticks, apply_presentation_map wraps matching atoms
- when old EN is absent, apply is a no-op and critic prompt still requires
  literal presentation normalization

## Suggested layout

```
tests/golden/blobdepot/
  source.ru.md
  old.en.md          # optional presentation reference
  expectations.json  # atoms, forbidden fragments, presentation wraps
```

Driver: unit test that runs `prepare_document` → restore → presentation map
only (no model). Optionally a scripted critic/arbiter fixture later.

## Exit criteria

- Golden test in non-live suite
- Documented in `debt-map.md` as done
- Then live `doc_translate` re-proof on a known source PR
