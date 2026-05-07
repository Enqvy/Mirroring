# Download Manager

Automated mirroring of GitHub releases and direct URLs.  
Every asset is wrapped inside a **`.zip` container** (optional compression) and split into ⩽ 99 MB volumes so they never exceed GitHub’s file size limit.  
Already‑compressed archives (`.zip`, `.7z`) are stored as‑is – only split if they exceed the threshold.  
All other files are compressed with **Deflate (level 9)** by default.

---

## ⚠️ Disclaimer

**This tool is provided “as is”, without warranty of any kind.**  
I do **not** take any responsibility for the content that is downloaded, stored, or distributed by anyone using this script.  

All files mirrored by this repository are the **property of their respective owners**. This project is intended for **personal/archival use, software preservation, and fair‑use mirroring** only.  

If you are a copyright holder and believe your work is being mirrored without permission, please open an issue and the content will be removed immediately.

**I do not endorse, verify, or guarantee the safety of any linked content.**  
Download and use mirrored files at your own risk.

---

## 📥 Download Index

[View all mirrored downloads →](INDEX.md)

---

## How it works

1. Reads `repo.txt` for a list of GitHub repositories (or direct URLs).  
2. Fetches the **latest** release (including pre‑releases if `[pre]` is set).  
3. Downloads only the assets that match your filters.  
4. **`.zip` / `.7z` files** are left untouched (store mode) – they are never re‑compressed.  
5. **All other files** are compressed into a `.zip` archive with Deflate level 9.  
   - If compression doesn’t reduce the size, the original file is kept as‑is.  
6. Any file larger than **99 MB** is split into store‑mode `.zip` volumes.  
7. Writes per‑folder `README.md` files (shown inside `INDEX.md`).  
8. Pushes everything back to your repository.

All settings can be adjusted in **`config.toml`** (compression level, split size, etc.).

---

## Setup

1. Copy the workflow file into `.github/workflows/downloader.yml`.  
2. Ensure the workflow has `contents: write` permission.  
3. The script is stored at `scripts/download_manager.py`.  
4. Create a `repo.txt` (see syntax below).  
5. (Optional) Create a `config.toml` next to the script for custom settings.  

The workflow runs every **12 hours** and on every push to `main`.

---

## `repo.txt` syntax

| Syntax | Explanation |
|--------|-------------|
| `owner/repo` | Download all assets with default extensions (`.exe`, `.zip`, `.apk`) |
| `owner/repo [ext1, ext2]` | Only assets ending with those extensions |
| `owner/repo [file*name.exe]` | Globs – wildcards `*` and `?` allowed |
| `owner/repo [all]` | All assets (except checksum/signature files) |
| `owner/repo [nocompress]` | Keep files ≤ 99 MB raw, split larger ones without compression |
| `owner/repo [pre]` | Fetch the absolute latest release (including pre‑releases) |
| `owner/repo [lfs]` | Use Git LFS for the file – no compression, no splitting |
| `https://github.com/owner/repo/releases/latest [filter]` | Full release URL, automatically converted |
| `https://example.com/file.zip` | Direct download URL |
| `https://example.com/file.zip [nocompress]` | Direct download without compression |

Flags can be combined: `[pre, nocompress, all]`

---

## Commit‑triggered downloads

Push a commit containing one or more URLs – the workflow will download them immediately.  

Add `[nocompress]` anywhere in the commit message to skip compression for **all** URLs in that commit.

```
git commit -m "https://example.com/tool.zip [nocompress]"
```

---

## Range downloads

Download a specific byte range of a large file.  
Commit message format:

```
URL [startMB-endMB]
```

**Example – download the first 200 MB:**

```
https://example.com/big.iso [0-200]
```

---

## Output & file structure

Each download creates a timestamped folder:

- **`downloads/`** – direct URLs  
- **`repos/`** – GitHub releases  

Inside each folder:

- The mirrored file(s) – either raw or inside a `.zip` container.  
- `README.md` – list of files with sizes, CRC32 hashes, and compression percentages.  
- `metadata.json` – URL, method, checksums, and asset info.  

All files are accessible via raw GitHub links.

---

## Extracting `.zip` files

**Single `.zip` file:**  
```bash
unzip file.zip
```

**Split volumes (`.z01`, `.z02`, … `.zip`):**  
Place all parts in the same folder and run:

```bash
zip -FF file.zip --out repaired.zip && unzip repaired.zip
```

Or use a tool like `7z`:

```bash
7z x file.zip
```

---

## Incremental updates

- **GitHub releases** – the script remembers the tag of the last mirrored release.  
  If the tag hasn’t changed, the whole release is **skipped**.  
- **Direct downloads** – once a direct URL has been downloaded, it’s recorded in `state.json`. The same URL will never be downloaded again.

This keeps your repository small and the runs fast.

---

## Automatic checksum / signature skipping

The following extensions are **always ignored**, even with `[all]`:

`.sha256`, `.sha256sum`, `.sha512`, `.sha512sum`, `.sha1`, `.sha1sum`, `.md5`, `.md5sum`, `.asc`, `.sig`, `.sign`, `.pgp`, `.blake2b`, `.blake2s`, `.sha3`, and various `.txt`/`.sums` variants.

---

## Extension‑less files

If a downloaded file has no extension (e.g. an executable named `zyrln-linux-amd64`), the script uses `file --mime-type` and Python’s `mimetypes` module to guess the correct extension and renames the file.

- `video/mp4` → `*.mp4`  
- `application/x-dosexec` → `*.exe`  

If the type cannot be determined, the file is **kept as‑is** (no renaming, no deletion).

---

## Per‑file CRC32 & compression percentage

Inside each folder’s `README.md` (and therefore in `INDEX.md`), you’ll see:

- **CRC32** checksum for every final file.  
- **Compression percentage** (e.g. `-12.3%`) showing the space saved compared to the original release asset (only for files that were actually compressed).  
- Already‑compressed archives (`.zip`/`.7z`) will not show a percentage because they are stored as‑is.

---

## Configuration (`config.toml`)

Place a file named `config.toml` next to `download_manager.py` to override defaults:

```toml
split_mb = 99                # file‑size threshold for splitting (MiB)
push_batch_bytes = 367001600  # max bytes per git push (350 MiB)
max_parallel = 4              # simultaneous downloads
compression_level = 9         # 0 = store, 1 = fastest, 9 = best
compression_method = "Deflate" # kept for compatibility, ignored
extract_archive_exts = [".zip", ".jar", ".war", ".ear"]
skip_asset_exts = [ … ]       # list of extensions to ignore
```

If the file is missing, defaults are used.

---

## Tips

- Use **glob filters** to catch version‑independent installers (e.g. `app_*_setup.exe`).  
- Use **`[nocompress]`** for rule sets / config files that are updated frequently.  
- The workflow runs every **12 hours** – no need to poll manually.  
- If a push fails because of a 100 MB limit, check the logs – the split guarantee should have kicked in; if not, temporarily increase `SPLIT_MB` to 95.  
- `.zip` and `.7z` files are never re‑compressed; they are only split if they exceed the size limit.

---

## Example `repo.txt`

```
# VPN apps
therealaleph/MasterHttpRelayVPN-RUST [all, pre]
ajavadinezhad/zyrln [all, pre]

# Tools
2dust/v2rayN [v2rayN-linux-64.zip, pre]
2dust/v2rayNG [v2rayNG_*_universal.apk, pre]

# Windows
imputnet/helium-windows [helium_*_x64-installer.exe, pre]

# Android
MetaCubeX/ClashMetaForAndroid [cmfa-*-meta-universal-release.apk, pre]

# Rules (raw, no compression)
Chocolate4U/Iran-v2ray-rules [all, nocompress, pre]

# Direct download
https://example.com/files/tool.bin
```
