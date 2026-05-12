#!/usr/bin/env python3
import os, sys, subprocess, shutil, tempfile, pathlib

TOKEN = os.environ["GITHUB_TOKEN"]
REPO = os.environ["GITHUB_REPOSITORY"]
REPO_URL = f"https://x-access-token:{TOKEN}@github.com/{REPO}.git"

MAX_BATCH_BYTES = 500 * 1024 * 1024   # 500 MB per commit

def run(cmd, check=True, **kwargs):
    proc = subprocess.run(cmd, capture_output=True, text=True, **kwargs)
    if check and proc.returncode != 0:
        print(f"Error running {' '.join(cmd)}: {proc.stderr}")
        sys.exit(1)
    return proc.stdout.strip()

def main():
    tmpdir = tempfile.mkdtemp()
    clone_dir = os.path.join(tmpdir, "repo.git")
    try:
        print("Cloning bare repository (metadata only)...")
        run(["git", "clone", "--bare", "--depth=1", "--filter=blob:none", REPO_URL, clone_dir])
        os.environ["GIT_DIR"] = clone_dir

        os.environ["GIT_AUTHOR_NAME"] = "github-actions[bot]"
        os.environ["GIT_AUTHOR_EMAIL"] = "github-actions[bot]@users.noreply.github.com"
        os.environ["GIT_COMMITTER_NAME"] = "github-actions[bot]"
        os.environ["GIT_COMMITTER_EMAIL"] = "github-actions[bot]@users.noreply.github.com"

        index_file = os.path.join(tmpdir, "index.tmp")
        os.environ["GIT_INDEX_FILE"] = index_file

        # Gather all files on disk
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

        # Convert to POSIX relative paths
        current_paths = set(pathlib.Path(f).as_posix() for f in current_files)

        old_tree_out = run(["git", "ls-tree", "-r", "--name-only", "main"])
        old_paths = set(old_tree_out.splitlines()) if old_tree_out.strip() else set()
        to_delete = old_paths - current_paths

        parent_commit = run(["git", "rev-parse", "main"])

        # Build the full new tree (we'll create commits incrementally)
        # First, create the index with all changes
        run(["git", "read-tree", parent_commit])

        for path in sorted(to_delete):
            run(["git", "update-index", "--force-remove", path], check=False)

        # Add all files, collecting their sizes
        file_info = []
        for fpath in sorted(current_paths):
            sha = run(["git", "hash-object", "-w", fpath])
            size = os.path.getsize(fpath)
            file_info.append((fpath, sha, size))
            run(["git", "update-index", "--add", "--cacheinfo", "100644", sha, fpath])

        new_tree = run(["git", "write-tree"])
        parent_tree = run(["git", "rev-parse", f"{parent_commit}^{{tree}}"])

        if new_tree == parent_tree:
            print("No changes detected. Skipping push.")
            return

        # If total size <= MAX_BATCH_BYTES, do a single commit
        total_size = sum(s for _, _, s in file_info)
        if total_size <= MAX_BATCH_BYTES:
            new_commit = run(["git", "commit-tree", "-p", parent_commit,
                              "-m", "Sync downloads [skip ci]", new_tree])
            run(["git", "update-ref", "refs/heads/main", new_commit])
            run(["git", "push", "origin", "main"])
            print(f"Single commit {new_commit} ({len(file_info)} files, {total_size/(1024*1024):.1f} MB)")
            return

        # Otherwise, split into cumulative batches
        remaining = sorted(file_info, key=lambda x: x[2])  # sort by size (optional)
        batches = []
        batch = []
        batch_size = 0
        for fpath, sha, size in remaining:
            if batch_size + size > MAX_BATCH_BYTES and batch:
                batches.append(batch)
                batch = []
                batch_size = 0
            batch.append((fpath, sha, size))
            batch_size += size
        if batch:
            batches.append(batch)

        print(f"Total {len(file_info)} files, {total_size/(1024*1024):.1f} MB -> {len(batches)} batches")

        # Create commits cumulatively
        current_parent = parent_commit
        for i, batch in enumerate(batches, 1):
            # Start with parent's tree
            run(["git", "read-tree", current_parent])

            # Apply deletions only in first batch
            if i == 1 and to_delete:
                for path in sorted(to_delete):
                    run(["git", "update-index", "--force-remove", path], check=False)

            # Add batch files
            for fpath, sha, size in batch:
                run(["git", "update-index", "--add", "--cacheinfo", "100644", sha, fpath])

            batch_tree = run(["git", "write-tree"])
            batch_commit = run(["git", "commit-tree", "-p", current_parent,
                                "-m", f"Sync downloads batch {i}/{len(batches)} [skip ci]", batch_tree])
            run(["git", "update-ref", "refs/heads/main", batch_commit])
            run(["git", "push", "origin", "main"])
            print(f"Batch {i}/{len(batches)}: {batch_commit} ({len(batch)} files)")
            current_parent = batch_commit

        print("All batches pushed successfully.")

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

if __name__ == "__main__":
    main()
