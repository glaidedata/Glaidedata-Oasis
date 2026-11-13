"""Offline checks for the restricted netcup deployment script."""

import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest


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
        *' config -q') ;;
        *' ps --status running -q logtransfer')
            [[ ${FAIL_LOGTRANSFER_QUERY:-0} == 0 ]] || return 1
            [[ ${ACTIVE_LOGTRANSFER:-0} == 0 ]] || printf '%s\n' logtransfer1 ;;
        *' ps --status running -q worker') printf '%s\n' w1 w2 w3 w4 ;;
        'pull '*) [[ ${FAIL_PULL:-0} == 0 || $* != *'/jupyter:'* ]] ;;
        *' up -d '*) ;;
        *' exec -T proxy nginx -t'|*' exec -T proxy nginx -s reload') ;;
        *) echo "Unexpected Docker call: $*" >&2; return 99 ;;
    esac
}
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
        for kind, source in [("compose", COMPOSE), ("nomad", NOMAD)]:
            with self.subTest(kind=kind):
                result = self.rewrite(kind, source)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, source.replace(":v1.2.1", ":v1.2.2"))

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

    def deploy(self, *, marker=True, fail_pull=False, fail_logtransfer_query=False,
               active_logtransfer=False):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            stack, private = base / "stack", base / "private"
            (stack / "configs").mkdir(parents=True)
            private.mkdir()
            compose = stack / "docker-compose.staging.yaml"
            nomad = stack / "configs/nomad.staging.yaml"
            compose.write_text(COMPOSE)
            nomad.write_text(NOMAD)
            for name in ["db.env", "nomad.env"]:
                (private / name).write_text("EXISTING_SECRET=unchanged\n")
            if marker:
                (private / "north-state-verified").write_text(
                    "glaide_oasis_staging:north-state-persistent\n"
                )
            source = SCRIPT.read_text().replace(
                "[[ $EUID == 0 ]] || die 'Run through the restricted sudo rule.'",
                ": # root check stubbed in offline test",
            ).replace("/opt/nomad/glaide-oasis-staging", str(stack)).replace(
                "/etc/nomad-oasis-staging", str(private)
            )
            fixture = base / "deploy.sh"
            fixture.write_text(source)
            calls = base / "calls"
            result = subprocess.run(
                ["bash", "-c", 'source "$1"\n' + MOCKS, "test", str(fixture)],
                capture_output=True, text=True,
                env={**os.environ, "CALLS": str(calls), "TAG": "v1.2.2",
                     "FAIL_PULL": "1" if fail_pull else "0",
                     "FAIL_LOGTRANSFER_QUERY": "1" if fail_logtransfer_query else "0",
                     "ACTIVE_LOGTRANSFER": "1" if active_logtransfer else "0"},
            )
            return (result, calls.read_text() if calls.exists() else "",
                    compose.read_text(), nomad.read_text())

    def test_north_guard_blocks_recreation(self):
        result, calls, compose, nomad = self.deploy(marker=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("north-state-verified", result.stderr)
        self.assertEqual(calls, "")
        self.assertEqual((compose, nomad), (COMPOSE, NOMAD))

    def test_pull_failure_keeps_active_configuration(self):
        result, calls, compose, nomad = self.deploy(fail_pull=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("pull --platform linux/amd64 ghcr.io/glaidedata/glaidedata-oasis/jupyter:v1.2.2", calls)
        self.assertNotIn(" up -d ", calls)
        self.assertEqual((compose, nomad), (COMPOSE, NOMAD))

    def test_unknown_logtransfer_state_blocks_deployment(self):
        result, calls, compose, nomad = self.deploy(fail_logtransfer_query=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("pull --platform", calls)
        self.assertEqual((compose, nomad), (COMPOSE, NOMAD))

    def test_success_recreates_only_application_services(self):
        result, calls, compose, nomad = self.deploy()
        self.assertEqual(result.returncode, 0, result.stderr)
        updates = [line for line in calls.splitlines() if " up -d " in line]
        self.assertEqual([line.split()[-1] for line in updates], ["north", "worker", "app"])
        self.assertIn("--scale worker=4", updates[1])
        self.assertTrue(all("--no-deps" in line for line in updates))
        self.assertTrue(all("--wait --wait-timeout 1200" in line for line in updates))
        self.assertLess(calls.index("/jupyter:v1.2.2"), calls.index(" up -d "))
        self.assertNotIn(" down", calls)
        self.assertNotIn("prune", calls)
        self.assertEqual(compose, COMPOSE.replace(":v1.2.1", ":v1.2.2"))
        self.assertEqual(nomad, NOMAD.replace(":v1.2.1", ":v1.2.2"))

    def test_active_logtransfer_waits_and_smoke_checks_production_domain(self):
        result, calls, _, _ = self.deploy(active_logtransfer=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        updates = [line for line in calls.splitlines() if " up -d " in line]
        self.assertEqual(updates[-1].split()[-1], "logtransfer")
        self.assertIn("--wait --wait-timeout 1200", updates[-1])
        for path in ("alive", "gui/"):
            self.assertIn(
                f"--resolve nomad-oasis.glaidedata.com:443:127.0.0.1 "
                f"https://nomad-oasis.glaidedata.com/nomad-oasis/{path}",
                calls,
            )
        self.assertNotIn("nomad-oasis-test.glaidedata.com", calls)

    def test_actions_deployment_job_has_30_minute_timeout(self):
        workflow = (SCRIPT.parents[1] / ".github/workflows/release-tag-build-and-deploy.yml").read_text()
        self.assertEqual(re.findall(r"(?m)^    timeout-minutes: (\d+)$", workflow), ["30"])


if __name__ == "__main__":
    unittest.main()
