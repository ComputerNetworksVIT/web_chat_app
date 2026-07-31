import os
import sqlite3
from collections import defaultdict
from datetime import datetime
from flask import (
    Flask,
    render_template,
    request,
    jsonify,
    session,
    redirect,
    send_from_directory,
    Response,
)
from flask_socketio import SocketIO, emit
from werkzeug.utils import secure_filename
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", os.urandom(24).hex())

socketio = SocketIO(app, cors_allowed_origins="*")

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

ALLOWED_EXT = {
    "pdf",
    "doc",
    "docx",
    "xls",
    "xlsx",
    "ppt",
    "pptx",
    "txt",
    "png",
    "jpg",
    "jpeg",
    "gif",
    "webp",
    "mp4",
    "webm",
    "mov",
    "avi",
    "mkv",
    "zip",
    "rar",
    "7z",
}
MAX_UPLOAD_MB = 25
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024

online_users = set()
user_sids = defaultdict(set)
connections = defaultdict(set)
pending_to = defaultdict(set)
blocked = defaultdict(set)

DB_DIR = os.path.join(BASE_DIR, "instance")
os.makedirs(DB_DIR, exist_ok=True)
DB_PATH = os.path.join(DB_DIR, "chat.db")


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as conn:
        c = conn.cursor()
        c.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender TEXT NOT NULL,
            receiver TEXT NOT NULL,
            kind TEXT NOT NULL,
            msg TEXT,
            file_url TEXT,
            time TEXT NOT NULL,
            delivered INTEGER DEFAULT 0,
            read INTEGER DEFAULT 0
        )
        """)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(messages)")]
        if "file_name" not in cols:
            try:
                conn.execute("ALTER TABLE messages ADD COLUMN file_name TEXT")
            except Exception:
                pass
        c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            username TEXT PRIMARY KEY,
            password TEXT NOT NULL
        )
        """)
        conn.commit()


init_db()


def save_message(sender, receiver, kind, msg_text=None, file_url=None, file_name=None):
    ts = datetime.now().strftime("%I:%M %p")
    with db() as conn:
        cur = conn.cursor()
        cur.execute(
            """
        INSERT INTO messages(sender, receiver, kind, msg, file_url, time, delivered, read, file_name)
        VALUES (?, ?, ?, ?, ?, ?, 0, 0, ?)
        """,
            (sender, receiver, kind, msg_text, file_url, ts, file_name),
        )
        mid = cur.lastrowid
        conn.commit()
    return mid, ts


def _file_size_from_url(file_url: str) -> int:
    try:
        if not file_url:
            return 0
        name = (file_url or "").split("/")[-1]
        p = os.path.join(UPLOAD_DIR, name)
        return os.path.getsize(p) if os.path.exists(p) else 0
    except Exception:
        return 0


def load_history(u1, u2, limit=500):
    with db() as conn:
        cur = conn.cursor()
        cur.execute(
            """
        SELECT id, sender, receiver, kind, msg, file_url, file_name, time, delivered, read
        FROM messages
        WHERE (sender=? AND receiver=?) OR (sender=? AND receiver=?)
        ORDER BY id ASC
        LIMIT ?
        """,
            (u1, u2, u2, u1, limit),
        )
        rows = [dict(row) for row in cur.fetchall()]
        for r in rows:
            if r.get("kind") == "file":
                r["file_size"] = _file_size_from_url(r.get("file_url") or "")
        return rows


def mark_delivered(message_ids):
    if not message_ids:
        return
    with db() as conn:
        cur = conn.cursor()
        cur.execute(
            f"UPDATE messages SET delivered=1 WHERE id IN ({','.join('?' * len(message_ids))})",
            message_ids,
        )
        conn.commit()


def mark_read_up_to(me, peer, up_to_id):
    with db() as conn:
        cur = conn.cursor()
        cur.execute(
            """
        UPDATE messages SET read=1, delivered=1
        WHERE receiver=? AND sender=? AND id<=?
        """,
            (me, peer, up_to_id),
        )
        conn.commit()


@app.errorhandler(RequestEntityTooLarge)
def handle_file_too_large(e):
    return jsonify({"success": False, "error": "File too large"}), 413


def get_peer_state(me: str, peer: str):
    return {
        "connected": peer in connections[me],
        "blockedByMe": peer in blocked[me],
        "blockedMe": me in blocked[peer],
        "pendingSent": me in pending_to[peer],
        "pendingReceived": peer in pending_to[me],
    }


def emit_user_states(me: str):
    peers = [u for u in online_users if u != me]
    states = {u: get_peer_state(me, u) for u in peers}
    for sid in list(user_sids.get(me, [])):
        emit("user_states", states, to=sid)


def gate_send(me, to_user):
    if not me or not to_user:
        return False, "Invalid"
    if to_user in blocked[me]:
        return False, f"You blocked {to_user}. Unblock to send."
    if me in blocked[to_user]:
        return False, f"{to_user} has blocked you. Message not delivered."
    if to_user not in connections[me]:
        return False, f"Not connected with {to_user}. Send a request first."
    return True, ""


@app.route("/")
def home():
    return redirect("/login")


@app.route("/login", methods=["GET"])
def login_page():
    return render_template("login.html")


@app.route("/chat", methods=["GET"])
def chat_page():
    if "username" not in session:
        return redirect("/login")
    return render_template("chat.html", username=session["username"])


@app.route("/register", methods=["POST"])
def register():
    username = (request.form.get("username") or "").strip()
    password = (request.form.get("password") or "").strip()
    if not username or not password:
        return jsonify({"success": False, "error": "All fields required"})
    try:
        with db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT 1 FROM users WHERE username=?", (username,))
            if cur.fetchone():
                return jsonify({"success": False, "error": "Username already exists"})
            hashed_password = generate_password_hash(password)
            cur.execute(
                "INSERT INTO users(username, password) VALUES(?, ?)",
                (username, hashed_password),
            )
            conn.commit()
        session["username"] = username
        return jsonify({"success": True})
    except Exception:
        return jsonify({"success": False, "error": "Registration failed"}), 500


@app.route("/login", methods=["POST"])
def login():
    username = (request.form.get("username") or "").strip()
    password = (request.form.get("password") or "").strip()
    if not username or not password:
        return jsonify({"success": False, "error": "Invalid credentials"})
    with db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT password FROM users WHERE username=?", (username,))
        row = cur.fetchone()
        if not row or not check_password_hash(row["password"], password):
            return jsonify({"success": False, "error": "Invalid credentials"})
    session["username"] = username
    return jsonify({"success": True})


@app.route("/logout", methods=["POST"])
def logout():
    session.pop("username", None)
    return jsonify({"success": True})


@app.route("/upload", methods=["POST"])
def upload():
    try:
        me = session.get("username")
        to_user = request.form.get("to")
        if "file" not in request.files:
            return jsonify({"success": False, "error": "No file"}), 400

        ok, reason = gate_send(me, to_user)
        if not ok:
            return jsonify({"success": False, "error": reason}), 403

        f = request.files["file"]
        if f.filename == "":
            return jsonify({"success": False, "error": "Empty filename"}), 400

        original_name = f.filename
        filename = secure_filename(f.filename)
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if ext not in ALLOWED_EXT:
            return jsonify({"success": False, "error": "File type not allowed"}), 400

        save_path = os.path.join(UPLOAD_DIR, filename)
        base, dot, tail = filename.partition(".")
        i = 1
        while os.path.exists(save_path):
            filename = f"{base}_{i}.{tail}" if dot else f"{base}_{i}"
            save_path = os.path.join(UPLOAD_DIR, filename)
            i += 1

        f.save(save_path)
        file_url = f"/uploads/{filename}"
        size_bytes = os.path.getsize(save_path)

        mid, ts = save_message(
            me,
            to_user,
            "file",
            msg_text=None,
            file_url=file_url,
            file_name=original_name,
        )

        delivered_ids = []
        for sid in list(user_sids.get(to_user, [])):
            socketio.emit(
                "direct_file",
                {
                    "id": mid,
                    "from": me,
                    "url": file_url,
                    "time": ts,
                    "name": original_name,
                    "size": size_bytes,
                },
                to=sid,
            )
            delivered_ids.append(mid)
        if delivered_ids:
            mark_delivered(delivered_ids)

        return jsonify(
            {
                "success": True,
                "id": mid,
                "time": ts,
                "url": file_url,
                "name": original_name,
                "size": size_bytes,
            }
        )
    except RequestEntityTooLarge:
        raise
    except Exception as e:
        return jsonify({"success": False, "error": f"Upload failed: {str(e)}"}), 500


@app.route("/uploads/<path:filename>")
def serve_upload(filename):
    return send_from_directory(UPLOAD_DIR, filename, as_attachment=False)


# HTTP speed test stream (for Network Info tab)
@app.route("/speedtest")
def speedtest():
    size_mb = int(request.args.get("mb", "20"))
    chunk = os.urandom(1024 * 64)
    total = size_mb * 1024 * 1024
    sent = 0

    def gen():
        nonlocal sent
        while sent < total:
            to_send = min(len(chunk), total - sent)
            yield chunk[:to_send]
            sent += to_send

    return Response(gen(), mimetype="application/octet-stream")


@socketio.on("connect")
def on_connect():
    username = session.get("username")
    if not username:
        return
    user_sids[username].add(request.sid)
    online_users.add(username)
    emit("update_users", list(online_users), broadcast=True)
    emit_user_states(username)


@socketio.on("disconnect")
def on_disconnect():
    username = session.get("username")
    if not username:
        return
    sids = user_sids.get(username, set())
    sids.discard(request.sid)
    if not sids:
        user_sids.pop(username, None)
        if username in online_users:
            online_users.remove(username)
            emit("update_users", list(online_users), broadcast=True)
            for other in list(online_users):
                emit_user_states(other)


@socketio.on("get_states")
def get_states():
    me = session.get("username")
    if not me:
        return
    emit_user_states(me)


@socketio.on("select_peer")
def select_peer(data):
    me = session.get("username")
    peer = (data or {}).get("peer")
    if not me or not peer:
        return
    history = load_history(me, peer, limit=500)
    emit("chat_history", {"peer": peer, "history": history})
    emit("peer_state", {"peer": peer, **get_peer_state(me, peer)})


@socketio.on("connect_request")
def handle_connect_request(data):
    sender = session.get("username")
    receiver = (data or {}).get("to")
    if not sender or not receiver or sender == receiver:
        return
    if receiver in blocked[sender] or sender in blocked[receiver]:
        emit(
            "system_message",
            {"msg": f"Cannot request. You or {receiver} has blocked."},
            to=request.sid,
        )
        emit_user_states(sender)
        return
    if receiver in connections[sender]:
        emit(
            "system_message",
            {"msg": f"Already connected with {receiver}."},
            to=request.sid,
        )
        emit_user_states(sender)
        return
    if sender in pending_to[receiver]:
        emit(
            "system_message",
            {"msg": f"Request already sent to {receiver}."},
            to=request.sid,
        )
        emit_user_states(sender)
        return

    pending_to[receiver].add(sender)
    for sid in list(user_sids.get(receiver, [])):
        emit("connection_request", {"from": sender}, to=sid)
    emit(
        "system_message",
        {"msg": f"Connection request sent to {receiver}."},
        to=request.sid,
    )
    emit_user_states(sender)
    emit_user_states(receiver)


@socketio.on("respond_connection")
def handle_connection_response(data):
    receiver = session.get("username")
    sender = (data or {}).get("to")
    accepted = bool((data or {}).get("accepted"))
    if not receiver or not sender:
        return
    if sender not in pending_to[receiver]:
        emit(
            "system_message",
            {"msg": "No pending request from that user."},
            to=request.sid,
        )
        emit_user_states(receiver)
        return

    pending_to[receiver].discard(sender)
    if accepted:
        connections[sender].add(receiver)
        connections[receiver].add(sender)
        msg = f"{sender} and {receiver} are now connected."
        targets = set(user_sids.get(sender, set())) | set(
            user_sids.get(receiver, set())
        )
        for sid in targets:
            emit("system_message", {"msg": msg}, to=sid)
            emit(
                "connection_established",
                {"with": sender if sid in user_sids.get(receiver, set()) else receiver},
                to=sid,
            )
    else:
        for sid in list(user_sids.get(sender, [])):
            emit(
                "system_message",
                {"msg": f"{receiver} declined your connection request."},
                to=sid,
            )

    emit_user_states(receiver)
    emit_user_states(sender)


@socketio.on("disconnect_peer")
def disconnect_peer_evt(data):
    me = session.get("username")
    target = (data or {}).get("user")
    if not me or not target or me == target:
        return
    if target in connections[me]:
        connections[me].discard(target)
        connections[target].discard(me)
        for sid in list(user_sids.get(me, [])):
            emit("system_message", {"msg": f"You disconnected from {target}."}, to=sid)
        for sid in list(user_sids.get(target, [])):
            emit("system_message", {"msg": f"{me} disconnected from you."}, to=sid)
    emit_user_states(me)
    if target in online_users:
        emit_user_states(target)


@socketio.on("block_user")
def block_user_evt(data):
    me = session.get("username")
    target = (data or {}).get("user")
    if not me or not target or me == target:
        return
    blocked[me].add(target)
    if target in connections[me]:
        connections[me].discard(target)
        connections[target].discard(me)
    pending_to[me].discard(target)
    pending_to[target].discard(me)
    emit("system_message", {"msg": f"You blocked {target}."}, to=request.sid)
    for sid in list(user_sids.get(target, [])):
        emit("system_message", {"msg": f"{me} blocked you."}, to=sid)
    emit_user_states(me)
    if target in online_users:
        emit_user_states(target)


@socketio.on("unblock_user")
def unblock_user_evt(data):
    me = session.get("username")
    target = (data or {}).get("user")
    if not me or not target or me == target:
        return
    if target in blocked[me]:
        blocked[me].discard(target)
        emit("system_message", {"msg": f"You unblocked {target}."}, to=request.sid)
    emit_user_states(me)
    if target in online_users:
        emit_user_states(target)


@socketio.on("direct_message")
def direct_message(data):
    me = session.get("username")
    to_user = (data or {}).get("to")
    msg_text = (data or {}).get("msg", "").strip()
    if not me or not to_user or not msg_text:
        return
    ok, reason = gate_send(me, to_user)
    if not ok:
        emit("system_message", {"msg": reason}, to=request.sid)
        return

    mid, ts = save_message(me, to_user, "text", msg_text, None, None)
    delivered_ids = []
    for sid in list(user_sids.get(to_user, [])):
        emit(
            "direct_message",
            {"id": mid, "from": me, "msg": msg_text, "time": ts},
            to=sid,
        )
        delivered_ids.append(mid)
    if delivered_ids:
        mark_delivered(delivered_ids)
        for sid in list(user_sids.get(me, [])):
            emit("message_delivered", {"id": mid}, to=sid)
    emit("message_saved", {"id": mid, "time": ts}, to=request.sid)


@socketio.on("mark_read")
def mark_read(data):
    me = session.get("username")
    peer = (data or {}).get("peer")
    up_to_id = (data or {}).get("up_to_id")
    if not me or not peer or not isinstance(up_to_id, int):
        return
    mark_read_up_to(me, peer, up_to_id)
    for sid in list(user_sids.get(peer, [])):
        emit("messages_read", {"peer": me, "up_to_id": up_to_id}, to=sid)


@socketio.on("typing")
def on_typing(data):
    me = session.get("username")
    to_user = (data or {}).get("to")
    if not me or not to_user:
        return
    ok, _ = gate_send(me, to_user)
    if not ok:
        return
    for sid in list(user_sids.get(to_user, [])):
        emit("typing", {"from": me}, to=sid)


@socketio.on("stop_typing")
def on_stop_typing(data):
    me = session.get("username")
    to_user = (data or {}).get("to")
    if not me or not to_user:
        return
    ok, _ = gate_send(me, to_user)
    if not ok:
        return
    for sid in list(user_sids.get(to_user, [])):
        emit("stop_typing", {"from": me}, to=sid)


@socketio.on("toggle_reaction")
def toggle_reaction_evt(data):
    me = session.get("username")
    to_user = (data or {}).get("to")
    msg_id = (data or {}).get("id")
    emoji = (data or {}).get("emoji")
    action = (data or {}).get("action")
    if (
        not me
        or not to_user
        or not isinstance(msg_id, int)
        or not emoji
        or action not in ("add", "remove")
    ):
        return
    ok, _ = gate_send(me, to_user)
    if not ok:
        return
    payload = {"id": msg_id, "emoji": emoji, "action": action, "by": me}
    targets = set(user_sids.get(to_user, set())) | set(user_sids.get(me, set()))
    for sid in list(targets):
        emit("reaction_update", payload, to=sid)


@socketio.on("rtt_ping")
def rtt_ping(data):
    t = (data or {}).get("t")
    emit("rtt_pong", {"t": t})


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    debug_mode = os.getenv("FLASK_DEBUG", "false").lower() == "true"
    socketio.run(app, host="0.0.0.0", port=port, debug=debug_mode)
