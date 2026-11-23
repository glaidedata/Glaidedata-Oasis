import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


spec = importlib.util.spec_from_file_location(
    "check_doc_links", Path(__file__).resolve().parents[1] / "scripts/check-doc-links.py"
)
links = importlib.util.module_from_spec(spec)
spec.loader.exec_module(links)


class DocumentationLinksTest(unittest.TestCase):
    def setUp(self):
        settings = patch.dict(
            "os.environ",
            {
                "NOMAD_SERVICES_API_HOST": "nomad-oasis.glaidedata.com",
                "NOMAD_SERVICES_API_PORT": "443",
                "NOMAD_SERVICES_HTTPS": "true",
                "NOMAD_SERVICES_API_BASE_PATH": "/nomad-oasis",
            },
        )
        settings.start()
        self.addCleanup(settings.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.site = Path(self.temp.name)
        self.api = self.site / "howto/manage/program/api.html"
        self.oasis = self.site / "explanation/oasis.html"
        self.api.parent.mkdir(parents=True)
        self.oasis.parent.mkdir(parents=True)
        self.api.write_text(self.api_links())
        self.oasis.write_text(f'<a href="{links.UPSTREAM_CLI_LINK}">CLI</a>')

    def api_links(self):
        base = links.public_base()
        return f'<a href="{base}/api/v1/">API</a><a href="{base}/api/../gui/v2/apis">GUI</a>'

    def test_correct_public_links_and_upstream_cli_fix(self):
        self.api.write_text(
            self.api.read_text()
            + '<pre><code>http://localhost:8000/api '
            + '<a href="http://localhost:8000/api">example</a></code></pre>'
        )
        links.validate(self.site)
        self.assertIn(
            f"{links.public_base()}/docs/reference/cli.html#nomad-admin-uploads-export",
            self.oasis.read_text(),
        )

    def test_rejects_localhost_link_on_any_page(self):
        (self.site / "other.html").write_text('<a href="http://localhost:8000/nomad-oasis/api">bad</a>')
        with self.assertRaisesRegex(ValueError, "other.html"):
            links.validate(self.site)

    def test_rejects_old_gui_route(self):
        self.api.write_text(self.api.read_text().replace("gui/v2/apis", "gui/analyze/apis"))
        with self.assertRaisesRegex(ValueError, "missing expected link"):
            links.validate(self.site)

    def test_preview_uses_the_same_service_settings(self):
        with patch.dict(
            "os.environ",
            {
                "NOMAD_SERVICES_API_HOST": "preview.example.org",
                "NOMAD_SERVICES_API_PORT": "8443",
                "NOMAD_SERVICES_HTTPS": "true",
                "NOMAD_SERVICES_API_BASE_PATH": "/preview-oasis",
            },
        ):
            self.api.write_text(self.api_links())
            links.validate(self.site)
            self.assertIn("https://preview.example.org:8443/preview-oasis/docs/", self.oasis.read_text())


if __name__ == "__main__":
    unittest.main()
