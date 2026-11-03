---
name: pinboard
description: Use before answering status, planning or ideation about saved Pinboard work, including quick read-only status, or any product, roadmap or architecture discussion in a Pinboard-managed project. Coach discussion points and preserve decisions even when no implementation is requested. Also coordinate next-work selection, parallel work, worker dispatch and continuation, lifecycle, review, recovery and long-running continuity. Do not implement an assigned worker attempt; use $pinboard-deliver.
---

# Coordinate with Pinboard

Load exactly one complete startup document for your current runtime before acting. It contains the common rules, coordinator procedure and runtime instructions inline; do not reconstruct startup by navigating instruction references.

| Current runtime | Complete startup document |
| --- | --- |
| Codex | [Read coordinator-codex.md](startup/coordinator-codex.md) |
| Claude Code | [Read coordinator-claude-code.md](startup/coordinator-claude-code.md) |
| Antigravity | [Read coordinator-antigravity.md](startup/coordinator-antigravity.md) |

Use a native loader or native `read_file` only when it delivers the complete file through its final section. In Antigravity, continue native reads at `ByteOffset=46080` and later offsets for files over 46,080 bytes. If native complete reading is unavailable or refused, use `pinboard_startup_text` `file-start` with the actual resolved absolute path, exact roots and `max_batch_bytes: "host-profile"`; follow each continuation verbatim under `request` until null, one page per outer response without aggregation. EOF establishes delivery only. Missing complete content stops startup. A launched role follows its verified native envelope’s exact selection instead of loading coordinator startup.
