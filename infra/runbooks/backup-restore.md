# Backup and restore

Two layers protect the data:

| Layer | What | Where | Schedule |
|---|---|---|---|
| Database dump | `pg_dump --format=custom` of the `quantfolio` database | `/var/backups/quantfolio/quantfolio-<UTC stamp>.dump` on the app LXC (111) | Nightly 02:15 (`/etc/cron.d/quantfolio-backup`), and before every `quantfolio-update-app` |
| Container snapshot | `vzdump` of every LXC (zstd, snapshot mode) | Proxmox Backup Server storage `proxmox-backups` (10.0.0.12:8007) | Nightly 03:00 (`/etc/pve/jobs.cfg`) |

The dump is the one to restore application data from; the container snapshot
brings back a whole LXC (OS, packages, `.env`). Dumps older than 14 days are
deleted by the nightly run. Dumps sit on the same host as the database, so the
container snapshot on the backup server is the only off-host copy.

The daily self-check (`python -m app.foundation.self_check`) alerts on Telegram
when the last good dump is older than 36 hours, when the backup server does not
answer, or when the nightly run failed. It reads
`/var/lib/quantfolio-backup/status.json`, which `backup-db.sh` writes after
every run.

Until 2026-10-14 root's crontab called a dev-era `backup.sh` that was not
executable and expected the database on localhost; no nightly dump was ever
written, only the pre-update ones. `update-app.sh` now installs the cron file
and removes that line.

## Take a backup now

```bash
# On the app LXC (111), as root
bash /opt/quantfolio/infra/scripts/backup-db.sh
cat /var/lib/quantfolio-backup/status.json
```

## List backups

```bash
ls -lt /var/backups/quantfolio/ | head
tail -n 20 /var/log/quantfolio-backup.log
```

## Check a dump (restore into a scratch database)

Do this once a month; a backup that was never restored is a hope, not a backup.

```bash
# On the DB LXC (110): create the scratch database once
su postgres -c "createdb -O quantfolio_user quantfolio_restored"

# On the app LXC (111)
bash /opt/quantfolio/infra/scripts/restore-db.sh /var/backups/quantfolio/quantfolio-<STAMP>.dump
# prints the alembic revision and the user count of the restored copy

# On the DB LXC (110): drop it again
su postgres -c "dropdb quantfolio_restored"
```

## Restore the live database

Only after data loss or a failed migration that cannot be rolled forward.

```bash
# On the app LXC (111)
systemctl stop quantfolio-worker quantfolio-api
bash /opt/quantfolio/infra/scripts/backup-db.sh          # keep what is there now
bash /opt/quantfolio/infra/scripts/restore-db.sh /var/backups/quantfolio/quantfolio-<STAMP>.dump quantfolio --replace-live
cd /opt/quantfolio/backend && set -a && . ../.env && set +a
.venv/bin/alembic current                                  # must match the code's head, else check out that revision
systemctl start quantfolio-api quantfolio-worker
```

If the code on disk is newer than the dump's alembic revision, run
`.venv/bin/alembic upgrade head` before starting the services.

## Restore a whole container

From the Proxmox web UI (Datacenter → Backup storage → `proxmox-backups`) or:

```bash
# On the Proxmox host
pvesm list proxmox-backups | grep vzdump-lxc-111
pct stop 111
pct restore 111 <volume id from the list> --force
pct start 111
```

## When the backup server is down

`pvesm status` shows `proxmox-backups` as `inactive` and the nightly vzdump
task fails with "No route to host". Start the backup server (or fix its
address in Datacenter → Storage); the next nightly run picks it up. Until
then only the local dumps exist.
