# Fidelly Prospection V8 Smart Incremental

## Ce que V8 change

V8 utilise le Google Sheet comme mémoire de prospection avant de lancer les recherches lourdes.

- Nouveau SIRET -> enrichissement complet.
- SIRET déjà connu et exploitable -> SKIP immédiat.
- SIRET connu mais incomplet -> recherche uniquement des informations manquantes.
- Fiche COMPLETE/ACTIONABLE déjà passée en V8 -> nouveau contrôle après 60 jours.
- Fiche PARTIAL -> nouveau contrôle après 14 jours.
- Fiche EMPTY -> nouveau contrôle après 7 jours.
- `force_refresh=true` -> force le passage dans les sources d'enrichissement, sans effacer les anciennes données.
- Le webhook Google Sheets fusionne les données et n'efface jamais un champ rempli avec une valeur vide.
- Chaque ville reste un job séparé, séquentiel, avec checkpoints et artifacts.

## Fichiers du pack

- `fidelly_prospect_finder_v8_smart.py` -> à ajouter à la racine du repo GitHub.
- `.github/workflows/fidelly-prospection.yml` -> remplace le workflow actuel.
- `apps_script/Code.gs` -> remplace le code du Google Apps Script déjà déployé.

Ne supprime pas `fidelly_prospect_finder_v6_cloud.py` : V8 l'utilise comme moteur de découverte/enrichissement.

## 1. GitHub

Dans `Winnipowa/Fidelly` :

1. Ajouter à la racine `fidelly_prospect_finder_v8_smart.py`.
2. Ouvrir `.github/workflows/fidelly-prospection.yml`.
3. Remplacer tout son contenu par le fichier du pack au même chemin.
4. Commit les changements.
5. Conserver `requirements.txt` et `fidelly_prospect_finder_v6_cloud.py`.

## 2. Google Apps Script

1. Ouvrir le projet Apps Script actuellement utilisé par `SHEETS_WEBHOOK_URL`.
2. Ouvrir `Code.gs`.
3. Remplacer TOUT le contenu par `apps_script/Code.gs` du pack.
4. Enregistrer.
5. `Deploy` -> `Manage deployments`.
6. Éditer le déploiement Web App existant avec le crayon.
7. Choisir `New version` puis `Deploy`.
8. Conserver :
   - Execute as: `Me`
   - Who has access: `Anyone`
9. Si l'URL `/exec` reste la même, ne change rien dans GitHub Secrets.
10. Si Google fournit une nouvelle URL `/exec`, remplacer `SHEETS_WEBHOOK_URL` dans GitHub Actions Secrets.

Le `WEBHOOK_TOKEN` ne change pas. Il doit rester identique entre Script Properties et `SHEETS_WEBHOOK_TOKEN` dans GitHub.

## 3. Vérifier le webhook

Ouvrir l'URL `/exec` dans le navigateur. La réponse doit contenir :

```json
{"ok":true,"service":"Fidelly Prospect Receiver","version":"8.0","message":"Webhook actif"}
```

## 4. Premier test recommandé

GitHub -> Actions -> `Fidelly Prospection V8 Smart Incremental` -> Run workflow.

Utiliser :

- Villes : `Issoudun`
- Activités : `caviste|restaurant`
- Maximum par ville : `20`
- Pages : `2`
- Checkpoint : `5`
- Force refresh : `false`

Comme Issoudun existe déjà dans le Sheet, les logs doivent montrer des lignes de ce type :

```text
Sheets snapshot : OK — ... existants chargés
... | SKIP (LEGACY_COMPLETE)
... | SKIP (LEGACY_ACTIONABLE)
... | ENRICH (LEGACY_PARTIAL_NEEDS_ENRICHMENT)
```

Le Sheet ajoute automatiquement les colonnes V8 sans effacer les données existantes :

- `enrichment_state`
- `enrichment_version`
- `last_enriched_at`
- `last_error`

## 5. Lancement multi-ville

Après le test :

Villes :

```text
Bourges|Châteauroux|Vierzon|Romorantin-Lanthenay|Saint-Amand-Montrond
```

Activités :

```text
restaurant|café|bar|caviste|coiffeur|barbier|institut de beauté|CBD|fleuriste|animalerie|boulangerie
```

Réglages recommandés :

- Maximum par ville : `50`
- Pages : `2`
- Checkpoint : `5`
- Force refresh : `false`

## Remarque importante

Aucun workflow GitHub Actions ne peut garantir de tourner indéfiniment sans interruption. V8 est conçu pour être **reprenable** : les données déjà poussées sont retrouvées au prochain run et les fiches suffisamment complètes sont sautées, ce qui évite de repartir de zéro après un timeout, un crash réseau ou une relance manuelle.
