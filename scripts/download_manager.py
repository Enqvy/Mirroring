#!/usr/bin/env python3
"""
Mirror manager – fast compression, incremental updates, parallel downloads, CRC32 integrity.
All behaviour is configured through config.toml (next to this script).
Reads repo.txt for sources. Generates per‑release README.md and a global INDEX.md.
"""

import os
import sys
import json
import re
import time
import shutil
import subprocess
import argparse
import tempfile
import zipfile
import shlex
import fnmatch
import zlib
import mimetypes
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, quote
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from tqdm import tqdm

# ------------------------------------------------------------------------------
# TOML support (fallback to defaults if library missing)
# ------------------------------------------------------------------------------
try:
    import tomllib  # Python 3.11+
except ImportError:
    try:
        import tomli as tomllib
    except ImportError:
        tomllib = None

# ------------------------------------------------------------------------------
# Default configuration (mirrors config.toml)
# ------------------------------------------------------------------------------
DEFAULTS = {
    "split_mb": 99,
    "push_batch_bytes": 350 * 1024 * 1024,  # 350 MiB
    "max_parallel": 4,
    "compression_level": 5,
    "compression_method": "Deflate64",
    "extract_archive_exts": [".zip", ".jar", ".war", ".ear"],
    "skip_asset_exts": [
        ".sha256", ".sha256sum", ".sha512", ".sha512sum",
        ".sha1", ".sha1sum", ".md5", ".md5sum",
        ".asc", ".sig", ".sign", ".pgp",
        ".blake2b", ".blake2s", ".sha3",
        ".sha256.txt", ".sha512.txt", ".sha1.txt", ".md5.txt",
        ".sha256sums", ".sha512sums", ".sha1sums", ".md5sums",
    ],
}

# ------------------------------------------------------------------------------
# Load config.toml
# ------------------------------------------------------------------------------
def load_config():
    config = DEFAULTS.copy()
    script_dir = Path(__file__).resolve().parent
    config_file = script_dir / "config.toml"

    if config_file.is_file():
        if tomllib is None:
            print("⚠️  TOML library missing – using hard‑coded defaults.")
        else:
            try:
                with open(config_file, "rb") as f:
                    user = tomllib.load(f)
                for key in DEFAULTS:
                    if key in user:
                        config[key] = user[key]
            except Exception as e:
                print(f"⚠️  Failed to parse config.toml: {e}")
    return config

CFG = load_config()

# ------------------------------------------------------------------------------
# Application constants (from config)
# ------------------------------------------------------------------------------
SPLIT_MB            = CFG["split_mb"]
PUSH_BATCH_BYTES    = CFG["push_batch_bytes"]
MAX_PARALLEL        = CFG["max_parallel"]
COMPRESSION_LEVEL   = CFG["compression_level"]
COMPRESSION_METHOD  = CFG["compression_method"]
EXTRACT_ARCHIVE_EXTS = set(CFG["extract_archive_exts"])
SKIP_ASSET_EXTS     = set(CFG["skip_asset_exts"])

# ------------------------------------------------------------------------------
# Other constants
# ------------------------------------------------------------------------------
STATE_FILE = "state.json"
GITHUB_REPOSITORY = os.getenv("GITHUB_REPOSITORY", "unknown/unknown")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
VERBOSE = os.getenv("VERBOSE", "0") == "1"

# ------------------------------------------------------------------------------
# Regex patterns
# ------------------------------------------------------------------------------
URL_PATTERN = re.compile(r'(https?://[^\s]+)')
GITHUB_RELEASE_PATTERN = re.compile(
    r'https?://github\.com/([^/]+/[^/]+)/releases/(?:latest|tag/(.+))'
)
RANGE_PATTERN = re.compile(
    r'((?:https?://\S+))\s*\[(\d+)mb?[ ,\-]*(\d+)mb?\]',
    re.I
)
FILTER_PATTERN = re.compile(r'^(.*?)\s*\[(.*?)\]$')
SAFE_FILENAME_PATTERN = re.compile(r'[\\/:*?"<>|]')
NOCOMPRESS_COMMIT = re.compile(r'\[nocompress\]', re.I)

# ------------------------------------------------------------------------------
# Logging & helpers
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
            log(f"❌ Command failed: {cmd}", "ERROR")
            if err:
                log(f"   stderr: {err}", "ERROR")
            raise RuntimeError(f"Command failed (exit {proc.returncode}): {cmd}")
        return proc.stdout.strip()
    except subprocess.TimeoutExpired:
        log(f"⏰ Timeout after {timeout}s: {cmd}", "ERROR")
        raise RuntimeError(f"Command timed out: {cmd}")
    except Exception as e:
        log(f"💥 Unexpected error running command: {e}", "ERROR")
        raise

def check_disk_space(path, required_bytes):
    free = shutil.disk_usage(path).free
    if free < required_bytes:
        raise RuntimeError(
            f"Insufficient disk space: {human_size(free)} free, needed {human_size(required_bytes)}"
        )

def human_size(size):
    for unit in ['B','KB','MB','GB']:
        if size < 1024: return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"

def url_encode(s):
    return quote(s, safe='')

def crc32_file(path):
    """Fast CRC32 checksum of a file."""
    prev = 0
    with open(path, 'rb') as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            prev = zlib.crc32(chunk, prev)
    return format(prev & 0xFFFFFFFF, '08x')

# ------------------------------------------------------------------------------
# File extension fixing (MIME detection)
# ------------------------------------------------------------------------------
def get_mime_type(filepath):
    try:
        out = run(f'file -b --mime-type "{filepath}"', shell=True, quiet=True)
        return out.strip()
    except Exception:
        return None

def fix_extension(filepath):
    fpath = Path(filepath)
    if fpath.suffix:
        return filepath

    mime = get_mime_type(filepath)
    if not mime:
        log(f"⚠️  Cannot detect type for {fpath.name}, keeping as-is", "WARN")
        return filepath

    ext = mimetypes.guess_extension(mime, strict=False)
    if ext:
        new_path = fpath.with_suffix(ext)
        fpath.rename(new_path)
        log(f"🔧 Renamed {fpath.name} → {new_path.name} (mime: {mime})")
        return str(new_path)

    log(f"⚠️  Unknown MIME type '{mime}' for {fpath.name}, keeping as-is", "WARN")
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
    log(f"📝 metadata → {path}")

def write_readme(folder, title, url, method, extra=None, hashes=None):
    """Write per‑folder README with per‑file CRC32 hashes next to sizes."""
    lines = [
        f"# {title}", "",
        "| Property | Value |",
        "|--- |---|",
        f"| **URL** | {url} |",
    ]
    if extra:
        for k, v in extra.items():
            lines.append(f"| **{k}** | {v} |")
    lines += ["", "<details><summary>Files</summary>", ""]
    for f in sorted(Path(folder).iterdir()):
        if f.name in ("README.md", "metadata.json"): continue
        rel = f"{folder}/{f.name}"
        size = human_size(f.stat().st_size)
        display_name = unquote(f.name)
        crc_str = ""
        if hashes and f.name in hashes:
            crc_str = f" `(CRC32: {hashes[f.name]})`"
        lines.append(
            f"- [`{display_name}`](https://github.com/{GITHUB_REPOSITORY}/raw/main/{url_encode(rel)}) ({size}){crc_str}"
        )
    lines += ["", "</details>"]
    readme_path = os.path.join(folder, "README.md")
    with open(readme_path, "w") as f:
        f.write("\n".join(lines))
    log(f"📝 README → {readme_path}")

def update_root_readme(state):
    """Regenerate the global index of all downloads (INDEX.md)."""
    content = ["# Downloads", "", "---", ""]
    for section_key in ["downloads", "repos"]:
        entries = state.get(section_key, {})
        for key, info in entries.items():
            folder = info.get("folder")
            if not folder: continue
            readme = Path(folder) / "README.md"
            if readme.exists():
                content.extend(readme.read_text().splitlines())
            else:
                content.append(f"## {Path(folder).name}")
            content += ["", "---", ""]
    Path("INDEX.md").write_text("\n".join(content))
    log("📄 INDEX.md regenerated")

# ------------------------------------------------------------------------------
# Parsing filters – supports "nocompress" flag
# ------------------------------------------------------------------------------
def parse_filter(line):
    """Return (repo, filters, no_compress)."""
    m = FILTER_PATTERN.match(line.strip())
    if not m:
        return line.strip(), None, False

    repo = m.group(1).strip()
    raw_filters = [f.strip() for f in m.group(2).split(',') if f.strip()]

    no_compress = False
    real_filters = []
    for f in raw_filters:
        if f.lower() == 'nocompress':
            no_compress = True
        else:
            real_filters.append(f)

    if not real_filters:
        return repo, None, no_compress

    if 'all' in [f.lower() for f in real_filters]:
        return repo, ["all"], no_compress

    processed = []
    for f in real_filters:
        if '*' in f or '?' in f:
            processed.append(f)
        else:
            processed.append(f'.{f.lstrip(".")}')
    return repo, processed, no_compress

def asset_matches(asset_name, filters):
    if not filters or filters == ["all"]:
        return True
    name_lower = asset_name.lower()
    for f in filters:
        if f.startswith('.'):
            if name_lower.endswith(f.lower()):
                return True
        else:
            if fnmatch.fnmatch(name_lower, f.lower()):
                return True
    return False

def is_skip_asset(name):
    name_lower = name.lower()
    for ext in SKIP_ASSET_EXTS:
        if name_lower.endswith(ext):
            return True
    return False

def detect_no_compress(msg):
    return bool(NOCOMPRESS_COMMIT.search(msg))

# ------------------------------------------------------------------------------
# 7z helpers – using configurable method + level
# ------------------------------------------------------------------------------
def _7z_available():
    try:
        subprocess.run(["7z"], capture_output=True, timeout=5)
        return True
    except Exception:
        raise RuntimeError("7z is not installed – please install p7zip-full")

def _7z_cmd_base(compression_level, split=False):
    cmd = ['7z', 'a', '-t7z']
    if split:
        cmd.append(f'-v{SPLIT_MB}m')
    if compression_level == 0:
        cmd.extend(['-mx=0', '-m0=Copy'])
    else:
        cmd.extend([f'-m0={COMPRESSION_METHOD}', f'-mx={compression_level}', '-mmt=on'])
    return cmd

def _archive_single(filepath, out_7z, level=COMPRESSION_LEVEL):
    cmd = _7z_cmd_base(level) + [f'"{out_7z}"', f'"{filepath}"']
    run(' '.join(cmd), shell=True)

def _archive_dir(tmpdir, out_7z, level=COMPRESSION_LEVEL):
    cmd = _7z_cmd_base(level) + [f'"{out_7z}"', f'"{tmpdir}/*"']
    run(' '.join(cmd), shell=True)

def _create_store_archive(filepath, out_7z):
    """Create a .7z container with NO compression (store)."""
    _archive_single(filepath, out_7z, level=0)

def _split_store(filepath):
    """Split a single file into 7z store‑mode volumes (no compression)."""
    base = os.path.splitext(filepath)[0]
    if filepath.lower().endswith('.7z'):
        base = filepath[:-len('.7z')] + '_split'
    out = base + ".7z"
    cmd = [
        "7z", "a",
        "-t7z",
        f"-v{SPLIT_MB}m",
        "-mx=0",
        f'"{out}"',
        f'"{filepath}"'
    ]
    log(f"✂️  Splitting {os.path.basename(filepath)} into {SPLIT_MB} MB volumes")
    run(" ".join(cmd), shell=True)
    if os.path.exists(filepath):
        os.remove(filepath)
    if os.path.exists(filepath):
        raise RuntimeError(f"Failed to delete original after split: {filepath}")

# ------------------------------------------------------------------------------
# Smart archiving – APK stays whole, other archives extracted, nocompress support
# ------------------------------------------------------------------------------
def archive_file(filepath, folder, no_compress=False):
    fpath = Path(filepath)
    ext = fpath.suffix.lower()
    orig_size = fpath.stat().st_size
    log(f"🗜️  Processing {fpath.name} ({human_size(orig_size)}){' (nocompress)' if no_compress else ''}")
    check_disk_space(folder, orig_size * 2)

    # --------------- NOCOMPRESS: keep raw if small, split if large ---------------
    if no_compress:
        limit = SPLIT_MB * 1024 * 1024
        if orig_size <= limit:
            log(f"📄 Keeping raw file (under {SPLIT_MB} MB, no compression)")
            return filepath
        else:
            log(f"📦 Large file, splitting into store‑mode volumes")
            _split_store(filepath)
            return os.path.splitext(filepath)[0] + ".7z"

    # --------------- NORMAL PATH (compression) ---------------
    out_7z = os.path.join(folder, fpath.stem + ".7z")
    tmp_extract = None
    compressed_ok = False

    try:
        if ext in EXTRACT_ARCHIVE_EXTS:
            log(f"📂 Extracting {fpath.name} for better compression")
            tmp_extract = tempfile.mkdtemp(prefix="ext_", dir=folder)
            with zipfile.ZipFile(filepath, 'r') as zf:
                for member in zf.namelist():
                    member_path = os.path.realpath(os.path.join(tmp_extract, member))
                    if not member_path.startswith(os.path.realpath(tmp_extract)):
                        raise RuntimeError(f"Path traversal detected: {member}")
                zf.extractall(tmp_extract)
            _archive_dir(tmp_extract, out_7z, level=COMPRESSION_LEVEL)
        else:
            _archive_single(filepath, out_7z, level=COMPRESSION_LEVEL)
        new_size = os.path.getsize(out_7z)
        compressed_ok = True
    except Exception as e:
        log(f"Compression failed: {e}, falling back to store", "WARN")
    finally:
        if tmp_extract and os.path.exists(tmp_extract):
            shutil.rmtree(tmp_extract, ignore_errors=True)

    if compressed_ok and new_size < orig_size:
        log(f"✅ Compressed: {human_size(new_size)} (saved {human_size(orig_size - new_size)})")
        os.remove(filepath)
        final_archive = out_7z
    else:
        if compressed_ok:
            log(f"⚠️  No space saved, using store")
            os.remove(out_7z)
        else:
            log("📦 Store original inside .7z")
        _create_store_archive(filepath, out_7z)
        os.remove(filepath)
        final_archive = out_7z
        log(f"📦 Store archive: {human_size(os.path.getsize(final_archive))}")

    # ---- GUARANTEE: no file > SPLIT_MB leaves this function ----
    limit = SPLIT_MB * 1024 * 1024
    while os.path.exists(final_archive) and os.path.getsize(final_archive) > limit:
        log(f"  🔁 Splitting oversized: {os.path.basename(final_archive)} ({human_size(os.path.getsize(final_archive))})")
        _split_store(final_archive)
        if os.path.exists(final_archive):
            log(f"  ❌ Split failed to remove original, retrying", "ERROR")
            _split_store(final_archive)
            if os.path.exists(final_archive):
                raise RuntimeError("Cannot split file despite repeated attempts")

    if not os.path.exists(final_archive):
        return os.path.splitext(final_archive)[0] + ".7z"
    return final_archive

def ensure_all_files_small(folder):
    limit = SPLIT_MB * 1024 * 1024
    for f in Path(folder).iterdir():
        if f.is_file() and f.name not in ("README.md", "metadata.json"):
            if f.stat().st_size > limit:
                log(f"⚠️  Safety split: {f.name} ({human_size(f.stat().st_size)})")
                _split_store(str(f))

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
# Download helpers – incremental + parallel
# ------------------------------------------------------------------------------
def download_asset(url, dest):
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    fname = unquote(os.path.basename(url.split("?")[0]))
    local_path = dest / fname
    if local_path.exists():
        log(f"✓ Already exists: {fname}")
        return str(local_path)

    log(f"⬇️  {fname}")
    cmd = (f'aria2c --summary-interval=2 --continue --max-connection-per-server=8 '
           f'--split=8 --min-split-size=1M --dir="{dest}" --out="{fname}" '
           f'--timeout=120 --max-tries=5 "{url}"')
    run(cmd, shell=True, timeout=600)
    if not local_path.exists() or local_path.stat().st_size == 0:
        raise RuntimeError(f"Download failed: {fname}")

    return fix_extension(str(local_path))

def download_file(url, dest_dir):
    return download_asset(url, dest_dir)

def download_and_chunk(url, dest_base, no_compress=False):
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    basename = unquote(os.path.basename(url.split("?")[0])) or "file"
    folder_name = f"{basename}_{ts}"
    folder = Path(dest_base) / folder_name
    folder.mkdir(parents=True)
    log(f"📁 {folder}")

    path = download_file(url, str(folder))
    if not path:
        return None

    archive_file(path, str(folder), no_compress=no_compress)
    ensure_all_files_small(str(folder))
    final_files = [f for f in folder.iterdir() if f.is_file() and f.name not in ("README.md", "metadata.json")]
    crc_info = {f.name: crc32_file(str(f)) for f in final_files}
    write_metadata(str(folder), url, "direct", crc32=crc_info)
    write_readme(str(folder), basename, url, "Direct Download", hashes=crc_info)
    return str(folder)

def github_release(url, dest_dir, filters=None, no_compress=False):
    m = GITHUB_RELEASE_PATTERN.match(url)
    if not m:
        raise ValueError("Invalid GitHub release URL")
    repo = m.group(1)
    tag = m.group(2) or "latest"
    api_url = (
        f"https://api.github.com/repos/{repo}/releases/tags/{tag}"
        if tag != "latest"
        else f"https://api.github.com/repos/{repo}/releases/latest"
    )
    release = github_api(api_url)
    release_name = release.get("name") or release.get("tag_name") or repo
    tag = release.get("tag_name")
    safe_name = SAFE_FILENAME_PATTERN.sub('_', release_name)
    safe_tag = SAFE_FILENAME_PATTERN.sub('_', tag)
    folder_name = f"{safe_name}_{safe_tag}"
    folder = Path(dest_dir) / folder_name

    if folder.exists():
        log(f"🗑️  Removing leftover folder: {folder}")
        shutil.rmtree(str(folder), ignore_errors=True)
    folder.mkdir(parents=True)
    log(f"📁 {folder}")

    all_assets = release.get("assets", [])
    wanted_assets = []
    for a in all_assets:
        name = a.get("name")
        if not name: continue
        if filters and filters != ["all"] and not asset_matches(name, filters):
            continue
        if is_skip_asset(name):
            log(f"  ⏭️  Skipping checksum/signature: {name}")
            continue
        wanted_assets.append((name, a.get("browser_download_url"), a.get("size")))

    if not wanted_assets:
        log("⚠️  No matching assets found.")
        return str(folder), repo, tag

    # Incremental check
    prev_info = None
    state = load_state()
    if repo in state.get("repos", {}) and state["repos"][repo].get("tag") == tag:
        prev_folder = state["repos"][repo].get("folder")
        if prev_folder and os.path.exists(prev_folder):
            prev_meta = os.path.join(prev_folder, "metadata.json")
            if os.path.exists(prev_meta):
                with open(prev_meta) as f:
                    prev_info = json.load(f).get("assets", [])
    current_set = {(n, sz) for n, u, sz in wanted_assets}
    prev_set = set()
    if prev_info:
        for p in prev_info:
            prev_set.add((p["name"], p["size"]))
    unchanged = current_set & prev_set
    if unchanged == current_set and prev_set:
        log(f"✅ No changes in release {tag}, skipping.")
        return prev_folder, repo, tag

    # Download assets in parallel
    log(f"⬇️  Downloading {len(wanted_assets)} assets (parallel={MAX_PARALLEL})")
    with ThreadPoolExecutor(max_workers=MAX_PARALLEL) as executor:
        futures = {}
        for name, url, size in wanted_assets:
            futures[executor.submit(download_asset, url, folder)] = name
        for future in tqdm(as_completed(futures), total=len(futures), desc="Assets"):
            name = futures[future]
            try:
                path = future.result()
                log(f"  ✓ {name}")
            except Exception as e:
                log(f"  ❌ {name}: {e}")

    # Process each remaining file
    for f in sorted(folder.iterdir()):
        if f.is_file() and f.name not in ("README.md", "metadata.json"):
            archive_file(str(f), str(folder), no_compress=no_compress)
    ensure_all_files_small(str(folder))

    final_files = [f for f in folder.iterdir() if f.is_file() and f.name not in ("README.md", "metadata.json")]
    crc_info = {f.name: crc32_file(str(f)) for f in final_files}

    # ---- Enriched metadata ----
    total_size = sum(os.path.getsize(str(f)) for f in final_files)
    main_hash = crc_info[final_files[0].name] if final_files else ""

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
            release_date_str = f"{dt.strftime('%Y-%m-%d %H:%M UTC')} ({ago_str})"
        except Exception:
            release_date_str = rel_date
    else:
        release_date_str = "N/A"

    extra = {
        "Release Date": release_date_str,
        "Total Size": human_size(total_size),
        "Hash (CRC32)": main_hash,
    }
    if release_name:
        extra["Release Name"] = release_name
    extra["Tag"] = tag

    write_metadata(str(folder), url, "github_release", repo=repo, tag=tag,
                   assets=[{"name": n, "size": sz} for n, u, sz in wanted_assets],
                   crc32=crc_info,
                   total_size=total_size,
                   release_date=rel_date)
    title = repo
    write_readme(str(folder), title, f"https://github.com/{repo}/releases/tag/{tag}",
                 "GitHub Release", extra=extra, hashes=crc_info)
    return str(folder), repo, tag

def range_download(url, start, end, base_dir):
    log(f"📡 Range download: {url} bytes {start}-{end}")
    state = load_state()
    folder = None
    if url in state["ranges"]:
        folder = Path(state["ranges"][url]["folder"])
        if folder.exists():
            log(f"♻️  Reusing existing range folder: {folder}")
        else:
            folder = None
    if not folder:
        bname = unquote(os.path.basename(url.split("?")[0])).rsplit('.', 1)[0]
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        folder = Path(base_dir) / f"{bname}_{ts}"
        folder.mkdir(parents=True, exist_ok=True)
        log(f"📁 New range folder: {folder}")

    tmp_file = folder / "downloaded_range.tmp"
    range_size = end - start + 1
    log(f"📥 Downloading {human_size(range_size)} range [{start}-{end}]")
    check_disk_space(str(folder), range_size * 2)

    run(
        f'curl -sSfL --retry 3 --retry-delay 5 --connect-timeout 30 '
        f'-r {start}-{end} -o "{tmp_file}" "{url}"',
        shell=True, timeout=300
    )
    if not tmp_file.exists() or tmp_file.stat().st_size == 0:
        raise RuntimeError("Range download returned empty file")

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
        f"| **Compression** | {COMPRESSION_METHOD} (level {COMPRESSION_LEVEL}) |\n\n"
        "<details><summary>Files</summary>\n\n"
    )
    for f in sorted(folder.iterdir()):
        if f.name in ("README.md", "metadata.json"): continue
        rel = f"{folder}/{f.name}"
        size = human_size(f.stat().st_size)
        display_name = unquote(f.name)
        crc_str = ""
        if crc_info and f.name in crc_info:
            crc_str = f" `(CRC32: {crc_info[f.name]})`"
        readme += f"- [`{display_name}`](https://github.com/{GITHUB_REPOSITORY}/raw/main/{url_encode(rel)}) ({size}){crc_str}\n"
    readme += "\n</details>\n"
    (folder / "README.md").write_text(readme)
    state["ranges"][url] = {"folder": str(folder), "last_range_end": end}
    save_state(state)
    log(f"✅ Range chunk saved → {folder}")
    return str(folder)

# ------------------------------------------------------------------------------
# Batch commit / push helpers
# ------------------------------------------------------------------------------
def batch_commit_and_push(new_folders):
    all_files = []
    for folder in new_folders:
        if not os.path.isdir(folder): continue
        for f in Path(folder).rglob('*'):
            if f.is_file():
                all_files.append(str(f))
    if not all_files:
        log("ℹ️  No new files to commit")
        return

    all_files.sort(key=lambda x: os.path.getsize(x), reverse=True)
    batch, batch_size, batch_num = [], 0, 1
    total_size = sum(os.path.getsize(f) for f in all_files)
    total_batches = max((total_size + PUSH_BATCH_BYTES - 1) // PUSH_BATCH_BYTES, 1)

    for f in all_files:
        fsize = os.path.getsize(f)
        if batch_size + fsize > PUSH_BATCH_BYTES and batch:
            commit_and_push_batch(batch, batch_num, total_batches)
            batch, batch_size = [], 0
            batch_num += 1
        batch.append(f)
        batch_size += fsize
    if batch:
        commit_and_push_batch(batch, batch_num, total_batches)

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
def cleanup_missing_folders(state):
    changed = False
    for section in ["downloads", "repos"]:
        to_remove = [
            k for k, v in state.get(section, {}).items()
            if v.get("folder") and not os.path.exists(v["folder"])
        ]
        for k in to_remove:
            log(f"🧹 Pruning stale state: {k}")
            del state[section][k]
            changed = True
    to_remove = [
        url for url, info in state.get("ranges", {}).items()
        if info.get("folder") and not os.path.exists(info["folder"])
    ]
    for url in to_remove:
        del state["ranges"][url]
        changed = True
    return changed

def prune_old_state_entries(state, max_age_days=90):
    cutoff = datetime.now(timezone.utc).timestamp() - max_age_days * 86400
    changed = False
    for section in ["downloads", "repos"]:
        to_remove = [
            k for k, v in state.get(section, {}).items()
            if v.get("folder") and os.path.exists(v["folder"])
            and os.path.getmtime(v["folder"]) < cutoff
        ]
        for k in to_remove:
            log(f"🧹 Pruning old entry: {k}")
            del state[section][k]
            changed = True
    return changed

def remove_old_releases(repo_name, state):
    if repo_name in state["repos"]:
        old_folder = state["repos"][repo_name].get("folder")
        if old_folder:
            log(f"🗑️  Removing old release: {old_folder}")
            run(f"git rm -r --ignore-unmatch {old_folder} 2>/dev/null || true",
                check=False, quiet=True, shell=True)
            if os.path.exists(old_folder):
                shutil.rmtree(old_folder, ignore_errors=True)
        del state["repos"][repo_name]

def cleanup_removed_repos(state, current_repos):
    removed = [r for r in state["repos"] if r not in current_repos]
    for repo in removed:
        remove_old_releases(repo, state)
    return len(removed) > 0

# ------------------------------------------------------------------------------
# Main workflows
# ------------------------------------------------------------------------------
def _normalize_line(line):
    line = line.strip()
    m = re.match(r'^https?://github\.com/([^/]+/[^/]+)/releases/.*\s*\[(.*)\]$', line)
    if m:
        repo = m.group(1)
        return parse_filter(f"{repo} [{m.group(2)}]")
    m = re.match(r'^https?://github\.com/([^/]+/[^/]+)/releases/.*$', line)
    if m:
        return m.group(1), None, False
    return parse_filter(line)

def process_updates(no_push=False):
    _7z_available()
    log("🔄 Starting repo.txt update", "INFO")
    log(f"Compression: {COMPRESSION_METHOD} level {COMPRESSION_LEVEL}")
    state = load_state()
    if cleanup_missing_folders(state): save_state(state)
    if prune_old_state_entries(state): save_state(state)

    if not Path("repo.txt").exists():
        log("⚠️  repo.txt not found – skipping updates")
        return

    lines = [l.strip() for l in open("repo.txt") if l.strip() and not l.startswith('#')]
    current_repos = []
    new_folders = []

    for line in lines:
        repo, filters, no_compress = _normalize_line(line)
        if not repo:
            continue
        if repo.startswith("http://") or repo.startswith("https://"):
            log(f"\n🔗 Direct URL: {repo}")
            try:
                folder = download_and_chunk(repo, "downloads", no_compress=no_compress)
                state["downloads"][repo] = {"folder": folder}
                if folder: new_folders.append(folder)
            except Exception as e:
                log(f"❌ Failed to process direct URL: {e}", "ERROR")
        else:
            current_repos.append(repo)

    if cleanup_removed_repos(state, current_repos):
        log("🧹 Removed old repos not in repo.txt")

    for line in lines:
        repo, filters, no_compress = _normalize_line(line)
        if not repo or repo.startswith("http://") or repo.startswith("https://"):
            continue
        log(f"\n📋 Repo: {repo} (filter: {filters or 'all'}, nocompress: {no_compress})")
        remove_old_releases(repo, state)
        try:
            folder, _, tag = github_release(
                f"https://github.com/{repo}/releases/latest",
                "repos", filters, no_compress
            )
            state["repos"][repo] = {"folder": folder, "tag": tag}
            new_folders.append(folder)
        except Exception as e:
            log(f"❌ Failed to process release {repo}: {e}", "ERROR")

    save_state(state)
    update_root_readme(state)
    new_folders.extend(["state.json", "INDEX.md"])
    if not no_push:
        batch_commit_and_push(new_folders)
    log("✅ Repo updates finished")

def process_commit(custom_msg=None, no_push=False):
    _7z_available()
    msg = custom_msg or run("git log -1 --pretty=%B", shell=True)
    log(f"📩 Commit message: {msg}")
    state = load_state()
    if cleanup_missing_folders(state): save_state(state)
    if prune_old_state_entries(state): save_state(state)

    no_compress = detect_no_compress(msg)
    if no_compress:
        log("🏷️  [nocompress] flag detected in commit – all files will be kept raw")

    new_folders = []
    urls = URL_PATTERN.findall(msg)

    if len(urls) > 1:
        log(f"📦 Processing {len(urls)} separate URLs")
        for url in urls:
            log(f"\n🌐 {url}")
            try:
                if GITHUB_RELEASE_PATTERN.match(url):
                    folder, repo, tag = github_release(url, "repos", ["all"], no_compress=no_compress)
                    state["repos"][repo] = {"folder": folder, "tag": tag}
                else:
                    folder = download_and_chunk(url, "downloads", no_compress=no_compress)
                    if folder:
                        state["downloads"][url] = {"folder": folder}
                new_folders.append(folder)
            except Exception as e:
                log(f"❌ {e}", "ERROR")
        save_state(state)
        update_root_readme(state)
        new_folders.extend(["state.json", "INDEX.md"])
        if not no_push:
            batch_commit_and_push(new_folders)
        log(f"🎉 Done processing {len(urls)} URLs")
        return

    range_m = RANGE_PATTERN.search(msg)
    if range_m:
        url = range_m.group(1)
        start_mb, end_mb = int(range_m.group(2)), int(range_m.group(3))
        start, end = start_mb * 1024 * 1024, end_mb * 1024 * 1024 - 1
        folder = range_download(url, start, end, "downloads")
        update_root_readme(state)
        new_folders.append(folder)
        new_folders.extend(["state.json", "INDEX.md"])
        if not no_push:
            batch_commit_and_push(new_folders)
        return

    url_m = URL_PATTERN.search(msg)
    if not url_m:
        log("ℹ️  No URL found in commit")
        return
    url = url_m.group(1)
    log(f"🌐 Processing single URL: {url}")
    try:
        if GITHUB_RELEASE_PATTERN.match(url):
            folder, repo, tag = github_release(url, "repos", ["all"], no_compress=no_compress)
            state["repos"][repo] = {"folder": folder, "tag": tag}
        else:
            folder = download_and_chunk(url, "downloads", no_compress=no_compress)
            if folder:
                state["downloads"][url] = {"folder": folder}
        new_folders.append(folder)
        if folder:
            save_state(state)
            update_root_readme(state)
            new_folders.extend(["state.json", "INDEX.md"])
            if not no_push:
                batch_commit_and_push(new_folders)
        log(f"🎉 Done → {folder}")
    except Exception as e:
        log(f"❌ Failed: {e}", "ERROR")
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
        log(f"🔥 Fatal error: {e}", "ERROR")
        raise
