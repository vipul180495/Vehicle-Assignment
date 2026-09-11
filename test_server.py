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


if __name__ == "__main__":
    unittest.main()
