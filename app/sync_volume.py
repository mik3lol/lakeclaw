import json
import os
import sys
from databricks.sdk import WorkspaceClient
from pathlib import Path

# Config
APP_HOME = Path("/home/app")
LOCAL_DIR = APP_HOME / ".openclaw"
REMOTE_VOL = os.getenv("MY_UC_VOLUME_PATH")
PUSH_MANIFEST_PATH = APP_HOME / ".lakeclaw_push_manifest.json"
VALID_EXTS = frozenset({".md", ".json", ".jsonl", ".db", ".key", ".yaml"})


def _fingerprint(path: Path) -> tuple[int, int]:
    st = path.stat()
    mtime_ns = getattr(st, "st_mtime_ns", None)
    if mtime_ns is None:
        mtime_ns = int(st.st_mtime * 1_000_000_000)
    return mtime_ns, st.st_size


def iter_push_candidate_files():
    """Paths under .openclaw that are eligible for UC push (same rules as upload)."""
    for local_file in LOCAL_DIR.rglob("*"):
        if local_file.name == "openclaw.json" or local_file.is_symlink():
            continue
        if local_file.is_file() and local_file.suffix.lower() in VALID_EXTS:
            yield local_file


def _load_push_manifest() -> dict:
    if not PUSH_MANIFEST_PATH.is_file():
        return {}
    try:
        data = json.loads(PUSH_MANIFEST_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        print(f"Push manifest unreadable ({e}); starting fresh.")
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def _save_push_manifest(manifest: dict) -> None:
    tmp = PUSH_MANIFEST_PATH.with_suffix(".json.tmp")
    payload = json.dumps(manifest, indent=0, sort_keys=True)
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, PUSH_MANIFEST_PATH)
    os.chmod(PUSH_MANIFEST_PATH, 0o600)


def seed_push_manifest_from_local() -> None:
    """Record current local fingerprints so the next push skips unchanged files (e.g. after UC pull)."""
    manifest = {}
    for local_file in iter_push_candidate_files():
        key = local_file.relative_to(APP_HOME).as_posix()
        mtime_ns, size = _fingerprint(local_file)
        manifest[key] = {"mtime_ns": mtime_ns, "size": size}
    try:
        _save_push_manifest(manifest)
    except OSError as e:
        print(f"Could not write push manifest: {e}")


def sync_from_volume(w, remote_path, local_root):
    """Downloads everything from the Volume to the App container."""
    try:
        for entry in w.files.list_directory_contents(remote_path):
            relative_path = entry.path.replace(REMOTE_VOL, "").lstrip("/")
            local_path = local_root / relative_path

            if entry.is_directory:
                local_path.mkdir(parents=True, exist_ok=True)
                sync_from_volume(w, entry.path, local_root)
            else:
                local_path.parent.mkdir(parents=True, exist_ok=True)
                response = w.files.download(entry.path)
                with open(local_path, "wb") as f:
                    f.write(response.contents.read())
                os.chmod(local_path, 0o600)
    except Exception as e:
        print(f"Pull error: {e}")


def push_to_volume(w):
    if not REMOTE_VOL:
        print("MY_UC_VOLUME_PATH not set. Skipping push.")
        return

    print("--- Starting Push to UC volume (manifest incremental) ---")

    manifest = _load_push_manifest()
    dirty = False
    current_keys = set()

    for local_file in iter_push_candidate_files():
        key = local_file.relative_to(APP_HOME).as_posix()
        current_keys.add(key)
        mtime_ns, size = _fingerprint(local_file)
        prev = manifest.get(key)
        if (
            prev
            and prev.get("mtime_ns") == mtime_ns
            and prev.get("size") == size
        ):
            continue

        remote_path = f"{REMOTE_VOL}/{key}"
        try:
            with open(local_file, "rb") as f:
                w.files.upload(file_path=remote_path, contents=f, overwrite=True)
            manifest[key] = {"mtime_ns": mtime_ns, "size": size}
            dirty = True
            print(f"  [SYNCED] {key}")
        except Exception as e:
            print(f"  [SKIPPED] {key}: {e}")

    for key in list(manifest.keys()):
        if key not in current_keys:
            del manifest[key]
            dirty = True

    if dirty:
        try:
            _save_push_manifest(manifest)
        except OSError as e:
            print(f"  [WARN] Could not save push manifest: {e}")

    print("--- Push Complete ---")


if __name__ == "__main__":
    # The App runtime auto-injects OAuth credentials (CLIENT_ID/SECRET) alongside
    # the PAT from Databricks secrets. The SDK refuses multiple auth methods.
    # Hide the PAT so the SDK authenticates with OAuth only.
    saved_token = os.environ.pop("DATABRICKS_TOKEN", None)
    try:
        client = WorkspaceClient()
    finally:
        if saved_token is not None:
            os.environ["DATABRICKS_TOKEN"] = saved_token

    if len(sys.argv) > 1 and sys.argv[1] == "--push":
        push_to_volume(client)
    else:
        if REMOTE_VOL:
            sync_from_volume(client, REMOTE_VOL, APP_HOME)
        else:
            print("MY_UC_VOLUME_PATH not set. Skipping pull.")
        seed_push_manifest_from_local()
