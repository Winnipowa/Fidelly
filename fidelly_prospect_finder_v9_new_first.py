#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fidelly Prospect Finder V9 New-First.

But:
- maximiser les SIRET jamais vus ;
- ne pas gaspiller du temps sur les prospects déjà actionnables ;
- enregistrer immédiatement les nouveaux dans Google Sheets ;
- enrichir les nouveaux avant tout ancien prospect ;
- supporter Paris/Lyon/Marseille par arrondissement ;
- permettre un mode "tous les métiers" basé sur les 732 sous-classes NAF.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import time
from collections import defaultdict, deque
from pathlib import Path

import fidelly_prospect_finder_v8_smart as core

VERSION = "9.1"
ORIGINAL_RESOLVE = core.v6.resolve_zone


def smart_resolve(zone: str):
    z = core.norm(zone)
    m = re.search(
        r"\b(paris|lyon|marseille)\b.*?\b(\d{1,2})(?:er|e|ème|eme)?\b",
        z.lower(),
    )
    cp = z if re.fullmatch(r"\d{5}", z) else ""

    if m:
        city, n = m.group(1), int(m.group(2))
        limits = {"paris": 20, "lyon": 9, "marseille": 16}
        if 1 <= n <= limits[city]:
            if city == "paris":
                cp = f"750{n:02d}"
            elif city == "lyon":
                cp = f"6900{n}"
            else:
                cp = f"130{n:02d}"

    if cp:
        r = core.v6.SESSION.get(
            core.v6.GEO_COMMUNES_URL,
            params={
                "codePostal": cp,
                "fields": "nom,code,codesPostaux,departement,region,population",
                "limit": 5,
            },
            timeout=core.v6.TIMEOUT,
        )
        r.raise_for_status()
        data = r.json() or []
        if data:
            item = dict(data[0])
            item["codesPostaux"] = [cp]
            if m:
                item["nom"] = z
            return [item]

    return ORIGINAL_RESOLVE(z)


def load_business_categories(path: str) -> dict[str, dict]:
    """Charge la taxonomie NAF, et la génère si elle n'existe pas encore."""
    p = Path(path)

    if not p.exists():
        print(
            f"{p} absent : génération de la taxonomie NAF depuis la source INSEE...",
            flush=True,
        )
        try:
            import build_business_categories as builder

            xml_bytes = builder.read_source(None)
            rows = builder.parse_naf_subclasses(xml_bytes)
            if len(rows) != builder.EXPECTED_COUNT:
                raise RuntimeError(
                    f"{len(rows)} sous-classes reçues au lieu de "
                    f"{builder.EXPECTED_COUNT}"
                )
            data = builder.build_dataset(rows)
            p.write_text(
                json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except Exception as exc:
            raise SystemExit(
                "Impossible de générer business_categories.json. "
                "Exécute d'abord `python build_business_categories.py` "
                f"ou fournis --business-categories. Détail: {exc}"
            ) from exc

    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        raise SystemExit(f"Taxonomie NAF invalide ({p}): {exc}") from exc

    valid = {
        str(code).upper(): value
        for code, value in data.items()
        if re.fullmatch(r"\d{2}\.\d{2}[A-Z]", str(code).upper())
        and isinstance(value, dict)
    }
    if not valid:
        raise SystemExit(f"Aucune activité NAF valide dans {p}.")

    return valid


def balanced_naf_codes(categories: dict[str, dict]) -> list[str]:
    """Entrelace les divisions NAF pour éviter un scan biaisé secteur par secteur."""
    buckets: dict[str, deque[str]] = defaultdict(deque)
    for code in sorted(categories):
        buckets[code[:2]].append(code)

    divisions = sorted(buckets)
    result: list[str] = []
    while True:
        added = False
        for division in divisions:
            if buckets[division]:
                result.append(buckets[division].popleft())
                added = True
        if not added:
            break
    return result


def category_label(code: str, categories: dict[str, dict]) -> str:
    meta = categories.get(code.upper()) or {}
    label = core.norm(meta.get("category"))
    if label:
        return label
    keywords = meta.get("keywords") or []
    return core.norm(keywords[0]) if keywords else f"NAF {code}"


def discover_zone_all_businesses(
    zone: str,
    categories: dict[str, dict],
    max_prospects: int,
    max_per_naf: int = 4,
) -> tuple[str, list[dict]]:
    """Découvre un éventail large d'établissements en parcourant tous les codes NAF.

    Une seule page API est demandée par code NAF et on limite le nombre retenu par
    code afin de privilégier la diversité des métiers plutôt qu'un secteur dominant.
    """
    try:
        communes = core.v6.resolve_zone(zone)
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
    print(
        f"  Mode TOUS LES MÉTIERS : {len(categories)} codes NAF disponibles",
        flush=True,
    )

    rows: list[dict] = []
    global_seen = set()
    ordered_codes = balanced_naf_codes(categories)
    max_per_naf = max(1, int(max_per_naf))

    for cp in postcodes[:3]:
        if len(rows) >= max_prospects:
            break

        for idx, naf in enumerate(ordered_codes, 1):
            if len(rows) >= max_prospects:
                break

            label = category_label(naf, categories)
            if idx == 1 or idx % 50 == 0:
                print(
                    f"  -> progression {idx}/{len(ordered_codes)} codes NAF "
                    f"| {len(rows)}/{max_prospects} prospects",
                    flush=True,
                )

            kept_for_naf = 0
            try:
                # Le filtre NAF suffit : le texte n'est utilisé que comme label local.
                for item in core.v6.gov_search(cp, label, naf, pages=1):
                    key = item.get("siret") or (item.get("name"), item.get("address"))
                    if not key or key in global_seen:
                        continue
                    global_seen.add(key)
                    rows.append(core.empty_row(zone_name, label, item))
                    kept_for_naf += 1

                    if kept_for_naf >= max_per_naf or len(rows) >= max_prospects:
                        break
            except Exception as exc:
                print(f"    ! découverte NAF {naf}: {exc}", flush=True)

    print(
        f"{len(rows)} établissements retenus pour {zone_name} "
        f"(tous métiers).\n",
        flush=True,
    )
    return zone_name, rows


def push_batches(rows, url, token, batch_size=100):
    synced, failed = 0, []
    for start in range(0, len(rows), batch_size):
        batch = rows[start : start + batch_size]
        ok, msg = core.push_rows(batch, url, token)
        print(
            f"Nouveaux -> Sheets: {'OK' if ok else 'ECHEC'} "
            f"({len(batch)}) — {msg}",
            flush=True,
        )
        if ok:
            synced += len(batch)
        else:
            failed.extend(batch)
    return synced, failed


def never_enriched(existing):
    state = core.norm(existing.get("enrichment_state")).upper()
    return (
        not core.norm(existing.get("last_enriched_at"))
        and state in {"", "NEW", "DISCOVERED", "QUEUED", "EMPTY", "PARTIAL"}
    )


def main():
    p = argparse.ArgumentParser(description="Fidelly V9 New-First")
    p.add_argument("--zones-pipe", required=True)
    p.add_argument(
        "--keywords-pipe",
        default="",
        help="Activités séparées par |. Facultatif avec --all-businesses.",
    )
    p.add_argument(
        "--all-businesses",
        action="store_true",
        help="Parcourt toute la taxonomie NAF au lieu d'une liste de mots-clés.",
    )
    p.add_argument(
        "--business-categories",
        default="business_categories.json",
        help="Fichier JSON généré par build_business_categories.py.",
    )
    p.add_argument(
        "--max-per-naf",
        type=int,
        default=4,
        help="En mode tous métiers, maximum retenu par code NAF et code postal.",
    )
    p.add_argument("--pages", type=int, default=5)
    p.add_argument("--target-new-per-zone", type=int, default=100)
    p.add_argument("--discovery-multiplier", type=int, default=8)
    p.add_argument("--max-enrich-per-run", type=int, default=100)
    p.add_argument("--checkpoint-every", type=int, default=5)
    p.add_argument("--max-runtime-minutes", type=int, default=160)
    p.add_argument("--force-refresh", action="store_true")
    p.add_argument("--push-sheets", action="store_true")
    p.add_argument("--output", default="fidelly_prospects.csv")
    p.add_argument("--no-web-search", action="store_true")
    args = p.parse_args()

    zones = core.split_pipe(args.zones_pipe)
    keywords = core.split_pipe(args.keywords_pipe)
    categories: dict[str, dict] = {}

    if args.all_businesses:
        categories = load_business_categories(args.business_categories)

    if not zones:
        raise SystemExit("Zones invalides.")
    if not args.all_businesses and not keywords:
        raise SystemExit(
            "Aucune activité valide. Utilise --keywords-pipe ou --all-businesses."
        )

    target = max(1, args.target_new_per_zone)
    discovery_cap = min(
        5000,
        max(target * max(2, args.discovery_multiplier), target + 300),
    )
    max_enrich = max(1, args.max_enrich_per_run)
    deadline = time.monotonic() + max(5, args.max_runtime_minutes) * 60

    url = os.getenv("SHEETS_WEBHOOK_URL", "")
    token = os.getenv("SHEETS_WEBHOOK_TOKEN", "")

    core.v6.resolve_zone = smart_resolve

    output_rows = []
    pending = []
    stats = {
        "version": VERSION,
        "started_at": core.utc_now_iso(),
        "mode": "all_businesses" if args.all_businesses else "keywords",
        "naf_categories": len(categories) if args.all_businesses else 0,
        "keywords": keywords if not args.all_businesses else [],
        "target_new_per_zone": target,
        "discovery_cap_per_zone": discovery_cap,
        "new_selected": 0,
        "new_registered": 0,
        "known_seen": 0,
        "known_skipped": 0,
        "old_queue": 0,
        "enriched": 0,
        "zones": {},
    }

    mode_label = (
        f"TOUS MÉTIERS ({len(categories)} NAF)"
        if args.all_businesses
        else f"{len(keywords)} activités ciblées"
    )
    print(
        f"[Fidelly V9.1 NEW-FIRST] mode={mode_label} | "
        f"objectif nouveaux/zone={target} | "
        f"vivier/zone={discovery_cap} | enrich max/run={max_enrich}",
        flush=True,
    )

    for zone in zones:
        if time.monotonic() >= deadline:
            break

        if args.all_businesses:
            zone_name, candidates = discover_zone_all_businesses(
                zone,
                categories,
                discovery_cap,
                max_per_naf=args.max_per_naf,
            )
        else:
            zone_name, candidates = core.discover_zone(
                zone,
                keywords,
                max(1, args.pages),
                discovery_cap,
            )

        if not candidates:
            stats["zones"][zone] = {"candidates": 0, "new": 0}
            continue

        existing = {}
        snapshot_ok = not args.push_sheets
        snapshot_msg = "sans Google Sheets"

        if args.push_sheets:
            existing, snapshot_msg = core.fetch_existing(
                candidates,
                url,
                token,
                touch=False,
            )
            snapshot_ok = snapshot_msg.endswith("existants chargés")
            print(f"{zone_name} — snapshot: {snapshot_msg}", flush=True)

            if not snapshot_ok:
                print(
                    "Snapshot Sheets indisponible: zone ignorée pour éviter "
                    "de recréer des doublons.",
                    flush=True,
                )
                stats["zones"][zone] = {
                    "candidates": len(candidates),
                    "snapshot_ok": False,
                }
                continue

        new_candidates = []
        old_queue = []

        for candidate in candidates:
            siret = core.norm(candidate.get("siret"))
            old = existing.get(siret) if siret else None

            if not old:
                candidate["enrichment_state"] = "DISCOVERED"
                candidate["enrichment_version"] = VERSION
                candidate["last_enriched_at"] = ""
                new_candidates.append(candidate)
                continue

            stats["known_seen"] += 1
            row = core.seed_from_existing(candidate, old)
            state = core.determine_state(row)

            if args.force_refresh:
                old_queue.append(row)
                continue

            if state in {"COMPLETE", "ACTIONABLE"}:
                stats["known_skipped"] += 1
                continue

            do_enrich, _reason = core.should_enrich(
                row,
                old,
                False,
                60,
                14,
                7,
            )
            if never_enriched(old) or do_enrich:
                old_queue.append(row)
            else:
                stats["known_skipped"] += 1

        selected_new = new_candidates[:target]
        stats["new_selected"] += len(selected_new)
        stats["old_queue"] += len(old_queue)

        print(
            f"{zone_name}: {len(new_candidates)} nouveaux dans le vivier; "
            f"{len(selected_new)} retenus / objectif {target}; "
            f"{len(old_queue)} anciens incomplets.",
            flush=True,
        )

        if args.push_sheets and selected_new:
            synced, failed = push_batches(selected_new, url, token)
            stats["new_registered"] += synced
            pending.extend(failed)

        output_rows.extend(selected_new)
        core.save_csv(output_rows, args.output)

        if selected_new:
            work = selected_new[:max_enrich]
        else:
            work = old_queue[:max_enrich]
            for row in work:
                if row not in output_rows:
                    output_rows.append(row)

        for index, row in enumerate(work, 1):
            if time.monotonic() >= deadline:
                break

            label = "NEW" if selected_new else "OLD-INCOMPLETE"
            print(
                f"[{index}/{len(work)}] {label} — "
                f"{row.get('name')} — {row.get('city')}",
                flush=True,
            )

            try:
                row, errors = core.enrich_one(
                    row,
                    args.no_web_search,
                    "",
                )
                row["enrichment_version"] = VERSION
                stats["enriched"] += 1
                if errors:
                    print("  ! " + " | ".join(errors)[:400], flush=True)
            except Exception as exc:
                row["last_error"] = str(exc)[:1500]

            pending.append(row)
            core.save_csv(output_rows, args.output)

            if (
                args.push_sheets
                and len(pending) >= max(1, args.checkpoint_every)
            ):
                core.flush_pending(
                    pending,
                    args.output,
                    url,
                    token,
                )

        if args.push_sheets and pending:
            core.flush_pending(
                pending,
                args.output,
                url,
                token,
            )

        stats["zones"][zone] = {
            "candidates": len(candidates),
            "new_detected": len(new_candidates),
            "new_selected": len(selected_new),
            "old_queue": len(old_queue),
            "snapshot_ok": snapshot_ok,
        }

    core.save_csv(output_rows, args.output)
    stats["finished_at"] = core.utc_now_iso()
    stats["output_rows"] = len(output_rows)
    core.save_json(stats, core.summary_path(args.output))

    print("=" * 78)
    print(
        f"V9.1 terminé — nouveaux={stats['new_selected']} | "
        f"enregistrés={stats['new_registered']} | "
        f"enrichis={stats['enriched']} | "
        f"anciens ignorés={stats['known_skipped']}"
    )
    print(
        "Priorité stricte: nouveaux SIRET > anciens incomplets > "
        "anciens actionnables (ignorés par défaut)"
    )
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
