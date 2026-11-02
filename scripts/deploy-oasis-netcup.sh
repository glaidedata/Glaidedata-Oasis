#!/usr/bin/env bash
# Netcup adaptation of /srv/nomad/deploy_oasis.sh: same one-tag SSH interface.
# Installation, NORTH persistence prerequisite and rollback: docs/netcup-deployment.md
set -euo pipefail

die() { echo "ERROR: $*" >&2; exit 1; }

# Only support the reviewed staging YAML layout. Unexpected edits fail closed;
# unrelated lines (including authentication settings) are copied byte-for-byte.
rewrite_images() {
    local kind=$1 source=$2 tag=$3
    awk -v kind="$kind" -v tag="$tag" '
        BEGIN {
            repo = "ghcr.io/glaidedata/glaidedata-oasis"
            split("app worker north logtransfer jupyterlab_image", services, " ")
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
    local project=glaide_oasis_staging domain=nomad-oasis-test.glaidedata.com
    local repo=ghcr.io/glaidedata/glaidedata-oasis
    local compose_file=$stack/docker-compose.staging.yaml nomad_file=$stack/configs/nomad.staging.yaml
    local file service id old_jupyter backup active_logtransfer=0
    local -a ids dc=(docker --host unix:///var/run/docker.sock compose
        --project-directory "$stack" -p "$project" --env-file "$private/db.env")
    cd "$stack"
    exec 9>"$private/deploy.lock"
    flock -n 9 || die 'Another netcup deployment is running.'
    ip -4 -o address show | grep -q ' inet 159\.195\.131\.244/' || die 'Not the expected netcup host.'
    for file in "$compose_file" "$nomad_file"; do
        [[ -f $file && ! -L $file && $(stat -c %u "$file") == 0 ]] || die "Unexpected config file: $file"
        (( (8#$(stat -c %a "$file") & 8#022) == 0 )) || die "Writable config file: $file"
    done
    for file in "$private/nomad.env" "$private/db.env" "$private/north-state-verified"; do
        [[ -f $file && ! -L $file && $(stat -c %u:%a "$file") == 0:600 ]] ||
            die "Required root-owned mode-0600 file missing or unsafe: $file (see runbook)"
    done
    grep -qx 'glaide_oasis_staging:north-state-persistent' "$private/north-state-verified" ||
        die 'Verify and preserve JupyterHub state before allowing NORTH recreation.'
    "${dc[@]}" -f "$compose_file" config -q
    for service in mongo elastic rabbitmq postgresql temporal app north; do
        id=$("${dc[@]}" -f "$compose_file" ps -q "$service")
        [[ -n $id && $id != *$'\n'* ]] || die "Expected one running $service container."
        [[ $(docker inspect -f '{{.State.Health.Status}}' "$id") == healthy ]] || die "$service is not healthy."
    done
    # nginx has no Docker healthcheck; Compose can only confirm it is running.
    id=$("${dc[@]}" -f "$compose_file" ps -q proxy)
    [[ -n $id && $id != *$'\n'* ]] || die 'Expected one running proxy container.'
    mapfile -t ids < <("${dc[@]}" -f "$compose_file" ps --status running -q worker)
    [[ ${#ids[@]} == 4 ]] || die 'Expected four running workers.'
    id=$("${dc[@]}" -f "$compose_file" ps --status running -q logtransfer)
    [[ -z $id ]] || active_logtransfer=1

    # Keep candidates beside the originals so relative Compose bind paths resolve identically.
    candidate_compose=$(mktemp "$stack/.compose-deploy.XXXXXXXX.yaml")
    candidate_nomad=$(mktemp "$stack/configs/.nomad-deploy.XXXXXXXX.yaml")
    trap 'rc=$?; rm -f "$candidate_compose" "$candidate_nomad"; if (( rc != 0 )); then echo "Deployment failed; inspect current state and use the runbook rollback. No automatic rollback was attempted." >&2; fi' EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    rewrite_images compose "$compose_file" "$tag" > "$candidate_compose" || die 'Unexpected Compose image layout.'
    rewrite_images nomad "$nomad_file" "$tag" > "$candidate_nomad" || die 'Unexpected NOMAD Jupyter image layout.'
    "${dc[@]}" -f "$candidate_compose" config -q
    chmod --reference="$compose_file" "$candidate_compose"
    chmod --reference="$nomad_file" "$candidate_nomad"

    # Capture the running artifacts BEFORE pulling: a tag might have been republished.
    mkdir -p "$private/deploy-backups"
    backup=$(mktemp -d "$private/deploy-backups/$(date -u +%Y%m%dT%H%M%SZ).XXXXXXXX")
    cp -p "$compose_file" "$backup/docker-compose.staging.yaml"
    cp -p "$nomad_file" "$backup/nomad.staging.yaml"
    mapfile -t ids < <("${dc[@]}" -f "$compose_file" ps -q)
    docker inspect -f '{{.Name}} {{.Config.Image}} {{.Image}}' "${ids[@]}" > "$backup/containers.txt"
    for id in "${ids[@]}"; do
        docker image inspect -f '{{.Id}} {{json .RepoDigests}}' "$(docker inspect -f '{{.Image}}' "$id")"
    done > "$backup/image-digests.txt"
    old_jupyter=$(awk '$1 == "image:" && $2 ~ /^ghcr.io\/glaidedata\/glaidedata-oasis\/jupyter(:|@sha256:)/ {print $2}' "$nomad_file")
    printf 'previous_ref=%s\n' "$old_jupyter" > "$backup/jupyter-image.txt"
    if docker image inspect -f '{{.Id}} {{json .RepoDigests}}' "$old_jupyter" >> "$backup/jupyter-image.txt" 2>/dev/null; then
        echo 'locally_available=yes' >> "$backup/jupyter-image.txt"
    else
        echo 'locally_available=no' >> "$backup/jupyter-image.txt"
        echo "WARNING: Previous Jupyter image $old_jupyter is not local; rollback requires recovering it first." >&2
    fi
    echo "==> Deploying $tag; rollback files: $backup"
    docker pull --platform linux/amd64 "$repo:$tag"
    docker pull --platform linux/amd64 "$repo/jupyter:$tag"
    docker image inspect -f '{{.Id}} {{json .RepoDigests}}' "$repo:$tag" "$repo/jupyter:$tag" > "$backup/requested-images.txt"

    # Only image scalars change. Secret files, database services and storage are untouched.
    mv "$candidate_compose" "$compose_file"
    mv "$candidate_nomad" "$nomad_file"
    local -a up=("${dc[@]}" -f "$compose_file" up -d --no-deps --force-recreate
        --no-build --pull never --wait --wait-timeout 600)
    "${up[@]}" north
    "${up[@]}" --scale worker=4 worker
    "${up[@]}" app
    if (( active_logtransfer )); then
        "${dc[@]}" -f "$compose_file" --profile with_logtransfer up -d --no-deps --force-recreate \
            --no-build --pull never --wait --wait-timeout 600 logtransfer
    fi
    mapfile -t ids < <("${dc[@]}" -f "$compose_file" ps --status running -q worker)
    [[ ${#ids[@]} == 4 ]] || die 'Expected four running workers after deployment.'
    "${dc[@]}" -f "$compose_file" exec -T proxy nginx -t
    "${dc[@]}" -f "$compose_file" exec -T proxy nginx -s reload
    for path in alive gui/; do
        curl --fail --silent --show-error --connect-timeout 5 --max-time 20 \
            --retry 12 --retry-delay 5 --retry-connrefused \
            --resolve "$domain:443:127.0.0.1" "https://$domain/nomad-oasis/$path" >/dev/null
    done
    echo "==> Done: $tag. Complete authenticated upload and NORTH smoke checks in the runbook."
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
    main "$@"
fi
