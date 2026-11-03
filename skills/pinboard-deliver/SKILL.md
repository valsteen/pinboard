---
name: pinboard-deliver
description: Deliver exactly one active pinboard attempt from its accepted brief, current definition identity, and renewable lease. Use when the item, checkout, definition, acceptance criteria, and verification are already recorded. Do not use for intake, portfolio selection, broad audits, design exploration, or acceptance review.
---

# Deliver the accepted attempt

Load exactly one complete startup document for your current runtime before acting. It contains the common rules, worker procedure and runtime instructions inline; do not reconstruct startup by navigating instruction references.

| Current runtime | Complete startup document |
| --- | --- |
| Codex | [Read worker-codex.md](../pinboard/startup/worker-codex.md) |
| Claude Code | [Read worker-claude-code.md](../pinboard/startup/worker-claude-code.md) |
| Antigravity | [Read worker-antigravity.md](../pinboard/startup/worker-antigravity.md) |

Use a native loader or native `read_file` only when it delivers the complete file through its final section. In Antigravity, continue native reads at `ByteOffset=46080` and later offsets for files over 46,080 bytes. If native complete reading is unavailable or refused, use `pinboard_startup_text` `file-start` with the actual resolved absolute path, exact roots and `max_batch_bytes: "host-profile"`; follow each continuation verbatim under `request` until null, one page per outer response without aggregation. EOF establishes delivery only. Missing complete content stops startup. A launched role follows its verified native envelope’s exact selection instead of loading coordinator startup.
