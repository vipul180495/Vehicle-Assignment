import json
import os
import sqlite3
import threading
import urllib.request
import base64
import csv
import hashlib
import hmac
import io
import re
import secrets
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse
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
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "").strip()
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
          id {vehicle_id}, vin TEXT NOT NULL,
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
        CREATE TABLE IF NOT EXISTS monthly_archives (
          period TEXT NOT NULL, member_id INTEGER NOT NULL,
          overall_load INTEGER NOT NULL, manual_count INTEGER NOT NULL,
          auto_count INTEGER NOT NULL, exported_at TEXT NOT NULL,
          PRIMARY KEY(period, member_id),
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
            # Earlier versions treated VIN as unique. A vehicle can return for
            # another work assignment, so remove that legacy restriction.
            db.execute("ALTER TABLE vehicles DROP CONSTRAINT IF EXISTS vehicles_vin_key")
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


def recalculate_member_load(db, member_id):
    execute(db, """
      UPDATE members SET current_load=(
        SELECT COUNT(*) FROM vehicles
        WHERE vehicles.assigned_to=members.id AND vehicles.status='Assigned'
      ) WHERE id=?
    """, (member_id,))


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
    notification_title = "Vehicle assigned"
    reassigned = assignment_type == "Reassigned"
    completed = assignment_type == "Completed"
    if completed:
        notification_title = "✅ Vehicle Completed"
        facts = [
            {"title": "VIN", "value": vehicle["vin"]},
            {"title": "Engineer", "value": member["name"]},
            {"title": "Program", "value": vehicle["program"]},
            {"title": "Location", "value": vehicle["location"]},
            {"title": "Comments", "value": vehicle.get("comments") or "—"},
        ]
    elif reassigned:
        notification_title = "🔁 Vehicle Reassigned"
        facts = [
            {"title": "VIN", "value": vehicle["vin"]},
            {"title": "New Engineer", "value": member["name"]},
            {"title": "Previous Engineer", "value": vehicle.get("previous_name") or "—"},
            {"title": "Program", "value": vehicle["program"]},
            {"title": "Reason", "value": vehicle.get("reassignment_reason") or "—"},
            {"title": "Location", "value": vehicle["location"]},
            {"title": "Comments", "value": vehicle.get("comments") or "—"},
        ]
    else:
        facts = [
            {"title": "VIN", "value": vehicle["vin"]},
            {"title": "Assigned to", "value": member["name"]},
            {"title": "Program", "value": vehicle["program"]},
            {"title": "Location", "value": vehicle["location"]},
            {"title": "Comments", "value": vehicle.get("comments") or "—"},
        ]
    payload = {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "contentUrl": None,
            "content": {
                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                "type": "AdaptiveCard", "version": "1.4",
                "body": [
                    {"type": "TextBlock", "text": notification_title, "weight": "Bolder", "size": "Medium"},
                    {"type": "FactSet", "facts": facts}
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


def safe_notify_teams(vehicle, member, assignment_type):
    """A notification failure must never fail or roll back vehicle work."""
    try:
        return notify_teams(vehicle, member, assignment_type)
    except Exception as exc:
        print(f"Teams notification template failed: {exc}")
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
        return role if role in ("manager", "team", "admin") and hmac.compare_digest(signature, expected) else None

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
        if path == "/api/admin/export":
            if not self.require_role("admin"):
                return
            return self.export_month_csv()
        if path == "/api/state":
            if not self.require_role("manager", "team", "admin"):
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
            "/assets/dashboard-background.png",
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
        if path in ("/admin", "/admin.html"):
            if self.role() != "admin":
                return self.redirect_login("admin")
            return self.serve_static("/admin.html")
        if path == "/":
            destination = self.role()
            if destination not in ("manager", "team", "admin"):
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
            if path == "/api/admin/reset-counts":
                if not self.require_role("admin"): return
                return self.reset_monthly_counts(data)
            if path == "/api/admin/recalculate-loads":
                if not self.require_role("admin"): return
                return self.recalculate_current_loads()
            if path.startswith("/api/admin/members/") and path.endswith("/counts"):
                if not self.require_role("admin"): return
                return self.correct_member_counts(int(path.split("/")[4]), data)
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
            self.send_json({"error": "The submitted data conflicts with an existing record."}, 409)
        except Exception as exc:
            print(exc)
            self.send_json({"error": "The operation could not be completed."}, 500)

    def login(self, data):
        requested = str(data.get("role", "team"))
        supplied = str(data.get("password", ""))
        configured = {
            "manager": MANAGER_PASSWORD,
            "team": TEAM_PASSWORD,
            "admin": ADMIN_PASSWORD,
        }.get(requested, "")
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

    def requested_month(self, data=None):
        if data is None:
            query = parse_qs(urlparse(self.path).query)
            value = query.get("month", [""])[0]
        else:
            value = str(data.get("month", ""))
        if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value):
            raise ValueError("Month must use YYYY-MM format.")
        return value

    def export_month_csv(self):
        month = self.requested_month()
        with connect() as db:
            report_rows = rows(db, """
              SELECT e.created_at, e.event_type, v.vin, v.program, v.location,
                     m.name teammate, COALESCE(v.assignment_type,'') assignment_type,
                     COALESCE(e.details,'') details
              FROM events e JOIN vehicles v ON v.id=e.vehicle_id
              LEFT JOIN members m ON m.id=e.member_id
              WHERE e.created_at LIKE ? AND e.event_type IN ('Assigned','Reassigned','Completed')
              ORDER BY e.created_at, e.id
            """, (month + "%",))
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(["Date (UTC)", "Event", "VIN", "Program", "Location",
                         "Teammate", "Assignment Type", "Details"])
        for row in report_rows:
            writer.writerow([row["created_at"], row["event_type"], row["vin"],
                             row["program"], row["location"], row["teammate"] or "",
                             row["assignment_type"], row["details"]])
        body = output.getvalue().encode("utf-8-sig")
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="vehicle-assignments-{month}.csv"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def reset_monthly_counts(self, data):
        month = self.requested_month(data)
        stamp = now_iso()
        with DB_LOCK, connect() as db:
            begin_write(db)
            existing = execute(db, "SELECT 1 FROM monthly_archives WHERE period=? LIMIT 1", (month,)).fetchone()
            if existing:
                return self.send_json({"error": f"Counts for {month} were already archived and reset."}, 409)
            members = rows(db, "SELECT id,overall_load,manual_count,auto_count FROM members")
            for member in members:
                execute(db, """
                  INSERT INTO monthly_archives(period,member_id,overall_load,manual_count,auto_count,exported_at)
                  VALUES(?,?,?,?,?,?)
                """, (month, member["id"], member["overall_load"], member["manual_count"],
                      member["auto_count"], stamp))
            execute(db, "UPDATE members SET overall_load=0,manual_count=0,auto_count=0")
        self.send_json({"ok": True, "membersArchived": len(members), "period": month})

    def correct_member_counts(self, member_id, data):
        auto_count = int(data.get("autoCount", 0))
        manual_count = int(data.get("manualCount", 0))
        if auto_count < 0 or manual_count < 0:
            return self.send_json({"error": "Counts cannot be negative."}, 400)
        with DB_LOCK, connect() as db:
            updated = execute(
                db,
                "UPDATE members SET auto_count=?,manual_count=?,overall_load=? WHERE id=?",
                (auto_count, manual_count, auto_count + manual_count, member_id),
            ).rowcount
            if updated != 1:
                return self.send_json({"error": "Teammate not found."}, 404)
        self.send_json({"ok": True, "overallLoad": auto_count + manual_count})

    def recalculate_current_loads(self):
        with DB_LOCK, connect() as db:
            begin_write(db)
            execute(db, """
              UPDATE members SET current_load=(
                SELECT COUNT(*) FROM vehicles
                WHERE vehicles.assigned_to=members.id AND vehicles.status='Assigned'
              )
            """)
        self.send_json({"ok": True})

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
        sent = safe_notify_teams(result_vehicle, result_member, mode)
        self.send_json({"ok": True, "assignedTo": result_member["name"], "teamsSent": sent})

    def complete_vehicle(self, vehicle_id):
        next_assignment = None
        completed_vehicle = None
        completed_member = None
        with DB_LOCK, connect() as db:
            begin_write(db)
            vehicle = execute(db, "SELECT * FROM vehicles WHERE id=?", (vehicle_id,)).fetchone()
            if not vehicle or vehicle["status"] != "Assigned":
                return self.send_json({"error": "Vehicle is not currently assigned."}, 409)
            member = execute(db, "SELECT * FROM members WHERE id=?", (vehicle["assigned_to"],)).fetchone()
            stamp = now_iso()
            execute(db, "UPDATE vehicles SET status='Completed',completed_at=? WHERE id=?", (stamp, vehicle_id))
            recalculate_member_load(db, vehicle["assigned_to"])
            execute(db, "INSERT INTO events(vehicle_id,event_type,member_id,created_at) VALUES(?,?,?,?)",
                       (vehicle_id, "Completed", vehicle["assigned_to"], stamp))
            next_assignment = assign_oldest_waiting(db, vehicle["assigned_to"])
            completed_vehicle = dict(vehicle)
            completed_member = dict(member) if member else {"name": "Unknown"}
        completed_sent = safe_notify_teams(completed_vehicle, completed_member, "Completed")
        if next_assignment:
            queued_vehicle, free_member = next_assignment
            queue_sent = safe_notify_teams(queued_vehicle, free_member, "Auto")
            return self.send_json({"ok": True, "autoAssigned": queued_vehicle["vin"],
                                   "assignedTo": free_member["name"], "teamsSent": completed_sent,
                                   "queueTeamsSent": queue_sent})
        self.send_json({"ok": True, "autoAssigned": None, "teamsSent": completed_sent})

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
            previous_member = execute(db, "SELECT name FROM members WHERE id=?", (old_id,)).fetchone()
            execute(db, "UPDATE vehicles SET previous_assignee=?,assigned_to=?,assignment_type='Manual',assigned_at=?,reassignment_reason=? WHERE id=?",
                       (old_id, new_member_id, stamp, reason, vehicle_id))
            execute(db, "UPDATE members SET overall_load=overall_load+1,manual_count=manual_count+1 WHERE id=?", (new_member_id,))
            recalculate_member_load(db, old_id)
            recalculate_member_load(db, new_member_id)
            execute(db, "INSERT INTO events(vehicle_id,event_type,member_id,details,created_at) VALUES(?,?,?,?,?)",
                       (vehicle_id, "Reassigned", new_member_id, reason, stamp))
            result_vehicle, result_member = dict(vehicle), dict(member)
            result_vehicle["previous_name"] = previous_member["name"] if previous_member else ""
            result_vehicle["reassignment_reason"] = reason
        sent = safe_notify_teams(result_vehicle, result_member, "Reassigned")
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
            sent = safe_notify_teams(queued_vehicle, free_member, "Auto")
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
    if not MANAGER_PASSWORD or not TEAM_PASSWORD or not ADMIN_PASSWORD:
        print("WARNING: Set MANAGER_PASSWORD, TEAM_PASSWORD, and ADMIN_PASSWORD before sharing the app.")
    server.serve_forever()
