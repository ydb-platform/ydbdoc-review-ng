# Feature flags and conditional warnings

Identify affected features and public issue/implementation references. Record inaccessible feature-task evidence.

3. Cross-check the flag against the reviewed immutable HEAD: its declaration and default in `ydb/core/protos/feature_flags.proto`, and the implementation that gates this feature. Follow the existing evidence gate and code-discovery instructions. For a backport use that branch's code, not main. If implementation is absent from HEAD, check linked or sibling implementation PRs and distinguish an unmerged dependency from an unavailable feature. An unqualified claim that the feature is available in that release is a separate availability finding when absence is proven; an absent flag must not produce a visible experimental warning.
4. A blank or `None` Feature Flag field does not establish that the feature has no flag. Look at implementation PRs and the actual gate. Conversely, a flag named in Tracker does not prove it exists or has the same default in the reviewed release. Keep discrepancies explicit.
5. A failed or inaccessible search is `unverified`, not `no task` or `no flag`. Continue independent code checks and report the remaining gap. For an editorial PR with no gated feature, record the searched feature names and why the check is not applicable.

## Check the canonical warning

Inspect the canonical warning in the available public instructions. If its exact text cannot be verified, record the gap.

Apply the policy's default warning requirement to functionality controlled by a feature flag. Use the stable-capability exemption below only when feature-specific evidence establishes it; absence of an existing warning or uncertainty about maturity does not establish an exemption. Inspect every affected feature article or section in RU and EN, including relevant existing pages outside the PR diff. The required construct is:

```markdown
{% if feature_flags.enable_xxx == false %}
{% note warning "Экспериментальная функциональность" %}

<exact RU text from the policy, with flag and relative paths substituted>

{% endnote %}
{% endif %}
```

Use the policy's English title and text in EN. Verify that both the flag link and `feature_flags.md#experimental` resolve in the reviewed branch. An unconditional warning, an `info` note, a custom paraphrase, a literal default in prose, or a manually edited `presets.yaml` is not a substitute for the conditional snippet.

Check these distinct states:

| Branch default / generated variable | Expected visibility |
| --- | --- |
| `false` | Warning shown |
| `true` | Warning hidden |
| Flag absent | Warning hidden |

When adding or changing a warning, verify these states with actual Diplodoc preprocessing in both languages. For a review-only request, use an isolated fixture or available build artifacts without pushing changes. A successful ordinary build tests only its own variable values; do not claim it verifies all three states. For unchanged existing warnings, check canonical text, condition, links, and the branch preview when available; report which visibility states were actually exercised. Track markup, link resolution, and rendered visibility independently. If preprocessing or a preview is unavailable, distinguish inspected markup from unverified rendered behavior.

The policy explicitly exempts flags for established, stable capabilities that are disabled because not everyone needs them. Apply this exemption only with feature-specific evidence. Do not infer the exemption from a blank Tracker field or a subjective belief that the feature seems stable. This is a documentation-policy requirement, not permission to invent additional runtime risks beyond the approved template. For an exempt feature, check its ordinary prerequisite/configuration documentation instead. A true or absent flag must not produce a visible experimental warning merely because main used to have a false default.
