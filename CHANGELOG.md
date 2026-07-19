# Changelog

All notable changes to the GUI plugin are documented in this file.

## [1.0.4] - 2026-07-19

### Fixed

- Added a Catalog enhanced-HLS gate so unavailable songs never consume a wrapper playback instance
- Serialized wrapper probes with a post-failure health check against a previously successful song
- Restarted only the VM wrapper-manager service when a failed song poisons subsequent queries
- Continued the remaining playlist query after isolating the failed track

## [1.0.3] - 2026-07-19

### Fixed

- Isolated a song-specific quality-query failure without replacing the shared gRPC channel
- Prevented a failed playlist probe from cascading into subsequent tracks or cache decryption
- Removed stale decrypt-stream stop markers that could immediately terminate a restarted stream
- Confirmed and recovered decrypt-stream readiness before local-cache import begins
- Reported the active cache preparation/decryption item instead of appearing stuck at zero

## [1.0.2] - 2026-07-19

### Fixed

- Bundled and explicitly registered creart event-loop support for frozen startup
- Extended executable self-check to execute the external `run_sync()` path
- Applied native large and small Windows window icons before the taskbar window is mapped

## [1.0.1] - 2026-07-18

### Fixed

- Rebuilt the Windows icon with dedicated low-resolution fingerprint artwork
- Prevented the full-size PNG from overriding the multi-size ICO on Windows
- Added Windows file version metadata and refreshed the taskbar application identity

## [1.0.0] - 2026-07-18

### Added

- Windows GUI for AppleMusicDecrypt download and cache-import workflows
- GUI-only single-file executable that loads the upstream core externally
- Local Apple Music cache discovery and import through the existing decrypt/save chain
- Download, decrypt, throughput, per-track and wrapper-manager status views
- New `Touch_ID_Logo.svg` application, window and taskbar icon
- Reproducible GitHub Actions workflow for plugin-only release archives

### Fixed

- Hidden external command windows during download and decrypt operations
- Cache-import progress reporting
- Playlist query failure isolation
- Speed chart unit scaling
- Windows taskbar icon identity
