"""Contact extraction from website pages (pure functions, no I/O).

Every value keeps the URL of the page it came from.  Nothing is generated:
an email must appear on a page (plain, ``mailto:`` or the common ``[at]``
obfuscation), a phone must come from a ``tel:`` link or sit next to a phone
label; other phone-shaped numbers are reported separately as uncertain.
"""

from __future__ import annotations

import re
import unicodedata
import urllib.parse as up
from typing import Any

EMAIL_FULL = re.compile(r"[a-z0-9][a-z0-9._%+-]{0,63}@(?:[a-z0-9-]{1,63}\.)+[a-z]{2,24}", re.I)
EMAIL_RE = re.compile(r"(?<![\w.+-])([a-z0-9][a-z0-9._%+-]{0,63}@(?:[a-z0-9-]{1,63}\.)+[a-z]{2,24})(?![\w-])", re.I)
OBFUSCATED_RE = re.compile(
    r"([a-z0-9][a-z0-9._%+-]{0,63})\s*(?:\[at\]|\(at\)|\{at\}|\s+at\s+|\[@\]|\(@\))\s*"
    # A literal dot joins adjacent domain labels only. Whitespace around it can
    # be a sentence boundary ("confidence at CeX. Shop"), not an email.
    r"((?:[a-z0-9-]{1,63}(?:\.|\s*(?:\[dot\]|\(dot\)|\s+dot\s+)\s*))+[a-z]{2,24})\b", re.I)
EMAIL_NOISE = re.compile(r"\.(png|jpe?g|gif|webp|svg|css|js)$|^(u00|x[0-9a-f]{2})|@(sentry[\w.-]*|[\w.-]*wixpress|"
                         r"example|domain|email)\.(com|org|net|io)$|@[0-9]+x\.|^[0-9a-f]{16,}@", re.I)
PHONE_LABEL_RE = re.compile(
    r"(?:tel(?:efon|ephone)?|phone|fon|mobil[e]?|mob|handy|call|ruf|t\.)\s*[.:]*\s*((?:\+|00)?[\d(][\d\s()./-]{5,22}\d)",
    re.I)
PHONE_ANY_RE = re.compile(r"(?<![\w.])((?:\+|00)\d[\d\s()./-]{6,20}\d|\(?0\d{2,5}\)?[\s/-]\d[\d\s/-]{4,14}\d)(?![\w.])")
SOCIAL_HOSTS = {
    "facebooks": ("facebook.com", "fb.com", "fb.me"),
    "instagrams": ("instagram.com", "instagr.am"),
    "linkedIns": ("linkedin.com",),
    "twitters": ("twitter.com", "x.com"),
    "youtubes": ("youtube.com", "youtu.be"),
    "tiktoks": ("tiktok.com",),
    "pinterests": ("pinterest.com", "pin.it"),
    "discords": ("discord.gg", "discord.com"),
}
SOCIAL_SKIP_PATHS = re.compile(r"/(sharer|share|intent|dialog|plugins|tr|login|signup|hashtag|search|watch\?v=|embed|"
                               r"p/|reel/|explore|policies|privacy|legal|help|about/|home\.php|posts/|photos/|videos/|"
                               r"events/|groups/|stories/|status/)", re.I)
#: first path segments that are site features, not profiles
SOCIAL_RESERVED = {"recover", "reg", "lite", "watch", "business", "careers", "settings", "l.php", "ad_campaign",
                   "login", "signup", "help", "legal", "privacy", "policies", "about", "terms", "accounts", "explore",
                   "direct", "developers", "ads", "marketplace", "gaming", "fundraisers", "notes", "pages/create",
                   "pages/category", "home", "i", "hashtag", "results", "feed", "jobs", "learning", "premium", "intl",
                   "share", "sharer", "dialog", "tr", "web", "en", "de", "static", "embed", "plugins"}

CONTACT_HINTS = re.compile(
    r"(impressum|imprint|legal[-_ ]?notice|mentions[-_ ]?legales|aviso[-_ ]?legal|note[-_ ]?legali|colofon|"
    r"kontakt|contact|contacto|contatti|kontakty|about|ueber-uns|uber-uns|über-uns|chi-siamo|qui-sommes)", re.I)


def normalize_email(value: str) -> str | None:
    email = value.strip().strip(".").lower()
    email = email.replace("mailto:", "").split("?")[0]
    if EMAIL_NOISE.search(email) or "@" not in email:
        return None
    local, _, domain = email.partition("@")
    if not local or "." not in domain or len(email) > 254 or domain.startswith("www."):
        return None
    if not EMAIL_FULL.fullmatch(email):
        # e.g. a mailto: link "department@1-877-250-5823.%0A%0Aall" (walgreens.com): not an address
        return None
    return email


def emails_in(text: str, links: list[str]) -> list[str]:
    found: list[str] = []
    for link in links:
        if link.lower().startswith("mailto:"):
            for part in up.unquote(link[7:]).split("?")[0].split(","):
                e = normalize_email(part)
                if e:
                    found.append(e)
    for m in EMAIL_RE.finditer(text or ""):
        e = normalize_email(m.group(1))
        if e:
            found.append(e)
    for m in OBFUSCATED_RE.finditer(text or ""):
        domain = re.sub(r"\s*(?:\[dot\]|\(dot\)|\s+dot\s+)\s*", ".", m.group(2), flags=re.I).replace(" ", "")
        e = normalize_email(f"{m.group(1)}@{domain}")
        if e:
            found.append(e)
    return list(dict.fromkeys(found))


def normalize_phone(raw: str) -> str | None:
    text = raw.strip()
    plus = text.startswith("+") or text.startswith("00")
    digits = re.sub(r"\D", "", text)
    if text.startswith("00"):
        digits = digits[2:]
    if not 7 <= len(digits) <= 15:
        return None
    return ("+" if plus else "") + digits


#: country calling codes for national-format numbers (leading trunk 0)
CALLING_CODES = {"DE": "49", "AT": "43", "CH": "41", "GB": "44", "FR": "33", "NL": "31", "BE": "32", "IT": "39",
                 "ES": "34", "PL": "48", "CZ": "420", "DK": "45", "SE": "46", "NO": "47", "FI": "358", "IE": "353",
                 "PT": "351", "LU": "352", "US": "1", "CA": "1"}


def to_e164(phone: str, country: str | None) -> str:
    """``0301234567`` with country DE -> ``+49301234567``; numbers already international stay."""
    if phone.startswith("+") or not country:
        return phone
    code = CALLING_CODES.get(country.upper())
    if not code:
        return phone
    if code == "1":
        return "+1" + phone if len(phone) == 10 else ("+" + phone if len(phone) == 11 and phone.startswith("1") else phone)
    if phone.startswith("0") and not phone.startswith("00"):
        return "+" + code + phone[1:]
    return phone


def phones_in(text: str, links: list[str]) -> tuple[list[str], list[str]]:
    sure: list[str] = []
    for link in links:
        if link.lower().startswith("tel:"):
            p = normalize_phone(up.unquote(link[4:]))
            if p:
                sure.append(p)
    for m in PHONE_LABEL_RE.finditer(text or ""):
        p = normalize_phone(m.group(1))
        if p:
            sure.append(p)
    uncertain = []
    for m in PHONE_ANY_RE.finditer(text or ""):
        p = normalize_phone(m.group(1))
        if p and p not in sure:
            uncertain.append(p)
    return list(dict.fromkeys(sure)), list(dict.fromkeys(uncertain))


def socials_in(links: list[str]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {k: [] for k in SOCIAL_HOSTS}
    for link in links:
        try:
            parsed = up.urlparse(link)
        except ValueError:
            continue
        host = parsed.netloc.lower().split(":")[0]
        host = host[4:] if host.startswith("www.") else host
        host = host[2:] if host.startswith("m.") else host
        for key, hosts in SOCIAL_HOSTS.items():
            if any(host == h or host.endswith("." + h) for h in hosts):
                path = parsed.path.rstrip("/")
                if not path or path == "/" or SOCIAL_SKIP_PATHS.search(parsed.path + ("?" + parsed.query if parsed.query else "")):
                    continue
                segments = [x for x in path.split("/") if x]
                first = segments[0].lower() if segments else ""
                if first in SOCIAL_RESERVED or "/".join(segments[:2]).lower() in SOCIAL_RESERVED:
                    continue
                if host.startswith("l.") or host.startswith("lm."):
                    continue
                if key == "facebooks" and len(segments) > 1 and segments[0].lower() != "pages" \
                        and segments[0].lower() != "profile.php":
                    path = "/" + segments[0]
                if key in ("instagrams", "twitters", "tiktoks", "pinterests") and len(segments) > 1:
                    path = "/" + segments[0]
                clean = f"https://www.{host}{path}" if host in hosts and not host.startswith(("discord.gg", "youtu.be", "pin.it", "fb.me")) else f"https://{host}{path}"
                if clean not in out[key]:
                    out[key].append(clean)
    return out


def candidate_pages(base_url: str, links: list[dict[str, str]], limit: int) -> list[str]:
    """Same-site links whose URL or text looks like contact / about / Impressum, Impressum first."""
    base = up.urlparse(base_url)
    site = _site(base.netloc)
    scored: list[tuple[int, str]] = []
    seen = set()
    for link in links:
        href = link.get("href") or ""
        text = link.get("text") or ""
        if not href.startswith("http"):
            continue
        parsed = up.urlparse(href)
        if _site(parsed.netloc) != site:
            continue
        target = parsed._replace(fragment="").geturl()
        # one entry per address; spelling alone (a trailing slash, ``www.``) makes no two addresses one page
        key = f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{parsed.path or '/'}?{parsed.query}"
        base_key = f"{base.scheme.lower()}://{base.netloc.lower()}{base.path or '/'}?{base.query}"
        if key in seen or key == base_key:
            continue
        hay = f"{parsed.path} {text}"
        m = CONTACT_HINTS.search(hay)
        if not m:
            continue
        word = m.group(1).lower()
        score = 0 if word in ("impressum", "imprint") or "legal" in word or "mentions" in word or "aviso" in word else (
            1 if word.startswith(("kontakt", "contact", "contatti")) else 2)
        seen.add(key)
        scored.append((score, target))
    scored.sort()
    return [u for _, u in scored[:limit]]


def _site(netloc: str) -> str:
    host = netloc.lower().split(":")[0]
    parts = host.split(".")
    if len(parts) >= 3 and parts[-2] in ("co", "com", "org", "net", "gov", "ac") and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


# ----------------------------------------------------------------- Impressum
LEGAL_FORMS = ["GmbH & Co. KG", "UG (haftungsbeschränkt)", "gGmbH", "GmbH", "UG", "AG", "SE", "KGaA", "KG", "OHG",
               "GbR", "PartG mbB", "PartG", "e.K.", "e.V.", "eG", "Ltd.", "Ltd", "S.à r.l.", "SARL", "S.A.", "B.V.",
               "Sp. z o.o."]
ROLE_TYPES = [
    (re.compile(r"geschäftsführ|managing director|geschaeftsfuehr|ceo\b|gérant", re.I), "managing_director"),
    (re.compile(r"inhaber|owner|propriet", re.I), "owner"),
    (re.compile(r"vorstand|board of directors|executive board", re.I), "board_member"),
    (re.compile(r"aufsichtsrat|supervisory board", re.I), "supervisory_board"),
    (re.compile(r"verantwortlich|v\.?\s?i\.?\s?s\.?\s?d\.?\s?p|responsible for (the )?content|redaktion", re.I), "content_responsible"),
    (re.compile(r"datenschutzbeauftrag|data protection officer", re.I), "data_protection_officer"),
]
ROLE_LINE = re.compile(
    r"(Geschäftsführer(?:in)?|Geschäftsführung|Inhaber(?:in)?|Vertreten durch(?: den| die)?(?: Geschäftsführer(?:in)?)?|"
    r"Vorstand|Aufsichtsrat(?:svorsitzende[r]?)?|Verantwortlich(?: für den Inhalt)?(?: nach § ?\d+ ?(?:Abs\. ?\d+ )?(?:RStV|MStV|TMG|DDG))?|"
    r"V\.?\s?i\.?\s?S\.?\s?d\.?\s?P\.?|Datenschutzbeauftragte[r]?|Managing Directors?|Owner|Represented by|"
    r"Gesellschafter(?:in)?)\s*[:\-–]?\s*\n?\s*([^\n]{3,160})", re.I)
VAT_RE = re.compile(r"\b(DE\s?\d{9}|ATU\s?\d{8}|CHE[-\s]?\d{3}\.?\d{3}\.?\d{3}(?:\s?(?:MWST|TVA|IVA))?|"
                    r"(?:FR|IT|NL|BE|ES|PL|LU|DK|SE|FI|CZ|PT|IE)[0-9A-Z]{8,12})\b")
REG_RE = re.compile(r"\b(HR[AB]\s?\d{2,7}\s?[A-Z]?|FN\s?\d{3,6}\s?[a-z]|VR\s?\d{2,6}|PR\s?\d{2,6}|GnR\s?\d{2,6})\b")
COURT_NAMED_RE = re.compile(r"(Amtsgericht|Handelsgericht|Landesgericht)\s+([A-ZÄÖÜ][\wäöüß.\-]+(?:[ \t][A-ZÄÖÜ(][\wäöüß.()\-]+)?)")
COURT_RE = re.compile(r"(Amtsgericht|Registergericht|Handelsgericht|Landesgericht)\s*:?\s*([A-ZÄÖÜ][\wäöüß.\- ]{2,40}?)(?=[,\n;]|\s+(?:HR[AB]|unter|Register|$))")
ADDRESS_RE = re.compile(r"([A-ZÄÖÜ][\wäöüßéè.\- ]{2,60}?(?:straße|strasse|str\.|weg|platz|allee|gasse|ring|damm|ufer|"
                        r"markt|chaussee|steig|berg|hof)\s*\d+[a-zA-Z]?(?:\s?[-/]\s?\d+[a-zA-Z]?)?)\s*[,\n]\s*(?:D-|A-|CH-)?"
                        r"(\d{4,5})\s+([A-ZÄÖÜ][\wäöüß.\- ]{1,40})", re.I)
NAME_RE = re.compile(r"^(?:(?:Dr|Prof|Dipl|Mag|Ing)\.?(?:-[A-Za-z]+\.)?\s+)*[A-ZÄÖÜ][a-zäöüßéè'-]+(?:\s+[A-ZÄÖÜ][a-zäöüßéè'-]+){1,3}"
                     r"(?:,\s*(?:M\.?\s?Sc\.?|MBA|LL\.?M\.?))?")


def _role_type(role: str) -> str:
    for rx, kind in ROLE_TYPES:
        if rx.search(role):
            return kind
    return "other"


def parse_impressum(text: str, url: str, emails: list[str], phones: list[str]) -> dict[str, Any] | None:
    """Rule-based reading of an Impressum page; fields not on the page are left out."""
    clean = unicodedata.normalize("NFC", text or "")
    if len(clean) < 40:
        return None
    out: dict[str, Any] = {"url": url}
    company = None
    for line in clean.splitlines()[:120]:
        stripped = line.strip()
        if re.search(r"(?i:gericht|register|steuer)|\bUSt|\bVAT\b|\bHRB?\b", stripped):
            continue
        for form in LEGAL_FORMS:
            m = re.search(r"(?:^|\s)" + re.escape(form) + r"(?=$|[\s,])", stripped)
            if m and len(stripped) <= 120:
                # "BrezelBar e.K., Arndtstr. 35, 10965 Berlin": the name ends with its legal form
                company = {"name": stripped[:m.end()].strip().rstrip(","), "legalForm": form}
                break
        if company:
            break
    if company:
        out["company"] = company
    people = []
    for m in ROLE_LINE.finditer(clean):
        role, rest = m.group(1).strip(), m.group(2).strip()
        for chunk in re.split(r"\s*(?:,|;| und | and | & |\|)\s*(?=(?:Dr|Prof|[A-ZÄÖÜ]))", rest):
            name = NAME_RE.match(chunk.strip())
            if name and len(name.group(0)) <= 80:
                person = {"name": name.group(0).strip(), "role": role, "roleType": _role_type(role)}
                if person not in people:
                    people.append(person)
    if people:
        out["people"] = people
    if emails:
        out["emails"] = [{"value": e, "type": "generic" if e.split("@")[0] in GENERIC_LOCALS else "personal"} for e in emails]
    if phones:
        out["phones"] = [{"value": p, "type": "generic"} for p in phones]
    addresses = []
    for m in ADDRESS_RE.finditer(clean):
        addr = {"street": m.group(1).strip(), "postalCode": m.group(2), "city": m.group(3).strip()}
        if addr not in addresses:
            addresses.append(addr)
    if addresses:
        out["addresses"] = addresses[:3]
    vat = VAT_RE.search(clean)
    if vat:
        out["vatId"] = re.sub(r"\s", "", vat.group(1))
    regs = []
    court = COURT_NAMED_RE.search(clean) or COURT_RE.search(clean)
    for m in REG_RE.finditer(clean):
        entry = {"number": m.group(1).strip()}
        if court:
            entry["court"] = f"{court.group(1)} {court.group(2).strip()}"
        if entry not in regs:
            regs.append(entry)
    out["registrations"] = regs
    meaningful = {k for k in out if k not in ("url", "registrations")} | ({"registrations"} if regs else set())
    return out if meaningful - {"emails", "phones"} else None


GENERIC_LOCALS = {"info", "kontakt", "contact", "office", "mail", "hello", "hallo", "post", "service", "team",
                  "praxis", "buero", "büro", "support", "sales", "admin", "booking", "reservierung", "reservations",
                  "anfrage", "empfang", "verwaltung"}
