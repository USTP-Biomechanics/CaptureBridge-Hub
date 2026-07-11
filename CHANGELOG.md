# Changelog

## v1.0.9 - 2026-07-11

- Require Pillow 12.2 or newer and build the portable release from an exact,
  audited dependency set.
- Accept file payloads only after an operator-requested transfer, enforce
  per-file and per-request size limits, and publish files atomically after a
  matching FILE_DONE.
- Remove partial files after interrupted or invalid transfers.
- Add transfer-hardening tests, push/pull-request CI, dependency auditing, and
  a SHA-256 checksum for the portable release archive.
- Document the trusted-network threat model and publish the manuscript bench
  observation protocol and summary.

## v1.0.8 - 2026-07-11

- Display Android battery level and charging state.
- Harden protocol-line handling and UI metadata updates.
- Run the Hub unit-test suite in release builds.

## v1.0.7 - 2026-07-01

- Add USB/ADB reverse control transport and TCP live preview over USB.
- Add background clock-offset estimation and scheduled phone-clock capture
  boundaries.

## v1.0.6 - 2026-06-27

- Expand lag-test timing, segment, display, and analyzer diagnostics.
