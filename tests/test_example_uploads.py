import os
import time
from importlib import resources

from typing import TYPE_CHECKING

import pytest
from nomad.config import config
from conftest import make_request_with_retry, get_request, post_request

if TYPE_CHECKING:
    from nomad.config.models.plugins import ExampleUploadEntryPoint


PLUGINS_TO_SKIP = os.getenv("PLUGINS_STRING", "")
LUQY_PLACEHOLDER_ID = "nomad_luqy_plugin.example_uploads:example_upload_entry_point"


def is_luqy_documentation_only_example(entry_point_id: str) -> bool:
    """Recognize the README-only example at the pinned LuQY commit.

    Inspect the installed package so a later pin with data files receives the
    normal entry and archive checks instead of inheriting this exception.
    """
    if entry_point_id != LUQY_PLACEHOLDER_ID:
        return False
    directory = resources.files("nomad_luqy_plugin").joinpath(
        "example_uploads", "getting_started"
    )
    return (
        sorted(child.name for child in directory.iterdir()) == ["README.md"]
        and directory.joinpath("README.md").read_text().strip() == "EXAMPLE DATA"
    )


def get_example_upload_entrypoints() -> list["ExampleUploadEntryPoint"]:
    """
    Retrieves information about example upload entrypoints
    """
    config.load_plugins()
    if not config.plugins:
        return []

    example_uploads = [
        entry_point
        for entry_point in config.plugins.entry_points.filtered_values()
        if entry_point.entry_point_type == "example_upload"
    ]

    return example_uploads


def get_example_upload_ids() -> list[str]:
    return [
        entry_point.id
        for entry_point in get_example_upload_entrypoints()
        if entry_point.id
        and not entry_point.from_examples_directory
        and (entry_point.plugin_package or "") not in PLUGINS_TO_SKIP
    ]


@pytest.mark.parametrize(
    "entry_point_id",
    get_example_upload_ids(),
    ids=lambda entry_point_id: entry_point_id,
)
def test_example_uploads(entry_point_id, auth):
    url = f"uploads?example_upload_id={entry_point_id}"
    response = make_request_with_retry(post_request, url=url, auth=auth)
    upload_id = response.json().get("upload_id")
    assert upload_id, "Example upload response did not include an upload ID"
    url = f"uploads/{upload_id}"

    timeout = 600
    interval = 10
    start = time.time()
    processing = True

    while processing:
        if time.time() - start > timeout:
            raise TimeoutError("Example upload processing timed out")
        time.sleep(interval)
        url = f"uploads/{upload_id}"
        response = make_request_with_retry(get_request, url=url, auth=auth)
        upload_data = response.json()["data"]
        assert not upload_data["errors"]
        assert not upload_data["warnings"]
        if not upload_data["process_running"]:
            # Check that upload processed fine with no overall errors/warnings
            assert (
                upload_data["process_status"] == "READY"
                or upload_data["process_status"] == "SUCCESS"
            )
            processing = False

    # Check entries for errors
    entries = []
    page_after_value = None
    while True:
        pagination = {"page_size": 100}
        if page_after_value is not None:
            pagination["page_after_value"] = page_after_value
        response = make_request_with_retry(
            post_request,
            url="entries/query",
            auth=auth,
            json={
                "owner": "user",
                "query": {"upload_id": upload_id},
                "pagination": pagination,
            },
        )
        result = response.json()
        entries.extend(result["data"])
        page_after_value = result["pagination"].get("next_page_after_value")
        if page_after_value is None:
            assert len(entries) == result["pagination"]["total"]
            break

    if is_luqy_documentation_only_example(entry_point_id):
        assert not entries, "The LuQY README-only example unexpectedly produced entries"
        return

    assert entries, f"Example upload {upload_id} produced no searchable entries"
    entry_ids = [entry["entry_id"] for entry in entries]
    for entry_id in entry_ids:
        url = f"entries/{entry_id}/archive"
        response = make_request_with_retry(get_request, url=url, auth=auth)
        archive_data = response.json()["data"]
        assert archive_data["entry_id"] == entry_id
        assert archive_data["upload_id"] == upload_id
        logs = archive_data["archive"]["processing_logs"]
        for log in logs:
            assert log["level"] != "ERROR"
