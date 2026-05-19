# keel

The shared language of this project. keel is a curated, publishable mirror of
the parts of a personal `~/.claude` setup that the author wrote themselves — a
harness of engineering discipline layered on native Claude Code.

## Language

**keel**:
This repository — the curated, authored subset of a `~/.claude` setup,
plus the guide that documents it.
_Avoid_: config, dotfiles, setup

**harness**:
The collection of skills, hooks, scripts, agents, and config that adds
behavior on top of native Claude Code.
_Avoid_: framework, plugin, toolkit

**Source of truth**:
`~/.claude` — the live directory Claude Code actually reads. All editing
happens here.
_Avoid_: origin, master copy

**Mirror**:
keel itself — a downstream copy populated from the source of truth. Never
edited directly for harness content.
_Avoid_: backup, clone

**Authored content**:
Skills, hooks, scripts, and config the author wrote. Only this enters keel.
_Avoid_: my stuff, original code

**Third-party content**:
Skills and plugins from other authors (gstack, skill-manager skills,
Claude Code plugins). Referenced in `docs/DEPENDENCIES.md`, never copied in.
_Avoid_: external, vendored, deps

**sync**:
Running `sync.sh` to copy authored content from the source of truth into
the mirror. The one deliberate step that updates keel.
_Avoid_: update, pull, refresh

**allowlist**:
`.allowlist` — the explicit list of paths `sync.sh` mirrors. Defines, in
one place, exactly what counts as authored content.
_Avoid_: manifest, include list, config

**cluster**:
A set of harness components that only work together (e.g. the dev-log
cluster: a hook, an agent, and two skills). Installing one without the
rest yields a half-working feature.
_Avoid_: group, module, bundle

**claude-config**:
A *separate* repo that backs up the author's entire `~/.claude` for
machine sync. Not keel. keel is curated and publishable; claude-config is
a private full backup.
_Avoid_: using "the config repo" for keel

## Relationships

- The **source of truth** (`~/.claude`) is mirrored into **keel** by **sync**.
- The **allowlist** defines which paths count as **authored content**.
- **Third-party content** is referenced in `docs/DEPENDENCIES.md`, never mirrored.
- **keel** and **claude-config** both derive from `~/.claude` but serve
  different purposes — keel is a curated public-facing subset, claude-config
  is a private full backup.
- A **cluster** spans several components; the guide documents each cluster's
  wiring so partial installs are an informed choice.

## Flagged ambiguities

- "harness" was used to mean both the entire `~/.claude` setup and keel
  specifically — resolved: the **harness** is the behavior layer; **keel** is
  the curated mirror of the authored part of it.
- "guide" was used loosely — resolved: the guide is `README.md`, the entry
  point of keel; deeper docs live under `docs/`.
