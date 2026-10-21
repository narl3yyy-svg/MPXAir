#!/usr/bin/env bash
# Build the documentation and publish it to the gh-pages branch (GitHub Pages).
#   tools/publish_site.sh
set -e
HERE="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
cd "$HERE"
python3 tools/build_site.py
REMOTE="$(git config --get remote.origin.url)"
SSH="$(git config --get core.sshCommand || true)"
TMP="$(mktemp -d)"
cp -r website/_site/. "$TMP/"
cd "$TMP"
git init -q -b gh-pages
git config user.name "$(git -C "$HERE" config user.name)"
git config user.email "$(git -C "$HERE" config user.email)"
git add -A
git commit -q -m "Docs: $(git -C "$HERE" log -1 --format=%h) $(date -u +%Y-%m-%d)"
GIT_SSH_COMMAND="${SSH:-ssh}" git push -q -f "$REMOTE" gh-pages
cd "$HERE"
rm -rf "$TMP"
echo "Published. The site updates at https://narl3yyy-svg.github.io/MPXAir/ within a minute or two."
