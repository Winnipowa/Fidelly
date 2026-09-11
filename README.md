# Fidelly Prospect Finder v6 Cloud

Objectif : faire tourner la prospection entièrement en ligne et alimenter automatiquement un Google Sheet dans Google Drive.

## Architecture

GitHub Actions
→ `fidelly_prospect_finder_v6_cloud.py`
→ Google Apps Script Web App
→ Google Drive / `Fidelly Prospection`
→ Google Sheet `Fidelly Prospects`

Le PC peut être éteint.

---

## 1. Google Apps Script — une seule fois

1. Ouvrir https://script.google.com/
2. Créer un **Nouveau projet**
3. Remplacer le contenu de `Code.gs` par le fichier `apps_script/Code.gs`
4. Dans **Project Settings > Script Properties**, ajouter :

   - `WEBHOOK_TOKEN` = un secret long, par exemple une chaîne aléatoire de 40+ caractères
   - `FOLDER_NAME` = `Fidelly Prospection`
   - `SPREADSHEET_NAME` = `Fidelly Prospects`

5. **Deploy > New deployment**
6. Type : **Web app**
7. Execute as : **Me**
8. Who has access : l'option permettant à GitHub d'appeler le webhook sans authentification Google interactive
9. Deploy
10. Copier l'URL qui finit généralement par `/exec`

Au premier envoi valide, Apps Script crée automatiquement :
- dossier Drive : `Fidelly Prospection`
- Google Sheet : `Fidelly Prospects`
- onglet `Prospects`
- onglet `Runs`

Le tableau est dédupliqué par SIRET : un commerce déjà présent est mis à jour au lieu d'être dupliqué.

---

## 2. GitHub

Créer un dépôt privé, puis y mettre :

- `fidelly_prospect_finder_v6_cloud.py`
- `requirements.txt`
- dossier `.github/workflows/`
- `.gitignore`

Dans GitHub :

**Settings > Secrets and variables > Actions > New repository secret**

Créer :

### `SHEETS_WEBHOOK_URL`
URL `/exec` du Web App Apps Script.

### `SHEETS_WEBHOOK_TOKEN`
Exactement le même secret que `WEBHOOK_TOKEN` dans Apps Script.

Ne jamais mettre ces valeurs directement dans le code.

---

## 3. Lancer

GitHub :
**Actions > Fidelly Prospection > Run workflow**

Entrer par exemple :

Zones :
```text
Issoudun|Bourges|Châteauroux|Vierzon
```

Activités :
```text
restaurant|café|bar|caviste|coiffeur|barbier|institut de beauté|CBD|fleuriste|animalerie|boulangerie
```

Nombre :
```text
300
```

Puis `Run workflow`.

---

## 4. Résultat

Le Google Sheet contient notamment :

- Priority
- Score
- Zone
- Activité
- Nom
- Adresse
- SIRET / SIREN / NAF
- Site
- Email
- Source email
- Téléphone
- Instagram / Facebook / TikTok / LinkedIn
- Fidélité détectée
- Source enrichissement
- First seen
- Last seen

`Runs` garde un historique de chaque scan.

GitHub conserve aussi le CSV de chaque exécution pendant 30 jours.

---

## 5. Automatisation

Le workflow contient déjà un exemple de `schedule`.

Pour un scan chaque lundi, décommenter :

```yaml
schedule:
  - cron: '0 8 * * 1'
```

Pour éviter de collecter toujours les mêmes zones, il est préférable à terme d'avoir une liste de zones/segments à scanner progressivement.

---

## Sécurité

- Dépôt GitHub privé recommandé.
- Secrets uniquement dans GitHub Actions Secrets.
- Token Apps Script dans Script Properties.
- Ne jamais publier le webhook token dans le dépôt.
- Les coordonnées doivent être des coordonnées professionnelles rendues publiques.
