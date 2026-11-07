Use the assigned checkout or the owner’s selected Codex worktree. If the Codex worktree is detached, create a bounded local branch there before drafting the brief. Keep the exact shared work root.

For authorized Codex cleanup, after the pre-removal checks, run `git worktree remove <path>` from the main checkout, then `git branch -d <branch>`. Apply this to every worktree the item used, including the chat’s own checkout.
