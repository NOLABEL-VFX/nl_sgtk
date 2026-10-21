# SGTK 1.7.0.0 publication contracts

This release contains the five requested changes and retains previously
deployed noninteractive authentication, Prepro and recovery behavior in the
Git baseline. It introduces no new login flow or tracker schema migration.

## Version identity

Core snapshots and published-reference catalogues share one parser:
`comp_v005a` produces number 5, variant `a`, label `v005a`. An optional
single-letter suffix is lowercase; the last valid delimited version token
wins. A missing valid token produces zero and empty variant/label.

## Explicit file roles and slate

`metadata.file_roles` maps source paths to existing Version source fields:
`sg_path_to_frames`, `sg_path_to_movie`, `sg_path_to_geometry` or
`sg_path_to_script`. It uses the existing explicit publisher override;
unmapped files retain extension classification. Unsupported roles,
malformed mappings and conflicting normalized paths fail before login.
No PublishedFileType is created or changed by this override.

`metadata.has_slate` accepts only boolean true/false; absence keeps the
publisher default. Both explicit values reach `Version.sg_movie_has_slate`.
UUID retries reuse the existing Version rather than rewriting its slate.

## Description and authentication

`ShotgunPublish.validate()` requires three characters after `.strip()`;
`nl_sgtk.MIN_DESCRIPTION_LENGTH` and the class attribute expose that value.
The supplied text is retained. Trusted/validation-bypass paths keep their
existing contract. See [API validation](../API.md#publish-request-validation-1700).

Regression tests ensure noninteractive failures do not start login UI or
Toolkit renewal. Prepro tests cover one project-local library, missing or
ambiguous links and rejection of a library belonging to another project.
Those production behaviors were already deployed before this release.

## Recorded verification

- Windows Python 3.12.9: all 177 tests passed; four Linux-only cases skipped.
- Validation tests use injected tracker clients and do not publish real data.
- Release source syntax is checked against the declared Python 3.8 minimum.
- Wheel/runtime checks and fresh shared-import test results are retained
  with the deployment audit and SHA-256 backup manifest.

Restart Hub/DCC clients to load the new shared dependency. No running
service, project configuration, source media or farm job is changed.
Rollback restores the backed-up package files and restarts affected clients.
