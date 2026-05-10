#!/usr/bin/env python3
import os, sys, subprocess, shutil, tempfile, pathlib

TOKEN = os.environ["GITHUB_TOKEN"]
REPO = os.environ["GITHUB_REPOSITORY"]
REPO_URL = f"https://x-access-token:{TOKEN}@github.com/{REPO}.git"

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

        index_file = os.path.join(tmpdir, "index.tmp")
        os.environ["GIT_INDEX_FILE"] = index_file

        run(["git", "read-tree", "main"])

        for path in ["downloads", "repos", "state.json", "INDEX.md"]:
            run(["git", "rm", "--cached", "-r", "--quiet", "--", path], check=False)

        files_to_add = []
        for dirpath, _, filenames in os.walk("downloads"):
            for fname in filenames:
                if fname == ".gitkeep":
                    continue
                files_to_add.append(os.path.join(dirpath, fname))
        for dirpath, _, filenames in os.walk("repos"):
            for fname in filenames:
                files_to_add.append(os.path.join(dirpath, fname))
        files_to_add.append("state.json")
        files_to_add.append("INDEX.md")

        added = False
        for fpath in files_to_add:
            if not os.path.exists(fpath):
                continue
            sha = run(["git", "hash-object", "-w", fpath])
            rel_path = pathlib.Path(fpath).as_posix()
            run(["git", "update-index", "--add", "--cacheinfo", "100644", sha, rel_path])
            added = True

        if not added:
            print("No files to add.")
            return

        new_tree = run(["git", "write-tree"])
        print(f"New tree: {new_tree}")

        parent_commit = run(["git", "rev-parse", "main"])
        new_commit = run(["git", "commit-tree", "-p", parent_commit, "-m", "Sync downloads [skip ci]", new_tree])
        print(f"New commit: {new_commit}")

        run(["git", "update-ref", "refs/heads/main", new_commit])
        run(["git", "push", "origin", "main"])
        print("Push successful.")

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

if __name__ == "__main__":
    main()
