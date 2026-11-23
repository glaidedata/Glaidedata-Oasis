# Netcup production deployment

Production runs at `nomad-oasis.glaidedata.com` on netcup
(`159.195.131.244`); the starting version for this change is `v1.2.1`.
Published releases run the existing build/test workflow, publish both images,
and deploy automatically
through `.github/workflows/release-tag-build-and-deploy.yml`. The production
GitHub Actions deployment credentials target netcup. The published-release path
sends one version tag over SSH to `/usr/local/sbin/deploy-oasis-netcup.sh`, which
is a server-installed copy of `scripts/deploy-oasis-netcup.sh` in this repository.
The workflow can also be dispatched manually using its existing inputs; use
`mode=tag` with an explicit version for netcup. `main` is incompatible with the
installed script's version-only SSH interface, and `auto` may resolve to `main`.

## Prerequisite: protect NORTH state

The script requires `/etc/nomad-oasis-staging/north-state-verified` before it
recreates NORTH. This marker must only exist after an administrator has verified
that JupyterHub state (`/app/jupyterhub.sqlite`, its journal/WAL sidecars, and
`/app/jupyterhub_cookie_secret`) is preserved across NORTH recreation, with a
restorable backup. Coordinate active Jupyter sessions and verify login,
Jupyter and Voilà. Do not initialize replacement Hub state or change the
existing authentication, encryption key, service token, network or file
storage. If verification is still needed, create the marker only afterward:

```sh
printf '%s\n' 'glaide_oasis_staging:north-state-persistent' |
  sudo tee /etc/nomad-oasis-staging/north-state-verified >/dev/null
sudo chown root:root /etc/nomad-oasis-staging/north-state-verified
sudo chmod 0600 /etc/nomad-oasis-staging/north-state-verified
```

The marker records an administrator's verification; it is not itself proof of
persistence. The deployment script refuses to recreate NORTH without it.

## Prerequisite: review legacy logtransfer

NOMAD 2.0.1 no longer provides `python -m nomad.cli admin run logtransfer`.
Before publishing v1.3.0, an administrator must check the server-owned Compose
file and whether its logtransfer service is running. The deployment script
aborts before pulling images or changing configuration if logtransfer is
running or its state cannot be determined. A defined but stopped logtransfer
service is left unchanged; a Compose file without it is also supported. The
script never stops, migrates, or recreates logtransfer. Any change to the
server-owned Compose file or cleanup of the obsolete service requires separate
administrator review and explicit approval.

## Synchronize the server script

After this change is reviewed and available in an administrator's trusted
checkout on the server, compare it with the installed script and install the
reviewed version as root-owned mode 0755. This repository change alone does
not update the installed copy. Run these commands from the checkout on netcup,
with administrative access, during a separately scheduled server change:

```sh
sha256sum scripts/deploy-oasis-netcup.sh /usr/local/sbin/deploy-oasis-netcup.sh
bash -n scripts/deploy-oasis-netcup.sh
sudo install -o root -g root -m 0755 scripts/deploy-oasis-netcup.sh \
  /usr/local/sbin/deploy-oasis-netcup.sh
cmp scripts/deploy-oasis-netcup.sh /usr/local/sbin/deploy-oasis-netcup.sh
```

Keep the stack and private directories inaccessible to the restricted deploy
account. Retain the existing project `glaide_oasis_staging`, Compose file
`/opt/nomad/glaide-oasis-staging/docker-compose.staging.yaml`, NOMAD file
`configs/nomad.staging.yaml`, and `/etc/nomad-oasis-staging/{db,nomad}.env`.
The script expects the current unquoted app, worker, NORTH, and Jupyter image
fields, with or without a legacy logtransfer service. Ensure root can
read both GHCR images and that Bash, Docker Compose v2, awk, flock and curl are
installed.

The restricted SSH account must continue to invoke only this forced command
through its root-controlled `authorized_keys`:

```text
restrict,command="sudo -n /usr/local/sbin/deploy-oasis-netcup.sh \"$SSH_ORIGINAL_COMMAND\"" ssh-ed25519 PUBLIC_KEY
```

Allow only `/usr/local/sbin/deploy-oasis-netcup.sh *` through the existing
restricted sudo rule. The script accepts exactly one `vX.Y.Z` tag. Do not grant
general Docker or sudo access.

The existing workflow uses environment `glaidedata-oasis-prod` and its
`DEPLOY_HOST`, `DEPLOY_USER`, and `DEPLOY_SSH_KEY` secrets. Do not alter those
credentials as part of this timeout change.

## Server-side application health check

The production Compose file above is maintained on netcup and is not tracked
in this repository. In its `app` health check, change only `start_period` from
`10s` to `15m` (15 minutes). Preserve the installed health-check `test`,
`interval: 10s`, `timeout: 10s`, and `retries: 30`. Validate the resulting file
with `docker compose config -q` using the same project directory, project name,
environment file, and Compose file arguments as the deployment script. Review
the diff on the server before the next deployment. This server-side change is
required for the approximately 13-minute observed initialization and cannot be
applied by merging this repository change.

## Deployment and verification

Published releases deploy automatically after the build and tests pass. The
deployment job has a 90-minute limit to allow for image pulls, the backup and
the new app startup. Measure a complete backup and deployment in a separately
approved maintenance rehearsal before publishing; an Actions timeout fails the
job but may leave the deployment script running on the server and must not be
treated as proof of recovery. The installed script must be synchronized and the
Compose health check updated before relying on that limit. The script
waits up to 1200 seconds per Compose `up --wait` call; this is not a guarantee
that the whole Actions job will finish within 90 minutes. To deploy an existing
published image through the current manual workflow, use:

```text
mode   = tag
tag    = v1.2.1
```

After pulling both images, the script requires no active NOMAD processing or
running NORTH notebook containers, stops app, workers, NORTH and Temporal
without recreating them, and writes a root-only timestamped backup under
`/var/backups/glaide-oasis/`. It contains MongoDB and PostgreSQL logical dumps,
NOMAD files, JupyterHub state and server-owned deployment configuration. The
script checks archive integrity, required files, nonempty dumps and checksums,
then restarts and verifies only Temporal. The old app, workers and NORTH remain
stopped until new-version startup. On a failure before that startup, the script
restores and verifies the original containers. A backup or recovery failure
blocks deployment; inspect the server before retrying. The
checks are not a restore test. Test restoration only into isolated databases
and storage, never over production. Backups are retained until an administrator
reviews them; the script does not delete old backups.

The script also saves the current Compose and NOMAD files under
`/etc/nomad-oasis-staging/deploy-backups/` before maintenance. It verifies the
running NORTH mounts match the backed-up JupyterHub and NOMAD paths and confirms
the old app, workers and NORTH are stopped before starting any new-version
service. It recreates NORTH, four workers, and app. It leaves any
stopped logtransfer service and its image reference unchanged. It never runs Compose
`down`, removes volumes, or prunes images.
It validates Compose, waits for services, reloads nginx and checks the HTTPS
alive/GUI routes. An SSH/script failure makes the Actions job fail.

Before testing, confirm restorable backups and that the previous app and
Jupyter images are available for rollback; record their image IDs/digests
because tags can be republished. Record database container IDs and volume
names. Afterward, confirm those IDs and volumes are unchanged, four workers are
running, and authentication, an upload, search, a new Jupyter session and Voilà
work. Existing notebook containers do not switch images automatically.

## Rollback

If deployment or checks fail, inspect the host before retrying. For failures
after backup, the error identifies the data backup under
`/var/backups/glaide-oasis/` and the saved configuration under
`/etc/nomad-oasis-staging/deploy-backups/`.

- **Before any new-version service starts:** the script restores the saved
  configuration and starts the original container identities. Verify its
  recovery message and service health; if verification failed, recover manually
  before retrying.
- **After a new-version service may have started, with no data migration
  confirmed:** the script stops app, workers and NORTH, but does not start old
  containers. Confirm all writers are stopped, inspect logs and database state,
  then restore the saved configuration and previous images under a maintenance
  window. Start the old services only after confirming their data compatibility.
- **After a possible database or data migration:** keep NOMAD writers stopped.
  Assess the migration and test recovery using isolated copies. If old-version
  compatibility cannot be established, restoring the full pre-deployment MongoDB,
  PostgreSQL, NOMAD files and JupyterHub state may be required as one consistent
  set. An administrator must approve and perform any production restore; the
  script never restores databases automatically.

The following manual configuration and image rollback applies only after the
data-compatibility decision above. In a root shell, set `backup` to the printed
configuration-backup directory and hold the deployment lock:

```bash
exec 9>/etc/nomad-oasis-staging/deploy.lock
flock -n 9 || exit 1
: "${backup:?Set backup to the reviewed backup directory}"
cp -p "$backup/docker-compose.staging.yaml" /opt/nomad/glaide-oasis-staging/docker-compose.staging.yaml
cp -p "$backup/nomad.staging.yaml" /opt/nomad/glaide-oasis-staging/configs/nomad.staging.yaml
dc=(docker compose --project-directory /opt/nomad/glaide-oasis-staging
    -p glaide_oasis_staging --env-file /etc/nomad-oasis-staging/db.env
    -f /opt/nomad/glaide-oasis-staging/docker-compose.staging.yaml)
"${dc[@]}" config -q
"${dc[@]}" up -d --no-deps --force-recreate --pull never --wait --wait-timeout 1200 north
"${dc[@]}" up -d --no-deps --force-recreate --pull never --scale worker=4 --wait --wait-timeout 1200 worker
"${dc[@]}" up -d --no-deps --force-recreate --pull never --wait --wait-timeout 1200 app
"${dc[@]}" exec -T proxy nginx -t && "${dc[@]}" exec -T proxy nginx -s reload
```

Test HTTPS and the authenticated functions again. Handle any legacy
logtransfer restoration separately, after reviewing its old image and command.
Use the recorded prior image digests in the app, worker, NORTH, and Jupyter
image fields if a tag has moved; restoring YAML alone cannot undo an
application database migration.
Do not stop or recreate database services, remove volumes, or run Ansible's
initial start playbook as a rollback.
