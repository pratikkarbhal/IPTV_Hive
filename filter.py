import re
import sys
import threading
import urllib.request
from concurrent.futures import ThreadPoolExecutor

SOURCES_FILE = "sources.txt"   # playlist URLs, highest priority first
WANTED_FILE = "channels.txt"   # one channel per line = one entry in the output
OUTPUT = "playlist.m3u"
TIMEOUT = 10                   # seconds per stream check
WORKERS = 20
MAX_TRIES_PER_SOURCE = 5       # max candidates tested per channel per source
HEADERS = {"User-Agent": "Mozilla/5.0"}

_cache, _lock = {}, threading.Lock()


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


def clean_name(block):
    """Channel name without (720p) / [Geo-blocked] tags."""
    name = extinf(block).split(",", 1)[-1]
    name = re.sub(r"[\(\[].*?[\)\]]", "", name)
    return re.sub(r"\s+", " ", name).strip().lower()


def get_entries(i, url):
    """Download a source only when it is first needed."""
    with _lock:
        if i not in _cache:
            try:
                _cache[i] = parse(fetch(url))
                print(f"Loaded source {i + 1}: {len(_cache[i])} streams")
            except Exception as e:
                print(f"WARNING: could not load source {i + 1}: {e}")
                _cache[i] = []
        return _cache[i]


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


def resolve(args):
    """Go through sources in priority order; stop at the first working stream."""
    term, sources = args
    t = term.lower()
    for i, src in enumerate(sources):
        cands = [(b, u) for b, u in get_entries(i, src) if t in search_text(b)]
        # exact channel name first, then shorter (closer) names
        cands.sort(key=lambda c: (clean_name(c[0]) != t, len(clean_name(c[0]))))
        seen = set()
        for block, url in cands:
            if url in seen:
                continue
            seen.add(url)
            if len(seen) > MAX_TRIES_PER_SOURCE:
                break
            if is_alive(url):
                return term, block, url, i
    return term, None, None, None


def main():
    terms = read_list(WANTED_FILE)
    sources = read_list(SOURCES_FILE)

    with ThreadPoolExecutor(WORKERS) as ex:
        results = list(ex.map(resolve, [(t, sources) for t in terms]))  # keeps channels.txt order

    out, used = [], set()
    for term, block, url, src in results:
        if url is None:
            print(f"DEAD   {term} (nothing worked in any source)")
            continue
        if url in used:
            print(f"SKIP   {term} (same stream already added)")
            continue
        used.add(url)
        note = "" if src == 0 else " [backup source]"
        print(f"OK     {term} -> {clean_name(block)} (source {src + 1}){note}")
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
    
