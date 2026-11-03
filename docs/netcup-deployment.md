# Netcup deployment (manual migration phase)

Published releases still run the existing build/test workflow, publish both
images, and deploy automatically to Hetzner. The netcup target is selected only
by a manual dispatch of `release-tag-build-and-deploy.yml`. Both targets send one
tag over SSH to a restricted server-side Bash script. This does not rerun
Ansible or change DNS, certificates, secrets, volumes, or database services.

## Prerequisite: protect NORTH state

**Do not deploy, including a same-version v1.2.1 test, until this is done.**
The current NORTH container stores `/app/jupyterhub.sqlite` and
`/app/jupyterhub_cookie_secret` outside its persistent mounts. A separately
reviewed, one-time server preparation must back up and preserve their existing
bytes, including SQLite journal/WAL sidecars, in persistent storage. Coordinate
active Jupyter sessions and verify login, Jupyter and Voilà after that change.
Do not initialize replacement Hub state. Keep the existing authentication,
encryption key, service token, network and file storage. Verify a restorable
backup and persistence across NORTH recreation before creating the marker:

```sh
printf '%s\n' 'glaide_oasis_staging:north-state-persistent' |
  sudo tee /etc/nomad-oasis-staging/north-state-verified >/dev/null
sudo chown root:root /etc/nomad-oasis-staging/north-state-verified
sudo chmod 0600 /etc/nomad-oasis-staging/north-state-verified
```

The marker records an administrator's verification; it is not itself proof of
persistence. The deployment script refuses to recreate NORTH without it.

## One-time installation

After reviewing the installed staging Compose and NOMAD files, install the
reviewed script as root-owned mode 0755 at
`/usr/local/sbin/deploy-oasis-netcup.sh`. Keep the stack and private directories
inaccessible to the restricted deploy account. Retain the existing project
`glaide_oasis_staging`, Compose file
`/opt/nomad/glaide-oasis-staging/docker-compose.staging.yaml`, NOMAD file
`configs/nomad.staging.yaml`, and `/etc/nomad-oasis-staging/{db,nomad}.env`.
The script expects their current unquoted image-field layout. Ensure root can
read both GHCR images and that Bash, Docker Compose v2, awk, flock and curl are
installed.

```sh
sudo install -o root -g root -m 0755 scripts/deploy-oasis-netcup.sh \
  /usr/local/sbin/deploy-oasis-netcup.sh
```

Give a dedicated `deploy-netcup` SSH account a separate key and only this
forced command in its root-controlled `authorized_keys`:

```text
restrict,command="sudo -n /usr/local/sbin/deploy-oasis-netcup.sh \"$SSH_ORIGINAL_COMMAND\"" ssh-ed25519 PUBLIC_KEY
```

Allow only `/usr/local/sbin/deploy-oasis-netcup.sh *` through a root-owned,
mode-0440 sudoers file; validate it with `visudo -cf`. Do not grant general
Docker or sudo access. The script accepts exactly one `vX.Y.Z` tag.

Create GitHub environment `glaidedata-oasis-netcup-staging` with its own
environment-scoped `DEPLOY_HOST` (`159.195.131.244`), `DEPLOY_USER`,
`DEPLOY_SSH_KEY`, and `NETCUP_DEPLOY_KNOWN_HOSTS` secrets. Obtain the host key
through a trusted channel. Keep the existing `glaidedata-oasis-prod`
environment and Hetzner credentials unchanged. The workflow rejects a netcup
host other than the fixed IP and requires a matching pinned host key.

## Manual test and verification

After the workflow is available on the default branch and NORTH persistence is
verified, dispatch **Deploy oasis image to VM** on the reviewed workflow ref:

```text
target = netcup
mode   = tag
tag    = v1.2.1
```

The workflow checks that the tag is a published release. The script saves the
current Compose and NOMAD files under
`/etc/nomad-oasis-staging/deploy-backups/`, pulls both images before changing
either file, then recreates NORTH, four workers, app, and logtransfer only if
already active. It never runs Compose `down`, removes volumes, or prunes images.
It validates Compose, waits for services, reloads nginx and checks the HTTPS
alive/GUI routes. An SSH/script failure makes the Actions job fail.

Before testing, confirm restorable backups and that the previous app and
Jupyter images are available for rollback; record their image IDs/digests
because tags can be republished. Record database container IDs and volume
names. Afterward, confirm those IDs and
volumes are unchanged, four workers are running, and authentication, an upload,
search, a new Jupyter session and Voilà work. Existing notebook containers do
not switch images automatically.

## Rollback

If deployment or checks fail, inspect the host before retrying. The script
does not attempt automatic rollback. In a root shell, set `backup` to the
printed directory and hold the deployment lock while restoring configuration:

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
"${dc[@]}" up -d --no-deps --force-recreate --pull never --wait --wait-timeout 600 north
"${dc[@]}" up -d --no-deps --force-recreate --pull never --scale worker=4 --wait --wait-timeout 600 worker
"${dc[@]}" up -d --no-deps --force-recreate --pull never --wait --wait-timeout 600 app
"${dc[@]}" exec -T proxy nginx -t && "${dc[@]}" exec -T proxy nginx -s reload
```

If logtransfer was active, recreate it with its profile too. Test HTTPS and
the authenticated functions again. Use the recorded prior image digests in all
six image fields if a tag has moved; restoring YAML alone cannot undo an
application database migration.
Do not stop or recreate database services, remove volumes, or run Ansible's
initial start playbook as a rollback.

Automatic release deployment remains on Hetzner until a separately reviewed
cutover changes the existing release workflow call.
