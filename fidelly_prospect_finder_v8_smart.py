#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fidelly Prospect Finder v8 Smart Incremental.

V8 ajoute une vraie mémoire de prospection au pipeline V7 :
- lit dans Google Sheets les SIRET déjà connus AVANT l'enrichissement ;
- ne retravaille pas les fiches déjà suffisamment complètes ;
- complète uniquement les champs manquants ;
- applique un délai de rafraîchissement aux fiches partielles ;
- conserve toujours les meilleures données existantes ;
- écrit le CSV au fil de l'eau ;
- pousse Google Sheets par checkpoints avec retries ;
- isole les erreurs fiche par fiche ;
- s'arrête proprement avant le timeout GitHub ;
- permet un force-refresh manuel sans perdre les anciennes données.

Le moteur de découverte/enrichissement reste v6 afin d'éviter de dupliquer
les extracteurs et de conserver la compatibilité avec le dépôt existant.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import signal
import sys
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

import fidelly_prospect_finder_v6_cloud as v6

VERSION = "8.0"
SOURCE_NAME = "fidelly-prospect-finder-v8"

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
    "enrichment_state", "enrichment_version", "last_enriched_at", "last_error",
]

PLACEHOLDER_TOKENS = {
    "zone", "zones", "ville", "villes",
    "activité", "activités", "activite", "activites",
    "keyword", "keywords", "activity", "activities",
}

CONTACT_FIELDS = (
    "website", "email", "phone", "instagram", "facebook", "tiktok", "linkedin"
)

_STOP_REQUESTED = False


def _request_stop(signum, _frame):
    global _STOP_REQUESTED
    _STOP_REQUESTED = True
    print(f"\nSignal {signum} reçu : arrêt propre demandé, flush au prochain point sûr.", flush=True)


for _sig in (getattr(signal, "SIGTERM", None), getattr(signal, "SIGINT", None)):
    if _sig is not None:
        try:
            signal.signal(_sig, _request_stop)
        except Exception:
            pass


def norm(value: Any) -> str:
    return str(value or "").strip()


def split_pipe(raw: str) -> list[str]:
    items: list[str] = []
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


def slugify(value: str) -> str:
    text = unicodedata.normalize("NFKD", norm(value))
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    return text or "zone"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        dt = value
    else:
        raw = norm(value)
        if not raw:
            return None
        try:
            if raw.endswith("Z"):
                raw = raw[:-1] + "+00:00"
            dt = datetime.fromisoformat(raw)
        except ValueError:
            # Apps Script / Sheets peut parfois renvoyer une date localisée non ISO.
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y"):
                try:
                    dt = datetime.strptime(raw, fmt)
                    break
                except ValueError:
                    continue
            else:
                return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def age_days(value: Any) -> float | None:
    dt = parse_datetime(value)
    if not dt:
        return None
    return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0)


def empty_row(zone_name: str, keyword: str, item: dict) -> dict:
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
        "enrichment_state": "NEW",
        "enrichment_version": VERSION,
        "last_enriched_at": "",
        "last_error": "",
    }


def seed_from_existing(candidate: dict, existing: dict | None) -> dict:
    """Fusionne l'historique dans la fiche sans écraser les données légales fraîches."""
    if not existing:
        return candidate

    # Les données de découverte du jour restent prioritaires pour l'identité/adresse.
    keep_candidate = {
        "zone", "keyword", "name", "legal_name", "address", "postcode", "city",
        "siret", "siren", "naf", "employees", "creation_date",
        "open_establishments", "total_establishments", "company_status",
        "latitude", "longitude",
    }

    for field in FIELDS:
        old = existing.get(field)
        if field in keep_candidate:
            if not norm(candidate.get(field)) and norm(old):
                candidate[field] = old
            continue
        if norm(old) and not norm(candidate.get(field)):
            candidate[field] = old

    # Une fidélité détectée ne doit jamais redevenir "no" par manque de signal.
    if existing.get("loyalty") == "yes":
        candidate["loyalty"] = "yes"
        candidate["loyalty_evidence"] = existing.get("loyalty_evidence", "")

    candidate["enrichment_version"] = norm(existing.get("enrichment_version")) or VERSION
    return candidate


def contact_profile(row: dict) -> dict[str, bool]:
    return {field: bool(norm(row.get(field))) for field in CONTACT_FIELDS}


def determine_state(row: dict) -> str:
    p = contact_profile(row)
    social = p["instagram"] or p["facebook"] or p["tiktok"] or p["linkedin"]

    # Suffisamment riche pour la prospection Fidelly : on arrête de gaspiller du temps.
    if (p["email"] and p["phone"]) or (p["email"] and p["website"]) or (
        p["phone"] and p["website"] and social
    ):
        return "COMPLETE"
    if p["email"] or p["phone"]:
        return "ACTIONABLE"
    if p["website"] or social:
        return "PARTIAL"
    return "EMPTY"


def should_enrich(
    candidate: dict,
    existing: dict | None,
    force_refresh: bool,
    actionable_refresh_days: int,
    partial_refresh_days: int,
    empty_refresh_days: int,
) -> tuple[bool, str]:
    if force_refresh:
        return True, "FORCE_REFRESH"
    if not existing:
        return True, "NEW"

    state = determine_state(candidate)
    last_age = age_days(existing.get("last_enriched_at"))

    # Données historiques V6/V7 : si elles sont déjà actionnables, on les respecte
    # immédiatement au lieu de tout recalculer une première fois.
    if last_age is None:
        if state in {"COMPLETE", "ACTIONABLE"}:
            return False, f"LEGACY_{state}"
        return True, f"LEGACY_{state}_NEEDS_ENRICHMENT"

    threshold = {
        "COMPLETE": actionable_refresh_days,
        "ACTIONABLE": actionable_refresh_days,
        "PARTIAL": partial_refresh_days,
        "EMPTY": empty_refresh_days,
    }[state]

    if last_age < threshold:
        return False, f"FRESH_{state}_{last_age:.1f}D"
    return True, f"STALE_{state}_{last_age:.1f}D"


def save_csv(rows: list[dict], output_path: str) -> None:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(
        rows,
        key=lambda x: (
            x.get("score", 0) or 0,
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


def save_json(data: Any, path: str) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def discover_zone(zone: str, keywords: list[str], pages: int, max_prospects: int) -> tuple[str, list[dict]]:
    """Découverte équilibrée : collecte des pools puis round-robin entre activités."""
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

    # On autorise chaque activité à remplir un vivier assez grand pour pouvoir
    # compenser les catégories peu représentées sans laisser restaurant monopoliser.
    pool_cap = max(8, max_prospects)
    pools: list[list[dict]] = []

    for keyword in keywords:
        pool: list[dict] = []
        local_seen = set()
        naf_codes = v6.NAF_MAP.get(keyword.lower(), [])
        modes = naf_codes or [None]

        for cp in postcodes[:3]:
            for naf in modes:
                label = f"NAF {naf}" if naf else "texte"
                print(f"  -> {keyword} | {cp} | {label}", flush=True)
                try:
                    for item in v6.gov_search(cp, keyword, naf, pages):
                        key = item.get("siret") or (item.get("name"), item.get("address"))
                        if not key or key in local_seen:
                            continue
                        local_seen.add(key)
                        pool.append(empty_row(zone_name, keyword, item))
                        if len(pool) >= pool_cap:
                            break
                except Exception as exc:
                    print(f"    ! découverte {keyword}: {exc}", flush=True)
                if len(pool) >= pool_cap:
                    break
            if len(pool) >= pool_cap:
                break
        pools.append(pool)

    rows: list[dict] = []
    global_seen = set()
    i = 0
    while len(rows) < max_prospects:
        added = False
        for pool in pools:
            if i >= len(pool):
                continue
            item = pool[i]
            key = item.get("siret") or (item.get("name"), item.get("address"))
            if key and key not in global_seen:
                global_seen.add(key)
                rows.append(item)
                added = True
                if len(rows) >= max_prospects:
                    break
        if not added:
            break
        i += 1

    print(f"{len(rows)} établissements retenus pour {zone_name}.\n", flush=True)
    return zone_name, rows


def sheet_call(
    webhook_url: str,
    webhook_token: str,
    payload: dict,
    attempts: int = 5,
    timeout: int = 75,
) -> tuple[bool, dict | str]:
    if not webhook_url:
        return False, "SHEETS_WEBHOOK_URL absent"
    body = {
        "token": webhook_token,
        "source": SOURCE_NAME,
        "generated_at": utc_now_iso(),
        **payload,
    }
    last: str = ""
    for attempt in range(1, attempts + 1):
        try:
            r = requests.post(
                webhook_url,
                json=body,
                headers={"Content-Type": "application/json"},
                timeout=timeout,
                allow_redirects=True,
            )
            text = r.text[:5000]
            if r.status_code >= 400:
                last = f"HTTP {r.status_code}: {text[:800]}"
            else:
                try:
                    data = r.json()
                except Exception:
                    data = {"ok": False, "message": text}
                if data.get("ok"):
                    return True, data
                last = norm(data.get("message")) or text or "Réponse Apps Script invalide"
        except requests.RequestException as exc:
            last = str(exc)

        if attempt < attempts:
            wait = min(45, 3 * (2 ** (attempt - 1)))
            print(f"    Sheets tentative {attempt}/{attempts} échouée: {last}; retry {wait}s", flush=True)
            time.sleep(wait)
    return False, last


def fetch_existing(
    candidates: list[dict],
    webhook_url: str,
    webhook_token: str,
    touch: bool = True,
) -> tuple[dict[str, dict], str]:
    sirets = [norm(x.get("siret")) for x in candidates if norm(x.get("siret"))]
    if not sirets:
        return {}, "aucun SIRET"

    existing: dict[str, dict] = {}
    for start in range(0, len(sirets), 150):
        chunk = sirets[start:start + 150]
        ok, result = sheet_call(
            webhook_url,
            webhook_token,
            {"action": "snapshot", "sirets": chunk, "touch": bool(touch)},
            attempts=4,
        )
        if not ok:
            return existing, str(result)
        rows = result.get("rows") or []
        for row in rows:
            siret = norm(row.get("siret"))
            if siret:
                existing[siret] = row
    return existing, f"{len(existing)} existants chargés"


def push_rows(rows: list[dict], webhook_url: str, webhook_token: str) -> tuple[bool, str]:
    if not rows:
        return True, "aucune ligne"
    ok, result = sheet_call(
        webhook_url,
        webhook_token,
        {"action": "upsert", "rows": rows},
        attempts=5,
    )
    if ok:
        return True, norm(result.get("message")) or "OK"
    return False, str(result)


def enrich_one(row: dict, no_web_search: bool = False, nominatim_email: str = "") -> tuple[dict, list[str]]:
    """Enrichit seulement ce qui manque, sans écraser les données déjà acquises."""
    errors: list[str] = []

    # Nominatim est utile pour site/téléphone/réseaux manquants, mais on l'évite si
    # la fiche a déjà les trois piliers email+phone+website.
    if not (row.get("email") and row.get("phone") and row.get("website")):
        try:
            osm, osm_err = v6.nominatim_business(row, nominatim_email)
            if osm:
                v6.merge(row, osm, "Nominatim")
            elif osm_err:
                errors.append(osm_err)
        except Exception as exc:
            errors.append(f"Nominatim: {exc}")

    # Recherche web seulement si un des piliers reste absent.
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
            errors.append(f"Web search: {exc}")

    # Le crawl d'un site déjà connu peut compléter email/téléphone/réseaux/fidélité.
    if row.get("website") and (
        not row.get("email")
        or not row.get("phone")
        or not row.get("instagram")
        or row.get("loyalty") != "yes"
    ):
        try:
            v6.merge(row, v6.crawl_site(row["website"]), "SiteCrawl")
        except Exception as exc:
            errors.append(f"Site crawl: {exc}")

    try:
        pts, prio, details = v6.score(row)
    except Exception as exc:
        errors.append(f"Scoring: {exc}")
        pts, prio, details = 0, "C", f"scoring error: {exc}"

    row["score"] = pts
    row["priority"] = prio
    row["score_details"] = details
    row["enrichment_state"] = determine_state(row)
    row["enrichment_version"] = VERSION
    row["last_enriched_at"] = utc_now_iso()
    row["last_error"] = " | ".join(errors)[:1500]
    return row, errors


def rescore_without_enrichment(row: dict) -> dict:
    """Normalise une fiche historique sautée sans lancer de réseau lourd."""
    try:
        pts, prio, details = v6.score(row)
        row["score"] = pts
        row["priority"] = prio
        row["score_details"] = details
    except Exception:
        pass
    row["enrichment_state"] = determine_state(row)
    if not norm(row.get("enrichment_version")):
        row["enrichment_version"] = "legacy"
    return row


def pending_path(output_path: str) -> str:
    return str(Path(output_path).with_suffix(".pending.json"))


def summary_path(output_path: str) -> str:
    return str(Path(output_path).with_suffix(".summary.json"))


def flush_pending(pending: list[dict], output_path: str, webhook_url: str, webhook_token: str) -> bool:
    if not pending:
        p = Path(pending_path(output_path))
        if p.exists():
            p.unlink()
        return True
    ok, msg = push_rows(pending, webhook_url, webhook_token)
    if ok:
        print(f"    Sheets checkpoint OK — {msg}", flush=True)
        pending.clear()
        p = Path(pending_path(output_path))
        if p.exists():
            p.unlink()
        return True
    print(f"    Sheets checkpoint différé — {msg}", flush=True)
    save_json(pending, pending_path(output_path))
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Fidelly Prospect Finder v8 Smart Incremental")
    parser.add_argument("--zones-pipe", required=True, help="Villes séparées par |")
    parser.add_argument("--keywords-pipe", required=True, help="Activités séparées par |")
    parser.add_argument("--pages", type=int, default=2)
    parser.add_argument("--max-prospects-per-zone", type=int, default=50)
    parser.add_argument("--checkpoint-every", type=int, default=5)
    parser.add_argument("--max-runtime-minutes", type=int, default=160)
    parser.add_argument("--actionable-refresh-days", type=int, default=60)
    parser.add_argument("--partial-refresh-days", type=int, default=14)
    parser.add_argument("--empty-refresh-days", type=int, default=7)
    parser.add_argument("--force-refresh", action="store_true")
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
    deadline = time.monotonic() + max(5, args.max_runtime_minutes) * 60

    completed: list[dict] = []
    pending: list[dict] = []
    stats = {
        "version": VERSION,
        "started_at": utc_now_iso(),
        "zones": zones,
        "keywords": keywords,
        "discovered": 0,
        "new": 0,
        "existing": 0,
        "skipped": 0,
        "enriched": 0,
        "sheet_snapshot_ok": False,
        "sheet_push_ok": False,
        "stopped_early": False,
        "reasons": {},
    }

    print(
        f"[Fidelly v8 SMART] zones={len(zones)} | activités={len(keywords)} | "
        f"max/zone={args.max_prospects_per_zone} | checkpoint={args.checkpoint_every} | "
        f"force={'yes' if args.force_refresh else 'no'}",
        flush=True,
    )

    for zone in zones:
        if _STOP_REQUESTED or time.monotonic() >= deadline:
            stats["stopped_early"] = True
            print("Arrêt de sécurité avant nouvelle ville.", flush=True)
            break

        zone_name, candidates = discover_zone(zone, keywords, args.pages, args.max_prospects_per_zone)
        stats["discovered"] += len(candidates)

        existing_by_siret: dict[str, dict] = {}
        if args.push_sheets and candidates:
            print("Lecture des SIRET déjà présents dans Google Sheets...", flush=True)
            existing_by_siret, snapshot_msg = fetch_existing(
                candidates, webhook_url, webhook_token, touch=True
            )
            if existing_by_siret or snapshot_msg.endswith("existants chargés"):
                stats["sheet_snapshot_ok"] = True
                print(f"Sheets snapshot : OK — {snapshot_msg}", flush=True)
            else:
                print(
                    "Sheets snapshot indisponible — le scan continue, mais sans optimisation historique. "
                    f"Détail: {snapshot_msg}",
                    flush=True,
                )

        print(f"Traitement intelligent {zone_name}...", flush=True)

        for index, candidate in enumerate(candidates, 1):
            if _STOP_REQUESTED or time.monotonic() >= deadline:
                stats["stopped_early"] = True
                print("Temps/signal de sécurité atteint; flush immédiat.", flush=True)
                break

            siret = norm(candidate.get("siret"))
            existing = existing_by_siret.get(siret) if siret else None
            if existing:
                stats["existing"] += 1
            else:
                stats["new"] += 1

            row = seed_from_existing(candidate, existing)
            do_enrich, reason = should_enrich(
                row,
                existing,
                args.force_refresh,
                max(1, args.actionable_refresh_days),
                max(1, args.partial_refresh_days),
                max(1, args.empty_refresh_days),
            )
            stats["reasons"][reason] = stats["reasons"].get(reason, 0) + 1

            print(
                f"[{index}/{len(candidates)}] {row.get('name')} — {row.get('city')} | "
                f"{'ENRICH' if do_enrich else 'SKIP'} ({reason})",
                flush=True,
            )

            if do_enrich:
                try:
                    row, errors = enrich_one(row, args.no_web_search, args.nominatim_email)
                    stats["enriched"] += 1
                    if errors:
                        print(f"    avertissements: {' | '.join(errors)[:500]}", flush=True)
                except Exception as exc:
                    row["enrichment_state"] = determine_state(row)
                    row["enrichment_version"] = VERSION
                    row["last_enriched_at"] = utc_now_iso()
                    row["last_error"] = f"unexpected enrichment error: {exc}"[:1500]
                    print(f"    ! erreur prospect isolée: {exc}", flush=True)

                # Chaque enrichissement doit être persisté ; en cas de crash le rerun
                # le retrouvera dans le snapshot et ne repartira pas de zéro.
                pending.append(row)
            else:
                stats["skipped"] += 1
                rescore_without_enrichment(row)

            completed.append(row)
            save_csv(completed, args.output)

            print(
                "    "
                f"state={row.get('enrichment_state')} | "
                f"site={'OK' if row.get('website') else '-'} | "
                f"mail={'OK' if row.get('email') else '-'} | "
                f"tel={'OK' if row.get('phone') else '-'} | "
                f"IG={'OK' if row.get('instagram') else '-'} | "
                f"score={row.get('score', 0)}",
                flush=True,
            )

            if args.push_sheets and len(pending) >= max(1, args.checkpoint_every):
                if flush_pending(pending, args.output, webhook_url, webhook_token):
                    stats["sheet_push_ok"] = True

        if args.push_sheets and pending:
            if flush_pending(pending, args.output, webhook_url, webhook_token):
                stats["sheet_push_ok"] = True

        if stats["stopped_early"]:
            break

    save_csv(completed, args.output)
    if pending:
        save_json(pending, pending_path(args.output))

    states: dict[str, int] = {}
    for row in completed:
        state = row.get("enrichment_state") or determine_state(row)
        states[state] = states.get(state, 0) + 1

    stats["finished_at"] = utc_now_iso()
    stats["processed"] = len(completed)
    stats["pending_unsynced"] = len(pending)
    stats["states"] = states
    stats["sites"] = sum(bool(x.get("website")) for x in completed)
    stats["emails"] = sum(bool(x.get("email")) for x in completed)
    stats["phones"] = sum(bool(x.get("phone")) for x in completed)
    stats["instagram"] = sum(bool(x.get("instagram")) for x in completed)
    stats["priority_a"] = sum(x.get("priority") == "A" for x in completed)
    stats["priority_b"] = sum(x.get("priority") == "B" for x in completed)
    save_json(stats, summary_path(args.output))

    print("\n" + "=" * 78)
    print(f"CSV : {args.output}")
    print(
        f"Découverts {stats['discovered']} | traités {stats['processed']} | "
        f"nouveaux {stats['new']} | existants {stats['existing']}"
    )
    print(f"Enrichis {stats['enriched']} | SKIP intelligents {stats['skipped']}")
    print(f"États : {states}")
    print(
        f"A : {stats['priority_a']} | B : {stats['priority_b']} | "
        f"Emails : {stats['emails']} | Téléphones : {stats['phones']} | Sites : {stats['sites']}"
    )
    if pending:
        print(f"ATTENTION : {len(pending)} fiches non synchronisées -> {pending_path(args.output)}")
    elif args.push_sheets:
        print("Google Sheets : synchronisation à jour")
    print(f"Résumé : {summary_path(args.output)}")
    print("=" * 78)

    # Les pannes externes ponctuelles ne doivent pas tuer la chaîne : le pending
    # est conservé en artifact. Les erreurs de configuration/arguments sortent >0.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
