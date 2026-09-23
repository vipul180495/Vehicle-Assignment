# Vehicle Assignment

A dependency-free shared web app for vehicle assignments. It replaces separate Power Apps/Power Automate updates with one transactional SQLite database.

## Run locally

```powershell
python server.py
```

Before sharing locally, configure separate passwords in the same PowerShell window:

```powershell
$env:MANAGER_PASSWORD = "choose-a-strong-manager-password"
$env:TEAM_PASSWORD = "choose-a-different-team-password"
$env:ADMIN_PASSWORD = "choose-a-private-admin-password"
$env:COOKIE_SECRET = "a-long-random-secret-value"
python server.py
```

- Manager: `http://localhost:8080/manager`
- Team: `http://localhost:8080/team`

The database is created automatically as `vehicle_assignments.db` and seeded with the ten team members shown in the current SharePoint list.

## Microsoft Teams notification

In Teams, create a Workflow using the trigger **When a Teams webhook request is received**, add **Post card in a chat or channel**, select the group chat/channel, save it, and copy the generated webhook URL. Start the app with that URL:

```powershell
$env:TEAMS_WEBHOOK_URL = "PASTE_YOUR_WORKFLOW_WEBHOOK_URL"
python server.py
```

Assignments, reassignments, completions, holds, and resumptions post an Adaptive Card with the relevant vehicle details. Vehicle operations still succeed if Teams is temporarily unavailable; the server logs the notification failure.

## Business rules implemented

- Auto assignment chooses an available teammate in the same location with no active vehicle and the lowest overall assignment count.
- If nobody is available, the vehicle remains queued. The oldest same-location queued vehicle is automatically assigned when a teammate completes a vehicle or becomes available.
- Manual assignment uses the manager's selected available teammate.
- Managers can record work received outside the app as External, either in progress or already completed, with an optional Teams notification. It counts toward Total Assignments without changing the teammate's home location.
- Managers can correct VIN, program, location, and comments on non-completed records. They can also undo a mistaken assignment, returning the vehicle to the queue and reversing its current-period count.
- Managers can permanently cancel a vehicle with a required reason. Cancelled vehicles never return to the queue; active capacity is freed and the current assignment count is reversed.
- Assignment increments Active Vehicles, Total Assignments, and the corresponding auto/manual count in one database transaction.
- Teammates complete or reassign their active vehicles from the Team Board.
- An active vehicle can be placed On Hold with a reason. It remains owned by its engineer but no longer consumes their active capacity, allowing another vehicle to be assigned.
- When external work is finished, Resume starts the held vehicle immediately if its engineer is free. Otherwise it becomes Ready and automatically resumes, ahead of queued work, when that engineer becomes free.
- Completion decrements Active Vehicles but preserves Total Assignments.
- Reassignment frees the old teammate and increments the new teammate's manual and overall totals.
- The same VIN can be submitted again for a later work assignment; each submission is stored as a separate record. Concurrent requests cannot assign the same work item twice.

## Sharing with the team

For a pilot, run this on an always-on internal Windows PC/server and allow inbound TCP port 8080, then share `http://SERVER-NAME:8080/manager` with managers and `/team` with teammates. For production, put it behind company sign-in and HTTPS (IIS reverse proxy or Azure App Service) and restrict the manager route by an Entra ID group.

## Deploy to Render

The included `render.yaml` creates a free Render web service. Push this folder to a private GitHub repository, choose **New > Blueprint** in Render, connect the repository, and supply `DATABASE_URL`, `MANAGER_PASSWORD`, `TEAM_PASSWORD`, `ADMIN_PASSWORD`, and optionally `TEAMS_WEBHOOK_URL` when prompted. Render generates `COOKIE_SECRET` automatically.

The Admin page at `/admin` can export a selected month's event history as CSV, correct each teammate's Auto/Manual/External counts, recalculate Active Vehicles, and archive/reset monthly counters. Active Vehicles is never edited arbitrarily because it controls assignment availability.

For persistent storage on a free deployment, create a free Supabase project, copy its PostgreSQL connection string, and set it as Render's `DATABASE_URL`. Use the Supabase transaction pooler connection string when direct database connections are not available. The application uses local SQLite only when `DATABASE_URL` is absent.

## Backup

Stop the app briefly and copy `vehicle_assignments.db` to your normal backed-up company storage. Avoid placing the live database itself on a synchronized OneDrive/SharePoint folder.
