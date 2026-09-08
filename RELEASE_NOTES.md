# nl_sgtk 1.0.0.1

Released source build: 2026-09-08.

- Resolve exact Version launch sources and entity Tasks; supply direct Scene/Sequence relationships and scoped input discovery.
- Preserve exact input links and source metadata during review publishing, with UUID-based retry lookup and local manifest mirroring.
- Allow geometry-only exports without previews to complete locally when the project has Core configuration.
- Synchronize package/runtime/legacy-shim versions and compare three/four-component versions consistently.

## Compatibility

This is the 1.0 consolidation of previously unreleased work. Consumers must handle local_only results without a Version id. Use registration="tracker" when a tracker Version is explicitly required. Core manifest support requires the matched nl_core package and a configured project. Version.sg__publish_uuid must exist for the configured UUID publishing workflow.

## Validation

104 automated tests passed; tracker calls in these tests use controlled fixtures. No live tracker records were created by this release pass.

## Known limitations

UUID retries do not replace studio schema provisioning or a coordinated deployment. Manifest indexing failures after Version creation are logged for explicit repair. PublishedFile migration is deferred.
