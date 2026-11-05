from flask import Flask, request, jsonify, session, send_from_directory
from werkzeug.security import generate_password_hash, check_password_hash
from contextlib import contextmanager
from pathlib import Path
import os, re, secrets, time, sqlite3, hmac, hashlib

BASE_DIR = Path(__file__).resolve().parent
DATABASE_URL = os.environ.get('DATABASE_URL', '').strip()
app = Flask(__name__, static_folder=None)
app.secret_key = os.environ.get('SECRET_KEY') or secrets.token_hex(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=bool(DATABASE_URL) or os.environ.get('COOKIE_SECURE') == '1',
    MAX_CONTENT_LENGTH=1_500_000,
)

# PostgreSQL sur l'hébergeur ; SQLite automatiquement pour les essais sur le PC.
class DB:
    def __init__(self, conn, postgres=False):
        self.conn, self.postgres = conn, postgres
    def execute(self, sql, params=()):
        if self.postgres:
            from psycopg.rows import dict_row
            sql = sql.replace('?', '%s')
            cur = self.conn.cursor(row_factory=dict_row)
            cur.execute(sql, params)
            return cur
        return self.conn.execute(sql, params)

_PG_POOL = None

def get_pg_pool():
    global _PG_POOL
    if _PG_POOL is None:
        from psycopg_pool import ConnectionPool
        url = DATABASE_URL.replace('postgres://', 'postgresql://', 1)
        _PG_POOL = ConnectionPool(
            conninfo=url, min_size=1, max_size=5,
            kwargs={'sslmode': 'require'}, open=False
        )
        _PG_POOL.open(wait=False)
    return _PG_POOL

@contextmanager
def db():
    if DATABASE_URL:
        with get_pg_pool().connection() as conn:
            yield DB(conn, True)
    else:
        conn = sqlite3.connect(BASE_DIR / 'clics.db')
        conn.row_factory = sqlite3.Row
        try:
            yield DB(conn, False)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

def init_db():
    with db() as con:
        if con.postgres:
            con.execute('''CREATE TABLE IF NOT EXISTS users (
                id BIGSERIAL PRIMARY KEY, username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL, created_at BIGINT NOT NULL)''')
            con.execute('''CREATE TABLE IF NOT EXISTS scores (
                id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                clicks INTEGER NOT NULL, duration INTEGER NOT NULL, cps DOUBLE PRECISION NOT NULL,
                created_at BIGINT NOT NULL, auto_status TEXT NOT NULL DEFAULT 'clear', auto_reason TEXT NOT NULL DEFAULT '', click_pattern TEXT NOT NULL DEFAULT '', auto_processed BOOLEAN NOT NULL DEFAULT FALSE)''')
        else:
            con.execute('''CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL COLLATE NOCASE UNIQUE,
                password_hash TEXT NOT NULL, created_at INTEGER NOT NULL)''')
            con.execute('''CREATE TABLE IF NOT EXISTS scores (
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                clicks INTEGER NOT NULL, duration INTEGER NOT NULL, cps REAL NOT NULL, created_at INTEGER NOT NULL,
                auto_status TEXT NOT NULL DEFAULT 'clear', auto_reason TEXT NOT NULL DEFAULT '', click_pattern TEXT NOT NULL DEFAULT '', auto_processed INTEGER NOT NULL DEFAULT 0)''')
        if con.postgres:
            con.execute('ALTER TABLE users ADD COLUMN IF NOT EXISTS avatar_data TEXT')
            con.execute('ALTER TABLE clubs ADD COLUMN IF NOT EXISTS is_public BOOLEAN NOT NULL DEFAULT TRUE')
            con.execute("ALTER TABLE scores ADD COLUMN IF NOT EXISTS auto_status TEXT NOT NULL DEFAULT 'clear'")
            con.execute("ALTER TABLE scores ADD COLUMN IF NOT EXISTS auto_reason TEXT NOT NULL DEFAULT ''")
            con.execute("ALTER TABLE scores ADD COLUMN IF NOT EXISTS click_pattern TEXT NOT NULL DEFAULT ''")
            con.execute('ALTER TABLE scores ADD COLUMN IF NOT EXISTS auto_processed BOOLEAN NOT NULL DEFAULT FALSE')
            con.execute('ALTER TABLE scores ADD COLUMN IF NOT EXISTS replay_data TEXT NOT NULL DEFAULT \'[]\'')
            con.execute('''CREATE TABLE IF NOT EXISTS player_notifications (
                id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                body TEXT NOT NULL, created_at BIGINT NOT NULL, seen BOOLEAN NOT NULL DEFAULT FALSE)''')
            con.execute('''CREATE TABLE IF NOT EXISTS player_reports (
                id BIGSERIAL PRIMARY KEY, reporter_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                target_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                reason TEXT NOT NULL, created_at BIGINT NOT NULL)''')
        else:
            columns = [row['name'] for row in con.execute('PRAGMA table_info(users)').fetchall()]
            if 'avatar_data' not in columns:
                con.execute('ALTER TABLE users ADD COLUMN avatar_data TEXT')
            score_columns = [row['name'] for row in con.execute('PRAGMA table_info(scores)').fetchall()]
            if 'auto_status' not in score_columns: con.execute("ALTER TABLE scores ADD COLUMN auto_status TEXT NOT NULL DEFAULT 'clear'")
            if 'auto_reason' not in score_columns: con.execute("ALTER TABLE scores ADD COLUMN auto_reason TEXT NOT NULL DEFAULT ''")
            if 'click_pattern' not in score_columns: con.execute("ALTER TABLE scores ADD COLUMN click_pattern TEXT NOT NULL DEFAULT ''")
            if 'auto_processed' not in score_columns: con.execute('ALTER TABLE scores ADD COLUMN auto_processed INTEGER NOT NULL DEFAULT 0')
            if 'replay_data' not in score_columns: con.execute("ALTER TABLE scores ADD COLUMN replay_data TEXT NOT NULL DEFAULT '[]'")
            con.execute('''CREATE TABLE IF NOT EXISTS player_notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                body TEXT NOT NULL, created_at INTEGER NOT NULL, seen INTEGER NOT NULL DEFAULT 0)''')
            con.execute('''CREATE TABLE IF NOT EXISTS player_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT, reporter_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                target_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                reason TEXT NOT NULL, created_at INTEGER NOT NULL)''')
        con.execute('CREATE INDEX IF NOT EXISTS idx_scores_cps ON scores(cps DESC)')
        con.execute('CREATE INDEX IF NOT EXISTS idx_scores_user ON scores(user_id, created_at DESC)')
        con.execute('CREATE INDEX IF NOT EXISTS idx_scores_auto_created ON scores(auto_processed, created_at DESC)')
        if con.postgres:
            con.execute("""CREATE TABLE IF NOT EXISTS friendships (
                user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                friend_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at BIGINT NOT NULL, PRIMARY KEY(user_id, friend_id))""")
            con.execute("""CREATE TABLE IF NOT EXISTS friend_requests (
                id BIGSERIAL PRIMARY KEY, sender_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                recipient_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                status TEXT NOT NULL DEFAULT 'pending', created_at BIGINT NOT NULL)""")
            con.execute("""CREATE TABLE IF NOT EXISTS direct_messages (
                id BIGSERIAL PRIMARY KEY, sender_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                recipient_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                body TEXT NOT NULL, created_at BIGINT NOT NULL)""")
            con.execute("""CREATE TABLE IF NOT EXISTS message_read_state (
                user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                scope TEXT NOT NULL, conversation_id BIGINT NOT NULL, last_read_at BIGINT NOT NULL,
                PRIMARY KEY(user_id,scope,conversation_id))""")
            con.execute("""CREATE TABLE IF NOT EXISTS group_conversations (
                id BIGSERIAL PRIMARY KEY, name TEXT NOT NULL, owner_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at BIGINT NOT NULL)""")
            con.execute("""CREATE TABLE IF NOT EXISTS group_members (
                conversation_id BIGINT NOT NULL REFERENCES group_conversations(id) ON DELETE CASCADE,
                user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                joined_at BIGINT NOT NULL, PRIMARY KEY(conversation_id,user_id))""")
            con.execute("""CREATE TABLE IF NOT EXISTS group_messages (
                id BIGSERIAL PRIMARY KEY, conversation_id BIGINT NOT NULL REFERENCES group_conversations(id) ON DELETE CASCADE,
                sender_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                body TEXT NOT NULL, created_at BIGINT NOT NULL)""")
            con.execute("""CREATE TABLE IF NOT EXISTS clubs (
                id BIGSERIAL PRIMARY KEY, name TEXT NOT NULL UNIQUE, owner_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at BIGINT NOT NULL)""")
            con.execute("""CREATE TABLE IF NOT EXISTS club_members (
                club_id BIGINT NOT NULL REFERENCES clubs(id) ON DELETE CASCADE,
                user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                joined_at BIGINT NOT NULL, PRIMARY KEY(club_id,user_id))""")
            con.execute("""CREATE TABLE IF NOT EXISTS club_messages (
                id BIGSERIAL PRIMARY KEY, club_id BIGINT NOT NULL REFERENCES clubs(id) ON DELETE CASCADE,
                user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                body TEXT NOT NULL, created_at BIGINT NOT NULL)""")
        else:
            con.execute("""CREATE TABLE IF NOT EXISTS friendships (
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                friend_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at INTEGER NOT NULL, PRIMARY KEY(user_id, friend_id))""")
            con.execute("""CREATE TABLE IF NOT EXISTS friend_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT, sender_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                recipient_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                status TEXT NOT NULL DEFAULT 'pending', created_at INTEGER NOT NULL)""")
            con.execute("""CREATE TABLE IF NOT EXISTS direct_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT, sender_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                recipient_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                body TEXT NOT NULL, created_at INTEGER NOT NULL)""")
            con.execute("""CREATE TABLE IF NOT EXISTS message_read_state (
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                scope TEXT NOT NULL, conversation_id INTEGER NOT NULL, last_read_at INTEGER NOT NULL,
                PRIMARY KEY(user_id,scope,conversation_id))""")
            con.execute("""CREATE TABLE IF NOT EXISTS group_conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
                owner_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, created_at INTEGER NOT NULL)""")
            con.execute("""CREATE TABLE IF NOT EXISTS group_members (
                conversation_id INTEGER NOT NULL REFERENCES group_conversations(id) ON DELETE CASCADE,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                joined_at INTEGER NOT NULL, PRIMARY KEY(conversation_id,user_id))""")
            con.execute("""CREATE TABLE IF NOT EXISTS group_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id INTEGER NOT NULL REFERENCES group_conversations(id) ON DELETE CASCADE,
                sender_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                body TEXT NOT NULL, created_at INTEGER NOT NULL)""")
            con.execute("""CREATE TABLE IF NOT EXISTS clubs (
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                owner_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, created_at INTEGER NOT NULL)""")
            con.execute("""CREATE TABLE IF NOT EXISTS club_members (
                club_id INTEGER NOT NULL REFERENCES clubs(id) ON DELETE CASCADE,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                joined_at INTEGER NOT NULL, PRIMARY KEY(club_id,user_id))""")
            con.execute("""CREATE TABLE IF NOT EXISTS club_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT, club_id INTEGER NOT NULL REFERENCES clubs(id) ON DELETE CASCADE,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                body TEXT NOT NULL, created_at INTEGER NOT NULL)""")
        con.execute('CREATE INDEX IF NOT EXISTS idx_friend_requests_recipient ON friend_requests(recipient_id,status)')
        con.execute('CREATE INDEX IF NOT EXISTS idx_dm_pair ON direct_messages(sender_id,recipient_id,created_at)')
        con.execute('CREATE INDEX IF NOT EXISTS idx_group_members_user ON group_members(user_id,conversation_id)')
        con.execute('CREATE INDEX IF NOT EXISTS idx_group_messages ON group_messages(conversation_id,created_at)')
        if not con.postgres:
            club_columns = [row['name'] for row in con.execute('PRAGMA table_info(clubs)').fetchall()]
            if 'is_public' not in club_columns: con.execute('ALTER TABLE clubs ADD COLUMN is_public INTEGER NOT NULL DEFAULT 1')
        con.execute('CREATE INDEX IF NOT EXISTS idx_club_messages ON club_messages(club_id,created_at)')
        con.execute('CREATE INDEX IF NOT EXISTS idx_dm_recipient_pair ON direct_messages(recipient_id,sender_id,created_at)')
        con.execute('CREATE INDEX IF NOT EXISTS idx_scores_duration_cps ON scores(duration,cps DESC,user_id)')

        if con.postgres:
            con.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS role TEXT NOT NULL DEFAULT 'user'")
            con.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_banned BOOLEAN NOT NULL DEFAULT FALSE")
            con.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS ban_reason TEXT")
            con.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS ip_hash TEXT")
            con.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS anti_cheat_strikes INTEGER NOT NULL DEFAULT 0")
            con.execute("""CREATE TABLE IF NOT EXISTS admin_settings (
                id INTEGER PRIMARY KEY, creator_user_id BIGINT REFERENCES users(id) ON DELETE SET NULL)""")
            con.execute("""CREATE TABLE IF NOT EXISTS admin_requests (
                id BIGSERIAL PRIMARY KEY, requester_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                action TEXT NOT NULL, target_user_id BIGINT REFERENCES users(id) ON DELETE CASCADE,
                score_id BIGINT, reason TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'pending',
                reviewer_id BIGINT REFERENCES users(id) ON DELETE SET NULL, created_at BIGINT NOT NULL,
                reviewed_at BIGINT)""")
        else:
            columns = [row['name'] for row in con.execute('PRAGMA table_info(users)').fetchall()]
            if 'role' not in columns: con.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'user'")
            if 'is_banned' not in columns: con.execute("ALTER TABLE users ADD COLUMN is_banned INTEGER NOT NULL DEFAULT 0")
            if 'ban_reason' not in columns: con.execute("ALTER TABLE users ADD COLUMN ban_reason TEXT")
            if 'ip_hash' not in columns: con.execute("ALTER TABLE users ADD COLUMN ip_hash TEXT")
            if 'anti_cheat_strikes' not in columns: con.execute("ALTER TABLE users ADD COLUMN anti_cheat_strikes INTEGER NOT NULL DEFAULT 0")
            con.execute("""CREATE TABLE IF NOT EXISTS admin_settings (
                id INTEGER PRIMARY KEY, creator_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL)""")
            con.execute("""CREATE TABLE IF NOT EXISTS admin_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT, requester_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                action TEXT NOT NULL, target_user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                score_id INTEGER, reason TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'pending',
                reviewer_id INTEGER REFERENCES users(id) ON DELETE SET NULL, created_at INTEGER NOT NULL,
                reviewed_at INTEGER)""")
        con.execute('INSERT INTO admin_settings(id,creator_user_id) VALUES(1,NULL) ON CONFLICT DO NOTHING')
        creator = con.execute('SELECT id FROM users WHERE lower(username)=lower(?)', ('pledoux73',)).fetchone()
        if creator:
            con.execute('UPDATE admin_settings SET creator_user_id=? WHERE id=1 AND creator_user_id IS NULL', (creator['id'],))
        con.execute("""UPDATE users SET role='creator' WHERE id=(
            SELECT creator_user_id FROM admin_settings WHERE id=1 AND creator_user_id IS NOT NULL)""")
        con.execute('CREATE INDEX IF NOT EXISTS idx_admin_requests_status ON admin_requests(status,created_at DESC)')



def current_user():
    uid = session.get('user_id')
    if not uid: return None
    # En mode possession, seul le compte créateur d'origine peut reprendre la main.
    original_creator_id = session.get('impersonator_id')
    with db() as con:
        if original_creator_id:
            owner = con.execute("SELECT id,role FROM users WHERE id=?", (original_creator_id,)).fetchone()
            if not owner or owner['role'] != 'creator':
                session.clear()
                return None
        row = con.execute('SELECT id, username, created_at, role, is_banned, ban_reason FROM users WHERE id=?', (uid,)).fetchone()
    if not row: return None
    user = dict(row)
    # Le compte créateur pledoux73 est protégé contre toute suspension.
    # Si une ancienne règle ou une modification SQL l'a banni, on répare son état.
    if user.get('role') == 'creator' and user.get('is_banned'):
        with db() as con:
            con.execute('UPDATE users SET is_banned=?,ban_reason=NULL,anti_cheat_strikes=0 WHERE id=?',
                (False if con.postgres else 0, user['id']))
        user['is_banned'] = False
        user['ban_reason'] = None
    if user.get('is_banned'):
        session.clear()
        return None
    return user

def is_staff(user):
    return bool(user and user.get('role') in ('creator','admin','mod'))

def staff_required():
    user = current_user()
    if not user: return None, (jsonify(error='Connecte-toi avec un compte autorisé.'), 401)
    if not is_staff(user): return None, (jsonify(error='Accès réservé à l’équipe de modération.'), 403)
    return user, None

def protected_from(actor, target):
    # Admins cannot act on creator accounts or other admins. Only the creator can.
    if target['role'] == 'creator': return actor['role'] != 'creator'
    if target['role'] == 'admin': return actor['role'] != 'creator'
    return False

def apply_staff_action(con, actor, action, target_id=None, score_id=None, reason=''):
    if action in ('ban','unban'):
        target = con.execute('SELECT id,role FROM users WHERE id=?', (target_id,)).fetchone()
        if not target: return 'Joueur introuvable.'
        if target['id'] == actor['id']: return 'Tu ne peux pas appliquer cette action à ton propre compte.'
        if target['role'] == 'creator' and action == 'ban': return 'Le compte créateur est protégé contre les bannissements.'
        if protected_from(actor, target): return 'Tu ne peux pas modifier ce compte.'
        if action == 'ban':
            con.execute('UPDATE users SET is_banned=?,ban_reason=? WHERE id=?', (True if con.postgres else 1, reason[:300], target_id))
        else:
            con.execute('UPDATE users SET is_banned=?,ban_reason=NULL WHERE id=?', (False if con.postgres else 0, target_id))
        return None
    if action == 'delete_score':
        score = con.execute('SELECT s.id,u.id AS user_id,u.role FROM scores s JOIN users u ON u.id=s.user_id WHERE s.id=?', (score_id,)).fetchone()
        if not score: return 'Score introuvable.'
        if score['user_id'] == actor['id'] and actor['role'] != 'creator': return 'Tu ne peux pas supprimer tes propres scores.'
        if protected_from(actor, score): return 'Tu ne peux pas supprimer les scores de ce compte.'
        con.execute('DELETE FROM scores WHERE id=?', (score_id,))
        return None
    return 'Action non autorisée.'

def client_ip_fingerprint():
    # Render transmet l'adresse cliente dans X-Forwarded-For.
    # Le reverse proxy doit écraser ce header (ne pas exposer Flask directement).
    forwarded = request.headers.get('X-Forwarded-For', '')
    # Le proxy ajoute l'adresse cliente à la fin de la chaîne X-Forwarded-For.
    ip = (forwarded.split(',')[-1].strip() if forwarded else request.remote_addr) or ''
    if not ip:
        return None
    # HMAC : l'adresse IP brute n'est jamais enregistrée en base.
    key_material = os.environ.get('IP_HASH_SECRET') or os.environ.get('SECRET_KEY')
    # Sans secret stable configuré dans Render, on désactive le rapprochement IP
    # plutôt que de créer des empreintes différentes à chaque redémarrage.
    if not key_material:
        return None
    key = key_material.encode('utf-8')
    return hmac.new(key, ip.encode('utf-8'), hashlib.sha256).hexdigest()


def require_csrf():
    token = request.headers.get('X-CSRF-Token', '')
    if not token or not secrets.compare_digest(token, session.get('csrf_token', '')):
        return jsonify(error='Session expirée ou requête invalide. Recharge la page.'), 403
    return None

@app.after_request
def security_headers(resp):
    resp.headers['X-Content-Type-Options'] = 'nosniff'
    resp.headers['X-Frame-Options'] = 'DENY'
    resp.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    if request.path.startswith('/api/'):
        resp.headers['Cache-Control'] = 'no-store'
    elif request.path == '/':
        resp.headers['Cache-Control'] = 'no-cache'
    return resp

@app.get('/')
def home(): return send_from_directory(BASE_DIR, 'index.html')

@app.get('/api/state')
def state():
    user = current_user()
    if 'csrf_token' not in session: session['csrf_token'] = secrets.token_urlsafe(32)
    result = {'user': user, 'csrf': session['csrf_token']}
    with db() as con:
        creator = con.execute("SELECT username, avatar_data FROM users WHERE role='creator' ORDER BY id LIMIT 1").fetchone()
    result['creator_profile'] = dict(creator) if creator else {'username': 'pledoux73', 'avatar_data': None}
    if user and session.get('impersonator_id'):
        result['impersonation'] = {'active': True, 'username': user['username']}
    if user:
        with db() as con:
            profile = con.execute('SELECT avatar_data FROM users WHERE id=?', (user['id'],)).fetchone()
            best = con.execute('SELECT MAX(cps) AS best, COUNT(*) AS total FROM scores WHERE user_id=?', (user['id'],)).fetchone()
            recent = con.execute('SELECT id, clicks, duration, cps, created_at FROM scores WHERE user_id=? ORDER BY created_at DESC LIMIT 10', (user['id'],)).fetchall()
            notices = con.execute('SELECT id,body,created_at FROM player_notifications WHERE user_id=? AND seen=? ORDER BY created_at DESC LIMIT 5', (user['id'], False if con.postgres else 0)).fetchall()
            if notices:
                con.execute('UPDATE player_notifications SET seen=? WHERE user_id=?', (True if con.postgres else 1, user['id']))
        user['avatar_data'] = profile['avatar_data'] if profile else None
        result['stats'] = {'best': round(best['best'] or 0, 2), 'total': best['total']}
        result['history'] = [dict(r) for r in recent]
        result['notifications'] = [dict(r) for r in notices]
    return jsonify(result)

@app.post('/api/profile/history/<int:score_id>/delete')
def delete_own_history_score(score_id):
    bad = require_csrf()
    if bad: return bad
    actor = current_user()
    if not actor: return jsonify(error='Connecte-toi pour continuer.'), 401
    if actor.get('role') != 'creator': return jsonify(error='Action réservée au compte créateur.'), 403
    with db() as con:
        score = con.execute('SELECT id,user_id FROM scores WHERE id=?', (score_id,)).fetchone()
        if not score: return jsonify(error='Cette partie est introuvable.'), 404
        if int(score['user_id']) != int(actor['id']): return jsonify(error='Tu peux uniquement supprimer tes propres parties ici.'), 403
        con.execute('DELETE FROM scores WHERE id=?', (score_id,))
    return jsonify(ok=True)

@app.post('/api/register')
def register():
    bad = require_csrf()
    if bad: return bad
    data = request.get_json(silent=True) or {}
    username, password = str(data.get('username', '')).strip(), str(data.get('password', ''))
    if not re.fullmatch(r'[A-Za-z0-9_]{3,18}', username):
        return jsonify(error='Le pseudo doit contenir 3 à 18 caractères : lettres, chiffres ou _.'), 400
    if len(password) < 8 or len(password) > 128:
        return jsonify(error='Le mot de passe doit contenir au moins 8 caractères.'), 400
    ip_hash = client_ip_fingerprint()
    try:
        with db() as con:
            # Un nouvel identifiant créé depuis une IP déjà associée à un compte banni
            # est automatiquement suspendu. L'IP est hachée, jamais stockée en clair.
            linked_to_ban = bool(ip_hash and con.execute(
                'SELECT 1 FROM users WHERE ip_hash=? AND is_banned=? LIMIT 1',
                (ip_hash, True if con.postgres else 1)
            ).fetchone())
            if con.postgres:
                row = con.execute('''INSERT INTO users(username,password_hash,created_at,ip_hash,is_banned,ban_reason)
                    VALUES(?,?,?,?,?,?) RETURNING id''',
                    (username, generate_password_hash(password), int(time.time()), ip_hash,
                     linked_to_ban, 'Compte lié à une adresse IP associée à un compte banni.' if linked_to_ban else None)).fetchone()
                uid = row['id']
            else:
                cur = con.execute('''INSERT INTO users(username,password_hash,created_at,ip_hash,is_banned,ban_reason)
                    VALUES(?,?,?,?,?,?)''',
                    (username, generate_password_hash(password), int(time.time()), ip_hash,
                     1 if linked_to_ban else 0, 'Compte lié à une adresse IP associée à un compte banni.' if linked_to_ban else None))
                uid = cur.lastrowid
    except Exception as exc:
        if 'unique' in str(exc).lower() or 'duplicate' in str(exc).lower():
            return jsonify(error='Ce pseudo est déjà utilisé.'), 409
        raise
    session.clear(); session['user_id'] = uid; session['csrf_token'] = secrets.token_urlsafe(32)
    return jsonify(ok=True)

@app.post('/api/login')
def login():
    bad = require_csrf()
    if bad: return bad
    data = request.get_json(silent=True) or {}
    username, password = str(data.get('username', '')).strip(), str(data.get('password', ''))
    with db() as con:
        user = con.execute('SELECT id, password_hash FROM users WHERE username=? COLLATE NOCASE' if not con.postgres else 'SELECT id, password_hash FROM users WHERE lower(username)=lower(?)', (username,)).fetchone()
    if not user or not check_password_hash(user['password_hash'], password):
        return jsonify(error='Pseudo ou mot de passe incorrect.'), 401
    ip_hash = client_ip_fingerprint()
    with db() as con:
        account = con.execute('SELECT is_banned,ban_reason,role,ip_hash FROM users WHERE id=?', (user['id'],)).fetchone()
        if account and not account['is_banned'] and ip_hash:
            linked_ban = con.execute('SELECT 1 FROM users WHERE ip_hash=? AND is_banned=? AND id<>? LIMIT 1',
                (ip_hash, True if con.postgres else 1, user['id'])).fetchone()
            # Un compte existant est seulement marqué par son IP à la connexion.
            # On ne bannit pas rétroactivement un foyer entier sur la seule IP.
            con.execute('UPDATE users SET ip_hash=? WHERE id=?', (ip_hash, user['id']))
    if account and account['is_banned']:
        return jsonify(error='Compte suspendu. Motif : '+str(account['ban_reason'] or 'Non précisé')), 403
    session.clear(); session['user_id'] = user['id']; session['csrf_token'] = secrets.token_urlsafe(32)
    return jsonify(ok=True)

@app.post('/api/admin/impersonate')
def start_impersonation():
    bad = require_csrf()
    if bad: return bad
    actor = current_user()
    if not actor or actor.get('role') != 'creator' or session.get('impersonator_id'):
        return jsonify(error='Cette fonction est réservée au créateur, hors mode possession.'), 403
    data = request.get_json(silent=True) or {}
    try: target_id = int(data.get('target_id'))
    except (TypeError, ValueError): return jsonify(error='Joueur invalide.'), 400
    with db() as con:
        target = con.execute('SELECT id,username,role,is_banned FROM users WHERE id=?', (target_id,)).fetchone()
        if not target: return jsonify(error='Joueur introuvable.'), 404
        if target['id'] == actor['id'] or target['role'] not in ('user', 'mod', 'admin'):
            return jsonify(error='La possession est réservée aux comptes joueurs, modos et admins.'), 403
        if target['is_banned']:
            return jsonify(error='Impossible de prendre possession d’un compte suspendu.'), 403
    session['impersonator_id'] = actor['id']
    session['user_id'] = target_id
    return jsonify(ok=True, username=target['username'])

@app.post('/api/admin/stop-impersonation')
def stop_impersonation():
    bad = require_csrf()
    if bad: return bad
    original_id = session.get('impersonator_id')
    if not original_id:
        return jsonify(error='Aucune possession en cours.'), 400
    with db() as con:
        owner = con.execute("SELECT id,role FROM users WHERE id=?", (original_id,)).fetchone()
    if not owner or owner['role'] != 'creator':
        session.clear()
        return jsonify(error='Session créateur invalide. Reconnecte-toi.'), 403
    session['user_id'] = original_id
    session.pop('impersonator_id', None)
    session['csrf_token'] = secrets.token_urlsafe(32)
    return jsonify(ok=True)

@app.post('/api/logout')
def logout():
    bad = require_csrf()
    if bad: return bad
    if session.get('impersonator_id'):
        original_id = session.get('impersonator_id')
        session['user_id'] = original_id
        session.pop('impersonator_id', None)
        session['csrf_token'] = secrets.token_urlsafe(32)
        return jsonify(ok=True, impersonation_ended=True)
    session.clear(); return jsonify(ok=True)

@app.post('/api/profile')
def update_profile():
    bad = require_csrf()
    if bad: return bad
    user = current_user()
    if not user: return jsonify(error='Connecte-toi pour modifier ton profil.'), 401
    data = request.get_json(silent=True) or {}
    username = str(data.get('username', '')).strip()
    if not re.fullmatch(r'[A-Za-z0-9_]{3,18}', username):
        return jsonify(error='Le pseudo doit contenir 3 à 18 caractères : lettres, chiffres ou _.'), 400
    avatar_data = data.get('avatar')
    if 'avatar' not in data:
        with db() as con:
            old_avatar = con.execute('SELECT avatar_data FROM users WHERE id=?', (user['id'],)).fetchone()
        avatar_data = old_avatar['avatar_data'] if old_avatar else None
    if avatar_data is not None:
        avatar_data = str(avatar_data)
        match = re.fullmatch(r'data:image/(png|jpeg|webp);base64,([A-Za-z0-9+/]+={0,2})', avatar_data)
        if not match or len(avatar_data) > 1_100_000:
            return jsonify(error='Photo invalide ou trop volumineuse.'), 400
        import base64
        try:
            raw = base64.b64decode(match.group(2), validate=True)
        except Exception:
            return jsonify(error='Le fichier image est invalide.'), 400
        if len(raw) > 800_000:
            return jsonify(error='La photo doit faire moins de 800 Ko après compression.'), 400
        kind = match.group(1)
        valid = (kind == 'jpeg' and raw.startswith(bytes([255,216,255]))) or (kind == 'png' and raw.startswith(bytes([137,80,78,71,13,10,26,10]))) or (kind == 'webp' and raw[:4] == b'RIFF' and raw[8:12] == b'WEBP')
        if not valid:
            return jsonify(error='Le contenu du fichier ne correspond pas à une image valide.'), 400
    try:
        with db() as con:
            duplicate = con.execute('SELECT id FROM users WHERE lower(username)=lower(?) AND id<>?', (username, user['id'])).fetchone()
            if duplicate:
                return jsonify(error='Ce pseudo est déjà utilisé.'), 409
            con.execute('UPDATE users SET username=?, avatar_data=? WHERE id=?', (username, avatar_data, user['id']))
    except Exception as exc:
        if 'unique' in str(exc).lower() or 'duplicate' in str(exc).lower():
            return jsonify(error='Ce pseudo est déjà utilisé.'), 409
        raise
    return jsonify(ok=True)

@app.post('/api/profile/password')
def change_password():
    bad = require_csrf()
    if bad: return bad
    if session.get('impersonator_id'):
        return jsonify(error='La modification du mot de passe est désactivée pendant une prise de possession.'), 403
    actor = current_user()
    if not actor: return jsonify(error='Connecte-toi pour modifier ton mot de passe.'), 401
    data = request.get_json(silent=True) or {}
    current_password = str(data.get('current_password', ''))
    new_password = str(data.get('new_password', ''))
    if len(new_password) < 8 or len(new_password) > 128:
        return jsonify(error='Le nouveau mot de passe doit contenir entre 8 et 128 caractères.'), 400
    with db() as con:
        row = con.execute('SELECT password_hash FROM users WHERE id=?', (actor['id'],)).fetchone()
        if not row or not check_password_hash(row['password_hash'], current_password):
            return jsonify(error='Mot de passe actuel incorrect.'), 403
        con.execute('UPDATE users SET password_hash=? WHERE id=?',
            (generate_password_hash(new_password), actor['id']))
    return jsonify(ok=True)

@app.post('/api/reports')
def create_player_report():
    bad = require_csrf()
    if bad: return bad
    reporter = current_user()
    if not reporter: return jsonify(error='Connecte-toi pour signaler un joueur.'), 401
    data = request.get_json(silent=True) or {}
    target_name = str(data.get('username', '')).strip()
    reason = str(data.get('reason', '')).strip()
    if not re.fullmatch(r'[A-Za-z0-9_]{3,18}', target_name):
        return jsonify(error='Indique un pseudo valide.'), 400
    if len(reason) < 8 or len(reason) > 500:
        return jsonify(error='Explique le motif en 8 à 500 caractères.'), 400
    now = int(time.time())
    with db() as con:
        target = con.execute('SELECT id,username,role FROM users WHERE lower(username)=lower(?)', (target_name,)).fetchone()
        if not target: return jsonify(error='Joueur introuvable.'), 404
        if int(target['id']) == int(reporter['id']):
            return jsonify(error='Tu ne peux pas te signaler toi-même.'), 400
        if target['role'] == 'creator':
            return jsonify(error='Le compte créateur ne peut pas être signalé.'), 403
        recent = con.execute('SELECT id FROM player_reports WHERE reporter_id=? AND target_id=? AND created_at>? LIMIT 1',
            (reporter['id'], target['id'], now - 86400)).fetchone()
        if recent:
            return jsonify(error='Tu as déjà signalé ce joueur au cours des dernières 24 heures.'), 429
        con.execute('INSERT INTO player_reports(reporter_id,target_id,reason,created_at) VALUES(?,?,?,?)',
            (reporter['id'], target['id'], reason, now))
        creators = con.execute("SELECT id FROM users WHERE role='creator'").fetchall()
        for creator in creators:
            con.execute('INSERT INTO player_notifications(user_id,body,created_at) VALUES(?,?,?)',
                (creator['id'], '🚩 Signalement de '+reporter['username']+' contre '+target['username']+' : '+reason, now))
    return jsonify(ok=True)

def add_anti_cheat_strike(con, user_id, reason):
    con.execute('UPDATE users SET anti_cheat_strikes=anti_cheat_strikes+1 WHERE id=?', (user_id,))
    row = con.execute('SELECT anti_cheat_strikes FROM users WHERE id=?', (user_id,)).fetchone()
    strikes = int(row['anti_cheat_strikes']) if row else 0
    if strikes >= 3:
        con.execute('UPDATE users SET is_banned=?,ban_reason=? WHERE id=?',
            (True if con.postgres else 1, 'Bannissement automatique anti-triche : ' + reason[:200], user_id))
    return strikes


@app.post('/api/score')
def save_score():
    bad = require_csrf()
    if bad: return bad
    user = current_user()
    if not user: return jsonify(error='Connecte-toi pour sauvegarder ton score.'), 401
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict) or type(data.get('clicks')) is not int or type(data.get('duration')) is not int:
        return jsonify(error='Format de score invalide.'), 400
    clicks, duration = data['clicks'], data['duration']
    if duration not in (5, 10, 15, 30, 60) or clicks < 0 or clicks > duration * 30:
        return jsonify(error='Score hors limites.'), 400
    cps = round(clicks / duration, 2)
    now = int(time.time())
    replay_events = data.get('replay', [])
    if not isinstance(replay_events, list) or len(replay_events) > 2500:
        return jsonify(error='Replay invalide.'), 400
    clean_events = []
    for event in replay_events:
        if not isinstance(event, dict) or event.get('type') not in ('click', 'key'):
            return jsonify(error='Événement de replay invalide.'), 400
        try: event_time = max(0, min(duration * 1000, int(event.get('t', 0))))
        except (TypeError, ValueError): return jsonify(error='Horodatage de replay invalide.'), 400
        item = {'t': event_time, 'type': event['type']}
        if event['type'] == 'key':
            key = str(event.get('key', ''))[:16]
            if not key: continue
            item['key'] = key
        clean_events.append(item)
    with db() as con:
        # Un test normal dure au minimum la durée choisie. Cette vérification
        # bloque les envois répétés/fabriqués trop rapprochés.
        previous = con.execute('SELECT created_at FROM scores WHERE user_id=? ORDER BY created_at DESC LIMIT 1',
            (user['id'],)).fetchone()
        if previous and now - int(previous['created_at']) < max(3, duration - 1):
            strikes = add_anti_cheat_strike(con, user['id'], 'parties envoyées trop rapidement')
            if strikes >= 3:
                session.clear()
                return jsonify(error='Compte banni automatiquement après plusieurs tentatives de triche.'), 403
            return jsonify(error='Score refusé : délai anormal entre deux parties ('+str(strikes)+'/3 avertissements).'), 429
        # Les scores suspects sont conservés pour l'historique de modération.
        # Le bouton Auto filtre applique ensuite les sanctions et avertissements.
        intervals = data.get('intervals', [])
        if not isinstance(intervals, list) or len(intervals) > 2000:
            return jsonify(error='Données de cadence invalides.'), 400
        clean_intervals = []
        for value in intervals:
            try:
                number = float(value)
                if not (0 < number <= 5000): raise ValueError()
                clean_intervals.append(round(number, 2))
            except (TypeError, ValueError):
                return jsonify(error='Données de cadence invalides.'), 400
        pattern = ','.join(str(x) for x in clean_intervals)
        con.execute('INSERT INTO scores(user_id,clicks,duration,cps,created_at,click_pattern,replay_data) VALUES(?,?,?,?,?,?,?)',
            (user['id'], clicks, duration, cps, now, pattern, __import__('json').dumps(clean_events, separators=(',', ':'))))
    return jsonify(ok=True, cps=cps)

@app.get('/api/leaderboard')
def leaderboard():
    try: duration = int(request.args.get('duration', '10'))
    except ValueError: duration = 10
    if duration not in (5, 10, 15, 30, 60): duration = 10
    with db() as con:
        rows = con.execute('''SELECT u.username, MAX(s.cps) AS cps, MAX(s.clicks) AS clicks
            FROM scores s JOIN users u ON u.id=s.user_id
            WHERE s.duration=? GROUP BY u.id, u.username ORDER BY cps DESC LIMIT 20''', (duration,)).fetchall()
    return jsonify([dict(r) for r in rows])


def social_user_row(row):
    return {'id': row['id'], 'username': row['username'], 'avatar_data': row.get('avatar_data') if hasattr(row, 'get') else row['avatar_data']}

@app.get('/api/unread-counts')
def unread_counts():
    user=current_user()
    if not user:return jsonify(messages=0,clubs=0,dm={},groups={},club_items={})
    uid=user['id']
    with db() as con:
        dm_rows=con.execute("""SELECT m.sender_id AS id,COUNT(*) AS n FROM direct_messages m
            WHERE m.recipient_id=? AND m.sender_id<>? AND m.created_at>COALESCE(
                (SELECT r.last_read_at FROM message_read_state r
                 WHERE r.user_id=? AND r.scope='dm' AND r.conversation_id=m.sender_id),0)
            GROUP BY m.sender_id""",(uid,uid,uid)).fetchall()
        group_rows=con.execute("""SELECT m.conversation_id AS id,COUNT(*) AS n FROM group_messages m
            JOIN group_members gm ON gm.conversation_id=m.conversation_id AND gm.user_id=?
            WHERE m.sender_id<>? AND m.created_at>COALESCE(
                (SELECT r.last_read_at FROM message_read_state r
                 WHERE r.user_id=? AND r.scope='group' AND r.conversation_id=m.conversation_id),0)
            GROUP BY m.conversation_id""",(uid,uid,uid)).fetchall()
        club_rows=con.execute("""SELECT m.club_id AS id,COUNT(*) AS n FROM club_messages m
            JOIN club_members cm ON cm.club_id=m.club_id AND cm.user_id=?
            WHERE m.user_id<>? AND m.created_at>COALESCE(
                (SELECT r.last_read_at FROM message_read_state r
                 WHERE r.user_id=? AND r.scope='club' AND r.conversation_id=m.club_id),0)
            GROUP BY m.club_id""",(uid,uid,uid)).fetchall()
    dm_counts={str(r['id']):int(r['n']) for r in dm_rows}
    group_counts={str(r['id']):int(r['n']) for r in group_rows}
    club_counts={str(r['id']):int(r['n']) for r in club_rows}
    return jsonify(messages=sum(dm_counts.values())+sum(group_counts.values()),
        clubs=sum(club_counts.values()),dm=dm_counts,groups=group_counts,club_items=club_counts)


@app.get('/api/social')
def social_state():
    user = current_user()
    if not user: return jsonify(error='Connecte-toi pour accéder aux fonctions sociales.'), 401
    uid = user['id']
    with db() as con:
        friends = con.execute("""SELECT u.id,u.username,u.avatar_data FROM friendships f
            JOIN users u ON u.id=f.friend_id WHERE f.user_id=? ORDER BY lower(u.username)""", (uid,)).fetchall()
        incoming = con.execute("""SELECT r.id,u.username,u.id AS user_id,u.avatar_data FROM friend_requests r
            JOIN users u ON u.id=r.sender_id WHERE r.recipient_id=? AND r.status='pending'
            ORDER BY r.created_at DESC""", (uid,)).fetchall()
        outgoing = con.execute("""SELECT u.username FROM friend_requests r
            JOIN users u ON u.id=r.recipient_id WHERE r.sender_id=? AND r.status='pending'
            ORDER BY r.created_at DESC""", (uid,)).fetchall()
        clubs = con.execute("""SELECT c.id,c.name,c.owner_id,c.is_public,
            (SELECT COUNT(*) FROM club_members m WHERE m.club_id=c.id) AS members
            FROM clubs c JOIN club_members mine ON mine.club_id=c.id
            WHERE mine.user_id=? ORDER BY lower(c.name)""", (uid,)).fetchall()
        discover = con.execute("""SELECT c.id,c.name,c.owner_id,c.is_public,
            (SELECT COUNT(*) FROM club_members m WHERE m.club_id=c.id) AS members
            FROM clubs c WHERE c.is_public=TRUE AND NOT EXISTS
            (SELECT 1 FROM club_members mine WHERE mine.club_id=c.id AND mine.user_id=?)
            ORDER BY c.created_at DESC LIMIT 30""", (uid,)).fetchall()
    return jsonify(friends=[dict(r) for r in friends], incoming=[dict(r) for r in incoming],
        outgoing=[dict(r) for r in outgoing], clubs=[dict(r) for r in clubs],
        discover=[dict(r) for r in discover])

@app.post('/api/friends/request')
def friend_request():
    bad=require_csrf()
    if bad:return bad
    user=current_user()
    if not user:return jsonify(error='Connecte-toi.'),401
    data=request.get_json(silent=True) or {}
    name=str(data.get('username','')).strip()
    if not re.fullmatch(r'[A-Za-z0-9_]{3,18}',name):return jsonify(error='Entre un pseudo valide.'),400
    with db() as con:
        target=con.execute('SELECT id,username FROM users WHERE lower(username)=lower(?)',(name,)).fetchone()
        if not target:return jsonify(error='Aucun joueur trouvé avec ce pseudo.'),404
        if target['id']==user['id']:return jsonify(error='Tu ne peux pas t’ajouter toi-même.'),400
        exists=con.execute('SELECT 1 FROM friendships WHERE user_id=? AND friend_id=?',(user['id'],target['id'])).fetchone()
        if exists:return jsonify(error='Ce joueur est déjà ton ami.'),409
        reverse=con.execute("SELECT id FROM friend_requests WHERE sender_id=? AND recipient_id=? AND status='pending'",(target['id'],user['id'])).fetchone()
        if reverse:return jsonify(error='Ce joueur t’a déjà envoyé une demande. Accepte-la dans les demandes reçues.'),409
        pending=con.execute("SELECT 1 FROM friend_requests WHERE sender_id=? AND recipient_id=? AND status='pending'",(user['id'],target['id'])).fetchone()
        if pending:return jsonify(error='Demande déjà envoyée.'),409
        con.execute("INSERT INTO friend_requests(sender_id,recipient_id,status,created_at) VALUES(?,?,'pending',?)",(user['id'],target['id'],int(time.time())))
    return jsonify(ok=True)

@app.post('/api/friends/respond')
def friend_respond():
    bad=require_csrf()
    if bad:return bad
    user=current_user()
    if not user:return jsonify(error='Connecte-toi.'),401
    data=request.get_json(silent=True) or {}
    try:rid=int(data.get('request_id'))
    except (TypeError,ValueError):return jsonify(error='Demande invalide.'),400
    action=data.get('action')
    if action not in ('accept','reject'):return jsonify(error='Action invalide.'),400
    with db() as con:
        req=con.execute("SELECT sender_id,recipient_id FROM friend_requests WHERE id=? AND recipient_id=? AND status='pending'",(rid,user['id'])).fetchone()
        if not req:return jsonify(error='Cette demande n’est plus disponible.'),404
        status='accepted' if action=='accept' else 'rejected'
        con.execute('UPDATE friend_requests SET status=? WHERE id=?',(status,rid))
        if action=='accept':
            now=int(time.time())
            con.execute('INSERT INTO friendships(user_id,friend_id,created_at) VALUES(?,?,?) ON CONFLICT DO NOTHING',(user['id'],req['sender_id'],now))
            con.execute('INSERT INTO friendships(user_id,friend_id,created_at) VALUES(?,?,?) ON CONFLICT DO NOTHING',(req['sender_id'],user['id'],now))
    return jsonify(ok=True)

@app.post('/api/friends/remove')
def friend_remove():
    bad=require_csrf()
    if bad:return bad
    user=current_user()
    if not user:return jsonify(error='Connecte-toi.'),401
    data=request.get_json(silent=True) or {}
    try:fid=int(data.get('friend_id'))
    except (TypeError,ValueError):return jsonify(error='Ami invalide.'),400
    with db() as con:
        con.execute('DELETE FROM friendships WHERE (user_id=? AND friend_id=?) OR (user_id=? AND friend_id=?)',(user['id'],fid,fid,user['id']))
    return jsonify(ok=True)

def are_friends(con,uid,other):
    return bool(con.execute('SELECT 1 FROM friendships WHERE user_id=? AND friend_id=?',(uid,other)).fetchone())

@app.get('/api/messages')
def get_messages():
    user=current_user()
    if not user:return jsonify(error='Connecte-toi.'),401
    try:other=int(request.args.get('friend_id',''))
    except ValueError:return jsonify(error='Ami invalide.'),400
    with db() as con:
        if not are_friends(con,user['id'],other):return jsonify(error='Tu dois être ami avec ce joueur.'),403
        rows=con.execute("""SELECT m.id,m.sender_id,m.recipient_id,m.body,m.created_at,u.username,u.avatar_data
            FROM direct_messages m JOIN users u ON u.id=m.sender_id
            WHERE (m.sender_id=? AND m.recipient_id=?) OR (m.sender_id=? AND m.recipient_id=?)
            ORDER BY m.created_at DESC,m.id DESC LIMIT 100""",(user['id'],other,other,user['id'])).fetchall()
        con.execute("""INSERT INTO message_read_state(user_id,scope,conversation_id,last_read_at) VALUES(?,?,?,?)
            ON CONFLICT(user_id,scope,conversation_id) DO UPDATE SET last_read_at=excluded.last_read_at""",
            (user['id'],'dm',other,int(time.time())))
    return jsonify(messages=[dict(r) for r in reversed(rows)])

@app.post('/api/messages')
def send_message():
    bad=require_csrf()
    if bad:return bad
    user=current_user()
    if not user:return jsonify(error='Connecte-toi.'),401
    data=request.get_json(silent=True) or {}
    try:other=int(data.get('friend_id'))
    except (TypeError,ValueError):return jsonify(error='Ami invalide.'),400
    body=str(data.get('body','')).strip()
    if not body or len(body)>1000:return jsonify(error='Le message doit contenir entre 1 et 1000 caractères.'),400
    with db() as con:
        if not are_friends(con,user['id'],other):return jsonify(error='Tu dois être ami avec ce joueur.'),403
        con.execute('INSERT INTO direct_messages(sender_id,recipient_id,body,created_at) VALUES(?,?,?,?)',(user['id'],other,body,int(time.time())))
    return jsonify(ok=True)

@app.get('/api/groups')
def list_message_groups():
    user=current_user()
    if not user:return jsonify(error='Connecte-toi.'),401
    with db() as con:
        rows=con.execute("""SELECT c.id,c.name,c.owner_id,c.created_at,
            (SELECT COUNT(*) FROM group_members gm WHERE gm.conversation_id=c.id) AS members
            FROM group_conversations c JOIN group_members mine ON mine.conversation_id=c.id
            WHERE mine.user_id=? ORDER BY c.created_at DESC""",(user['id'],)).fetchall()
    return jsonify(groups=[dict(r) for r in rows])


@app.post('/api/groups')
def create_message_group():
    bad=require_csrf()
    if bad:return bad
    user=current_user()
    if not user:return jsonify(error='Connecte-toi.'),401
    data=request.get_json(silent=True) or {}
    name=str(data.get('name','')).strip()
    raw=data.get('member_ids',[])
    if not isinstance(raw,list):return jsonify(error='Liste de membres invalide.'),400
    try: member_ids=list(dict.fromkeys(int(x) for x in raw if int(x)!=user['id']))
    except (TypeError,ValueError):return jsonify(error='Membres invalides.'),400
    if not re.fullmatch(r'.{2,32}',name):return jsonify(error='Le nom du groupe doit contenir 2 à 32 caractères.'),400
    if not member_ids or len(member_ids)>30:return jsonify(error='Choisis entre 1 et 30 amis.'),400
    with db() as con:
        for other in member_ids:
            if not are_friends(con,user['id'],other):
                return jsonify(error='Tous les membres doivent être dans ta liste d’amis.'),403
        if con.postgres:
            row=con.execute('INSERT INTO group_conversations(name,owner_id,created_at) VALUES(?,?,?) RETURNING id',
                (name,user['id'],int(time.time()))).fetchone()
            gid=row['id']
        else:
            cur=con.execute('INSERT INTO group_conversations(name,owner_id,created_at) VALUES(?,?,?)',
                (name,user['id'],int(time.time())))
            gid=cur.lastrowid
        now=int(time.time())
        con.execute('INSERT INTO group_members(conversation_id,user_id,joined_at) VALUES(?,?,?)',(gid,user['id'],now))
        for other in member_ids:
            con.execute('INSERT INTO group_members(conversation_id,user_id,joined_at) VALUES(?,?,?)',(gid,other,now))
    return jsonify(ok=True,group_id=gid)


@app.get('/api/groups/<int:group_id>/messages')
def get_message_group(group_id):
    user=current_user()
    if not user:return jsonify(error='Connecte-toi.'),401
    with db() as con:
        member=con.execute('SELECT 1 FROM group_members WHERE conversation_id=? AND user_id=?',(group_id,user['id'])).fetchone()
        if not member:return jsonify(error='Tu ne fais pas partie de ce groupe.'),403
        rows=con.execute("""SELECT m.id,m.sender_id,m.body,m.created_at,u.username,u.avatar_data
            FROM group_messages m JOIN users u ON u.id=m.sender_id WHERE m.conversation_id=?
            ORDER BY m.created_at DESC,m.id DESC LIMIT 100""",(group_id,)).fetchall()
        con.execute("""INSERT INTO message_read_state(user_id,scope,conversation_id,last_read_at) VALUES(?,?,?,?)
            ON CONFLICT(user_id,scope,conversation_id) DO UPDATE SET last_read_at=excluded.last_read_at""",
            (user['id'],'group',group_id,int(time.time())))
    return jsonify(messages=[dict(r) for r in reversed(rows)])


@app.post('/api/groups/<int:group_id>/messages')
def send_message_group(group_id):
    bad=require_csrf()
    if bad:return bad
    user=current_user()
    if not user:return jsonify(error='Connecte-toi.'),401
    data=request.get_json(silent=True) or {}
    body=str(data.get('body','')).strip()
    if not body or len(body)>1000:return jsonify(error='Le message doit contenir entre 1 et 1000 caractères.'),400
    with db() as con:
        member=con.execute('SELECT 1 FROM group_members WHERE conversation_id=? AND user_id=?',(group_id,user['id'])).fetchone()
        if not member:return jsonify(error='Tu ne fais pas partie de ce groupe.'),403
        con.execute('INSERT INTO group_messages(conversation_id,sender_id,body,created_at) VALUES(?,?,?,?)',
            (group_id,user['id'],body,int(time.time())))
    return jsonify(ok=True)


@app.post('/api/clubs')
def create_club():
    bad=require_csrf()
    if bad:return bad
    user=current_user()
    if not user:return jsonify(error='Connecte-toi.'),401
    data=request.get_json(silent=True) or {}
    name=str(data.get('name','')).strip()
    is_public=bool(data.get('is_public',True))
    if not re.fullmatch(r'[A-Za-z0-9 _-]{3,24}',name):return jsonify(error='Le nom doit faire 3 à 24 caractères (lettres, chiffres, espaces, _ ou -).'),400
    try:
        with db() as con:
            if con.postgres:
                row=con.execute('INSERT INTO clubs(name,owner_id,created_at,is_public) VALUES(?,?,?,?) RETURNING id',(name,user['id'],int(time.time()),is_public)).fetchone()
                cid=row['id']
            else:
                cur=con.execute('INSERT INTO clubs(name,owner_id,created_at,is_public) VALUES(?,?,?,?)',(name,user['id'],int(time.time()),1 if is_public else 0))
                cid=cur.lastrowid
            con.execute('INSERT INTO club_members(club_id,user_id,joined_at) VALUES(?,?,?)',(cid,user['id'],int(time.time())))
    except Exception as exc:
        if 'unique' in str(exc).lower() or 'duplicate' in str(exc).lower():return jsonify(error='Ce nom de club est déjà pris.'),409
        raise
    return jsonify(ok=True,club_id=cid)

@app.post('/api/clubs/join')
def join_club():
    bad=require_csrf()
    if bad:return bad
    user=current_user()
    if not user:return jsonify(error='Connecte-toi.'),401
    data=request.get_json(silent=True) or {}
    try:cid=int(data.get('club_id'))
    except (TypeError,ValueError):return jsonify(error='Club invalide.'),400
    with db() as con:
        exists=con.execute('SELECT id,is_public FROM clubs WHERE id=?',(cid,)).fetchone()
        if not exists:return jsonify(error='Club introuvable.'),404
        if not exists['is_public']:return jsonify(error='Ce club est privé : demande une invitation à son créateur.'),403
        con.execute('INSERT INTO club_members(club_id,user_id,joined_at) VALUES(?,?,?) ON CONFLICT DO NOTHING',(cid,user['id'],int(time.time())))
    return jsonify(ok=True)

@app.get('/api/clubs/<int:club_id>/settings')
def get_club_settings(club_id):
    user=current_user()
    if not user:return jsonify(error='Connecte-toi.'),401
    with db() as con:
        club=con.execute('SELECT id,name,owner_id,is_public FROM clubs WHERE id=?',(club_id,)).fetchone()
        if not club:return jsonify(error='Ligue introuvable.'),404
        if club['owner_id']!=user['id']:return jsonify(error='Seul le créateur peut consulter ces paramètres.'),403
        members=con.execute("""SELECT u.id,u.username,u.role,u.avatar_data
            FROM club_members m JOIN users u ON u.id=m.user_id
            WHERE m.club_id=? ORDER BY CASE WHEN u.id=? THEN 0 ELSE 1 END,lower(u.username)""",
            (club_id,user['id'])).fetchall()
    return jsonify(club=dict(club),members=[dict(m) for m in members])

@app.post('/api/clubs/<int:club_id>/settings')
def update_club_settings(club_id):
    bad=require_csrf()
    if bad:return bad
    user=current_user()
    if not user:return jsonify(error='Connecte-toi.'),401
    data=request.get_json(silent=True) or {}
    if 'is_public' not in data:return jsonify(error='Statut manquant.'),400
    is_public=bool(data.get('is_public'))
    with db() as con:
        club=con.execute('SELECT owner_id FROM clubs WHERE id=?',(club_id,)).fetchone()
        if not club:return jsonify(error='Ligue introuvable.'),404
        if club['owner_id']!=user['id']:return jsonify(error='Seul le créateur peut modifier cette ligue.'),403
        con.execute('UPDATE clubs SET is_public=? WHERE id=?',
            (is_public if con.postgres else (1 if is_public else 0),club_id))
    return jsonify(ok=True,is_public=is_public)

@app.post('/api/clubs/<int:club_id>/members/<int:member_id>/remove')
def remove_club_member(club_id,member_id):
    bad=require_csrf()
    if bad:return bad
    user=current_user()
    if not user:return jsonify(error='Connecte-toi.'),401
    with db() as con:
        club=con.execute('SELECT owner_id FROM clubs WHERE id=?',(club_id,)).fetchone()
        if not club:return jsonify(error='Ligue introuvable.'),404
        if club['owner_id']!=user['id']:return jsonify(error='Seul le créateur peut gérer les membres.'),403
        if member_id==user['id']:return jsonify(error='Tu ne peux pas te retirer toi-même des membres ici.'),400
        cur=con.execute('DELETE FROM club_members WHERE club_id=? AND user_id=?',(club_id,member_id))
        if cur.rowcount==0:return jsonify(error='Ce joueur ne fait pas partie de la ligue.'),404
    return jsonify(ok=True)

@app.post('/api/clubs/<int:club_id>/delete')
def delete_club(club_id):
    bad=require_csrf()
    if bad:return bad
    user=current_user()
    if not user:return jsonify(error='Connecte-toi.'),401
    with db() as con:
        club=con.execute('SELECT owner_id FROM clubs WHERE id=?',(club_id,)).fetchone()
        if not club:return jsonify(error='Club introuvable.'),404
        if club['owner_id']!=user['id']:return jsonify(error='Seul le créateur du club peut le supprimer.'),403
        con.execute('DELETE FROM clubs WHERE id=?',(club_id,))
    return jsonify(ok=True)

@app.post('/api/clubs/<int:club_id>/invite')
def invite_to_club(club_id):
    bad=require_csrf()
    if bad:return bad
    user=current_user()
    if not user:return jsonify(error='Connecte-toi.'),401
    data=request.get_json(silent=True) or {}
    name=str(data.get('username','')).strip()
    if not re.fullmatch(r'[A-Za-z0-9_]{3,18}',name):return jsonify(error='Pseudo invalide.'),400
    with db() as con:
        club=con.execute('SELECT owner_id FROM clubs WHERE id=?',(club_id,)).fetchone()
        if not club:return jsonify(error='Club introuvable.'),404
        if club['owner_id']!=user['id']:return jsonify(error='Seul le créateur peut inviter des joueurs.'),403
        target=con.execute('SELECT id FROM users WHERE lower(username)=lower(?)',(name,)).fetchone()
        if not target:return jsonify(error='Joueur introuvable.'),404
        con.execute('INSERT INTO club_members(club_id,user_id,joined_at) VALUES(?,?,?) ON CONFLICT DO NOTHING',(club_id,target['id'],int(time.time())))
    return jsonify(ok=True)

@app.post('/api/clubs/leave')
def leave_club():
    bad=require_csrf()
    if bad:return bad
    user=current_user()
    if not user:return jsonify(error='Connecte-toi.'),401
    data=request.get_json(silent=True) or {}
    try:cid=int(data.get('club_id'))
    except (TypeError,ValueError):return jsonify(error='Club invalide.'),400
    with db() as con:
        club=con.execute('SELECT owner_id FROM clubs WHERE id=?',(cid,)).fetchone()
        if not club:return jsonify(error='Club introuvable.'),404
        if club['owner_id']==user['id']:return jsonify(error='Le créateur ne peut pas quitter son club. Supprimer cette fonction nécessiterait une option de transfert.'),400
        con.execute('DELETE FROM club_members WHERE club_id=? AND user_id=?',(cid,user['id']))
    return jsonify(ok=True)

@app.get('/api/clubs/<int:club_id>/messages')
def get_club_messages(club_id):
    user=current_user()
    if not user:return jsonify(error='Connecte-toi.'),401
    with db() as con:
        member=con.execute('SELECT 1 FROM club_members WHERE club_id=? AND user_id=?',(club_id,user['id'])).fetchone()
        if not member:return jsonify(error='Tu dois rejoindre ce club.'),403
        rows=con.execute("""SELECT m.id,m.body,m.created_at,m.user_id,u.username,u.avatar_data
            FROM club_messages m JOIN users u ON u.id=m.user_id WHERE m.club_id=?
            ORDER BY m.created_at DESC,m.id DESC LIMIT 100""",(club_id,)).fetchall()
        con.execute("""INSERT INTO message_read_state(user_id,scope,conversation_id,last_read_at) VALUES(?,?,?,?)
            ON CONFLICT(user_id,scope,conversation_id) DO UPDATE SET last_read_at=excluded.last_read_at""",
            (user['id'],'club',club_id,int(time.time())))
    return jsonify(messages=[dict(r) for r in reversed(rows)])

@app.post('/api/clubs/<int:club_id>/messages')
def send_club_message(club_id):
    bad=require_csrf()
    if bad:return bad
    user=current_user()
    if not user:return jsonify(error='Connecte-toi.'),401
    data=request.get_json(silent=True) or {}
    body=str(data.get('body','')).strip()
    if not body or len(body)>1000:return jsonify(error='Le message doit contenir entre 1 et 1000 caractères.'),400
    with db() as con:
        member=con.execute('SELECT 1 FROM club_members WHERE club_id=? AND user_id=?',(club_id,user['id'])).fetchone()
        if not member:return jsonify(error='Tu dois rejoindre ce club.'),403
        con.execute('INSERT INTO club_messages(club_id,user_id,body,created_at) VALUES(?,?,?,?)',(club_id,user['id'],body,int(time.time())))
    return jsonify(ok=True)


@app.post('/api/admin/auto-filter')
def admin_auto_filter():
    bad = require_csrf()
    if bad: return bad
    actor, error = staff_required()
    if error: return error
    if actor['role'] not in ('admin', 'creator'):
        return jsonify(error='Seuls les admins peuvent lancer le filtre automatique.'), 403
    processed = {'cheat': 0, 'suspect': 0, 'clear': 0}
    with db() as con:
        rows = con.execute("""SELECT s.id,s.user_id,s.cps,s.click_pattern,u.role,u.username
            FROM scores s JOIN users u ON u.id=s.user_id
            WHERE s.auto_processed=? ORDER BY s.created_at DESC LIMIT 200""", (False if con.postgres else 0,)).fetchall()
        for row in rows:
            try:
                gaps = [float(x) for x in (row['click_pattern'] or '').split(',') if x]
            except ValueError:
                gaps = []
            mean = sum(gaps) / len(gaps) if gaps else 0
            cv = 0
            if len(gaps) >= 10 and mean > 0:
                variance = sum((x-mean)**2 for x in gaps) / len(gaps)
                cv = (variance ** 0.5) / mean
            cheat = float(row['cps']) > 20 or (len(gaps) >= 14 and 35 <= mean <= 180 and cv < 0.018)
            suspect = not cheat and (float(row['cps']) >= 15 or (len(gaps) >= 10 and 35 <= mean <= 180 and cv < 0.06))
            if cheat and row['role'] not in ('creator', 'admin'):
                status, reason = 'cheat', 'Cadence de clics incompatible avec un jeu manuel.'
                con.execute('UPDATE users SET is_banned=?,ban_reason=? WHERE id=?',
                    (True if con.postgres else 1, reason, row['user_id']))
                processed['cheat'] += 1
            elif suspect:
                status, reason = 'suspect', 'Cadence inhabituelle : vérification recommandée.'
                con.execute('INSERT INTO player_notifications(user_id,body,created_at) VALUES(?,?,?)',
                    (row['user_id'], 'Avertissement ClickRush : une de tes parties a été signalée comme suspecte. Joue uniquement avec des clics manuels.', int(time.time())))
                processed['suspect'] += 1
            else:
                status, reason = 'clear', ''
                processed['clear'] += 1
            con.execute('UPDATE scores SET auto_status=?,auto_reason=?,auto_processed=? WHERE id=?',
                (status, reason, True if con.postgres else 1, row['id']))
            if status == 'cheat':
                # La partie détectée comme triche est retirée de l'historique.
                con.execute('DELETE FROM scores WHERE id=?', (row['id'],))
    return jsonify(ok=True, processed=processed)

@app.get('/api/admin')
def admin_overview():
    actor, error = staff_required()
    if error: return error
    with db() as con:
        users = con.execute("""SELECT id,username,avatar_data,role,is_banned,ban_reason,created_at
            FROM users ORDER BY lower(username) LIMIT 300""").fetchall()
        scores = con.execute("""SELECT s.id,s.user_id,u.username,s.clicks,s.duration,s.cps,s.created_at,u.avatar_data,s.auto_status,s.auto_reason,s.auto_processed
            FROM scores s JOIN users u ON u.id=s.user_id ORDER BY s.created_at DESC LIMIT 100""").fetchall()
        if actor['role'] in ('admin','creator'):
            pending = con.execute("""SELECT r.id,r.action,r.target_user_id,r.score_id,r.reason,r.created_at,
                req.username AS requester,req.avatar_data AS requester_avatar,target.username AS target,target.avatar_data AS target_avatar
                FROM admin_requests r JOIN users req ON req.id=r.requester_id
                LEFT JOIN users target ON target.id=r.target_user_id
                WHERE r.status='pending' ORDER BY r.created_at ASC LIMIT 100""").fetchall()
        else:
            pending = con.execute("""SELECT r.id,r.action,r.target_user_id,r.score_id,r.reason,r.created_at,
                req.username AS requester,req.avatar_data AS requester_avatar,target.username AS target,target.avatar_data AS target_avatar
                FROM admin_requests r JOIN users req ON req.id=r.requester_id
                LEFT JOIN users target ON target.id=r.target_user_id
                WHERE r.status='pending' AND r.requester_id=? ORDER BY r.created_at ASC LIMIT 100""",(actor['id'],)).fetchall()
    return jsonify(role=actor['role'],users=[dict(r) for r in users],
        scores=[dict(r) for r in scores],requests=[dict(r) for r in pending])

@app.get('/api/admin/replay/<int:score_id>')
def admin_replay(score_id):
    actor,error=staff_required()
    if error:return error
    with db() as con:
        row=con.execute('SELECT s.id,s.user_id,s.clicks,s.duration,s.cps,s.created_at,s.replay_data,u.username FROM scores s JOIN users u ON u.id=s.user_id WHERE s.id=?',(score_id,)).fetchone()
        if not row:return jsonify(error='Partie introuvable.'),404
    try: events=__import__('json').loads(row['replay_data'] or '[]')
    except (ValueError,TypeError): events=[]
    return jsonify(score={k:row[k] for k in ('id','user_id','clicks','duration','cps','created_at','username')},events=events,available=bool(events))

@app.get('/api/admin/history/<int:target_id>')
def admin_history(target_id):
    actor,error=staff_required()
    if error:return error
    with db() as con:
        target=con.execute('SELECT id,username,avatar_data,role,is_banned,created_at FROM users WHERE id=?',(target_id,)).fetchone()
        if not target:return jsonify(error='Joueur introuvable.'),404
        rows=con.execute('SELECT id,clicks,duration,cps,created_at,auto_status,auto_reason FROM scores WHERE user_id=? ORDER BY created_at DESC LIMIT 200',(target_id,)).fetchall()
    return jsonify(user=dict(target),history=[dict(r) for r in rows])

@app.post('/api/admin/action')
def admin_action():
    bad=require_csrf()
    if bad:return bad
    actor,error=staff_required()
    if error:return error
    data=request.get_json(silent=True) or {}
    action=str(data.get('action',''))
    try:
        target_id=int(data.get('target_id')) if data.get('target_id') is not None else None
        score_id=int(data.get('score_id')) if data.get('score_id') is not None else None
    except (TypeError,ValueError):return jsonify(error='Identifiant invalide.'),400
    reason=str(data.get('reason','')).strip()[:300]
    if action not in ('ban','unban','delete_score'):return jsonify(error='Action invalide.'),400
    if actor['role']=='mod':
        if action in ('ban','unban') and not target_id:return jsonify(error='Choisis un joueur.'),400
        if action=='delete_score' and not score_id:return jsonify(error='Choisis un score.'),400
        with db() as con:
            if target_id:
                target=con.execute('SELECT id,role FROM users WHERE id=?',(target_id,)).fetchone()
                if not target:return jsonify(error='Joueur introuvable.'),404
                if protected_from(actor,target) or target['id']==actor['id']:return jsonify(error='Action interdite sur ce compte.'),403
            if action=='delete_score':
                score=con.execute('SELECT s.id,u.id AS user_id,u.role FROM scores s JOIN users u ON u.id=s.user_id WHERE s.id=?',(score_id,)).fetchone()
                if not score:return jsonify(error='Score introuvable.'),404
                if (score['user_id']==actor['id'] and actor['role']!='creator') or protected_from(actor,score):return jsonify(error='Action interdite sur ce score.'),403
            con.execute("""INSERT INTO admin_requests(requester_id,action,target_user_id,score_id,reason,status,created_at)
                VALUES(?,?,?,?,?,'pending',?)""",(actor['id'],action,target_id,score_id,reason,int(time.time())))
        return jsonify(ok=True,pending_approval=True)
    if actor['role'] not in ('admin','creator'):return jsonify(error='Accès refusé.'),403
    with db() as con:
        message=apply_staff_action(con,actor,action,target_id,score_id,reason)
    if message:return jsonify(error=message),400
    return jsonify(ok=True)

@app.post('/api/admin/role')
def admin_set_role():
    bad=require_csrf()
    if bad:return bad
    actor,error=staff_required()
    if error:return error
    if actor['role']!='creator':return jsonify(error='Seul le créateur peut nommer des administrateurs ou modérateurs.'),403
    data=request.get_json(silent=True) or {}
    try:target_id=int(data.get('target_id'))
    except (TypeError,ValueError):return jsonify(error='Joueur invalide.'),400
    role=str(data.get('role',''))
    if role not in ('user','mod','admin'):return jsonify(error='Rôle invalide.'),400
    with db() as con:
        target=con.execute('SELECT id,role FROM users WHERE id=?',(target_id,)).fetchone()
        if not target:return jsonify(error='Joueur introuvable.'),404
        if target['role']=='creator':return jsonify(error='Le compte créateur ne peut pas être modifié.'),403
        con.execute('UPDATE users SET role=? WHERE id=?',(role,target_id))
    return jsonify(ok=True)

@app.post('/api/admin/requests/<int:request_id>')
def admin_review_request(request_id):
    bad=require_csrf()
    if bad:return bad
    actor,error=staff_required()
    if error:return error
    if actor['role'] not in ('admin','creator'):return jsonify(error='Seuls les admins peuvent valider les demandes.'),403
    data=request.get_json(silent=True) or {}
    decision=data.get('decision')
    if decision not in ('approve','reject'):return jsonify(error='Décision invalide.'),400
    with db() as con:
        req=con.execute("""SELECT id,requester_id,action,target_user_id,score_id,reason,status
            FROM admin_requests WHERE id=?""",(request_id,)).fetchone()
        if not req or req['status']!='pending':return jsonify(error='Demande déjà traitée ou introuvable.'),404
        if decision=='approve':
            message=apply_staff_action(con,actor,req['action'],req['target_user_id'],req['score_id'],req['reason'])
            if message:return jsonify(error=message),400
        con.execute('UPDATE admin_requests SET status=?,reviewer_id=?,reviewed_at=? WHERE id=?',
            ('approved' if decision=='approve' else 'rejected',actor['id'],int(time.time()),request_id))
    return jsonify(ok=True)

init_db()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', '8766'))
    app.run(host='0.0.0.0', port=port, debug=False)
