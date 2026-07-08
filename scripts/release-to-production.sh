#!/usr/bin/env bash
#
# release-to-production.sh
#
# Releases the frontend and backend repos from `main` to `production` over SSH.
#
# SSH-only: no GitHub token required. It uses each repo's existing `origin`
# remote (your git@github.com-CMM SSH profile). GitHub does not expose PR
# creation/merge over SSH, so this does NOT open a PR — it performs a direct
# merge of `main` into `production` and pushes. The merge commit is titled
# "release: DD/MM/YYYY" so production keeps a clear release audit trail.
#
# Safety: all work happens in a throwaway git worktree. Your working checkouts,
# current branch, and uncommitted changes are never touched.
#
# Usage:
#   ./scripts/release-to-production.sh
#
# Options (env vars):
#   FRONTEND_DIR   local checkout of cmm-frontend (default below)
#   BACKEND_DIR    local checkout of cmm-backend  (default below)
#   BASE_BRANCH    target branch, default: production
#   HEAD_BRANCH    source branch, default: main
#   DRY_RUN        set to 1 to preview without merging/pushing
#
set -euo pipefail

# ----- Config -------------------------------------------------------------
FRONTEND_DIR="${FRONTEND_DIR:-/Users/nnavu/WebstormProjects/cmm-frontend}"
BACKEND_DIR="${BACKEND_DIR:-/Users/nnavu/PycharmProjects/cmm-backend}"
BASE_BRANCH="${BASE_BRANCH:-production}"
HEAD_BRANCH="${HEAD_BRANCH:-main}"
DRY_RUN="${DRY_RUN:-0}"

# name|local-dir  — release runs in this order
REPOS=(
  "cmm-frontend|${FRONTEND_DIR}"
  "cmm-backend|${BACKEND_DIR}"
)

RELEASE_DATE="$(date +%d/%m/%Y)"
MERGE_MSG="release: ${RELEASE_DATE}"

# ----- Helpers ------------------------------------------------------------
c_red=$'\033[31m'; c_grn=$'\033[32m'; c_ylw=$'\033[33m'; c_dim=$'\033[2m'; c_rst=$'\033[0m'
info() { printf '%s\n' "$*"; }
ok()   { printf '%s%s%s\n' "$c_grn" "$*" "$c_rst"; }
warn() { printf '%s%s%s\n' "$c_ylw" "$*" "$c_rst"; }
err()  { printf '%s%s%s\n' "$c_red" "$*" "$c_rst" >&2; }

info "Release date : ${RELEASE_DATE}"
info "Merge commit : ${MERGE_MSG}"
info "Flow         : ${HEAD_BRANCH} -> ${BASE_BRANCH} (direct merge over SSH, no PR)"
[[ "$DRY_RUN" == "1" ]] && warn "DRY_RUN=1 — nothing will be merged or pushed."
info ""

# ----- Per-repo release ---------------------------------------------------
release_repo() {
  local name="$1" dir="$2"
  info "${c_dim}────────────────────────────────────────────${c_rst}"
  info "Repo: ${name}  (${dir})"

  if [[ ! -d "$dir/.git" ]]; then
    err "  ✗ Not a git checkout: ${dir}"; return 1
  fi

  # Fetch the freshest main/production from origin (SSH).
  if ! git -C "$dir" fetch --prune origin "$HEAD_BRANCH" "$BASE_BRANCH" >/dev/null 2>&1; then
    err "  ✗ git fetch failed (SSH/branch issue?) for ${name}"; return 1
  fi

  # Confirm both remote branches exist.
  local base_ref="origin/${BASE_BRANCH}" head_ref="origin/${HEAD_BRANCH}"
  if ! git -C "$dir" rev-parse --verify -q "$base_ref" >/dev/null; then
    err "  ✗ ${base_ref} not found."; return 1
  fi
  if ! git -C "$dir" rev-parse --verify -q "$head_ref" >/dev/null; then
    err "  ✗ ${head_ref} not found."; return 1
  fi

  # Anything to release? (is main already contained in production?)
  if git -C "$dir" merge-base --is-ancestor "$head_ref" "$base_ref"; then
    warn "  • Nothing to release — ${BASE_BRANCH} already contains ${HEAD_BRANCH}. Skipping."
    return 0
  fi

  local ahead
  ahead="$(git -C "$dir" rev-list --count "${base_ref}..${head_ref}")"
  info "  • ${HEAD_BRANCH} is ${ahead} commit(s) ahead of ${BASE_BRANCH}:"
  git -C "$dir" log --oneline --no-decorate "${base_ref}..${head_ref}" | sed 's/^/      /' | head -20

  if [[ "$DRY_RUN" == "1" ]]; then
    warn "  • [dry-run] Would merge ${head_ref} into ${BASE_BRANCH} and push."
    return 0
  fi

  # Isolated worktree so the user's checkout/branch/working tree is untouched.
  local tmp_branch="__release_${name}_$$"
  local wt; wt="$(mktemp -d "${TMPDIR:-/tmp}/rel-${name}-XXXXXX")"

  # Ensure cleanup regardless of outcome.
  cleanup_wt() {
    git -C "$dir" worktree remove --force "$wt" >/dev/null 2>&1 || true
    git -C "$dir" branch -D "$tmp_branch" >/dev/null 2>&1 || true
    rm -rf "$wt" >/dev/null 2>&1 || true
  }

  if ! git -C "$dir" worktree add -B "$tmp_branch" "$wt" "$base_ref" >/dev/null 2>&1; then
    err "  ✗ Could not create worktree."; cleanup_wt; return 1
  fi

  if ! git -C "$wt" merge --no-ff "$head_ref" -m "$MERGE_MSG" >/dev/null 2>&1; then
    git -C "$wt" merge --abort >/dev/null 2>&1 || true
    err "  ✗ Merge conflict merging ${HEAD_BRANCH} into ${BASE_BRANCH}."
    err "    ${BASE_BRANCH} has diverged (e.g. hotfixes not in ${HEAD_BRANCH}). Resolve manually."
    cleanup_wt; return 1
  fi

  # Push the merge result to production over SSH.
  if git -C "$wt" push origin "HEAD:${BASE_BRANCH}"; then
    local new_sha; new_sha="$(git -C "$wt" rev-parse --short HEAD)"
    ok "  ✓ Released ${name}: ${BASE_BRANCH} now at ${new_sha} (${MERGE_MSG})"
  else
    err "  ✗ Push to ${BASE_BRANCH} failed for ${name}."
    cleanup_wt; return 1
  fi

  cleanup_wt
}

# ----- Main ---------------------------------------------------------------
rc=0
for entry in "${REPOS[@]}"; do
  name="${entry%%|*}"; dir="${entry##*|}"
  if ! release_repo "$name" "$dir"; then
    rc=1
  fi
done

info "${c_dim}────────────────────────────────────────────${c_rst}"
if [[ "$rc" == "0" ]]; then
  ok "Release complete."
else
  err "Release finished with errors — see output above."
fi
exit "$rc"
