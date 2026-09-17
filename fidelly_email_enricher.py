#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fidelly autonomous email enricher.

Scans existing prospects whose email is blank, searches public sources,
validates identity, and writes only sufficiently reliable emails.

Preferred webhook actions (see apps_script/email_enrichment_patch.gs):
  - list_missing_emails
  - patch_email

If list_missing_emails is not deployed yet, the bot falls back to rediscovering
existing prospects in configured zones and uses the existing V8/V9 snapshot.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import socket
import time
import unicodedata
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

import fidelly_prospect_finder_v6_cloud as v6
import fidelly_prospect_finder_v8_smart as core

VERSION = "1.0"
SOURCE_NAME = "fidelly-email-enricher"
DEFAULT_ZONES = (
    "Issoudun|Bourges|Châteauroux|Vierzon|Romorantin-Lanthenay|"
    "Saint-Amand-Montrond|Lyon"
)
DEFAULT_KEYWORDS = (
    "restaurant|café|bar|caviste|coiffeur|institut de beauté|boulangerie|"
    "pâtisserie|fleuriste|spa|animalerie|épicerie|primeur|boucherie|"
    "poissonnerie|librairie|magasin de vêtements|chaussures|bijouterie|"
    "opticien|photographe|salle de sport|tatoueur|toilettage"
)

EMAIL_RE = re.compile(r"(?i)(?<![\w.+-])([A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,})(?![\w.-])")
GENERIC_MAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "orange.fr", "wanadoo.fr", "hotmail.fr",
    "hotmail.com", "outlook.fr", "outlook.com", "live.fr", "yahoo.fr",
    "yahoo.com", "laposte.net", "free.fr", "sfr.fr", "icloud.com",
}
BAD_EMAIL_PARTS = (
    "example.", "sentry.", "wixpress.", "wordpress.", "yourdomain.",
    ".png", ".jpg", ".jpeg", ".svg", ".webp", ".gif", ".css", ".js",
    "noreply@", "no-reply@", "donotreply@", "do-not-reply@",
)
BAD_EXACT = {
    "l@imes.com", "world@las.com", "rottentom@oes.com", "d@a.gouv.fr",
    "timeandd@e.com", "abbrevi@ionfinder.org",
}
DIRECTORY_HINTS = (
    "pagesjaunes.", "tripadvisor.", "yelp.", "justacote.", "petitfute.",
    "mappy.", "allbiz.", "annuaire", "telephone.city", "uneboulangerie.",
    "bureautabac.", "icoiffeur.", "salonsmassage.", "horairesdouverture",
    "ubereats.", "deliveroo.", "thefork.", "societe.com", "pappers.fr",
    "entreprises.lefigaro.fr",
)
SOCIAL_HINTS = ("facebook.com", "instagram.com", "linkedin.com", "tiktok.com")
CONTACT_HINTS = (
    "contact", "nous-contacter", "contactez-nous", "mentions-legales",
    "mentions légales", "legal", "cgv", "cgu", "a-propos", "qui-sommes-nous",
)

SESSION = requests.Session()
SESSION.headers.update(v6.BROWSER_HEADERS)


@dataclass
class Candidate:
    email: str
    source: str
    score: int
    reason: str
    source_kind: str


def norm(v: Any) -> str:
    return str(v or "").strip()


def ascii_norm(v: Any) -> str:
    s = unicodedata.normalize("NFKD", norm(v).lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())


def domain(v: str) -> str:
    v = norm(v)
    if not v:
        return ""
    if "@" in v and "://" not in v:
        return v.rsplit("@", 1)[-1].lower().strip(".")
    try:
        host = urlparse(v if "://" in v else "https://" + v).netloc.lower()
        return host[4:] if host.startswith("www.") else host
    except Exception:
        return ""


def sane_email(email: str) -> bool:
    e = v6.clean_email(email)
    if not e or e in BAD_EXACT or "@" not in e:
        return False
    if any(x in e.lower() for x in BAD_EMAIL_PARTS):
        return False
    d = domain(e)
    return bool(d and "." in d and len(e) <= 254)


def domain_resolves(email: str) -> bool:
    d = domain(email)
    if not d:
        return False
    try:
        import dns.resolver  # type: ignore
        return bool(list(dns.resolver.resolve(d, "MX", lifetime=4.0)))
    except Exception:
        try:
            socket.getaddrinfo(d, 443)
            return True
        except Exception:
            return False


def name_tokens(row: dict[str, Any]) -> list[str]:
    text = ascii_norm(row.get("name") or row.get("legal_name"))
    stop = {
        "sarl", "sas", "sasu", "eurl", "societe", "france", "lyon",
        "restaurant", "cafe", "bar", "coiffure", "institut", "boulangerie",
    }
    return [w for w in text.split() if len(w) >= 4 and w not in stop][:6]


def identity_score(row: dict[str, Any], text: str, url: str) -> tuple[int, list[str]]:
    hay = ascii_norm(text + " " + url)
    digits = re.sub(r"\D", "", text)
    pts, why = 0, []
    siret = re.sub(r"\D", "", norm(row.get("siret")))
    siren = re.sub(r"\D", "", norm(row.get("siren")))
    if siret and siret in digits:
        pts += 42
        why.append("SIRET")
    elif siren and siren in digits:
        pts += 28
        why.append("SIREN")

    tokens = name_tokens(row)
    matched = sum(t in hay for t in tokens)
    if tokens and matched >= min(2, len(tokens)):
        pts += 24
        why.append("nom")
    elif matched:
        pts += 12
        why.append("nom partiel")

    city = ascii_norm(row.get("city") or row.get("zone"))
    if city and city in hay:
        pts += 10
        why.append("ville")
    postcode = re.sub(r"\D", "", norm(row.get("postcode")))
    if postcode and postcode in text:
        pts += 8
        why.append("CP")

    address_words = [w for w in ascii_norm(row.get("address")).split() if len(w) >= 5][:4]
    if address_words and sum(w in hay for w in address_words) >= min(2, len(address_words)):
        pts += 10
        why.append("adresse")
    return pts, why


def classify_source(url: str, row: dict[str, Any]) -> str:
    d = domain(url)
    known = domain(norm(row.get("website")))
    if known and (d == known or d.endswith("." + known)):
        return "official"
    if any(x in d for x in SOCIAL_HINTS):
        return "social"
    if any(x in d for x in DIRECTORY_HINTS):
        return "directory"
    return "web"


def make_candidate(
    row: dict[str, Any], email: str, source: str, text: str, mailto: bool = False
) -> Candidate | None:
    email = v6.clean_email(email)
    if not sane_email(email):
        return None

    kind = classify_source(source, row)
    pts, why = identity_score(row, text, source)
    if kind == "official":
        pts += 48
        why.append("site officiel")
    elif kind == "directory":
        pts += 10
        why.append("annuaire")
    elif kind == "social":
        pts += 8
        why.append("social")
    else:
        pts += 14
        why.append("web")

    source_domain = domain(source)
    email_domain = domain(email)
    known_domain = domain(norm(row.get("website")))
    if source_domain and email_domain == source_domain:
        pts += 28
        why.append("domaine=source")
    elif known_domain and email_domain == known_domain:
        pts += 28
        why.append("domaine=site")
    elif email_domain in GENERIC_MAIL_DOMAINS:
        pts += 2
        why.append("messagerie générique")
    if mailto:
        pts += 6
        why.append("mailto")

    if domain_resolves(email):
        pts += 7
        why.append("domaine joignable")
    else:
        pts -= 18
        why.append("domaine non vérifié")

    if kind in {"directory", "social"} and pts < 70:
        return None
    return Candidate(email, source, max(0, min(100, pts)), ", ".join(why), kind)


def extract_page(
    row: dict[str, Any], url: str, timeout: int
) -> tuple[list[Candidate], list[str]]:
    try:
        r = SESSION.get(url, timeout=timeout, allow_redirects=True)
        if r.status_code >= 400 or "html" not in r.headers.get("content-type", "").lower():
            return [], []
    except requests.RequestException:
        return [], []

    body = r.text[:2_000_000]
    soup = BeautifulSoup(body, "html.parser")
    visible = soup.get_text(" ", strip=True)
    identity_text = visible + " " + body[:150_000]

    mailtos = set()
    for a in soup.select('a[href^="mailto:"]'):
        e = v6.clean_email(a.get("href", "")[7:].split("?", 1)[0])
        if sane_email(e):
            mailtos.add(e)

    emails = []
    for blob in (body, visible, v6.deobfuscate(visible)):
        for e in EMAIL_RE.findall(blob):
            e = v6.clean_email(e)
            if sane_email(e) and e not in emails:
                emails.append(e)

    candidates = []
    for e in emails[:12]:
        c = make_candidate(row, e, r.url, identity_text, e in mailtos)
        if c:
            candidates.append(c)

    links, seen = [], set()
    for a in soup.select("a[href]"):
        full = urljoin(r.url, a.get("href", "")).split("#", 1)[0]
        label = ascii_norm(a.get_text(" ", strip=True) + " " + full)
        if domain(full) == domain(r.url) and any(ascii_norm(h) in label for h in CONTACT_HINTS):
            if full not in seen:
                seen.add(full)
                links.append(full)
    for p in ("/contact", "/nous-contacter", "/mentions-legales", "/cgv"):
        full = urljoin(r.url, p)
        if full not in seen:
            seen.add(full)
            links.append(full)
    return candidates, links[:7]


def parse_results(html: str, engine: str) -> list[tuple[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    out = []
    if engine == "ddg":
        for block in soup.select(".result")[:10]:
            a = block.select_one("a.result__a")
            if not a:
                continue
            href = a.get("href", "")
            if "uddg=" in href:
                href = unquote(parse_qs(urlparse(href).query).get("uddg", [""])[0])
            if href.startswith("http"):
                out.append((href, block.get_text(" ", strip=True)))
    elif engine == "bing":
        for block in soup.select("li.b_algo")[:10]:
            a = block.select_one("h2 a")
            if a and a.get("href", "").startswith("http"):
                out.append((a.get("href", ""), block.get_text(" ", strip=True)))
    elif engine == "brave":
        for a in soup.select('a[href^="http"]')[:70]:
            text = a.parent.get_text(" ", strip=True) if a.parent else a.get_text(" ", strip=True)
            out.append((a.get("href", ""), text))

    dedup, seen = [], set()
    for url, text in out:
        key = url.split("#", 1)[0]
        if key not in seen:
            seen.add(key)
            dedup.append((url, text))
    return dedup[:10]


def web_results(row: dict[str, Any], timeout: int) -> list[tuple[str, str]]:
    name = norm(row.get("name") or row.get("legal_name"))
    city = norm(row.get("city") or row.get("zone"))
    postcode = norm(row.get("postcode"))
    siret = norm(row.get("siret"))
    queries = [
        f'"{name}" "{city}" email',
        f'"{name}" "{city}" contact',
        f'"{name}" "{postcode}"',
    ]
    if siret:
        queries.append(f'"{siret}" email')

    engines = [
        ("ddg", v6.DDG_HTML_URL, {}),
        ("bing", v6.BING_URL, {"setlang": "fr-FR"}),
        ("brave", v6.BRAVE_URL, {"source": "web"}),
    ]
    out, seen = [], set()
    for query in queries:
        for engine, endpoint, extra in engines:
            try:
                r = SESSION.get(endpoint, params={"q": query, **extra}, timeout=timeout)
                if r.status_code != 200:
                    continue
                for url, text in parse_results(r.text, engine):
                    if url not in seen:
                        seen.add(url)
                        out.append((url, text))
                if len(out) >= 12:
                    return out[:12]
            except requests.RequestException:
                continue
    return out[:12]


def enrich(row: dict[str, Any], min_score: int, timeout: int) -> Candidate | None:
    candidates, visited = [], set()
    website = norm(row.get("website"))
    if website and not v6.is_directory(website):
        queue = [v6.ensure_url(website)]
        while queue and len(visited) < 7:
            url = queue.pop(0)
            if url in visited:
                continue
            visited.add(url)
            found, links = extract_page(row, url, timeout)
            candidates.extend(found)
            queue.extend(x for x in links if x not in visited)

    if not candidates or max(c.score for c in candidates) < min_score:
        for url, snippet in web_results(row, timeout):
            for e in EMAIL_RE.findall(snippet):
                c = make_candidate(row, e, url, snippet)
                if c:
                    candidates.append(c)
            if url not in visited:
                visited.add(url)
                found, links = extract_page(row, url, timeout)
                candidates.extend(found)
                for link in links[:3]:
                    if link not in visited:
                        visited.add(link)
                        more, _ = extract_page(row, link, timeout)
                        candidates.extend(more)
            if candidates and max(c.score for c in candidates) >= 96:
                break

    if not candidates:
        return None
    grouped = {}
    for c in candidates:
        grouped.setdefault(c.email.lower(), []).append(c)
    ranked = []
    for group in grouped.values():
        best = max(group, key=lambda x: x.score)
        domains = {domain(x.source) for x in group if domain(x.source)}
        bonus = min(8, max(0, len(domains) - 1) * 4)
        ranked.append(
            Candidate(best.email, best.source, min(100, best.score + bonus), best.reason, best.source_kind)
        )
    ranked.sort(key=lambda c: (c.score, c.source_kind == "official"), reverse=True)
    return ranked[0] if ranked[0].score >= min_score else None


def sheet_call(payload: dict[str, Any], attempts: int = 3):
    return core.sheet_call(
        os.getenv("SHEETS_WEBHOOK_URL", ""),
        os.getenv("SHEETS_WEBHOOK_TOKEN", ""),
        payload,
        attempts=attempts,
        timeout=75,
    )


def list_missing(limit: int):
    # Le point de départ tourne toutes les 3 h pour ne pas retraiter toujours
    # les mêmes lignes difficiles. Le patch Apps Script ramène ce curseur
    # dans les bornes réelles du Sheet.
    rotating_cursor = int(time.time() // (3 * 3600)) * max(1, limit)
    ok, result = sheet_call(
        {"action": "list_missing_emails", "limit": max(1, min(200, limit)), "cursor": rotating_cursor},
        attempts=2,
    )
    if ok and isinstance(result, dict):
        return result.get("rows") or [], "sheet"
    return [], str(result)


def snapshot(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    sirets = [norm(r.get("siret")) for r in rows if norm(r.get("siret"))]
    out = {}
    for start in range(0, len(sirets), 150):
        ok, result = sheet_call(
            {"action": "snapshot", "sirets": sirets[start:start + 150], "touch": False},
            attempts=3,
        )
        if ok and isinstance(result, dict):
            for row in result.get("rows") or []:
                siret = norm(row.get("siret"))
                if siret:
                    out[siret] = row
    return out


def fallback(zones_pipe: str, keywords_pipe: str, pages: int) -> list[dict[str, Any]]:
    candidates = {}
    for zone in core.split_pipe(zones_pipe):
        try:
            communes = v6.resolve_zone(zone)
        except Exception:
            continue
        if not communes:
            continue
        commune = communes[0]
        zone_name = commune.get("nom", zone)
        for postcode in (commune.get("codesPostaux") or [])[:3]:
            for keyword in core.split_pipe(keywords_pipe):
                for naf in (v6.NAF_MAP.get(keyword.lower(), []) or [None]):
                    try:
                        for item in v6.gov_search(postcode, keyword, naf, max(1, pages)):
                            siret = norm(item.get("siret"))
                            if siret and siret not in candidates:
                                candidates[siret] = {"zone": zone_name, "keyword": keyword, **item}
                    except Exception:
                        continue
    existing = snapshot(list(candidates.values()))
    return [r for r in existing.values() if norm(r.get("siret")) and not norm(r.get("email"))]


def safe_write(row: dict[str, Any], candidate: Candidate) -> tuple[bool, str]:
    siret = norm(row.get("siret"))
    if not siret:
        return False, "SIRET absent"

    latest = snapshot([{"siret": siret}]).get(siret)
    if latest and norm(latest.get("email")):
        return False, "déjà rempli"

    ok, result = sheet_call(
        {
            "action": "patch_email",
            "updates": [{
                "siret": siret,
                "email": candidate.email,
                "email_source": candidate.source,
                "confidence": candidate.score,
                "reason": candidate.reason,
            }],
        },
        attempts=2,
    )
    if ok and isinstance(result, dict):
        if int(result.get("written", 0) or 0):
            return True, "patch_email"
        if int(result.get("skipped_existing", 0) or 0):
            return False, "déjà rempli (lock)"

    # Temporary compatibility with the current V8/V9 webhook until the atomic
    # Apps Script patch is deployed. Re-snapshot immediately before partial upsert.
    latest = snapshot([{"siret": siret}]).get(siret)
    if latest and norm(latest.get("email")):
        return False, "déjà rempli avant fallback"
    ok2, msg = core.push_rows(
        [{"siret": siret, "email": candidate.email, "email_source": candidate.source}],
        os.getenv("SHEETS_WEBHOOK_URL", ""),
        os.getenv("SHEETS_WEBHOOK_TOKEN", ""),
    )
    return ok2, "legacy upsert" if ok2 else str(msg)


def save(report: dict[str, Any], path: str):
    Path(path).write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def main() -> int:
    p = argparse.ArgumentParser(description="Fidelly autonomous email enricher")
    p.add_argument("--batch-size", type=int, default=40)
    p.add_argument("--min-confidence", type=int, default=80)
    p.add_argument("--timeout", type=int, default=10)
    p.add_argument("--max-runtime-minutes", type=int, default=50)
    p.add_argument("--fallback-zones", default=DEFAULT_ZONES)
    p.add_argument("--fallback-keywords", default=DEFAULT_KEYWORDS)
    p.add_argument("--fallback-pages", type=int, default=2)
    p.add_argument("--report", default="fidelly_email_enrichment_report.json")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    deadline = time.monotonic() + max(5, args.max_runtime_minutes) * 60
    min_score = max(65, min(100, args.min_confidence))
    batch = max(1, min(150, args.batch_size))

    rows, mode = list_missing(batch)
    if mode != "sheet":
        print("list_missing_emails indisponible -> fallback de redécouverte", flush=True)
        rows = fallback(args.fallback_zones, args.fallback_keywords, args.fallback_pages)[:batch]
        mode = "fallback"

    report = {
        "version": VERSION,
        "source": SOURCE_NAME,
        "started_at": core.utc_now_iso(),
        "mode": mode,
        "scanned": 0,
        "found": 0,
        "written": 0,
        "skipped": 0,
        "results": [],
    }
    rows.sort(
        key=lambda r: (bool(norm(r.get("website"))), bool(norm(r.get("phone")))),
        reverse=True,
    )

    for i, row in enumerate(rows, 1):
        if time.monotonic() >= deadline:
            break
        if norm(row.get("email")):
            continue
        report["scanned"] += 1
        print(
            f"[{i}/{len(rows)}] {row.get('name')} — {row.get('city')} — {row.get('siret')}",
            flush=True,
        )
        try:
            candidate = enrich(row, min_score, max(5, args.timeout))
        except Exception as exc:
            print(f"  erreur isolée: {exc}", flush=True)
            continue
        if not candidate:
            print("  aucun email assez fiable", flush=True)
            continue

        report["found"] += 1
        entry = {
            "siret": norm(row.get("siret")),
            "name": norm(row.get("name")),
            "city": norm(row.get("city")),
            **asdict(candidate),
        }
        if args.dry_run:
            entry["write"] = "dry-run"
        else:
            ok, message = safe_write(row, candidate)
            entry["write"] = message
            if ok:
                report["written"] += 1
            else:
                report["skipped"] += 1
        report["results"].append(entry)
        save(report, args.report)
        time.sleep(0.5)

    report["finished_at"] = core.utc_now_iso()
    save(report, args.report)
    print(
        f"done scanned={report['scanned']} found={report['found']} written={report['written']}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
