from flask import Flask, request, jsonify, session, send_from_directory
from werkzeug.security import generate_password_hash, check_password_hash
from contextlib import contextmanager
from pathlib import Path
import os, re, secrets, time, sqlite3

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
                created_at BIGINT NOT NULL)''')
        else:
            con.execute('''CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL COLLATE NOCASE UNIQUE,
                password_hash TEXT NOT NULL, created_at INTEGER NOT NULL)''')
            con.execute('''CREATE TABLE IF NOT EXISTS scores (
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                clicks INTEGER NOT NULL, duration INTEGER NOT NULL, cps REAL NOT NULL, created_at INTEGER NOT NULL)''')
        if con.postgres:
            con.execute('ALTER TABLE users ADD COLUMN IF NOT EXISTS avatar_data TEXT')
        else:
            columns = [row['name'] for row in con.execute('PRAGMA table_info(users)').fetchall()]
            if 'avatar_data' not in columns:
                con.execute('ALTER TABLE users ADD COLUMN avatar_data TEXT')
        con.execute('CREATE INDEX IF NOT EXISTS idx_scores_cps ON scores(cps DESC)')
        con.execute('CREATE INDEX IF NOT EXISTS idx_scores_user ON scores(user_id, created_at DESC)')
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
        con.execute('CREATE INDEX IF NOT EXISTS idx_club_messages ON club_messages(club_id,created_at)')
        con.execute('CREATE INDEX IF NOT EXISTS idx_dm_recipient_pair ON direct_messages(recipient_id,sender_id,created_at)')
        con.execute('CREATE INDEX IF NOT EXISTS idx_scores_duration_cps ON scores(duration,cps DESC,user_id)')


def current_user():
    # Keep session lookups lightweight: avatar_data can be a large base64 image.
    uid = session.get('user_id')
    if not uid: return None
    with db() as con:
        row = con.execute('SELECT id, username, created_at FROM users WHERE id=?', (uid,)).fetchone()
    return dict(row) if row else None

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
    if user:
        with db() as con:
            profile = con.execute('SELECT avatar_data FROM users WHERE id=?', (user['id'],)).fetchone()
            best = con.execute('SELECT MAX(cps) AS best, COUNT(*) AS total FROM scores WHERE user_id=?', (user['id'],)).fetchone()
            recent = con.execute('SELECT clicks, duration, cps, created_at FROM scores WHERE user_id=? ORDER BY created_at DESC LIMIT 10', (user['id'],)).fetchall()
        user['avatar_data'] = profile['avatar_data'] if profile else None
        result['stats'] = {'best': round(best['best'] or 0, 2), 'total': best['total']}
        result['history'] = [dict(r) for r in recent]
    return jsonify(result)

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
    try:
        with db() as con:
            if con.postgres:
                row = con.execute('INSERT INTO users(username,password_hash,created_at) VALUES(?,?,?) RETURNING id', (username, generate_password_hash(password), int(time.time()))).fetchone()
                uid = row['id']
            else:
                cur = con.execute('INSERT INTO users(username,password_hash,created_at) VALUES(?,?,?)', (username, generate_password_hash(password), int(time.time())))
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
    session.clear(); session['user_id'] = user['id']; session['csrf_token'] = secrets.token_urlsafe(32)
    return jsonify(ok=True)

@app.post('/api/logout')
def logout():
    bad = require_csrf()
    if bad: return bad
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

@app.post('/api/score')
def save_score():
    bad = require_csrf()
    if bad: return bad
    user = current_user()
    if not user: return jsonify(error='Connecte-toi pour sauvegarder ton score.'), 401
    data = request.get_json(silent=True) or {}
    try:
        clicks, duration = int(data.get('clicks')), int(data.get('duration'))
    except (TypeError, ValueError): return jsonify(error='Score invalide.'), 400
    if duration not in (5, 10, 15, 30, 60) or clicks < 0 or clicks > duration * 30:
        return jsonify(error='Score hors limites.'), 400
    cps = round(clicks / duration, 2)
    with db() as con:
        con.execute('INSERT INTO scores(user_id,clicks,duration,cps,created_at) VALUES(?,?,?,?,?)', (user['id'], clicks, duration, cps, int(time.time())))
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

@app.get('/api/social')
def social_state():
    user = current_user()
    if not user: return jsonify(error='Connecte-toi pour accéder aux fonctions sociales.'), 401
    uid = user['id']
    with db() as con:
        friends = con.execute("""SELECT u.id,u.username,u.avatar_data FROM friendships f
            JOIN users u ON u.id=f.friend_id WHERE f.user_id=? ORDER BY lower(u.username)""", (uid,)).fetchall()
        incoming = con.execute("""SELECT r.id,u.username,u.id AS user_id FROM friend_requests r
            JOIN users u ON u.id=r.sender_id WHERE r.recipient_id=? AND r.status='pending'
            ORDER BY r.created_at DESC""", (uid,)).fetchall()
        outgoing = con.execute("""SELECT u.username FROM friend_requests r
            JOIN users u ON u.id=r.recipient_id WHERE r.sender_id=? AND r.status='pending'
            ORDER BY r.created_at DESC""", (uid,)).fetchall()
        clubs = con.execute("""SELECT c.id,c.name,c.owner_id,
            (SELECT COUNT(*) FROM club_members m WHERE m.club_id=c.id) AS members
            FROM clubs c JOIN club_members mine ON mine.club_id=c.id
            WHERE mine.user_id=? ORDER BY lower(c.name)""", (uid,)).fetchall()
        discover = con.execute("""SELECT c.id,c.name,c.owner_id,
            (SELECT COUNT(*) FROM club_members m WHERE m.club_id=c.id) AS members
            FROM clubs c WHERE NOT EXISTS
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
        rows=con.execute("""SELECT m.id,m.sender_id,m.recipient_id,m.body,m.created_at,u.username
            FROM direct_messages m JOIN users u ON u.id=m.sender_id
            WHERE (m.sender_id=? AND m.recipient_id=?) OR (m.sender_id=? AND m.recipient_id=?)
            ORDER BY m.created_at DESC,m.id DESC LIMIT 100""",(user['id'],other,other,user['id'])).fetchall()
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

@app.post('/api/clubs')
def create_club():
    bad=require_csrf()
    if bad:return bad
    user=current_user()
    if not user:return jsonify(error='Connecte-toi.'),401
    data=request.get_json(silent=True) or {}
    name=str(data.get('name','')).strip()
    if not re.fullmatch(r'[A-Za-z0-9 _-]{3,24}',name):return jsonify(error='Le nom doit faire 3 à 24 caractères (lettres, chiffres, espaces, _ ou -).'),400
    try:
        with db() as con:
            if con.postgres:
                row=con.execute('INSERT INTO clubs(name,owner_id,created_at) VALUES(?,?,?) RETURNING id',(name,user['id'],int(time.time()))).fetchone()
                cid=row['id']
            else:
                cur=con.execute('INSERT INTO clubs(name,owner_id,created_at) VALUES(?,?,?)',(name,user['id'],int(time.time())))
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
        exists=con.execute('SELECT id FROM clubs WHERE id=?',(cid,)).fetchone()
        if not exists:return jsonify(error='Club introuvable.'),404
        con.execute('INSERT INTO club_members(club_id,user_id,joined_at) VALUES(?,?,?) ON CONFLICT DO NOTHING',(cid,user['id'],int(time.time())))
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
        rows=con.execute("""SELECT m.id,m.body,m.created_at,m.user_id,u.username
            FROM club_messages m JOIN users u ON u.id=m.user_id WHERE m.club_id=?
            ORDER BY m.created_at DESC,m.id DESC LIMIT 100""",(club_id,)).fetchall()
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

init_db()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', '8766'))
    app.run(host='0.0.0.0', port=port, debug=False)
