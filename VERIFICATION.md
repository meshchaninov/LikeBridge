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

The destination was migrated to DroppedNeedle v3.0.0 on 2026-11-15. Bearer authentication, account probe, album search and track previews pass against the live v3 instance. Album and track intake paths have adapter coverage; v1 behavior remains covered.

## Album artwork update

- 16 tests pass, including upgrading an existing database and refreshing artwork without resetting queue status or delivery history.
- Spotify, Yandex and Apple adapters retain album artwork; ranked destination candidates retain artwork metadata.
- Queue thumbnails load lazily in the browser. Missing or broken images use a fixed-size placeholder; no image cache or additional service is introduced.

## Like-date ordering update

- 19 tests pass. Added coverage for source timestamps and timezone conversion, mixed-provider date ordering across pages, and Yandex metadata arriving in a different order from liked-track IDs.
- The database upgrade preserves existing queue/delivery states and fills dates during the next provider synchronization.
- Queue and processing order use the real source like timestamp, newest first. Missing source dates are shown explicitly and sorted last; import time is not used as a substitute.

## Short track previews

- Previews are resolved on demand via the existing destination, never downloaded or proxied through this application. A single browser player limits playback to 30 seconds. Every track also has a YouTube search link and an original-service link when available.
- Tests cover authentication, explicit/on-demand lookup, missing previews, matching track versions, trusted HTTPS audio hosts, preserved queue state and lookup recovery after failure.

- 23 tests pass, including preservation of source recording versions for preview matching. Real browser playback was verified: an audio sample loaded and ended at approximately 30 seconds, without errors; closing the player releases its audio source.

## Individual track requests

- 28 tests pass. Added tests for recording resolution within an album and via standalone search, request-kind settings and per-item overrides, album/recording history separation, two tracks from one album, cross-provider recording deduplication, already-owned recordings, and stale candidate rejection.
- An isolated HTTP integration test verifies that single-track mode uses only `/tracks/{recording_mbid}/request` and never posts an album request.
- Local browser validation covers selecting "track only", receiving the isolated destination's already-owned response, and persisting the default request kind. Existing album delivery history and queue modes survive the database upgrade.

## Configurable automatic matching

- 44 automated tests pass. Coverage includes the exact 70% boundary, rejection below the threshold, near-tied candidates, disabled automation, validation of out-of-range and non-finite values, and selective reprocessing of reviews while preserving baseline, skipped, completed and uncertain-delivery records.
- Saving the threshold and preserving it after a browser reload were verified through the local UI. Existing settings adopt the 70% default once while retaining the sync interval.
- The Linux amd64 image starts locally and returns a healthy HTTP response with the 70% default. Server deployment keeps the existing 128 MiB RAM limit and data volume.

## Candidate album samples

- 48 tests pass, including candidate-bound preview lookup, authentication, preserved queue/delivery state, trusted audio URLs, missing samples, individual recording versions, and releasing the shared preview lock after lookup failures.
- The installed destination's album-preview endpoint returns real album samples; the UI reuses the existing single 30-second player. No preview lookup happens while rendering the queue.

## Automatic album-to-track fallback

- 58 tests pass. Added coverage for absent and below-threshold albums, the exact recording threshold, persisted request kind before submission, restart/cross-provider deduplication, album priority, disabled automation/fallback, catalog outages, preserved manual album choices and uncertain-delivery protection. Existing album results are reused during recording lookup.
- The setting defaults to enabled and can be disabled in the UI. Existing pending reviews are reconsidered once on upgrade, preserving the baseline list.

## Recording lookup recovery

- 63 tests pass. Explicit `(original mix)`, `[Original Version]` and `- Original Mix` labels now match the default recording in album/recording lookup and previews. Live, remix, radio edit, extended and remastered labels remain distinct in normalization.
- The default manual query uses the source album first, avoiding an unnecessary secondary catalogue call. Custom queries retain their search text. A secondary MusicBrainz outage preserves already-found recording candidates; when there are none, the error identifies MusicBrainz.
