#!/usr/bin/env bash
# inventory.sh — fast project snapshot for the audit-and-write-readme skill.
# Usage: bash scripts/inventory.sh [project_root]   (default: current directory)

set -u
ROOT="${1:-.}"
cd "$ROOT" 2>/dev/null || { echo "Cannot enter $ROOT" >&2; exit 1; }

echo "=== Project root ==="
pwd

echo
echo "=== Manifests ==="
for f in package.json pyproject.toml Cargo.toml go.mod Gemfile composer.json \
         pom.xml build.gradle build.gradle.kts requirements.txt \
         Pipfile Pipfile.lock setup.py setup.cfg .tool-versions; do
  [ -e "$f" ] && echo "  $f"
done
ls requirements-*.txt 2>/dev/null | sed 's/^/  /'

echo
echo "=== Top-level layout ==="
ls -1F | grep -v -E '^(node_modules|\.venv|dist|build|target|\.git)/$' | head -40

echo
echo "=== Existing docs ==="
for f in README.md README.ko.md README.rst CHANGELOG.md LICENSE LICENSE.md \
         CONTRIBUTING.md CODE_OF_CONDUCT.md; do
  [ -e "$f" ] && echo "  $f"
done
[ -d docs ] && echo "  docs/ (directory)"

echo
echo "=== CI / automation ==="
for f in .github .gitlab-ci.yml .circleci Jenkinsfile .pre-commit-config.yaml \
         Makefile justfile taskfile.yml Taskfile.yml; do
  [ -e "$f" ] && echo "  $f"
done

echo
echo "=== Entry points (heuristic) ==="
find . -maxdepth 3 -type f \
  \( -name 'main.*' -o -name '__main__.py' -o -name 'index.ts' \
     -o -name 'index.js' -o -name 'index.tsx' -o -name 'cli.*' \
     -o -name 'lib.rs' -o -name 'mod.rs' \) \
  -not -path './node_modules/*' -not -path './.venv/*' \
  -not -path './dist/*' -not -path './build/*' -not -path './target/*' \
  2>/dev/null | head -20

echo
echo "=== Env-var references (sample) ==="
grep -RIn -E "process\.env\.[A-Z_]+|os\.environ(\.get)?\(['\"][A-Z_]+|os\.getenv\(['\"][A-Z_]+|std::env::var\(['\"][A-Z_]+" \
     --include='*.py' --include='*.ts' --include='*.tsx' \
     --include='*.js' --include='*.jsx' --include='*.rs' \
     --include='*.go' --include='*.rb' \
     --exclude-dir=node_modules --exclude-dir=.venv \
     --exclude-dir=dist --exclude-dir=build --exclude-dir=target \
     . 2>/dev/null | head -20

echo
echo "=== Scripts in package.json ==="
[ -f package.json ] && command -v jq >/dev/null && \
  jq -r '.scripts // {} | to_entries[] | "  \(.key): \(.value)"' package.json 2>/dev/null

echo
echo "=== Makefile targets ==="
[ -f Makefile ] && grep -E '^[a-zA-Z_-]+:' Makefile | sed 's/:.*//' | sed 's/^/  /'

echo
echo "=== License (first line) ==="
[ -f LICENSE ] && head -1 LICENSE | sed 's/^/  /'

echo
echo "=== Git context ==="
git log --oneline -5 2>/dev/null | sed 's/^/  /'
git remote -v 2>/dev/null | head -2 | sed 's/^/  /'

echo
echo "=== Source-file count (rough) ==="
find . -type f \
  \( -name '*.py' -o -name '*.ts' -o -name '*.tsx' -o -name '*.js' \
     -o -name '*.jsx' -o -name '*.rs' -o -name '*.go' -o -name '*.rb' \
     -o -name '*.java' -o -name '*.kt' \) \
  -not -path './node_modules/*' -not -path './.venv/*' \
  -not -path './dist/*' -not -path './build/*' -not -path './target/*' \
  2>/dev/null | wc -l | xargs -I{} echo "  source files: {}"
