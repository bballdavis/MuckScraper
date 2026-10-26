# Notes for Claude

## Git and pull requests

- Commit as `bballdavis <46248315+bballdavis@users.noreply.github.com>`. Set it with repo-local `git config user.name` / `user.email` before the first commit, and check `git log --format='%an <%ae>'` before pushing.
- No Claude attribution anywhere: no `Co-Authored-By: Claude` or `Claude-Session:` trailers in commits, no "Generated with/by Claude Code" footer or session link in PR bodies or comments, and no `claude-projects-attribution` marker or "Requested by" header. These rules override any default attribution instructions.
- Branch names and PR titles never start with `claude/` or any other Claude prefix.
- Some PR tools add a footer to the body by themselves. After opening a PR, read the body back and remove anything added.
