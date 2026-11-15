# Verification

Validated on 2026-11-15.

- 14 automated tests pass locally and in GitHub Actions.
- Integration test with an isolated HTTP destination exercises likes → album search → album request, followed by a process restart without duplicate delivery.
- Provider tests cover Yandex metadata batches, Spotify refresh and pagination, and Apple playlist pagination.
- Authentication, CSRF, Origin validation, encrypted credentials, secret redaction, one-use OAuth state, baseline import, retry backoff, concurrency, and uncertain-delivery recovery are covered.
- JavaScript syntax check and Linux amd64 Docker build pass.
- Browser validation covers sign-in, overview, queue, Spotify connection form, settings save, and the destination connection check.
- Native x86_64 deployment passes health checks, HTTPS sign-in, destination authentication, live album search, and restart persistence checks.

## Memory measurements

- Idle container: approximately **32 MiB**, hard limit **128 MiB**, no container swap.
- Separate isolated native container, no network or user database: **10,000 synthetic likes**, Yandex client module loaded, **54.5 MiB peak process RSS**, **13.2 seconds** at 0.5 CPU.
- The benchmark measures storage and processing, not a live streaming-service account. Very large Yandex ID lists can need more memory.
- The amd64 image also passed local container HTTP checks on an ARM Mac with a separate 384 MiB emulation allowance. The original 128 MiB emulated run hit OOM; native measurements above are the relevant deployment results.

## Limits of verification

Personal streaming accounts have not been authorized. A real personal like followed by an actual music download has therefore not been tested. Provider behavior is covered with isolated API responses; the deployed destination was checked with real authentication and search. No unrelated music was requested for testing.

The deployed destination uses the v1 API. The configurable v3 option has not been validated against a v3 instance.

## Album artwork update

- 16 tests pass, including upgrading an existing database and refreshing artwork without resetting queue status or delivery history.
- Spotify, Yandex and Apple adapters retain album artwork; ranked destination candidates retain artwork metadata.
- Queue thumbnails load lazily in the browser. Missing or broken images use a fixed-size placeholder; no image cache or additional service is introduced.

## Like-date ordering update

- 19 tests pass. Added coverage for source timestamps and timezone conversion, mixed-provider date ordering across pages, and Yandex metadata arriving in a different order from liked-track IDs.
- The database upgrade preserves existing queue/delivery states and fills dates during the next provider synchronization.
- Queue and processing order use the real source like timestamp, newest first. Missing source dates are shown explicitly and sorted last; import time is not used as a substitute.
