#!/bin/bash
# Build AppImage for mk_ssh
# Requires: appimagetool (https://github.com/AppImage/appimagetool)
# Run from project root after: python build.py

set -e

APP_NAME="mk_ssh"
VERSION="1.0.0"
ARCH="x86_64"

# Paths
DIST_DIR="dist"
APPDIR="${APP_NAME}.AppDir"
APPIMAGE="${APP_NAME}-${VERSION}-${ARCH}.AppImage"

echo "Building AppImage for ${APP_NAME}..."

# Clean previous build
rm -rf "${APPDIR}" "${APPIMAGE}"

# Create AppDir structure
mkdir -p "${APPDIR}/usr/bin"
mkdir -p "${APPDIR}/usr/share/applications"
mkdir -p "${APPDIR}/usr/share/icons/hicolor/256x256/apps"
mkdir -p "${APPDIR}/usr/share/metainfo"

# Copy the PyInstaller-built executable
if [ ! -f "${DIST_DIR}/${APP_NAME}" ]; then
    echo "Error: ${DIST_DIR}/${APP_NAME} not found. Run 'python build.py' first."
    exit 1
fi

cp "${DIST_DIR}/${APP_NAME}" "${APPDIR}/usr/bin/${APP_NAME}"
chmod +x "${APPDIR}/usr/bin/${APP_NAME}"

# Copy icon
if [ -f "mk_ssh.png" ]; then
    cp mk_ssh.png "${APPDIR}/usr/share/icons/hicolor/256x256/apps/${APP_NAME}.png"
    cp mk_ssh.png "${APPDIR}/${APP_NAME}.png"  # For AppImage spec
else
    echo "Warning: mk_ssh.png not found"
fi

# Create AppRun (entry point)
cat > "${APPDIR}/AppRun" << 'EOF'
#!/bin/bash
# AppRun - Entry point for AppImage
HERE="$(dirname "$(readlink -f "${0}")")"
export PATH="${HERE}/usr/bin:${PATH}"
export LD_LIBRARY_PATH="${HERE}/usr/lib:${LD_LIBRARY_PATH}"

# Handle arguments
exec "${HERE}/usr/bin/mk_ssh" "$@"
EOF
chmod +x "${APPDIR}/AppRun"

# Create .desktop file (installs to menus via desktop-file-install)
cat > "${APPDIR}/usr/share/applications/${APP_NAME}.desktop" << EOF
[Desktop Entry]
Type=Application
Name=mk_ssh
GenericName=SSH Connection Manager
Comment=GUI for managing SSH connections
Exec=mk_ssh
Icon=mk_ssh
Terminal=false
Categories=Network;RemoteAccess;System;
StartupNotify=true
Keywords=ssh;connection;manager;terminal;
MimeType=x-scheme-handler/ssh;
EOF

# Also place .desktop at AppDir root for some launchers
cp "${APPDIR}/usr/share/applications/${APP_NAME}.desktop" "${APPDIR}/${APP_NAME}.desktop"

# Create AppStream metainfo (for software centers)
cat > "${APPDIR}/usr/share/metainfo/${APP_NAME}.metainfo.xml" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<component type="desktop-application">
  <id>${APP_NAME}</id>
  <name>mk_ssh</name>
  <summary>SSH Connection Manager</summary>
  <description>
    <p>A GUI application for managing and launching SSH connections from your SSH config.</p>
    <p>Features:</p>
    <ul>
      <li>Parse and display SSH config hosts</li>
      <li>Filter and search hosts</li>
      <li>Group hosts by config sections</li>
      <li>Preview connection details</li>
      <li>Launch SSH with a click</li>
      <li>Auto-reload on config changes</li>
    </ul>
  </description>
  <project_license>MIT</project_license>
  <url type="homepage">https://github.com/mmay/mk_ssh</url>
  <url type="bugtracker">https://github.com/mmay/mk_ssh/issues</url>
  <screenshots>
    <screenshot type="default">
      <image>https://github.com/mmay/mk_ssh/raw/main/screenshot.png</image>
    </screenshot>
  </screenshots>
  <provides>
    <binary>${APP_NAME}</binary>
  </provides>
  <releases>
    <release version="${VERSION}" date="$(date +%Y-%m-%d)"/>
  </releases>
</component>
EOF

# Download appimagetool if not present
if ! command -v appimagetool &> /dev/null; then
    echo "appimagetool not found, downloading..."
    wget -q "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-${ARCH}.AppImage" -O appimagetool
    chmod +x appimagetool
    APPIMAGETOOL="./appimagetool"
else
    APPIMAGETOOL="appimagetool"
fi

# Build the AppImage
echo "Packaging AppImage..."
ARCH=${ARCH} ${APPIMAGETOOL} "${APPDIR}" "${APPIMAGE}"

# Cleanup
rm -rf "${APPDIR}"
[ -f "./appimagetool" ] && rm -f "./appimagetool"

echo ""
echo "========================================="
echo "AppImage created: ${APPIMAGE}"
echo "========================================="
echo ""
echo "To install system-wide (adds to menus):"
echo "  sudo cp ${APPIMAGE} /usr/local/bin/${APP_NAME}"
echo "  sudo chmod +x /usr/local/bin/${APP_NAME}"
echo ""
echo "Or use AppImageLauncher for user-level integration."
echo ""
echo "To test without installing:"
echo "  ./${APPIMAGE}"