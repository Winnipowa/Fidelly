# Fidelly V9 New-First

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

Pour viser 500 nouveaux prospects dans une ville :
- target_new_per_city = 500
- pages = 5
- discovery_multiplier = 8
- max_enrich_per_run = 100

Les nouveaux trouvés sont enregistrés rapidement dans Sheets. L'enrichissement lourd reste limité pour éviter les timeouts.
