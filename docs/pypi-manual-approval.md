# PyPI manual approval on the local instance

For the current frontend-driven example, see
[Manual PyPI audit and promotion](pypi-frontend-manual-review.md).
The automatic promoter is stopped and staging now requires native promotion
approval. The setup history below predates this change.

## Active audit workflow as of 2026-10-28

The active setup uses an automatic worker: submit pinned requirements, let it
download and quarantine the wheels in private `pypi-staging`, run AK scans and
Dependency-Track analysis, and promote passing distributions to `pypi-release`.
The public `pypi` virtual repository contains only `pypi-release`. No manual
uploads are required. The upstream remote is now private.

See [the audit worker guide](../scripts/pypi-audit/README.md) for submission
commands, policies, reports, platform selection, and verification evidence.
The Age Gate CSV describes the earlier queue and is not the audit report store.

The remainder of this document records earlier configurations and is historical.

## Previous configuration: Age Gate

The separate **Age Gate** feature provides the manual review workflow that
the curation catalog does not. On 2026-10-27 the configuration was changed:

- Public virtual repository `pypi` contains public remote `pypi-approved`.
- Index: `http://localhost/pypi/pypi/simple/`.
- Remote Age Gate: enabled, `mode: first_seen`, `min_age_days: 3650`.
- The earlier block-all curation rule was deleted after enabling Age Gate.
- The separate Package Age Policy was left unchanged.

First-seen mode holds newly encountered versions even if they were published
years ago. It is a time-limited approximation of manual-only approval:
versions can automatically mature after 3650 days from first observation.
No staging repository is needed for this Age Gate workflow.

Requesting the requests project index created 160 pending version entries.
A direct request for `requests-2.32.3-py3-none-any.whl` through the public
virtual returned HTTP 451, `age_gate_blocked`, with pending review ID
`f3331e4d-5495-419c-bd27-164a777f411c`. No version was manually approved.
Project discovery can queue many historical versions, not only the version
eventually selected by pip.

Use the **Age Gate review queue**, not the curation package catalog. Its API:

```text
GET /api/v1/admin/age-gate/reviews?repository_key=pypi-approved&status=pending
POST /api/v1/admin/age-gate/reviews/{id}/approve
```

Both endpoints require administrator authentication. Approval accepts a JSON
body such as `{"reason":"Dependency reviewed"}`. Review each transitive
dependency version too, then retry the download:

```bash
pip download requests==2.32.3 \
  --index-url http://localhost/pypi/pypi/simple/ \
  --dest ./downloads
```

The notes below describe the superseded curation-only setup and its limits;
their limitations do not describe the separate Age Gate review system.

## Historical curation-only setup (superseded)

Configured on 2026-10-27:

- Repository: `pypi-approved` (`8810e177-93cd-4411-b1c0-b577701212df`).
- Type: private PyPI remote, upstream `https://pypi.org`.
- Client index: `http://localhost/pypi/pypi-approved/simple/` (authentication required).
- Curation enabled; default action `review`.
- Repository-specific explicit block rule: `2329364f-34fd-4366-9b06-150616dd91d8`, package/version/architecture `*`, priority `1000000`.

No dependencies have been approved. Authenticated requests for both
`simple/requests/` and `simple/requests/requests-2.32.3-py3-none-any.whl`
returned HTTP 403 with `curation_blocked`.

A public virtual PyPI repository, `pypi`
(`e73e9cc4-0fbc-4939-95ac-43b5e69bb610`), was subsequently created with
`pypi-approved` as its only member (priority 1). Its index URL is
`http://localhost/pypi/pypi/simple/`.

The remote remains private. This version checks member visibility when
walking virtual repositories, so an anonymous project request currently
returns HTTP 404, `Virtual repository has no accessible members`. Automatic
approval review rejected making the remote public because that separately
exposes its direct proxy endpoint. That access change awaits explicit user
approval; the remote's curation block remains enabled.

## What this configuration supports

An administrator can manually create higher-priority `allow` curation rules
scoped to the remote repository ID using `POST /api/v1/curation/rules`.
Lower priority numbers run first. The API calls the scope field
`staging_repo_id`, but the foreign key accepts the remote repository ID and
the proxy evaluates rules against that ID.

A package-wide allow rule with `version_constraint: "*"` approves **every
version, including future releases**. That does not satisfy approval of each
new dependency version. No such allow rule was created.

A version-specific allow rule alone does not provide a usable pip workflow:
the project index request has no version, so it skips that rule and hits the
block-all rule. Do not solve this by adding a package-wide wildcard allow
without accounting for its permission to download future versions.

## Current implementation limits

- `backend/src/api/handlers/proxy_helpers.rs`, `evaluate_curation`: only an
  explicit `block` outcome denies a download; `review` permits it. Evaluation
  errors also fail open. Consequently this is a verified block-rule setup,
  not a fail-closed approval security boundary.
- `backend/src/services/curation_service.rs`, `evaluate_pep503_package`: the
  proxy evaluates rules, not approval status in `curation_packages`.
- `backend/src/api/handlers/curation.rs`, `approve_package`: approval changes
  catalog status; it does not create a proxy allow rule.
- PyPI on-demand catalog ingestion happens after a successful proxy response.
  Blocked requests do not automatically populate a pending approval queue.

Creating a staging repository and selecting Review therefore does not by
itself enforce approval before upstream downloads or serving.

## Choosing a workflow

For manual review before **consumer access**, use a private intake remote,
a private staging repository, and a local approved repository. Reviewers
fetch and upload candidate distributions to staging, then manually promote
approved artifacts to the local repository. Point consumers only at that
local index, with no upstream remote member or extra PyPI index. Restrict
intake/staging access to reviewers. This permits reviewer downloads before
approval and requires approval for each transitive dependency as well.

For approval before the proxy itself downloads an artifact, the PyPI gate
needs implementation changes: consult approved name/version (and preferably
distribution hash) records, fail closed on pending/missing/error states,
provide metadata-only discovery and an approval queue, and enforce the
decision on index links, direct downloads, cached responses, and virtual
repository members. The new remote remains blocked pending that workflow;
no staging or approved-local repositories were created.
