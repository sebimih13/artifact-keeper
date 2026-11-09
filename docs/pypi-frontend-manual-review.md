# Manual PyPI audit and promotion in the frontend

## Prepared example

`six==1.16.0` has been uploaded to **pypi-staging**, with SHA-256 verified
against PyPI. It has not been scanned, approved, or promoted.

| Field | Value |
|---|---|
| File | `six-1.16.0-py2.py3-none-any.whl` |
| Stored path | `six/1.16.0/six-1.16.0-py2.py3-none-any.whl` |
| Artifact ID | `7c496d50-3677-49d2-8eae-5ee7786984cd` |
| Pending approval ID | `a3de7ab2-d647-425c-95c2-52b9c220f887` |
| Requester | `svc-pypi-manual-requester` |
| Release target | `pypi-release` |
| Quarantine | Indefinite admin hold; downloads blocked |

The automatic promotion worker is stopped. The scanner database refresher
remains running. The requester service account has read permission on staging
and a repository-restricted, read-only token that expires after seven days.
It can submit a promotion request but cannot approve it. Its token is stored
only in ignored `.local/pypi-audit/secrets/manual-requester-token`.

## Repository configuration

The staging repository is configured with:

- Format **PyPI**, repository type **Staging**, private visibility.
- Quarantine enabled, with an indefinite hold applied to this example.
- Security scanning enabled; scan on upload disabled so you can trigger it.
- Linked release target **pypi-release**.
- Native promotion approval required (`require_approval=true`). This build
  has no API/UI setter for this field; it was set directly on this repository
  in the database.
- A **PyPI manual approval scan gate** promotion rule with automatic
  promotion disabled. It blocks genuinely unscanned artifacts and findings
  above Medium severity. The source's existing scan policy also blocks
  unscanned/failed scans and High/Critical findings.

The public virtual **pypi** repository contains only **pypi-release**.
The staging and intake remote repositories remain private.

## 1. Locate the staged wheel

Open [pypi-staging](http://localhost/repositories/pypi-staging) as an admin.
Find the artifact at `six/1.16.0/six-1.16.0-py2.py3-none-any.whl`.
It is expected to be quarantined and unscanned at this point.

## 2. Trigger security scanning

Open [Security → Scans](http://localhost/security/scans):

1. Click **Trigger Scan**.
2. Choose **Specific Artifact** as the scan mode.
3. Choose **pypi-staging** as the repository.
4. Choose `six`, version `1.16.0` / the wheel filename. Match the artifact ID
   above if more than one six version is shown.
5. Submit the dialog.
6. Wait for the dependency and Grype scans to finish. Check the individual
   results and findings, rather than only a summary badge.

OpenSCAP/Incus and other format-specific scanners can report Not Applicable
for a Python wheel. A failed or incomplete applicable scan requires
investigation; do not approve on that basis.

## 3. Review the SBOM and Dependency-Track analysis

There is no separate DT start command needed for this hosted artifact.
After scanning, AK generates a CycloneDX SBOM from the scan inventory and
submits it to Dependency-Track automatically when the integration is enabled.

Open [Security → DT Projects](http://localhost/security/dt-projects). Search
for **six** and select version **1.16.0**. Native AK submission uses the package
name/version, not the earlier worker's `ak-audit/...` project names.

Wait for the project to appear and its analysis to finish. Inspect the
components/SBOM inventory, vulnerability findings, and policy violations.
Refresh after the import if results have not arrived yet. The DT interface
is also available at [localhost:8093](http://localhost:8093).

An AK scan finishing successfully does not prove DT submission or analysis
succeeded: native submission is best-effort. A missing project, an integration
error, or unfinished analysis is a reason to keep the artifact pending, not
evidence that it is clean. DT policy enforcement must be checked manually in
this frontend-only workflow; the promotion rule is an AK scan gate, not a
DT-completion gate.

## 4. Manually approve the promotion

Only after reviewing the current scans and Dependency-Track results:

1. Open [Approval Queue](http://localhost/approvals).
2. Find the pending **pypi-staging → pypi-release** request for artifact
   `7c496d50-3677-49d2-8eae-5ee7786984cd`.
3. Choose its **Approve** action, review the **Approve Promotion** dialog,
   and add notes describing the security/DT review.
4. Confirm approval.

Approval executes the promotion; do not perform a separate direct promotion
first. The original staged artifact can remain held in private staging;
the approved copy is published in pypi-release. Do not use a policy bypass.

The request was deliberately created before scanning. Its displayed policy
result is a snapshot and can initially say **unscanned** even after scans
finish. Review the current scan results. The promotion rule is re-evaluated
when you approve and rejects this artifact while it is genuinely unscanned.

AK requires different principals for requesting and approving. The prepared
request came from the limited service account so your admin account can
review it. Attempting to approve a request you created yourself is rejected.

## 5. Verify client access after your approval

```bash
pip download --no-deps six==1.16.0 \
  --index-url http://localhost/pypi/pypi/simple/ \
  --dest ./downloads
```

Before approval this version is absent from the public index. After
successful approval/promotion the command should download the approved wheel.

## Further artifacts

Upload additional wheels through the staging repository's upload action,
keeping package/version/filename coordinates consistent, and repeat scanning
and review. This build exposes the frontend approval queue, but its deployed
frontend has no promotion-request creation action. A requester must create
the request using `POST /api/v1/approval/request`, for example:

```bash
curl --fail-with-body -X POST http://localhost/api/v1/approval/request \
  -H "Authorization: Bearer $AK_REQUESTER_TOKEN" \
  -H 'Content-Type: application/json' \
  --data '{
    "source_repository": "pypi-staging",
    "target_repository": "pypi-release",
    "artifact_id": "REPLACE_WITH_UPLOADED_ARTIFACT_ID",
    "skip_policy_check": false,
    "notes": "Scans and Dependency-Track results ready for reviewer inspection"
  }'
```

Prefer creating future requests after scanning to capture a current policy
snapshot. Use a different admin account to approve them in the frontend.

## Checks performed while preparing this example

- Artifact is present in staging with the expected PyPI SHA-256.
- Zero scans exist for it, leaving the scan trigger for you.
- Quarantine status is `quarantined`, no expiry, `is_blocked=true`.
- Native approval status is `pending`.
- Read-only promotion-rule evaluation reports `never_scanned` and fails.
- Anonymous public direct download returns 404.

See [pypi-manual-example.json](pypi-manual-example.json) for the recorded IDs
and checks. No approval or promotion was performed while preparing the example.
