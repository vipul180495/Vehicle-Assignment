import json
import os
import sqlite3
import threading
import urllib.request
import base64
import hashlib
import hmac
import secrets
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse
from http.cookies import SimpleCookie


ROOT = Path(__file__).resolve().parent
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
USE_POSTGRES = DATABASE_URL.startswith(("postgres://", "postgresql://"))
DB_PATH = Path(os.getenv("DATABASE_PATH", str(ROOT / "vehicle_assignments.db")))
DB_LOCK = threading.Lock()
PORT = int(os.getenv("PORT", "8080"))
COOKIE_SECRET = os.getenv("COOKIE_SECRET", "").strip() or secrets.token_hex(32)
MANAGER_PASSWORD = os.getenv("MANAGER_PASSWORD", "").strip()
TEAM_PASSWORD = os.getenv("TEAM_PASSWORD", "").strip()
PROGRAMS = {
    "DT REEV SFFB", "WS REEV SFFB", "DT REEV", "WS REEV",
    "DT ICE", "DT TRX", "DT F16", "HDCC",
}

SEED_MEMBERS = [
    (1, "Dheeraj Adabala", "FREC"), (2, "Elias Saleh", "FREC"),
    (3, "Farhan Naeem", "FREC"), (4, "Joseph Girimonte", "FREC"),
    (5, "Monisha Lanka", "FREC"), (6, "Narendra Reddy", "FREC"),
    (7, "Rishika Kumar", "FREC"), (8, "Sameer Mohammed", "FREC"),
    (9, "Syed Ahmed", "FREC"), (10, "Vipul Prajapati", "FREC"),
]


def connect():
    if USE_POSTGRES:
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise RuntimeError("PostgreSQL requires: pip install -r requirements.txt") from exc
        # Supabase's transaction pooler uses PgBouncer. Named prepared
        # statements can collide when pooled server connections are reused.
        return psycopg.connect(
            DATABASE_URL,
            row_factory=dict_row,
            prepare_threshold=None,
        )
    db = sqlite3.connect(DB_PATH, timeout=15)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA journal_mode = WAL")
    return db


def init_db():
    if not USE_POSTGRES:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with connect() as db:
        schema = """
        CREATE TABLE IF NOT EXISTS members (
          id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, location TEXT NOT NULL,
          available_today INTEGER NOT NULL DEFAULT 1,
          current_load INTEGER NOT NULL DEFAULT 0,
          overall_load INTEGER NOT NULL DEFAULT 0,
          manual_count INTEGER NOT NULL DEFAULT 0,
          auto_count INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS vehicles (
          id {vehicle_id}, vin TEXT NOT NULL UNIQUE,
          program TEXT NOT NULL, location TEXT NOT NULL, comments TEXT DEFAULT '',
          status TEXT NOT NULL DEFAULT 'Queued', assigned_to INTEGER,
          assignment_type TEXT, assigned_at TEXT, completed_at TEXT,
          previous_assignee INTEGER, reassignment_reason TEXT,
          FOREIGN KEY(assigned_to) REFERENCES members(id),
          FOREIGN KEY(previous_assignee) REFERENCES members(id)
        );
        CREATE TABLE IF NOT EXISTS events (
          id {event_id}, vehicle_id INTEGER NOT NULL,
          event_type TEXT NOT NULL, member_id INTEGER, details TEXT,
          created_at TEXT NOT NULL,
          FOREIGN KEY(vehicle_id) REFERENCES vehicles(id),
          FOREIGN KEY(member_id) REFERENCES members(id)
        );
        """.format(
            vehicle_id="SERIAL PRIMARY KEY" if USE_POSTGRES else "INTEGER PRIMARY KEY AUTOINCREMENT",
            event_id="SERIAL PRIMARY KEY" if USE_POSTGRES else "INTEGER PRIMARY KEY AUTOINCREMENT",
        )
        if USE_POSTGRES:
            for statement in schema.split(";"):
                if statement.strip():
                    db.execute(statement)
            for member in SEED_MEMBERS:
                db.execute(
                    "INSERT INTO members(id,name,location) VALUES(%s,%s,%s) ON CONFLICT (id) DO NOTHING",
                    member,
                )
        else:
            db.executescript(schema)
            db.executemany(
                "INSERT OR IGNORE INTO members(id,name,location) VALUES(?,?,?)", SEED_MEMBERS
            )


def sql(query):
    """Translate SQLite placeholders for PostgreSQL while keeping queries readable."""
    return query.replace("?", "%s") if USE_POSTGRES else query


def execute(db, query, args=()):
    return db.execute(sql(query), args)


def begin_write(db):
    if not USE_POSTGRES:
        db.execute("BEGIN IMMEDIATE")


def decrement_expression(column):
    fn = "GREATEST" if USE_POSTGRES else "MAX"
    return f"{fn}(0,{column}-1)"


def assign_oldest_waiting(db, member_id):
    """Assign the oldest same-location queue item to a teammate who just became free."""
    member_lock = " FOR UPDATE" if USE_POSTGRES else ""
    member = execute(
        db,
        "SELECT * FROM members WHERE id=? AND available_today=1 AND current_load=0" + member_lock,
        (member_id,),
    ).fetchone()
    if not member:
        return None
    vehicle_lock = " FOR UPDATE SKIP LOCKED" if USE_POSTGRES else ""
    vehicle = execute(
        db,
        "SELECT * FROM vehicles WHERE status='Queued' AND location=? ORDER BY id LIMIT 1" + vehicle_lock,
        (member["location"],),
    ).fetchone()
    if not vehicle:
        return None
    stamp = now_iso()
    updated = execute(
        db,
        "UPDATE vehicles SET status='Assigned',assigned_to=?,assignment_type='Auto',assigned_at=? "
        "WHERE id=? AND status='Queued'",
        (member_id, stamp, vehicle["id"]),
    ).rowcount
    if updated != 1:
        return None
    execute(
        db,
        "UPDATE members SET current_load=current_load+1,overall_load=overall_load+1,"
        "auto_count=auto_count+1 WHERE id=?",
        (member_id,),
    )
    execute(
        db,
        "INSERT INTO events(vehicle_id,event_type,member_id,details,created_at) VALUES(?,?,?,?,?)",
        (vehicle["id"], "Assigned", member_id, "Auto from queue", stamp),
    )
    return dict(vehicle), dict(member)


def rows(db, sql, args=()):
    return [dict(row) for row in execute(db, sql, args).fetchall()]


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def notify_teams(vehicle, member, assignment_type):
    url = os.getenv("TEAMS_WEBHOOK_URL", "").strip()
    if not url:
        return False
    payload = {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "contentUrl": None,
            "content": {
                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                "type": "AdaptiveCard", "version": "1.4",
                "body": [
                    {"type": "TextBlock", "text": "Vehicle assigned", "weight": "Bolder", "size": "Medium"},
                    {"type": "FactSet", "facts": [
                        {"title": "VIN", "value": vehicle["vin"]},
                        {"title": "Assigned to", "value": member["name"]},
                        {"title": "Program", "value": vehicle["program"]},
                        {"title": "Location", "value": vehicle["location"]},
                        {"title": "Assignment", "value": assignment_type},
                        {"title": "Comments", "value": vehicle.get("comments") or "—"}
                    ]}
                ]
            }
        }]
    }
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            return 200 <= response.status < 300
    except Exception as exc:
        print(f"Teams notification failed: {exc}")
        return False


class Handler(SimpleHTTPRequestHandler):
    def redirect_login(self, destination):
        self.send_response(302)
        self.send_header("Location", f"/login?next={destination}")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def serve_static(self, target):
        self.path = target
        return super().do_GET()

    def role(self):
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        value = cookie.get("vehicle_session")
        if not value or "." not in value.value:
            return None
        role, signature = value.value.split(".", 1)
        expected = hmac.new(COOKIE_SECRET.encode(), role.encode(), hashlib.sha256).hexdigest()
        return role if role in ("manager", "team") and hmac.compare_digest(signature, expected) else None

    def require_role(self, *allowed):
        if self.role() not in allowed:
            self.send_json({"error": "Please sign in to continue."}, 401)
            return False
        return True

    def session_cookie(self, role):
        signature = hmac.new(COOKIE_SECRET.encode(), role.encode(), hashlib.sha256).hexdigest()
        secure = "; Secure" if os.getenv("RENDER") else ""
        return f"vehicle_session={role}.{signature}; Path=/; HttpOnly; SameSite=Strict; Max-Age=43200{secure}"

    def send_json(self, value, status=200):
        body = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/state":
            if not self.require_role("manager", "team"):
                return
            with connect() as db:
                members = rows(db, "SELECT * FROM members ORDER BY name")
                vehicles = rows(db, """
                  SELECT v.*, m.name assigned_name, p.name previous_name
                  FROM vehicles v LEFT JOIN members m ON m.id=v.assigned_to
                  LEFT JOIN members p ON p.id=v.previous_assignee
                  ORDER BY CASE v.status WHEN 'Assigned' THEN 0 WHEN 'Queued' THEN 1 ELSE 2 END,
                           COALESCE(v.assigned_at, v.completed_at, '') DESC, v.id DESC
                """)
            return self.send_json({"members": members, "vehicles": vehicles,
                                   "teamsConfigured": bool(os.getenv("TEAMS_WEBHOOK_URL")),
                                   "role": self.role()})
        if path == "/api/session":
            return self.send_json({"role": self.role()})
        if path in ("/login", "/login.html"):
            return self.serve_static("/login.html")
        if path in (
            "/styles.css", "/app.js",
            "/assets/vona-logo.png", "/assets/rob-caudill-logo.png",
        ):
            return self.serve_static(path)
        if path in ("/manager", "/manager.html"):
            if self.role() != "manager":
                return self.redirect_login("manager")
            return self.serve_static("/manager.html")
        if path in ("/team", "/team.html"):
            if self.role() != "team":
                return self.redirect_login("team")
            return self.serve_static("/team.html")
        if path == "/":
            destination = self.role()
            if destination not in ("manager", "team"):
                return self.redirect_login("team")
            self.send_response(302)
            self.send_header("Location", f"/{destination}")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return
        # Never expose source code, the local database, Git metadata, or other files.
        self.send_error(404, "Not found")

    def do_POST(self):
        try:
            data = self.read_json()
            path = urlparse(self.path).path
            if path == "/api/login":
                return self.login(data)
            if path == "/api/logout":
                self.send_response(200)
                self.send_header("Set-Cookie", "vehicle_session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0")
                self.send_header("Content-Type", "application/json")
                self.end_headers(); self.wfile.write(b'{"ok":true}'); return
            if path == "/api/vehicles":
                if not self.require_role("manager"): return
                return self.create_vehicle(data)
            if path.startswith("/api/vehicles/") and path.endswith("/assign"):
                if not self.require_role("manager"): return
                return self.assign_vehicle(int(path.split("/")[3]), data)
            if path.startswith("/api/vehicles/") and path.endswith("/complete"):
                if not self.require_role("team"): return
                return self.complete_vehicle(int(path.split("/")[3]))
            if path.startswith("/api/vehicles/") and path.endswith("/reassign"):
                if not self.require_role("team"): return
                return self.reassign_vehicle(int(path.split("/")[3]), data)
            if path.startswith("/api/members/") and path.endswith("/availability"):
                if not self.require_role("team"): return
                return self.set_availability(int(path.split("/")[3]), data)
            self.send_json({"error": "Not found"}, 404)
        except (ValueError, KeyError, json.JSONDecodeError) as exc:
            self.send_json({"error": f"Invalid request: {exc}"}, 400)
        except sqlite3.IntegrityError as exc:
            self.send_json({"error": "VIN already exists or the data is invalid."}, 409)
        except Exception as exc:
            print(exc)
            self.send_json({"error": "The operation could not be completed."}, 500)

    def login(self, data):
        requested = str(data.get("role", "team"))
        supplied = str(data.get("password", ""))
        configured = MANAGER_PASSWORD if requested == "manager" else TEAM_PASSWORD
        if not configured:
            return self.send_json({"error": f"The {requested} password has not been configured."}, 503)
        if not hmac.compare_digest(supplied, configured):
            return self.send_json({"error": "Incorrect password."}, 401)
        body = b'{"ok":true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Set-Cookie", self.session_cookie(requested))
        self.end_headers()
        self.wfile.write(body)

    def create_vehicle(self, data):
        vin = str(data["vin"]).strip().upper()
        program = str(data["program"]).strip()
        location = str(data["location"]).strip()
        if not vin or not program or not location:
            return self.send_json({"error": "VIN, program and location are required."}, 400)
        if program not in PROGRAMS:
            return self.send_json({"error": "Select a valid vehicle program."}, 400)
        with DB_LOCK, connect() as db:
            insert = "INSERT INTO vehicles(vin,program,location,comments) VALUES(?,?,?,?)"
            if USE_POSTGRES:
                insert += " RETURNING id"
            cursor = execute(db, insert,
                (vin, program, location, str(data.get("comments", "")).strip())
            )
            vehicle_id = cursor.fetchone()["id"] if USE_POSTGRES else cursor.lastrowid
        if data.get("assignMode") in ("Auto", "Manual"):
            return self.assign_vehicle(vehicle_id, data)
        self.send_json({"ok": True, "id": vehicle_id}, 201)

    def assign_vehicle(self, vehicle_id, data):
        mode = data.get("assignMode", "Auto")
        with DB_LOCK, connect() as db:
            begin_write(db)
            vehicle = execute(db, "SELECT * FROM vehicles WHERE id=?", (vehicle_id,)).fetchone()
            if not vehicle or vehicle["status"] != "Queued":
                return self.send_json({"error": "Vehicle is not available to assign."}, 409)
            if mode == "Manual":
                lock = " FOR UPDATE" if USE_POSTGRES else ""
                member = execute(db,
                    "SELECT * FROM members WHERE id=? AND available_today=1 AND current_load=0" + lock,
                    (int(data["memberId"]),)
                ).fetchone()
            else:
                lock = " FOR UPDATE SKIP LOCKED" if USE_POSTGRES else ""
                member = execute(db, """
                  SELECT * FROM members WHERE available_today=1 AND current_load=0
                  AND location=? ORDER BY overall_load, auto_count, id LIMIT 1
                """ + lock, (vehicle["location"],)).fetchone()
            if not member:
                return self.send_json({"error": "No available teammate for this location."}, 409)
            stamp = now_iso()
            updated = execute(db,
                "UPDATE vehicles SET status='Assigned',assigned_to=?,assignment_type=?,assigned_at=? WHERE id=? AND status='Queued'",
                (member["id"], mode, stamp, vehicle_id)
            ).rowcount
            if updated != 1:
                return self.send_json({"error": "Vehicle was assigned by someone else. Refresh and try again."}, 409)
            count_field = "manual_count" if mode == "Manual" else "auto_count"
            execute(db, f"UPDATE members SET current_load=current_load+1,overall_load=overall_load+1,{count_field}={count_field}+1 WHERE id=?", (member["id"],))
            execute(db, "INSERT INTO events(vehicle_id,event_type,member_id,details,created_at) VALUES(?,?,?,?,?)",
                       (vehicle_id, "Assigned", member["id"], mode, stamp))
            result_vehicle, result_member = dict(vehicle), dict(member)
        sent = notify_teams(result_vehicle, result_member, mode)
        self.send_json({"ok": True, "assignedTo": result_member["name"], "teamsSent": sent})

    def complete_vehicle(self, vehicle_id):
        next_assignment = None
        with DB_LOCK, connect() as db:
            begin_write(db)
            vehicle = execute(db, "SELECT * FROM vehicles WHERE id=?", (vehicle_id,)).fetchone()
            if not vehicle or vehicle["status"] != "Assigned":
                return self.send_json({"error": "Vehicle is not currently assigned."}, 409)
            stamp = now_iso()
            execute(db, "UPDATE vehicles SET status='Completed',completed_at=? WHERE id=?", (stamp, vehicle_id))
            execute(db, f"UPDATE members SET current_load={decrement_expression('current_load')} WHERE id=?", (vehicle["assigned_to"],))
            execute(db, "INSERT INTO events(vehicle_id,event_type,member_id,created_at) VALUES(?,?,?,?)",
                       (vehicle_id, "Completed", vehicle["assigned_to"], stamp))
            next_assignment = assign_oldest_waiting(db, vehicle["assigned_to"])
        if next_assignment:
            queued_vehicle, free_member = next_assignment
            sent = notify_teams(queued_vehicle, free_member, "Auto")
            return self.send_json({"ok": True, "autoAssigned": queued_vehicle["vin"],
                                   "assignedTo": free_member["name"], "teamsSent": sent})
        self.send_json({"ok": True, "autoAssigned": None})

    def reassign_vehicle(self, vehicle_id, data):
        new_member_id = int(data["memberId"])
        reason = str(data.get("reason", "")).strip()
        with DB_LOCK, connect() as db:
            begin_write(db)
            vehicle = execute(db, "SELECT * FROM vehicles WHERE id=? AND status='Assigned'", (vehicle_id,)).fetchone()
            lock = " FOR UPDATE" if USE_POSTGRES else ""
            member = execute(db, "SELECT * FROM members WHERE id=? AND available_today=1 AND current_load=0" + lock, (new_member_id,)).fetchone()
            if not vehicle or not member:
                return self.send_json({"error": "Vehicle or selected teammate is no longer available."}, 409)
            old_id, stamp = vehicle["assigned_to"], now_iso()
            execute(db, "UPDATE vehicles SET previous_assignee=?,assigned_to=?,assignment_type='Manual',assigned_at=?,reassignment_reason=? WHERE id=?",
                       (old_id, new_member_id, stamp, reason, vehicle_id))
            execute(db, f"UPDATE members SET current_load={decrement_expression('current_load')} WHERE id=?", (old_id,))
            execute(db, "UPDATE members SET current_load=current_load+1,overall_load=overall_load+1,manual_count=manual_count+1 WHERE id=?", (new_member_id,))
            execute(db, "INSERT INTO events(vehicle_id,event_type,member_id,details,created_at) VALUES(?,?,?,?,?)",
                       (vehicle_id, "Reassigned", new_member_id, reason, stamp))
            result_vehicle, result_member = dict(vehicle), dict(member)
        sent = notify_teams(result_vehicle, result_member, "Reassigned")
        self.send_json({"ok": True, "assignedTo": result_member["name"], "teamsSent": sent})

    def set_availability(self, member_id, data):
        available = 1 if data.get("available") else 0
        next_assignment = None
        with DB_LOCK, connect() as db:
            begin_write(db)
            execute(db, "UPDATE members SET available_today=? WHERE id=?", (available, member_id))
            if available:
                next_assignment = assign_oldest_waiting(db, member_id)
        if next_assignment:
            queued_vehicle, free_member = next_assignment
            sent = notify_teams(queued_vehicle, free_member, "Auto")
            return self.send_json({"ok": True, "autoAssigned": queued_vehicle["vin"],
                                   "assignedTo": free_member["name"], "teamsSent": sent})
        self.send_json({"ok": True, "autoAssigned": None})


if __name__ == "__main__":
    os.chdir(ROOT)
    init_db()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Vehicle Assignment app: http://localhost:{PORT}")
    print(f"Manager view: http://localhost:{PORT}/manager")
    print(f"Team view:    http://localhost:{PORT}/team")
    if not MANAGER_PASSWORD or not TEAM_PASSWORD:
        print("WARNING: Set MANAGER_PASSWORD and TEAM_PASSWORD before sharing the app.")
    server.serve_forever()
