#!/usr/bin/env bash
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
apps="$HOME/.local/share/applications"
icons="$HOME/.local/share/icons/hicolor/scalable/apps"
mkdir -p "$apps" "$icons"
install -m644 "$here/icons/mousetrack.svg" "$icons/mousetrack.svg"
sed -e "s|^Exec=.*|Exec=$here/bin/mousetrack|" \
    -e "s|^TryExec=.*|TryExec=$here/bin/mousetrack|" \
    "$here/mousetrack.desktop" > "$apps/mousetrack.desktop"
chmod +x "$here/bin/mousetrack"
command -v update-desktop-database >/dev/null && update-desktop-database "$apps" || true
command -v gtk-update-icon-cache  >/dev/null && gtk-update-icon-cache -qtf "$HOME/.local/share/icons/hicolor" 2>/dev/null || true
command -v xdg-icon-resource      >/dev/null && xdg-icon-resource forceupdate --theme hicolor 2>/dev/null || true
echo "Installed $apps/mousetrack.desktop"
echo "Installed $icons/mousetrack.svg"

if ! id -nG "$USER" | tr ' ' '\n' | grep -qx input; then
  echo
  echo "AVISO: $USER não está no grupo 'input'."
  echo "Sem isso o app não consegue ler /dev/input/event*. Corrija com:"
  echo "    sudo usermod -aG input $USER"
  echo "e relogue."
fi
