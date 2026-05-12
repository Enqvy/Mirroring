#!/usr/bin/env python3
import os, sys, json, re, time, shutil, subprocess, argparse, tempfile, zipfile
import fnmatch, zlib, mimetypes
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, quote
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache

import requests
from tqdm import tqdm

try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib
    except ImportError:
        tomllib = None

_DEFAULTS = {
    "split_mb": 99,
    "push_batch_bytes": 500 * 1024 * 1024,
    "max_parallel": 4,
    "compression_level": 9,
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

def _load_config():
    config = _DEFAULTS.copy()
    script_dir = Path(__file__).resolve().parent
    cfg_file = script_dir / "config.toml"
    if cfg_file.is_file() and tomllib is not None:
        try:
            with open(cfg_file, "rb") as fh:
                user = tomllib.load(fh)
            for key in _DEFAULTS:
                if key in user:
                    config[key] = user[key]
        except Exception:
            pass
    return config

CFG = _load_config()

SPLIT_MB            = CFG["split_mb"]
PUSH_BATCH_BYTES    = CFG["push_batch_bytes"]
MAX_PARALLEL        = CFG["max_parallel"]
COMPRESSION_LEVEL   = CFG["compression_level"]
EXTRACT_ARCHIVE_EXTS = set(CFG["extract_archive_exts"])
SKIP_ASSET_EXTS     = set(CFG["skip_asset_exts"])

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

IN_GIT_REPO = os.path.exists('.git')

# ── MIME detection (cached) ──
try:
    import magic
    HAVE_MAGIC = True
except ImportError:
    HAVE_MAGIC = False

@lru_cache(maxsize=256)
def _mime_from_file(path: str) -> str:
    if HAVE_MAGIC:
        return magic.from_file(path, mime=True)
    try:
        return subprocess.check_output(['file', '--mime-type', '-b', path], text=True).strip()
    except Exception:
        return None

def fix_extension(filepath):
    """Only add an extension if the file has none, avoiding collisions."""
    if os.path.splitext(filepath)[1]:
        return filepath
    mime = _mime_from_file(filepath)
    if not mime:
        return filepath
    ext = mimetypes.guess_extension(mime, strict=False)
    if not ext:
        return filepath
    new_path = filepath + ext
    if os.path.exists(new_path):
        log(f"⚠️  Target {new_path} already exists, leaving original", "WARN")
        return filepath
    shutil.move(filepath, new_path)
    log(f"🔧 Added extension: {os.path.basename(filepath)} -> {os.path.basename(new_path)}")
    return new_path

# ── Utility functions ──
def log(msg, level="INFO"):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {level}: {msg}")

def run(cmd, check=True, quiet=False, timeout=3600, shell=None):
    quiet = quiet or not VERBOSE
    if shell is None:
        use_shell = not isinstance(cmd, list)
    else:
        use_shell = shell
    if isinstance(cmd, list):
        cmd_args = cmd
    else:
        cmd_args = cmd.split() if not use_shell else cmd
    if not quiet:
        display = ' '.join(cmd) if isinstance(cmd, list) else cmd
        log(f"⚡ {display}", "DEBUG")
    try:
        proc = subprocess.run(cmd_args, shell=use_shell, capture_output=True, text=True, timeout=timeout)
        if proc.stdout.strip() and not quiet:
            log(f"↳ {proc.stdout.strip()}", "DEBUG")
        if check and proc.returncode != 0:
            err = proc.stderr.strip()
            log(f"❌ Command failed: {display}\n   {err}", "ERROR")
            raise RuntimeError(f"Command failed (exit {proc.returncode}): {display}")
        return proc.stdout.strip()
    except subprocess.TimeoutExpired:
        log(f"⏰ Timeout: {display}", "ERROR")
        raise
    except Exception as e:
        log(f"💥 {e}", "ERROR")
        raise

def git_run(cmd, check=True, quiet=False, shell=None):
    if IN_GIT_REPO:
        return run(cmd, check=check, quiet=quiet, shell=shell)
    return ""

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

def load_state():
    if not os.path.exists(STATE_FILE) or os.path.getsize(STATE_FILE) == 0:
        log("📄 No state.json or empty – starting fresh")
        return {"repos": {}, "downloads": {}, "ranges": {}}
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except json.JSONDecodeError:
        log("⚠️  state.json corrupted – starting fresh")
        return {"repos": {}, "downloads": {}, "ranges": {}}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)
    git_run("git add -f state.json 2>/dev/null || true", check=False, quiet=True, shell=True)

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
            if not folder:
                continue
            folder_path = Path(folder)
            if not folder_path.exists():
                content.append(f"## {folder_path.name}")
                content.append("")
                content.append("*This folder no longer exists.*")
                content += ["", "---", ""]
                continue

            stored_files = info.get("files", [])
            if stored_files:
                meta_file = folder_path / "metadata.json"
                title = folder_path.name
                if meta_file.exists():
                    try:
                        meta = json.loads(meta_file.read_text())
                        artist = meta.get("artist", "")
                        album = meta.get("album", "")
                        if artist and album:
                            title = f"{artist} - {album}"
                        elif album:
                            title = album
                    except:
                        pass
                content.append(f"## {title}")
                content.append("")
                content.append("| File | Size | CRC32 |")
                content.append("|--- |--- |---|")
                for f in stored_files:
                    name = f["name"]
                    size = human_size(f["size"])
                    crc = f.get("crc32", "")
                    rel = f"{folder}/{quote(name)}"
                    link = f"https://github.com/{GITHUB_REPOSITORY}/raw/main/{url_encode(rel)}"
                    content.append(f"| [`{name}`]({link}) | {size} | {crc} |")
            else:
                files = sorted(
                    f for f in folder_path.iterdir()
                    if f.is_file() and f.name not in ("README.md", "metadata.json", ".gitkeep")
                )
                if not files:
                    content.append(f"## {folder_path.name}")
                    content.append("*No files.*")
                else:
                    content.append(f"## {folder_path.name}")
                    content.append("| File | Size |")
                    content.append("|--- |---|")
                    for f in files:
                        rel = f"{folder}/{f.name}"
                        sz = human_size(f.stat().st_size)
                        name = unquote(f.name)
                        link = f"https://github.com/{GITHUB_REPOSITORY}/raw/main/{url_encode(rel)}"
                        content.append(f"| [`{name}`]({link}) | {sz} |")
            content += ["", "---", ""]
    Path("INDEX.md").write_text("\n".join(content))
    log("📄 INDEX.md regenerated")

# ── Filter parsing (unchanged from original, no bugs found) ──
def parse_filter(line):
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
        processed = ["all"]
        for r in real:
            if r.lower() == 'all':
                continue
            if r.startswith('!'):
                pattern = r[1:]
                if '*' in pattern or '?' in pattern:
                    processed.append(f"!{pattern}")
                else:
                    processed.append(f"!{'.' + pattern.lstrip('.')}")
            else:
                if '*' in r or '?' in r:
                    processed.append(r)
                else:
                    processed.append('.' + r.lstrip('.'))
        return repo, processed, no_compress, pre_release, use_lfs

    processed = []
    for r in real:
        if r.startswith('!'):
            pattern = r[1:]
            if '*' in pattern or '?' in pattern:
                processed.append(f"!{pattern}")
            else:
                processed.append(f"!{'.' + pattern.lstrip('.')}")
        else:
            if '*' in r or '?' in r:
                processed.append(r)
            else:
                processed.append('.' + r.lstrip('.'))
    return repo, processed, no_compress, pre_release, use_lfs

def asset_matches(name, filters):
    if not filters:
        return True
    nl = name.lower()
    for f in filters:
        if f.startswith('!'):
            pattern = f[1:]
            if pattern.startswith('.'):
                if nl.endswith(pattern.lower()):
                    return False
            else:
                if fnmatch.fnmatch(nl, pattern.lower()):
                    return False
    if "all" in filters:
        return True
    inc = [f for f in filters if not f.startswith('!')]
    if not inc:
        return True
    for f in inc:
        if f.startswith('.'):
            if nl.endswith(f.lower()):
                return True
        else:
            if fnmatch.fnmatch(nl, f.lower()):
                return True
    return False

def is_skip_asset(name):
    nl = name.lower()
    for ext in SKIP_ASSET_EXTS:
        if nl.endswith(ext): return True
    return False

def detect_no_compress(msg):
    return bool(NOCOMPRESS_COMMIT.search(msg))

def _ensure_zip_available():
    if shutil.which("zip") is None:
        raise RuntimeError("zip is not installed – install zip")

# ── Compression and splitting (safe subprocess calls) ──
def _zip_archive_single(filepath, out_zip, store=False):
    level = 0 if store else COMPRESSION_LEVEL
    cmd = ["zip", "-r"]
    if level == 0:
        cmd.append("-0")
    else:
        cmd.append(f"-{level}")
    cmd.extend([out_zip, filepath])
    run(cmd)

def _zip_archive_dir(dirpath, out_zip, store=False):
    level = 0 if store else COMPRESSION_LEVEL
    cmd = ["zip", "-r"]
    if level == 0:
        cmd.append("-0")
    else:
        cmd.append(f"-{level}")
    cmd.extend([out_zip, "."])
    run(cmd, cwd=dirpath)   # run in the directory

def _zip_split(filepath, store=True):
    base = os.path.splitext(filepath)[0]
    if filepath.lower().endswith('.zip'):
        base = filepath[:-len('.zip')] + '_split'
    out_zip = base + ".zip"
    level = "0" if store else str(COMPRESSION_LEVEL)
    # use argument list to avoid shell injection
    cmd = ["zip", "-r", f"-{level}", "-s", f"{SPLIT_MB}m", out_zip, filepath]
    log(f"✂️  Splitting {os.path.basename(filepath)} into {SPLIT_MB} MB volumes")
    run(cmd, shell=False)   # no shell needed
    if os.path.exists(filepath):
        os.remove(filepath)
    if os.path.exists(filepath):
        raise RuntimeError(f"Failed to delete original after split: {filepath}")

def archive_file(filepath, folder, no_compress=False):
    fp = Path(filepath)
    ext = fp.suffix.lower()
    orig = fp.stat().st_size
    if ext in {'.zip', '.7z'}:
        no_compress = True
    log(f"🗜️  {fp.name} ({human_size(orig)})" + (" (nocompress)" if no_compress else ""))
    check_disk_space(folder, orig * 2)

    if no_compress:
        if orig <= SPLIT_MB * 1024 * 1024:
            log(f"📄 Keeping raw (≤ {SPLIT_MB} MB)")
            return filepath
        log("📦 Large file → store‑mode split")
        _zip_split(filepath, store=True)
        return os.path.splitext(filepath)[0] + ".zip"

    out_zip = os.path.join(folder, fp.stem + ".zip")
    tmp_extract = None
    ok = False
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
            _zip_archive_dir(tmp_extract, out_zip)
        else:
            _zip_archive_single(filepath, out_zip)
        new_size = os.path.getsize(out_zip)
        ok = True
    except Exception as e:
        log(f"Compression failed: {e}, keeping original as‑is", "WARN")
    finally:
        if tmp_extract and os.path.exists(tmp_extract):
            shutil.rmtree(tmp_extract, ignore_errors=True)

    if ok and new_size < orig:
        log(f"✅ Compressed: {human_size(new_size)} (saved {human_size(orig - new_size)})")
        os.remove(filepath)
        final = out_zip
    else:
        if ok:
            log("⚠️  No space saved → using original")
            if os.path.exists(out_zip):
                os.remove(out_zip)
        else:
            log("📄 Keeping original file unchanged")
        final = filepath

    limit = SPLIT_MB * 1024 * 1024
    while os.path.exists(final) and os.path.getsize(final) > limit:
        log(f"🔁 Splitting {os.path.basename(final)} ({human_size(os.path.getsize(final))})")
        _zip_split(final, store=False)
        # after split the original is removed, so if it still exists something went wrong
        if os.path.exists(final):
            log("❌ Split did not remove original, retrying", "ERROR")
            _zip_split(final, store=False)
            if os.path.exists(final):
                raise RuntimeError("Cannot split file after multiple attempts")

    if not os.path.exists(final):
        return os.path.splitext(filepath)[0] + ".zip"
    return final

def ensure_all_files_small(folder):
    limit = SPLIT_MB * 1024 * 1024
    for f in Path(folder).iterdir():
        if f.is_file() and f.name not in ("README.md", "metadata.json"):
            if f.stat().st_size > limit:
                log(f"⚠️  Safety split: {f.name} ({human_size(f.stat().st_size)})")
                _zip_split(str(f), store=False)

# ── GitHub API ──
def github_api(url):
    headers = {"User-Agent": UA}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"token {GITHUB_TOKEN}"
    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.json()

# ── Download helpers (safe aria2c calls) ──
def download_asset(url, dest):
    """Download with aria2c, then fix extension if missing."""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    fname = unquote(os.path.basename(url.split("?")[0]))
    local = dest / fname
    if local.exists():
        log(f"✓ Already exists: {fname}")
        return fix_extension(str(local))
    log(f"⬇️  {fname}")
    cmd = [
        "aria2c", "--summary-interval=2", "--continue",
        "--max-connection-per-server=8", "--split=8", "--min-split-size=1M",
        f"--dir={dest}", f"--out={fname}",
        "--timeout=120", "--max-tries=5", url
    ]
    run(cmd, timeout=600)
    if not local.exists() or local.stat().st_size == 0:
        raise RuntimeError(f"Download failed: {fname}")
    return fix_extension(str(local))

def download_to_file(url, dest_dir, filename):
    """Download a specific filename with aria2c, then fix extension if missing."""
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    local = dest / filename
    if local.exists():
        log(f"✓ Already exists: {filename}")
        return fix_extension(str(local))
    log(f"⬇️  {filename}")
    cmd = [
        "aria2c", "--summary-interval=2", "--continue",
        "--max-connection-per-server=8", "--split=8", "--min-split-size=1M",
        f"--dir={dest_dir}", f"--out={filename}",
        "--timeout=120", "--max-tries=5", url
    ]
    run(cmd, timeout=600)
    if not local.exists() or local.stat().st_size == 0:
        raise RuntimeError(f"Download failed: {filename}")
    return fix_extension(str(local))

# ── LFS tracker ──
def process_lfs_assets(folder):
    if IN_GIT_REPO:
        for f in Path(folder).iterdir():
            if f.is_file() and f.name not in ("README.md", "metadata.json"):
                rel = os.path.relpath(str(f), os.getcwd())
                git_run(f'git lfs track "{rel}"', shell=True, quiet=True)
        git_run("git add -f .gitattributes", shell=True, quiet=True)

# ── Main download functions ──
def download_and_chunk(url, dest_base, no_compress=False, use_lfs=False):
    state = load_state()
    existing = state.get("downloads", {}).get(url)
    if existing:
        folder = existing.get("folder")
        if folder and os.path.exists(folder) and existing.get("files"):
            missing = any(not os.path.exists(os.path.join(folder, f["name"])) for f in existing["files"])
            if not missing:
                log(f"✅ Direct URL already mirrored, skipping.")
                return folder
            # If missing files, clean up old folder and re-download
            log(f"⚠️  Some files missing, re-downloading {url}")
            shutil.rmtree(folder, ignore_errors=True)

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    base_name = unquote(os.path.basename(url.split("?")[0])) or "file"
    folder = Path(dest_base) / f"{base_name}_{ts}"
    folder.mkdir(parents=True)
    log(f"📁 {folder}")

    path = download_to_file(url, str(folder), base_name)
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

    file_entries = []
    for f in final_files:
        file_entries.append({
            "name": f.name,
            "size": os.path.getsize(str(f)),
            "crc32": crc_info.get(f.name, ""),
            "path": os.path.relpath(str(f), os.getcwd()).replace("\\", "/")
        })

    total_compressed = sum(os.path.getsize(str(f)) for f in final_files)
    savings = {}
    if orig_size > 0 and total_compressed > 0:
        pct = (total_compressed / orig_size - 1) * 100
        label = f"{pct:.1f}%"
        if final_files:
            savings[final_files[0].name] = label

    write_metadata(str(folder), url, "direct", crc32=crc_info,
                   downloaded=datetime.now(timezone.utc).isoformat())
    extra = {"Downloaded": datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}
    write_readme(str(folder), base_name, url, "Direct Download", extra=extra, hashes=crc_info, savings=savings)
    if use_lfs:
        process_lfs_assets(str(folder))

    state["downloads"][url] = {"folder": str(folder), "files": file_entries}
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
    existing = state.get("repos", {}).get(repo)

    # Build wanted list from release assets
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

    # Idempotency check
    if existing and existing.get("tag") == tag:
        stored_original = existing.get("original_assets", [])
        if stored_original:
            stored_set = {(a["name"], a["size"]) for a in stored_original}
            wanted_set = {(w[0], w[2]) for w in wanted}
            if stored_set == wanted_set:
                prev_folder = existing["folder"]
                if os.path.exists(prev_folder):
                    # Verify all expected files actually exist
                    if all(os.path.exists(os.path.join(prev_folder, f["name"]))
                           for f in existing.get("files", [])):
                        log(f"✅ Release {tag} already mirrored, nothing to do.")
                        return prev_folder, repo, tag

    # Remove old version if exists
    if repo in state.get("repos", {}):
        old_folder = state["repos"][repo].get("folder")
        if old_folder and os.path.exists(old_folder):
            log(f"🗑️  Removing old release: {old_folder}")
            shutil.rmtree(old_folder, ignore_errors=True)
        del state["repos"][repo]

    release_name = release.get("name") or tag or repo
    safe_name = SAFE_FILENAME_PATTERN.sub('_', release_name)
    safe_tag = SAFE_FILENAME_PATTERN.sub('_', tag)
    folder = Path(dest_dir) / f"{safe_name}_{safe_tag}"
    if folder.exists():
        shutil.rmtree(str(folder), ignore_errors=True)
    folder.mkdir(parents=True)
    log(f"📁 {folder}")

    if not wanted:
        log("⚠️  No matching assets found.")
        state["repos"][repo] = {
            "folder": str(folder),
            "tag": tag,
            "files": [],
            "original_assets": [],
            "url": url,
            "method": "github_release"
        }
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
        # Compress all downloaded assets in parallel (CPU-bound, but zip can run concurrently)
        assets_to_archive = [str(f) for f in folder.iterdir()
                             if f.is_file() and f.name not in ("README.md", "metadata.json")]
        with ThreadPoolExecutor(max_workers=max(1, MAX_PARALLEL // 2)) as comp_executor:
            list(tqdm(comp_executor.map(
                lambda f: archive_file(f, str(folder), no_compress),
                assets_to_archive
            ), total=len(assets_to_archive), desc="Compressing"))
        ensure_all_files_small(str(folder))

    final_files = [f for f in folder.iterdir() if f.is_file() and f.name not in ("README.md", "metadata.json")]
    crc_info = {f.name: crc32_file(str(f)) for f in final_files}
    total_size = sum(os.path.getsize(str(f)) for f in final_files)

    file_entries = []
    for f in final_files:
        file_entries.append({
            "name": f.name,
            "size": os.path.getsize(str(f)),
            "crc32": crc_info.get(f.name, ""),
            "path": os.path.relpath(str(f), os.getcwd()).replace("\\", "/")
        })

    original_assets = [{"name": name, "size": size} for name, _, size in wanted]

    rel_date = release.get("published_at") or release.get("created_at")
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

    extra = {
        "Downloaded": datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'),
        "Release Date": release_date_str,
        "Total Size": human_size(total_size),
        "Release Name": release_name,
        "Tag": tag,
    }
    write_metadata(str(folder), url, "github_release", repo=repo, tag=tag,
                   assets=original_assets,
                   crc32=crc_info, total_size=total_size, release_date=rel_date, use_lfs=use_lfs,
                   downloaded=datetime.now(timezone.utc).isoformat())
    write_readme(str(folder), repo, f"https://github.com/{repo}/releases/tag/{tag}",
                 "GitHub Release", extra=extra, hashes=crc_info)

    state["repos"][repo] = {
        "folder": str(folder),
        "tag": tag,
        "files": file_entries,
        "original_assets": original_assets,
        "url": url,
        "method": "github_release"
    }
    save_state(state)
    return str(folder), repo, tag

def range_download(url, start, end, base_dir):
    log(f"📡 Range: {url} [{start}-{end}]")
    state = load_state()
    existing = state.get("ranges", {}).get(url)
    if existing:
        folder = Path(existing["folder"])
        # Check that the folder exists and contains at least the split base file
        if folder.exists() and any(f for f in folder.iterdir() if f.name not in ("README.md", "metadata.json")):
            log(f"♻️  Reusing range folder: {folder}")
            return str(folder)
        else:
            # incomplete, delete and re-download
            shutil.rmtree(str(folder), ignore_errors=True)

    bname = unquote(os.path.basename(url.split("?")[0])).rsplit('.', 1)[0]
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    folder = Path(base_dir) / f"{bname}_{ts}"
    folder.mkdir(parents=True, exist_ok=True)
    log(f"📁 New range folder: {folder}")

    tmp_file = folder / "downloaded_range.tmp"
    range_sz = end - start + 1
    check_disk_space(str(folder), range_sz * 2)
    curl_cmd = [
        "curl", "-sSfL", "--retry", "3", "--retry-delay", "5",
        "--connect-timeout", "30", "-r", f"{start}-{end}",
        "-o", str(tmp_file), url
    ]
    run(curl_cmd, timeout=300)
    if not tmp_file.exists() or tmp_file.stat().st_size == 0:
        raise RuntimeError("Range download empty")

    fix_extension(str(tmp_file))
    archive_file(str(tmp_file), str(folder))
    ensure_all_files_small(str(folder))

    final_files = [f for f in folder.iterdir() if f.is_file() and f.name not in ("README.md", "metadata.json")]
    crc_info = {f.name: crc32_file(str(f)) for f in final_files}
    write_metadata(str(folder), url, "direct_chunked", crc32=crc_info,
                   downloaded=datetime.now(timezone.utc).isoformat())
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

# ── Maintenance ──
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
                shutil.rmtree(folder, ignore_errors=True)   # delete folder
                del state[section][k]
                changed = True
    return changed

def cleanup_removed_repos(state, current):
    removed = [r for r in state.get("repos", {}) if r not in current]
    for r in removed:
        old = state["repos"][r].get("folder")
        if old and os.path.exists(old):
            log(f"🗑️  Removing old release: {old}")
            shutil.rmtree(old, ignore_errors=True)
        del state["repos"][r]
    return len(removed) > 0

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
    _ensure_zip_available()
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
    log("✅ Update finished")

def process_commit(custom_msg=None, no_push=False):
    _ensure_zip_available()
    msg = custom_msg or (git_run("git log -1 --pretty=%B", shell=True) if IN_GIT_REPO else "")
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
                    state["repos"][repo] = {"folder": folder, "tag": tag, "files": [], "original_assets": []}
                else:
                    folder = download_and_chunk(url, "downloads", no_compress, False)
                    if folder:
                        state["downloads"][url] = {"folder": folder, "files": []}
                new_folders.append(folder)
            except Exception as e:
                log(f"❌ {e}", "ERROR")
        save_state(state)
        update_index_md(state)
        new_folders.extend(["state.json", "INDEX.md"])
        log(f"🎉 {len(urls)} URLs done")
        return

    rm = RANGE_PATTERN.search(msg)
    if rm:
        url = rm.group(1)
        start_mb, end_mb = int(rm.group(2)), int(rm.group(3))
        start, end = start_mb * 1024 * 1024, end_mb * 1024 * 1024 - 1
        folder = range_download(url, start, end, "downloads")
        update_index_md(state)
        new_folders.extend(["state.json", "INDEX.md"])
        log("✅ Range download complete")
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
            state["repos"][repo] = {"folder": folder, "tag": tag, "files": [], "original_assets": []}
        else:
            folder = download_and_chunk(url, "downloads", no_compress, False)
            if folder:
                state["downloads"][url] = {"folder": folder, "files": []}
        new_folders.append(folder)
        if folder:
            save_state(state)
            update_index_md(state)
            new_folders.extend(["state.json", "INDEX.md"])
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
