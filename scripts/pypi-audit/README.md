# Automated PyPI intake and security audit

**Current deployment mode:** automatic promotion is stopped and
`pypi-staging` now requires native manual promotion approval. Follow the
[frontend manual-review guide](../../docs/pypi-frontend-manual-review.md) for
the prepared `six==1.16.0` example. The commands below describe the earlier
automated mode; restarting its worker does not bypass the native approval gate.
The `scanner-db` service remains active.

This instance now uses the following flow:

```text
Submit pinned requirements (CI or an operator)
  → resolve wheel dependencies from PyPI
  → verify each distribution's SHA-256 and archive metadata
  → upload automatically to private pypi-staging and hold in quarantine
  → run AK dependency + Grype scans
  → generate a CycloneDX SBOM and analyze it in Dependency-Track
  → enforce vulnerability, license, and Dependency-Track policies
  → promote identical bytes to pypi-release
  → serve through the public pypi virtual repository
```

No manual artifact uploads are needed. The worker downloads and stages files.
Only `pypi-release` is a member of the public `pypi` virtual repository.
`pypi-approved` remains an intake remote, now private. Existing Age Gate
decisions are preserved but do not authorize releases in this workflow.

## Start and submit

From the repository root, start the persistent services:

```bash
docker compose -f scripts/pypi-audit/compose.yml up -d
```

Submit pinned requirements, including their resolved transitive dependencies:

```bash
docker compose -f scripts/pypi-audit/compose.yml exec -T worker \
  python worker.py submit 'requests==2.32.3'
```

By default resolution targets the worker's Python 3.13 Linux environment.
For this workstation's Python 3.14 Linux client, submit the matching variants:

```bash
docker compose -f scripts/pypi-audit/compose.yml exec -T worker \
  python worker.py submit --python-version 3.14 --implementation cp \
  --abi cp314 --platform manylinux_2_28_x86_64 'requests==2.32.3'
```

Use separate submissions for other supported platforms/Python versions.
Requirements must be explicitly pinned (`package==version`); URLs, pip options,
and source distributions are not accepted. Wheels are inspected without
installing them or running their build scripts.

Client misses do **not** automatically enqueue packages. Have CI submit pinned
dependencies before installing from AK. Clients use only the approved index:

```bash
pip download --only-binary=:all: requests==2.32.3 \
  --index-url http://localhost/pypi/pypi/simple/ --dest ./downloads
```

Do not add PyPI as an extra index if installs must use only audited artifacts.

## Policies and evidence

The worker requires both `dependency` and `grype` scans to complete with
complete coverage and a vulnerability DB timestamp no older than seven days.
All other configured scanner results must be terminal and none may fail.
Scanners not applicable to Python wheels, such as OpenSCAP, do not count as
completed Python security checks.

Current release policy:

- Block High/Critical AK or Dependency-Track vulnerability findings.
- Block unknown/ungraded Dependency-Track vulnerability severities.
- Block every reported Dependency-Track policy violation.
- Require every SBOM component's licenses to be explicitly allowed.
- Block absent/empty SBOMs, missing required scanners, incomplete scans,
  stale Grype data, API errors, analysis timeouts, and uninitialized PyPI
  advisory data.
- Verify the staging hash before promotion and the downloaded release hash
  afterward; quarantine the release if the latter verification fails.

Low/Medium vulnerabilities can pass this policy. In particular, an artifact
being promoted does not mean it has no vulnerabilities. Defaults allow
`Apache-2.0`, `BSD-2-Clause`, `BSD-3-Clause`, `MIT`, `ISC`, `MPL-2.0`, and
`PSF-2.0`; unknown identifiers and unapproved license expressions stay blocked.
Change `AUDIT_ALLOWED_LICENSES` in the worker's environment to adopt an
explicitly reviewed license policy.

Dependency-Track projects are named `ak-audit/<package>/<sha256>` and tagged
`ak-pypi-audit`, with one project per distribution digest. The high/critical
Dependency-Track policy is scoped to this tag. Analysis is awaited explicitly;
AK's automatic, best-effort SBOM submission is not used as proof of approval.
These SBOMs describe individual wheel inventories, not a complete application
dependency graph; the submission command audits the resolved wheels separately.

The scanner refresher updates Grype's database every six hours on the shared
`artifact-keeper_scan_workspace` volume. The backend is configured to read
`/scan-workspace/grype-db`. Dependency-Track mirrors PyPI and crates.io OSV
advisories; the installed 4.14 version requires a semicolon-separated ecosystem
setting (`PyPI;crates.io`). A PyPI advisory canary checks initial readiness but
does not prove that the whole mirror remains current. Monitor mirror failures
in Dependency-Track as part of operating the service.

## Review results and retry

Reports and the exact uploaded CycloneDX documents are stored locally:

```text
.local/pypi-audit/reports/<sha256>/report.json
.local/pypi-audit/reports/<sha256>/bom.cdx.json
.local/pypi-audit/queue/       pending or in-progress jobs
.local/pypi-audit/done/        promoted jobs
.local/pypi-audit/blocked/     jobs requiring investigation or a retry
```

`report.json` includes scan results, Dependency-Track project/findings/policy
violations, the artifact hash, and the release decision. Secrets and runtime
state are under ignored `.local/pypi-audit`; they are not committed.

```bash
docker compose -f scripts/pypi-audit/compose.yml logs --tail 50 worker
docker compose -f scripts/pypi-audit/compose.yml exec -T worker \
  python worker.py retry-blocked --package requests
```

For a manual audit without promotion:

```bash
docker compose -f scripts/pypi-audit/compose.yml exec -T worker \
  python worker.py audit requests 2.32.3 requests-2.32.3-py3-none-any.whl
```

The persistent `watch` worker automatically promotes passing jobs. Keep it
stopped and use manual `audit` followed by `audit --promote` if every passing
artifact also needs human approval. Retrying always runs a fresh audit; it does
not promote based on a saved report. Published artifacts are immutable, and
retries recognize an existing release only if the hash matches.

## Provisioning and runtime credentials

`configure.py` provisions the repositories and audit policies idempotently.
It reads the AK token from `AK_API_TOKEN_FILE`, defaulting to
`.local/pypi-audit/secrets/ak-api-token`. It prompts for the Dependency-Track
admin password, or reads `DTRACK_ADMIN_PASSWORD` from the environment. It does
not store the admin password. The running worker uses the existing integration
API key in `.local/pypi-audit/secrets/dtrack-api-key`, not the DT admin password.

```bash
python3 scripts/pypi-audit/configure.py --cutover
```

`--cutover` replaces the public virtual repository's complete member list with
`pypi-release` and makes the intake remote private. Without it, provisioning
leaves the public member list unchanged. Restart Dependency-Track after changing
its mirror ecosystems to start the initial synchronization immediately.

The Compose file uses UID 1000 for worker state and UID 1001 for the backend's
scanner volume; adjust these for a different host/deployment. The refresher
uses a local copy of the backend's Grype binary. After upgrading the backend,
refresh that copy and recreate the audit services:

```bash
docker cp artifact-keeper-backend:/usr/local/bin/grype .local/pypi-audit/bin/grype
docker compose -f scripts/pypi-audit/compose.yml up -d --force-recreate
```

Keep the backend `GRYPE_DB_CACHE_DIR` override in the active Compose file. Losing
the database volume or credentials causes audits to block until restored.

## Verification on 2026-10-28

- Eleven automated tests passed, including outages, stale databases,
  incomplete scans, unknown licenses, and release-integrity failures.
- Anonymous pip downloaded `six==1.17.0` and `requests==2.32.3` with all four
  resolved dependencies through AK alone; downloaded hashes matched audit
  reports. Python 3.13 and 3.14 charset-normalizer variants were audited.
- `requests==2.19.1` was blocked by AK and Dependency-Track (10 findings and
  one policy violation) and never entered the release repository.
- Anonymous requests to intake/staging returned 401; both public protocol
  and generic download paths refused the blocked artifact. Even an
  authenticated staging download remained blocked by quarantine (409).

See [verification.json](verification.json) for the recorded hashes and probes.

This covers the configured vulnerability, SBOM, license, integrity, and archive
checks. It does not provide malware detection, prove code is benign, verify all
publisher attestations, support source builds, automatically revoke releases
on later CVEs, or intercept arbitrary pip misses. Promotion and DT policy
orchestration are enforced by this worker; use it as the release path and
restrict other promotion credentials accordingly.
