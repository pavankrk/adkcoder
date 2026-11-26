#!/bin/sh
set -eu

pkg_dir=$(CDPATH= cd -P "$(dirname "$0")/.." && pwd)
cd "$pkg_dir"

if [ -x "$pkg_dir/.venv/bin/python" ]; then
    py="$pkg_dir/.venv/bin/python"
elif command -v python3.13 >/dev/null 2>&1; then
    py="python3.13"
elif command -v python3.12 >/dev/null 2>&1; then
    py="python3.12"
elif command -v python3.11 >/dev/null 2>&1; then
    py="python3.11"
else
    py="python3"
fi

version=$(sed -n 's/^version *= *"\([^"]*\)".*/\1/p' pyproject.toml | head -n 1)
if [ -z "$version" ]; then
    echo "Error: could not read project version from pyproject.toml" >&2
    exit 1
fi

mkdir -p "$pkg_dir/dist"

echo "==> 1. Building Python wheel (for uvx / uv tool / pipx)..."
rm -f "$pkg_dir/dist"/adk_coder-*.whl "$pkg_dir/dist"/adk_coder-*.tar.gz
if command -v uv >/dev/null 2>&1; then
    uv build --wheel --out-dir "$pkg_dir/dist"
else
    "$py" -m pip wheel --no-deps . --wheel-dir "$pkg_dir/dist"
fi
wheel=$(ls "$pkg_dir/dist"/adk_coder-*.whl | head -n 1)
echo "    Wheel: $wheel"

echo "==> 2. Building npm/npx tarball..."
rm -rf "$pkg_dir/npm-package/wheels"
mkdir -p "$pkg_dir/npm-package/wheels"
cp "$wheel" "$pkg_dir/npm-package/wheels/"
chmod +x "$pkg_dir/npm-package/bin/adk-coder.js" "$pkg_dir/npm-package/bin/adk-coder-config.js" "$pkg_dir/npm-package/bin/adk-coder-app.js" "$pkg_dir/npm-package/bin/runner.js"
rm -f "$pkg_dir/dist"/adk-coder-"${version}".tgz
(cd "$pkg_dir/npm-package" && npm pack --cache /tmp/npm-cache --pack-destination "$pkg_dir/dist" >/dev/null)
rm -rf "$pkg_dir/npm-package/wheels"
npm_tgz="$pkg_dir/dist/adk-coder-${version}.tgz"
echo "    npm tarball: $npm_tgz"

echo "==> 2b. Building portable install bundle zip..."
zip_stage="$pkg_dir/dist/install_bundle_stage"
rm -rf "$zip_stage" "$pkg_dir/dist/adk-coder-${version}-install-bundle.zip"
mkdir -p "$zip_stage"
cp "$wheel" "$npm_tgz" "$pkg_dir/scripts/install.ps1" "$pkg_dir/scripts/create-windows-shortcut.ps1" "$pkg_dir/scripts/run-desktop.ps1" "$pkg_dir/scripts/run-desktop.sh" "$pkg_dir/RELEASE_NOTES.md" "$zip_stage/"
(cd "$zip_stage" && zip -q -r "$pkg_dir/dist/adk-coder-${version}-install-bundle.zip" .)
rm -rf "$zip_stage"
echo "    Install bundle zip: $pkg_dir/dist/adk-coder-${version}-install-bundle.zip"

if [ "${1:-}" = "--offline" ]; then
    echo "==> 3. Building offline Python wheelhouse bundle..."
    stage="$pkg_dir/dist/bundle_stage"
    rm -rf "$stage"
    mkdir -p "$stage/wheels"
    if "$py" -m pip --version >/dev/null 2>&1; then
        "$py" -m pip wheel '.[local]' --wheel-dir "$stage/wheels"
    elif command -v uv >/dev/null 2>&1; then
        uv run --python ">=3.11" --with pip python -m pip wheel '.[local]' --wheel-dir "$stage/wheels"
    else
        echo "Error: --offline requires pip or uv to download dependency wheels." >&2
        exit 1
    fi
    cat > "$stage/install.sh" <<'EOF'
#!/bin/sh
set -eu
dir=$(CDPATH= cd -P "$(dirname "$0")" && pwd)
prefix="${XDG_DATA_HOME:-$HOME/.local/share}/adk-coder"
bin_dir="${HOME}/.local/bin"
python3 -m venv "$prefix/venv"
"$prefix/venv/bin/pip" install --no-index --find-links "$dir/wheels" "adk-coder[local]"
mkdir -p "$bin_dir"
ln -sf "$prefix/venv/bin/adk-coder" "$bin_dir/adk-coder"
ln -sf "$prefix/venv/bin/adk-coder-config" "$bin_dir/adk-coder-config"
echo "Installed adk-coder and adk-coder-config into $bin_dir"
echo "Run: adk-coder --config"
EOF
    chmod +x "$stage/install.sh"
    cp "$pkg_dir/scripts/install.ps1" "$stage/install.ps1"
    offline_bundle="$pkg_dir/dist/adk-coder-${version}-offline-bundle.tar.gz"
    tar -czf "$offline_bundle" -C "$stage" .
    rm -rf "$stage"
    echo "    Offline bundle: $offline_bundle"
fi

echo ""
echo "Done! Install or run with:"
echo "  Python uvx:   uvx --from \"$wheel[local]\" adk-coder --config"
echo "  uv tool:      uv tool install \"$wheel[local]\""
echo "  npm / npx:    npx \"$npm_tgz\" --config"
echo "  npm global:   npm install -g \"$npm_tgz\""
