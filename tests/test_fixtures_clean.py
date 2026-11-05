"""Guards the committed n8n fixtures: real shapes, fake values, nothing personal.

The repository is public. These checks do not replace reading a fixture before committing
it; they only catch the mistakes a regex can catch.
"""
import json
import re
from pathlib import Path

import pytest

FIXTURES = sorted((Path(__file__).parent / "fixtures" / "n8n").glob("*.json"))
EMAIL = re.compile(r"[\w.+-]+@([\w-]+\.)+\w+")
URL = re.compile(r"https?://([^/\s\"'\\]+)")


def test_there_are_fixtures_to_check():
    assert {f.name for f in FIXTURES} >= {
        "F1_list_page1.json", "F2_list_page2.json", "F3_error_with_data.json",
        "F4_success_no_data.json", "F5_active_list.json", "F5_workflow.json",
    }


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.name)
class TestFixture:
    def test_is_valid_json(self, path):
        json.loads(path.read_text(encoding="utf-8"))

    def test_no_emails_outside_example_com(self, path):
        for match in EMAIL.finditer(path.read_text(encoding="utf-8")):
            assert match.group(0).lower().endswith("@example.com"), match.group(0)

    def test_no_urls_outside_example_domains(self, path):
        for host in URL.findall(path.read_text(encoding="utf-8")):
            assert host == "example.com" or host.endswith(".example.com"), host

    def test_no_credentials_or_tokens(self, path):
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"bearer\s+\S{8,}", text, re.I)
        assert not re.search(r"(api[_-]?key|password|secret|token)[\"']?\s*[:=]\s*[\"']?\w{8,}", text, re.I)

    def test_execution_files_use_the_fake_workflow_id(self, path):
        data = json.loads(path.read_text(encoding="utf-8"))
        rows = data["data"] if isinstance(data.get("data"), list) else [data]
        for row in rows:
            if "workflowId" in row:
                assert row["workflowId"] == "wf_fixture_001"
            if path.name.startswith("F5"):
                assert row["id"].startswith("wf_fixture_")
