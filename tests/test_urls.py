import pytest

from indexkit.urls import InvalidURLError, URLPolicy, batches, clean, normalize


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("HTTPS://WWW.Example.COM", "https://www.example.com/"),
        ("https://www.example.com:443/a", "https://www.example.com/a"),
        ("http://www.example.com:80/a", "http://www.example.com/a"),
        ("https://www.example.com:8443/a", "https://www.example.com:8443/a"),
        ("https://www.example.com/a#section", "https://www.example.com/a"),
        ("https://www.example.com/a b", "https://www.example.com/a%20b"),
        ("https://www.example.com/a%20b", "https://www.example.com/a%20b"),
        ("https://www.example.com/?b=2&a=1", "https://www.example.com/?b=2&a=1"),  # query order kept
        ("https://bücher.example/", "https://xn--bcher-kva.example/"),
        ("  https://www.example.com/x  ", "https://www.example.com/x"),
    ],
)
def test_normalize(raw: str, expected: str) -> None:
    assert normalize(raw) == expected


@pytest.mark.parametrize("raw", ["ftp://example.com/x", "/relative/path", "https://", "https://user:pass@example.com/"])
def test_normalize_rejects(raw: str) -> None:
    with pytest.raises(InvalidURLError):
        normalize(raw)


def test_tracking_parameters_removed_only_on_request() -> None:
    url = "https://www.example.com/p?utm_source=x&id=7&gclid=abc"
    assert normalize(url) == url
    assert normalize(url, strip_tracking=True) == "https://www.example.com/p?id=7"


def test_clean_filters_and_deduplicates_in_order() -> None:
    policy = URLPolicy.for_hosts(["www.example.com"], exclude=[r"/private/"])
    accepted, rejected = clean(
        [
            "https://www.example.com/b",
            "https://WWW.EXAMPLE.COM/b#top",  # duplicate after normalisation
            "https://www.example.com/a",
            "https://evil.example.net/a",
            "https://www.example.com/private/x",
            "mailto:someone@example.com",
            "https://www.example.com/" + "x" * 2100,
        ],
        policy,
    )
    assert accepted == ["https://www.example.com/b", "https://www.example.com/a"]
    reasons = [r.reason for r in rejected]
    assert reasons[0] == "duplicate"
    assert "not in the allowed hosts" in reasons[1]
    assert "exclude" in reasons[2]
    assert "unsupported scheme" in reasons[3]
    assert "longer than 2048" in reasons[4]


def test_include_patterns() -> None:
    policy = URLPolicy.for_hosts(["www.example.com"], include=[r"/jobs/"])
    accepted, rejected = clean(["https://www.example.com/jobs/1", "https://www.example.com/blog/1"], policy)
    assert accepted == ["https://www.example.com/jobs/1"]
    assert rejected[0].reason == "does not match any --include pattern"


def test_batches() -> None:
    assert list(batches(list("abcde"), 2)) == [["a", "b"], ["c", "d"], ["e"]]
    with pytest.raises(ValueError):
        list(batches(["a"], 0))
