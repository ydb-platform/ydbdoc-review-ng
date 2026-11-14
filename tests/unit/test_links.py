from __future__ import annotations

from typing import Any


def test_internal_ydb_link_changes_only_documentation_locale() -> None:
    from ydbdoc_review_ng.links import LinkDestinationResolver

    resolver = LinkDestinationResolver("en", "ru")

    assert resolver(
        "https://ydb.tech/docs/en/analyst/olap_quickstart?version=v26.3"
    ) == "https://ydb.tech/docs/ru/analyst/olap_quickstart?version=v26.3"
    assert resolver("/docs/en/reference/ydb-cli/#anchor") == (
        "/docs/ru/reference/ydb-cli/#anchor"
    )
    assert resolver("../dev/optimization/hints.md") == (
        "../dev/optimization/hints.md"
    )


def test_wikipedia_uses_official_language_link_when_available() -> None:
    from ydbdoc_review_ng.links import LinkDestinationResolver, WikipediaLanglinks

    requested: list[str] = []

    def fetch(url: str) -> Any:
        requested.append(url)
        return {
            "query": {
                "pages": [
                    {
                        "langlinks": [
                            {
                                "lang": "en",
                                "url": "https://en.wikipedia.org/wiki/Distributed_computing",
                            }
                        ]
                    }
                ]
            }
        }

    resolver = LinkDestinationResolver("ru", "en", WikipediaLanglinks(fetch))

    assert resolver(
        "https://ru.wikipedia.org/wiki/Распределённые_вычисления"
    ) == "https://en.wikipedia.org/wiki/Distributed_computing"
    assert len(requested) == 1
    assert "prop=langlinks" in requested[0]
    assert "lllang=en" in requested[0]
    assert "redirects=1" in requested[0]


def test_wikipedia_missing_translation_or_api_failure_keeps_source_url() -> None:
    from ydbdoc_review_ng.links import LinkDestinationResolver, WikipediaLanglinks

    source = "https://ru.wikipedia.org/wiki/Редкая_статья"
    missing = LinkDestinationResolver(
        "ru",
        "en",
        WikipediaLanglinks(lambda _url: {"query": {"pages": [{}]}}),
    )

    def fail(_url: str) -> Any:
        raise OSError("offline")

    unavailable = LinkDestinationResolver("ru", "en", WikipediaLanglinks(fail))

    assert missing(source) == source
    assert unavailable(source) == source
    assert unavailable.allows(
        source,
        "https://en.wikipedia.org/wiki/Rare_article",
    )


def test_wikipedia_fragment_and_other_external_links_are_not_rewritten() -> None:
    from ydbdoc_review_ng.links import LinkDestinationResolver, WikipediaLanglinks

    calls = 0

    def fetch(_url: str) -> Any:
        nonlocal calls
        calls += 1
        return {}

    resolver = LinkDestinationResolver("ru", "en", WikipediaLanglinks(fetch))

    assert resolver("https://ru.wikipedia.org/wiki/YDB#Раздел") == (
        "https://ru.wikipedia.org/wiki/YDB#Раздел"
    )
    assert resolver("https://github.com/ydb-platform/ydb") == (
        "https://github.com/ydb-platform/ydb"
    )
    assert calls == 0


def test_only_exact_internal_ydb_locale_rewrite_is_allowed() -> None:
    from ydbdoc_review_ng.links import LinkDestinationResolver

    resolver = LinkDestinationResolver("ru", "en")
    source = "https://ydb.tech/docs/ru/analyst/olap_quickstart?version=v26.3"

    assert resolver.allows(
        source,
        "https://ydb.tech/docs/en/analyst/olap_quickstart?version=v26.3",
    )
    assert not resolver.allows(
        source,
        "https://ydb.tech/docs/en/analyst/different_page?version=v26.3",
    )


def test_existing_target_anchor_override_keeps_path_and_query_contract() -> None:
    from ydbdoc_review_ng.links import LinkDestinationResolver

    source = "../configuration/dynamic-config.md#obnovlenie-dinamicheskoj-konfiguracii"
    target = "../configuration/dynamic-config.md#updating-dynamic-configuration"
    resolver = LinkDestinationResolver("ru", "en", overrides={source: target})

    assert resolver(source) == target
    assert resolver.allows(source, target)
    assert not resolver.allows(source, "../configuration/other.md#updating")


def test_existing_target_overrides_are_inferred_only_for_same_internal_page() -> None:
    from ydbdoc_review_ng.links import existing_target_link_overrides

    source = b"[one](../glossary.md#stroki) and [two](https://example.com/#ru)"
    target = b"[one](../glossary.md#row-oriented-tables) and [two](https://example.com/#en)"

    assert existing_target_link_overrides(source, target) == {
        "../glossary.md#stroki": "../glossary.md#row-oriented-tables"
    }


def test_existing_target_override_copies_only_fragment() -> None:
    from ydbdoc_review_ng.links import (
        LinkDestinationResolver,
        existing_target_link_overrides,
    )

    source = b"[x](https://ydb.tech/docs/ru/page?version=1#istochnik)"
    target = b"[x](http://www.ydb.tech/docs/en/page?version=1#target)"

    overrides = existing_target_link_overrides(source, target)
    assert overrides == {
        "https://ydb.tech/docs/ru/page?version=1#istochnik": (
            "https://ydb.tech/docs/ru/page?version=1#target"
        )
    }
    resolver = LinkDestinationResolver("ru", "en", overrides=overrides)
    assert resolver("https://ydb.tech/docs/ru/page?version=1#istochnik") == (
        "https://ydb.tech/docs/en/page?version=1#target"
    )


def test_missing_fragment_uses_unique_singular_plural_target_anchor() -> None:
    from ydbdoc_review_ng.links import closest_target_anchor

    assert closest_target_anchor(
        "row-oriented-table", {"row-oriented-tables", "column-oriented-tables"}
    ) == "row-oriented-tables"
    assert closest_target_anchor("operator", {"database", "topic"}) is None


def test_paired_heading_maps_only_inside_unambiguous_anchor_landmarks() -> None:
    from ydbdoc_review_ng.links import paired_target_anchor

    source = (
        "# Свойства JDBC-драйвера {#properties}\n\n"
        "## Примеры JDBC URL {#jdbc-url}\n"
        "## Конец {#end}\n"
    ).encode()
    target = (
        b"# JDBC Driver Properties {#properties}\n\n"
        b"## JDBC URL Examples {#jdbc-url-examples}\n"
        b"## End {#end}\n"
    )

    assert paired_target_anchor("jdbc-url", source, target) == "jdbc-url-examples"


def test_paired_heading_rejects_reordered_siblings() -> None:
    from ydbdoc_review_ng.links import paired_target_anchor

    source = b"# Page {#page}\n## JDBC {#jdbc}\n## Other {#other}\n## End {#end}\n"
    target = b"# Page {#page}\n## Other {#other}\n## JDBC EN {#jdbc-en}\n## End {#end}\n"

    assert paired_target_anchor("jdbc", source, target) is None


def test_paired_heading_rejects_reordered_translated_siblings() -> None:
    from ydbdoc_review_ng.links import paired_target_anchor

    source = b"# Root {#root}\n## A {#a-ru}\n## B {#b-ru}\n## End {#end}\n"
    target = b"# Root {#root}\n## B {#b-en}\n## A {#a-en}\n## End {#end}\n"

    assert paired_target_anchor("a-ru", source, target) is None


def test_reference_definition_destinations_are_localized_and_validated() -> None:
    from ydbdoc_review_ng.links import (
        LinkDestinationResolver,
        localize_markdown_link_destinations,
        markdown_link_destinations_are_localized,
    )

    source = b"See [JDBC][properties].\n\n[properties]: properties.md#jdbc-url\n"
    expected = b"See [JDBC][properties].\n\n[properties]: properties.md#jdbc-url-examples\n"
    resolver = LinkDestinationResolver(
        "ru",
        "en",
        overrides={"properties.md#jdbc-url": "properties.md#jdbc-url-examples"},
    )

    assert localize_markdown_link_destinations(source, resolver) == expected
    assert markdown_link_destinations_are_localized(source, expected, resolver)
    assert not markdown_link_destinations_are_localized(source, source, resolver)


def test_destinations_inside_code_are_not_localized_or_validated() -> None:
    from ydbdoc_review_ng.links import (
        LinkDestinationResolver,
        localize_markdown_link_destinations,
        markdown_link_destinations_are_localized,
    )

    source = (
        b"```md\n[properties]: missing.md#jdbc-url\n"
        b"<https://ydb.tech/docs/ru/missing#jdbc-url>\n```\n\n"
        b"`[inline](missing.md#jdbc-url)`\n"
    )
    resolver = LinkDestinationResolver(
        "ru",
        "en",
        invalid_sources=frozenset(
            {
                "missing.md#jdbc-url",
                "https://ydb.tech/docs/ru/missing#jdbc-url",
            }
        ),
    )

    assert localize_markdown_link_destinations(source, resolver) == source
    assert markdown_link_destinations_are_localized(source, source, resolver)


def test_destinations_inside_blockquote_code_are_opaque() -> None:
    from ydbdoc_review_ng.links import markdown_link_destinations

    source = (
        b"> ~~~md\n"
        b"> [fenced](page.md#section)\n"
        b"> ~~~\n\n"
        b">     [indented](page.md#section)\n"
    )

    assert markdown_link_destinations(source) == ()


def test_localized_destination_collision_cannot_hide_lost_link() -> None:
    from ydbdoc_review_ng.links import (
        LinkDestinationResolver,
        markdown_link_destinations_are_localized,
    )

    source = b"[first](page.md#x) and [second](page.md#y)\n"
    lost_first = b"[second](page.md#y)\n"
    resolver = LinkDestinationResolver(
        "ru",
        "en",
        overrides={"page.md#x": "page.md#y"},
    )

    assert not markdown_link_destinations_are_localized(source, lost_first, resolver)
