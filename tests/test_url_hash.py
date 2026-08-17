"""url_hash helpers."""

from app.ingest.url_hash import hash_url, normalize_url


def test_normalize_url_strips_fragment_and_trailing_slash() -> None:
    assert (
        normalize_url("HTTPS://Example.COM/jobs/1/?x=1#frag")
        == "https://example.com/jobs/1?x=1"
    )


def test_hash_url_stable() -> None:
    a = hash_url("https://example.com/jobs/1")
    b = hash_url("https://example.com/jobs/1/")
    assert a == b
    assert len(a) == 64
