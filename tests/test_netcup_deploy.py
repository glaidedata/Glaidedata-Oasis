"""Offline checks for the restricted netcup deployment script."""

import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
import gzip


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/deploy-oasis-netcup.sh"
COMPOSE = """name: glaide_oasis_staging
services:
  mongo:
    image: mongo:5
    volumes:
      - mongo:/data/db
""" + "".join(
    f"  {service}:\n    image: ghcr.io/glaidedata/glaidedata-oasis{suffix}:v1.2.1\n"
    for service, suffix in [
        ("app", ""), ("worker", ""), ("north", ""), ("logtransfer", ""),
        ("jupyterlab_image", "/jupyter"),
    ]
) + """volumes:
  mongo:
    name: glaide_oasis_staging_mongo
"""
NO_LOGTRANSFER_COMPOSE = COMPOSE.replace(
    "  logtransfer:\n    image: ghcr.io/glaidedata/glaidedata-oasis:v1.2.1\n", ""
)
NOMAD = """auth:
  require_authentication: true
plugins:
  entry_points:
    options:
      nomad_north_jupyter.north_tools:jupyter:
        north_tool:
          image: ghcr.io/glaidedata/glaidedata-oasis/jupyter:v1.2.1
north:
  enabled: true
"""

MOCKS = r"""
docker() {
    printf '%s\n' "$*" >> "$CALLS"
    case "$*" in
        'inspect --format {{range .Mounts}}'* )
            [[ ${FAIL_MOUNT_INSPECT:-0} == 0 ]] || return 1
            [[ ${FAIL_MOUNT_AFTER_BACKUP:-0} == 0 ||
               $(grep -c 'inspect --format {{range .Mounts}}' "$CALLS") -le 3 ]] || return 1
            case "$*" in
                *'/app/jupyterhub.sqlite'*) echo "$VOLUMES/jupyterhub/jupyterhub.sqlite" ;;
                *'/app/jupyterhub_cookie_secret'*)
                    [[ ${WRONG_NORTH_MOUNT:-0} == 0 ]] && echo "$VOLUMES/jupyterhub/jupyterhub_cookie_secret" || echo "$VOLUMES/elsewhere" ;;
                *'/app/.volumes/fs'*) echo "$VOLUMES/fs" ;;
            esac ;;
        'inspect --format {{.State.Health.Status}} w'*) return 99 ;;
        'inspect --format {{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}} temporal1')
            echo healthy ;;
        'inspect --format {{.State.Health.Status}} '*)
            [[ ${FAIL_HEALTH:-0} == 0 ]] && echo healthy || echo unhealthy ;;
        'ps -q --filter name=^/nomad_oasis_north-')
            [[ ${FAIL_SESSIONS_QUERY:-0} == 0 ]] || return 1
            [[ ${ACTIVE_SESSIONS:-0} == 0 ]] || printf '%s\n' notebook1 ;;
        *' config --services')
            [[ ${FAIL_SERVICES_QUERY:-0} == 0 ]] || return 1
            printf '%s\n' app worker north jupyterlab_image
            [[ ${HAS_LOGTRANSFER:-0} == 0 ]] || printf '%s\n' logtransfer ;;
        *' config -q') ;;
        *' ps --status running -q logtransfer')
            [[ ${FAIL_LOGTRANSFER_QUERY:-0} == 0 ]] || return 1
            [[ ${ACTIVE_LOGTRANSFER:-0} == 0 ]] || printf '%s\n' logtransfer1 ;;
        *' ps --status running -q app') [[ ${RUNNING_APP:-1} == 0 ]] || echo app1 ;;
        *' ps --status running -q worker')
            if [[ ${RUNNING_WORKER:-1} != 0 ]]; then
                printf '%s\n' w1 w2 w3
                [[ ${LOST_WORKER_AFTER_RESTART:-0} == 1 && ${RESTORED:-0} == 1 ]] || echo w4
            fi ;;
        *' ps --status running -q north') [[ ${RUNNING_NORTH:-1} == 0 ]] || echo north1 ;;
        *' ps --status running -q temporal') [[ ${RUNNING_TEMPORAL:-1} == 0 ]] || echo temporal1 ;;
        *' exec -T worker python -c '*|*' exec -T north python -c '*)
            [[ ${FAIL_PROCESSING:-0} == 0 ]] || return 1 ;;
        *' exec -T mongo mongodump --archive --gzip')
            [[ ${FAIL_MONGO_DUMP:-0} == 0 ]] || return 1
            cat "$MOCK_MONGO_ARCHIVE" ;;
        *' exec -T postgresql sh -c '*)
            [[ ${FAIL_POSTGRES_DUMP:-0} == 0 ]] || return 1
            printf '%s\n' '-- PostgreSQL database cluster dump' ;;
        *' stop app') RUNNING_APP=0 ;;
        *' stop worker') RUNNING_WORKER=0 ;;
        *' stop north') RUNNING_NORTH=0 ;;
        *' stop temporal') RUNNING_TEMPORAL=0 ;;
        *' stop app worker north')
            [[ ${FAIL_POST_CUTOVER_STOP:-0} == 0 ]] || return 1
            RUNNING_APP=0; RUNNING_WORKER=0; RUNNING_NORTH=0 ;;
        *' start temporal') RUNNING_TEMPORAL=1 ;;
        *' start north worker app')
            [[ ${FAIL_RESTORE:-0} == 0 ]] || return 1
            RUNNING_APP=1; RUNNING_WORKER=1; RUNNING_NORTH=1; RESTORED=1 ;;
        'pull '*) [[ ${FAIL_PULL:-0} == 0 || $* != *'/jupyter:'* ]] ;;
        *' up -d '*)
            if [[ ${NEW_STARTED:-0} == 0 ]]; then
                [[ ${RUNNING_APP:-1} == 0 && ${RUNNING_WORKER:-1} == 0 &&
                   ${RUNNING_NORTH:-1} == 0 ]] || return 98
                NEW_STARTED=1
            fi
            [[ ${FAIL_UP_SERVICE:-} != "${@: -1}" ]] || return 1
            case ${@: -1} in
                north) RUNNING_NORTH=1 ;;
                worker) RUNNING_WORKER=1 ;;
                app) RUNNING_APP=1 ;;
            esac ;;
        *' exec -T proxy nginx -t'|*' exec -T proxy nginx -s reload') ;;
        *) echo "Unexpected Docker call: $*" >&2; return 99 ;;
    esac
}
curl() { printf 'curl %s\n' "$*" >> "$CALLS"; }
stat() {
    case "$2" in
        %u) echo 0 ;;
        %u:%a) echo 0:600 ;;
        %a) [[ $3 == "$BACKUP_ROOT" ]] && echo 700 || echo 644 ;;
        *) return 99 ;;
    esac
}
main "$TAG"
"""


class NetcupDeployTests(unittest.TestCase):
    @staticmethod
    def expected_compose(source):
        for service, suffix in [
            ("app", ""), ("worker", ""), ("north", ""),
            ("jupyterlab_image", "/jupyter"),
        ]:
            old = f"  {service}:\n    image: ghcr.io/glaidedata/glaidedata-oasis{suffix}:v1.2.1"
            new = old.replace(":v1.2.1", ":v1.2.2")
            source = source.replace(old, new)
        return source

    def rewrite(self, kind, content):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.yaml"
            source.write_text(content)
            return subprocess.run(
                ["bash", "-c", 'source "$1"; rewrite_images "$2" "$3" v1.2.2',
                 "test", str(SCRIPT), kind, str(source)],
                capture_output=True, text=True,
            )

    def test_only_expected_image_fields_change(self):
        for kind, source in [
            ("compose", COMPOSE), ("compose", NO_LOGTRANSFER_COMPOSE), ("nomad", NOMAD)
        ]:
            with self.subTest(kind=kind):
                result = self.rewrite(kind, source)
                self.assertEqual(result.returncode, 0, result.stderr)
                expected = (self.expected_compose(source) if kind == "compose"
                            else source.replace(":v1.2.1", ":v1.2.2"))
                self.assertEqual(result.stdout, expected)

    def test_unexpected_image_layout_is_rejected(self):
        for kind, source in [
            ("compose", COMPOSE.replace("  north:\n", "  other:\n")),
            ("compose", COMPOSE.replace("glaidedata-oasis:v1.2.1", "other:v1.2.1")),
            ("nomad", NOMAD.replace("north_tool:", "other_tool:")),
        ]:
            with self.subTest(kind=kind):
                self.assertNotEqual(self.rewrite(kind, source).returncode, 0)

    def test_invalid_ssh_arguments_are_rejected(self):
        for args in [[], ["main"], ["v1.2.1;id"], ["v1.2.1", "extra"]]:
            with self.subTest(args=args):
                result = subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Usage:", result.stderr)

    def deploy(self, *, compose_content=COMPOSE, marker=True, fail_pull=False,
               fail_services_query=False, fail_logtransfer_query=False,
               active_logtransfer=False, active_sessions=False, fail_sessions_query=False,
               fail_processing=False, fail_mongo_dump=False, fail_postgres_dump=False,
               fail_restore=False,
               lost_worker_after_restart=False, wrong_north_mount=False,
               fail_mount_inspect=False, fail_mount_after_backup=False,
               fail_up_service="", fail_post_cutover_stop=False):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            stack, private = base / "stack", base / "private"
            (stack / "configs").mkdir(parents=True)
            private.mkdir()
            backup_root = base / "backups"
            backup_root.mkdir()
            backup_root.chmod(0o700)
            volumes = base / "volumes"
            (volumes / "fs").mkdir(parents=True)
            (volumes / "fs" / "upload.txt").write_text("example upload")
            (volumes / "jupyterhub").mkdir()
            (volumes / "jupyterhub/jupyterhub.sqlite").write_bytes(b"sqlite fixture")
            (volumes / "jupyterhub/jupyterhub_cookie_secret").write_bytes(b"cookie fixture")
            mongo_archive = base / "mongo.archive.gz"
            mongo_archive.write_bytes(gzip.compress(b"mongodump fixture"))
            compose = stack / "docker-compose.staging.yaml"
            nomad = stack / "configs/nomad.staging.yaml"
            compose.write_text(compose_content)
            nomad.write_text(NOMAD)
            for name in ["db.env", "nomad.env"]:
                (private / name).write_text("EXISTING_SECRET=unchanged\n")
            if marker:
                (private / "north-state-verified").write_text(
                    "glaide_oasis_staging:north-state-persistent\n"
                )
            source = SCRIPT.read_text().replace("attempt<120", "attempt<1").replace(
                "sleep 10", ":"
            ).replace(
                "[[ $EUID == 0 ]] || die 'Run through the restricted sudo rule.'",
                ": # root check stubbed in offline test",
            ).replace("/opt/nomad/glaide-oasis-staging", str(stack)).replace(
                "/etc/nomad-oasis-staging", str(private)
            ).replace("/var/backups/glaide-oasis", str(backup_root)).replace(
                "/srv/nomad/.volumes", str(volumes)
            )
            fixture = base / "deploy.sh"
            fixture.write_text(source)
            calls = base / "calls"
            result = subprocess.run(
                ["bash", "-c", 'source "$1"\n' + MOCKS, "test", str(fixture)],
                capture_output=True, text=True,
                env={**os.environ, "CALLS": str(calls), "TAG": "v1.2.2",
                     "BACKUP_ROOT": str(backup_root),
                     "VOLUMES": str(volumes),
                     "MOCK_MONGO_ARCHIVE": str(mongo_archive),
                     "FAIL_PULL": "1" if fail_pull else "0",
                     "ACTIVE_SESSIONS": "1" if active_sessions else "0",
                     "FAIL_SESSIONS_QUERY": "1" if fail_sessions_query else "0",
                     "FAIL_PROCESSING": "1" if fail_processing else "0",
                     "FAIL_MONGO_DUMP": "1" if fail_mongo_dump else "0",
                     "FAIL_POSTGRES_DUMP": "1" if fail_postgres_dump else "0",
                     "FAIL_RESTORE": "1" if fail_restore else "0",
                     "LOST_WORKER_AFTER_RESTART": "1" if lost_worker_after_restart else "0",
                     "WRONG_NORTH_MOUNT": "1" if wrong_north_mount else "0",
                     "FAIL_MOUNT_INSPECT": "1" if fail_mount_inspect else "0",
                     "FAIL_MOUNT_AFTER_BACKUP": "1" if fail_mount_after_backup else "0",
                     "FAIL_UP_SERVICE": fail_up_service,
                     "FAIL_POST_CUTOVER_STOP": "1" if fail_post_cutover_stop else "0",
                     "FAIL_SERVICES_QUERY": "1" if fail_services_query else "0",
                     "FAIL_LOGTRANSFER_QUERY": "1" if fail_logtransfer_query else "0",
                     "ACTIVE_LOGTRANSFER": "1" if active_logtransfer else "0",
                     "HAS_LOGTRANSFER": "1" if compose_content == COMPOSE else "0"},
            )
            backups = list(backup_root.iterdir())
            backup_files = set(p.name for p in backups[0].iterdir()) if backups else set()
            return (result, calls.read_text() if calls.exists() else "",
                    compose.read_text(), nomad.read_text(), backup_files)

    def test_north_guard_blocks_recreation(self):
        result, calls, compose, nomad, _ = self.deploy(marker=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("north-state-verified", result.stderr)
        self.assertEqual(calls, "")
        self.assertEqual((compose, nomad), (COMPOSE, NOMAD))

    def test_pull_failure_keeps_active_configuration(self):
        result, calls, compose, nomad, backup_files = self.deploy(fail_pull=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("pull --platform linux/amd64 ghcr.io/glaidedata/glaidedata-oasis/jupyter:v1.2.2", calls)
        self.assertNotIn(" up -d ", calls)
        self.assertEqual((compose, nomad), (COMPOSE, NOMAD))
        self.assertNotIn("stop app", calls)
        self.assertFalse(backup_files)

    def test_unknown_logtransfer_state_blocks_deployment(self):
        result, calls, compose, nomad, _ = self.deploy(fail_logtransfer_query=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Could not determine whether logtransfer is running", result.stderr)
        self.assertNotIn("pull --platform", calls)
        self.assertEqual((compose, nomad), (COMPOSE, NOMAD))

    def test_unknown_service_list_blocks_deployment(self):
        result, calls, compose, nomad, _ = self.deploy(fail_services_query=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Could not determine the defined Compose services", result.stderr)
        self.assertNotIn("pull --platform", calls)
        self.assertEqual((compose, nomad), (COMPOSE, NOMAD))

    def test_success_recreates_only_application_services(self):
        for source in (COMPOSE, NO_LOGTRANSFER_COMPOSE):
            with self.subTest(logtransfer_defined=source == COMPOSE):
                result, calls, compose, nomad, backup_files = self.deploy(compose_content=source)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(backup_files, {
                    "mongo.archive.gz", "postgresql.sql", "nomad-files.tar.gz",
                    "config.tar.gz", "SHA256SUMS", "mongodump.log", "pg_dumpall.log",
                    "files-tar.log", "config-tar.log",
                })
                self.assertIn("stop app", calls)
                self.assertIn("stop worker", calls)
                self.assertIn("stop north", calls)
                self.assertIn("stop temporal", calls)
                self.assertIn("start temporal", calls)
                self.assertNotIn("start north worker app", calls)
                self.assertNotIn("inspect --format {{.State.Health.Status}} app1", calls)
                self.assertNotIn("inspect --format {{.State.Health.Status}} north1", calls)
                self.assertNotIn("inspect --format {{.State.Health.Status}} w", calls)
                self.assertLess(calls.index("stop temporal"), calls.index("exec -T postgresql"))
                self.assertLess(calls.index("pull --platform"), calls.index("stop app"))
                self.assertLess(calls.index("stop north"), calls.index("start temporal"))
                self.assertLess(calls.index("start temporal"), calls.index(" up -d "))
                self.assertEqual(calls.count("inspect --format {{range .Mounts}}"), 6)
                updates = [line for line in calls.splitlines() if " up -d " in line]
                self.assertEqual([line.split()[-1] for line in updates], ["north", "worker", "app"])
                self.assertIn("--scale worker=4", updates[1])
                self.assertTrue(all("--no-deps" in line for line in updates))
                self.assertTrue(all("--wait --wait-timeout 1200" in line for line in updates))
                self.assertLess(calls.index("/jupyter:v1.2.2"), calls.index(" up -d "))
                if source == NO_LOGTRANSFER_COMPOSE:
                    self.assertNotIn("ps --status running -q logtransfer", calls)
                self.assertNotIn(" down", calls)
                self.assertNotIn("prune", calls)
                self.assertEqual(compose, self.expected_compose(source))
                self.assertEqual(nomad, NOMAD.replace(":v1.2.1", ":v1.2.2"))
                for path in ("alive", "gui/"):
                    self.assertIn(
                        f"--resolve nomad-oasis.glaidedata.com:443:127.0.0.1 "
                        f"https://nomad-oasis.glaidedata.com/nomad-oasis/{path}", calls
                    )

    def test_active_logtransfer_blocks_before_changes(self):
        result, calls, compose, nomad, _ = self.deploy(active_logtransfer=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Logtransfer is running", result.stderr)
        self.assertNotIn("pull --platform", calls)
        self.assertNotIn(" up -d ", calls)
        self.assertEqual((compose, nomad), (COMPOSE, NOMAD))

    def test_backup_failure_restores_original_services_and_blocks_cutover(self):
        result, calls, compose, nomad, _ = self.deploy(fail_mongo_dump=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("MongoDB dump failed", result.stderr)
        self.assertIn("stop app", calls)
        self.assertIn("stop worker", calls)
        self.assertIn("stop north", calls)
        self.assertIn("stop temporal", calls)
        self.assertIn("start temporal", calls)
        self.assertIn("start north worker app", calls)
        self.assertIn("pull --platform", calls)
        self.assertNotIn(" up -d ", calls)
        self.assertEqual((compose, nomad), (COMPOSE, NOMAD))

    def test_wrong_or_unknown_north_mount_blocks_backup(self):
        for options in ({"wrong_north_mount": True}, {"fail_mount_inspect": True}):
            with self.subTest(options=options):
                result, calls, compose, nomad, backup_files = self.deploy(**options)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Running NORTH mounts", result.stderr)
                self.assertNotIn("stop app", calls)
                self.assertIn("pull --platform", calls)
                self.assertFalse(backup_files)
                self.assertEqual((compose, nomad), (COMPOSE, NOMAD))

    def test_failed_post_backup_mount_check_recovers_original_containers(self):
        result, calls, compose, nomad, backup_files = self.deploy(fail_mount_after_backup=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Running NORTH mounts changed", result.stderr)
        self.assertIn("original configuration and containers restored", result.stderr)
        self.assertIn("start north worker app", calls)
        self.assertNotIn(" up -d ", calls)
        self.assertIn("SHA256SUMS", backup_files)
        self.assertEqual((compose, nomad), (COMPOSE, NOMAD))

    def test_failed_new_worker_stops_nomad_without_old_restart(self):
        result, calls, compose, nomad, backup_files = self.deploy(fail_up_service="worker")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Post-cutover failure", result.stderr)
        self.assertIn("Data backup:", result.stderr)
        self.assertIn("configuration backup:", result.stderr)
        self.assertIn("stop app worker north", calls)
        self.assertNotIn("start north worker app", calls)
        self.assertIn("SHA256SUMS", backup_files)
        self.assertEqual(compose, self.expected_compose(COMPOSE))
        self.assertEqual(nomad, NOMAD.replace(":v1.2.1", ":v1.2.2"))

    def test_failed_post_cutover_stop_reports_unverified_state(self):
        result, calls, _, _, _ = self.deploy(
            fail_up_service="worker", fail_post_cutover_stop=True
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("could not be verified stopped", result.stderr)
        self.assertIn("Administrator intervention required immediately", result.stderr)
        self.assertNotIn("start north worker app", calls)

    def test_failed_service_recovery_blocks_deployment(self):
        result, calls, compose, nomad, _ = self.deploy(fail_mongo_dump=True, fail_restore=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("administrator recovery required", result.stderr)
        self.assertIn("pull --platform", calls)
        self.assertEqual((compose, nomad), (COMPOSE, NOMAD))

    def test_postgresql_dump_failure_restores_temporal_and_blocks_cutover(self):
        result, calls, compose, nomad, _ = self.deploy(fail_postgres_dump=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("PostgreSQL dump failed", result.stderr)
        self.assertLess(calls.index("stop temporal"), calls.index("exec -T postgresql"))
        self.assertLess(calls.index("exec -T postgresql"), calls.index("start temporal"))
        self.assertIn("pull --platform", calls)
        self.assertEqual((compose, nomad), (COMPOSE, NOMAD))

    def test_missing_original_worker_blocks_deployment_without_health_probe(self):
        result, calls, compose, nomad, _ = self.deploy(
            fail_mongo_dump=True, lost_worker_after_restart=True
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("administrator recovery required", result.stderr)
        self.assertNotIn("inspect --format {{.State.Health.Status}} w", calls)
        self.assertIn("pull --platform", calls)
        self.assertEqual((compose, nomad), (COMPOSE, NOMAD))

    def test_processing_or_north_session_blocks_backup(self):
        for kwargs in ({"fail_processing": True}, {"active_sessions": True},
                       {"fail_sessions_query": True}):
            with self.subTest(kwargs=kwargs):
                result, calls, compose, nomad, backup_files = self.deploy(**kwargs)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("stop app", calls)
                self.assertIn("pull --platform", calls)
                self.assertFalse(backup_files)
                self.assertEqual((compose, nomad), (COMPOSE, NOMAD))

    def test_actions_deployment_job_allows_backup_and_startup(self):
        workflow = (SCRIPT.parents[1] / ".github/workflows/release-tag-build-and-deploy.yml").read_text()
        self.assertEqual(re.findall(r"(?m)^    timeout-minutes: (\d+)$", workflow), ["90"])


if __name__ == "__main__":
    unittest.main()
