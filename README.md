# ClickRush

Site de test de vitesse de clic avec comptes utilisateurs et classement mondial.

## Déploiement Render

- **Build Command** : `pip install -r requirements.txt`
- **Start Command** : `gunicorn serveur:app`
- Variables d'environnement à configurer dans Render :
  - `DATABASE_URL` : URL de connexion PostgreSQL fournie par Neon (garder la valeur secrète).
  - `SECRET_KEY` : longue chaîne aléatoire privée.

La base PostgreSQL est utilisée si `DATABASE_URL` est défini. Sans cette variable, le serveur utilise SQLite localement pour les essais sur le PC.

## Essai local Windows

1. Installer Python 3.10 ou plus récent.
2. Dans ce dossier, ouvrir l'invite de commandes.
3. `py -m pip install -r requirements.txt`
4. `py serveur.py`
5. Ouvrir http://127.0.0.1:8766

Ne publie jamais la valeur de `DATABASE_URL` ou `SECRET_KEY` dans un dépôt GitHub.

## Application Windows (.exe et .zip)

Le dossier `desktop/` contient l'application ClickRush pour Windows. Elle ouvre le site ClickRush hébergé en ligne : une connexion Internet est donc nécessaire, et les comptes, scores, amis et ligues restent synchronisés avec le site.

Pour générer les fichiers Windows :
1. Installer Node.js 22 ou plus récent.
2. Ouvrir un terminal dans `desktop/`.
3. Exécuter `npm install`.
4. Exécuter `npm run dist:win`.

Le dossier `desktop/dist/` contient :
- `ClickRush-1.0.0-x64.exe` : installateur Windows.
- `ClickRush-1.0.0-x64.zip` : version portable à extraire ; lancer ensuite `ClickRush.exe`.

GitHub Actions génère également ces deux fichiers à chaque modification du dossier `desktop/`. Les fichiers sont disponibles dans l'onglet **Actions**, dans les artefacts du workflow **Build ClickRush for Windows** (conservés 30 jours).
