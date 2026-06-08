"""DaVinci Resolve Scripting API bridge — DRT import/export via sandbox projects."""

from __future__ import annotations

import copy
import os
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Optional

from pr2_constants import FCP7_DOCTYPE
from pr2_utils import _recycle


# ═══════════════════════════════════════════════════════════════════════════════
# DRT Output — DaVinci Resolve Scripting API Bridge
# ═══════════════════════════════════════════════════════════════════════════════

# DaVinci Resolve Scripting API module paths
_RESOLVE_API_PATHS: dict[str, list[str]] = {
    "win32": [
        r"C:\ProgramData\Blackmagic Design\DaVinci Resolve\Support\Developer\Scripting\Modules",
        r"C:\Program Files\Blackmagic Design\DaVinci Resolve\Support\Developer\Scripting\Modules",
    ],
    "darwin": [
        "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting/Modules",
    ],
    "linux": [
        "/opt/resolve/Developer/Scripting/Modules",
    ],
}

# DaVinci Resolve install directories — built dynamically from env vars, no hardcoded drives
def _build_resolve_candidates() -> list[str]:
    """Build list of possible DaVinci Resolve install directories.

    Uses environment variables (PROGRAMFILES, PROGRAMDATA, RESOLVE_INSTALL_DIR)
    and scans all fixed drives. No hardcoded drive letters.

    Returns:
        List of directory paths to check for fusionscript.dll
    """
    import string
    candidates: list[str] = []
    subdir = r"Blackmagic Design\DaVinci Resolve"

    env_override = os.environ.get("RESOLVE_INSTALL_DIR", "")
    if env_override:
        candidates.append(env_override)

    for env_var in ["PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMDATA"]:
        base = os.environ.get(env_var, "")
        if base:
            candidates.append(str(Path(base) / subdir))

    sys_drive = os.environ.get("SystemDrive", "C:")
    candidates.append(str(Path(sys_drive) / "Program Files" / subdir))
    candidates.append(str(Path(sys_drive) / "ProgramData" / subdir))

    for letter in string.ascii_uppercase:
        p = Path(f"{letter}:\\Program Files") / subdir
        if p.exists():
            candidates.append(str(p))

    return candidates


def _find_resolve_install_dir() -> Optional[Path]:
    """Find DaVinci Resolve installation directory by checking common paths.

    Returns:
        Path to DaVinci Resolve install dir (containing fusionscript.dll), or None
    """
    for candidate in _build_resolve_candidates():
        p = Path(candidate)
        if (p / "fusionscript.dll").exists():
            return p
    return None


def _is_resolve_running() -> bool:
    """Check if DaVinci Resolve (Resolve.exe) is running as a process.

    Returns:
        True if Resolve.exe is found in running processes
    """
    try:
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq Resolve.exe", "/NH"],
            capture_output=True, text=True, timeout=5
        )
        return "Resolve.exe" in result.stdout
    except Exception:
        return False


def _try_import_resolve() -> Any:
    """Attempt to import the DaVinciResolveScript module.

    Searches standard installation paths for the module, sets RESOLVE_SCRIPT_LIB
    to point to fusionscript.dll in the detected install directory, then imports.

    Returns:
        The Resolve object if successful, None otherwise
    """
    sys_name = sys.platform
    paths = _RESOLVE_API_PATHS.get(sys_name, [])

    # Check environment variable
    env_path = os.environ.get("RESOLVE_SCRIPT_API", "")
    if env_path:
        paths.insert(0, env_path)

    # Find install dir and set RESOLVE_SCRIPT_LIB for DaVinciResolveScript.py
    # (which hardcodes "C:\Program Files" on line 42)
    install_dir = _find_resolve_install_dir()
    if install_dir:
        dll_path = str(install_dir / "fusionscript.dll")
        if Path(dll_path).exists():
            os.environ["RESOLVE_SCRIPT_LIB"] = dll_path

    for p in paths:
        if Path(p).exists() and p not in sys.path:
            sys.path.append(p)

    try:
        import DaVinciResolveScript as dvr  # type: ignore[import-untyped]
        resolve = dvr.scriptapp("Resolve")
        return resolve
    except ImportError:
        return None
    except Exception:
        return None


def _check_resolve_running() -> Any:
    """Check if DaVinci Resolve is running and Scripting API is available.

    First checks if Resolve.exe process is running. If so, attempts to
    connect via Scripting API.

    Returns:
        The Resolve object if available, None otherwise
    """
    if not _is_resolve_running():
        return None
    resolve = _try_import_resolve()
    if resolve is None:
        return None
    try:
        pm = resolve.GetProjectManager()
        if pm is None:
            return None
        return resolve
    except Exception:
        return None


def _drt_sandbox_export(
    resolve: Any,
    xml_path: Path,
    output_path: Path,
    timeline_name: str = "Imported",
) -> bool:
    """Export DRT via a temporary sandbox project.

    Creates a temporary DaVinci project, imports the XML, exports DRT,
    then restores the user's original project. Never touches user's project.

    DaVinci API facts used:
    - DeleteTimeline() does NOT exist -> sandbox project is the only clean path
    - DeleteProject() only works on unloaded projects
    - CloseProject() closes without saving
    - LoadProject(name) loads an existing project

    Args:
        resolve: The DaVinci Resolve object
        xml_path: Path to the FCP7 XML file
        output_path: Path to write the .drt file
        timeline_name: Name for the imported timeline

    Returns:
        True if successful, False otherwise
    """
    try:
        pm = resolve.GetProjectManager()
        original_project = pm.GetCurrentProject()
        original_name = original_project.GetName() if original_project else None

        # Create unique temp project name
        temp_name = f"pr2resolve_drt_{int(time.time())}"
        print(f"  Creating temporary project: \"{temp_name}\"")

        # Close current project first if one is open
        if original_project is not None:
            pm.SaveProject()
            pm.CloseProject(original_project)

        project = pm.CreateProject(temp_name)
        if project is None:
            print("  Error: Could not create temporary project")
            # Try to restore original
            if original_name:
                pm.LoadProject(original_name)
            return False

        media_pool = project.GetMediaPool()

        # Strip <file> elements from a temp XML copy before DRT import.
        # Offline media paths get encoded into the DRT's SeqContainer
        # as <MediaFilePath> entries that crash DaVinci on reimport.
        # Stripping them from a temp copy avoids corruption while
        # keeping the user's output XML intact.
        drt_import_xml = xml_path
        temp_stripped = None
        try:
            tree = ET.parse(str(xml_path))
            has_files = tree.find(".//file") is not None
            if has_files:
                stripped = copy.deepcopy(tree.getroot())
                for ci in stripped.iter("clipitem"):
                    fi = ci.find("file")
                    if fi is not None:
                        ci.remove(fi)
                temp_stripped = xml_path.parent / f"_pr2resolve_stripped_{int(time.time())}.xml"
                ET.ElementTree(stripped).write(
                    str(temp_stripped), encoding="utf-8",
                    xml_declaration=True
                )
                # Fix up DOCTYPE that ET strips
                content = temp_stripped.read_text(encoding="utf-8")
                content = content.replace(
                    '<?xml version="1.0" encoding="utf-8"?>',
                    '<?xml version="1.0" encoding="UTF-8"?>\n' + FCP7_DOCTYPE
                )
                temp_stripped.write_text(content, encoding="utf-8")
                drt_import_xml = temp_stripped
        except Exception:
            pass  # fall back to original file

        timeline = media_pool.ImportTimelineFromFile(
            str(drt_import_xml),
            {"timelineName": timeline_name, "importSourceClips": False},
        )
        if temp_stripped is not None and temp_stripped.exists():
            temp_stripped.unlink(missing_ok=True)

        if timeline is not None:
            print("  (timeline structure imported, media to be relinked on target machine)")

        if timeline is None:
            print("  Error: Failed to import timeline from XML")
            # Clean up and restore
            pm.CloseProject(project)
            if original_name:
                pm.LoadProject(original_name)
            return False

        print(f"  Timeline imported: {timeline.GetName()}")

        # Export DRT
        export_result = timeline.Export(
            str(output_path),
            resolve.EXPORT_DRT,
        )
        if not export_result:
            print("  Error: DRT export failed")
            pm.CloseProject(project)
            if original_name:
                pm.LoadProject(original_name)
            return False

        print(f"  DRT exported: {output_path}")

        # Restore user's original project
        pm.CloseProject(project)
        if original_name:
            pm.LoadProject(original_name)
            print(f"  Restored: \"{original_name}\"")
        else:
            print("  (no original project to restore)")

        # Clean up: delete temp project (only deletable when unloaded)
        try:
            pm.DeleteProject(temp_name)
        except Exception:
            pass  # best-effort cleanup

        return True

    except Exception as e:
        print(f"  Error during DRT sandbox: {e}")
        # Best-effort restore
        try:
            pm = resolve.GetProjectManager()
            current = pm.GetCurrentProject()
            if current is not None and original_name and (
                current.GetName() if hasattr(current, 'GetName') else ""
            ) != original_name:
                pm.CloseProject(current)
                pm.LoadProject(original_name)
        except Exception:
            pass
        return False


def _ensure_resolve_running(timeout: int = 60) -> Any:
    """Ensure DaVinci Resolve is running and the Scripting API is available.

    If DaVinci is not running, auto-launches it and polls until the API
    becomes available (typically 10-30 seconds for a cold start).

    Args:
        timeout: Maximum seconds to wait for DaVinci to become ready

    Returns:
        The Resolve object if available, None otherwise
    """
    # 1. Quick check: already running?
    resolve = _check_resolve_running()
    if resolve is not None:
        return resolve

    # 2. Auto-launch
    print("  Launching DaVinci Resolve...")
    install_dir = _find_resolve_install_dir()
    if not install_dir:
        print("  Could not find DaVinci installation.")
        return None
    exe = install_dir / "Resolve.exe"
    if not exe.exists():
        print(f"  Resolve.exe not found at: {exe}")
        return None
    try:
        subprocess.Popen(
            [str(exe)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=0x00000008 if sys.platform == "win32" else 0,
        )
        print(f"  Started: {exe}")
    except Exception as e:
        print(f"  Failed to start: {e}")
        return None

    # 3. Poll until API available
    start = time.time()
    poll_interval = 2
    while time.time() - start < timeout:
        time.sleep(poll_interval)
        elapsed = int(time.time() - start)
        print(f"  Waiting for DaVinci... ({elapsed}s)")
        resolve = _check_resolve_running()
        if resolve is not None:
            print(f"  DaVinci ready after {elapsed}s.")
            return resolve
        # Increase poll interval after first few attempts
        if elapsed > 15:
            poll_interval = 5

    print(f"  DaVinci did not start within {timeout}s.")
    return None


def _drt_supplement_lumetri(
    resolve: Any,
    lumetri_data: dict[str, dict[str, float]],
) -> bool:
    """Supplement DaVinci timeline with Lumetri color data from .prproj.

    Maps PR Lumetri parameters to DaVinci Color Corrector nodes.

    Args:
        resolve: The DaVinci Resolve object
        lumetri_data: Dict mapping clip name -> {param_name: value}

    Returns:
        True if at least one clip was updated
    """
    # Lumetri -> DaVinci Color parameter mapping
    _LUMETRI_TO_DAVINCI: dict[str, str] = {
        "曝光": "Gain",        # Exposure -> Gain wheel
        "对比度": "Contrast",
        "高光": "Highlights",
        "阴影": "Shadows",
        "白色": "Gain",        # Whites -> Gain (not "Whites")
        "黑色": "Lift",        # Blacks -> Lift (not "Blacks")
        "饱和度": "Saturation",
        "色温": "Temperature",
        "色彩": "Tint",
    }

    try:
        pm = resolve.GetProjectManager()
        project = pm.GetCurrentProject()
        if project is None:
            return False

        timeline = project.GetCurrentTimeline()
        if timeline is None:
            return False

        updated = 0
        for track_idx in range(1, timeline.GetTrackCount("video") + 1):
            clips = timeline.GetItemListInTrack("video", track_idx)
            if not clips:
                continue
            for clip in clips:
                clip_name = clip.GetName()
                if clip_name not in lumetri_data:
                    continue

                params = lumetri_data[clip_name]
                if not params:
                    continue
                try:
                    color = clip.GetColor()
                    if color is None:
                        continue
                    for pr_name, da_name in _LUMETRI_TO_DAVINCI.items():
                        if pr_name not in params:
                            continue
                        try:
                            color.SetCurrentParameterByName(da_name, params[pr_name])
                            updated += 1
                        except Exception:
                            pass
                except Exception:
                    pass

        print(f"  Lumetri data applied to {updated} parameters")
        return updated > 0

    except Exception as e:
        print(f"  Error supplementing Lumetri: {e}")
        return False
