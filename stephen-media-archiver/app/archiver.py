#!/usr/bin/env python3
"""Media Archiver: moves rarely watched titles from the internal drive to the
WD USB archive through Radarr/Sonarr, keeping Jellyfin watch status.

Standard library only. Runs a nightly job and serves a small status page.
Starts in DRY_RUN mode: it plans and reports, but moves nothing.
"""
import datetime as dt
import glob
import html
import json
import os
import re
import shutil
import sqlite3
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def env(name, default):
    return os.environ.get(name, default)


DRY_RUN = env("DRY_RUN", "true").lower() != "false"
TRIGGER_FREE_GB = float(env("TRIGGER_FREE_GB", "300"))
TARGET_FREE_GB = float(env("TARGET_FREE_GB", "400"))
MAX_GB_PER_RUN = float(env("MAX_GB_PER_RUN", "40"))
ARCHIVE_RESERVE_GB = float(env("ARCHIVE_RESERVE_GB", "150"))
MOVIE_MIN_DAYS = int(env("MOVIE_MIN_DAYS", "60"))
SERIES_MIN_DAYS = int(env("SERIES_MIN_DAYS", "90"))
KEEP_TAG = env("KEEP_TAG", "keep").lower()
RUN_AT = env("RUN_AT", "03:30")
PORT = int(env("PORT", "8080"))
VERSION = "0.2.1"

RADARR_URL = env("RADARR_URL", "http://stephen-radarr_server_1:7878")
SONARR_URL = env("SONARR_URL", "http://stephen-sonarr_server_1:8989")
BAZARR_URL = env("BAZARR_URL", "http://bazarr_server_1:6767")
JELLYFIN_URL = env("JELLYFIN_URL", "http://jellyfin_server_1:8096")

RADARR_CONFIG = "/keys/radarr/config.xml"
SONARR_CONFIG = "/keys/sonarr/config.xml"
BAZARR_CONFIG = "/keys/bazarr/config.yaml"
JELLYFIN_DB = "/keys/jellyfin/jellyfin.db"
JELLYFIN_KEY_NAME = env("JELLYFIN_KEY_NAME", "media-archiver")

# Same folder seen by different apps.
ARR_MAIN_MOVIES, ARR_ARCHIVE_MOVIES = "/downloads/media/movies", "/archive/movies"
ARR_MAIN_TV, ARR_ARCHIVE_TV = "/downloads/media/tv", "/archive/tv"
JF_ARCHIVE_PREFIX = "/media"  # Jellyfin sees Media Archive at /media

DATA_DIR = "/data"
REPORT_DIR = os.path.join(DATA_DIR, "reports")
GB = 1e9

state = {"running": False, "last": None, "next_run": None}
lock = threading.Lock()


# ---------- helpers ----------

def log(report, msg):
    line = f"{dt.datetime.now().strftime('%H:%M:%S')} {msg}"
    print(line, flush=True)
    report["log"].append(line)


def http(method, url, headers=None, body=None, timeout=60):
    data = None
    headers = dict(headers or {})
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        return json.loads(raw) if raw.strip() else None


def read_xml_key(path):
    m = re.search(r"<ApiKey>([^<]+)</ApiKey>", open(path).read())
    if not m:
        raise RuntimeError(f"no ApiKey in {path}")
    return m.group(1).strip()


def read_bazarr_key(path):
    text = open(path).read()
    m = re.search(r"^auth:\s*\n((?:[ \t]+.*\n)+)", text, re.M)
    block = m.group(1) if m else text
    m = re.search(r"apikey:\s*['\"]?([0-9a-zA-Z]{16,})", block)
    if not m:
        raise RuntimeError("no Bazarr apikey found")
    return m.group(1)


def read_jellyfin_key(db_path, name):
    """Copy Jellyfin's DB (with WAL) to tmp and read the named API key."""
    tmp = tempfile.mkdtemp()
    try:
        for f in glob.glob(db_path + "*"):
            shutil.copy(f, os.path.join(tmp, os.path.basename(f)))
        con = sqlite3.connect(os.path.join(tmp, os.path.basename(db_path)))
        tables = [r[0] for r in con.execute("select name from sqlite_master where type='table'")]
        for t in tables:
            if "apikey" not in t.lower():
                continue
            cols = [r[1] for r in con.execute(f'pragma table_info("{t}")')]
            ncol = next((c for c in cols if c.lower() in ("name", "appname")), None)
            kcol = next((c for c in cols if c.lower() in ("accesstoken", "token", "key")), None)
            if ncol and kcol:
                row = con.execute(f'select "{kcol}" from "{t}" where "{ncol}"=?', (name,)).fetchone()
                if row:
                    return row[0]
        raise RuntimeError(f"Jellyfin API key '{name}' not found (tables: {tables})")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


class Arr:
    def __init__(self, url, key):
        self.url, self.h = url.rstrip("/"), {"X-Api-Key": key}

    def get(self, p):
        return http("GET", f"{self.url}/api/v3/{p}", self.h)

    def put(self, p, body):
        return http("PUT", f"{self.url}/api/v3/{p}", self.h, body)

    def post(self, p, body):
        return http("POST", f"{self.url}/api/v3/{p}", self.h, body)

    def free_gb(self, path):
        for d in self.get("diskspace"):
            if d["path"] == path:
                return d["freeSpace"] / GB
        raise RuntimeError(f"{path} not in diskspace")

    def tag_ids(self, label):
        return {t["id"] for t in self.get("tag") if t["label"].lower() == label}


class Jellyfin:
    def __init__(self, url, key):
        self.url = url.rstrip("/")
        self.h = {"Authorization": f'MediaBrowser Token="{key}"'}

    def get(self, p):
        return http("GET", f"{self.url}{p}", self.h, timeout=180)

    def post(self, p, body=None):
        return http("POST", f"{self.url}{p}", self.h, body)

    def users(self):
        return self.get("/Users")

    def items_with_userdata(self, uid):
        q = urllib.parse.urlencode({
            "userId": uid, "Recursive": "true", "IncludeItemTypes": "Movie,Episode",
            "Fields": "Path", "enableUserData": "true",
        })
        try:
            return self.get(f"/Items?{q}")["Items"]
        except urllib.error.HTTPError:
            return self.get(f"/Users/{uid}/Items?{q}")["Items"]

    def set_userdata(self, uid, item_id, ud):
        body = {k: ud.get(k) for k in ("Played", "PlaybackPositionTicks", "PlayCount",
                                        "LastPlayedDate", "IsFavorite") if ud.get(k) is not None}
        try:
            self.post(f"/UserItems/{item_id}/UserData?userId={uid}", body)
        except urllib.error.HTTPError:
            if ud.get("Played"):
                self.post(f"/UserPlayedItems/{item_id}?userId={uid}")


def parse_date(s):
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    s = re.sub(r"\.(\d{6})\d+", r".\1", s)
    try:
        d = dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)


def days_since(d, now):
    return None if d is None else (now - d).days


def jf_path(arr_path):
    """Radarr/Sonarr path -> Jellyfin path."""
    if arr_path.startswith("/archive/"):
        return JF_ARCHIVE_PREFIX + arr_path[len("/archive"):]
    return arr_path


def real_freed_bytes(path):
    """Bytes the internal drive gets back if this file leaves it (0 if hardlinked elsewhere)."""
    try:
        st = os.stat(path)
        return st.st_size if st.st_nlink <= 1 else 0
    except OSError:
        return None


def dir_freed_bytes(path):
    total, unknown = 0, False
    for root, _, files in os.walk(path):
        for f in files:
            b = real_freed_bytes(os.path.join(root, f))
            if b is None:
                unknown = True
            else:
                total += b
    return total, unknown


# ---------- the job ----------

def snapshot_watch(jf, report):
    """path -> list of (user_id, user_name, userdata)"""
    by_path = {}
    for u in jf.users():
        for it in jf.items_with_userdata(u["Id"]):
            p = it.get("Path")
            if p:
                by_path.setdefault(p, []).append((u["Id"], u["Name"], it.get("UserData") or {}))
    log(report, f"Jellyfin: watch data for {len(by_path)} files")
    return by_path


def activity(paths, watch):
    """Latest play date and whether anyone has progress, across the given Jellyfin paths."""
    last, in_progress = None, False
    for p in paths:
        for _, _, ud in watch.get(p, []):
            d = parse_date(ud.get("LastPlayedDate"))
            if d and (last is None or d > last):
                last = d
            if ud.get("PlaybackPositionTicks", 0) > 0 and not ud.get("Played"):
                in_progress = True
    return last, in_progress


def plan(radarr, sonarr, watch, now, report):
    cands = []
    keep_r, keep_s = radarr.tag_ids(KEEP_TAG), sonarr.tag_ids(KEEP_TAG)
    queued_r = {q.get("movieId") for q in radarr.get("queue?pageSize=500").get("records", [])}

    for m in radarr.get("movie"):
        f = m.get("movieFile")
        if not f or not m.get("path", "").startswith(ARR_MAIN_MOVIES):
            continue
        why_not = None
        if keep_r & set(m.get("tags", [])):
            why_not = f"tag '{KEEP_TAG}'"
        elif m["id"] in queued_r:
            why_not = "download in progress"
        added = parse_date(f.get("dateAdded"))
        last, prog = activity([jf_path(f["path"])], watch)
        idle = min(x for x in [days_since(added, now), days_since(last, now)] if x is not None) \
            if (added or last) else 0
        if not why_not and prog:
            why_not = "someone is part-way through"
        if not why_not and idle < MOVIE_MIN_DAYS:
            why_not = f"active {idle} days ago (< {MOVIE_MIN_DAYS})"
        freed = real_freed_bytes(f["path"])
        if not why_not and freed == 0:
            why_not = "torrent copy still on disk (moving frees nothing yet)"
        cands.append({
            "kind": "movie", "id": m["id"], "title": f'{m["title"]} ({m.get("year", "")})',
            "size": f.get("size", 0), "freed": freed if freed is not None else f.get("size", 0),
            "idle_days": idle, "last_played": last.date().isoformat() if last else None,
            "eligible": why_not is None, "why_not": why_not,
        })

    for s in sonarr.get("series"):
        if not s.get("path", "").startswith(ARR_MAIN_TV):
            continue
        st = s.get("statistics", {})
        if not st.get("episodeFileCount"):
            continue
        why_not = None
        if keep_s & set(s.get("tags", [])):
            why_not = f"tag '{KEEP_TAG}'"
        elif s.get("status") != "ended":
            why_not = f"series is {s.get('status')}"
        files = sonarr.get(f"episodefile?seriesId={s['id']}")
        newest = max((parse_date(e.get("dateAdded")) for e in files if e.get("dateAdded")), default=None)
        last, prog = activity([jf_path(e["path"]) for e in files], watch)
        idle_vals = [x for x in [days_since(newest, now), days_since(last, now)] if x is not None]
        idle = min(idle_vals) if idle_vals else 0
        if not why_not and prog and idle < SERIES_MIN_DAYS:
            why_not = "someone is part-way through"
        if not why_not and idle < SERIES_MIN_DAYS:
            why_not = f"active {idle} days ago (< {SERIES_MIN_DAYS})"
        freed, unknown = dir_freed_bytes(s["path"])
        if unknown:
            freed = st.get("sizeOnDisk", 0)
        if not why_not and freed == 0:
            why_not = "torrent copies still on disk (moving frees nothing yet)"
        cands.append({
            "kind": "series", "id": s["id"], "title": s["title"],
            "size": st.get("sizeOnDisk", 0), "freed": freed, "idle_days": idle,
            "last_played": last.date().isoformat() if last else None,
            "eligible": why_not is None, "why_not": why_not,
        })

    cands.sort(key=lambda c: (not c["eligible"], -c["idle_days"]))
    return cands


MOVE_CMDS = ("MoveMovie", "MoveSeries", "BulkMoveMovie", "BulkMoveSeries")


def moves_running(arr):
    return [c for c in arr.get("command") if c.get("status") in ("queued", "started")
            and c.get("name") in MOVE_CMDS]


def wait_moved(arr, kind, cid, old_path, archive_root, report, timeout=12 * 3600):
    """Wait for the move command to finish, then make sure the app's path points at the archive.

    Seen on 2026-11-21 (Chuck): Sonarr moved every file but kept the old series path.
    If that happens and the files are at the destination, set the path without moving."""
    ep = "movie" if kind == "movie" else "series"
    t0 = time.time()
    time.sleep(30)
    while moves_running(arr):
        if time.time() - t0 > timeout:
            raise RuntimeError("move did not finish in time")
        time.sleep(60)
    obj = arr.get(f"{ep}/{cid}")
    if obj.get("path", "").startswith("/archive/"):
        return obj
    dest = archive_root.rstrip("/") + "/" + os.path.basename(old_path.rstrip("/"))
    listing = arr.get("filesystem?includeFiles=true&path=" + urllib.parse.quote(dest + "/"))
    has_files = bool(listing.get("files")) or bool(listing.get("directories"))
    src = arr.get("filesystem?includeFiles=true&path=" + urllib.parse.quote(old_path.rstrip("/") + "/"))
    src_files = len(src.get("files", [])) + sum(
        len(arr.get("filesystem?includeFiles=true&path=" + urllib.parse.quote(d["path"])).get("files", []))
        for d in src.get("directories", []))
    if not has_files or src_files:
        raise RuntimeError(f"move incomplete: dest has files={has_files}, source still has {src_files} files")
    log(report, f"  {ep} path not updated by the app; setting it to {dest} (files already there)")
    obj["path"] = dest
    obj["rootFolderPath"] = archive_root.rstrip("/") + "/"
    arr.put(f"{ep}/{cid}?moveFiles=false", obj)
    arr.post("command", {"name": "RescanMovie", "movieId": cid} if kind == "movie"
             else {"name": "RescanSeries", "seriesId": cid})
    time.sleep(30)
    return arr.get(f"{ep}/{cid}")


def restore_watch(jf, old_new_paths, watch, report):
    """Re-apply each user's userdata to the item at the new path."""
    pending = {new: watch[old] for old, new in old_new_paths if old in watch}
    if not pending:
        return 0
    updates = [{"Path": new, "UpdateType": "Created"} for _, new in old_new_paths] + \
              [{"Path": old, "UpdateType": "Deleted"} for old, _ in old_new_paths]
    try:
        jf.post("/Library/Media/Updated", {"Updates": updates})
    except Exception:
        jf.post("/Library/Refresh")
    restored, t0, refreshed = 0, time.time(), False
    while pending and time.time() - t0 < 1800:
        time.sleep(60)
        users = {uid for v in pending.values() for uid, _, _ in v}
        found = {}
        for uid in users:
            for it in jf.items_with_userdata(uid):
                if it.get("Path") in pending:
                    found.setdefault(it["Path"], {})[uid] = it["Id"]
        for path in list(pending):
            ids = found.get(path)
            if not ids:
                continue
            for uid, uname, ud in pending[path]:
                if uid in ids and (ud.get("Played") or ud.get("PlaybackPositionTicks") or ud.get("IsFavorite")):
                    jf.set_userdata(uid, ids[uid], ud)
                    restored += 1
            del pending[path]
        if pending and not refreshed and time.time() - t0 > 600:
            jf.post("/Library/Refresh")
            refreshed = True
    if pending:
        log(report, f"WARNING: Jellyfin never picked up {len(pending)} files; watch status not restored for them")
    return restored


def bazarr_subs(bazarr_key, cand, report):
    if not bazarr_key:
        return
    h = {"X-API-KEY": bazarr_key}
    if cand["kind"] == "movie":
        url = f"{BAZARR_URL}/api/movies?radarrid={cand['id']}&action=search-missing"
    else:
        url = f"{BAZARR_URL}/api/series?seriesid={cand['id']}&action=search-missing"
    try:
        http("PATCH", url, h, timeout=600)
        log(report, f"  Bazarr: searched missing subtitles")
    except Exception as e:
        log(report, f"  Bazarr: subtitle search failed ({e}); continuing")


def run_job(trigger="schedule"):
    if not lock.acquire(blocking=False):
        return
    state["running"] = True
    now = dt.datetime.now(dt.timezone.utc)
    report = {"started": now.isoformat(), "trigger": trigger, "dry_run": DRY_RUN,
              "log": [], "moved": [], "candidates": [], "error": None}
    try:
        radarr = Arr(RADARR_URL, read_xml_key(RADARR_CONFIG))
        sonarr = Arr(SONARR_URL, read_xml_key(SONARR_CONFIG))
        jf = Jellyfin(JELLYFIN_URL, read_jellyfin_key(JELLYFIN_DB, JELLYFIN_KEY_NAME))
        try:
            bazarr_key = read_bazarr_key(BAZARR_CONFIG)
        except Exception as e:
            bazarr_key = None
            log(report, f"Bazarr key unavailable ({e}); subtitles step skipped")

        free = radarr.free_gb("/downloads")
        arch_free = radarr.free_gb("/archive")
        report.update(internal_free_gb=round(free), archive_free_gb=round(arch_free))
        log(report, f"Internal free {free:.0f} GB (trigger {TRIGGER_FREE_GB:.0f}), archive free {arch_free:.0f} GB")

        if moves_running(radarr) or moves_running(sonarr):
            log(report, "A Radarr/Sonarr move is already running; skipping tonight")
            return
        watch = snapshot_watch(jf, report)
        cands = plan(radarr, sonarr, watch, now, report)
        report["candidates"] = cands
        eligible = [c for c in cands if c["eligible"]]
        log(report, f"{len(eligible)} eligible of {len(cands)} titles checked")

        needed = max(0.0, TARGET_FREE_GB - free) if free < TRIGGER_FREE_GB else 0.0
        if needed == 0:
            log(report, "Free space above trigger: nothing needs to move tonight")
        budget = min(needed, MAX_GB_PER_RUN)
        picked, total = [], 0.0
        for c in eligible:
            if total >= budget:
                break
            if c["size"] / GB > arch_free - ARCHIVE_RESERVE_GB - total:
                log(report, f"Archive too full for {c['title']}; stopping")
                break
            picked.append(c)
            total += c["freed"] / GB
        report["planned_gb"] = round(total, 1)
        for c in picked:
            c["planned"] = True
        if picked:
            log(report, f"Plan: move {len(picked)} titles, about {total:.0f} GB freed")

        for c in picked:
            arr = radarr if c["kind"] == "movie" else sonarr
            if DRY_RUN:
                log(report, f"[dry run] would move {c['kind']} '{c['title']}' ({c['size'] / GB:.1f} GB)")
                continue
            log(report, f"Moving {c['kind']} '{c['title']}' ({c['size'] / GB:.1f} GB)")
            bazarr_subs(bazarr_key, c, report)
            if c["kind"] == "movie":
                before = radarr.get(f"moviefile?movieId={c['id']}")
                old_paths = [jf_path(f["path"]) for f in before]
                old_dir = radarr.get(f"movie/{c['id']}")["path"]
                radarr.put("movie/editor", {"movieIds": [c["id"]], "rootFolderPath": ARR_ARCHIVE_MOVIES,
                                            "moveFiles": True})
                wait_moved(radarr, "movie", c["id"], old_dir, ARR_ARCHIVE_MOVIES, report)
                after = radarr.get(f"moviefile?movieId={c['id']}")
                new_paths = [jf_path(f["path"]) for f in after]
            else:
                before = sonarr.get(f"episodefile?seriesId={c['id']}")
                ids_old = {f["id"]: jf_path(f["path"]) for f in before}
                old_dir = sonarr.get(f"series/{c['id']}")["path"]
                sonarr.put("series/editor", {"seriesIds": [c["id"]], "rootFolderPath": ARR_ARCHIVE_TV,
                                             "moveFiles": True})
                wait_moved(sonarr, "series", c["id"], old_dir, ARR_ARCHIVE_TV, report)
                after = sonarr.get(f"episodefile?seriesId={c['id']}")
                ids_new = {f["id"]: jf_path(f["path"]) for f in after}
                old_paths = [ids_old[i] for i in ids_old if i in ids_new]
                new_paths = [ids_new[i] for i in ids_old if i in ids_new]
            pairs = list(zip(old_paths, new_paths))
            n = restore_watch(jf, pairs, watch, report)
            log(report, f"  moved; restored {n} watch-status entries")
            report["moved"].append({"title": c["title"], "kind": c["kind"], "gb": round(c["size"] / GB, 1)})

        report["internal_free_after_gb"] = round(radarr.free_gb("/downloads"))
    except Exception as e:
        report["error"] = f"{e}"
        log(report, "ERROR: " + traceback.format_exc().strip().splitlines()[-1])
        traceback.print_exc()
    finally:
        report["finished"] = dt.datetime.now(dt.timezone.utc).isoformat()
        os.makedirs(REPORT_DIR, exist_ok=True)
        name = report["started"][:19].replace(":", "-")
        with open(os.path.join(REPORT_DIR, f"{name}.json"), "w") as fh:
            json.dump(report, fh, indent=1, default=str)
        for old in sorted(os.listdir(REPORT_DIR))[:-60]:
            os.remove(os.path.join(REPORT_DIR, old))
        state["last"], state["running"] = report, False
        lock.release()


def next_run_time():
    hh, mm = (int(x) for x in RUN_AT.split(":"))
    now = dt.datetime.now()
    t = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    return t if t > now else t + dt.timedelta(days=1)


def scheduler():
    while True:
        t = next_run_time()
        state["next_run"] = t.isoformat(timespec="minutes")
        time.sleep(max(1, (t - dt.datetime.now()).total_seconds()))
        run_job("schedule")


# ---------- metrics for Home Assistant ----------

_metrics_cache = {"t": 0, "data": None}


def collect_metrics():
    if _metrics_cache["data"] and time.time() - _metrics_cache["t"] < 60:
        return _metrics_cache["data"]
    m = {"ok": True, "error": ""}
    try:
        radarr = Arr(RADARR_URL, read_xml_key(RADARR_CONFIG))
        sonarr = Arr(SONARR_URL, read_xml_key(SONARR_CONFIG))
        for d in radarr.get("diskspace"):
            key = {"/downloads": "internal", "/archive": "archive"}.get(d["path"])
            if key:
                tot = d["totalSpace"] or 1
                m[f"{key}_free_gb"] = round(d["freeSpace"] / GB)
                m[f"{key}_total_gb"] = round(tot / GB)
                m[f"{key}_used_pct"] = round(100 * (1 - d["freeSpace"] / tot), 1)
        for name, arr in (("sonarr", sonarr), ("radarr", radarr)):
            health = arr.get("health")
            m[f"{name}_warnings"] = len(health)
            m[f"{name}_warning_text"] = "; ".join(h.get("message", "")[:120] for h in health)[:250] or "none"
            q = arr.get("queue?pageSize=500")
            recs = q.get("records", [])
            m[f"{name}_queue"] = q.get("totalRecords", len(recs))
            m[f"{name}_queue_stuck"] = sum(1 for r in recs if r.get("trackedDownloadStatus") in ("warning", "error"))
        m["moves_running"] = len(moves_running(radarr)) + len(moves_running(sonarr))
    except Exception as e:
        m["ok"], m["error"] = False, str(e)[:200]
    last = state.get("last") or {}
    m["dry_run"] = DRY_RUN
    m["last_run"] = (last.get("started") or "")[:16].replace("T", " ")
    m["last_run_error"] = last.get("error") or "none"
    m["last_run_moved"] = len(last.get("moved", []))
    m["last_run_eligible"] = sum(1 for c in last.get("candidates", []) if c.get("eligible"))
    _metrics_cache.update(t=time.time(), data=m)
    return m


def render_metrics():
    m = collect_metrics()
    rows = "".join(f'<tr><td>{html.escape(k)}</td><td id="{html.escape(k)}">{html.escape(str(v))}</td></tr>'
                   for k, v in m.items())
    return f"<!doctype html><html><head><meta charset='utf-8'><title>metrics</title></head><body><table>{rows}</table></body></html>"


# ---------- status page ----------

def gb(b):
    return f"{(b or 0) / GB:.1f}"


def render():
    r = state["last"]
    mode = "DRY RUN (plans only, moves nothing)" if DRY_RUN else "LIVE (moves files)"
    out = [f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Media Archiver</title><style>
body{{font:15px system-ui,sans-serif;margin:16px;max-width:980px;color:#222}}
table{{border-collapse:collapse;width:100%;font-size:13px}}td,th{{border-bottom:1px solid #ddd;padding:4px 6px;text-align:left}}
.ok{{color:#176c2e}}.no{{color:#888}}.err{{color:#b00020}}pre{{background:#f4f4f4;padding:8px;white-space:pre-wrap;font-size:12px}}
button{{font-size:15px;padding:6px 12px}}</style></head><body>
<h2>Media Archiver <small style="color:#888">v{VERSION}</small></h2><p><b>Mode:</b> {mode}<br><b>Rules:</b> act when internal free &lt; {TRIGGER_FREE_GB:.0f} GB, free up to {TARGET_FREE_GB:.0f} GB,
max {MAX_GB_PER_RUN:.0f} GB/night; movies idle &ge; {MOVIE_MIN_DAYS} days; ended series idle &ge; {SERIES_MIN_DAYS} days;
tag <code>{html.escape(KEEP_TAG)}</code> in Radarr/Sonarr = never archive.<br>
<b>Next scheduled run:</b> {state.get('next_run') or '-'} {'<b>(running now)</b>' if state['running'] else ''}</p>
<form method="post" action="run"><button>Run now{' (dry run)' if DRY_RUN else ''}</button></form>"""]
    if not r:
        out.append("<p>No run yet.</p>")
    else:
        err = f'<p class="err"><b>Error:</b> {html.escape(r["error"])}</p>' if r.get("error") else ""
        out.append(f"""<h3>Last run</h3><p>{html.escape(r['started'][:16].replace('T', ' '))} UTC ({html.escape(r['trigger'])}),
internal free {r.get('internal_free_gb', '?')} GB, archive free {r.get('archive_free_gb', '?')} GB,
planned {r.get('planned_gb', 0)} GB, moved {len(r.get('moved', []))} titles.</p>{err}""")
        out.append("<table><tr><th>Title</th><th>Type</th><th>Size GB</th><th>Frees GB</th><th>Idle days</th><th>Last played</th><th>Status</th></tr>")
        for c in r.get("candidates", []):
            status = ("<b>planned</b>" if c.get("planned") else "eligible") if c["eligible"] \
                else html.escape(c.get("why_not") or "")
            cls = "ok" if c["eligible"] else "no"
            out.append(f"<tr class='{cls}'><td>{html.escape(c['title'])}</td><td>{c['kind']}</td><td>{gb(c['size'])}</td>"
                       f"<td>{gb(c['freed'])}</td><td>{c['idle_days']}</td><td>{c['last_played'] or '-'}</td><td>{status}</td></tr>")
        out.append("</table><h3>Log</h3><pre>" + html.escape("\n".join(r.get("log", []))) + "</pre>")
    out.append("</body></html>")
    return "".join(out)


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        b = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        self.path = self.path.split("?", 1)[0]
        if self.path.rstrip("/").endswith("/api/status"):
            self._send(200, json.dumps({"dry_run": DRY_RUN, "running": state["running"],
                                        "next_run": state["next_run"], "last": state["last"]}, default=str),
                       "application/json")
        elif self.path.rstrip("/").endswith("/api/metrics"):
            self._send(200, json.dumps(collect_metrics()), "application/json")
        elif self.path.rstrip("/").endswith("/metrics"):
            self._send(200, render_metrics())
        elif self.path.rstrip("/").endswith("/health"):
            self._send(200, "OK", "text/plain")
        else:
            self._send(200, render())

    def do_POST(self):
        self.path = self.path.split("?", 1)[0]
        if self.path.rstrip("/").endswith("run"):
            threading.Thread(target=run_job, args=("manual",), daemon=True).start()
            time.sleep(1)
            self.send_response(303)
            self.send_header("Location", "./")
            self.end_headers()
        else:
            self._send(404, "not found", "text/plain")

    def log_message(self, *a):
        pass


def load_last():
    try:
        files = sorted(os.listdir(REPORT_DIR))
        if files:
            state["last"] = json.load(open(os.path.join(REPORT_DIR, files[-1])))
    except Exception:
        pass


if __name__ == "__main__":
    os.makedirs(REPORT_DIR, exist_ok=True)
    load_last()
    threading.Thread(target=scheduler, daemon=True).start()
    print(f"Media Archiver on :{PORT}, dry_run={DRY_RUN}, run at {RUN_AT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
