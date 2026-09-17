# Fidelly - Bot autonome d'enrichissement des emails

## Fichiers

- `fidelly_email_enricher.py` : recherche + validation + écriture.
- `.github/workflows/fidelly-email-enrichment.yml` : run manuel + automatique toutes les 3 heures.
- `apps_script/email_enrichment_patch.gs` : lecture exacte des lignes sans email + écriture atomique non destructive.

## Installation dans le repo Fidelly

Copier le contenu de ce pack à la racine de `Winnipowa/Fidelly` en conservant les dossiers.
Ajouter aussi `dnspython>=2.6.1` dans `requirements.txt`.

## Une seule étape côté Google Apps Script

Le bot possède un fallback compatible avec ton webhook V8/V9 actuel. Pour qu'il parcoure directement toutes les lignes vides du Sheet :

1. Ouvrir le projet Google Apps Script déjà utilisé par `SHEETS_WEBHOOK_URL`.
2. Ajouter le contenu de `apps_script/email_enrichment_patch.gs` à la suite de `Code.gs`.
3. Dans `doPost(e)`, juste après le parsing du JSON et la validation du token, ajouter :

```javascript
const emailResponse = fidellyEmailEnrichmentRoute_(body);
if (emailResponse) return emailResponse;
```

Si l'objet JSON parsé porte un autre nom que `body`, utiliser ce nom.

4. `Deploy` > `Manage deployments` > crayon > `New version` > `Deploy`.

L'URL `SHEETS_WEBHOOK_URL` et le token existants ne changent pas.

## Fonctionnement

Chaque run :

1. prend jusqu'à 40 lignes dont `email` est vide ;
2. regarde d'abord le site officiel + contact/mentions légales ;
3. lance plusieurs recherches web si nécessaire ;
4. croise nom, ville, CP, adresse, SIRET/SIREN et domaine ;
5. contrôle le domaine mail ;
6. n'écrit qu'au-dessus du score de confiance (80 par défaut) ;
7. relit la ligne juste avant écriture ;
8. le patch Apps Script utilise `LockService` et refuse toute écriture si Email n'est plus vide.

Le workflow automatique est prévu toutes les 3 heures à la minute 17.

## Règles de sécurité

Le bot ne doit jamais :

- supprimer une ligne ;
- vider une cellule ;
- remplacer un email existant ;
- fabriquer une adresse du type `contact@domaine.fr` ;
- accepter un homonyme uniquement parce que le nom ressemble.
