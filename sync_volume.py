import os
import io
import sys
from databricks.sdk import WorkspaceClient
from pathlib import Path

# Config
APP_HOME = Path("/home/app")
LOCAL_DIR = APP_HOME / ".openclaw"
REMOTE_VOL = os.getenv("MY_UC_VOLUME_PATH")

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
        
    print(f"--- Starting Push to {REMOTE_VOL} ---")
    
    # Expanded list to catch all LakeClaw personality and state files
    valid_exts = [".md", ".json", ".jsonl", ".db", ".key", ".yaml"]
    
    # Pointing directly at LOCAL_DIR (.openclaw) to avoid hidden folder skipping
    for local_file in LOCAL_DIR.rglob("*"):
        # SKIP the config file and any symlinks to prevent loops
        if local_file.name == "openclaw.json" or local_file.is_symlink():
            continue

        if local_file.is_file() and local_file.suffix.lower() in valid_exts:
            # We want the path relative to /home/app
            # So /home/app/.openclaw/SOUL.md becomes .openclaw/SOUL.md
            relative_path = local_file.relative_to(APP_HOME)
            remote_path = f"{REMOTE_VOL}/{relative_path}"
            
            try:
                with open(local_file, "rb") as f:
                    w.files.upload(file_path=remote_path, contents=f, overwrite=True)
                print(f"  [SYNCED] {relative_path}")
            except Exception as e:
                print(f"  [SKIPPED] {relative_path}: {e}")
                
    print(f"--- Push Complete ---")

if __name__ == "__main__":
    client = WorkspaceClient()
    if len(sys.argv) > 1 and sys.argv[1] == "--push":
        push_to_volume(client)
    else:
        sync_from_volume(client, REMOTE_VOL, APP_HOME)