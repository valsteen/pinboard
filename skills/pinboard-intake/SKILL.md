---
name: pinboard-intake
description: Preserve one newly proposed piece of project work as a ready item on the pinboard. Use when the user explicitly asks to add, queue, intake, preserve, save for later, or send a prerequisite, bug, cleanup, feature, contradiction, or clarification for later work, including when no board exists yet. Do not use merely because a conversation explores an idea.
---

# Add to the pinboard

Before intake calls, read exactly one complete coordinator startup document under the resolved Pinboard plugin root: `skills/pinboard/startup/coordinator-codex.md` for Codex, `coordinator-claude-code.md` for Claude Code or `coordinator-antigravity.md` for Antigravity, all in that startup directory. It contains common policy, every binding coordinator phase and that runtime’s procedures inline. Intake is a specialized coordinator caller, not another role. Use native complete loading or native `read_file`; in Antigravity continue at `ByteOffset=46080` and later offsets until the final section. If native complete reading is unavailable or refused, use `pinboard_startup_text` `file-start` with the actual resolved absolute path, exact roots and `max_batch_bytes: "host-profile"`, following each continuation verbatim until null, one page per outer response without aggregation. Missing complete content stops startup; EOF establishes delivery only. Intake persistence remains the correctness boundary. An embedded launched worker keeps its already selected worker startup document and its permitted nested-intake scope.

Follow that document’s inline intake procedure. Standalone intake preserves one explicitly requested concern as a ready item; embedded intake returns to its retained continuation anchor. An immediate-start request continues through the coordinator’s inline preparation and dispatch procedure. A launched worker keeps its existing role and permitted nested-intake scope.
