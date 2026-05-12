#!/usr/bin/env python3
import os, sys, pathlib, hashlib, requests

TOKEN = os.environ["GITHUB_TOKEN"]
REPO = os.environ["GITHUB_REPOSITORY"]
API_BASE = f"https://api.github.com/repos/{REPO}"
HEADERS = {
    "Authorization": f"token {TOKEN}",
    "Accept": "application/vnd.github.v3+json",
    "User-Agent": "MirrorBot/2.0",
}

def git_hash_object(path):
    """Compute a Git blob SHA-1 without writing to a database."""
    header = f"blob {os.path.getsize(path)}\0"
    with open(path, "rb") as f:
        content = f.read()
    return hashlib.sha1(header.encode() + content).hexdigest()

def upload_blob(path):
    """Upload blob content via API, return SHA."""
    with open(path, "rb") as f:
        content = f.read()
    # GitHub expects base64-encoded content
    import base64
    payload = {"content": base64.b64encode(content).decode(), "encoding": "base64"}
    resp = requests.post(f"{API_BASE}/git/blobs", headers=HEADERS, json=payload)
    resp.raise_for_status()
    return resp.json()["sha"]

def main():
    # 1. Get the latest commit SHA and its tree SHA
    ref_resp = requests.get(f"{API_BASE}/git/ref/heads/main", headers=HEADERS)
    ref_resp.raise_for_status()
    latest_commit_sha = ref_resp.json()["object"]["sha"]

    commit_resp = requests.get(f"{API_BASE}/git/commits/{latest_commit_sha}", headers=HEADERS)
    commit_resp.raise_for_status()
    base_tree_sha = commit_resp.json()["tree"]["sha"]

    # 2. Collect local file paths
    current_files = []
    for dirpath, _, filenames in os.walk("downloads"):
        for fname in filenames:
            if fname == ".gitkeep": continue
            current_files.append(os.path.join(dirpath, fname))
    for dirpath, _, filenames in os.walk("repos"):
        for fname in filenames:
            current_files.append(os.path.join(dirpath, fname))
    for f in ["state.json", "INDEX.md"]:
        if os.path.exists(f):
            current_files.append(f)

    if not current_files:
        print("No files to add.")
        return

    # 3. Build new tree entries
    tree_entries = []
    for f in sorted(current_files):
        posix_path = pathlib.Path(f).as_posix()
        # Compute local SHA-1 (to check if blob already exists)
        sha_local = git_hash_object(f)
        # Upload blob (idempotent – GitHub deduplicates)
        sha_remote = upload_blob(f)
        # The SHA-1 should match, but we use the local one (Git's hash) for tree entry
        tree_entries.append({
            "path": posix_path,
            "mode": "100644",
            "type": "blob",
            "sha": sha_local
        })

    # 4. Create the new tree (replace old tree, omitting deleted files)
    payload = {"base_tree": base_tree_sha, "tree": tree_entries}
    tree_resp = requests.post(f"{API_BASE}/git/trees", headers=HEADERS, json=payload)
    tree_resp.raise_for_status()
    new_tree_sha = tree_resp.json()["sha"]

    if new_tree_sha == base_tree_sha:
        print("No changes detected. Skipping push.")
        return

    # 5. Create commit
    commit_payload = {
        "message": "Sync downloads [skip ci]",
        "tree": new_tree_sha,
        "parents": [latest_commit_sha]
    }
    commit_resp = requests.post(f"{API_BASE}/git/commits", headers=HEADERS, json=commit_payload)
    commit_resp.raise_for_status()
    new_commit_sha = commit_resp.json()["sha"]

    # 6. Update branch reference
    update_resp = requests.patch(
        f"{API_BASE}/git/refs/heads/main",
        headers=HEADERS,
        json={"sha": new_commit_sha, "force": False}
    )
    update_resp.raise_for_status()

    print(f"Pushed commit {new_commit_sha}")

if __name__ == "__main__":
    main()
