# Implementation Plan: Lumielle Nexus 0.4.0 Member Sets and Safe Moderation

## Goal

Add deterministic group-member search and group-scoped member sets, allow collection and relay snapshots to reference those sets, and add opt-in single-member moderation with prepare/confirm safety. Preserve the existing task scheduler, archive, summary, relay, collection, and adapter boundaries.

## Tasks

1. Add migration-safe `member_sets` and `member_set_members` storage plus deterministic member normalization, search, exact resolution, and moderation preflight helpers. Write focused storage/core tests first.
2. Extend collection payloads with target-member snapshots and apply the same target/current-human intersection to status, chase, and XLSX output. Add regression tests for snapshot immutability and departed members.
3. Extend relay prepare/confirm to snapshot current members from a set, render the snapshot in the preview, and send the stored IDs. Add tests for mutual exclusion, empty snapshots, and post-prepare set changes.
4. Add `get_group_member_info`, `set_group_ban`, and `set_group_kick` to `QQAdapter`; add transactional moderation claim, TTL, crash recovery, role preflight, terminal failure, and no-retry execution. Add fake-adapter tests for every safety boundary.
5. Add the seven new tools and the required fallback commands, update config/schema, version metadata, README, and AGENTS rules. Short-circuit empty on-demand summaries before provider lookup and update the plugin contract tests to 28 tools.
6. Run the focused unit suite, compile/lint/diff checks, and the AstrBot 4.28 package-loader smoke; inspect the final diff for runtime artifacts, then commit and push `main`.

## Verification

Use the existing unittest suite plus focused tests for schema migration, member resolution, collection/relay snapshots, moderation safety, and empty-summary provider avoidance. Do not perform real QQ mute or kick operations.
