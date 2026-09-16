# Fidelly V9.1 New-First

Priorité du moteur :

1. Chercher un vivier beaucoup plus large que la cible.
2. Lire les SIRET déjà présents dans Google Sheets.
3. Garder d'abord les SIRET jamais vus.
4. Enregistrer immédiatement ces nouveaux prospects dans Sheets.
5. Enrichir uniquement les nouveaux tant qu'il y en a.
6. Les anciens incomplets ne sont retravaillés que lorsqu'aucun nouveau n'est disponible.
7. Les anciens ACTIONABLE / COMPLETE sont ignorés par défaut.
8. `force_refresh=true` permet volontairement de retraiter l'historique.
9. Paris, Lyon et Marseille peuvent être saisis par arrondissement.
10. Le mode `all_businesses` utilise la nomenclature NAF rév. 2 complète (732 sous-classes) pour ne plus avoir à saisir les métiers à la main.

## Mode recommandé : tous les métiers

Dans **GitHub Actions > Fidelly Prospection V9 New-First > Run workflow** :

- renseigner `zones` ;
- laisser **Scanner tous les métiers (732 activités NAF)** activé ;
- laisser `keywords` vide ;
- choisir l'objectif de nouveaux prospects par ville.

Le workflow génère automatiquement `business_categories.json` depuis la nomenclature officielle INSEE puis lance :

```bash
python fidelly_prospect_finder_v9_new_first.py \
  --zones-pipe "Lyon" \
  --all-businesses \
  --business-categories business_categories.json \
  --target-new-per-zone 100 \
  --push-sheets
```

`max_per_naf` limite le nombre de prospects retenus par activité NAF afin d'éviter qu'un secteur très dense monopolise tout le résultat.

## Mode ciblé

Pour rechercher seulement certains métiers, désactiver **Scanner tous les métiers** et remplir `keywords`, par exemple :

```text
restaurant|bar|caviste|coiffeur
```

## Exemple de volume

Pour viser 500 nouveaux prospects dans une ville :

- `target_new_per_city = 500`
- `pages = 5` (mode ciblé)
- `discovery_multiplier = 8`
- `max_enrich_per_run = 100`

Les nouveaux trouvés sont enregistrés rapidement dans Sheets. L'enrichissement lourd reste limité pour éviter les timeouts.

## V9.2 Fast Discovery

V9.2 sépare la découverte de l'enrichissement :

- les nouveaux prospects sont détectés puis enregistrés immédiatement dans Google Sheets ;
- le scan NAF vérifie les doublons par lots et s'arrête dès que l'objectif de nouveaux prospects est atteint ;
- les activités sont classées A/B/C pour éviter d'enrichir par défaut les profils peu commerciaux ;
- seuls les profils A sont enrichis par défaut (option pour inclure B) ;
- enrichissement web parallèle (`workers=8` par défaut) ;
- recherche web courte : 1 requête, 2 moteurs maximum ;
- timeout réduit à 7 secondes ;
- Nominatim est retiré du chemin d'enrichissement rapide.

Le workflow GitHub Actions appelle `fidelly_prospect_finder_v9_2_fast.py`.
