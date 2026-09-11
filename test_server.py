import gc
import unittest
from pathlib import Path

import server


class ResponseRecorder:
    def send_json(self, value, status=200):
        self.value = value
        self.status = status
        return value


class DeleteDuplicateVehicleTests(unittest.TestCase):
    def setUp(self):
        server.USE_POSTGRES = False
        server.DB_PATH = Path(__file__).resolve().parent / ".test_vehicle_delete.db"
        self.remove_test_database()
        server.init_db()

    def tearDown(self):
        self.remove_test_database()

    def remove_test_database(self):
        gc.collect()
        for suffix in ("", "-wal", "-shm"):
            path = Path(str(server.DB_PATH) + suffix)
            if path.exists():
                path.unlink()

    def seed_vehicle(self, vin, status, assigned=True):
        with server.connect() as db:
            cursor = server.execute(
                db,
                "INSERT INTO vehicles(vin,program,location,status,assigned_to,assignment_type,assigned_at) VALUES(?,?,?,?,?,?,?)",
                (vin, "DT ICE", "FREC", status, 1 if assigned else None,
                 "Auto" if assigned else None, "2026-09-11T12:00:00+00:00" if assigned else None),
            )
            vehicle_id = cursor.lastrowid
            if assigned:
                server.execute(
                    db,
                    "INSERT INTO events(vehicle_id,event_type,member_id,details,created_at) VALUES(?,?,?,?,?)",
                    (vehicle_id, "Assigned", 1, "Auto", "2026-09-11T12:00:00+00:00"),
                )
            return vehicle_id

    def test_delete_duplicate_corrects_counts_and_load(self):
        duplicate_id = self.seed_vehicle("DUP100", "Assigned")
        self.seed_vehicle("DUP100", "Completed")
        with server.connect() as db:
            server.execute(
                db,
                "UPDATE members SET current_load=1,overall_load=2,auto_count=2 WHERE id=1",
            )

        response = ResponseRecorder()
        server.Handler.delete_duplicate_vehicle(response, duplicate_id)

        self.assertEqual(response.status, 200)
        with server.connect() as db:
            member = server.execute(db, "SELECT * FROM members WHERE id=1").fetchone()
            self.assertEqual(member["current_load"], 0)
            self.assertEqual(member["overall_load"], 1)
            self.assertEqual(member["auto_count"], 1)
            self.assertEqual(
                server.execute(db, "SELECT COUNT(*) total FROM vehicles WHERE vin='DUP100'").fetchone()["total"],
                1,
            )
            self.assertEqual(
                server.execute(db, "SELECT COUNT(*) total FROM events WHERE vehicle_id=?", (duplicate_id,)).fetchone()["total"],
                0,
            )

    def test_unique_vin_cannot_be_deleted(self):
        vehicle_id = self.seed_vehicle("ONLY100", "Completed")
        response = ResponseRecorder()

        server.Handler.delete_duplicate_vehicle(response, vehicle_id)

        self.assertEqual(response.status, 409)
        with server.connect() as db:
            self.assertEqual(
                server.execute(db, "SELECT COUNT(*) total FROM vehicles WHERE id=?", (vehicle_id,)).fetchone()["total"],
                1,
            )

    def test_hold_frees_engineer_and_assigns_oldest_queue(self):
        active_id = self.seed_vehicle("ACTIVE100", "Assigned")
        queued_id = self.seed_vehicle("QUEUE100", "Queued", assigned=False)
        with server.connect() as db:
            server.execute(
                db,
                "UPDATE members SET current_load=1,overall_load=1,auto_count=1 WHERE id=1",
            )

        response = ResponseRecorder()
        server.Handler.hold_vehicle(response, active_id, {"reason": "Waiting for electrical team"})

        self.assertEqual(response.status, 200)
        self.assertEqual(response.value["autoAssigned"], "QUEUE100")
        with server.connect() as db:
            held = server.execute(db, "SELECT * FROM vehicles WHERE id=?", (active_id,)).fetchone()
            queued = server.execute(db, "SELECT * FROM vehicles WHERE id=?", (queued_id,)).fetchone()
            member = server.execute(db, "SELECT * FROM members WHERE id=1").fetchone()
            self.assertEqual(held["status"], "On Hold")
            self.assertEqual(held["hold_reason"], "Waiting for electrical team")
            self.assertEqual(queued["status"], "Assigned")
            self.assertEqual(member["current_load"], 1)
            self.assertEqual(member["overall_load"], 2)

    def test_ready_vehicle_resumes_before_new_queue_after_completion(self):
        current_id = self.seed_vehicle("CURRENT100", "Assigned")
        ready_id = self.seed_vehicle("READY100", "Ready")
        self.seed_vehicle("QUEUE200", "Queued", assigned=False)
        with server.connect() as db:
            server.execute(db, "UPDATE vehicles SET hold_reason='External work complete' WHERE id=?", (ready_id,))
            server.execute(db, "UPDATE members SET current_load=1,overall_load=2,auto_count=2 WHERE id=1")

        response = ResponseRecorder()
        server.Handler.complete_vehicle(response, current_id)

        self.assertEqual(response.status, 200)
        self.assertEqual(response.value["resumed"], "READY100")
        self.assertIsNone(response.value["autoAssigned"])
        with server.connect() as db:
            ready = server.execute(db, "SELECT status FROM vehicles WHERE id=?", (ready_id,)).fetchone()
            queued = server.execute(db, "SELECT status FROM vehicles WHERE vin='QUEUE200'").fetchone()
            self.assertEqual(ready["status"], "Assigned")
            self.assertEqual(queued["status"], "Queued")


if __name__ == "__main__":
    unittest.main()
