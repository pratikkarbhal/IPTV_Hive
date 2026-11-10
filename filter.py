import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

SOURCES_FILE = "sources.txt"   # playlist URLs, highest priority first
WANTED_FILE = "channels.txt"   # one line = one channel; separate alternative names with |
OUTPUT = "playlist.m3u"
TIMEOUT = 10                   # seconds per stream check
WORKERS = 20
MAX_TRIES_PER_SOURCE = 5       # max candidates tested per channel per source
CHECK_VIDEO = True             # skip streams that have audio but no video
PROBE_SLOTS = 4                # max parallel ffprobe checks (be gentle to small servers)
HEADERS = {"User-Agent": "Mozilla/5.0"}

_cache, _lock = {}, threading.Lock()
FFPROBE = shutil.which("ffprobe")
_probe_slots = threading.BoundedSemaphore(PROBE_SLOTS)
VIDEO_CODECS = ("avc", "hvc", "hev", "mp4v", "vp0", "vp8", "vp9", "av01", "dvh", "mpeg")


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


def matches(term, block):
    """Match if the term is a substring, or all its words appear in the name (any order, extra words allowed)."""
    text = search_text(block)
    if term in text:
        return True
    return set(re.findall(r"\w+", term)) <= set(re.findall(r"\w+", text))


def tokens(text):
    return re.findall(r"[a-z0-9]+", text.lower())


def raw_name(block):
    return extinf(block).split(",", 1)[-1]


def tvg_id(block):
    m = re.search(r'tvg-id="([^"]*)"', extinf(block))
    return m.group(1) if m else ""


def parse_term(term):
    """'=Discovery' -> exact; '-Turbo' -> exclude word. Returns (exact, words, exclude)."""
    term = term.strip()
    exact = term.startswith("=")
    parts = term.lstrip("=").split()
    exclude = set(tokens(" ".join(p[1:] for p in parts if p.startswith("-"))))
    words = tokens(" ".join(p for p in parts if not p.startswith("-")))
    return exact, words, exclude


def matches(term, block):
    exact, words, exclude = parse_term(term)
    if not words:
        return False
    if exclude & set(tokens(raw_name(block))):
        return False
    if exact:  # whole channel name (tags like (720p) ignored) or tvg-id must equal the term
        return words == tokens(clean_name(block)) or words == tokens(tvg_id(block))
    toks = tokens(search_text(block))
    if set(words) <= set(toks):
        return True
    # spelling without spaces: "AajTak" equals consecutive words "Aaj" + "Tak"
    joined = "".join(words)
    for a in range(len(toks)):
        run = ""
        for b in range(a, len(toks)):
            run += toks[b]
            if run == joined:
                return True
            if len(run) >= len(joined):
                break
    return False


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


def hls_has_video(text):
    """HLS playlist: True = has video, False = audio only, None = can't tell."""
    lines = [l.strip() for l in text.splitlines()]
    variants = [l for l in lines if l.startswith("#EXT-X-STREAM-INF")]
    if variants:
        unknown = False
        for v in variants:
            if "RESOLUTION=" in v.upper():
                return True
            m = re.search(r'CODECS="([^"]*)"', v, re.I)
            if not m:
                unknown = True
                continue
            if any(c.strip().lower().startswith(VIDEO_CODECS) for c in m.group(1).split(",")):
                return True
        return None if unknown else False
    segs = [l for l in lines if l and not l.startswith("#")]
    if segs and all(re.search(r"\.(aac|mp3|m4a|ac3)(\?|$)", x, re.I) for x in segs):
        return False
    return None


def probe(url, seconds):
    """Ask ffprobe what is inside the stream. Returns its lines, or None if it failed."""
    cmd = [FFPROBE, "-v", "error", "-analyzeduration", str(seconds * 1_000_000),
           "-probesize", str(seconds * 2_000_000), "-rw_timeout", str((seconds + 8) * 1_000_000),
           "-user_agent", HEADERS["User-Agent"],
           "-show_entries", "stream=codec_type,codec_name", "-of", "csv=p=0", url]
    with _probe_slots:
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=seconds + 25)
        except Exception:
            return None
    if r.returncode != 0:
        return None
    return [l.strip() for l in r.stdout.splitlines() if l.strip()]


def only_audio(url):
    """Returns what ffprobe saw (e.g. 'aac,audio') if the stream is audio only, else None.
    A quick look first; a stream is only called audio-only if a longer look agrees."""
    if not FFPROBE:
        return None
    seen = None
    for seconds in (3, 12):
        lines = probe(url, seconds)
        if lines is None:
            return None                      # could not read it - keep the stream
        kinds = {t for l in lines for t in l.split(",")}
        if "video" in kinds or "audio" not in kinds:
            return None
        seen = "; ".join(dict.fromkeys(lines))
        if seconds == 3:
            time.sleep(1)                    # let the server free the connection
    return seen


def is_alive(url):
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            if r.status != 200:
                return False
            ctype = r.headers.get("Content-Type", "").lower()
            is_hls = ".m3u8" in url.lower() or "mpegurl" in ctype
            chunk = r.read(65536 if is_hls else 2048)
            if not chunk:
                return False
            if is_hls:
                if b"#EXTM3U" not in chunk:
                    return False
                if CHECK_VIDEO and hls_has_video(chunk.decode("utf-8", "replace")) is False:
                    print(f"AUDIO-ONLY skipped (HLS playlist): {url}")
                    return False
                return True
    except Exception:
        return False
    # not an HLS playlist (e.g. raw TS from Tvheadend): look inside the stream
    if CHECK_VIDEO:
        seen = only_audio(url)
        if seen:
            print(f"AUDIO-ONLY skipped: {url} (ffprobe saw: {seen})")
            return False
    return True


def resolve(args):
    """Sources in priority order; within a source, aliases in the order written.
    Stops at the first working stream."""
    line, sources = args
    nocheck = line.lstrip().startswith("!")          # ! = use without testing
    body = line.lstrip().lstrip("!")
    aliases = [a.strip() for a in body.split("|") if a.strip()]
    for i, src in enumerate(sources):
        entries = get_entries(i, src)
        seen, tries = set(), 0
        for alias in aliases:
            want = parse_term(alias)[1]
            cands = [(b, u) for b, u in entries if matches(alias, b)]
            # exact channel name first, then shorter (closer) names
            cands.sort(key=lambda c: (tokens(clean_name(c[0])) != want,
                                      len(clean_name(c[0]))))
            for block, url in cands:
                if url in seen:
                    continue
                if tries >= MAX_TRIES_PER_SOURCE:
                    break
                seen.add(url)
                tries += 1
                if nocheck or is_alive(url):
                    return line, block, url, i
    return line, None, None, None


def main():
    terms = read_list(WANTED_FILE)
    sources = read_list(SOURCES_FILE)

    with ThreadPoolExecutor(WORKERS) as ex:
        results = list(ex.map(resolve, [(t, sources) for t in terms]))  # keeps channels.txt order

    out, used = [], set()
    for line, block, url, src in results:
        unchecked = line.lstrip().startswith("!")
        term = line.lstrip().lstrip("!").split("|")[0].strip()
        if url is None:
            print(f"DEAD   {term} (nothing worked in any source)")
            continue
        if url in used:
            print(f"SKIP   {term} (same stream already added)")
            continue
        used.add(url)
        note = "" if src == 0 else " [backup source]"
        if unchecked:
            note += " [unchecked]"
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
    
