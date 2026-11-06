import re
import sys
import urllib.request

SOURCE = "https://iptv-org.github.io/iptv/countries/in.m3u"
WANTED_FILE = "channels.txt"
OUTPUT = "playlist.m3u"


def load_wanted():
    with open(WANTED_FILE, encoding="utf-8") as f:
        return [l.strip().lower() for l in f if l.strip() and not l.startswith("#")]


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode("utf-8", errors="replace")


def parse(text):
    """Return list of (header_lines, url). Keeps #EXTINF and any #EXTVLCOPT lines."""
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


def search_text(block):
    extinf = next((l for l in block if l.startswith("#EXTINF")), "")
    name = extinf.split(",", 1)[-1]
    m = re.search(r'tvg-id="([^"]*)"', extinf)
    tvg_id = m.group(1) if m else ""
    return f"{name} {tvg_id}".lower()


def main():
    wanted = load_wanted()
    entries = parse(fetch(SOURCE))
    print(f"Source has {len(entries)} streams")

    kept, matched = [], set()
    for block, url in entries:
        text = search_text(block)
        hits = [w for w in wanted if w in text]
        if hits:
            matched.update(hits)
            kept.append((block, url))

    for w in wanted:
        if w not in matched:
            print(f"WARNING: no match for '{w}'")

    if not kept:
        print("No channels matched - not overwriting playlist.")
        sys.exit(1)

    with open(OUTPUT, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for block, url in kept:
            f.write("\n".join(block) + "\n" + url + "\n")
    print(f"Wrote {len(kept)} streams to {OUTPUT}")


if __name__ == "__main__":
    main()
  
