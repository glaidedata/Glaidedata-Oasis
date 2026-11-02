"""Offline regression checks: no Docker daemon, SSH, root access or network.

The lifecycle fixture substitutes fixed host paths and the root-ownership probe
only in a temporary script copy. Docker/ip/curl are shell mocks, not executables.
"""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/deploy-oasis-netcup.sh"
COMPOSE = """name: glaide_oasis_staging
services:
  mongo:
    image: docker.io/mongo:5.0.6
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
NOMAD = """services:
  api_host: nomad-oasis-test.glaidedata.com
auth:
  require_authentication: true
  authorized_users:
    - existing-user
plugins:
  entry_points:
    options:
      nomad_north_jupyter.north_tools:jupyter:
        north_tool:
          image: ghcr.io/glaidedata/glaidedata-oasis/jupyter:v1.2.1
      another-plugin:
        keep: custom-setting
north:
  enabled: true
"""

MOCKS = r"""
docker() {
    printf '%s\n' "$*" >> "$CALLS"
    case "$*" in
        *' config -q') return 0 ;;
        *' ps --status running -q worker') printf '%s\n' w1 w2 w3 w4 ;;
        *' ps --status running -q logtransfer')
            [[ ${FAILURE:-} != logtransfer_status ]] || return 1
            [[ ${ACTIVE_LOGTRANSFER:-0} == 0 ]] || echo logtransfer ;;
        *' ps -q proxy') [[ ${FAILURE:-} == proxy_down ]] || echo proxy-container ;;
        *' ps -q mongo'|*' ps -q elastic'|*' ps -q rabbitmq'|*' ps -q postgresql'|*' ps -q temporal'|*' ps -q app'|*' ps -q north') echo container ;;
        *' ps -q') printf '%s\n' app north w1 w2 w3 w4 mongo ;;
        'inspect -f {{.State.Health.Status}} '*)
            if [[ ${FAILURE:-} == unhealthy ]]; then echo unhealthy; else echo healthy; fi ;;
        'inspect -f {{.Image}} '*) echo sha256:previous ;;
        'inspect -f {{.Name}} {{.Config.Image}} {{.Image}} '*) echo 'app previous:tag sha256:previous' ;;
        'image inspect '*)
            if [[ ${FAILURE:-} == old_jupyter_missing && $* == *'/jupyter:v1.2.1' ]]; then return 1; fi
            echo 'sha256:previous ["example@sha256:previous"]' ;;
        'pull '*)
            if [[ ${FAILURE:-} == pull && $* == *'/jupyter:'* ]]; then return 1; fi ;;
        *' up -d '*) [[ ${FAILURE:-} != recreate ]] ;;
        *' exec -T proxy nginx -t'|*' exec -T proxy nginx -s reload') return 0 ;;
        *) echo "Unexpected Docker call: $*" >&2; return 99 ;;
    esac
}
ip() { echo '2: eth0 inet 159.195.131.244/24'; }
curl() { printf 'curl %s\n' "$*" >> "$CALLS"; }
stat() {
    case "$2" in
        %u) echo 0 ;;
        %u:%a) echo 0:600 ;;
        %a) echo 644 ;;
        *) return 99 ;;
    esac
}
main "$TAG"
"""


class NetcupDeployTests(unittest.TestCase):
    def render(self, kind, content):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.yaml"
            source.write_text(content)
            return subprocess.run(
                ["bash", "-c", 'source "$1"; rewrite_images "$2" "$3" v1.2.2',
                 "test", str(SCRIPT), kind, str(source)],
                capture_output=True, text=True,
            )

    def test_only_reviewed_image_fields_change(self):
        for kind, source in [("compose", COMPOSE), ("nomad", NOMAD)]:
            with self.subTest(kind=kind):
                result = self.render(kind, source)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, source.replace(":v1.2.1", ":v1.2.2"))

    def test_unexpected_layouts_and_images_are_rejected(self):
        cases = [
            ("compose", COMPOSE.replace("  north:\n", "  unexpected:\n")),
            ("compose", COMPOSE.replace("glaidedata-oasis:v1.2.1", "other-app:v1.2.1")),
            ("compose", COMPOSE.replace("    image:", "      image:", 2)),
            ("compose", COMPOSE.replace("  app:\n", "  app:\n    image: duplicate:tag\n")),
            ("nomad", NOMAD.replace("north_tool:", "different_tool:")),
            ("nomad", NOMAD.replace("entry_points:", "wrong_path:")),
            ("nomad", NOMAD.replace("          image:", "          image: duplicate:tag\n          image:")),
        ]
        for kind, content in cases:
            with self.subTest(kind=kind, content=content):
                self.assertNotEqual(self.render(kind, content).returncode, 0)

    def test_deployment_after_digest_pinned_rollback(self):
        for kind, source in [("compose", COMPOSE), ("nomad", NOMAD)]:
            with self.subTest(kind=kind):
                result = self.render(kind, source.replace(":v1.2.1", "@sha256:" + "a" * 64))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, source.replace(":v1.2.1", ":v1.2.2"))

    def test_invalid_requests_fail_before_host_checks(self):
        for args in [[], ["main"], [""], ["v1.2.1;id"], ["v1.2.1\nid"],
                     ["v1.2.1 extra"], ["v1.2.1", "extra"], ["--help"]]:
            with self.subTest(args=args):
                result = subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Usage:", result.stderr)

    def lifecycle(self, *, failure="", marker=True, logtransfer=False):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            stack, private = base / "stack", base / "private"
            (stack / "configs").mkdir(parents=True)
            private.mkdir()
            compose, nomad = stack / "docker-compose.staging.yaml", stack / "configs/nomad.staging.yaml"
            compose.write_text(COMPOSE)
            nomad.write_text(NOMAD)
            for name in ["nomad.env", "db.env"]:
                (private / name).write_text("EXISTING_SECRET=unchanged\n")
            if marker:
                (private / "north-state-verified").write_text("glaide_oasis_staging:north-state-persistent\n")
            source = SCRIPT.read_text().replace(
                '[[ $EUID == 0 ]] || die \'Run through the restricted sudo rule.\'',
                ": # Root check stubbed only in this offline fixture",
            ).replace("/opt/nomad/glaide-oasis-staging", str(stack)).replace(
                "/etc/nomad-oasis-staging", str(private)
            )
            fixture = base / "deploy.sh"
            fixture.write_text(source)
            calls = base / "calls"
            result = subprocess.run(
                ["bash", "-c", 'source "$1"\n' + MOCKS, "test", str(fixture)],
                capture_output=True, text=True,
                env={**os.environ, "CALLS": str(calls), "TAG": "v1.2.2", "FAILURE": failure,
                     "ACTIVE_LOGTRANSFER": "1" if logtransfer else "0"},
            )
            for name in ["nomad.env", "db.env"]:
                self.assertEqual((private / name).read_text(), "EXISTING_SECRET=unchanged\n")
            backups = list((private / "deploy-backups").glob("*"))
            if backups:
                self.assertEqual((backups[0] / "docker-compose.staging.yaml").read_text(), COMPOSE)
                self.assertEqual((backups[0] / "nomad.staging.yaml").read_text(), NOMAD)
                self.assertTrue((backups[0] / "jupyter-image.txt").is_file())
                jupyter_record = (backups[0] / "jupyter-image.txt").read_text()
                self.assertIn("previous_ref=ghcr.io/glaidedata/glaidedata-oasis/jupyter:v1.2.1", jupyter_record)
                if failure == "old_jupyter_missing":
                    self.assertIn("locally_available=no", jupyter_record)
                else:
                    self.assertIn("locally_available=yes", jupyter_record)
            return result, calls.read_text() if calls.exists() else "", compose.read_text(), nomad.read_text()

    def test_missing_north_attestation_prevents_deployment(self):
        result, calls, compose, nomad = self.lifecycle(marker=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("north-state-verified", result.stderr)
        self.assertEqual(calls, "")
        self.assertEqual((compose, nomad), (COMPOSE, NOMAD))

    def test_jupyter_pull_failure_leaves_both_active_configs_untouched(self):
        result, calls, compose, nomad = self.lifecycle(failure="pull")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("/jupyter:v1.2.2", calls)
        self.assertNotIn(" up -d ", calls)
        self.assertEqual((compose, nomad), (COMPOSE, NOMAD))

    def test_missing_old_jupyter_image_does_not_block_new_release(self):
        result, calls, compose, nomad = self.lifecycle(failure="old_jupyter_missing")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Previous Jupyter image", result.stderr)
        self.assertIn("/jupyter:v1.2.2", calls)
        self.assertIn(":v1.2.2", compose)
        self.assertIn(":v1.2.2", nomad)

    def test_proxy_is_checked_for_running_state_without_fake_health(self):
        result, calls, _, _ = self.lifecycle()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(" ps -q proxy\n", calls)
        self.assertNotIn("inspect -f {{.State.Health.Status}} proxy-container", calls)

    def test_unhealthy_dependency_and_stopped_proxy_block_deployment(self):
        for failure in ["unhealthy", "proxy_down"]:
            with self.subTest(failure=failure):
                result, calls, compose, nomad = self.lifecycle(failure=failure)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("pull --platform", calls)
                self.assertEqual((compose, nomad), (COMPOSE, NOMAD))

    def test_recreation_failure_reports_failure_and_retains_backups(self):
        result, calls, compose, nomad = self.lifecycle(failure="recreate")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("No automatic rollback", result.stderr)
        self.assertNotIn("nginx -s reload", calls)
        self.assertIn(":v1.2.2", compose)
        self.assertIn(":v1.2.2", nomad)

    def test_optional_service_query_failure_is_not_ignored(self):
        result, calls, compose, nomad = self.lifecycle(failure="logtransfer_status")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("pull --platform", calls)
        self.assertEqual((compose, nomad), (COMPOSE, NOMAD))

    def test_success_only_recreates_application_services(self):
        for active in [False, True]:
            with self.subTest(logtransfer=active):
                result, calls, compose, nomad = self.lifecycle(logtransfer=active)
                self.assertEqual(result.returncode, 0, result.stderr)
                updates = [line for line in calls.splitlines() if " up -d " in line]
                self.assertEqual([line.split()[-1] for line in updates],
                                 ["north", "worker", "app"] + (["logtransfer"] if active else []))
                for line in updates:
                    for flag in ["--no-deps", "--force-recreate", "--pull never", "--wait-timeout 600"]:
                        self.assertIn(flag, line)
                self.assertIn("--scale worker=4", updates[1])
                self.assertLess(calls.index("pull --platform linux/amd64 ghcr.io/glaidedata/glaidedata-oasis/jupyter:"),
                                calls.index(" up -d "))
                self.assertIn("nginx -s reload", calls)
                self.assertIn("--resolve nomad-oasis-test.glaidedata.com:443:127.0.0.1", calls)
                self.assertNotIn("--remove-orphans", calls)
                self.assertNotIn(" down", calls)
                self.assertNotIn("prune", calls)
                self.assertEqual(compose, COMPOSE.replace(":v1.2.1", ":v1.2.2"))
                self.assertEqual(nomad, NOMAD.replace(":v1.2.1", ":v1.2.2"))


if __name__ == "__main__":
    unittest.main()
