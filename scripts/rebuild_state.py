#!/usr/bin/env python3
import os, json, zlib
from pathlib import Path

STATE_FILE = "state.json"

def crc32_file(path):
    prev = 0
    with open(path, 'rb') as f:
        while chunk := f.read(65536):
            prev = zlib.crc32(chunk, prev)
    return format(prev & 0xFFFFFFFF, '08x')

def build_state():
    state = {"repos": {}, "downloads": {}, "ranges": {}}
    
    for section in ["repos", "downloads"]:
        if not os.path.isdir(section):
            continue
        for entry in os.listdir(section):
            folder = Path(section) / entry
            if not folder.is_dir():
                continue
            meta_file = folder / "metadata.json"
            if not meta_file.exists():
                continue
            
            meta = json.loads(meta_file.read_text())
            url = meta.get("url", "")
            method = meta.get("method", "")
            
            # Final files (after compression/split) – same as before
            final_files = []
            for f in folder.iterdir():
                if f.is_file() and f.name not in ("README.md", "metadata.json", ".gitkeep"):
                    size = f.stat().st_size
                    crc = crc32_file(str(f))
                    final_files.append({
                        "name": f.name,
                        "size": size,
                        "crc32": crc,
                        "path": os.path.relpath(str(f), os.getcwd()).replace("\\", "/")
                    })
            
            entry_data = {
                "folder": str(folder),
                "files": final_files,
                "method": method
            }
            
            if section == "repos":
                repo = meta.get("repo", "")
                tag = meta.get("tag", "")
                if not repo:
                    continue
                # Store original assets from metadata (used for idempotency)
                original_assets = meta.get("assets", [])
                entry_data.update({
                    "tag": tag,
                    "url": url,
                    "original_assets": original_assets
                })
                state["repos"][repo] = entry_data
            else:
                # Direct download – no original assets to track
                entry_data["url"] = url
                state["downloads"][url] = entry_data
    
    # Preserve range data
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                old = json.load(f)
            state["ranges"] = old.get("ranges", {})
        except:
            pass
    
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)
    
    total_files = sum(len(v['files']) for s in ('repos','downloads') for v in state[s].values())
    print(f"state.json rebuilt with {total_files} files across {len(state['repos'])} repos and {len(state['downloads'])} downloads.")

if __name__ == "__main__":
    build_state()
