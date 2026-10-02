# P2: BlobDepot golden harness

Offline golden for the CI translation redesign. Locks the #50839 BlobDepot
regression without live GitHub or DeepSeek.

## Goal

Freeze a small RU BlobDepot article (+ old EN presentation reference) and
assert deterministic prep/restore/presentation/gate outcomes:

- `BS_CONTROLLER` / `CREATE_FAILED` / `POOL_NAME` / `CREATED_FAILED` remain
  identifier atoms across `\_`
- no `page_CONTROLLER` / split-underscore prose after prepare+restore
- when old EN has backticks, `apply_presentation_map` wraps matching atoms
- when old EN is absent, apply is a no-op (critic still owns literal
  presentation for bare CLI flags / short states)
- critic unavailable after retry → RED, not arbiter GREEN on raw draft

## Layout

```
tests/golden/blobdepot/
  source.ru.md         # focused RU excerpt from #50839
  old.en.md            # presentation reference (backticks on atoms)
  expectations.json    # atoms, forbidden fragments, wraps
tests/golden/test_blobdepot_golden.py
```

Driver runs `prepare_document` → restore → presentation map, plus a scripted
`review_pr` critic-unavailable path. No network, no real model.

## Exit criteria

- [x] Golden test in non-live suite
- [x] Documented in `debt-map.md` as done
- [ ] Live `doc_translate` re-proof on a known source PR (P2 offline does not
      replace this; remaining live risk: model quality + credentials)
