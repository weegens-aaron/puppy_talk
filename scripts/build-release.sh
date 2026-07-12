#!/usr/bin/env bash
#
# build-release.sh -- Build the clean puppy_talk release zip from the explicit
# allowlist in scripts/ship-manifest.txt (same pattern as bead-chain).
#
# What it does (deterministic, idempotent):
#   1. Reads __version__ from __init__.py (single source of truth).
#   2. Cleans staging/ and dist/.
#   3. Copies ONLY the allowlisted runtime paths into staging/puppy_talk/.
#   4. Zips staging/ so the archive's single top-level entry is puppy_talk/
#      (the folder name matters: modules import `from puppy_talk import ...`).
#   5. Writes BOTH a stable name (dist/puppy-talk.zip -- enables the
#      /releases/latest/download/puppy-talk.zip URL) and a versioned name
#      (dist/puppy-talk-v<version>.zip).
#   6. Writes a SHA256 checksum file alongside each zip so users -- and the
#      published GitHub Release -- can verify download integrity.
#   7. Self-checks: extracts the stable zip to a temp dir and imports
#      puppy_talk.register_callbacks. A missing runtime file => ImportError =>
#      the allowlist is incomplete and the build fails loudly.
#
# The self-check import needs the code_puppy host package. Override the
# interpreter with PUPPY_TALK_PYTHON, e.g.:
#   PUPPY_TALK_PYTHON="uv run --with code-puppy python" scripts/build-release.sh

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." >/dev/null 2>&1 && pwd)"
cd "${REPO_ROOT}"

MANIFEST_FILE="${SCRIPT_DIR}/ship-manifest.txt"
if [[ ! -f "${MANIFEST_FILE}" ]]; then
  echo "ERROR: ship manifest not found: ${MANIFEST_FILE}" >&2
  exit 1
fi

ALLOWLIST=()
while IFS= read -r line || [[ -n "${line}" ]]; do
  line="${line%%#*}"
  line="${line#"${line%%[![:space:]]*}"}"
  line="${line%"${line##*[![:space:]]}"}"
  [[ -z "${line}" ]] && continue
  ALLOWLIST+=("${line}")
done < "${MANIFEST_FILE}"

if [[ ${#ALLOWLIST[@]} -eq 0 ]]; then
  echo "ERROR: ship manifest is empty after parsing: ${MANIFEST_FILE}" >&2
  exit 1
fi

PKG_NAME="puppy_talk"
STAGING_DIR="${REPO_ROOT}/staging"
DIST_DIR="${REPO_ROOT}/dist"
STABLE_ZIP="${DIST_DIR}/puppy-talk.zip"

read_version() {
  local line ver
  line="$(grep -E '^__version__[[:space:]]*=' "${REPO_ROOT}/__init__.py" | head -n1)"
  if [[ -z "${line}" ]]; then
    echo "ERROR: could not find __version__ in __init__.py" >&2
    exit 1
  fi
  ver="$(printf '%s\n' "${line}" | sed -E 's/^[^"]*"([^"]*)".*$/\1/')"
  if [[ -z "${ver}" || "${ver}" == "${line}" ]]; then
    echo "ERROR: could not parse a quoted version from: ${line}" >&2
    exit 1
  fi
  printf '%s\n' "${ver}"
}

sha256_in_dist() {
  local file="$1"
  (
    cd "${DIST_DIR}"
    if command -v sha256sum >/dev/null 2>&1; then
      sha256sum "${file}" > "${file}.sha256"
    elif command -v shasum >/dev/null 2>&1; then
      shasum -a 256 "${file}" > "${file}.sha256"
    else
      echo "ERROR: neither sha256sum nor shasum found" >&2
      exit 1
    fi
  )
}

find_python() {
  local candidate
  for candidate in python3 python; do
    if command -v "${candidate}" >/dev/null 2>&1 \
       && "${candidate}" -c 'import sys' >/dev/null 2>&1; then
      printf '%s\n' "${candidate}"
      return 0
    fi
  done
  echo "ERROR: no working python3/python found on PATH" >&2
  exit 1
}

if [[ -n "${PUPPY_TALK_PYTHON:-}" ]]; then
  read -r -a PY_CMD <<< "${PUPPY_TALK_PYTHON}"
else
  PY_CMD=("$(find_python)")
fi

VERSION="$(read_version)"
VERSIONED_ZIP="${DIST_DIR}/puppy-talk-v${VERSION}.zip"

echo "==> puppy_talk release build (v${VERSION})"

echo "==> Cleaning staging/ and dist/"
rm -rf "${STAGING_DIR}" "${DIST_DIR}"
mkdir -p "${STAGING_DIR}/${PKG_NAME}" "${DIST_DIR}"

echo "==> Copying ${#ALLOWLIST[@]} allowlisted paths into staging/${PKG_NAME}/"
for path in "${ALLOWLIST[@]}"; do
  if [[ ! -e "${REPO_ROOT}/${path}" ]]; then
    echo "ERROR: allowlisted path is missing from the repo: ${path}" >&2
    exit 1
  fi
  cp -f "${REPO_ROOT}/${path}" "${STAGING_DIR}/${PKG_NAME}/"
  echo "    + ${path}"
done

echo "==> Building ${STABLE_ZIP}"
if command -v zip >/dev/null 2>&1; then
  (
    cd "${STAGING_DIR}"
    zip -X -r -q "${STABLE_ZIP}" "${PKG_NAME}"
  )
else
  # Git Bash on Windows has no `zip` -- use Python's stdlib. Sorted walk for
  # a deterministic archive; forward-slash arcnames keep it portable.
  "${PY_CMD[@]}" - "${STAGING_DIR}" "${PKG_NAME}" "${STABLE_ZIP}" <<'PY'
import os, sys, zipfile
staging, pkg, dest = sys.argv[1], sys.argv[2], sys.argv[3]
root = os.path.join(staging, pkg)
with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            arc = os.path.relpath(full, staging).replace(os.sep, "/")
            zf.write(full, arc)
PY
fi
cp -f "${STABLE_ZIP}" "${VERSIONED_ZIP}"
echo "    wrote $(basename "${STABLE_ZIP}") and $(basename "${VERSIONED_ZIP}")"

echo "==> Writing SHA256 checksums"
sha256_in_dist "$(basename "${STABLE_ZIP}")"
sha256_in_dist "$(basename "${VERSIONED_ZIP}")"

echo "==> Self-check: extracting and importing ${PKG_NAME}.register_callbacks"
TMP_CHECK="$(mktemp -d)"
cleanup() { rm -rf "${TMP_CHECK}"; }
trap cleanup EXIT

# Extract with unzip when present, else Python (Git Bash lacks unzip too).
if command -v unzip >/dev/null 2>&1; then
  unzip -q "${STABLE_ZIP}" -d "${TMP_CHECK}"
else
  "${PY_CMD[@]}" -c "import zipfile, sys; zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])" \
    "${STABLE_ZIP}" "$(cygpath -m "${TMP_CHECK}" 2>/dev/null || printf '%s' "${TMP_CHECK}")"
fi

TMP_CHECK_PY="$(cygpath -m "${TMP_CHECK}" 2>/dev/null || printf '%s' "${TMP_CHECK}")"
"${PY_CMD[@]}" -c "import sys; sys.path.insert(0, r'${TMP_CHECK_PY}'); import ${PKG_NAME}.register_callbacks; print('    import OK:', ${PKG_NAME}.register_callbacks.__name__)"

echo "==> Done. Release artifacts in dist/:"
ls -1 "${DIST_DIR}"

echo
echo "==> To publish (upload zips AND their .sha256 assets):"
echo "      gh release create v${VERSION} dist/puppy-talk*.zip dist/puppy-talk*.zip.sha256"
