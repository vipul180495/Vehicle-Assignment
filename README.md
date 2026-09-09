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

Every new assignment and reassignment posts an Adaptive Card with the VIN, teammate, program, location, assignment type, and comments. Assignment still succeeds if Teams is temporarily unavailable; the server logs the notification failure.

## Business rules implemented

- Auto assignment chooses an available teammate in the same location with no active vehicle and the lowest overall assignment count.
- If nobody is available, the vehicle remains queued. The oldest same-location queued vehicle is automatically assigned when a teammate completes a vehicle or becomes available.
- Manual assignment uses the manager's selected available teammate.
- Assignment increments current load, overall load, and the corresponding auto/manual count in one database transaction.
- Teammates complete or reassign their active vehicles from the Team Board.
- Completion decrements current load but preserves historical totals.
- Reassignment frees the old teammate and increments the new teammate's manual and overall totals.
- The database rejects duplicate VINs, and concurrent requests cannot assign the same vehicle twice.

## Sharing with the team

For a pilot, run this on an always-on internal Windows PC/server and allow inbound TCP port 8080, then share `http://SERVER-NAME:8080/manager` with managers and `/team` with teammates. For production, put it behind company sign-in and HTTPS (IIS reverse proxy or Azure App Service) and restrict the manager route by an Entra ID group.

## Deploy to Render

The included `render.yaml` creates a free Render web service. Push this folder to a private GitHub repository, choose **New > Blueprint** in Render, connect the repository, and supply `DATABASE_URL`, `MANAGER_PASSWORD`, `TEAM_PASSWORD`, `ADMIN_PASSWORD`, and optionally `TEAMS_WEBHOOK_URL` when prompted. Render generates `COOKIE_SECRET` automatically.

The Admin page at `/admin` can export a selected month's event history as CSV and archive/reset monthly Overall, Auto, and Manual counters. Current Load and active vehicle assignments are never reset.

For persistent storage on a free deployment, create a free Supabase project, copy its PostgreSQL connection string, and set it as Render's `DATABASE_URL`. Use the Supabase transaction pooler connection string when direct database connections are not available. The application uses local SQLite only when `DATABASE_URL` is absent.

## Backup

Stop the app briefly and copy `vehicle_assignments.db` to your normal backed-up company storage. Avoid placing the live database itself on a synchronized OneDrive/SharePoint folder.
