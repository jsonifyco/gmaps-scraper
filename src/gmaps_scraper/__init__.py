"""Google Maps scraper driven by the bro stealth browser.

Collects places, reviews, images and website contacts from Google Maps
through the public ``bro-api-sdk`` and keeps all state in SQLite, so a run
can be resumed and re-exported without collecting again.  Input and the
``apify`` export follow the public contract of compass/crawler-google-places
build 0.14.761 (see ``contracts/apify-0.14.761``).
"""

__version__ = "1.0.0"

#: the Apify build whose input/output contract this project reproduces
APIFY_CONTRACT_BUILD = "0.14.761"
