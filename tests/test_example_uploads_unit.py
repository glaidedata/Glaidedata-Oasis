from unittest.mock import Mock, patch

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
