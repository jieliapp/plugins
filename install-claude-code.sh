#!/usr/bin/env bash
set -euo pipefail

repo_url="${JIELI_PLUGINS_REPO_URL:-https://github.com/burugo/jieli-plugins.git}"
install_root="${JIELI_PLUGIN_INSTALL_ROOT:-${HOME}/.jieli/plugins}"
repo_dir="${install_root}/jieli-plugins"

if ! command -v git >/dev/null 2>&1; then
  echo "git is required to install Jieli Claude Code plugin" >&2
  exit 1
fi

if ! command -v claude >/dev/null 2>&1; then
  echo "claude CLI is required to install Jieli Claude Code plugin" >&2
  exit 1
fi

mkdir -p "${install_root}"

if [ -d "${repo_dir}/.git" ]; then
  git -C "${repo_dir}" pull --ff-only
else
  git clone "${repo_url}" "${repo_dir}"
fi

claude plugin install "${repo_dir}/claude-code"

cat <<'EOF'

Jieli Claude Code plugin installed.

Next:
  1. Get an API key from https://jieli.app
  2. Export it before starting Claude Code:

       export JIELI_API_KEY="your-jieli-api-key"

For self-hosted Jieli, also set:

       export JIELI_BASE_URL="https://your-jieli.example.com"

EOF
