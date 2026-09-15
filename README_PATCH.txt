Fidelly V9.1 - Tous les métiers

Fichiers à remplacer/ajouter dans Winnipowa/Fidelly :
- fidelly_prospect_finder_v9_new_first.py
- build_business_categories.py
- .github/workflows/fidelly-prospection.yml

Le workflow ajoute l'option "Scanner tous les métiers (732 activités NAF)" activée par défaut.
Quand elle est activée, GitHub Actions génère business_categories.json depuis l'INSEE puis lance le V9.1 avec --all-businesses.
