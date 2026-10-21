"""Tests for input_model validation and schema parsing."""

from __future__ import annotations

import pytest

from gmaps_scraper.input_model import InputError, parse_input


def test_valid_minimal_input(sample_input_dict: dict):
    run = parse_input(sample_input_dict)
    assert run.search_terms == ["cafe"]
    assert run.location_mode == "query"
    assert run.details_enabled is True
    assert run.reviews_limit == 5
    assert run.images_limit == 5
    assert run.bro.concurrency == 1


def test_excluded_addons_rejected():
    with pytest.raises(InputError) as exc_info:
        parse_input({"searchStringsArray": ["pizza"], "verifyLeadsEnrichmentEmails": True})
    assert any("email verification" in p for p in exc_info.value.problems)

    with pytest.raises(InputError) as exc_info:
        parse_input({"searchStringsArray": ["pizza"], "maximumLeadsEnrichmentRecords": 10})
    assert any("business leads enrichment" in p for p in exc_info.value.problems)

    with pytest.raises(InputError) as exc_info:
        parse_input({"searchStringsArray": ["pizza"], "scrapeSocialMediaProfiles": {"instagram": True}})
    assert any("social media profile enrichment" in p for p in exc_info.value.problems)

    with pytest.raises(InputError) as exc_info:
        parse_input({"searchStringsArray": ["pizza"], "enableCompetitorAnalysis": True})
    assert any("AI competitor analysis" in p for p in exc_info.value.problems)


def test_start_urls_priority():
    raw = {
        "startUrls": [{"url": "https://www.google.com/maps/place/Berlin"}],
        "searchStringsArray": ["coffee"],
        "locationQuery": "Munich",
    }
    run = parse_input(raw)
    assert len(run.start_urls) == 1
    assert any("startUrls/placeIds take priority" in w for w in run.warnings)



def test_place_ids_priority():
    raw = {
        "placeIds": ["ChIJAVkDP2xquEcRcDteW0YgIQQ"],
        "searchStringsArray": ["coffee"],
    }
    run = parse_input(raw)
    assert run.direct_place_ids == ["ChIJAVkDP2xquEcRcDteW0YgIQQ"]

