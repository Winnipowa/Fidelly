#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fidelly Prospect Finder V9.2 Fast Discovery.

Objectif:
- trouver les nouveaux prospects le plus vite possible ;
- les enregistrer immédiatement dans Google Sheets avant enrichissement ;
- ne pas perdre du temps à enrichir les profils peu intéressants ;
- enrichir en parallèle avec des timeouts courts ;
- conserver le mode 732 activités NAF de V9.1.

Le moteur de découverte et les extracteurs restent compatibles avec V9.1/V8/V6.
"""
from __future__ import annotations

import argparse
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import requests

import fidelly_prospect_finder_v9_new_first as v91

core = v91.core
VERSION = "9.2"
SOURCE_NAME = "fidelly-prospect-finder-v9.2-fast"

# Activités généralement peu pertinentes pour une prospection commerciale locale.
# Elles sont quand même découvertes et enregistrées, mais pas enrichies par défaut.
SKIP_EXACT_NAF = {
    "53.10Z",  # activités de poste dans le cadre d'une obligation de service universel
    "64.20Z",  # activités des sociétés holding
    "64.30Z",  # fonds de placement et entités financières similaires
}
SKIP_DIVISIONS = {"84", "94", "97", "98", "99"}

# Secteurs particulièrement intéressants pour de la prospection B2B/local commerce.
HIGH_VALUE_DIVISIONS = {
    "43",  # construction spécialisée
    "45", "46", "47",  # commerce
    "55", "56",  # hébergement/restauration
    "68", "69", "70", "71", "73", "74", "75",  # services pros
    "78", "79", "80", "81",  # RH/tourisme/sécurité/services
    "85", "86", "88",  # formation/santé/social
    "93", "95", "96",  # sport/réparation/services personnels
}


def commercial_tier(row: dict[str, Any]) -> str:
    """A = priorité haute, B = normale, C = conserver sans enrichir."""
    naf = core.norm(row.get("naf")).upper()
    division = naf[:2]
    if naf in SKIP_EXACT_NAF or division in SKIP_DIVISIONS:
        return "C"
    if division in HIGH_VALUE_DIVISIONS:
        return "A"
    return "B"


def mark_raw(row: dict[str, Any]) -> dict[str, Any]:
    tier = commercial_tier(row)
    row["priority"] = tier
    row["score"] = 0
    row["score_details"] = f"fast-tier:{tier}"
    row["enrichment_state"] = "DISCOVERED"
    row["enrichment_version"] = VERSION
    row["last_enriched_at"] = ""
    row["last_error"] = ""
    return row


def fast_web_search_business(row: dict[str, Any], timeout: int = 7) -> dict[str, str]:
    """Recherche courte: une requête, deux moteurs max, arrêt dès qu'un site est trouvé."""
    v6 = core.v6
    name = row.get("name") or row.get("legal_name")
    city = row.get("city") or row.get("zone")
    postcode = row.get("postcode")
    if not core.norm(name):
        return {"website": "", "email": "", "phone": "", "search_source": ""}

    q = f'"{name}" "{city}" {postcode}'.strip()
    engines = [
        ("ddg", v6.DDG_HTML_URL),
        ("bing", v6.BING_URL),
    ]
    best = {"website": "", "email": "", "phone": "", "search_source": ""}

    for engine, url in engines:
        try:
            params = {"q": q}
            if engine == "bing":
                params["setlang"] = "fr-FR"
            r = requests.get(
                url,
                params=params,
                headers=v6.BROWSER_HEADERS,
                timeout=max(3, int(timeout)),
            )
            if r.status_code != 200:
                continue
            parsed = v6.parse_generic_search(r.text, name, city, engine)
            for field in ("website", "email", "phone"):
                if parsed.get(field) and not best[field]:
                    best[field] = parsed[field]
                    best["search_source"] = best["search_source"] or engine

            # Le site officiel est la meilleure porte d'entrée: le crawl prendra la suite.
            if best["website"] or (best["email"] and best["phone"]):
                break
        except requests.RequestException:
            continue
        except Exception:
            continue

    return best


def fast_enrich_one(
    row: dict[str, Any],
    timeout: int = 7,
    no_web_search: bool = False,
) -> tuple[dict[str, Any], list[str]]:
    """Enrichissement rapide sans Nominatim, conçu pour être parallélisé."""
    v6 = core.v6
    errors: list[str] = []

    if not no_web_search and (
        not row.get("website") or not row.get("email") or not row.get("phone")
    ):
        try:
            web = fast_web_search_business(row, timeout=timeout)
            if web:
                data = {
                    "website": web.get("website", ""),
                    "email": web.get("email", ""),
                    "phone": web.get("phone", ""),
                }
                if data["email"]:
                    data["email_source"] = f"Fast search ({web.get('search_source') or 'web'})"
                source = f"FastWeb:{web.get('search_source', '')}" if web.get("search_source") else "FastWeb"
                v6.merge(row, data, source)
        except Exception as exc:
            errors.append(f"Fast web: {exc}")

    # Le crawl du site est généralement plus rentable que 6-8 requêtes moteurs.
    if row.get("website") and (
        not row.get("email")
        or not row.get("phone")
        or not row.get("instagram")
        or row.get("loyalty") != "yes"
    ):
        try:
            v6.merge(row, v6.crawl_site(row["website"]), "FastSiteCrawl")
        except Exception as exc:
            errors.append(f"Site crawl: {exc}")

    try:
        pts, prio, details = v6.score(row)
    except Exception as exc:
        errors.append(f"Scoring: {exc}")
        pts, prio, details = 0, commercial_tier(row), f"scoring error: {exc}"

    row["score"] = pts
    row["priority"] = prio
    row["score_details"] = details
    row["enrichment_state"] = core.determine_state(row)
    row["enrichment_version"] = VERSION
    row["last_enriched_at"] = core.utc_now_iso()
    row["last_error"] = " | ".join(errors)[:1500]
    return row, errors


def _snapshot_new(
    batch: list[dict[str, Any]],
    push_sheets: bool,
    url: str,
    token: str,
) -> tuple[list[dict[str, Any]], int, bool, str]:
    if not batch:
        return [], 0, True, "empty"

    if not push_sheets:
        return [mark_raw(x) for x in batch], 0, True, "sans Google Sheets"

    existing, msg = core.fetch_existing(batch, url, token, touch=False)
    ok = msg.endswith("existants chargés")
    if not ok:
        return [], 0, False, msg

    new_rows: list[dict[str, Any]] = []
    known = 0
    for row in batch:
        siret = core.norm(row.get("siret"))
        if siret and siret in existing:
            known += 1
            continue
        new_rows.append(mark_raw(row))
    return new_rows, known, True, msg


def discover_new_all_businesses_fast(
    zone: str,
    categories: dict[str, dict],
    target_new: int,
    max_per_naf: int,
    push_sheets: bool,
    url: str,
    token: str,
    deadline: float,
    snapshot_batch: int = 40,
) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    """Scan NAF progressif: vérifie les doublons par lots et s'arrête dès la cible atteinte."""
    try:
        communes = core.v6.resolve_zone(zone)
    except Exception as exc:
        print(f"! Commune {zone}: {exc}", flush=True)
        return zone, [], {"error": str(exc)}

    if not communes:
        return zone, [], {"error": "commune introuvable"}

    commune = communes[0]
    zone_name = commune.get("nom", zone)
    postcodes = commune.get("codesPostaux") or []
    ordered_codes = v91.balanced_naf_codes(categories)
    max_per_naf = max(1, int(max_per_naf))
    snapshot_batch = max(10, int(snapshot_batch))

    print(
        f"=== {zone_name} | FAST DISCOVERY | {len(categories)} codes NAF | cible {target_new} nouveaux ===",
        flush=True,
    )

    new_rows: list[dict[str, Any]] = []
    buffer: list[dict[str, Any]] = []
    seen = set()
    stats = {
        "codes_scanned": 0,
        "raw_candidates": 0,
        "known_seen": 0,
        "registered": 0,
        "snapshot_failed": False,
    }

    def flush_buffer() -> bool:
        nonlocal buffer
        if not buffer or len(new_rows) >= target_new:
            buffer = []
            return True

        remaining = target_new - len(new_rows)
        fresh, known, ok, msg = _snapshot_new(buffer, push_sheets, url, token)
        stats["known_seen"] += known
        buffer = []
        if not ok:
            print(f"Snapshot Sheets impossible: {msg}", flush=True)
            stats["snapshot_failed"] = True
            return False

        fresh = fresh[:remaining]
        if fresh:
            if push_sheets:
                synced, failed = v91.push_batches(fresh, url, token, batch_size=100)
                stats["registered"] += synced
                if failed:
                    print(f"! {len(failed)} nouveaux non enregistrés immédiatement", flush=True)
            new_rows.extend(fresh)
            print(
                f"FAST DISCOVERY: {len(new_rows)}/{target_new} nouveaux | "
                f"{stats['known_seen']} déjà connus",
                flush=True,
            )
        return True

    for cp in postcodes[:3]:
        if len(new_rows) >= target_new or time.monotonic() >= deadline:
            break

        for idx, naf in enumerate(ordered_codes, 1):
            if len(new_rows) >= target_new or time.monotonic() >= deadline:
                break
            stats["codes_scanned"] += 1
            label = v91.category_label(naf, categories)
            kept_for_naf = 0

            try:
                for item in core.v6.gov_search(cp, label, naf, pages=1):
                    key = item.get("siret") or (item.get("name"), item.get("address"))
                    if not key or key in seen:
                        continue
                    seen.add(key)
                    buffer.append(core.empty_row(zone_name, label, item))
                    stats["raw_candidates"] += 1
                    kept_for_naf += 1

                    if len(buffer) >= snapshot_batch:
                        if not flush_buffer():
                            return zone_name, new_rows, stats
                        if len(new_rows) >= target_new:
                            break

                    if kept_for_naf >= max_per_naf:
                        break
            except Exception as exc:
                print(f"  ! NAF {naf}: {exc}", flush=True)

            if idx == 1 or idx % 50 == 0:
                print(
                    f"  scan NAF {idx}/{len(ordered_codes)} | "
                    f"{len(new_rows)}/{target_new} nouveaux",
                    flush=True,
                )

    if buffer and len(new_rows) < target_new and not stats["snapshot_failed"]:
        flush_buffer()

    return zone_name, new_rows[:target_new], stats


def discover_new_targeted_fast(
    zone: str,
    keywords: list[str],
    pages: int,
    target_new: int,
    push_sheets: bool,
    url: str,
    token: str,
) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    # En mode ciblé on garde le moteur V8, mais avec un vivier plus compact.
    cap = min(2000, max(target_new * 3, target_new + 100))
    zone_name, candidates = core.discover_zone(zone, keywords, max(1, pages), cap)
    fresh, known, ok, msg = _snapshot_new(candidates, push_sheets, url, token)
    if not ok:
        return zone_name, [], {"error": msg, "snapshot_failed": True}
    fresh = fresh[:target_new]
    registered = 0
    if push_sheets and fresh:
        registered, _failed = v91.push_batches(fresh, url, token)
    return zone_name, fresh, {
        "raw_candidates": len(candidates),
        "known_seen": known,
        "registered": registered,
        "snapshot_failed": False,
    }


def main() -> int:
    p = argparse.ArgumentParser(description="Fidelly V9.2 Fast Discovery")
    p.add_argument("--zones-pipe", required=True)
    p.add_argument("--keywords-pipe", default="")
    p.add_argument("--all-businesses", action="store_true")
    p.add_argument("--business-categories", default="business_categories.json")
    p.add_argument("--max-per-naf", type=int, default=4)
    p.add_argument("--pages", type=int, default=3)
    p.add_argument("--target-new-per-zone", type=int, default=100)
    p.add_argument("--max-enrich-per-run", type=int, default=40)
    p.add_argument("--checkpoint-every", type=int, default=10)
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--fast-timeout", type=int, default=7)
    p.add_argument("--max-runtime-minutes", type=int, default=75)
    p.add_argument("--push-sheets", action="store_true")
    p.add_argument("--output", default="fidelly_prospects.csv")
    p.add_argument("--no-web-search", action="store_true")
    p.add_argument(
        "--enrich-tier-b",
        action="store_true",
        help="Après les A, autorise aussi l'enrichissement des profils B.",
    )
    args = p.parse_args()

    zones = core.split_pipe(args.zones_pipe)
    keywords = core.split_pipe(args.keywords_pipe)
    if not zones:
        raise SystemExit("Zones invalides.")
    if not args.all_businesses and not keywords:
        raise SystemExit("Renseigne --keywords-pipe ou utilise --all-businesses.")

    categories: dict[str, dict] = {}
    if args.all_businesses:
        categories = v91.load_business_categories(args.business_categories)

    url = os.getenv("SHEETS_WEBHOOK_URL", "")
    token = os.getenv("SHEETS_WEBHOOK_TOKEN", "")
    target = max(1, args.target_new_per_zone)
    max_enrich = max(0, args.max_enrich_per_run)
    deadline = time.monotonic() + max(10, args.max_runtime_minutes) * 60
    workers = max(1, min(16, args.workers))
    fast_timeout = max(3, min(15, args.fast_timeout))

    # Corrige les arrondissements comme V9.1.
    core.v6.resolve_zone = v91.smart_resolve

    output_rows: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    stats: dict[str, Any] = {
        "version": VERSION,
        "started_at": core.utc_now_iso(),
        "mode": "all_businesses" if args.all_businesses else "keywords",
        "target_new_per_zone": target,
        "max_enrich_per_zone": max_enrich,
        "workers": workers,
        "fast_timeout": fast_timeout,
        "zones": {},
    }

    for zone in zones:
        if time.monotonic() >= deadline:
            break
        zone_started = time.monotonic()

        if args.all_businesses:
            zone_name, new_rows, discovery_stats = discover_new_all_businesses_fast(
                zone,
                categories,
                target,
                args.max_per_naf,
                args.push_sheets,
                url,
                token,
                deadline,
            )
        else:
            zone_name, new_rows, discovery_stats = discover_new_targeted_fast(
                zone,
                keywords,
                args.pages,
                target,
                args.push_sheets,
                url,
                token,
            )

        discovery_seconds = round(time.monotonic() - zone_started, 2)
        output_rows.extend(new_rows)
        core.save_csv(output_rows, args.output)

        tiers = {"A": [], "B": [], "C": []}
        for row in new_rows:
            tiers[commercial_tier(row)].append(row)

        queue = list(tiers["A"])
        if args.enrich_tier_b:
            queue.extend(tiers["B"])
        queue = queue[:max_enrich]

        print(
            f"{zone_name}: découverte terminée en {discovery_seconds:.1f}s | "
            f"nouveaux={len(new_rows)} | tiers A/B/C="
            f"{len(tiers['A'])}/{len(tiers['B'])}/{len(tiers['C'])} | "
            f"à enrichir={len(queue)}",
            flush=True,
        )

        enrichment_started = time.monotonic()
        enriched = 0

        if queue and time.monotonic() < deadline:
            # Réduit les timeouts de crawl sans toucher à la phase de découverte.
            core.v6.TIMEOUT = fast_timeout

            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="fidelly") as executor:
                future_map = {
                    executor.submit(
                        fast_enrich_one,
                        row,
                        fast_timeout,
                        args.no_web_search,
                    ): row
                    for row in queue
                }

                for future in as_completed(future_map):
                    row = future_map[future]
                    if time.monotonic() >= deadline:
                        break
                    try:
                        _updated, errors = future.result()
                        enriched += 1
                        print(
                            f"FAST ENRICH [{enriched}/{len(queue)}] "
                            f"{row.get('name')} | {row.get('enrichment_state')} | "
                            f"email={'yes' if row.get('email') else 'no'} | "
                            f"site={'yes' if row.get('website') else 'no'}",
                            flush=True,
                        )
                        if errors:
                            print("  ! " + " | ".join(errors)[:300], flush=True)
                    except Exception as exc:
                        row["last_error"] = str(exc)[:1500]

                    pending.append(row)
                    core.save_csv(output_rows, args.output)

                    if args.push_sheets and len(pending) >= max(1, args.checkpoint_every):
                        core.flush_pending(pending, args.output, url, token)

        if args.push_sheets and pending:
            core.flush_pending(pending, args.output, url, token)

        enrichment_seconds = round(time.monotonic() - enrichment_started, 2)
        stats["zones"][zone] = {
            **discovery_stats,
            "zone_name": zone_name,
            "new": len(new_rows),
            "tier_a": len(tiers["A"]),
            "tier_b": len(tiers["B"]),
            "tier_c": len(tiers["C"]),
            "enriched": enriched,
            "discovery_seconds": discovery_seconds,
            "enrichment_seconds": enrichment_seconds,
        }

    core.save_csv(output_rows, args.output)
    stats["finished_at"] = core.utc_now_iso()
    stats["output_rows"] = len(output_rows)
    core.save_json(stats, core.summary_path(args.output))

    print("=" * 78)
    print(
        f"V9.2 FAST terminé — nouveaux={len(output_rows)} | "
        f"mode={'tous métiers' if args.all_businesses else 'ciblé'}"
    )
    print("Découverte rapide d'abord, enrichissement parallèle ensuite.")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
