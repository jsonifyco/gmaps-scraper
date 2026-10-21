"""Tests for Apify and Native dataset export transformations."""

from __future__ import annotations

from gmaps_scraper.export.apify import apify_forms, apify_review, registrable_domain


def test_registrable_domain():
    assert registrable_domain("example.com") == "example.com"
    assert registrable_domain("sub.domain.co.uk") == "domain.co.uk"
    assert registrable_domain(None) is None


def test_apify_forms_omits_empty():
    item = {
        "title": "Bakery",
        "additionalInfo": {},
        "additionalOpeningHours": [],
        "phone": None,
        "website": "https://example.com",
    }
    cleaned = apify_forms(item, details=True)
    assert "additionalInfo" not in cleaned
    assert "additionalOpeningHours" not in cleaned
    assert cleaned["phone"] == ""
    assert cleaned["website"] == "https://example.com"


def test_apify_review_mapping():
    raw_review = {
        "reviewId": "rev123",
        "name": "Alice",
        "text": "Great place!",
        "publishedAtDate": "2026-01-01T12:00:00.000Z",
        "lastEditedAtDate": "2026-01-02T15:00:00.000Z",
        "stars": 5,
        "reviewOrigin": "Google",
    }
    transformed = apify_review(raw_review)
    assert transformed["reviewId"] == "rev123"
    assert transformed["name"] == "Alice"
    assert transformed["publishedAtDate"] == "2026-01-02T15:00:00.000Z"
    assert "lastEditedAtDate" not in transformed
