#!/usr/bin/env python3
"""
Mirror Manager – production release (ZIP backend).

Fetches the latest GitHub releases (or direct URLs), compresses them
into .zip containers (Deflate, level 9 by default, configurable),
and pushes to your repository.

Key features:
- Reads repo.txt for sources; supports inline flags [nocompress], [pre], [lfs].
- Incremental: skips releases when the tag hasn't changed and direct downloads
  when they’ve already been mirrored.
- Compresses everything into .zip containers (Deflate, level 9).
- Files larger than 99 MB are automatically split using zip's multi‑volume feature.
- Per‑file CRC32 checksums are shown in the file list, alongside the compression
  percentage (e.g., -12.3%).
- All tunable parameters live in config.toml, next to this script.

Usage:
  python3 scripts/download_manager.py update [--no-push]
  python3 scripts/download_manager.py commit [--msg MSG] [--no-push]
"""

import os, sys, json, re, time, shutil, subprocess, argparse, tempfile, zipfile
import shlex, fnmatch, zlib, mimetypes
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, quote
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from tqdm import tqdm

# ------------------------------------------------------------------------------
# TOML configuration support (falls back to defaults if missing)
# ------------------------------------------------------------------------------
try:
    import tomllib  # Python 3.11+
except ImportError:
    try:
        import tomli as tomllib
    except ImportError:
        tomllib = None

_DEFAULTS = {
    "split_mb": 99,
    "push_batch_bytes": 350 * 1024 * 1024,  # 350 MiB
    "max_parallel": 4,
    "compression_level": 9,                  # 0 = store, 1‑9 = deflate effort
    "compression_method": "Deflate",         # kept for compatibility, ignored
    "extract_archive_exts": [".zip", ".7z"],
    "skip_asset_exts": [
        ".sha256", ".sha256sum", ".sha512", ".sha512sum",
        ".sha1", ".sha1sum", ".md5", ".md5sum",
        ".asc", ".sig", ".sign", ".pgp",
        ".blake2b", ".blake2s", ".sha3",
        ".sha256.txt", ".sha512.txt", ".sha1.txt", ".md5.txt",
        ".sha256sums", ".sha512sums", ".sha1sums", ".md5sums",
    ],
}

def _load_config():
    config = _DEFAULTS.copy()
    script_dir = Path(__file__).resolve().parent
    cfg_file = script_dir / "config.toml"
    if cfg_file.is_file():
        if tomllib is None:
            print("⚠️  TOML library missing – using hard‑coded defaults.")
        else:
            try:
                with open(cfg_file, "rb") as fh:
                    user = tomllib.load(fh)
                for key in _DEFAULTS:
                    if key in user:
                        config[key] = user[key]
            except Exception as e:
                print(f"⚠️  config.toml parse error: {e}")
    return config

CFG = _load_config()

SPLIT_MB            = CFG["split_mb"]
PUSH_BATCH_BYTES    = CFG["push_batch_bytes"]
MAX_PARALLEL        = CFG["max_parallel"]
COMPRESSION_LEVEL   = CFG["compression_level"]
COMPRESSION_METHOD  = CFG["compression_method"]  # unused, kept for compatibility
EXTRACT_ARCHIVE_EXTS = set(CFG["extract_archive_exts"])
SKIP_ASSET_EXTS     = set(CFG["skip_asset_exts"])

# ------------------------------------------------------------------------------
# Constants
# ------------------------------------------------------------------------------
STATE_FILE = "state.json"
GITHUB_REPOSITORY = os.getenv("GITHUB_REPOSITORY", "unknown/unknown")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
UA = "Mozilla/5.0 (compatible; MirrorBot/1.0)"
VERBOSE = os.getenv("VERBOSE", "0") == "1"

URL_PATTERN = re.compile(r'(https?://[^\s]+)')
GITHUB_RELEASE_PATTERN = re.compile(
    r'https?://github\.com/([^/]+/[^/]+)/releases/(?:latest|tag/(.+))'
)
RANGE_PATTERN = re.compile(
    r'((?:https?://\S+))\s*\[(\d+)mb?[ ,\-]*(\d+)mb?\]', re.I
)
FILTER_PATTERN = re.compile(r'^(.*?)\s*\[(.*?)\]$')
SAFE_FILENAME_PATTERN = re.compile(r'[\\/:*?"<>|]')
NOCOMPRESS_COMMIT = re.compile(r'\[nocompress\]', re.I)

# ------------------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------------------
def log(msg, level="INFO"):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {level}: {msg}")

def run(cmd, check=True, quiet=False, shell=None, timeout=3600):
    quiet = quiet or not VERBOSE
    if shell is None:
        shell = any(c in cmd for c in '|&;<>()$`*?[]~')
    if not quiet:
        log(f"⚡ {cmd}", "DEBUG")
    try:
        if not shell and isinstance(cmd, str):
            proc = subprocess.run(cmd.split(), capture_output=True, text=True, timeout=timeout)
        else:
            proc = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        if proc.stdout.strip() and not quiet:
            log(f"↳ {proc.stdout.strip()}", "DEBUG")
        if check and proc.returncode != 0:
            err = proc.stderr.strip()
            log(f"❌ Command failed: {cmd}\n   {err}", "ERROR")
            raise RuntimeError(f"Command failed (exit {proc.returncode}): {cmd}")
        return proc.stdout.strip()
    except subprocess.TimeoutExpired:
        log(f"⏰ Timeout: {cmd}", "ERROR")
        raise
    except Exception as e:
        log(f"💥 {e}", "ERROR")
        raise

def check_disk_space(path, required_bytes):
    free = shutil.disk_usage(path).free
    if free < required_bytes:
        raise RuntimeError(f"Need {human_size(required_bytes)}, have {human_size(free)} free")

def human_size(size):
    for unit in ['B','KB','MB','GB']:
        if size < 1024: return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"

def url_encode(s):
    return quote(s, safe='')

def crc32_file(path):
    prev = 0
    with open(path, 'rb') as f:
        while chunk := f.read(65536):
            prev = zlib.crc32(chunk, prev)
    return format(prev & 0xFFFFFFFF, '08x')

# ------------------------------------------------------------------------------
# MIME‑based file extension fixing
# ------------------------------------------------------------------------------
def get_mime_type(filepath):
    try:
        return run(f'file -b --mime-type "{filepath}"', shell=True, quiet=True).strip()
    except Exception:
        return None

def fix_extension(filepath):
    fp = Path(filepath)
    if fp.suffix:
        return filepath
    mime = get_mime_type(filepath)
    if not mime:
        log(f"⚠️  Can't detect type for {fp.name}, leaving as‑is", "WARN")
        return filepath
    ext = mimetypes.guess_extension(mime, strict=False)
    if ext:
        new = fp.with_suffix(ext)
        fp.rename(new)
        log(f"🔧 Renamed {fp.name} → {new.name}")
        return str(new)
    log(f"⚠️  Unknown MIME '{mime}' for {fp.name}, leaving as‑is", "WARN")
    return filepath

# ------------------------------------------------------------------------------
# State & metadata
# ------------------------------------------------------------------------------
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    log("📄 No state.json – starting fresh")
    return {"repos": {}, "downloads": {}, "ranges": {}}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)
    run("git add -f state.json 2>/dev/null || true", check=False, quiet=True, shell=True)

def write_metadata(folder, url, method, **extra):
    path = os.path.join(folder, "metadata.json")
    with open(path, "w") as f:
        json.dump({"url": url, "method": method, **extra}, f, indent=2)

def write_readme(folder, title, url, method, extra=None, hashes=None, savings=None):
    lines = [f"# {title}", "", "| Property | Value |", "|--- |---|",
             f"| **URL** | {url} |"]
    if extra:
        for k, v in extra.items():
            lines.append(f"| **{k}** | {v} |")
    lines += ["", "<details><summary>Files</summary>", ""]
    for f in sorted(Path(folder).iterdir()):
        if f.name in ("README.md", "metadata.json"): continue
        rel = f"{folder}/{f.name}"
        sz = human_size(f.stat().st_size)
        name = unquote(f.name)
        crc_str = ""
        if hashes and f.name in hashes:
            crc_str = f" `(CRC32: {hashes[f.name]})`"
        save_str = ""
        if savings and f.name in savings:
            save_str = f" ({savings[f.name]})"
        lines.append(
            f"- [`{name}`](https://github.com/{GITHUB_REPOSITORY}/raw/main/{url_encode(rel)}) ({sz}){crc_str}{save_str}"
        )
    lines += ["", "</details>"]
    Path(os.path.join(folder, "README.md")).write_text("\n".join(lines))

def update_index_md(state):
    content = ["# Downloads", "", "---", ""]
    for section in ["downloads", "repos"]:
        for key, info in state.get(section, {}).items():
            folder = info.get("folder")
            if not folder: continue
            rm = Path(folder) / "README.md"
            if rm.exists():
                content.extend(rm.read_text().splitlines())
            else:
                content.append(f"## {Path(folder).name}")
            content += ["", "---", ""]
    Path("INDEX.md").write_text("\n".join(content))
    log("📄 INDEX.md regenerated")

# ------------------------------------------------------------------------------
# Filter parsing – dot means filename, no dot means extension, * = glob
# ------------------------------------------------------------------------------
def parse_filter(line):
    """Return (repo, filters, no_compress, pre_release, use_lfs)."""
    m = FILTER_PATTERN.match(line.strip())
    if not m:
        return line.strip(), None, False, False, False
    repo = m.group(1).strip()
    raw = [x.strip() for x in m.group(2).split(',') if x.strip()]
    no_compress = pre_release = use_lfs = False
    real = []
    for f in raw:
        fl = f.lower()
        if fl == 'nocompress': no_compress = True
        elif fl == 'pre': pre_release = True
        elif fl == 'lfs': use_lfs = True
        else: real.append(f)
    if not real:
        return repo, None, no_compress, pre_release, use_lfs
    if 'all' in [r.lower() for r in real]:
        return repo, ["all"], no_compress, pre_release, use_lfs

    processed = []
    for r in real:
        if '*' in r or '?' in r:
            processed.append(r)                    # glob
        elif '.' in r:
            processed.append(r)                    # exact filename
        else:
            processed.append(f'.{r.lstrip(".")}')  # extension
    return repo, processed, no_compress, pre_release, use_lfs

def asset_matches(name, filters):
    if not filters or filters == ["all"]: return True
    nl = name.lower()
    for f in filters:
        if f.startswith('.'):
            if nl.endswith(f.lower()): return True
        else:
            if fnmatch.fnmatch(nl, f.lower()): return True
    return False

def is_skip_asset(name):
    nl = name.lower()
    for ext in SKIP_ASSET_EXTS:
        if nl.endswith(ext): return True
    return False

def detect_no_compress(msg):
    return bool(NOCOMPRESS_COMMIT.search(msg))

# ------------------------------------------------------------------------------
# Zip helpers (replaces 7z)
# ------------------------------------------------------------------------------
def _zip_available():
    if shutil.which("zip") is None:
        raise RuntimeError("zip is not installed – install zip")

def _zip_cmd(filepath, out_zip, split=False):
    level = COMPRESSION_LEVEL
    cmd = ["zip", "-r"]
    if level == 0:
        cmd.append("-0")                     # store
    else:
        cmd.append(f"-{level}")              # deflate 1‑9
    if split:
        cmd.append(f"-s {SPLIT_MB}m")
    cmd.append(f'"{out_zip}"')
    cmd.append(f'"{filepath}"')
    return cmd

def _archive_single(filepath, out_zip):
    """Compress a single file into a .zip archive."""
    cmd = _zip_cmd(filepath, out_zip, split=False)
    run(" ".join(cmd), shell=True)

def _archive_dir(tmpdir, out_zip):
    """Compress a directory contents into a .zip archive."""
    cmd = _zip_cmd(tmpdir, out_zip, split=False)
    run(" ".join(cmd), shell=True)

def _store_archive(filepath, out_zip):
    """Create a .zip container with NO compression (store) for a non‑archive file."""
    global COMPRESSION_LEVEL
    saved_level = COMPRESSION_LEVEL
    COMPRESSION_LEVEL = 0
    try:
        _archive_single(filepath, out_zip, level=0)
    finally:
        COMPRESSION_LEVEL = saved_level

def _split_store(filepath):
    """Split a single file into zip volumes (store mode). Removes the original.
    If the file already ends with .zip, output base is adjusted.
    """
    base = os.path.splitext(filepath)[0]
    if filepath.lower().endswith('.zip'):
        base = filepath[:-len('.zip')] + '_split'
    out_zip = base + ".zip"
    cmd = ["zip", "-r", "-0", f"-s {SPLIT_MB}m", f'"{out_zip}"', f'"{filepath}"']
    log(f"✂️  Splitting {os.path.basename(filepath)} into {SPLIT_MB} MB volumes")
    run(" ".join(cmd), shell=True)
    if os.path.exists(filepath):
        os.remove(filepath)
    if os.path.exists(filepath):
        raise RuntimeError(f"Failed to delete original after split: {filepath}")

# ------------------------------------------------------------------------------
# Archive logic
# ------------------------------------------------------------------------------
def archive_file(filepath, folder, no_compress=False):
    fp = Path(filepath)
    ext = fp.suffix.lower()
    orig = fp.stat().st_size
    log(f"🗜️  {fp.name} ({human_size(orig)})" + (" (nocompress)" if no_compress else ""))
    check_disk_space(folder, orig * 2)

    # --------------- NOCOMPRESS: keep raw if small, split if large ---------------
    if no_compress:
        if orig <= SPLIT_MB * 1024 * 1024:
            log(f"📄 Keeping raw (≤ {SPLIT_MB} MB)")
            return filepath
        log("📦 Large file → store‑mode split")
        _split_store(filepath)
        return os.path.splitext(filepath)[0] + ".zip"

    # --------------- NORMAL PATH (compression) ---------------
    out_zip = os.path.join(folder, fp.stem + ".zip")
    tmp_extract = None
    compressed_ok = False

    try:
        if ext in EXTRACT_ARCHIVE_EXTS:
            log(f"📂 Extracting {fp.name}")
            tmp_extract = tempfile.mkdtemp(prefix="ext_", dir=folder)
            with zipfile.ZipFile(filepath, 'r') as zf:
                for m in zf.namelist():
                    mp = os.path.realpath(os.path.join(tmp_extract, m))
                    if not mp.startswith(os.path.realpath(tmp_extract)):
                        raise RuntimeError(f"Path traversal: {m}")
                zf.extractall(tmp_extract)
            _archive_dir(tmp_extract, out_zip)
        else:
            _archive_single(filepath, out_zip)
        new_size = os.path.getsize(out_zip)
        compressed_ok = True
    except Exception as e:
        log(f"Compression failed: {e}, fallback to store", "WARN")
    finally:
        if tmp_extract and os.path.exists(tmp_extract):
            shutil.rmtree(tmp_extract, ignore_errors=True)

    if compressed_ok and new_size < orig:
        log(f"✅ Compressed: {human_size(new_size)} (saved {human_size(orig - new_size)})")
        os.remove(filepath)
        final = out_zip
    else:
        if compressed_ok:
            log("⚠️  No space saved → using original")
            if os.path.exists(out_zip):
                os.remove(out_zip)
        else:
            # compression failed – fall back to store for non‑archives,
            # for archives just keep the original file unchanged
            if ext not in EXTRACT_ARCHIVE_EXTS:
                log("📦 Store inside .zip")
                _store_archive(filepath, out_zip)
                os.remove(filepath)
                final = out_zip
            else:
                log("📄 Keeping original archive unchanged")
        # if not already set (meaning we kept original), set final to original
        if not compressed_ok:
            final = filepath
        # else case where compressed but no space saved: we kept original file path
        else:
            final = filepath

        # Ensure we log store size if we created it
        if final == out_zip:
            log(f"📦 Store size: {human_size(os.path.getsize(final))}")

    # ---- GUARANTEE: no file > SPLIT_MB leaves this function ----
    limit = SPLIT_MB * 1024 * 1024
    while os.path.exists(final) and os.path.getsize(final) > limit:
        log(f"🔁 Splitting {os.path.basename(final)} ({human_size(os.path.getsize(final))})")
        _split_store(final)
        if os.path.exists(final):
            log("❌ Split did not remove original, retrying", "ERROR")
            _split_store(final)
            if os.path.exists(final):
                raise RuntimeError("Cannot split file after multiple attempts")

    if not os.path.exists(final):
        # split volumes were created; return base name for volume set
        return os.path.splitext(filepath)[0] + ".zip"
    return final

def ensure_all_files_small(folder):
    limit = SPLIT_MB * 1024 * 1024
    for f in Path(folder).iterdir():
        if f.is_file() and f.name not in ("README.md", "metadata.json"):
            if f.stat().st_size > limit:
                log(f"⚠️  Safety split: {f.name} ({human_size(f.stat().st_size)})")
                _split_store(str(f))

# ------------------------------------------------------------------------------
# LFS helpers
# ------------------------------------------------------------------------------
def ensure_git_lfs():
    if shutil.which("git-lfs") is None:
        raise RuntimeError("git-lfs not found – install Git LFS")
    if not (Path(".git/hooks/pre-push").exists() or Path(".git/hooks/pre-push.lfs_sample").exists()):
        run("git lfs install", shell=True)
    if not Path(".gitattributes").exists():
        Path(".gitattributes").touch()

def track_file_with_lfs(file_path):
    rel = os.path.relpath(file_path, os.getcwd())
    run(f'git lfs track "{rel}"', shell=True)

def push_lfs_files():
    if Path(".gitattributes").exists() and Path(".gitattributes").read_text().strip():
        log("📤 Pushing LFS objects...")
        run("git lfs push origin main", shell=True)

def process_lfs_assets(folder):
    for f in Path(folder).iterdir():
        if f.is_file() and f.name not in ("README.md", "metadata.json"):
            track_file_with_lfs(str(f))
    run("git add -f .gitattributes", shell=True, quiet=True)

# ------------------------------------------------------------------------------
# GitHub API
# ------------------------------------------------------------------------------
def github_api(url):
    log(f"🌐 GET {url}")
    headers = {"User-Agent": UA}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"token {GITHUB_TOKEN}"
    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.json()

# ------------------------------------------------------------------------------
# Helper: compute per‑file compression savings
# ------------------------------------------------------------------------------
def _compute_savings(folder, wanted):
    savings = {}
    for name, _, orig_size in wanted:
        stem = Path(name).stem
        # look for single .zip
        single = Path(folder) / (stem + ".zip")
        if single.exists():
            compressed = single.stat().st_size
        else:
            # look for split volumes
            pattern = f"{stem}_split.zip*"
            parts = list(Path(folder).glob(pattern))
            if not parts:
                # also try .z01 etc. which might appear with `-s`
                pattern = f"{stem}.z*"
                parts = list(Path(folder).glob(pattern))
            if parts:
                compressed = sum(p.stat().st_size for p in parts)
            else:
                raw = Path(folder) / name
                if raw.exists():
                    compressed = raw.stat().st_size
                else:
                    continue
        if orig_size > 0 and compressed > 0:
            pct = (compressed / orig_size - 1) * 100
            label = f"{pct:.1f}%"
            if single.exists():
                savings[single.name] = label
            elif parts:
                first = sorted(parts)[0]
                savings[first.name] = label
            else:
                savings[Path(name).name] = label
    return savings

# ------------------------------------------------------------------------------
# Download helpers
# ------------------------------------------------------------------------------
def download_asset(url, dest):
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    fname = unquote(os.path.basename(url.split("?")[0]))
    local = dest / fname
    if local.exists():
        log(f"✓ Already exists: {fname}")
        return str(local)
    log(f"⬇️  {fname}")
    cmd = (f'aria2c --summary-interval=2 --continue --max-connection-per-server=8 '
           f'--split=8 --min-split-size=1M --dir="{dest}" --out="{fname}" '
           f'--timeout=120 --max-tries=5 "{url}"')
    run(cmd, shell=True, timeout=600)
    if not local.exists() or local.stat().st_size == 0:
        raise RuntimeError(f"Download failed: {fname}")
    return fix_extension(str(local))

def download_file(url, dest_dir):
    return download_asset(url, dest_dir)

def download_and_chunk(url, dest_base, no_compress=False, use_lfs=False):
    state = load_state()
    if url in state.get("downloads", {}):
        folder = state["downloads"][url].get("folder")
        if folder and os.path.exists(folder):
            log(f"✅ Direct URL already mirrored, skipping.")
            return folder

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    base_name = unquote(os.path.basename(url.split("?")[0])) or "file"
    folder = Path(dest_base) / f"{base_name}_{ts}"
    folder.mkdir(parents=True)
    log(f"📁 {folder}")

    path = download_file(url, str(folder))
    if not path:
        return None
    orig_size = os.path.getsize(path)

    if use_lfs:
        log("🗃️  LFS mode – keeping raw")
    else:
        archive_file(path, str(folder), no_compress=no_compress)
        ensure_all_files_small(str(folder))

    final_files = [f for f in folder.iterdir() if f.is_file() and f.name not in ("README.md", "metadata.json")]
    crc_info = {f.name: crc32_file(str(f)) for f in final_files}

    # Per‑file compression savings
    total_compressed = sum(os.path.getsize(str(f)) for f in final_files)
    savings = {}
    if orig_size > 0 and total_compressed > 0:
        pct = (total_compressed / orig_size - 1) * 100
        label = f"{pct:.1f}%"
        if final_files:
            savings[final_files[0].name] = label

    write_metadata(str(folder), url, "direct", crc32=crc_info)
    write_readme(str(folder), base_name, url, "Direct Download", hashes=crc_info, savings=savings)
    if use_lfs:
        process_lfs_assets(str(folder))

    state["downloads"][url] = {"folder": str(folder)}
    save_state(state)
    return str(folder)

def github_release(url, dest_dir, filters=None, no_compress=False,
                   pre_release=False, use_lfs=False):
    m = GITHUB_RELEASE_PATTERN.match(url)
    if not m:
        raise ValueError("Invalid GitHub release URL")
    repo = m.group(1)
    tag = m.group(2) or "latest"

    if pre_release:
        releases = github_api(f"https://api.github.com/repos/{repo}/releases?per_page=1")
        if not releases:
            raise RuntimeError(f"No releases found for {repo}")
        release = releases[0]
        tag = release.get("tag_name")
    else:
        api_url = (f"https://api.github.com/repos/{repo}/releases/tags/{tag}"
                   if tag != "latest" else f"https://api.github.com/repos/{repo}/releases/latest")
        release = github_api(api_url)
        tag = release.get("tag_name")

    state = load_state()

    # Incremental skip
    if repo in state.get("repos", {}) and state["repos"][repo].get("tag") == tag:
        prev_folder = state["repos"][repo].get("folder")
        if prev_folder and os.path.exists(prev_folder):
            log(f"✅ Release {tag} already mirrored, skipping.")
            return prev_folder, repo, tag

    # Clean up old
    if repo in state.get("repos", {}):
        old_folder = state["repos"][repo].get("folder")
        if old_folder:
            log(f"🗑️  Removing old release: {old_folder}")
            run(f"git rm -r --ignore-unmatch {old_folder} 2>/dev/null || true",
                check=False, quiet=True, shell=True)
            if os.path.exists(old_folder):
                shutil.rmtree(old_folder, ignore_errors=True)
        del state["repos"][repo]

    release_name = release.get("name") or release.get("tag_name") or repo
    safe_name = SAFE_FILENAME_PATTERN.sub('_', release_name)
    safe_tag = SAFE_FILENAME_PATTERN.sub('_', tag)
    folder = Path(dest_dir) / f"{safe_name}_{safe_tag}"
    if folder.exists():
        shutil.rmtree(str(folder), ignore_errors=True)
    folder.mkdir(parents=True)
    log(f"📁 {folder}")

    all_assets = release.get("assets", [])
    wanted = []
    for a in all_assets:
        name = a.get("name")
        if not name:
            continue
        if filters and filters != ["all"] and not asset_matches(name, filters):
            continue
        if is_skip_asset(name):
            log(f"  ⏭️  Skipping {name}")
            continue
        wanted.append((name, a.get("browser_download_url"), a.get("size")))

    if not wanted:
        log("⚠️  No matching assets found.")
        state["repos"][repo] = {"folder": str(folder), "tag": tag}
        save_state(state)
        if folder.exists():
            shutil.rmtree(str(folder), ignore_errors=True)
        return str(folder), repo, tag

    log(f"⬇️  Downloading {len(wanted)} assets (parallel={MAX_PARALLEL})")
    with ThreadPoolExecutor(max_workers=MAX_PARALLEL) as executor:
        futures = {}
        for name, url, size in wanted:
            futures[executor.submit(download_asset, url, folder)] = name
        for future in tqdm(as_completed(futures), total=len(futures), desc="Assets"):
            name = futures[future]
            try:
                future.result()
                log(f"  ✓ {name}")
            except Exception as e:
                log(f"  ❌ {name}: {e}")

    if use_lfs:
        log("🗃️  LFS mode – keeping raw")
        process_lfs_assets(str(folder))
    else:
        for f in sorted(folder.iterdir()):
            if f.is_file() and f.name not in ("README.md", "metadata.json"):
                archive_file(str(f), str(folder), no_compress=no_compress)
        ensure_all_files_small(str(folder))

    final_files = [f for f in folder.iterdir() if f.is_file() and f.name not in ("README.md", "metadata.json")]
    crc_info = {f.name: crc32_file(str(f)) for f in final_files}
    total_size = sum(os.path.getsize(str(f)) for f in final_files)

    # Per‑file compression savings
    savings = _compute_savings(str(folder), wanted)

    rel_date = release.get("published_at") or release.get("created_at")
    if rel_date:
        try:
            dt = datetime.fromisoformat(rel_date.replace('Z', '+00:00'))
            ago = datetime.now(timezone.utc) - dt
            if ago.days > 0:
                ago_str = f"{ago.days} day{'s' if ago.days>1 else ''} ago"
            else:
                secs = ago.seconds
                ago_str = f"{secs//3600} hr ago" if secs >= 3600 else f"{secs//60} min ago"
            rel_str = f"{dt.strftime('%Y-%m-%d %H:%M UTC')} ({ago_str})"
        except Exception:
            rel_str = rel_date
    else:
        rel_str = "N/A"

    extra = {
        "Release Date": rel_str,
        "Total Size": human_size(total_size),
        "Release Name": release_name,
        "Tag": tag,
    }
    write_metadata(str(folder), url, "github_release", repo=repo, tag=tag,
                   assets=[{"name": n, "size": s} for n, _, s in wanted],
                   crc32=crc_info, total_size=total_size, release_date=rel_date, use_lfs=use_lfs)
    write_readme(str(folder), repo, f"https://github.com/{repo}/releases/tag/{tag}",
                 "GitHub Release", extra=extra, hashes=crc_info, savings=savings)

    state["repos"][repo] = {"folder": str(folder), "tag": tag}
    save_state(state)
    return str(folder), repo, tag

def range_download(url, start, end, base_dir):
    log(f"📡 Range: {url} [{start}-{end}]")
    state = load_state()
    folder = None
    if url in state["ranges"]:
        folder = Path(state["ranges"][url]["folder"])
        if folder.exists():
            log(f"♻️  Reusing range folder: {folder}")
        else:
            folder = None
    if not folder:
        bname = unquote(os.path.basename(url.split("?")[0])).rsplit('.', 1)[0]
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        folder = Path(base_dir) / f"{bname}_{ts}"
        folder.mkdir(parents=True, exist_ok=True)
        log(f"📁 New range folder: {folder}")

    tmp_file = folder / "downloaded_range.tmp"
    range_sz = end - start + 1
    check_disk_space(str(folder), range_sz * 2)
    run(f'curl -sSfL --retry 3 --retry-delay 5 --connect-timeout 30 '
        f'-r {start}-{end} -o "{tmp_file}" "{url}"', shell=True, timeout=300)
    if not tmp_file.exists() or tmp_file.stat().st_size == 0:
        raise RuntimeError("Range download empty")

    archive_file(str(tmp_file), str(folder))
    ensure_all_files_small(str(folder))

    final_files = [f for f in folder.iterdir() if f.is_file() and f.name not in ("README.md", "metadata.json")]
    crc_info = {f.name: crc32_file(str(f)) for f in final_files}

    write_metadata(str(folder), url, "direct_chunked", crc32=crc_info)
    title = folder.name.rsplit('_', 1)[0]
    readme = (
        f"# {title}\n\n"
        f"| Property | Value |\n|--- |---|\n"
        f"| **URL** | {url} |\n"
        f"| **Range** | {start}-{end} bytes |\n"
        f"| **Compression** | Deflate (level {COMPRESSION_LEVEL}) |\n\n"
        "<details><summary>Files</summary>\n\n"
    )
    for f in sorted(folder.iterdir()):
        if f.name in ("README.md", "metadata.json"): continue
        rel = f"{folder}/{f.name}"
        sz = human_size(f.stat().st_size)
        name = unquote(f.name)
        h = f" `(CRC32: {crc_info[f.name]})`" if crc_info and f.name in crc_info else ""
        readme += f"- [`{name}`](https://github.com/{GITHUB_REPOSITORY}/raw/main/{url_encode(rel)}) ({sz}){h}\n"
    readme += "\n</details>\n"
    (folder / "README.md").write_text(readme)

    state["ranges"][url] = {"folder": str(folder), "last_range_end": end}
    save_state(state)
    return str(folder)

# ------------------------------------------------------------------------------
# Batch push
# ------------------------------------------------------------------------------
def batch_commit_and_push(new_folders):
    all_files = []
    for folder in new_folders:
        if not os.path.isdir(folder):
            continue
        for f in Path(folder).rglob('*'):
            if f.is_file():
                all_files.append(str(f))
    if not all_files:
        log("ℹ️  No new files")
        return

    all_files.sort(key=lambda x: os.path.getsize(x), reverse=True)
    batch, batch_size, batch_num = [], 0, 1
    total_size = sum(os.path.getsize(f) for f in all_files)
    total_batches = max((total_size + PUSH_BATCH_BYTES - 1) // PUSH_BATCH_BYTES, 1)

    for f in all_files:
        fsize = os.path.getsize(f)
        if batch_size + fsize > PUSH_BATCH_BYTES and batch:
            commit_and_push_batch(batch, batch_num, total_batches)
            push_lfs_files()
            batch, batch_size = [], 0
            batch_num += 1
        batch.append(f)
        batch_size += fsize
    if batch:
        commit_and_push_batch(batch, batch_num, total_batches)
        push_lfs_files()

def commit_and_push_batch(batch_files, batch_num, total_batches):
    msg = f"Sync downloads batch {batch_num}/{total_batches} [skip ci]"
    batch_size = sum(os.path.getsize(f) for f in batch_files)
    log(f"📦 Batch {batch_num}/{total_batches}: {len(batch_files)} files ({human_size(batch_size)})")
    for f in batch_files:
        run(f'git add -f "{f}"', shell=True, quiet=True)
    run(f'git commit -m "{msg}"', shell=True)
    run("git push", shell=True)
    log(f"✅ Pushed batch {batch_num}")

# ------------------------------------------------------------------------------
# State maintenance
# ------------------------------------------------------------------------------
def clean_state(state):
    changed = False
    for section in ["downloads", "repos"]:
        for k, v in list(state.get(section, {}).items()):
            if v.get("folder") and not os.path.exists(v["folder"]):
                del state[section][k]
                changed = True
    for url, info in list(state.get("ranges", {}).items()):
        if info.get("folder") and not os.path.exists(info["folder"]):
            del state["ranges"][url]
            changed = True
    return changed

def prune_old(state, days=90):
    cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
    changed = False
    for section in ["downloads", "repos"]:
        for k, v in list(state.get(section, {}).items()):
            folder = v.get("folder")
            if folder and os.path.exists(folder) and os.path.getmtime(folder) < cutoff:
                del state[section][k]
                changed = True
    return changed

def cleanup_removed_repos(state, current):
    removed = [r for r in state.get("repos", {}) if r not in current]
    for r in removed:
        old = state["repos"][r].get("folder")
        if old:
            log(f"🗑️  Removing old release: {old}")
            run(f"git rm -r --ignore-unmatch {old} 2>/dev/null || true", check=False, quiet=True, shell=True)
            if os.path.exists(old): shutil.rmtree(old, ignore_errors=True)
        del state["repos"][r]
    return len(removed) > 0

# ------------------------------------------------------------------------------
# Main flows
# ------------------------------------------------------------------------------
def _normalize_line(line):
    line = line.strip()
    m = re.match(r'^https?://github\.com/([^/]+/[^/]+)/releases/.*\s*\[(.*)\]$', line)
    if m:
        return parse_filter(f"{m.group(1)} [{m.group(2)}]")
    m = re.match(r'^https?://github\.com/([^/]+/[^/]+)/releases/.*$', line)
    if m:
        return m.group(1), None, False, False, False
    return parse_filter(line)

def process_updates(no_push=False):
    _zip_available()
    log("🔄 Repo.txt update", "INFO")
    log(f"Compression: Deflate level {COMPRESSION_LEVEL}")

    state = load_state()
    if clean_state(state): save_state(state)
    if prune_old(state): save_state(state)

    if not Path("repo.txt").exists():
        log("⚠️  repo.txt not found – skipping")
        return

    lines = [l.strip() for l in open("repo.txt") if l.strip() and not l.startswith('#')]
    current_repos = []
    new_folders = []

    for line in lines:
        repo, filters, no_compress, pre_release, use_lfs = _normalize_line(line)
        if not repo:
            continue
        if repo.startswith("http://") or repo.startswith("https://"):
            log(f"\n🔗 Direct URL: {repo}")
            try:
                folder = download_and_chunk(repo, "downloads", no_compress=no_compress, use_lfs=use_lfs)
                if folder:
                    new_folders.append(folder)
            except Exception as e:
                log(f"❌ {e}", "ERROR")
        else:
            current_repos.append(repo)

    if cleanup_removed_repos(state, current_repos):
        log("🧹 Cleaned removed repos")

    for line in lines:
        repo, filters, no_compress, pre_release, use_lfs = _normalize_line(line)
        if not repo or repo.startswith("http"):
            continue
        flags = []
        if no_compress: flags.append("nocompress")
        if pre_release: flags.append("pre")
        if use_lfs: flags.append("lfs")
        flag_str = f" ({', '.join(flags)})" if flags else ""
        log(f"\n📋 {repo} (filter: {filters or 'all'}{flag_str})")
        try:
            folder, _, tag = github_release(
                f"https://github.com/{repo}/releases/latest",
                "repos", filters, no_compress, pre_release, use_lfs
            )
            new_folders.append(folder)
        except Exception as e:
            log(f"❌ {e}", "ERROR")

    state = load_state()
    update_index_md(state)
    new_folders.extend(["state.json", "INDEX.md"])
    if not no_push:
        batch_commit_and_push(new_folders)
    log("✅ Update finished")

def process_commit(custom_msg=None, no_push=False):
    _zip_available()
    msg = custom_msg or run("git log -1 --pretty=%B", shell=True)
    log(f"📩 Commit: {msg}")

    state = load_state()
    if clean_state(state): save_state(state)
    if prune_old(state): save_state(state)

    no_compress = detect_no_compress(msg)
    if no_compress:
        log("🏷️  [nocompress] detected – all files raw")

    new_folders = []
    urls = URL_PATTERN.findall(msg)

    if len(urls) > 1:
        log(f"📦 {len(urls)} URLs")
        for url in urls:
            log(f"\n🌐 {url}")
            try:
                if GITHUB_RELEASE_PATTERN.match(url):
                    folder, repo, tag = github_release(url, "repos", ["all"], no_compress, False, False)
                    state["repos"][repo] = {"folder": folder, "tag": tag}
                else:
                    folder = download_and_chunk(url, "downloads", no_compress, False)
                    if folder:
                        state["downloads"][url] = {"folder": folder}
                new_folders.append(folder)
            except Exception as e:
                log(f"❌ {e}", "ERROR")
        save_state(state)
        update_index_md(state)
        new_folders.extend(["state.json", "INDEX.md"])
        if not no_push:
            batch_commit_and_push(new_folders)
        log(f"🎉 {len(urls)} URLs done")
        return

    rm = RANGE_PATTERN.search(msg)
    if rm:
        url = rm.group(1)
        start_mb, end_mb = int(rm.group(2)), int(rm.group(3))
        start, end = start_mb * 1024 * 1024, end_mb * 1024 * 1024 - 1
        folder = range_download(url, start, end, "downloads")
        update_index_md(state)
        new_folders.append(folder)
        new_folders.extend(["state.json", "INDEX.md"])
        if not no_push:
            batch_commit_and_push(new_folders)
        return

    um = URL_PATTERN.search(msg)
    if not um:
        log("ℹ️  No URL found")
        return
    url = um.group(1)
    log(f"🌐 {url}")
    try:
        if GITHUB_RELEASE_PATTERN.match(url):
            folder, repo, tag = github_release(url, "repos", ["all"], no_compress, False, False)
            state["repos"][repo] = {"folder": folder, "tag": tag}
        else:
            folder = download_and_chunk(url, "downloads", no_compress, False)
            if folder:
                state["downloads"][url] = {"folder": folder}
        new_folders.append(folder)
        if folder:
            save_state(state)
            update_index_md(state)
            new_folders.extend(["state.json", "INDEX.md"])
            if not no_push:
                batch_commit_and_push(new_folders)
        log(f"🎉 Done → {folder}")
    except Exception as e:
        log(f"❌ {e}", "ERROR")
        sys.exit(1)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["update", "commit"])
    parser.add_argument("--no-push", action="store_true")
    parser.add_argument("--msg", help="Commit message (commit mode)")
    args = parser.parse_args()

    try:
        if args.mode == "update":
            process_updates(no_push=args.no_push)
        else:
            process_commit(custom_msg=args.msg, no_push=args.no_push)
    except Exception as e:
        log(f"🔥 Fatal: {e}", "ERROR")
        raise
