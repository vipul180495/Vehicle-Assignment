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

    def test_manager_can_correct_then_cancel_mistaken_assignment(self):
        vehicle_id = self.seed_vehicle("WRONG100", "Assigned")
        with server.connect() as db:
            server.execute(
                db, "UPDATE members SET current_load=1,overall_load=1,auto_count=1 WHERE id=1"
            )
        response = ResponseRecorder()

        server.Handler.edit_vehicle(response, vehicle_id, {
            "vin": "RIGHT100", "program": "HDCC", "location": "CTC", "comments": "Corrected"
        })
        self.assertEqual(response.status, 200)
        server.Handler.cancel_assignment(response, vehicle_id)

        self.assertEqual(response.status, 200)
        with server.connect() as db:
            vehicle = server.execute(db, "SELECT * FROM vehicles WHERE id=?", (vehicle_id,)).fetchone()
            member = server.execute(db, "SELECT * FROM members WHERE id=1").fetchone()
            self.assertEqual(vehicle["vin"], "RIGHT100")
            self.assertEqual(vehicle["program"], "HDCC")
            self.assertEqual(vehicle["location"], "CTC")
            self.assertEqual(vehicle["status"], "Queued")
            self.assertIsNone(vehicle["assigned_to"])
            self.assertEqual(member["current_load"], 0)
            self.assertEqual(member["overall_load"], 0)
            self.assertEqual(member["auto_count"], 0)

    def test_edit_can_transfer_mistaken_assignee_and_count(self):
        vehicle_id = self.seed_vehicle("ASSIGNEE100", "Assigned")
        with server.connect() as db:
            server.execute(db, "UPDATE members SET current_load=1,overall_load=1,auto_count=1 WHERE id=1")
        response = ResponseRecorder()

        server.Handler.edit_vehicle(response, vehicle_id, {
            "vin": "ASSIGNEE100", "program": "DT ICE", "location": "FREC",
            "comments": "", "memberId": 2,
        })

        self.assertEqual(response.status, 200)
        self.assertTrue(response.value["assigneeChanged"])
        with server.connect() as db:
            vehicle = server.execute(db, "SELECT * FROM vehicles WHERE id=?", (vehicle_id,)).fetchone()
            old_member = server.execute(db, "SELECT * FROM members WHERE id=1").fetchone()
            new_member = server.execute(db, "SELECT * FROM members WHERE id=2").fetchone()
            self.assertEqual(vehicle["assigned_to"], 2)
            self.assertEqual(old_member["current_load"], 0)
            self.assertEqual(old_member["overall_load"], 0)
            self.assertEqual(old_member["auto_count"], 0)
            self.assertEqual(new_member["current_load"], 1)
            self.assertEqual(new_member["overall_load"], 1)
            self.assertEqual(new_member["auto_count"], 1)

    def test_permanent_cancel_is_final_and_reverses_assignment_count(self):
        vehicle_id = self.seed_vehicle("CANCEL100", "Assigned")
        with server.connect() as db:
            server.execute(db, "UPDATE members SET current_load=1,overall_load=1,auto_count=1 WHERE id=1")
        response = ResponseRecorder()

        server.Handler.cancel_vehicle(response, vehicle_id, {"reason": "Vehicle removed from program"})

        self.assertEqual(response.status, 200)
        with server.connect() as db:
            vehicle = server.execute(db, "SELECT * FROM vehicles WHERE id=?", (vehicle_id,)).fetchone()
            member = server.execute(db, "SELECT * FROM members WHERE id=1").fetchone()
            self.assertEqual(vehicle["status"], "Cancelled")
            self.assertEqual(vehicle["cancellation_reason"], "Vehicle removed from program")
            self.assertIsNotNone(vehicle["cancelled_at"])
            self.assertEqual(member["current_load"], 0)
            self.assertEqual(member["overall_load"], 0)
            self.assertEqual(member["auto_count"], 0)

    def test_external_work_is_recorded_without_changing_home_location(self):
        response = ResponseRecorder()
        server.Handler.create_external_work(response, {
            "vin": "EXT100", "program": "DT ICE", "location": "Auburn Hills",
            "comments": "Assigned directly on site", "memberId": 1,
            "status": "Assigned", "assignedDate": "2026-09-23", "sendTeams": False,
        })

        self.assertEqual(response.status, 200)
        with server.connect() as db:
            vehicle = server.execute(db, "SELECT * FROM vehicles WHERE vin='EXT100'").fetchone()
            member = server.execute(db, "SELECT * FROM members WHERE id=1").fetchone()
            self.assertEqual(vehicle["assignment_type"], "External")
            self.assertEqual(vehicle["status"], "Assigned")
            self.assertEqual(vehicle["location"], "AUBURN HILLS")
            self.assertEqual(member["location"], "FREC")
            self.assertEqual(member["current_load"], 1)
            self.assertEqual(member["overall_load"], 1)
            self.assertEqual(member["auto_count"], 0)
            self.assertEqual(member["manual_count"], 0)

    def test_vehicle_history_tracks_previous_engineer_on_reassignment(self):
        vehicle_id = self.seed_vehicle("HISTORY100", "Assigned")
        with server.connect() as db:
            server.execute(db, "INSERT INTO events(vehicle_id,event_type,member_id,details,created_at) VALUES(?,?,?,?,?)",
                           (vehicle_id, "Reassigned", 2, "Coverage change", "2026-09-11T13:00:00+00:00"))
        response = ResponseRecorder()

        server.Handler.vehicle_history(response, vehicle_id)

        self.assertEqual(response.status, 200)
        reassigned = response.value["history"][-1]
        self.assertEqual(reassigned["event_type"], "Reassigned")
        self.assertEqual(reassigned["member_name"], "Elias Saleh")
        self.assertEqual(reassigned["previous_engineer"], "Dheeraj Adabala")
        self.assertEqual(reassigned["details"], "Coverage change")


if __name__ == "__main__":
    unittest.main()
