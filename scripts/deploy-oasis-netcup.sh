#!/usr/bin/env bash
# Netcup adaptation of /srv/nomad/deploy_oasis.sh: same one-tag SSH interface.
# Installation, NORTH persistence prerequisite and rollback: docs/netcup-deployment.md
set -euo pipefail

die() { echo "ERROR: $*" >&2; exit 1; }

running_ids() {
    "${dc[@]}" -f "$compose_file" ps --status running -q "$1" | sort
}

verify_north_mounts() {
    local destination source actual
    for destination in /app/jupyterhub.sqlite /app/jupyterhub_cookie_secret /app/.volumes/fs; do
        case $destination in
            /app/.volumes/fs) source=/srv/nomad/.volumes/fs ;;
            *) source=/srv/nomad/.volumes/jupyterhub/${destination##*/} ;;
        esac
        actual=$(docker inspect --format \
            "{{range .Mounts}}{{if eq .Destination \"$destination\"}}{{println .Source}}{{end}}{{end}}" \
            "$original_north") || return 1
        [[ $actual == "$source" ]] || return 1
    done
}

nomad_services_stopped() {
    local app_ids worker_ids north_ids
    app_ids=$(running_ids app) || return 1
    worker_ids=$(running_ids worker) || return 1
    north_ids=$(running_ids north) || return 1
    [[ -z $app_ids && -z $worker_ids && -z $north_ids ]]
}

restore_backup_services() {
    "${dc[@]}" -f "$compose_file" start temporal || return 1
    local attempt app_health north_health temporal_state=
    for ((attempt=0; attempt<120; attempt++)); do
        if [[ $(running_ids temporal) == "$original_temporal" ]]; then
            temporal_state=$(docker inspect --format \
                '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' \
                "$original_temporal") || return 1
            [[ $temporal_state == healthy || $temporal_state == running ]] && break
        fi
        sleep 10
    done
    [[ $temporal_state == healthy || $temporal_state == running ]] || return 1
    [[ ${1:-} == temporal_only ]] && return 0
    "${dc[@]}" -f "$compose_file" start north worker app || return 1
    for ((attempt=0; attempt<120; attempt++)); do
        if [[ $(running_ids temporal) == "$original_temporal" &&
              $(running_ids north) == "$original_north" &&
              $(running_ids worker) == "$original_workers" &&
              $(running_ids app) == "$original_app" ]]; then
            temporal_state=$(docker inspect --format \
                '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' \
                "$original_temporal") || return 1
            north_health=$(docker inspect --format '{{.State.Health.Status}}' "$original_north") || return 1
            app_health=$(docker inspect --format '{{.State.Health.Status}}' "$original_app") || return 1
            [[ $temporal_state == healthy || $temporal_state == running ]] &&
                [[ $north_health == healthy && $app_health == healthy ]] && return 0
        fi
        sleep 10
    done
    return 1
}

on_exit() {
    local status=$1
    if [[ ${backup_services_stopped:-0} == 1 ]]; then
        if ! restore_backup_services; then
            echo 'ERROR: Backup interrupted and original services could not be verified running; administrator recovery required.' >&2
            status=1
        fi
    elif [[ ${deployment_phase:-} == pre_cutover ]]; then
        if ! cp -p "$backup/docker-compose.staging.yaml" "$compose_file" ||
           ! cp -p "$backup/nomad.staging.yaml" "$nomad_file" ||
           ! restore_backup_services; then
            echo 'ERROR: Pre-cutover failure; original configuration or containers could not be verified. Administrator recovery required.' >&2
        else
            echo 'ERROR: Pre-cutover failure; original configuration and containers restored.' >&2
        fi
        echo "ERROR: Data backup: $data_backup; configuration backup: $backup" >&2
        status=1
    elif [[ ${deployment_phase:-} == post_cutover ]]; then
        if ! "${dc[@]}" -f "$compose_file" stop app worker north ||
           ! nomad_services_stopped; then
            echo 'ERROR: Post-cutover failure; NOMAD services could not be verified stopped. Administrator intervention required immediately.' >&2
        else
            echo 'ERROR: Post-cutover failure; NOMAD app, workers and NORTH are stopped. Do not restart the old version until data migration is assessed.' >&2
        fi
        echo "ERROR: Data backup: $data_backup; configuration backup: $backup. Administrator recovery required." >&2
        status=1
    fi
    rm -f "${candidate_compose:-}" "${candidate_nomad:-}"
    exit "$status"
}

no_active_processing() {
    # Query NOMAD's own Mongo-backed processing state through the installed image.
    "${dc[@]}" -f "$compose_file" exec -T "$1" python -c '
from nomad import infrastructure
from nomad.processing.base import ProcessStatus
from nomad.processing.data import Entry, Upload
infrastructure.setup_mongo()
statuses = ProcessStatus.STATUSES_PROCESSING
if Upload.objects(process_status__in=statuses).first() or Entry.objects(process_status__in=statuses).first():
    raise SystemExit(1)
' >/dev/null 2>&1
}

pre_deployment_backup() {
    local root=/var/backups/glaide-oasis directory notebook_sessions stopped_app stopped_workers stopped_north stopped_temporal
    [[ -d $root && ! -L $root && $(stat -c %u "$root") == 0 ]] ||
        die 'Backup root is missing or unsafe.'
    (( (8#$(stat -c %a "$root") & 8#077) == 0 )) || die 'Backup root must be root-only.'
    [[ -d /srv/nomad/.volumes/fs && ! -L /srv/nomad/.volumes/fs &&
       -d /srv/nomad/.volumes/jupyterhub && ! -L /srv/nomad/.volumes/jupyterhub &&
       -s /srv/nomad/.volumes/jupyterhub/jupyterhub.sqlite &&
       ! -L /srv/nomad/.volumes/jupyterhub/jupyterhub.sqlite &&
       -s /srv/nomad/.volumes/jupyterhub/jupyterhub_cookie_secret &&
       ! -L /srv/nomad/.volumes/jupyterhub/jupyterhub_cookie_secret ]] ||
        die 'Required NOMAD or JupyterHub backup source is missing.'

    original_app=$(running_ids app) || die 'Cannot determine running app state.'
    original_workers=$(running_ids worker) || die 'Cannot determine running worker state.'
    original_north=$(running_ids north) || die 'Cannot determine running NORTH state.'
    original_temporal=$(running_ids temporal) || die 'Cannot determine running Temporal state.'
    [[ -n $original_app && -n $original_workers && -n $original_north &&
       -n $original_temporal &&
       $(wc -l <<< "$original_app") == 1 &&
       $(wc -l <<< "$original_workers") == 4 &&
       $(wc -l <<< "$original_north") == 1 &&
       $(wc -l <<< "$original_temporal") == 1 ]] ||
        die 'Expected one app, four workers, one NORTH and one Temporal before backup.'
    verify_north_mounts || die 'Running NORTH mounts do not match the JupyterHub and NOMAD backup sources.'
    notebook_sessions=$(docker ps -q --filter 'name=^/nomad_oasis_north-') ||
        die 'Cannot determine running NORTH sessions.'
    [[ -z $notebook_sessions ]] || die 'NORTH sessions are running; postpone deployment.'
    no_active_processing worker || die 'Processing is active or its state cannot be checked.'

    # Stop public writes first, then recheck for work admitted during preflight.
    backup_services_stopped=1
    "${dc[@]}" -f "$compose_file" stop app || die 'Could not stop app for backup.'
    no_active_processing worker || die 'Processing started during maintenance entry or cannot be checked.'
    "${dc[@]}" -f "$compose_file" stop worker || die 'Could not stop workers for backup.'
    no_active_processing north || die 'Processing remains queued or its state cannot be checked.'
    notebook_sessions=$(docker ps -q --filter 'name=^/nomad_oasis_north-') ||
        die 'Cannot recheck running NORTH sessions.'
    [[ -z $notebook_sessions ]] || die 'NORTH session started during maintenance entry.'
    "${dc[@]}" -f "$compose_file" stop north || die 'Could not stop NORTH for backup.'
    "${dc[@]}" -f "$compose_file" stop temporal || die 'Could not stop Temporal for backup.'
    stopped_app=$(running_ids app) || die 'Cannot verify stopped app.'
    stopped_workers=$(running_ids worker) || die 'Cannot verify stopped workers.'
    stopped_north=$(running_ids north) || die 'Cannot verify stopped NORTH.'
    stopped_temporal=$(running_ids temporal) || die 'Cannot verify stopped Temporal.'
    [[ -z $stopped_app && -z $stopped_workers && -z $stopped_north &&
       -z $stopped_temporal ]] ||
        die 'Application services did not stop cleanly.'

    directory=$(mktemp -d "$root/$(date -u +%Y%m%dT%H%M%SZ).XXXXXXXX") ||
        die 'Could not create backup directory.'
    data_backup=$directory
    echo "==> Creating pre-deployment backup: $directory"
    "${dc[@]}" -f "$compose_file" exec -T mongo mongodump --archive --gzip \
        > "$directory/mongo.archive.gz" 2> "$directory/mongodump.log" ||
        die 'MongoDB dump failed.'
    "${dc[@]}" -f "$compose_file" exec -T postgresql sh -c 'exec pg_dumpall -U "$POSTGRES_USER"' \
        > "$directory/postgresql.sql" 2> "$directory/pg_dumpall.log" ||
        die 'PostgreSQL dump failed.'
    tar -C /srv/nomad/.volumes -czf "$directory/nomad-files.tar.gz" fs jupyterhub \
        2> "$directory/files-tar.log" ||
        die 'NOMAD files or JupyterHub backup failed.'
    tar -C / -czf "$directory/config.tar.gz" \
        "${compose_file#/}" "${nomad_file#/}" "${stack#/}/configs" \
        "${private#/}/db.env" "${private#/}/nomad.env" "${private#/}/north-state-verified" \
        2> "$directory/config-tar.log" ||
        die 'Deployment configuration backup failed.'
    [[ -s $directory/mongo.archive.gz && -s $directory/postgresql.sql &&
       -s $directory/nomad-files.tar.gz && -s $directory/config.tar.gz ]] ||
        die 'Backup contains an empty dump or archive.'
    gzip -t "$directory/mongo.archive.gz" || die 'MongoDB archive integrity check failed.'
    tar -tzf "$directory/nomad-files.tar.gz" > "$directory/files.list" ||
        die 'NOMAD files archive integrity check failed.'
    grep -Fxq fs/ "$directory/files.list" &&
        grep -Fxq jupyterhub/jupyterhub.sqlite "$directory/files.list" &&
        grep -Fxq jupyterhub/jupyterhub_cookie_secret "$directory/files.list" ||
        die 'Required files are missing from the backup archive.'
    rm "$directory/files.list"
    tar -tzf "$directory/config.tar.gz" >/dev/null || die 'Configuration archive integrity check failed.'
    (cd "$directory" && sha256sum mongo.archive.gz postgresql.sql nomad-files.tar.gz config.tar.gz > SHA256SUMS &&
        sha256sum --check --status SHA256SUMS) || die 'Backup checksum verification failed.'
    restore_backup_services temporal_only || die 'Backup completed but Temporal could not be verified running.'
    nomad_services_stopped || die 'Old NOMAD services resumed after backup.'
    deployment_phase=pre_cutover
    backup_services_stopped=0
    echo "==> Verified pre-deployment backup: $directory"
}

# Only support the reviewed staging YAML layout. Unexpected edits fail closed;
# unrelated lines (including authentication settings) are copied byte-for-byte.
rewrite_images() {
    local kind=$1 source=$2 tag=$3
    awk -v kind="$kind" -v tag="$tag" '
        BEGIN {
            repo = "ghcr.io/glaidedata/glaidedata-oasis"
            split("app worker north jupyterlab_image", services, " ")
            for (i in services) wanted[services[i]] = 1
        }
        /^[^ #]/ { section = $1 }
        kind == "compose" && section == "services:" && /^  [a-z_]+:$/ {
            service = $1; sub(/:$/, "", service)
        }
        kind == "nomad" && /^  [^ ]/ { level2 = $1 }
        kind == "nomad" && /^    [^ ]/ { level4 = $1 }
        kind == "nomad" && /^      [^ ]/ { tool = $1 }
        kind == "nomad" && /^        [^ ]/ { option = $1 }
        {
            target = ""
            if (kind == "compose" && section == "services:" && (service in wanted) && /^    image:/)
                target = service
            if (kind == "nomad" && section == "plugins:" && level2 == "entry_points:" &&
                level4 == "options:" && tool == "nomad_north_jupyter.north_tools:jupyter:" &&
                option == "north_tool:" && /^          image:/)
                target = "jupyter"
            if (target != "") {
                image = repo ((target == "jupyter" || target == "jupyterlab_image") ? "/jupyter" : "")
                if (NF != 2 || (index($2, image ":") != 1 && index($2, image "@sha256:") != 1) || ++seen[target] != 1) {
                    print "Unexpected or duplicate image field: " target > "/dev/stderr"
                    exit 1
                }
                sub(/image:.*/, "image: " image ":" tag)
            }
            print
        }
        END {
            if (kind == "compose") {
                for (service in wanted) if (seen[service] != 1) exit 1
            } else if (seen["jupyter"] != 1) exit 1
        }
    ' "$source"
}

main() {
    [[ $# == 1 && $1 =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || die 'Usage: deploy-oasis-netcup.sh vX.Y.Z'
    [[ $EUID == 0 ]] || die 'Run through the restricted sudo rule.'
    export PATH=/usr/sbin:/usr/bin:/sbin:/bin
    export DOCKER_HOST=unix:///var/run/docker.sock
    unset DOCKER_CONTEXT COMPOSE_FILE COMPOSE_PROFILES COMPOSE_PROJECT_NAME
    umask 077
    local tag=$1 stack=/opt/nomad/glaide-oasis-staging private=/etc/nomad-oasis-staging
    local project=glaide_oasis_staging domain=nomad-oasis.glaidedata.com
    local repo=ghcr.io/glaidedata/glaidedata-oasis
    compose_file=$stack/docker-compose.staging.yaml nomad_file=$stack/configs/nomad.staging.yaml
    local file logtransfer_id services
    backup= candidate_compose= candidate_nomad= data_backup= deployment_phase=
    original_app= original_workers= original_north= original_temporal= backup_services_stopped=0
    dc=(docker compose
        --project-directory "$stack" -p "$project" --env-file "$private/db.env")
    cd "$stack"
    exec 9>"$private/deploy.lock"
    flock -n 9 || die 'Another netcup deployment is running.'
    for file in "$compose_file" "$nomad_file"; do
        [[ -f $file && ! -L $file && $(stat -c %u "$file") == 0 ]] || die "Unexpected config file: $file"
        (( (8#$(stat -c %a "$file") & 8#022) == 0 )) || die "Writable config file: $file"
    done
    for file in "$private/nomad.env" "$private/db.env" "$private/north-state-verified"; do
        [[ -f $file && ! -L $file && $(stat -c %u:%a "$file") == 0:600 ]] ||
            die "Required root-owned mode-0600 file missing or unsafe: $file"
    done
    grep -qx 'glaide_oasis_staging:north-state-persistent' "$private/north-state-verified" ||
        die 'Verify and preserve JupyterHub state before allowing NORTH recreation.'
    "${dc[@]}" -f "$compose_file" --profile '*' config -q
    services=$("${dc[@]}" -f "$compose_file" --profile '*' config --services) ||
        die 'Could not determine the defined Compose services.'
    if grep -Fxq logtransfer <<< "$services"; then
        logtransfer_id=$("${dc[@]}" -f "$compose_file" --profile '*' ps --status running -q logtransfer) ||
            die 'Could not determine whether logtransfer is running.'
        [[ -z $logtransfer_id ]] ||
            die 'Logtransfer is running; review and handle it separately before deploying NOMAD 2.0.1.'
    fi

    # Keep candidates beside the originals so relative Compose bind paths resolve identically.
    candidate_compose=$(mktemp "$stack/.compose-deploy.XXXXXXXX.yaml")
    trap 'on_exit $?' EXIT
    trap 'exit 1' HUP INT TERM
    candidate_nomad=$(mktemp "$stack/configs/.nomad-deploy.XXXXXXXX.yaml")
    rewrite_images compose "$compose_file" "$tag" > "$candidate_compose" || die 'Unexpected Compose image layout.'
    rewrite_images nomad "$nomad_file" "$tag" > "$candidate_nomad" || die 'Unexpected NOMAD Jupyter image layout.'
    "${dc[@]}" -f "$candidate_compose" --profile '*' config -q
    chmod --reference="$compose_file" "$candidate_compose"
    chmod --reference="$nomad_file" "$candidate_nomad"

    mkdir -p "$private/deploy-backups"
    backup=$(mktemp -d "$private/deploy-backups/$(date -u +%Y%m%dT%H%M%SZ).XXXXXXXX")
    cp -p "$compose_file" "$backup/docker-compose.staging.yaml"
    cp -p "$nomad_file" "$backup/nomad.staging.yaml"
    echo "==> Deploying $tag; rollback files: $backup"
    docker pull --platform linux/amd64 "$repo:$tag"
    docker pull --platform linux/amd64 "$repo/jupyter:$tag"

    pre_deployment_backup

    verify_north_mounts || die 'Running NORTH mounts changed before recreation.'
    nomad_services_stopped ||
        die 'Old NOMAD services did not all stop before cutover.'

    # Only application and Jupyter image scalars change; do not recreate data services or volumes.
    mv "$candidate_compose" "$compose_file"
    mv "$candidate_nomad" "$nomad_file"
    local -a up=("${dc[@]}" -f "$compose_file" up -d --no-deps --force-recreate
        --no-build --pull never --wait --wait-timeout 1200)
    deployment_phase=post_cutover
    "${up[@]}" north
    "${up[@]}" --scale worker=4 worker
    "${up[@]}" app
    [[ $("${dc[@]}" -f "$compose_file" ps --status running -q worker | wc -l) == 4 ]] ||
        die 'Expected four running workers after deployment.'
    "${dc[@]}" -f "$compose_file" exec -T proxy nginx -t
    "${dc[@]}" -f "$compose_file" exec -T proxy nginx -s reload
    for path in alive gui/; do
        curl --fail --silent --show-error --connect-timeout 5 --max-time 20 \
            --retry 12 --retry-delay 5 --retry-connrefused \
            --resolve "$domain:443:127.0.0.1" "https://$domain/nomad-oasis/$path" >/dev/null
    done
    echo "==> Done: $tag. Complete authenticated upload and NORTH smoke checks in the runbook."
    deployment_phase=complete
    rm -f "$candidate_compose" "$candidate_nomad"
    trap - EXIT HUP INT TERM
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
    main "$@"
fi
