"""Cross-platform file recycling utilities."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def _recycle(path: Path) -> None:
    """Move a file to the system recycle bin / trash. Never permanently delete.

    Platform support:
    - Windows: PowerShell shell API -> Recycle Bin
    - macOS: ~/.Trash
    - Linux: gio trash (GNOME/KDE) or ~/.local/share/Trash/files/ (XDG spec)

    Args:
        path: Path to the file to recycle
    """
    if not path.exists():
        return
    try:
        if sys.platform == "win32":
            subprocess.run([
                "powershell", "-Command",
                "Add-Type -AssemblyName Microsoft.VisualBasic;"
                f"[Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile("
                f"'{path}','OnlyErrorDialogs','SendToRecycleBin')"
            ], capture_output=True, timeout=10)
        elif sys.platform == "darwin":
            trash = Path.home() / ".Trash"
            trash.mkdir(exist_ok=True)
            path.rename(trash / path.name)
        else:
            # Linux: try gio first (GNOME/KDE), fall back to XDG Trash spec
            result = subprocess.run(
                ["gio", "trash", str(path)],
                capture_output=True, timeout=10
            )
            if result.returncode != 0:
                # XDG Trash spec fallback
                trash_files = Path(os.environ.get(
                    "XDG_DATA_HOME",
                    str(Path.home() / ".local" / "share")
                )) / "Trash" / "files"
                trash_files.mkdir(parents=True, exist_ok=True)
                path.rename(trash_files / path.name)
    except Exception:
        pass  # best-effort, silently ignore failures
