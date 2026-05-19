# keel is a downstream mirror of ~/.claude, not the source of truth

Claude Code reads its configuration from `~/.claude`, so all editing happens
there; keel is populated from `~/.claude` by `sync.sh` against an explicit
allowlist (`.allowlist`). We rejected making keel the source of truth — with
`~/.claude` symlinked into it — because that needs install machinery and path
indirection up front, and the point of keel is to grow one piece at a time
without that weight. The cost: keel can drift from `~/.claude` until `sync.sh`
is run. That is accepted — `sync.sh` *is* the deliberate update step.
