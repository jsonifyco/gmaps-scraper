"""Shared test fixtures for gmaps-scraper-bro."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
ROOT_DIR = TESTS_DIR.parent
EXAMPLES_DIR = ROOT_DIR / "examples"


@pytest.fixture
def examples_dir() -> Path:
    return EXAMPLES_DIR


@pytest.fixture
def sample_input_dict() -> dict:
    return {
        "searchStringsArray": ["cafe"],
        "locationQuery": "Mitte, Berlin, Germany",
        "maxCrawledPlacesPerSearch": 10,
        "language": "en",
        "scrapePlaceDetailPage": True,
        "maxReviews": 5,
        "maxImages": 5,
        "bro": {
            "concurrency": 1,
            "coverage": "standard",
            "reviewsMode": "fast",
        },
    }
