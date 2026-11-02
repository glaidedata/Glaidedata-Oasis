# Netcup deployment: manual migration phase

This uses the existing release build/test workflow and the existing tag-over-SSH
deployment interface. The netcup Bash script adapts the Hetzner script supplied
by the administrator; it does not install Oasis or run Ansible provisioning.

**Automatic releases still deploy to Hetzner.** The reusable workflow has no
netcup target input in this phase. Only a manual dispatch of the deployment
workflow can select netcup. Keep the Hetzner script, environment and credentials
unchanged. Do not rerun a release build to test netcup deployment.

All server commands below are for a separately approved installation/test window.
They have not been executed as part of this implementation.

## Prerequisites before installation or container recreation

- Verify netcup is `159.195.131.244`, running project `glaide_oasis_staging` under
  `/opt/nomad/glaide-oasis-staging`, with four workers and healthy dependencies.
  RabbitMQ, Elasticsearch, MongoDB, PostgreSQL, Temporal, app and NORTH define
  Docker healthchecks. Worker, proxy and optional logtransfer do not: check
  their running state, then verify processing and proxy behavior separately.
- Review the installed Compose and NOMAD YAML against the Ansible working files.
  Preserve manual changes. The script supports their existing indentation and
  unquoted image scalars; it refuses missing, duplicate or unexpected image
  references rather than guessing. It changes only the five Compose image fields
  and the Jupyter tool image under `plugins.entry_points.options` in NOMAD YAML.
- Verify current, restorable backups of the databases, file store, configuration
  and private secrets. The script saves configuration/image metadata, **not data
  backups**. Review release compatibility; an image downgrade cannot undo schema
  changes. Confirm enough disk and RAM for pulling and starting both images.
- **Protect NORTH state before the first recreation.** The netcup administrator
  confirmed that `/app/jupyterhub.sqlite` and `/app/jupyterhub_cookie_secret`
  are in NORTH's nonpersistent container layer. Complete a separately reviewed,
  one-time server preparation that preserves both files and any SQLite sidecars;
  verify persistence and a restorable backup before enabling deployment. Keep
  the existing crypt key, service token and authentication settings. Do not
  initialize replacement Hub state. This preparation is outside this deployment
  implementation.
- Keep `/etc/nomad-oasis-staging/{nomad.env,db.env}` unchanged, root-owned and
  mode 0600. Preserve all named volumes, `/srv/nomad/.volumes/fs` (including NORTH
  users), authentication settings, Docker group access and the existing network.
- Schedule downtime, finish/drain processing, and have users save and stop their
  notebooks through NORTH. Existing notebook containers do not acquire the new
  Jupyter image automatically. Four running workers alone do not prove processing
  works or that peak workloads fit in RAM.

In the later server-preparation phase, an administrator can use these **read-only**
commands to identify the running container and examine the relevant paths without
printing secret contents:

```bash
dc=(sudo docker compose --project-directory /opt/nomad/glaide-oasis-staging
    -p glaide_oasis_staging --env-file /etc/nomad-oasis-staging/db.env
    -f /opt/nomad/glaide-oasis-staging/docker-compose.staging.yaml)
north_id=$("${dc[@]}" ps -q north)
test -n "$north_id" || { echo 'NORTH is not running' >&2; exit 1; }
sudo docker inspect --format 'workdir={{.Config.WorkingDir}} mounts={{json .Mounts}}' "$north_id"
sudo docker exec "$north_id" sh -c '
  pwd
  for path in /app/jupyterhub.sqlite /app/jupyterhub_cookie_secret; do
    if [ -e "$path" ]; then stat -c "%n owner=%u mode=%a" "$path";
    else printf "%s absent\n" "$path"; fi
  done
  for key in JPY_COOKIE_SECRET JUPYTERHUB_CRYPT_KEY; do
    if printenv "$key" >/dev/null; then printf "%s is set\n" "$key";
    else printf "%s is absent\n" "$key"; fi
  done
'
```

These commands identify likely defaults and mounts; they do not prove the Hub's
effective database path or that a file has a restorable copy. Privately review
the installed Hub configuration/overrides and match the actual database and
cookie-secret locations to a persistent mount or separately preserved setting.
Check backups and permissions without printing key values. If the state is only
inside the container, stop; this deployment must not recreate NORTH yet.

Only after that review or a separately approved persistence correction, record
the evidence privately and create the explicit prerequisite file on netcup:

```sh
printf '%s\n' 'glaide_oasis_staging:north-state-persistent' |
  sudo tee /etc/nomad-oasis-staging/north-state-verified >/dev/null
sudo chown root:root /etc/nomad-oasis-staging/north-state-verified
sudo chmod 0600 /etc/nomad-oasis-staging/north-state-verified
```

This file records an administrator's completed review; its presence does **not**
prove persistence. Do not create it from the example alone. Remove it and repeat
the review whenever Hub storage/configuration changes. The deployment script
refuses to proceed without it.

## One-time restricted SSH setup

Use a dedicated `deploy-netcup` account and key, separate from Hetzner and the
administrator accounts. No Docker-group membership or general sudo permission.
The account needs a shell for the forced command, but no password login. Keep
its home, `.ssh`, and `authorized_keys` root-owned and not writable by this user.
Check existing SSH allowlists before installing the key; retain administrator
access. Obtain and verify the netcup host public key through a trusted channel.

Install the reviewed script through administrator access, from the supplied
source checkout/copy (never fetch changing source during a deployment):

```sh
sudo install -o root -g root -m 0755 scripts/deploy-oasis-netcup.sh \
  /usr/local/sbin/deploy-oasis-netcup.sh
```

Keep `/usr/local/sbin`, the stack/config directories and the private secrets
directory root-owned and not writable by the deployment account. Ensure Bash,
Docker Compose v2 (including `--wait`), curl, awk, flock and ip are available.
For private GHCR packages, provision root's read-only registry access separately;
do not send registry credentials in the SSH command.

Authorized-key entry (replace only the public key):

```text
restrict,command="sudo -n /usr/local/sbin/deploy-oasis-netcup.sh \"$SSH_ORIGINAL_COMMAND\"" ssh-ed25519 PUBLIC_KEY
```

Unlike the original unquoted argument expansion, this passes the complete request
as one argument. The script permits only `vX.Y.Z`; no `main`, whitespace, options
or shell expressions. Do not use `eval` or `bash -c` on the requested tag.

Create `/etc/sudoers.d/deploy-netcup` with `visudo`, root-owned mode 0440:

```sudoers
deploy-netcup ALL=(root) NOPASSWD: /usr/local/sbin/deploy-oasis-netcup.sh *
```

The script independently validates argument count and contents. Keep sudo's
environment reset; do not grant SETENV or preserve shell startup variables.
Validate the sudoers file with `visudo -cf` before testing.

## GitHub environment

Create `glaidedata-oasis-netcup-staging` with environment-scoped secrets:

| Secret | Value |
| --- | --- |
| `NETCUP_DEPLOY_HOST` | `159.195.131.244` |
| `NETCUP_DEPLOY_USER` | `deploy-netcup` |
| `NETCUP_DEPLOY_SSH_KEY` | Dedicated netcup private key |
| `NETCUP_DEPLOY_KNOWN_HOSTS` | Verified known_hosts line for `159.195.131.244` |

Do not define these names as shared repository/organization fallbacks. The legacy
`DEPLOY_*` secrets and `glaidedata-oasis-prod` environment remain unchanged.
Restrict deployment refs and enable environment review where available. Do not
approve an unreviewed workflow ref. The netcup job requires strict host checking;
an unverified runtime `ssh-keyscan` is not a substitute.

## Manual v1.2.1 test

After the workflow is available on the default branch and the selected code ref
is approved, dispatch **Deploy oasis image to VM** with:

```text
target = netcup
mode   = tag
tag    = v1.2.1
```

Select the reviewed workflow branch, not the old `v1.2.1` tag as the workflow ref.
GitHub verifies the published release; the remote script pulls both images before
changing configuration. The script validates the current/candidate Compose,
saves prior files and running image identities, and recreates only NORTH, four
workers, app and already-running logtransfer. It uses a host lock and allows
600 seconds per service wait. nginx is tested/reloaded and HTTPS alive/GUI checks
use the real hostname with local address resolution and certificate validation.

The script uses release tags, as Hetzner does. Tags can be republished. For this
rehearsal compare `requested-images.txt` with the original build's manifest-list
digests (an image ID is a different kind of digest):

```text
app:     sha256:7dc9e8fed6b844d7f320596b8465e3d87e4369c9472caf1158d316fa3b54f697
jupyter: sha256:b75520bbc3dd6a83fae06a179ba859888568acff1d231451a8031a61868a3f2b
```

Before and after the test, record the database container IDs, mounted volume
names, four worker IDs/count, running image IDs and private secret checksums.
Check that databases were not recreated and non-image settings/secrets did not
change. Verify login/authorization, representative upload processing/download,
search, a newly launched Jupyter session, persistent notebook files, Voilà and
WebSockets. Inspect memory, restart/OOM state and worker activity. Do not treat
the Actions green result as proof of authenticated end-to-end functionality.

Do not rerun Ansible steps 02 or 04 as an upgrade: they can restore the v1.2.1
bootstrap files. Reconcile any future provisioning changes with the deployed
configuration first. No DNS, certificate or production-domain changes belong to
this phase. The legacy Hetzner NOMAD Jupyter `:main` setting is left untouched;
netcup updates its effective tool image alongside Compose.

## Failure and manual rollback

SSH propagates script failures to Actions. A timeout/disconnect may leave a
partial deployment: inspect the host and lock before retrying. No automatic
rollback is attempted. The printed root-only directory under
`/etc/nomad-oasis-staging/deploy-backups/` contains both prior configuration files,
running container image IDs/repository digests, the old Jupyter image reference
and its local image identity when available,
and, after successful pulls, the requested image identities. These backups are
not pruned by the script. The initial Ansible start playbook explicitly pulls
Jupyter if Compose skips its zero-replica service, but later local image cleanup
could remove that image. The script records `locally_available=no` and continues
the upgrade if the old image is missing. In that case, rollback is **not ready**
until an administrator recovers and verifies the previous Jupyter image.

For an approved rollback, first finish/stop conflicting deployment activity and
acquire the same lock in an administrator root shell. Check schema compatibility
and confirm prior images remain available. Restore the saved Compose and NOMAD
files with their permissions while holding the lock. If a tag moved, use the recorded immutable image
references in all affected image fields, including NOMAD Jupyter, instead of
pulling that tag again. Do not restore databases merely to undo image selection.

In the root shell, set `backup` to the reviewed backup directory, then:

```bash
exec 9>/etc/nomad-oasis-staging/deploy.lock
flock -n 9 || exit 1
: "${backup:?Set backup to the reviewed deployment backup directory}"
cp -p "$backup/docker-compose.staging.yaml" /opt/nomad/glaide-oasis-staging/docker-compose.staging.yaml
cp -p "$backup/nomad.staging.yaml" /opt/nomad/glaide-oasis-staging/configs/nomad.staging.yaml
# If prior tags moved, replace restored image scalars with recorded immutable refs here.
dc=(docker compose --project-directory /opt/nomad/glaide-oasis-staging
    -p glaide_oasis_staging --env-file /etc/nomad-oasis-staging/db.env
    -f /opt/nomad/glaide-oasis-staging/docker-compose.staging.yaml)
"${dc[@]}" config -q
"${dc[@]}" up -d --no-deps --force-recreate --no-build --pull never --wait --wait-timeout 600 north
"${dc[@]}" up -d --no-deps --force-recreate --no-build --pull never --scale worker=4 --wait --wait-timeout 600 worker
"${dc[@]}" up -d --no-deps --force-recreate --no-build --pull never --wait --wait-timeout 600 app
# Recreate logtransfer with --profile with_logtransfer only if it was active.
"${dc[@]}" exec -T proxy nginx -t
"${dc[@]}" exec -T proxy nginx -s reload
```

Repeat the HTTPS and authenticated verification above. A database migration
requires its own reviewed recovery procedure. Do not use stack shutdown, volume
removal, orphan removal or image pruning for deployment or rollback.

## Later automatic switch (not implemented)

After migration acceptance, review adding a `target` input to `workflow_call`
and setting the existing `docker-publish.yml` deployment call to netcup, with the
appropriate production environment/hostname checks. Retain one deployment call.
Historical workflow reruns retain old code: separately disable their ability to
deploy to Hetzner at final cutover, while retaining its configuration for recovery.

## Local validation only

```sh
bash -n scripts/deploy-oasis-netcup.sh
python3 tests/test_netcup_deploy.py
git diff --check
# If installed:
shellcheck scripts/deploy-oasis-netcup.sh
actionlint .github/workflows/release-tag-build-and-deploy.yml
```
