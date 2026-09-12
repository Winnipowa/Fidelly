#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fidelly Prospect Finder v7 Resilient.

Orchestrateur robuste autour du moteur v6 :
- conserve les expressions multi-mots via des entrées séparées par `|`;
- traite chaque ville indépendamment;
- répartit la découverte entre activités au lieu de laisser la première remplir le quota;
- isole les erreurs prospect par prospect;
- écrit le CSV au fil de l'eau;
- pousse Google Sheets par petits checkpoints avec retries exponentiels;
- s'arrête proprement avant le timeout GitHub au lieu d'être tué brutalement.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import sys
import time
import unicodedata
from pathlib import Path

import fidelly_prospect_finder_v6_cloud as v6


FIELDS = [
    "priority", "score", "zone", "keyword",
    "name", "legal_name", "address", "postcode", "city",
    "siret", "siren", "naf", "employees", "creation_date",
    "open_establishments", "total_establishments", "company_status",
    "website", "email", "email_source", "phone",
    "instagram", "facebook", "tiktok", "linkedin",
    "loyalty", "loyalty_evidence",
    "osm_match_name", "osm_match_score", "osm_id", "osm_display_name",
    "enrichment_source", "google_rating", "google_reviews",
    "latitude", "longitude", "score_details",
]

PLACEHOLDER_TOKENS = {
    "zone", "zones", "ville", "villes",
    "activité", "activités", "activite", "activites",
    "keyword", "keywords", "activity", "activities",
}


def norm(value):
    return str(value or "").strip()


def split_pipe(raw):
    items = []
    seen = set()
    for part in str(raw or "").split("|"):
        value = part.strip()
        if not value or value.lower() in PLACEHOLDER_TOKENS:
            continue
        key = value.casefold()
        if key not in seen:
            seen.add(key)
            items.append(value)
    return items


def slugify(value):
    text = unicodedata.normalize("NFKD", norm(value))
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    return text or "zone"


def empty_row(zone_name, keyword, item):
    return {
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
    }


def save_csv(rows, output_path):
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(
        rows,
        key=lambda x: (
            x.get("score", 0),
            bool(x.get("email")),
            bool(x.get("phone")),
            bool(x.get("website")),
        ),
        reverse=True,
    )
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(ordered)
    tmp.replace(path)


def discover_zone(zone, keywords, pages, max_prospects):
    """Découverte équilibrée entre activités pour une seule commune."""
    try:
        communes = v6.resolve_zone(zone)
    except Exception as exc:
        print(f"! Commune {zone}: {exc}", flush=True)
        return zone, []

    if not communes:
        print(f"! Commune introuvable: {zone}", flush=True)
        return zone, []

    commune = communes[0]
    zone_name = commune.get("nom", zone)
    postcodes = commune.get("codesPostaux") or []
    print(f"=== {zone_name} : {', '.join(postcodes)} ===", flush=True)

    # On collecte un petit vivier par activité, puis on entrelace les résultats.
    # Cela évite qu'une activité très dense (restaurants) remplisse tout le quota.
    per_keyword_target = max(6, math.ceil(max_prospects / max(1, len(keywords))) + 4)
    pools = []

    for keyword in keywords:
        pool = []
        local_seen = set()
        naf_codes = v6.NAF_MAP.get(keyword.lower(), [])
        modes = naf_codes or [None]

        for cp in postcodes[:3]:
            for naf in modes:
                label = f"NAF {naf}" if naf else "texte"
                print(f"  -> {keyword} | {cp} | {label}", flush=True)
                try:
                    iterator = v6.gov_search(cp, keyword, naf, pages)
                    for item in iterator:
                        key = item.get("siret") or (item.get("name"), item.get("address"))
                        if not key or key in local_seen:
                            continue
                        local_seen.add(key)
                        pool.append(empty_row(zone_name, keyword, item))
                        if len(pool) >= per_keyword_target:
                            break
                except Exception as exc:
                    print(f"    ! découverte {keyword}: {exc}", flush=True)
                if len(pool) >= per_keyword_target:
                    break
            if len(pool) >= per_keyword_target:
                break
        pools.append(pool)

    rows = []
    global_seen = set()
    round_index = 0
    while len(rows) < max_prospects:
        added = False
        for pool in pools:
            if round_index >= len(pool):
                continue
            item = pool[round_index]
            key = item.get("siret") or (item.get("name"), item.get("address"))
            if key and key not in global_seen:
                global_seen.add(key)
                rows.append(item)
                added = True
                if len(rows) >= max_prospects:
                    break
        if not added:
            break
        round_index += 1

    print(f"{len(rows)} établissements retenus pour {zone_name}.\n", flush=True)
    return zone_name, rows


def enrich_one(row, no_web_search=False, nominatim_email=""):
    """Chaque source est isolée : une panne n'arrête jamais le prospect suivant."""
    try:
        osm, osm_err = v6.nominatim_business(row, nominatim_email)
        if osm:
            v6.merge(row, osm, "Nominatim")
        elif osm_err:
            print(f"    ! {osm_err}", flush=True)
    except Exception as exc:
        print(f"    ! Nominatim: {exc}", flush=True)

    if not no_web_search and (
        not row.get("website") or not row.get("email") or not row.get("phone")
    ):
        try:
            web = v6.web_search_business(row)
            if web:
                data = {
                    "website": web.get("website", ""),
                    "email": web.get("email", ""),
                    "phone": web.get("phone", ""),
                }
                if data["email"]:
                    data["email_source"] = f"Search snippet ({web.get('search_source', 'web')})"
                source = f"Web:{web.get('search_source', '')}" if web.get("search_source") else "Web"
                v6.merge(row, data, source)
        except Exception as exc:
            print(f"    ! Web search: {exc}", flush=True)

    if row.get("website"):
        try:
            v6.merge(row, v6.crawl_site(row["website"]), "SiteCrawl")
        except Exception as exc:
            print(f"    ! Site crawl: {exc}", flush=True)

    try:
        pts, prio, details = v6.score(row)
    except Exception as exc:
        print(f"    ! Scoring: {exc}", flush=True)
        pts, prio, details = 0, "C", f"scoring error: {exc}"
    row["score"] = pts
    row["priority"] = prio
    row["score_details"] = details
    return row


def push_with_retry(rows, webhook_url, webhook_token, attempts=5):
    if not rows:
        return True, "aucune ligne"
    last = ""
    for attempt in range(1, attempts + 1):
        ok, msg = v6.push_rows_to_google_sheets(
            rows,
            webhook_url=webhook_url,
            webhook_token=webhook_token,
        )
        if ok:
            return True, msg
        last = msg
        if attempt < attempts:
            wait = min(45, 3 * (2 ** (attempt - 1)))
            print(f"    Sheets tentative {attempt}/{attempts} échouée: {msg}; retry dans {wait}s", flush=True)
            time.sleep(wait)
    return False, last


def pending_path(output_path):
    return str(Path(output_path).with_suffix(".pending.json"))


def save_pending(rows, output_path):
    path = Path(pending_path(output_path))
    if rows:
        path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    elif path.exists():
        path.unlink()


def main():
    parser = argparse.ArgumentParser(description="Fidelly Prospect Finder v7 Resilient")
    parser.add_argument("--zones-pipe", required=True, help="Villes séparées par |")
    parser.add_argument("--keywords-pipe", required=True, help="Activités séparées par |")
    parser.add_argument("--pages", type=int, default=2)
    parser.add_argument("--max-prospects-per-zone", type=int, default=50)
    parser.add_argument("--checkpoint-every", type=int, default=5)
    parser.add_argument("--max-runtime-minutes", type=int, default=160)
    parser.add_argument("--output", default="fidelly_prospects.csv")
    parser.add_argument("--push-sheets", action="store_true")
    parser.add_argument("--webhook-url", default="")
    parser.add_argument("--webhook-token", default="")
    parser.add_argument("--nominatim-email", default="")
    parser.add_argument("--no-web-search", action="store_true")
    args = parser.parse_args()

    zones = split_pipe(args.zones_pipe)
    keywords = split_pipe(args.keywords_pipe)
    if not zones:
        print("Aucune ville valide.", file=sys.stderr)
        return 2
    if not keywords:
        print("Aucune activité valide.", file=sys.stderr)
        return 2

    webhook_url = args.webhook_url or os.getenv("SHEETS_WEBHOOK_URL", "")
    webhook_token = args.webhook_token or os.getenv("SHEETS_WEBHOOK_TOKEN", "")
    started = time.monotonic()
    deadline = started + max(5, args.max_runtime_minutes) * 60
    completed_all = []
    pending = []
    sheet_ok_once = False

    print(
        f"[Fidelly v7 RESILIENT] zones={len(zones)} | activités={len(keywords)} | "
        f"max/zone={args.max_prospects_per_zone} | checkpoint={args.checkpoint_every}",
        flush=True,
    )

    for zone in zones:
        if time.monotonic() >= deadline:
            print("Temps de sécurité atteint avant une nouvelle ville; arrêt propre.", flush=True)
            break

        zone_name, candidates = discover_zone(
            zone, keywords, args.pages, args.max_prospects_per_zone
        )
        print(f"Enrichissement {zone_name}...", flush=True)

        for index, row in enumerate(candidates, 1):
            # Garde une marge pour checkpoint / artifact avant le timeout du runner.
            if time.monotonic() >= deadline:
                print("Temps de sécurité atteint; sauvegarde immédiate du travail acquis.", flush=True)
                break

            print(f"[{index}/{len(candidates)}] {row.get('name')} — {row.get('city')}", flush=True)
            try:
                enrich_one(row, args.no_web_search, args.nominatim_email)
            except Exception as exc:
                # Ultime filet : même un bug inattendu sur une fiche ne stoppe pas le job.
                row["score"] = row.get("score") or 0
                row["priority"] = row.get("priority") or "C"
                row["score_details"] = f"unexpected enrichment error: {exc}"
                print(f"    ! erreur prospect isolée: {exc}", flush=True)

            completed_all.append(row)
            pending.append(row)
            save_csv(completed_all, args.output)

            print(
                "    "
                f"site={'OK' if row.get('website') else '-'} | "
                f"mail={'OK' if row.get('email') else '-'} | "
                f"tel={'OK' if row.get('phone') else '-'} | "
                f"IG={'OK' if row.get('instagram') else '-'} | "
                f"score={row.get('score', 0)}",
                flush=True,
            )

            if args.push_sheets and len(pending) >= max(1, args.checkpoint_every):
                ok, msg = push_with_retry(pending, webhook_url, webhook_token)
                if ok:
                    print(f"    Sheets checkpoint OK — {msg}", flush=True)
                    pending.clear()
                    save_pending([], args.output)
                    sheet_ok_once = True
                else:
                    print(f"    Sheets checkpoint différé — {msg}", flush=True)
                    save_pending(pending, args.output)

        # On pousse aussi la fin de ville, même si le checkpoint n'est pas plein.
        if args.push_sheets and pending:
            ok, msg = push_with_retry(pending, webhook_url, webhook_token)
            if ok:
                print(f"Sheets fin de ville OK — {msg}", flush=True)
                pending.clear()
                save_pending([], args.output)
                sheet_ok_once = True
            else:
                print(f"Sheets fin de ville différée — {msg}", flush=True)
                save_pending(pending, args.output)

    save_csv(completed_all, args.output)
    save_pending(pending, args.output)

    sites = sum(bool(x.get("website")) for x in completed_all)
    emails = sum(bool(x.get("email")) for x in completed_all)
    phones = sum(bool(x.get("phone")) for x in completed_all)
    insta = sum(bool(x.get("instagram")) for x in completed_all)
    aa = sum(x.get("priority") == "A" for x in completed_all)
    bb = sum(x.get("priority") == "B" for x in completed_all)

    print("\n" + "=" * 74)
    print(f"CSV : {args.output}")
    print(f"Prospects traités : {len(completed_all)} | A : {aa} | B : {bb}")
    print(f"Sites : {sites} | Emails : {emails} | Téléphones : {phones} | Instagram : {insta}")
    if pending:
        print(f"ATTENTION : {len(pending)} lignes restent dans {pending_path(args.output)}")
    elif args.push_sheets:
        print("Google Sheets : checkpoints vidés avec succès" if sheet_ok_once else "Google Sheets : aucune ligne à pousser")
    print("=" * 74)

    # Une panne d'enrichissement externe ne doit pas casser la chaîne.
    # Les lignes non poussées restent disponibles dans l'artifact .pending.json.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
