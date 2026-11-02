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
    local file backup candidate_compose candidate_nomad logtransfer_id active_logtransfer=0
    local -a dc=(docker compose
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
    "${dc[@]}" -f "$compose_file" config -q
    logtransfer_id=$("${dc[@]}" -f "$compose_file" ps --status running -q logtransfer) ||
        die 'Could not determine whether logtransfer is active.'
    [[ -z $logtransfer_id ]] || active_logtransfer=1

    # Keep candidates beside the originals so relative Compose bind paths resolve identically.
    candidate_compose=$(mktemp "$stack/.compose-deploy.XXXXXXXX.yaml")
    trap 'rm -f "${candidate_compose:-}" "${candidate_nomad:-}"' EXIT
    candidate_nomad=$(mktemp "$stack/configs/.nomad-deploy.XXXXXXXX.yaml")
    rewrite_images compose "$compose_file" "$tag" > "$candidate_compose" || die 'Unexpected Compose image layout.'
    rewrite_images nomad "$nomad_file" "$tag" > "$candidate_nomad" || die 'Unexpected NOMAD Jupyter image layout.'
    "${dc[@]}" -f "$candidate_compose" config -q
    chmod --reference="$compose_file" "$candidate_compose"
    chmod --reference="$nomad_file" "$candidate_nomad"

    mkdir -p "$private/deploy-backups"
    backup=$(mktemp -d "$private/deploy-backups/$(date -u +%Y%m%dT%H%M%SZ).XXXXXXXX")
    cp -p "$compose_file" "$backup/docker-compose.staging.yaml"
    cp -p "$nomad_file" "$backup/nomad.staging.yaml"
    echo "==> Deploying $tag; rollback files: $backup"
    docker pull --platform linux/amd64 "$repo:$tag"
    docker pull --platform linux/amd64 "$repo/jupyter:$tag"

    # Only image scalars change; do not recreate data services or volumes.
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
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
    main "$@"
fi
