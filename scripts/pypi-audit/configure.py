#!/usr/bin/env python3
"""Provision the audit repositories and DT policy; optionally cut over clients."""
import argparse
import getpass
import json
import os
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request

from worker import Client


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cutover", action="store_true", help="Replace public pypi members with pypi-release; make intake private")
    args = parser.parse_args()
    state = Path(os.getenv("AUDIT_STATE", ".local/pypi-audit"))
    ak_token = os.getenv("AK_API_TOKEN") or Path(os.getenv("AK_API_TOKEN_FILE", str(state / "secrets/ak-api-token"))).read_text().strip()
    ak = Client(os.getenv("AK_URL", "http://localhost") + "/api/v1", {"Authorization": "Bearer " + ak_token})
    repos = {}
    for key, typ, public in [("pypi-staging", "staging", False), ("pypi-release", "local", True)]:
        try:
            repo = ak.request("/repositories/" + key)
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise
            repo = ak.request("/repositories", "POST", {"key": key, "name": key, "format": "pypi", "repo_type": typ, "is_public": public, "promotion_only": typ == "local"})
        if repo["repo_type"] != typ or repo["format"] != "pypi":
            raise RuntimeError("Unexpected repository type/format: " + key)
        repos[key] = repo
        ak.request("/repositories/" + key, "PATCH", {"is_public": public, "promotion_only": typ == "local"})
    ak.request("/repositories/pypi-staging", "PATCH", {"quarantine_enabled": True, "quarantine_duration_minutes": 5256000, "release_repository_key": "pypi-release"})
    ak.request("/repositories/pypi-staging/security", "PUT", {"scan_enabled": True, "scan_on_upload": False, "block_on_policy_violation": True, "severity_threshold": "high"})
    policies = ak.request("/security/policies")
    if not any(p["name"] == "PyPI audit release gate" and p.get("repository_id") == repos["pypi-staging"]["id"] for p in policies):
        ak.request("/security/policies", "POST", {"name": "PyPI audit release gate", "repository_id": repos["pypi-staging"]["id"], "max_severity": "high", "block_unscanned": True, "block_on_fail": True})

    dt_base = os.getenv("DTRACK_URL", "http://localhost:8092") + "/api/v1"
    password = os.getenv("DTRACK_ADMIN_PASSWORD") or getpass.getpass("Dependency-Track admin password: ")
    form = urllib.parse.urlencode({"username": "admin", "password": password}).encode()
    with urllib.request.urlopen(urllib.request.Request(dt_base + "/user/login", data=form), timeout=30) as response:
        token = response.read().decode().strip()
    dt = Client(dt_base, {"Authorization": "Bearer " + token})
    team = next(t for t in dt.pages("/team") if t["name"] == "Automation")
    try:
        dt.request("/permission/VIEW_POLICY_VIOLATION/team/" + team["uuid"], "POST")
    except urllib.error.HTTPError as exc:
        if exc.code not in (304, 409):
            raise
    dt.request("/tag", "PUT", ["ak-pypi-audit"])
    name = "AK audited PyPI high and critical vulnerabilities"
    policy = next((p for p in dt.pages("/policy") if p["name"] == name), None)
    if policy is None:
        policy = dt.request("/policy", "PUT", {"name": name, "operator": "ANY", "violationState": "FAIL", "global": False})
    existing = {c["value"] for c in policy.get("policyConditions", []) if c.get("subject") == "SEVERITY" and c.get("operator") == "IS"}
    for severity in {"HIGH", "CRITICAL"} - existing:
        dt.request("/policy/" + policy["uuid"] + "/condition", "PUT", {"subject": "SEVERITY", "operator": "IS", "value": severity})
    try:
        dt.request("/policy/" + policy["uuid"] + "/tag/ak-pypi-audit", "POST")
    except urllib.error.HTTPError as exc:
        if exc.code not in (304, 409):
            raise
    config = dt.request("/configProperty")
    prop = next(p for p in config if p["propertyName"] == "google.osv.enabled")
    ecosystems = set(filter(None, prop.get("propertyValue", "").replace(",", ";").split(";"))) | {"PyPI"}
    dt.request("/configProperty", "POST", {"groupName": "vuln-source", "propertyName": "google.osv.enabled", "propertyValue": ";".join(sorted(ecosystems))})

    if args.cutover:
        virtual = ak.request("/repositories/pypi")
        if virtual["format"] != "pypi" or virtual["repo_type"] != "virtual":
            raise RuntimeError("pypi is not a PyPI virtual repository")
        # Full-set replacement removes all upstream fallback paths.
        ak.request("/repositories/pypi/members", "PUT", {"members": [{"member_key": "pypi-release", "priority": 1}]})
        ak.request("/repositories/pypi", "PATCH", {"is_public": True})
        ak.request("/repositories/pypi-approved", "PATCH", {"is_public": False})
    print(json.dumps({"repositories": {k: v["id"] for k, v in repos.items()}, "dtrack_policy": policy["uuid"], "cutover": args.cutover}, indent=2))


if __name__ == "__main__":
    main()
