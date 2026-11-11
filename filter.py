import re
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin

SOURCES_FILE = "sources.txt"   # playlist URLs, highest priority first
WANTED_FILE = "channels.txt"   # one line = one channel; separate alternative names with |
OUTPUT = "playlist.m3u"
TIMEOUT = 10                   # seconds per stream check
WORKERS = 20
MAX_TRIES_PER_SOURCE = 8       # max candidates tested per channel per source
SNIFF_SLOTS = 4                # max parallel stream inspections (be gentle to small servers)
SNIFF_BYTES = 786432           # give up looking for the stream table after this many bytes
SNIFF_SECONDS = 8              # ...or after this many seconds
HEADERS = {"User-Agent": "Mozilla/5.0"}

# ---- Codec filter (modular) --------------------------------------------
# Set FILTER_CODECS = False to switch the filter off completely.
FILTER_CODECS = False
# Video codecs your player cannot decode. Add more names if needed (e.g. "hevc", "mpeg1video").
BLOCKED_VIDEO_CODECS = {"mpeg2video"}
# -------------------------------------------------------------------------

_cache, _lock = {}, threading.Lock()
_sniff_slots = threading.BoundedSemaphore(SNIFF_SLOTS)

# MPEG-TS stream types (from the stream table at the start of every TS stream)
TS_VIDEO = {0x01: "mpeg1video", 0x02: "mpeg2video", 0x10: "mpeg4video",
            0x1B: "h264", 0x20: "h264", 0x24: "hevc", 0x27: "hevc",
            0x42: "cavs", 0xD1: "dirac", 0xEA: "vc1"}


def read_list(path):
    with open(path, encoding="utf-8") as f:
        return [l.strip() for l in f if l.strip() and not l.startswith("#")]


def fetch(url, timeout=60):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as r:
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
    tvg = m.group(1) if m else ""
    return f"{line.split(',', 1)[-1]} {tvg}".lower()


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


# ---- Stream inspection (pure Python, replaces ffprobe) -------------------
def _ts_sync(buf):
    """Offset of the first 188-byte TS packet boundary, or -1."""
    for i in range(min(188, len(buf) - 376)):
        if buf[i] == 0x47 and buf[i + 188] == 0x47 and buf[i + 376] == 0x47:
            return i
    return -1


def parse_ts(buf):
    """Read the stream table (PAT -> PMT) from MPEG-TS bytes.
    Returns {"video": set_of_codec_names} when found,
    "more" if the table is not in the data yet, "no" if this is not MPEG-TS."""
    if len(buf) < 188 * 3 + 1:
        return "more"
    start = _ts_sync(buf)
    if start < 0:
        return "no"
    pmt_pid = None
    sections = {}                                   # pid -> section being assembled
    for pos in range(start, len(buf) - 187, 188):
        pkt = buf[pos:pos + 188]
        if pkt[0] != 0x47:
            continue
        pid = ((pkt[1] & 0x1F) << 8) | pkt[2]
        wanted = (pid == 0) if pmt_pid is None else (pid == pmt_pid)
        if not wanted:
            continue
        afc = (pkt[3] >> 4) & 3
        if not afc & 1:
            continue
        off = 4 + (1 + pkt[4] if afc & 2 else 0)
        if off >= 188:
            continue
        payload = pkt[off:]
        if pkt[1] & 0x40:                           # a new section starts here
            ptr = payload[0]
            sections[pid] = bytearray(payload[1 + ptr:])
        elif pid in sections:
            sections[pid] += payload
        else:
            continue
        sec = sections[pid]
        if len(sec) < 3:
            continue
        length = ((sec[1] & 0x0F) << 8) | sec[2]
        if len(sec) < 3 + length:
            continue                                # section continues in the next packet
        if pid == 0 and sec[0] == 0x00:             # PAT: find the first program's PMT
            body = sec[8:3 + length - 4]
            for k in range(0, len(body) - 3, 4):
                if (body[k] << 8) | body[k + 1] != 0:
                    pmt_pid = ((body[k + 2] & 0x1F) << 8) | body[k + 3]
                    break
            sections.pop(pid, None)
        elif pid == pmt_pid and sec[0] == 0x02:     # PMT: list of streams
            i = 12 + (((sec[10] & 0x0F) << 8) | sec[11])
            end = 3 + length - 4
            video = set()
            while i + 5 <= end:
                st = sec[i]
                if st in TS_VIDEO:
                    video.add(TS_VIDEO[st])
                i += 5 + (((sec[i + 3] & 0x0F) << 8) | sec[i + 4])
            return {"video": video}
        else:
            sections.pop(pid, None)
    return "more"


def sniff_ts(read, first=b""):
    """Read a stream until its stream table shows up.
    Returns {"video": set}, or None if unknown (stream is then kept)."""
    buf = bytearray(first)
    deadline = time.time() + SNIFF_SECONDS
    try:
        with _sniff_slots:
            while True:
                res = parse_ts(bytes(buf))
                if res == "no":
                    return None
                if res != "more":
                    return res
                if len(buf) >= SNIFF_BYTES or time.time() > deadline:
                    return None
                chunk = read(16384)
                if not chunk:
                    return None
                buf += chunk
    except Exception:
        return None


def hls_segment_url(url, text, depth=0):
    """Follow an HLS playlist (master -> media) down to its newest segment URL, or None."""
    lines = [l.strip() for l in text.splitlines()]
    if any(l.startswith("#EXT-X-MAP") for l in lines):
        return None                                 # fMP4/CMAF segments, not MPEG-TS
    for n, l in enumerate(lines):
        if l.startswith("#EXT-X-STREAM-INF"):       # master playlist: take first variant
            nxt = next((x for x in lines[n + 1:] if x and not x.startswith("#")), None)
            if not nxt or depth >= 2:
                return None
            sub = urljoin(url, nxt)
            return hls_segment_url(sub, fetch(sub, TIMEOUT), depth + 1)
    segs = [l for l in lines if l and not l.startswith("#")]
    return urljoin(url, segs[-1]) if segs else None


def sniff_hls(url):
    """Look inside the newest segment of an HLS stream."""
    try:
        seg = hls_segment_url(url, fetch(url, TIMEOUT))
        if not seg:
            return None
        req = urllib.request.Request(seg, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return sniff_ts(r.read)
    except Exception:
        return None


# ---- Codec filter function (modular) -------------------------------------
def codec_ok(url, info):
    """False if every video stream in `info` uses a blocked codec."""
    if not FILTER_CODECS:
        return True
    video = info["video"]
    if video and video <= BLOCKED_VIDEO_CODECS:
        print(f"BLOCKED CODEC skipped ({', '.join(sorted(video))}): {url}")
        return False
    return True
# -------------------------------------------------------------------------


def is_alive(url):
    inspect = FILTER_CODECS                         # only look inside streams if the filter is on
    is_hls, info = False, None
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
            elif inspect:
                # raw TS (e.g. Tvheadend): keep reading the same connection
                info = sniff_ts(r.read, chunk)
    except Exception:
        return False

    if is_hls and inspect:
        info = sniff_hls(url)

    if info:                                        # None = could not tell -> keep stream
        if not codec_ok(url, info):                 # <-- only call into the codec filter
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
