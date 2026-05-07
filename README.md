A complete reference for the **GitHub‑based download & archive manager**.

---

## 1. What does this tool do?

- Monitors a list of GitHub repositories (or direct URLs).
- Downloads the **latest** release assets that match your filters.
- Wraps every asset in a `.7z` container (compressed or store‑only, depending on settings).
- Splits files larger than **99 MB** into store‑mode `.7z.001`, `.7z.002`, … volumes so they never exceed GitHub’s 100 MB file limit.
- Skips checksum / signature files automatically.
- Renames extension‑less files using MIME‑type detection.
- Pushes the mirrored files back to your own GitHub repository.

Everything runs as a **GitHub Actions workflow** every 12 hours (or on push).

---

## 2. Setup

### a. Workflow file
Copy the workflow from the repository into `.github/workflows/downloader.yml`.  
No additional secrets are needed (the built‑in `GITHUB_TOKEN` is used).  
Make sure the workflow has `contents: write` permission.

### b. Dependencies
The workflow installs:
- `aria2` – fast downloader
- `p7zip-full` – 7‑Zip CLI
- `requests`, `psutil`, `tqdm` – Python modules

### c. Repository structure
Your repository should contain:

```
.
├── .github/workflows/download.yml
├── scripts/download_manager.py   ← the Python script
├── repo.txt                      ← your list of sources
├── state.json                    ← auto‑generated, tracks last known release
└── INDEX.md                     ← auto‑generated index
```

---

## 3. `repo.txt` syntax

Each non‑empty, non‑comment line specifies either a **GitHub release source** or a **direct URL**.

### a. GitHub release with default filters
```
owner/repo
```
Downloads all assets with the default extensions: `.exe`, `.zip`, `.apk`

**Example:**
```
imputnet/helium-windows
```

### b. Specify file extensions
```
owner/repo [ext1, ext2, ...]
```
Downloads only assets ending with those extensions (case‑insensitive).  
Extensions can be given with or without a leading dot.

**Example – download only `.exe` and `.appx`:**
```
imputnet/helium-windows [exe, appx]
```

### c. Use filename globs
```
owner/repo [helium_*_x64-installer.exe]
```
Globs support `*` (any characters) and `?` (any single character).  
Matching is **case‑insensitive**.

**Example – download all macOS ZIP files:**
```
clash-verge-rev/clash-verge-rev [Clash.Verge_*_x64.dmg, *.zip]
```

### d. Download all assets
```
owner/repo [all]
```
Downloads **every** asset in the release, minus checksum/signature files (see §9).

### e. No compression (store‑only)
```
owner/repo [all, nocompress]
```
- Files **≤ 99 MB** are kept **raw** (no `.7z` wrapper).
- Files **> 99 MB** are split into store‑mode `.7z` volumes (no compression).

`nocompress` can be combined with any other filter.

**Example – all APK files without compression:**
```
MetaCubeX/ClashMetaForAndroid [cmfa-*-meta-universal-release.apk, nocompress]
```

### f. Release page URL
You can also paste the full release URL. It will be automatically converted to `owner/repo`.

```
https://github.com/imputnet/helium-windows/releases/latest [helium_*_x64-installer.exe]
```

### g. Direct download URL
Any line starting with `http://` or `https://` that is **not** a GitHub release page will be treated as a one‑off direct download.

```
https://example.com/files/tool.zip
```

`[nocompress]` works here too.

---

## 4. Commit‑triggered downloads

If you push a commit containing one or more URLs, the workflow will download those URLs immediately.

### a. Single URL
Commit message:
```
https://example.com/file.bin
```

### b. Multiple URLs
```
https://example.com/one.exe
https://example.com/two.zip
```

### c. GitHub release URL
```
https://github.com/imputnet/helium-windows/releases/latest [helium_*_x64-installer.exe]
```
This is treated as `owner/repo` with the given filter and will be stored in `repos/`.

### d. Using `[nocompress]` in commits
Add `[nocompress]` anywhere in the commit message.  
It applies to **all URLs** in that commit.

```
https://iamworker.com/s7/v5/download/...?token=... [nocompress]
```

---

## 5. Range downloads

You can download a specific byte range of a large file and have it automatically split into volumes.

Commit message format:
```
URL [startMB-endMB]
```
`start` and `end` are in megabytes (MB).

**Example – download the first 200 MB of an ISO:**
```
https://example.com/big.iso [0-200]
```

The range download uses `curl -r` and saves the chunk inside a temporary `.tmp` file, then splits it if needed.

---

## 6. Output & file structure

Each download creates a **timestamped folder**:

- **`downloads/`** – direct URLs
- **`repos/`** – GitHub releases

Folder name format: `{release‑name}_{tag}` or `{filename}_{timestamp}`.

Inside each folder:
- **The mirrored file(s)** – either raw or inside a `.7z` container.
- `INDEX.md` – list of files with sizes and links.
- `metadata.json` – URL, method, CRC32 checksums, and asset info.

All files are accessible via raw GitHub links.

---

## 7. Extracting `.7z` files

**Single `.7z` file:**
```bash
7z x file.7z
```

**Split volumes (`.7z.001`, `.7z.002`, …):**
```bash
7z x file.7z.001
```
The rest of the volumes must be in the same directory.  
For an installer originally named `installer.exe`, you’ll get `installer.exe` after extraction.

---

## 8. Incremental updates

The script remembers the **tag** and **asset names + sizes** of the last processed release.  
On subsequent runs it compares the current release with the stored data.  
If **nothing changed**, the release is **skipped** completely – no re‑download, no new commit.

This keeps your repository small and the runs fast.

---

## 9. Automatic checksum / signature skipping

The following extensions are **always ignored**, even with `[all]`:

`.sha256`, `.sha256sum`, `.sha512`, `.sha512sum`, `.sha1`, `.sha1sum`, `.md5`, `.md5sum`, `.asc`, `.sig`, `.sign`, `.pgp`, `.blake2b`, `.blake2s`, `.sha3`, and various `.txt`/`.sums` variants.

---

## 10. Extension‑less files

If a downloaded file has no extension (e.g. an executable named `zyrln-linux-amd64`), the script uses `file --mime-type` and Python’s `mimetypes` module to guess the correct extension and renames the file.

- `video/mp4` → `*.mp4`
- `application/x-dosexec` → `*.exe`

If the type cannot be determined, the file is **kept as‑is** (no renaming, no deletion).

---

## 11. CRC32 integrity

After processing, a **CRC32** checksum is computed for every final file and stored in `metadata.json`.  
This allows users to verify their downloads without the overhead of SHA‑256.

---

## 12. Tips

- Use **glob filters** to catch version‑independent installers (e.g. `app_*_setup.exe`).
- Use **`[nocompress]`** for text‑based rulesets or configuration files that are updated frequently and are already small.
- The workflow runs every **12 hours** – no need to poll manually.
- If a push fails because of a 100 MB limit, check the logs – the split guarantee should have kicked in; if not, temporarily increase `SPLIT_MB` to 95.

---

## 13. Example `repo.txt`

```
# VPN apps
therealaleph/MasterHttpRelayVPN-RUST [all]
ajavadinezhad/zyrln [all, nocompress]

# Windows tools
imputnet/helium-windows [helium_*_x64-installer.exe]

# Android
MetaCubeX/ClashMetaForAndroid [cmfa-*-meta-universal-release.apk]

# Direct downloads
https://dl.iamworker.com/s7/v5/download/...?token=... [nocompress]
```
