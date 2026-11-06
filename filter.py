import re
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor

SOURCES_FILE = "sources.txt"   # playlist URLs, highest priority first
WANTED_FILE = "channels.txt"
OUTPUT = "playlist.m3u"
TIMEOUT = 10                   # seconds per stream check
WORKERS = 20
HEADERS = {"User-Agent": "Mozilla/5.0"}


def read_list(path):
    with open(path, encoding="utf-8") as f:
        return [l.strip() for l in f if l.strip() and not l.startswith("#")]


def fetch(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode("utf-8", errors="replace")


def parse(text):
    entries, block = [], []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#EXTM3U"):
            continue
        if line.startswith("#"):
            block.append(line)
        else:
            if block:
                entries.append((block, line))
            block = []
    return entries


def extinf(block):
    return next((l for l in block if l.startswith("#EXTINF")), "")


def search_text(block):
    line = extinf(block)
    m = re.search(r'tvg-id="([^"]*)"', line)
    tvg_id = m.group(1) if m else ""
    return f"{line.split(',', 1)[-1]} {tvg_id}".lower()


def group_key(block):
    """Same channel across sources: name without (720p) / [Geo-blocked] tags."""
    name = extinf(block).split(",", 1)[-1]
    name = re.sub(r"[\(\[].*?[\)\]]", "", name)
    return re.sub(r"\s+", " ", name).strip().lower()


def is_alive(url):
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            if r.status != 200:
                return False
            chunk = r.read(2048)
            if not chunk:
                return False
            ctype = r.headers.get("Content-Type", "").lower()
            if ".m3u8" in url.lower() or "mpegurl" in ctype:
                return b"#EXTM3U" in chunk
            return True
    except Exception:
        return False


def pick(item):
    key, cands = item
    for n, (block, url, src) in enumerate(cands):
        if is_alive(url):
            return key, block, url, src, n, len(cands)
    return key, None, None, None, None, len(cands)


def main():
    wanted = [w.lower() for w in read_list(WANTED_FILE)]
    sources = read_list(SOURCES_FILE)

    groups, matched = {}, set()
    for i, src in enumerate(sources):
        try:
            entries = parse(fetch(src))
        except Exception as e:
            print(f"WARNING: could not load source {i + 1}: {e}")
            continue
        print(f"Source {i + 1}: {len(entries)} streams")
        for block, url in entries:
            text = search_text(block)
            hits = [w for w in wanted if w in text]
            if not hits:
                continue
            matched.update(hits)
            cands = groups.setdefault(group_key(block), [])
            if all(url != c[1] for c in cands):
                cands.append((block, url, i))

    for w in wanted:
        if w not in matched:
            print(f"WARNING: no match for '{w}'")

    print(f"Checking {len(groups)} channels...")
    with ThreadPoolExecutor(WORKERS) as ex:
        results = list(ex.map(pick, groups.items()))

    out = []
    for key, block, url, src, n, total in results:
        if url is None:
            print(f"DEAD   {key} (all {total} URLs failed)")
            continue
        note = "" if n == 0 else f" [backup #{n} used]"
        print(f"OK     {key} (source {src + 1}){note}")
        out.append((block, url))

    if not out:
        print("No working streams - not overwriting playlist.")
        sys.exit(1)

    with open(OUTPUT, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for block, url in out:
            f.write("\n".join(block) + "\n" + url + "\n")
    print(f"Wrote {len(out)} working channels to {OUTPUT}")


if __name__ == "__main__":
    main()
    
