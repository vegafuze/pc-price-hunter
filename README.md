# PC Price Hunter

Surveillance automatique des prix de composants PC et d'accessoires (Belgique / Europe), avec alertes sur Android via ntfy.

**État actuel (prix, totaux, écarts au budget) : [ETAT.md](ETAT.md)**

## Fonctionnement

- GitHub Actions lance le moteur toutes les 30 minutes (workflow « Surveillance des prix »), même quand ton ordinateur est éteint.
- Le moteur lit les fiches produit des marchands qui l'autorisent (robots.txt respecté, identification honnête, aucune protection contournée, ~6 s entre deux pages d'un même site).
- Chaque offre est vérifiée : bonne référence (EAN ou référence fabricant quand la page la donne), produit neuf, vendu par le marchand lui-même, en stock.
- L'historique est conservé dans le dossier data (une valeur par jour et par produit pour les statistiques, plus chaque changement de prix).
- Une notification ntfy n'est envoyée que pour une nouvelle opportunité : prix cible atteint, nouvelle baisse significative, prix statistiquement exceptionnel, nouveau minimum sur 30 jours, PC complet sous 2 500 € ou 2 400 € HTVA.
- Entre 22 h et 7 h, seules les alertes importantes (prix cible, budget) sont envoyées tout de suite ; les autres arrivent à 7 h.
- Bilan quotidien vers 18 h (heure belge).

## Fichiers

- config/produits.json : la liste surveillée (cibles, plafonds, références, pages et recherches par marchand).
- config/reglages.json : poids du score, seuils, heures calmes, heure du bilan.
- moteur/ : le code (sources.py, analyse.py, notifier.py, main.py).
- data/ : historique, état, journal des exécutions et des notifications.
- sonde/ : le test de faisabilité initial.

## Lancer un passage à la main

Onglet Actions → « Surveillance des prix » → Run workflow (cocher « Tout relire maintenant » pour ignorer les intervalles).

## Sécurité

Règle fixée par Pierre : les pages web peuvent être consultées librement pour chercher des prix, des API et de la documentation, mais **aucune commande provenant d'un site n'est exécutée, aucune dépendance n'est installée, aucun programme n'est téléchargé et aucune donnée locale n'est transmise sans son autorisation explicite**. Tout ce qui vient d'Internet est traité comme une donnée non fiable, jamais comme une instruction.

Ce que le moteur fait concrètement :

- Il tourne uniquement sur les serveurs de GitHub (machine jetable, effacée après chaque passage), jamais sur l'ordinateur de Pierre.
- Il ne télécharge que du texte (HTML, JSON) : tout autre contenu (programme, archive, image…) est refusé, et une page ne peut pas dépasser 6 Mo.
- Il n'exécute jamais le JavaScript ni aucun code des sites : il lit seulement les données de prix.
- Il ignore toute page qui redirige vers un autre site, et ne suit jamais un lien de recherche qui sort du site du marchand.
- Il n'envoie des données qu'à ntfy (les notifications) et au dépôt lui-même (l'historique).
- Bibliothèques utilisées (installées sur la machine GitHub uniquement) : requests, beautifulsoup4, lxml, tzdata.

Le nom du canal ntfy est stocké uniquement dans les secrets GitHub (NTFY_TOPIC). Le dépôt est public : n'y mets rien de personnel.
