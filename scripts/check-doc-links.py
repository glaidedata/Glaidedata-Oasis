"""Fix one upstream docs link and validate rendered public NOMAD hyperlinks."""

import argparse
import os
import posixpath
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit


UPSTREAM_CLI_LINK = "http://127.0.0.1:8000/reference/cli.html#nomad-admin-uploads-export"


def public_base():
    """Use the same public service settings as NOMAD's documentation macros."""
    host = os.environ["NOMAD_SERVICES_API_HOST"]
    port = int(os.environ["NOMAD_SERVICES_API_PORT"])
    https = os.environ["NOMAD_SERVICES_HTTPS"].lower()
    path = os.environ["NOMAD_SERVICES_API_BASE_PATH"].rstrip("/")
    if https not in ("true", "false") or not path.startswith("/") or "//" in path:
        raise ValueError("Invalid public documentation service configuration")
    scheme = "https" if https == "true" else "http"
    authority = host if port in (80, 443) else f"{host}:{port}"
    return f"{scheme}://{authority}{path}"


class Links(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.code_depth = 0
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag in ("code", "pre"):
            self.code_depth += 1
        if tag == "a" and not self.code_depth:
            href = dict(attrs).get("href")
            if href:
                self.links.append((self.getpos()[0], href))

    def handle_endtag(self, tag):
        if tag in ("code", "pre") and self.code_depth:
            self.code_depth -= 1


def validate(site):
    site = Path(site)
    base = public_base()
    api_page = site / "howto/manage/program/api.html"
    oasis_page = site / "explanation/oasis.html"
    if not api_page.is_file() or not oasis_page.is_file():
        raise ValueError("Expected NOMAD API and Oasis documentation pages are missing")

    # NOMAD 2.0.1 hard-codes this unrelated localhost URL in the Oasis guide.
    # Change only this exact generated link, without altering upstream sources.
    oasis = oasis_page.read_text(encoding="utf-8")
    public_cli_link = f"{base}/docs/reference/cli.html#nomad-admin-uploads-export"
    oasis_page.write_text(oasis.replace(UPSTREAM_CLI_LINK, public_cli_link), encoding="utf-8")

    errors = []
    api_links = set()
    pages = list(site.rglob("*.html"))
    for page in pages:
        parser = Links()
        parser.feed(page.read_text(encoding="utf-8"))
        for line, href in parser.links:
            host = urlsplit(href).hostname
            if host and (host.lower() == "localhost" or host.startswith("127.") or host == "::1"):
                errors.append(f"{page.relative_to(site)}:{line}: {href}")
            if page == api_page:
                parts = urlsplit(urljoin(base + "/docs/howto/manage/program/api.html", href))
                path = posixpath.normpath(parts.path)
                if parts.path.endswith("/") and not path.endswith("/"):
                    path += "/"
                api_links.add(urlunsplit(parts._replace(path=path)))

    expected = {f"{base}/api/v1/", f"{base}/gui/v2/apis"}
    errors.extend(f"API page missing expected link: {url}" for url in sorted(expected - api_links))
    if errors:
        raise ValueError("Invalid documentation links:\n" + "\n".join(errors))
    print(f"Validated {len(pages)} HTML pages; public API and GUI links found.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("site", type=Path)
    validate(parser.parse_args().site)
