#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fidelly Prospect Finder V9 New-First.

But:
- maximiser les SIRET jamais vus ;
- ne pas gaspiller du temps sur les prospects déjà actionnables ;
- enregistrer immédiatement les nouveaux dans Google Sheets ;
- enrichir les nouveaux avant tout ancien prospect ;
- supporter Paris/Lyon/Marseille par arrondissement.
"""
from __future__ import annotations

import argparse
import os
import re
import time

import fidelly_prospect_finder_v8_smart as core

VERSION = "9.0"
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
    p.add_argument("--keywords-pipe", required=True)
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
    if not zones or not keywords:
        raise SystemExit("Zones/activités invalides.")

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

    print(
        f"[Fidelly V9 NEW-FIRST] objectif nouveaux/zone={target} | "
        f"vivier/zone={discovery_cap} | enrich max/run={max_enrich}",
        flush=True,
    )

    for zone in zones:
        if time.monotonic() >= deadline:
            break

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
        f"V9 terminé — nouveaux={stats['new_selected']} | "
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
