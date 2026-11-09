#!/usr/bin/env python3
"""Wheel-only PyPI intake. Failed/incomplete audits never promote artifacts."""
import argparse
import base64
import datetime as dt
import email.parser
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile


class Blocked(RuntimeError):
    pass


def normalized(name):
    return re.sub(r"[-_.]+", "-", name).lower()


class Client:
    def __init__(self, base, headers):
        self.base, self.headers = base.rstrip("/"), headers

    def request(self, path, method="GET", data=None, raw=False):
        headers = dict(self.headers)
        if data is not None and not isinstance(data, bytes):
            data = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=60) as response:
                body = response.read()
                return body if raw else (json.loads(body) if body else None)
        except urllib.error.HTTPError as exc:
            try:
                error = json.loads(exc.read())
                detail = error.get("message") or error.get("error")
                if isinstance(detail, str):
                    exc.reason = str(exc.reason) + ": " + detail[:1000]
            except (ValueError, AttributeError):
                pass
            raise

    def pages(self, path):
        result = []
        for page in range(1, 1001):
            sep = "&" if "?" in path else "?"
            batch = self.request(f"{path}{sep}pageNumber={page}&pageSize=100")
            if not isinstance(batch, list):
                raise Blocked("Unexpected Dependency-Track pagination response")
            result.extend(batch)
            if len(batch) < 100:
                return result
        raise Blocked("Dependency-Track pagination limit exceeded")


def wheel_metadata(content):
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        infos = archive.infolist()
        if sum(i.file_size for i in infos) > 512 * 1024 * 1024 or len(infos) > 20000:
            raise Blocked("Wheel exceeds archive inspection budget")
        for info in infos:
            path = PurePosixPath(info.filename)
            if path.is_absolute() or ".." in path.parts or "\\" in info.filename:
                raise Blocked("Unsafe archive path")
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise Blocked("Wheel contains a symbolic link")
            if info.flag_bits & 1:
                raise Blocked("Encrypted archive entry")
        metadata = [i for i in infos if i.filename.endswith(".dist-info/METADATA")]
        if len(metadata) != 1 or metadata[0].file_size > 4 * 1024 * 1024:
            raise Blocked("Missing, ambiguous, or oversized wheel metadata")
        parsed = email.parser.BytesParser().parsebytes(archive.read(metadata[0]))
        if not parsed.get("Name") or not parsed.get("Version"):
            raise Blocked("Wheel metadata has no coordinates")
        return parsed


class Worker:
    def __init__(self, state):
        self.state = Path(state)
        self.state.mkdir(parents=True, exist_ok=True)
        token = os.environ.get("AK_API_TOKEN") or Path(os.environ["AK_API_TOKEN_FILE"]).read_text().strip()
        key = Path(os.environ["DTRACK_API_KEY_FILE"]).read_text().strip()
        self.ak = Client(os.getenv("AK_URL", "http://localhost") + "/api/v1", {"Authorization": "Bearer " + token})
        self.dtrack = Client(os.getenv("DTRACK_URL", "http://localhost:8092") + "/api/v1", {"X-Api-Key": key})
        self.timeout = int(os.getenv("AUDIT_TIMEOUT_SECONDS", "600"))
        self.allowed_licenses = set(os.getenv("AUDIT_ALLOWED_LICENSES", "Apache-2.0,BSD-2-Clause,BSD-3-Clause,MIT,ISC,MPL-2.0,PSF-2.0").split(","))

    def wait(self, callback, description):
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            value = callback()
            if value:
                return value
            time.sleep(3)
        raise Blocked("Timed out waiting for " + description)

    def wait_dt(self, token):
        if not token:
            raise Blocked("Dependency-Track returned no processing token")
        def done():
            result = self.dtrack.request("/event/token/" + token)
            if result.get("status") == "FAILED":
                raise Blocked("Dependency-Track processing failed")
            # DT 4.14 returns only a boolean. These tokens must come directly
            # from our own successful upload/analyze calls, not user input.
            # Import timestamps and root inventory are verified separately.
            if result.get("status"):
                return result if result["status"] == "COMPLETED" else None
            return result if result.get("processing") is False else None
        return self.wait(done, "Dependency-Track analysis")

    def audit(self, package, version, filename, promote=False):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", package):
            raise Blocked("Invalid package name")
        package = normalized(package)
        info = Client("https://pypi.org", {}).request("/pypi/" + urllib.parse.quote(package) + "/" + urllib.parse.quote(version, safe="") + "/json")
        release = next((x for x in info["urls"] if x["filename"] == filename), None)
        if not release or not filename.endswith(".whl") or release.get("yanked"):
            raise Blocked("Only non-yanked wheels listed by PyPI can enter this pipeline")
        url = urllib.parse.urlsplit(release["url"])
        if url.scheme != "https" or url.hostname != "files.pythonhosted.org":
            raise Blocked("Untrusted distribution URL")
        if release["size"] > 100 * 1024 * 1024:
            raise Blocked("Wheel exceeds download budget")
        with urllib.request.urlopen(release["url"], timeout=60) as response:
            if urllib.parse.urlsplit(response.url).hostname != "files.pythonhosted.org":
                raise Blocked("Untrusted distribution redirect")
            content = response.read(100 * 1024 * 1024 + 1)
        digest = hashlib.sha256(content).hexdigest()
        if len(content) != release["size"] or digest != release["digests"]["sha256"]:
            raise Blocked("Distribution size or SHA-256 does not match PyPI")
        metadata = wheel_metadata(content)
        if normalized(metadata["Name"]) != package or metadata["Version"] != version:
            raise Blocked("Wheel coordinates do not match the requested release")
        report_dir = self.state / "reports" / digest
        report_dir.mkdir(parents=True, exist_ok=True)
        report = {"package": package, "version": version, "filename": filename, "sha256": digest,
                  "source": release["url"], "status": "auditing", "started_at": dt.datetime.now(dt.timezone.utc).isoformat()}
        def save():
            temporary = report_dir / "report.tmp"
            temporary.write_text(json.dumps(report, indent=2))
            temporary.replace(report_dir / "report.json")
        artifact_id = None
        try:
            path = "/".join(urllib.parse.quote(x, safe="") for x in [package, version, filename])
            try:
                artifact = self.ak.request("/repositories/pypi-staging/artifacts/" + path, "PUT", content)
            except urllib.error.HTTPError as exc:
                if exc.code != 409:
                    raise
                # Hosted paths are immutable. Re-audit the existing object only
                # when its identity and digest still match this exact intake.
                matches = []
                for page in range(1, 1001):
                    listed = self.ak.request(f"/repositories/pypi-staging/artifacts?per_page=100&page={page}")
                    matches.extend(a for a in listed["items"] if a["path"] == urllib.parse.unquote(path))
                    if page >= listed["pagination"]["total_pages"]:
                        break
                if len(matches) != 1:
                    raise Blocked("Conflicting upload has no unique staged artifact")
                artifact = matches[0]
            artifact_id = artifact["id"]
            report["artifact_id"] = artifact_id
            if artifact.get("checksum_sha256") != digest:
                raise Blocked("Staging checksum mismatch")
            self.ak.request("/quarantine/" + artifact_id + "/quarantine", "POST", {"reason": "Awaiting complete security and Dependency-Track audit"})
            save()
            triggered = self.ak.request("/security/scan", "POST", {"artifact_id": artifact_id, "bypass_dedup": True})
            ids = triggered.get("scan_result_ids", [])
            if not ids:
                raise Blocked("No security scanners were scheduled")
            def completed():
                scans = [self.ak.request("/security/scans/" + scan_id) for scan_id in ids]
                if any(s["status"] == "failed" for s in scans):
                    report["scans"] = scans
                    raise Blocked("At least one configured scanner failed")
                return scans if all(s["status"] in ("completed", "not_applicable") for s in scans) else None
            scans = self.wait(completed, "all configured security scanners")
            report["scans"] = scans
            required = set(os.getenv("AUDIT_REQUIRED_SCANNERS", "dependency,grype").split(","))
            completed_types = {s["scan_type"] for s in scans if s["status"] == "completed"}
            if not required <= completed_types:
                raise Blocked("Required scanners did not complete: " + ", ".join(required - completed_types))
            if any(s.get("scan_completeness") != "complete" for s in scans if s["status"] == "completed"):
                raise Blocked("A scanner reported incomplete or unknown coverage")
            for scan in scans:
                if scan["status"] != "completed":
                    continue
                published = scan.get("vuln_db_published_at")
                if not published:
                    raise Blocked("Scanner supplied no vulnerability database timestamp")
                age = dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(published.replace("Z", "+00:00"))
                if age.total_seconds() > int(os.getenv("AUDIT_MAX_DB_AGE_DAYS", "7")) * 86400:
                    raise Blocked("Scanner vulnerability database is stale: " + scan["scan_type"])
            sbom = self.ak.request("/sbom", "POST", {"artifact_id": artifact_id, "format": "cyclonedx", "force_regenerate": True})
            document = self.ak.request("/sbom/" + sbom["id"])["content"]
            components = document.get("components", [])
            if not components:
                raise Blocked("SBOM has no component inventory")
            root_purl = "pkg:pypi/" + package + "@" + version
            if not any(c.get("purl") == root_purl for c in components):
                root = {"type": "library", "name": package, "version": version, "purl": root_purl,
                        "bom-ref": root_purl, "hashes": [{"alg": "SHA-256", "content": digest}]}
                license_id = metadata.get("License-Expression")
                if license_id:
                    root["licenses"] = [{"expression": license_id}]
                components.append(root)
            document["components"] = components
            (report_dir / "bom.cdx.json").write_text(json.dumps(document, indent=2))
            # One immutable project per distribution digest avoids concurrent
            # uploads and per-platform wheels overwriting each other's results.
            project_name = "ak-audit/" + package + "/" + digest
            project_version = version
            try:
                project = self.dtrack.request("/project/lookup?" + urllib.parse.urlencode({"name": project_name, "version": project_version}))
            except urllib.error.HTTPError as exc:
                if exc.code != 404:
                    raise
                project = self.dtrack.request("/project", "PUT", {"name": project_name, "version": project_version, "active": True, "tags": [{"name": "ak-pypi-audit"}], "description": "AK PyPI audit; sha256=" + digest})
            project_id = project["uuid"]
            report["dtrack_project"] = project_id
            if "ak-pypi-audit" not in {t["name"] for t in project.get("tags", [])}:
                raise Blocked("Dependency-Track audit project has lost its policy tag")
            upload = self.dtrack.request("/bom", "PUT", {"project": project_id, "bom": base64.b64encode(json.dumps(document).encode()).decode()})
            report["dtrack_token"] = upload.get("token")
            save()
            report["dtrack_processing"] = self.wait_dt(upload.get("token"))
            analysis = self.dtrack.request("/finding/project/" + project_id + "/analyze", "POST")
            report["dtrack_analysis"] = self.wait_dt(analysis.get("token"))
            imported = self.dtrack.request("/project/" + project_id)
            if not imported.get("lastBomImport") or imported["lastBomImport"] < int(dt.datetime.fromisoformat(report["started_at"]).timestamp() * 1000):
                raise Blocked("Dependency-Track has not imported this audit's SBOM")
            findings = self.dtrack.pages("/finding/project/" + project_id)
            violations = self.dtrack.pages("/violation/project/" + project_id)
            report["dtrack_findings"] = findings
            report["dtrack_violations"] = violations
            scanned_components = self.dtrack.pages("/component/project/" + project_id)
            if not any(c.get("purl") == root_purl for c in scanned_components):
                raise Blocked("Dependency-Track did not import the root package")
            # Require evidence that the PyPI mirror actually contains advisories.
            # This is a readiness canary, not proof of complete feed freshness.
            try:
                self.dtrack.request("/vulnerability/source/OSV/vuln/PYSEC-2020-96")
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    raise Blocked("Dependency-Track PyPI advisory mirror is not ready") from exc
                raise
            # Run all analysis before making the release decision, even when
            # the artifact is already known vulnerable in a local scanner.
            issues = []
            if any(s.get("high_count", 0) or s.get("critical_count", 0) for s in scans):
                issues.append("AK scanner found high/critical vulnerabilities")
            if any(f.get("vulnerability", {}).get("severity", "UNKNOWN").upper() not in ("LOW", "MEDIUM", "INFO") for f in findings):
                issues.append("Dependency-Track found high/critical or unknown-severity vulnerabilities")
            if violations:
                issues.append("Dependency-Track policy violations")
            unknown_licenses = []
            for component in components:
                identifiers = {item.get("license", {}).get("id") or item.get("expression") for item in component.get("licenses", [])}
                if not identifiers or not identifiers <= self.allowed_licenses:
                    unknown_licenses.append(component.get("purl") or component.get("name"))
            if unknown_licenses:
                issues.append("Missing or unapproved licenses: " + ", ".join(unknown_licenses))
            if issues:
                raise Blocked("; ".join(issues))
            report["status"] = "passed"
            save()
            if promote:
                stage_repo = self.ak.request("/repositories/pypi-staging")
                release_repo = self.ak.request("/repositories/pypi-release")
                members = self.ak.request("/repositories/pypi/members")["members"]
                if stage_repo["is_public"] or not release_repo["promotion_only"] or {m["member_repo_key"] for m in members} != {"pypi-release"}:
                    raise Blocked("Repository topology no longer isolates unapproved content")
                # Re-read the stored identity immediately before promotion.
                stored = self.ak.request("/artifacts/" + artifact_id)
                if stored.get("checksum_sha256") != digest:
                    raise Blocked("Staged artifact changed during inspection")
                self.ak.request("/quarantine/" + artifact_id + "/release", "POST", {})
                try:
                    promoted = self.ak.request("/promotion/repositories/pypi-staging/artifacts/" + artifact_id + "/promote", "POST", {"target_repository": "pypi-release", "skip_policy_check": False, "notes": "Audit sha256=" + digest + "; DT project=" + project_id})
                except urllib.error.HTTPError as exc:
                    if exc.code != 409:
                        raise
                    existing = self.ak.request("/repositories/pypi-release/artifacts/" + path)
                    if existing.get("checksum_sha256") != digest:
                        raise Blocked("Existing release does not match audited digest") from exc
                    promoted = {"promoted": True, "already_present": True}
                if not promoted.get("promoted"):
                    raise Blocked("Promotion policy refused release: " + json.dumps(promoted))
                report["promotion"] = promoted
                released = self.ak.request("/repositories/pypi-release/download/" + path, raw=True)
                if hashlib.sha256(released).hexdigest() != digest:
                    raise Blocked("Released bytes do not match audited digest")
                report["status"] = "promoted"
                report["promotion"] = promoted
            save()
            print(json.dumps(report), flush=True)
            return report
        except Exception as exc:
            report["status"] = "blocked"
            report["error"] = str(exc)
            if report.get("promotion", {}).get("promoted"):
                try:
                    released_artifact = self.ak.request("/repositories/pypi-release/artifacts/" + path)
                    self.ak.request("/quarantine/" + released_artifact["id"] + "/quarantine", "POST", {"reason": "Post-promotion verification failed: " + str(exc)[:800]})
                    report["release_quarantined"] = True
                except Exception:
                    report["release_quarantine_failed"] = True
            if artifact_id:
                try:
                    self.ak.request("/quarantine/" + artifact_id + "/quarantine", "POST", {"reason": str(exc)[:1000]})
                except Exception:
                    report["quarantine_update_failed"] = True
            save()
            print(json.dumps(report), flush=True)
            raise


def submit(state, requirements, target=None):
    """Resolve dependencies in an isolated pip invocation; never build sdists."""
    queue = Path(state) / "queue"
    queue.mkdir(parents=True, exist_ok=True)
    if any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*(?:\[[A-Za-z0-9_,.-]+\])?==[A-Za-z0-9][A-Za-z0-9.!+_-]*", r) for r in requirements):
        raise Blocked("Submit explicit package==version requirements; URLs and pip options are not accepted")
    with tempfile.TemporaryDirectory() as tmp:
        env = {k: v for k, v in os.environ.items() if not k.startswith("PIP_")}
        target_args = []
        for key, value in (target or {}).items():
            if value:
                target_args.extend(["--" + key.replace("_", "-"), value])
        subprocess.run([sys.executable, "-m", "pip", "--isolated", "--disable-pip-version-check", "download", "--no-cache-dir", "--only-binary=:all:",
                        "--index-url", "https://pypi.org/simple/", "--dest", tmp, *target_args, *requirements], env=env, check=True)
        for path in sorted(Path(tmp).glob("*.whl")):
            metadata = wheel_metadata(path.read_bytes())
            job = {"package": normalized(metadata["Name"]), "version": metadata["Version"], "filename": path.name}
            target = queue / (hashlib.sha256(json.dumps(job, sort_keys=True).encode()).hexdigest() + ".json")
            temporary = target.with_suffix(".tmp")
            temporary.write_text(json.dumps(job))
            temporary.replace(target)
            print("Queued", job["package"], job["version"], path.name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", default=os.getenv("AUDIT_STATE", ".local/pypi-audit"))
    sub = parser.add_subparsers(dest="command", required=True)
    enqueue = sub.add_parser("submit")
    enqueue.add_argument("requirements", nargs="+")
    for option in ["python-version", "platform", "implementation", "abi"]:
        enqueue.add_argument("--" + option)
    audit = sub.add_parser("audit")
    audit.add_argument("package")
    audit.add_argument("version")
    audit.add_argument("filename")
    audit.add_argument("--promote", action="store_true")
    watch = sub.add_parser("watch")
    watch.add_argument("--once", action="store_true")
    refresh = sub.add_parser("refresh-db")
    refresh.add_argument("--once", action="store_true")
    retry = sub.add_parser("retry-blocked")
    retry.add_argument("--package")
    args = parser.parse_args()
    if args.command == "retry-blocked":
        queue = Path(args.state) / "queue"
        queue.mkdir(parents=True, exist_ok=True)
        for path in sorted((Path(args.state) / "blocked").glob("*.json")):
            job = json.loads(path.read_text())
            if args.package and normalized(job["package"]) != normalized(args.package):
                continue
            path.replace(queue / path.name)
            print("Requeued", job["package"], job["version"])
        return
    if args.command == "refresh-db":
        while True:
            result = subprocess.run([os.getenv("GRYPE_BINARY", "/usr/local/bin/grype"), "db", "update"], timeout=300, check=False)
            if args.once:
                if result.returncode:
                    raise Blocked("Grype database refresh failed")
                return
            time.sleep(21600 if result.returncode == 0 else 60)
    if args.command == "submit":
        submit(args.state, args.requirements, {k: getattr(args, k) for k in ["python_version", "platform", "implementation", "abi"]})
        return
    worker = Worker(args.state)
    if args.command == "audit":
        worker.audit(args.package, args.version, args.filename, args.promote)
        return
    while True:
        queue = worker.state / "queue"
        queue.mkdir(exist_ok=True)
        for path in sorted(queue.glob("*.json")):
            try:
                job = json.loads(path.read_text())
                worker.audit(job["package"], job["version"], job["filename"], promote=True)
                destination = worker.state / "done"
            except Exception as exc:
                print("Audit blocked:", str(exc), file=sys.stderr, flush=True)
                destination = worker.state / "blocked"
            destination.mkdir(exist_ok=True)
            path.replace(destination / path.name)
        if args.once:
            return
        time.sleep(10)


if __name__ == "__main__":
    main()
