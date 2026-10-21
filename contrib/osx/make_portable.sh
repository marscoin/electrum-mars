#!/usr/bin/env bash
# Make a built Electrum-Mars.app self-contained, then check it.
#
#   contrib/osx/make_portable.sh dist/Electrum-Mars.app [max-macos]
#
# Libraries copied from Homebrew keep absolute references to the build
# machine (/opt/homebrew/...). Rewrite every such reference to point at the
# copy inside the app, give bundled libraries relative ids, re-sign, and
# then fail if anything still points outside the app or the system, or
# needs a newer macOS than max-macos (default 14.0).
set -euo pipefail

APP="${1:?usage: make_portable.sh path/to/App.app [max-macos]}"
MAX_MACOS="${2:-14.0}"
FW="$APP/Contents/Frameworks"

is_macho() { file -b "$1" | grep -q '^Mach-O'; }
external() { case "$1" in /opt/homebrew/*|/usr/local/*) return 0;; *) return 1;; esac; }
deps() { otool -L "$1" | tail -n +2 | awk '{print $1}'; }

# ---- relocate
while IFS= read -r -d '' f; do
  is_macho "$f" || continue
  changed=0
  if [[ "$f" == *.dylib ]]; then
    id="$(otool -D "$f" | tail -n 1)"
    if external "$id"; then
      install_name_tool -id "@rpath/$(basename "$f")" "$f" 2>/dev/null
      changed=1
    fi
  fi
  for d in $(deps "$f"); do
    external "$d" || continue
    base="$(basename "$d")"
    if [ -f "$FW/$base" ]; then
      rel="$(python3 -c 'import os,sys; print(os.path.relpath(sys.argv[1], os.path.dirname(sys.argv[2])))' "$FW/$base" "$f")"
      install_name_tool -change "$d" "@loader_path/$rel" "$f" 2>/dev/null
      changed=1
    fi
  done
  [ "$changed" = 1 ] && echo "relocated: ${f#$APP/}"
done < <(find "$APP" -type f -print0)

codesign --force --deep -s - "$APP"

# ---- check
bad=0
while IFS= read -r -d '' f; do
  is_macho "$f" || continue
  for d in $(deps "$f"); do
    case "$d" in
      /usr/lib/*|/System/*|@rpath/*|@loader_path/*|@executable_path/*) ;;
      *) echo "EXTERNAL: ${f#$APP/} -> $d"; bad=1 ;;
    esac
  done
  minos="$(otool -l "$f" | awk '/LC_BUILD_VERSION/{b=1} b&&/minos/{print $2; exit}')"
  if [ -n "$minos" ] && python3 -c 'import sys; v=lambda s: tuple(int(x) for x in s.split(".")); sys.exit(0 if v(sys.argv[1]) > v(sys.argv[2]) else 1)' "$minos" "$MAX_MACOS"; then
    echo "NEEDS macOS $minos: ${f#$APP/}"; bad=1
  fi
done < <(find "$APP" -type f -print0)

if [ "$bad" = 1 ]; then
  echo "The app is not portable (see above)."; exit 1
fi
echo "Portable: no references outside the app, runs on macOS $MAX_MACOS or newer."
