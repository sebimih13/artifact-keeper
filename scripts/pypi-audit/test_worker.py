"""Exercise release decisions and ensure failures never reach promotion."""
import contextlib
import datetime as dt
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import zipfile

import worker


class Response(io.BytesIO):
    url = "https://files.pythonhosted.org/demo.whl"


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as z:
            z.writestr("demo-1.0.dist-info/METADATA", "Name: demo\nVersion: 1.0\nLicense-Expression: MIT\n")
        self.content = buffer.getvalue()
        self.digest = hashlib.sha256(self.content).hexdigest()
        self.calls = []
        self.failed_scan = False
        self.high_count = 0
        self.license = "MIT"
        self.dt_unavailable = False
        self.violation = False
        self.empty_sbom = False
        self.public_stage = False
        self.completeness = "complete"
        self.stale = False
        self.bad_release = False
        self.w = worker.Worker.__new__(worker.Worker)
        self.w.state = Path(self.tmp.name)
        self.w.timeout = 1
        self.w.allowed_licenses = {"MIT"}
        self.w.ak = self
        self.w.dtrack = self
        self.env = patch.dict(os.environ, {"DTRACK_AUDIT_POLICY_ID": "policy"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def component(self):
        return {"type": "library", "name": "demo", "version": "1.0", "purl": "pkg:pypi/demo@1.0", "licenses": [{"license": {"id": self.license}}]}

    def request(self, path, method="GET", data=None, raw=False):
        self.calls.append((path, method))
        if path.startswith("/repositories/pypi-staging/artifacts/") and method == "PUT":
            return {"id": "artifact", "checksum_sha256": self.digest}
        if path.startswith("/quarantine/"):
            return {}
        if path == "/security/scan":
            return {"scan_result_ids": ["dependency", "grype"]}
        if path.startswith("/security/scans/"):
            return {"scan_type": path.rsplit("/", 1)[1], "status": "failed" if self.failed_scan else "completed", "high_count": self.high_count, "critical_count": 0,
                    "scan_completeness": self.completeness, "vuln_db_published_at": (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30 if self.stale else 0)).isoformat()}
        if path == "/sbom":
            return {"id": "bom"}
        if path == "/sbom/bom":
            return {"content": {"components": [] if self.empty_sbom else [self.component()]}}
        if path.startswith("/project"):
            return {"uuid": "project", "tags": [{"name": "ak-pypi-audit"}], "lastBomImport": int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000) + 1000}
        if path == "/bom" or path.endswith("/analyze"):
            if self.dt_unavailable:
                raise urllib.error.URLError("Dependency-Track unreachable")
            return {"token": "known-token"}
        if path.startswith("/event/token/"):
            return {"processing": False}
        if path.startswith("/vulnerability/source/"):
            return {"vulnId": "PYSEC-2020-96"}
        if path == "/repositories/pypi-staging":
            return {"is_public": self.public_stage}
        if path == "/repositories/pypi-release":
            return {"promotion_only": True}
        if path == "/repositories/pypi/members":
            return {"members": [{"member_repo_key": "pypi-release"}]}
        if path == "/artifacts/artifact":
            return {"checksum_sha256": self.digest}
        if path.endswith("/promote"):
            return {"promoted": True}
        if path.startswith("/repositories/pypi-release/download/"):
            return b"corrupt" if self.bad_release else self.content
        if path.startswith("/repositories/pypi-release/artifacts/"):
            return {"id": "release", "checksum_sha256": self.digest}
        raise AssertionError("Unexpected API call: " + path)

    def pages(self, path):
        if path.startswith("/component/"):
            return [self.component()]
        if path.startswith("/violation/"):
            return [{"policyCondition": {}}] if self.violation else []
        return []

    def run_audit(self):
        metadata = {"urls": [{"filename": "demo-1.0-py3-none-any.whl", "url": Response.url, "size": len(self.content), "digests": {"sha256": self.digest}}]}
        with patch.object(worker.Client, "request", return_value=metadata), patch.object(worker.urllib.request, "urlopen", side_effect=lambda *a, **k: Response(self.content)), contextlib.redirect_stdout(io.StringIO()):
            return self.w.audit("demo", "1.0", "demo-1.0-py3-none-any.whl", promote=True)

    def assert_blocked(self):
        with self.assertRaises((worker.Blocked, urllib.error.URLError)):
            self.run_audit()
        self.assertFalse(any(path.endswith("/promote") for path, _ in self.calls))
        reports = list(self.w.state.glob("reports/*/report.json"))
        self.assertEqual(json.loads(reports[0].read_text())["status"], "blocked")

    def test_clean_artifact_is_promoted(self):
        self.assertEqual(self.run_audit()["status"], "promoted")

    def test_failed_scan_does_not_promote(self):
        self.failed_scan = True
        self.assert_blocked()

    def test_high_vulnerability_does_not_promote(self):
        self.high_count = 1
        self.assert_blocked()

    def test_dependency_track_outage_does_not_promote(self):
        self.dt_unavailable = True
        self.assert_blocked()

    def test_dependency_track_policy_violation_does_not_promote(self):
        self.violation = True
        self.assert_blocked()

    def test_unknown_license_does_not_promote(self):
        self.license = "unknown"
        self.assert_blocked()

    def test_empty_sbom_does_not_promote(self):
        self.empty_sbom = True
        self.assert_blocked()

    def test_public_staging_does_not_promote(self):
        self.public_stage = True
        self.assert_blocked()

    def test_incomplete_scan_does_not_promote(self):
        self.completeness = "partial"
        self.assert_blocked()

    def test_stale_database_does_not_promote(self):
        self.stale = True
        self.assert_blocked()

    def test_failed_post_promotion_integrity_check_quarantines_release(self):
        self.bad_release = True
        with self.assertRaisesRegex(worker.Blocked, "Released bytes"):
            self.run_audit()
        self.assertIn(("/quarantine/release/quarantine", "POST"), self.calls)


if __name__ == "__main__":
    unittest.main()
