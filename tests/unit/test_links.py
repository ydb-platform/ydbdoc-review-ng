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
