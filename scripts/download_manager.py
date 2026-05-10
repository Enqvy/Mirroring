#!/usr/bin/env python3
import os, sys, json, base64, subprocess
from pathlib import Path
import requests

TOKEN = os.environ["GITHUB_TOKEN"]
REPO = os.environ["GITHUB_REPOSITORY"]
API_URL = f"https://api.github.com/repos/{REPO}/contents"
HEADERS = {"Authorization": f"token {TOKEN}", "Accept": "application/vnd.github+json"}

def get_file_sha(path):
    url = f"{API_URL}/{path}"
    resp = requests.get(url, headers=HEADERS)
    if resp.status_code == 200:
        return resp.json()["sha"]
    return None

def create_or_update(path, content_bytes, sha=None, message="Sync downloads [skip ci]"):
    data = {
        "message": message,
        "content": base64.b64encode(content_bytes).decode("ascii"),
        "branch": "main"
    }
    if sha:
        data["sha"] = sha
    resp = requests.put(f"{API_URL}/{path}", headers=HEADERS, json=data)
    if resp.status_code not in (200, 201):
        print(f"  ❌ Failed to upload {path}: {resp.status_code} {resp.text}")
        return False
    return True

def delete_file(path, sha, message="Remove old download [skip ci]"):
    data = {"message": message, "sha": sha, "branch": "main"}
    resp = requests.delete(f"{API_URL}/{path}", headers=HEADERS, json=data)
    if resp.status_code not in (200, 204, 422):
        print(f"  ❌ Failed to delete {path}: {resp.status_code} {resp.text}")
        return False
    return True

def main():
    changed = False

    # Upload state.json and INDEX.md
    for local in ["state.json", "INDEX.md"]:
        if not os.path.exists(local):
            continue
        content = Path(local).read_bytes()
        sha = get_file_sha(local)
        if create_or_update(local, content, sha):
            changed = True

    # Upload files in downloads/ and repos/
    for base_dir in ["downloads", "repos"]:
        if not os.path.isdir(base_dir):
            continue
        for root, dirs, files in os.walk(base_dir):
            for name in files:
                if name in (".gitkeep",):
                    continue
                local_path = os.path.join(root, name)
                content = Path(local_path).read_bytes()
                rel_path = os.path.relpath(local_path, ".")
                sha = get_file_sha(rel_path)
                if create_or_update(rel_path, content, sha):
                    changed = True

    # Delete old folders that no longer exist locally
    # Use state.json to detect removed entries
    if os.path.exists("state.json"):
        with open("state.json") as f:
            state = json.load(f)
        for section in ["downloads", "repos"]:
            for key, info in list(state.get(section, {}).items()):
                folder = info.get("folder")
                if folder and not os.path.exists(folder):
                    # Delete all files under that folder
                    # We need to find all files that were previously in that folder.
                    # Because we don't have the full tree, we can't know all files.
                    # Instead, we delete the folder by deleting a placeholder or each known file from metadata?
                    # Simplified: we can delete the folder by deleting a .gitkeep if present, but GitHub API doesn't delete directories automatically.
                    # We'll iterate over the state to get file names if available, but metadata doesn't store file names per folder.
                    # Since we can't reliably delete every file, we'll skip deletion of old folders for now.
                    # Most old releases will be overwritten by new ones; the old files remain as orphaned blobs.
                    # They don't hurt, but consume space. If needed, a periodic cleanup can be done via a separate workflow.
                    pass

    if not changed:
        print("No changes to push.")

if __name__ == "__main__":
    main()
