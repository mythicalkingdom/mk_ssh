import os
import sys
import subprocess
import shutil
import platform
from pathlib import Path

# Configuration
APP_NAME = "mk_ssh"
ENTRY_POINT = "mk_ssh.py"
ICON_FILE = "mk_ssh.ico"
DATA_FILES = [("mk_ssh.png", ".")]

def run_command(cmd, env=None):
    print(f"Executing: {' '.join(cmd)}")
    result = subprocess.run(cmd, env=env, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"Error executing command:\n{result.stderr}")
        sys.exit(result.returncode)
    return result.stdout

def clean():
    print("Cleaning build artifacts...")
    dirs_to_clean = ["build", "dist"]
    for d in dirs_to_clean:
        if os.path.exists(d):
            shutil.rmtree(d)

    spec_file = f"{APP_NAME}.spec"
    if os.path.exists(spec_file):
        os.remove(spec_file)

    # Remove __pycache__
    for p in Path(".").rglob("__pycache__"):
        shutil.rmtree(p)

def build():
    print(f"Building {APP_NAME} for {platform.system()}...")

    # Construct PyInstaller command
    cmd = [
        "pyinstaller",
        "--clean",
        "--noconfirm",
        "--onefile",
        "--windowed",
        "--name", APP_NAME,
        ENTRY_POINT
    ]

    # Add icon if it exists
    if os.path.exists(ICON_FILE):
        cmd.extend(["--icon", ICON_FILE])

    # Add data files
    for src, dest in DATA_FILES:
        if os.path.exists(src):
            # PyInstaller uses ';' on Windows and ':' on other platforms
            sep = ";" if platform.system() == "Windows" else ":"
            cmd.extend(["--add-data", f"{src}{sep}{dest}"])
        else:
            print(f"Warning: Data file {src} not found, skipping.")

    # Windows specific optimization: disable UPX to avoid some antivirus false positives
    if platform.system() == "Windows":
        cmd.append("--noupx")

    run_command(cmd)
    print("\nBuild complete! Executable is located in the 'dist/' directory.")

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "clean":
        clean()
    else:
        clean()
        build()
