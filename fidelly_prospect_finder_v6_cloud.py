#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Fidelly Prospect Finder FREE v6 Cloud
- Sans Google Places
- Sans clé API
- Sans compte billing

Pipeline:
1) Annuaire des Entreprises: établissements ciblés par zone + activité
2) Nominatim / OpenStreetMap: recherche commerce par commerce
3) Recherche web gratuite de secours (DDG HTML/Lite, Bing, Brave)
4) Crawl du site officiel: email, téléphone, réseaux, fidélité
5) Scoring Fidelly

Important:
- Nominatim public: usage léger, max ~1 requête/seconde
- Google ratings/reviews non disponibles dans cette version
"""

import argparse
import csv
import html as html_lib
import json
import os
import re
import time
import unicodedata
from difflib import SequenceMatcher
from urllib.parse import urljoin, urlparse, parse_qs, unquote

import requests
from bs4 import BeautifulSoup


GEO_COMMUNES_URL = "https://geo.api.gouv.fr/communes"
ENTREPRISES_URL = "https://recherche-entreprises.api.gouv.fr/search"
NOMINATIM_SEARCH_URL = "https://nominatim.openstreetmap.org/search"

DDG_HTML_URL = "https://html.duckduckgo.com/html/"
DDG_LITE_URL = "https://lite.duckduckgo.com/lite/"
BING_URL = "https://www.bing.com/search"
BRAVE_URL = "https://search.brave.com/search"

TIMEOUT = 22

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "FidellyProspectFinder/6.0 (public-business-enrichment; Python requests)",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.7",
})

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/127.0 Safari/537.36"
    ),
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.7",
}

EMAIL_RE = re.compile(
    r"(?i)(?<![\w.+-])([A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,})(?![\w.-])"
)
PHONE_RE = re.compile(
    r"(?:(?:\+33|0033|0)[1-9](?:[\s.\-]?\d{2}){4})"
)

LEGAL_PREFIXES = {
    "sarl", "sas", "sasu", "eurl", "sa", "ei", "ets", "etablissement",
    "etablissements", "societe", "société", "ste", "scp", "selarl",
}

DIRECTORY_DOMAINS = (
    "facebook.com", "instagram.com", "linkedin.com", "tiktok.com",
    "tripadvisor.", "pagesjaunes.", "yelp.", "ubereats.", "deliveroo.",
    "thefork.", "justacote.", "societe.com", "pappers.fr",
    "annuaire-entreprises.data.gouv.fr", "verif.com", "manageo.fr",
    "kompass.", "hoodspot.", "118000.fr", "118712.fr", "wikipedia.org",
)

SOCIAL_DOMAINS = {
    "instagram": ("instagram.com",),
    "facebook": ("facebook.com", "fb.com"),
    "tiktok": ("tiktok.com",),
    "linkedin": ("linkedin.com",),
}

CONTACT_PAGE_HINTS = (
    "contact", "nous-contacter", "contactez-nous", "mentions-legales",
    "mentions légales", "mentions-legales", "legal", "a-propos",
    "à-propos", "qui-sommes-nous", "equipe", "team", "cgv",
)

LOYALTY_TERMS = (
    "carte de fidélité", "carte fidelite", "programme fidélité",
    "programme de fidélité", "programme fidelite", "points fidélité",
    "points fidelite", "club fidélité", "club fidelite", "cagnotte",
    "carte à tampons", "carte a tampons", "tampons fidélité",
    "tampons fidelite", "récompense fidélité", "recompense fidelite",
)

NAF_MAP = {
    "restaurant": ["56.10A", "56.10B", "56.10C"],
    "restaurants": ["56.10A", "56.10B", "56.10C"],
    "café": ["56.30Z"],
    "cafe": ["56.30Z"],
    "bar": ["56.30Z"],
    "bars": ["56.30Z"],
    "caviste": ["47.25Z"],
    "cavistes": ["47.25Z"],
    "coiffeur": ["96.02A"],
    "coiffeuse": ["96.02A"],
    "salon de coiffure": ["96.02A"],
    "barbier": ["96.02A"],
    "institut de beauté": ["96.02B"],
    "institut de beaute": ["96.02B"],
    "esthéticienne": ["96.02B"],
    "estheticienne": ["96.02B"],
    "fleuriste": ["47.76Z"],
    "animalerie": ["47.76Z"],
    "boulangerie": ["10.71C", "47.24Z"],
    "pâtisserie": ["10.71D", "47.24Z"],
    "patisserie": ["10.71D", "47.24Z"],
    "épicerie": ["47.11B", "47.11C", "47.11D", "47.11E", "47.11F"],
    "epicerie": ["47.11B", "47.11C", "47.11D", "47.11E", "47.11F"],
    "primeur": ["47.21Z"],
    "boucherie": ["47.22Z"],
    "poissonnerie": ["47.23Z"],
    "librairie": ["47.61Z"],
    "magasin de vêtements": ["47.71Z"],
    "magasin de vetements": ["47.71Z"],
    "chaussures": ["47.72A"],
    "bijouterie": ["47.77Z"],
    "opticien": ["47.78A"],
    "photographe": ["74.20Z"],
    "salle de sport": ["93.13Z"],
    "fitness": ["93.13Z"],
    "spa": ["96.04Z"],
    "tatoueur": ["96.09Z"],
    "toilettage": ["96.09Z"],
    "cbd": [],
}


# ------------------------- helpers -------------------------

def norm(v):
    return str(v or "").strip()


def ascii_norm(v):
    s = unicodedata.normalize("NFKD", norm(v).lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.replace("&", " et ")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    words = [w for w in s.split() if w not in LEGAL_PREFIXES]
    return " ".join(words).strip()


def ensure_url(url):
    u = norm(url)
    if not u:
        return ""
    if u.startswith("//"):
        u = "https:" + u
    if not u.startswith(("http://", "https://")):
        u = "https://" + u
    return u


def domain(url):
    try:
        d = urlparse(url).netloc.lower()
        if d.startswith("www."):
            d = d[4:]
        return d
    except Exception:
        return ""


def is_directory(url):
    d = domain(url)
    return any(x in d for x in DIRECTORY_DOMAINS)


def clean_email(v):
    e = html_lib.unescape(norm(v)).strip(".,;:()[]<>\"'")
    if not e or "@" not in e:
        return ""
    bad = (
        "example.", "sentry.", "wixpress.", "wordpress.", "yourdomain.",
        ".png", ".jpg", ".jpeg", ".svg", ".webp", ".gif"
    )
    if any(x in e.lower() for x in bad):
        return ""
    return e.lower()


def normalize_phone(v):
    return re.sub(r"\s+", " ", norm(v)).strip(" ;,|")


def similarity(a, b):
    a = ascii_norm(a)
    b = ascii_norm(b)
    if not a or not b:
        return 0.0

    ratio = SequenceMatcher(None, a, b).ratio()
    at = set(a.split())
    bt = set(b.split())
    overlap = len(at & bt) / max(1, min(len(at), len(bt)))
    contained = 0.95 if (a in b or b in a) and min(len(a), len(b)) >= 5 else 0.0
    return max(ratio, overlap * 0.92, contained)


def first(*vals):
    for v in vals:
        if norm(v):
            return norm(v)
    return ""


# ------------------------- communes -------------------------

def resolve_zone(zone):
    r = SESSION.get(
        GEO_COMMUNES_URL,
        params={
            "nom": zone,
            "fields": "nom,code,codesPostaux,departement,region,population",
            "boost": "population",
            "limit": 5,
        },
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    data = r.json()
    exact = [x for x in data if x.get("nom", "").lower() == zone.lower()]
    return exact or data[:1]


# ------------------------- entreprises -------------------------

def establishment_name(etab, company):
    if etab.get("nom_commercial"):
        return norm(etab["nom_commercial"])

    enseignes = etab.get("liste_enseignes") or []
    if isinstance(enseignes, list):
        for x in enseignes:
            if x:
                return norm(x)

    return first(
        etab.get("enseigne"),
        company.get("nom_complet"),
        company.get("nom_raison_sociale"),
        company.get("sigle"),
    )


def extract_establishment(company, postcode=None, naf_code=None):
    matching = company.get("matching_etablissements") or []
    siege = company.get("siege") or {}

    candidates = list(matching)
    if siege and not any(e.get("siret") == siege.get("siret") for e in candidates):
        candidates.append(siege)

    candidates = [
        e for e in candidates
        if not e.get("etat_administratif") or e.get("etat_administratif") == "A"
    ]

    if postcode:
        candidates = [
            e for e in candidates
            if norm(e.get("code_postal")) == norm(postcode)
        ]

    if naf_code:
        candidates = [
            e for e in candidates
            if norm(e.get("activite_principale")).upper() == norm(naf_code).upper()
        ]

    if not candidates:
        return None

    candidates.sort(
        key=lambda e: bool(
            e.get("nom_commercial") or e.get("liste_enseignes") or e.get("enseigne")
        ),
        reverse=True,
    )
    e = candidates[0]

    address = e.get("adresse") or " ".join(filter(None, [
        e.get("numero_voie"),
        e.get("type_voie"),
        e.get("libelle_voie"),
        e.get("code_postal"),
        e.get("libelle_commune"),
    ])).strip()

    return {
        "name": establishment_name(e, company),
        "address": norm(address),
        "postcode": norm(e.get("code_postal")),
        "city": norm(e.get("libelle_commune") or e.get("commune")),
        "siret": norm(e.get("siret")),
        "naf": norm(e.get("activite_principale")),
        "latitude": e.get("latitude") or "",
        "longitude": e.get("longitude") or "",
    }


def gov_search(postcode, keyword, naf_code=None, pages=2):
    seen = set()

    for page in range(1, pages + 1):
        params = {
            "page": page,
            "per_page": 25,
            "code_postal": postcode,
            "etat_administratif": "A",
        }
        if naf_code:
            params["activite_principale"] = naf_code
        else:
            params["q"] = keyword

        try:
            r = SESSION.get(ENTREPRISES_URL, params=params, timeout=TIMEOUT)
            r.raise_for_status()
            payload = r.json()
        except Exception as exc:
            print(f"    ! API Entreprises: {exc}")
            break

        results = payload.get("results") or []
        if not results:
            break

        for company in results:
            est = extract_establishment(company, postcode, naf_code)
            if not est:
                continue

            key = est["siret"] or (company.get("siren"), est["name"], est["address"])
            if not key or key in seen:
                continue
            seen.add(key)

            yield {
                **est,
                "siren": norm(company.get("siren")),
                "legal_name": norm(
                    company.get("nom_complet") or company.get("nom_raison_sociale")
                ),
                "employees": norm(company.get("tranche_effectif_salarie")),
                "creation_date": norm(company.get("date_creation")),
                "open_establishments": company.get("nombre_etablissements_ouverts") or "",
                "total_establishments": company.get("nombre_etablissements") or "",
                "company_status": norm(company.get("etat_administratif")),
            }

        total_pages = payload.get("total_pages")
        if total_pages and page >= total_pages:
            break


# ------------------------- Nominatim per business -------------------------

_last_nominatim_call = 0.0


def nominatim_wait():
    global _last_nominatim_call
    elapsed = time.monotonic() - _last_nominatim_call
    if elapsed < 1.05:
        time.sleep(1.05 - elapsed)
    _last_nominatim_call = time.monotonic()


def osm_social(extratags, key):
    return first(
        extratags.get(key),
        extratags.get(f"contact:{key}"),
    )


def nominatim_business(row, contact_email=""):
    """
    Recherche un seul commerce par nom + ville + CP.
    Retourne ses extratags OSM s'il y a un match suffisamment crédible.
    """
    queries = []

    display_name = ascii_norm(row.get("name"))
    legal = ascii_norm(row.get("legal_name"))

    if display_name:
        queries.append(
            f'{display_name}, {row.get("city")}, {row.get("postcode")}, France'
        )
    if legal and legal != display_name:
        queries.append(
            f'{legal}, {row.get("city")}, {row.get("postcode")}, France'
        )

    for q in queries[:2]:
        nominatim_wait()

        params = {
            "q": q,
            "format": "jsonv2",
            "addressdetails": 1,
            "extratags": 1,
            "namedetails": 1,
            "limit": 5,
            "countrycodes": "fr",
        }
        if contact_email:
            params["email"] = contact_email

        try:
            r = SESSION.get(
                NOMINATIM_SEARCH_URL,
                params=params,
                timeout=TIMEOUT,
            )
            r.raise_for_status()
            results = r.json()
        except Exception as exc:
            return {}, f"Nominatim error: {exc}"

        best = None
        best_score = 0.0

        for item in results:
            namedetails = item.get("namedetails") or {}
            address = item.get("address") or {}

            candidate_names = [
                namedetails.get("name"),
                namedetails.get("brand"),
                namedetails.get("operator"),
                item.get("display_name", "").split(",")[0],
            ]

            name_score = max(
                [similarity(row.get("name"), x) for x in candidate_names if x]
                + [similarity(row.get("legal_name"), x) * 0.92
                   for x in candidate_names if x]
                + [0.0]
            )

            postcode = first(address.get("postcode"))
            city = first(
                address.get("city"),
                address.get("town"),
                address.get("village"),
                address.get("municipality"),
            )

            if postcode and row.get("postcode") == postcode:
                name_score += 0.05
            if city and ascii_norm(city) == ascii_norm(row.get("city")):
                name_score += 0.04

            if name_score > best_score:
                best_score = name_score
                best = item

        if best and best_score >= 0.62:
            extra = best.get("extratags") or {}

            data = {
                "website": ensure_url(first(
                    extra.get("website"),
                    extra.get("contact:website"),
                    extra.get("url"),
                )),
                "email": clean_email(first(
                    extra.get("email"),
                    extra.get("contact:email"),
                )),
                "phone": normalize_phone(first(
                    extra.get("phone"),
                    extra.get("contact:phone"),
                    extra.get("mobile"),
                    extra.get("contact:mobile"),
                )),
                "instagram": ensure_url(osm_social(extra, "instagram")),
                "facebook": ensure_url(osm_social(extra, "facebook")),
                "tiktok": ensure_url(osm_social(extra, "tiktok")),
                "linkedin": ensure_url(osm_social(extra, "linkedin")),
                "osm_match_name": best.get("display_name", "").split(",")[0],
                "osm_match_score": round(min(best_score, 1.0), 3),
                "osm_id": f'{best.get("osm_type","")}/{best.get("osm_id","")}',
                "osm_display_name": best.get("display_name", ""),
            }
            if data["email"]:
                data["email_source"] = "OpenStreetMap/Nominatim"
            return data, ""

    return {}, ""


# ------------------------- search engine fallbacks -------------------------

def deobfuscate(text):
    s = html_lib.unescape(text or "")
    s = re.sub(r"\s*(?:\[|\()?at(?:\]|\))?\s*", "@", s, flags=re.I)
    s = re.sub(r"\s*(?:\[|\()?dot(?:\]|\))?\s*", ".", s, flags=re.I)
    s = re.sub(r"\s*\[\s*@\s*\]\s*", "@", s)
    s = re.sub(r"\s*\[\s*\.\s*\]\s*", ".", s)
    return s


def contacts_from_text(text):
    emails = []
    phones = []

    for blob in (text or "", deobfuscate(text or "")):
        for e in EMAIL_RE.findall(blob):
            e = clean_email(e)
            if e and e not in emails:
                emails.append(e)

    for p in PHONE_RE.findall(text or ""):
        p = normalize_phone(p)
        if p and p not in phones:
            phones.append(p)

    return emails, phones


def business_tokens_match(text, name, city):
    hay = ascii_norm(text)
    n = ascii_norm(name)
    c = ascii_norm(city)

    words = [w for w in n.split() if len(w) >= 4]
    if not words:
        return False

    matched = sum(w in hay for w in words)
    business_ok = matched >= max(1, min(2, len(words)))
    city_ok = not c or c in hay

    return business_ok and city_ok


def unwrap_ddg(href):
    if not href:
        return ""
    if href.startswith("//"):
        href = "https:" + href
    p = urlparse(href)
    qs = parse_qs(p.query)
    if qs.get("uddg"):
        return unquote(qs["uddg"][0])
    return href


def parse_generic_search(html_text, name, city, engine):
    soup = BeautifulSoup(html_text, "html.parser")
    candidate_sites = []
    emails = []
    phones = []

    # Extract contacts from snippets/page text
    page_emails, page_phones = contacts_from_text(soup.get_text(" ", strip=True))
    emails.extend(page_emails)
    phones.extend(page_phones)

    if engine == "ddg":
        blocks = soup.select(".result")
        for b in blocks[:12]:
            a = b.select_one("a.result__a")
            if not a:
                continue
            url = unwrap_ddg(a.get("href", ""))
            text = b.get_text(" ", strip=True)
            if (
                url.startswith("http")
                and not is_directory(url)
                and business_tokens_match(text, name, city)
            ):
                candidate_sites.append(url)

    elif engine == "ddg_lite":
        for a in soup.select("a[href]")[:80]:
            url = unwrap_ddg(a.get("href", ""))
            text = a.get_text(" ", strip=True)
            parent = a.parent.get_text(" ", strip=True) if a.parent else text
            if (
                url.startswith("http")
                and not is_directory(url)
                and business_tokens_match(parent, name, city)
            ):
                candidate_sites.append(url)

    elif engine == "bing":
        for b in soup.select("li.b_algo")[:12]:
            a = b.select_one("h2 a")
            if not a:
                continue
            url = a.get("href", "")
            text = b.get_text(" ", strip=True)
            if (
                url.startswith("http")
                and not is_directory(url)
                and business_tokens_match(text, name, city)
            ):
                candidate_sites.append(url)

    elif engine == "brave":
        # Brave markup can change; generic block/anchor parsing is intentional.
        for a in soup.select('a[href^="http"]')[:100]:
            url = a.get("href", "")
            text = (
                a.parent.get_text(" ", strip=True)
                if a.parent else a.get_text(" ", strip=True)
            )
            if (
                url.startswith("http")
                and not is_directory(url)
                and business_tokens_match(text, name, city)
            ):
                candidate_sites.append(url)

    # Deduplicate
    seen = set()
    sites = []
    for u in candidate_sites:
        d = domain(u)
        if d and d not in seen:
            seen.add(d)
            sites.append(u)

    return {
        "website": sites[0] if sites else "",
        "email": emails[0] if emails else "",
        "phone": phones[0] if phones else "",
    }


def web_search_business(row):
    name = row.get("name") or row.get("legal_name")
    city = row.get("city") or row.get("zone")
    postcode = row.get("postcode")

    queries = [
        f'"{name}" "{city}" {postcode}',
        f'"{name}" "{city}" contact',
    ]

    engines = [
        ("ddg", DDG_HTML_URL),
        ("ddg_lite", DDG_LITE_URL),
        ("bing", BING_URL),
        ("brave", BRAVE_URL),
    ]

    best = {"website": "", "email": "", "phone": "", "search_source": ""}

    for q in queries:
        for engine, url in engines:
            try:
                params = {"q": q}
                if engine == "brave":
                    params["source"] = "web"
                if engine == "bing":
                    params["setlang"] = "fr"

                r = requests.get(
                    url,
                    params=params,
                    headers=BROWSER_HEADERS,
                    timeout=TIMEOUT,
                )
                if r.status_code != 200:
                    continue

                parsed = parse_generic_search(r.text, name, city, engine)

                if parsed.get("website") and not best["website"]:
                    best["website"] = parsed["website"]
                    best["search_source"] = engine

                if parsed.get("email") and not best["email"]:
                    best["email"] = parsed["email"]
                    best["search_source"] = best["search_source"] or engine

                if parsed.get("phone") and not best["phone"]:
                    best["phone"] = parsed["phone"]
                    best["search_source"] = best["search_source"] or engine

                if best["website"] and (best["email"] or best["phone"]):
                    return best

            except requests.RequestException:
                continue

    return best


# ------------------------- site crawl -------------------------

def extract_page_contacts(page_url, body):
    soup = BeautifulSoup(body, "html.parser")
    visible = soup.get_text(" ", strip=True)

    emails, phones = contacts_from_text(body + " " + visible)
    socials = {k: "" for k in SOCIAL_DOMAINS}

    for a in soup.select('a[href^="mailto:"]'):
        e = clean_email(a.get("href", "")[7:].split("?")[0])
        if e and e not in emails:
            emails.insert(0, e)

    for a in soup.select('a[href^="tel:"]'):
        p = normalize_phone(a.get("href", "")[4:])
        if p and p not in phones:
            phones.insert(0, p)

    for a in soup.select("a[href]"):
        href = urljoin(page_url, a.get("href", ""))
        low = href.lower()
        for social, domains in SOCIAL_DOMAINS.items():
            if not socials[social] and any(d in low for d in domains):
                socials[social] = href

    return soup, visible.lower(), emails, phones, socials


def crawl_site(site_url):
    result = {
        "website": ensure_url(site_url),
        "email": "",
        "email_source": "",
        "phone": "",
        "instagram": "",
        "facebook": "",
        "tiktok": "",
        "linkedin": "",
        "loyalty": "no",
        "loyalty_evidence": "",
    }

    if not result["website"]:
        return result

    base_domain = domain(result["website"])
    queue = [result["website"]]
    visited = set()

    all_emails = []
    all_phones = []
    socials = {k: "" for k in SOCIAL_DOMAINS}
    loyalty_hit = None

    while queue and len(visited) < 10:
        url = queue.pop(0)
        if url in visited or domain(url) != base_domain:
            continue
        visited.add(url)

        try:
            r = requests.get(
                url,
                headers=BROWSER_HEADERS,
                timeout=TIMEOUT,
                allow_redirects=True,
            )
            if r.status_code >= 400:
                continue
            ctype = r.headers.get("content-type", "").lower()
            if "html" not in ctype:
                continue

            body = r.text[:2_500_000]
            soup, visible, emails, phones, page_socials = extract_page_contacts(
                r.url, body
            )

            for e in emails:
                if e not in all_emails:
                    all_emails.append(e)
                    if not result["email_source"]:
                        result["email_source"] = r.url

            for p in phones:
                if p not in all_phones:
                    all_phones.append(p)

            for key, val in page_socials.items():
                if val and not socials[key]:
                    socials[key] = val

            if not loyalty_hit:
                for term in LOYALTY_TERMS:
                    if term in visible:
                        loyalty_hit = (term, r.url)
                        break

            # Linked contact/legal/about pages
            for a in soup.select("a[href]"):
                href = a.get("href", "")
                label = f'{a.get_text(" ", strip=True)} {href}'.lower()
                if any(h in label for h in CONTACT_PAGE_HINTS):
                    full = urljoin(r.url, href)
                    if (
                        domain(full) == base_domain
                        and full not in visited
                        and full not in queue
                    ):
                        queue.append(full)

            # Common pages
            if len(visited) == 1:
                for pth in (
                    "/contact", "/contact/", "/nous-contacter",
                    "/mentions-legales", "/mentions-legales/",
                    "/a-propos", "/qui-sommes-nous"
                ):
                    full = urljoin(r.url, pth)
                    if full not in visited and full not in queue:
                        queue.append(full)

        except requests.RequestException:
            continue

    if all_emails:
        result["email"] = all_emails[0]
    if all_phones:
        result["phone"] = all_phones[0]

    for k, v in socials.items():
        result[k] = v

    if loyalty_hit:
        result["loyalty"] = "yes"
        result["loyalty_evidence"] = f"{loyalty_hit[0]} @ {loyalty_hit[1]}"

    return result


def merge(row, data, source=""):
    changed = []

    for field in (
        "website", "email", "phone", "instagram",
        "facebook", "tiktok", "linkedin",
        "osm_match_name", "osm_match_score", "osm_id", "osm_display_name"
    ):
        value = data.get(field)
        if value and not row.get(field):
            row[field] = value
            changed.append(field)

    if data.get("email_source") and not row.get("email_source"):
        row["email_source"] = data["email_source"]

    if data.get("loyalty") == "yes":
        row["loyalty"] = "yes"
        row["loyalty_evidence"] = data.get("loyalty_evidence", "")

    if changed and source:
        current = row.get("enrichment_source", "")
        sources = [x for x in current.split("+") if x]
        if source not in sources:
            sources.append(source)
        row["enrichment_source"] = "+".join(sources)


# ------------------------- scoring -------------------------

def score(row):
    points = 0
    reasons = []

    if row.get("email"):
        points += 30; reasons.append("+30 email")
    if row.get("phone"):
        points += 15; reasons.append("+15 téléphone")
    if row.get("website"):
        points += 12; reasons.append("+12 site")
    if row.get("instagram"):
        points += 10; reasons.append("+10 Instagram")
    if row.get("facebook"):
        points += 3; reasons.append("+3 Facebook")

    try:
        branches = int(row.get("open_establishments") or 1)
    except Exception:
        branches = 1

    if branches <= 1:
        points += 12; reasons.append("+12 indépendant probable")
    elif branches <= 3:
        points += 6; reasons.append("+6 petite enseigne")
    elif branches >= 10:
        points -= 18; reasons.append("-18 chaîne probable")

    if row.get("loyalty") == "yes":
        points -= 20; reasons.append("-20 fidélité déjà détectée")
    else:
        points += 13; reasons.append("+13 fidélité non détectée")

    if row.get("siret"):
        points += 5; reasons.append("+5 SIRET")

    points = max(0, min(100, points))
    priority = "A" if points >= 65 else "B" if points >= 45 else "C"
    return points, priority, "; ".join(reasons)



# ------------------------- Google Sheets webhook -------------------------

def push_rows_to_google_sheets(rows, webhook_url="", webhook_token=""):
    """
    Envoie le lot de prospects vers un Google Apps Script Web App.
    Aucun service account / aucune Google Sheets API key nécessaire.
    """
    webhook_url = webhook_url or os.getenv("SHEETS_WEBHOOK_URL", "")
    webhook_token = webhook_token or os.getenv("SHEETS_WEBHOOK_TOKEN", "")

    if not webhook_url:
        return False, "SHEETS_WEBHOOK_URL absent"

    payload = {
        "token": webhook_token,
        "source": "fidelly-prospect-finder-v6",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "rows": rows,
    }

    try:
        r = requests.post(
            webhook_url,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=60,
            allow_redirects=True,
        )
        body = r.text[:1000]
        if r.status_code >= 400:
            return False, f"HTTP {r.status_code}: {body}"

        try:
            data = r.json()
        except Exception:
            data = {"ok": False, "message": body}

        if data.get("ok"):
            return True, data.get("message", "OK")
        return False, data.get("message", body or "Réponse Apps Script invalide")
    except Exception as exc:
        return False, str(exc)



# ------------------------- main -------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Fidelly Prospect Finder FREE v6 Cloud"
    )
    ap.add_argument("--zones", nargs="+", required=True)
    ap.add_argument("--keywords", nargs="+", required=True)
    ap.add_argument("--pages", type=int, default=2)
    ap.add_argument("--max-prospects", type=int, default=200)
    ap.add_argument("--output", default="fidelly_prospects_v6.csv")
    ap.add_argument(
        "--push-sheets",
        action="store_true",
        help="Envoie les résultats vers Google Sheets via Apps Script",
    )
    ap.add_argument(
        "--webhook-url",
        default="",
        help="Optionnel; sinon variable SHEETS_WEBHOOK_URL",
    )
    ap.add_argument(
        "--webhook-token",
        default="",
        help="Optionnel; sinon variable SHEETS_WEBHOOK_TOKEN",
    )
    ap.add_argument(
        "--nominatim-email",
        default="",
        help="Optionnel: email d'identification pour Nominatim si gros volume",
    )
    ap.add_argument(
        "--no-web-search",
        action="store_true",
        help="Désactive les moteurs web de secours",
    )
    args = ap.parse_args()

    print(
        f"[Fidelly FREE v6 CLOUD] zones={len(args.zones)} | "
        f"activités={len(args.keywords)}"
    )
    print("Aucune clé API ni compte billing requis.\n")

    rows = []
    seen = set()

    # 1) Legal discovery
    for zone in args.zones:
        try:
            communes = resolve_zone(zone)
        except Exception as exc:
            print(f"! Commune {zone}: {exc}")
            continue

        if not communes:
            print(f"! Commune introuvable: {zone}")
            continue

        commune = communes[0]
        zone_name = commune.get("nom", zone)
        postcodes = commune.get("codesPostaux") or []

        print(f"=== {zone_name} : {', '.join(postcodes)} ===")

        for keyword in args.keywords:
            naf_codes = NAF_MAP.get(keyword.lower(), [])
            modes = naf_codes or [None]

            for cp in postcodes[:3]:
                for naf in modes:
                    print(
                        f"  -> {keyword} | {cp} | "
                        + (f"NAF {naf}" if naf else "texte")
                    )

                    for item in gov_search(cp, keyword, naf, args.pages):
                        key = item.get("siret") or (
                            item.get("name"), item.get("address")
                        )
                        if not key or key in seen:
                            continue
                        seen.add(key)

                        rows.append({
                            "zone": zone_name,
                            "keyword": keyword,
                            **item,
                            "website": "",
                            "email": "",
                            "email_source": "",
                            "phone": "",
                            "instagram": "",
                            "facebook": "",
                            "tiktok": "",
                            "linkedin": "",
                            "loyalty": "no",
                            "loyalty_evidence": "",
                            "osm_match_name": "",
                            "osm_match_score": "",
                            "osm_id": "",
                            "osm_display_name": "",
                            "enrichment_source": "",
                            "google_rating": "",
                            "google_reviews": "",
                            "score": 0,
                            "priority": "",
                            "score_details": "",
                        })

                        if len(rows) >= args.max_prospects:
                            break

                    if len(rows) >= args.max_prospects:
                        break
                if len(rows) >= args.max_prospects:
                    break
            if len(rows) >= args.max_prospects:
                break
        if len(rows) >= args.max_prospects:
            break

    print(f"\n{len(rows)} établissements officiels trouvés.")
    print("\nEnrichissement contacts...")

    # 2) Enrichment
    for i, row in enumerate(rows, 1):
        print(f"[{i}/{len(rows)}] {row.get('name')} — {row.get('city')}")

        # Nominatim / OSM
        osm, osm_err = nominatim_business(row, args.nominatim_email)
        if osm:
            merge(row, osm, "Nominatim")
        elif osm_err:
            print(f"    ! {osm_err}")

        # Web search fallback
        if (
            not args.no_web_search
            and (
                not row.get("website")
                or not row.get("email")
                or not row.get("phone")
            )
        ):
            web = web_search_business(row)
            if web:
                web_data = {
                    "website": web.get("website", ""),
                    "email": web.get("email", ""),
                    "phone": web.get("phone", ""),
                }
                if web_data["email"]:
                    web_data["email_source"] = (
                        f"Search snippet ({web.get('search_source','web')})"
                    )
                merge(
                    row,
                    web_data,
                    f"Web:{web.get('search_source','')}" if web.get("search_source") else "Web"
                )

        # Site crawl
        if row.get("website"):
            site = crawl_site(row["website"])
            merge(row, site, "SiteCrawl")

        pts, prio, details = score(row)
        row["score"] = pts
        row["priority"] = prio
        row["score_details"] = details

        print(
            "    "
            f"site={'OK' if row.get('website') else '-'} | "
            f"mail={'OK' if row.get('email') else '-'} | "
            f"tel={'OK' if row.get('phone') else '-'} | "
            f"IG={'OK' if row.get('instagram') else '-'} | "
            f"source={row.get('enrichment_source') or '-'} | "
            f"score={pts}"
        )

    # 3) Export
    rows.sort(
        key=lambda x: (
            x.get("score", 0),
            bool(x.get("email")),
            bool(x.get("phone")),
            bool(x.get("website")),
        ),
        reverse=True,
    )

    fields = [
        "priority", "score", "zone", "keyword",
        "name", "legal_name", "address", "postcode", "city",
        "siret", "siren", "naf", "employees", "creation_date",
        "open_establishments", "total_establishments", "company_status",
        "website", "email", "email_source", "phone",
        "instagram", "facebook", "tiktok", "linkedin",
        "loyalty", "loyalty_evidence",
        "osm_match_name", "osm_match_score", "osm_id", "osm_display_name",
        "enrichment_source",
        "google_rating", "google_reviews",
        "latitude", "longitude", "score_details",
    ]

    with open(args.output, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)

    if args.push_sheets:
        print("\nEnvoi vers Google Sheets...")
        ok, msg = push_rows_to_google_sheets(
            rows,
            webhook_url=args.webhook_url,
            webhook_token=args.webhook_token,
        )
        if ok:
            print(f"Google Sheets : OK — {msg}")
        else:
            print(f"Google Sheets : ECHEC — {msg}")

    sites = sum(bool(x.get("website")) for x in rows)
    emails = sum(bool(x.get("email")) for x in rows)
    phones = sum(bool(x.get("phone")) for x in rows)
    insta = sum(bool(x.get("instagram")) for x in rows)
    aa = sum(x.get("priority") == "A" for x in rows)
    bb = sum(x.get("priority") == "B" for x in rows)

    print("\n" + "=" * 74)
    print(f"CSV créé : {args.output}")
    print(f"Prospects : {len(rows)} | A : {aa} | B : {bb}")
    print(
        f"Sites : {sites} | Emails : {emails} | "
        f"Téléphones : {phones} | Instagram : {insta}"
    )
    print("Google rating/reviews : non disponibles sans Google Places")
    print("=" * 74)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
