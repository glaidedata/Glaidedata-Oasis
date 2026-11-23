from unittest.mock import Mock, patch
from pathlib import Path

import pytest
from pydantic import SecretStr

import conftest
import test_example_uploads


def test_auth_fixture_passes_plain_password():
    with (
        patch.object(conftest.config.client, "password", SecretStr("test-only-value")),
        patch("nomad.client.Auth") as auth_class,
    ):
        assert conftest.auth.__wrapped__() is auth_class.return_value
        assert auth_class.call_args.kwargs["password"] == "test-only-value"
        assert auth_class.call_args.kwargs["from_api"] is True


def test_example_upload_queries_entries_and_uses_string_archive_url():
    responses = [
        {"upload_id": "upload-1"},
        {"data": {"errors": [], "warnings": [], "process_running": False, "process_status": "READY"}},
        {
            "data": [{"entry_id": "entry-1", "upload_id": "upload-1"}],
            "pagination": {"total": 1},
        },
        {
            "data": {
                "entry_id": "entry-1",
                "upload_id": "upload-1",
                "archive": {"processing_logs": [{"level": "INFO"}]},
            }
        },
    ]
    with (
        patch.object(test_example_uploads, "make_request_with_retry") as request,
        patch.object(test_example_uploads.time, "sleep"),
    ):
        request.side_effect = [Mock(**{"json.return_value": data}) for data in responses]
        test_example_uploads.test_example_uploads("example-id", Mock())

    query = request.call_args_list[2]
    assert query.kwargs["json"]["owner"] == "user"
    assert query.kwargs["json"]["query"] == {"upload_id": "upload-1"}
    assert request.call_args_list[3].kwargs["url"] == "entries/entry-1/archive"


def run_example_with_query_results(entry_point_id, entries=None):
    entries = [] if entries is None else entries
    responses = [
        {"upload_id": "upload-1"},
        {"data": {"errors": [], "warnings": [], "process_running": False, "process_status": "READY"}},
        {"data": entries, "pagination": {"total": len(entries)}},
    ]
    with (
        patch.object(test_example_uploads, "make_request_with_retry") as request,
        patch.object(test_example_uploads.time, "sleep"),
    ):
        request.side_effect = [Mock(**{"json.return_value": data}) for data in responses]
        test_example_uploads.test_example_uploads(entry_point_id, Mock())
    assert request.call_count == 3


def test_readme_only_luqy_example_explicitly_accepts_zero_entries():
    assert test_example_uploads.is_luqy_documentation_only_example(
        test_example_uploads.LUQY_PLACEHOLDER_ID
    )
    run_example_with_query_results(test_example_uploads.LUQY_PLACEHOLDER_ID)


def test_readme_only_luqy_example_rejects_unexpected_entries():
    with pytest.raises(AssertionError, match="unexpectedly produced entries"):
        run_example_with_query_results(
            test_example_uploads.LUQY_PLACEHOLDER_ID,
            [{"entry_id": "entry-1", "upload_id": "upload-1"}],
        )


def test_other_examples_still_require_entries():
    with pytest.raises(AssertionError, match="no searchable entries"):
        run_example_with_query_results("other_plugin.example_uploads:example_upload_entry_point")


def test_luqy_example_with_new_data_must_produce_entries(tmp_path: Path):
    directory = tmp_path / "example_uploads" / "getting_started"
    directory.mkdir(parents=True)
    (directory / "README.md").write_text("EXAMPLE DATA")
    (directory / "data.csv").write_text("value\n1\n")
    with patch.object(test_example_uploads.resources, "files", return_value=tmp_path):
        assert not test_example_uploads.is_luqy_documentation_only_example(
            test_example_uploads.LUQY_PLACEHOLDER_ID
        )
        with pytest.raises(AssertionError, match="no searchable entries"):
            run_example_with_query_results(test_example_uploads.LUQY_PLACEHOLDER_ID)
