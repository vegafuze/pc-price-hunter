# PC Price Hunter

Surveillance des prix de composants PC (Belgique / Europe) avec GitHub Actions et notifications ntfy.

## Étape actuelle : sonde de faisabilité

Avant de construire l'application, on vérifie **depuis les serveurs GitHub** quels marchands laissent lire leurs prix proprement :

- `sonde/targets.json` : les pages testées (Ryzen 7 9800X3D, RTX 5070 Ti AORUS MASTER, Kingston FURY Beast RGB 32 Go, plus 4 pages Shopify en bonus) ;
- `sonde/probe.py` : le script qui lit ces pages ;
- `.github/workflows/sonde.yml` : le lancement manuel sur GitHub.

Règles du script : `robots.txt` respecté, User-Agent honnête, aucune tentative de contourner une protection, une requête toutes les ~6 s par site, aucun prix inventé.

Résultats : `results/sonde-latest.md` (lisible) et `results/sonde-latest.json` (brut).

## Lancer la sonde

1. Onglet **Actions** du dépôt → **Sonde de faisabilité** → **Run workflow**.
2. Attendre 2 à 4 minutes (pastille verte).
3. Ouvrir `results/sonde-latest.md`.

Facultatif : créer le secret `NTFY_TOPIC` (Settings → Secrets and variables → Actions) pour recevoir une notification de test à la fin.

## Sécurité

- Aucune clé ni aucun sujet ntfy dans le code : uniquement dans **GitHub Secrets**.
- Le dépôt est public : n'y mets rien de personnel.
