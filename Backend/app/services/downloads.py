"""
Resolve the REAL Windows Downloads folder of the interactive user and verify that
files were actually written there.

Why this exists: the export used `os.path.expanduser("~")\\Downloads`, which is the
profile of *whatever account the backend process runs as*. When the API runs as a
service account (or a different login) than the person using the app, files were
saved into the service account's profile and the user never saw them — while the UI
still reported success. This module:

  1. honours an explicit EXPORT_DOWNLOADS_DIR override (highest priority),
  2. otherwise targets the INTERACTIVE console user's Downloads — OneDrive/redirection
     aware — even when the backend runs as a different (service) account,
  3. falls back to the current process user's known Downloads folder, then
     %USERPROFILE%\\Downloads,
  4. never returns a temp dir or the project folder,
  5. always returns an ABSOLUTE path and creates it if missing.

Everything is best-effort and defensive: any tier that fails is logged and skipped.
"""
import os
import logging

logger = logging.getLogger(__name__)

# FOLDERID_Downloads / its User-Shell-Folders GUID.
_DOWNLOADS_GUID = "{374DE290-123F-4565-9164-39C4925E467B}"


def _known_folder_downloads() -> str | None:
    """Current process user's Downloads via the Shell Known Folder API (OneDrive-aware)."""
    try:
        import ctypes
        from ctypes import wintypes

        class GUID(ctypes.Structure):
            _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                        ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

        fid = GUID(0x374DE290, 0x123F, 0x4565,
                   (ctypes.c_ubyte * 8)(0x91, 0x64, 0x39, 0xC4, 0x92, 0x5E, 0x46, 0x7B))
        ptr = ctypes.c_wchar_p()
        rc = ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(fid), 0, None, ctypes.byref(ptr))
        if rc == 0 and ptr.value:
            path = ptr.value
            ctypes.windll.ole32.CoTaskMemFree(ptr)
            return path
    except Exception as e:  # noqa: BLE001
        logger.debug("Known-folder Downloads lookup failed: %s", e)
    return None


def _interactive_user_downloads() -> str | None:
    """
    Downloads folder of the user logged in at the physical console — resolved even when
    THIS process runs as a different (service) account. Reads that user's own registry
    hive so OneDrive redirection is honoured.
    """
    try:
        import ctypes
        from ctypes import wintypes
        import winreg

        # 1) Active console session → its user name.
        wtsapi = ctypes.windll.wtsapi32
        kernel = ctypes.windll.kernel32
        session_id = kernel.WTSGetActiveConsoleSessionId()
        if session_id in (0xFFFFFFFF, None):
            return None

        def _wts_str(info_class):
            buf = ctypes.c_wchar_p()
            n = wintypes.DWORD(0)
            if not wtsapi.WTSQuerySessionInformationW(0, session_id, info_class,
                                                      ctypes.byref(buf), ctypes.byref(n)):
                return None
            val = buf.value
            wtsapi.WTSFreeMemory(buf)
            return val

        username = _wts_str(5)   # WTSUserName
        domain   = _wts_str(7)   # WTSDomainName
        if not username:
            return None
        account = f"{domain}\\{username}" if domain else username

        # 2) User name → SID string.
        sid = ctypes.create_string_buffer(256)
        cb_sid = wintypes.DWORD(256)
        dom = ctypes.create_unicode_buffer(256)
        cb_dom = wintypes.DWORD(256)
        use = wintypes.DWORD()
        if not ctypes.windll.advapi32.LookupAccountNameW(
                None, account, sid, ctypes.byref(cb_sid), dom, ctypes.byref(cb_dom), ctypes.byref(use)):
            return None
        sid_str = ctypes.c_wchar_p()
        if not ctypes.windll.advapi32.ConvertSidToStringSidW(sid, ctypes.byref(sid_str)):
            return None
        sid_text = sid_str.value
        ctypes.windll.kernel32.LocalFree(sid_str)

        # 3) SID → profile directory (ProfileImagePath).
        profile_dir = None
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                rf"SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList\{sid_text}") as k:
                profile_dir = os.path.expandvars(winreg.QueryValueEx(k, "ProfileImagePath")[0])
        except OSError:
            pass

        # 4) That user's Downloads from THEIR hive (HKEY_USERS\<SID>), OneDrive-aware.
        downloads = None
        try:
            with winreg.OpenKey(winreg.HKEY_USERS,
                                rf"{sid_text}\Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as k:
                raw = winreg.QueryValueEx(k, _DOWNLOADS_GUID)[0]
            low = raw.lower()
            if "%userprofile%" in low and profile_dir:
                # Expand %USERPROFILE% against the TARGET user's profile (not this
                # process's), preserving the rest of the string's original case.
                idx = low.index("%userprofile%")
                downloads = profile_dir + raw[idx + len("%userprofile%"):]
            else:
                downloads = os.path.expandvars(raw)
        except OSError:
            pass

        if not downloads and profile_dir:
            downloads = os.path.join(profile_dir, "Downloads")

        if downloads and os.path.isdir(os.path.dirname(downloads) or downloads):
            return os.path.normpath(downloads)
    except Exception as e:  # noqa: BLE001
        logger.debug("Interactive-user Downloads lookup failed: %s", e)
    return None


def resolve_downloads_dir() -> str:
    """
    Absolute path to the folder exports should be saved in (the user's Downloads),
    created if missing. Never a temp dir or the project folder.
    """
    # 1) Explicit override — wins over everything (admin-configurable).
    override = os.environ.get("EXPORT_DOWNLOADS_DIR")
    if override:
        path = os.path.abspath(os.path.expandvars(override))
        os.makedirs(path, exist_ok=True)
        logger.info("Downloads dir (EXPORT_DOWNLOADS_DIR override) = %s", path)
        return path

    source = None
    path = _interactive_user_downloads()
    if path:
        source = "interactive-console-user"
    if not path:
        path = _known_folder_downloads()
        if path:
            source = "known-folder"
    if not path:
        path = os.path.join(os.path.expanduser("~"), "Downloads")
        source = "userprofile-fallback"

    path = os.path.abspath(path)
    os.makedirs(path, exist_ok=True)
    logger.info("Downloads dir (%s) = %s", source, path)
    return path


def _safe_report_folder(name: str) -> str:
    """Filesystem-safe, human-readable subfolder name for a report type."""
    cleaned = "".join(ch if (ch.isalnum() or ch in " &-_") else "_" for ch in str(name or "")).strip()
    return cleaned or "General"


def resolve_reports_dir(report_type: str | None = None) -> str:
    """
    The single ROOT that holds every on-disk report export: one "Reports" folder, with
    one subfolder PER report type inside it — Reports\\<Report Type>\\… — created on
    demand. This is the storage layout the client asked for:

        Reports\\
          ├── Tracker\\
          ├── String Combiner\\
          ├── <any other report type>\\
          └── …

    The per-type subfolder name is the LIVE equipment/report type passed in — nothing is
    hardcoded, so every current and future report type automatically lands under Reports
    without touching this function. Base location is configurable via REPORTS_EXPORT_DIR
    and otherwise defaults to <Downloads>\\Reports (the interactive user's Downloads,
    resolved by resolve_downloads_dir). Always returns an ABSOLUTE path and creates it.
    """
    base = os.environ.get("REPORTS_EXPORT_DIR")
    base = os.path.abspath(os.path.expandvars(base)) if base else os.path.join(resolve_downloads_dir(), "Reports")
    os.makedirs(base, exist_ok=True)

    if report_type:
        base = os.path.join(base, _safe_report_folder(report_type))
        os.makedirs(base, exist_ok=True)
    logger.info("Reports dir = %s", base)
    return base


def verify_files_written(paths) -> None:
    """
    Raise RuntimeError if any expected output file is missing or empty, so a failed
    write surfaces as an error instead of a false success. `paths` is an iterable of
    absolute file paths.
    """
    missing = [p for p in paths if not (os.path.isfile(p) and os.path.getsize(p) > 0)]
    if missing:
        raise RuntimeError(
            f"Export reported success but {len(missing)} file(s) are missing on disk: "
            f"{missing[:5]}{' …' if len(missing) > 5 else ''}"
        )
