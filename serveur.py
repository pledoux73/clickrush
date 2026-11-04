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

@contextmanager
def db():
    if DATABASE_URL:
        import psycopg
        url = DATABASE_URL.replace('postgres://', 'postgresql://', 1)
        with psycopg.connect(url, sslmode='require') as conn:
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

def current_user():
    uid = session.get('user_id')
    if not uid: return None
    with db() as con:
        row = con.execute('SELECT id, username, created_at, avatar_data FROM users WHERE id=?', (uid,)).fetchone()
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
    resp.headers['Cache-Control'] = 'no-store'
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
            best = con.execute('SELECT MAX(cps) AS best, COUNT(*) AS total FROM scores WHERE user_id=?', (user['id'],)).fetchone()
            recent = con.execute('SELECT clicks, duration, cps, created_at FROM scores WHERE user_id=? ORDER BY created_at DESC LIMIT 10', (user['id'],)).fetchall()
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
    avatar_data = data.get('avatar', user.get('avatar_data'))
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

init_db()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', '8766'))
    app.run(host='0.0.0.0', port=port, debug=False)
