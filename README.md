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
