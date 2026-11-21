"""
nvr.py -- one interface over Hikvision and Dahua NVRs

NVRs are listed in nvrs.env (next to this file, gitignored) -- see nvrs.env.example.
A camera is "<NVR name>:<camera no>", e.g. home-hik:15, where the number is the one
shown on the NVR / iVMS / SmartPSS screen (starting at 1); it is mapped to each SDK's
own channel number here (Hikvision: the first IP camera is usually 33, Dahua: 0-based).
On a Hik DVR with analog inputs, an analog camera is written a<N>, e.g. dvr:a1
(a plain number is still an IP camera: dvr:1 = D1).

    from nvr import open_camera
    with open_camera("home-hik:15") as cam:
        cam.download(start_dt, end_dt, "chunk.mp4")   # True / False / None (no recording)
        cam.rtsp_url()                                 # live main stream (holds the password -- never print)

        cam.snapshot(t, "x.jpg")                       # still from recorded footage at t (RTSP, no SDK login)
        cam.snapshots(times, "out/")                   # many at once, skipping ones already there

Try it against a real NVR:
    python nvr.py test home-hik:15 --minutes 1
    python nvr.py snapshot home-hik:15 "2026-10-09 04:00" x.jpg --precision exact
    python nvr.py snapshots home-hik:15 --days-back 7 --every 1h    # -> ./output/
"""

import csv
import os
import random
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

HERE = Path(__file__).resolve().parent
NVRS_ENV = Path(os.environ.get("NVRS_ENV", HERE / "nvrs.env"))

DEFAULT_PORTS = {"hik": 8000, "dahua": 37777}


def _read_env(path):
    out = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def load_nvrs(path=NVRS_ENV):
    """{name: {vendor, ip, port, user, password, rtsp_port, max_connections}} from nvrs.env.
    Format: NVR_<NAME>_<KEY>=... (the name home-hik is written HOME_HIK)"""
    if not Path(path).is_file():
        raise SystemExit(f"{path} not found -- copy nvrs.env.example to nvrs.env and fill in IP/user/password")
    env = _read_env(path)
    names = {k[4:-len("_VENDOR")] for k in env if k.startswith("NVR_") and k.endswith("_VENDOR")}
    nvrs = {}
    for key in names:
        get = lambda f, d=None: env.get(f"NVR_{key}_{f}", d)  # noqa: E731
        vendor = get("VENDOR").lower()
        if vendor not in DEFAULT_PORTS:
            raise SystemExit(f"NVR_{key}_VENDOR={vendor}: must be hik or dahua")
        max_conn = get("MAX_CONNECTIONS")
        nvrs[key.lower().replace("_", "-")] = {
            "vendor": vendor, "ip": get("IP"), "user": get("USER"), "password": get("PASSWORD"),
            "port": int(get("PORT", DEFAULT_PORTS[vendor])), "rtsp_port": int(get("RTSP_PORT", 554)),
            # cap on concurrent downloads for this NVR, whatever the caller asks; None = no cap
            "max_connections": int(max_conn) if max_conn else None,
        }
    return nvrs


def parse_cameras(spec):
    """"home-hik:15,shop-dahua:1,dvr:a2" -> [("home-hik", 15), ("shop-dahua", 1), ("dvr", "a2")]
    "a<N>" = analog input N of a Hik DVR (kept as the string "aN"); a plain number = IP camera."""
    out = []
    for part in spec.split(","):
        name, _, num = part.strip().rpartition(":")
        num = num.lower()
        digits = num[1:] if num.startswith("a") else num
        if not name or not digits.isdigit() or int(digits) < 1:
            raise SystemExit(f"--camera '{part}': use <NVR name>:<camera no>, e.g. home-hik:15 "
                             f"(an analog input of a Hik DVR: dvr:a1)")
        out.append((name.lower(), f"a{int(digits)}" if num.startswith("a") else int(digits)))
    return out


def split_camera_no(camera_no):
    """15 -> (False, 15) IP camera; "a3" -> (True, 3) analog input."""
    if isinstance(camera_no, str) and camera_no.lower().startswith("a"):
        return True, int(camera_no[1:])
    return False, int(camera_no)


# --------------------------------------------------------------------------
# One login per NVR, shared by every camera on it (both SDKs allow several
# concurrent downloads per login); a semaphore caps concurrent downloads per
# NVR because too many at once gives transient errors (Hik error 10).
# --------------------------------------------------------------------------

_hik_sdk = None      # NET_DVR_Init/Cleanup are process-wide: one per process, refcounted
_hik_users = 0


def _hik_acquire():
    global _hik_sdk, _hik_users
    import hik_sdk
    if _hik_sdk is None:
        sdk = hik_sdk.load_sdk()
        if not hik_sdk.net_init(sdk):
            raise RuntimeError("NET_DVR_Init failed")
        _hik_sdk = sdk
    _hik_users += 1
    return _hik_sdk


def _hik_release():
    global _hik_sdk, _hik_users
    import hik_sdk
    _hik_users -= 1
    if _hik_users == 0 and _hik_sdk is not None:
        hik_sdk.net_cleanup(_hik_sdk)
        _hik_sdk = None


class _Session:
    def __init__(self, name, cfg, max_connections):
        self.name, self.cfg = name, cfg
        self.max_connections = max_connections
        self.slots = threading.BoundedSemaphore(max_connections)
        self.refs = 0
        # The SDK login is lazy: RTSP (rtsp_url / snapshot) needs no SDK, no DLLs and
        # not even the SDK port open, so only download*() pays for it.
        self.mod = None
        self._login_lock = threading.Lock()
        self.chan_num = 0                # analog inputs (Hik DVR only), known after login

    def login(self):
        """Log in to the vendor SDK if not done yet. Raises RuntimeError on failure."""
        with self._login_lock:
            if self.mod is not None:
                return
            cfg, name = self.cfg, self.name
            if cfg["vendor"] == "hik":
                import hik_sdk
                self.sdk = _hik_acquire()
                self.uid, start_dchan, start_chan, chan_num = hik_sdk.login_ex(
                    self.sdk, cfg["ip"], cfg["port"], cfg["user"], cfg["password"])
                if self.uid < 0:
                    err = hik_sdk.last_error(self.sdk)
                    _hik_release()
                    raise RuntimeError(f"{name}: login failed (HCNetSDK error={err})")
                self.first_channel = start_dchan or start_chan or 1
                # Analog inputs ("aN") are SDK channels start_chan.. on a hybrid DVR
                self.start_chan, self.chan_num = start_chan, chan_num
                self.mod = hik_sdk
            else:
                import dahua_sdk
                dahua_sdk.init()
                self.uid, info = dahua_sdk.login(cfg["ip"], cfg["port"], cfg["user"], cfg["password"])
                if not self.uid:
                    raise RuntimeError(f"{name}: login failed ({info})")
                self.first_channel = 0
                self._kf_lock = threading.Lock()
                self._kf_free, self._kf_all = [], []   # extra logins for download_keyframes
                self.mod = dahua_sdk

    def sdk_channel(self, camera_no):
        self.login()
        analog, n = split_camera_no(camera_no)
        if analog:
            if not 1 <= n <= self.chan_num:
                raise RuntimeError(f"{self.name}: no analog camera a{n} "
                                   f"(this device has {self.chan_num} analog inputs)")
            return self.start_chan + n - 1
        return self.first_channel + n - 1

    def download(self, camera_no, start, end, dst, progress_cb=None, timeout=300):
        ch = self.sdk_channel(camera_no)
        with self.slots:
            if self.cfg["vendor"] == "hik":
                return self.mod.download(self.sdk, self.uid, ch, start, end, dst,
                                         progress_cb=progress_cb, timeout=timeout)
            return self.mod.download(self.uid, ch, start, end, dst,
                                     progress_cb=progress_cb, timeout=timeout)

    @property
    def supports_keyframes(self):
        return self.cfg["vendor"] == "dahua"

    def download_keyframes(self, camera_no, start, end, dst, progress_cb=None, timeout=300):
        if not self.supports_keyframes:
            raise NotImplementedError(f"{self.name}: keyframe-only fetch is Dahua only")
        self.login()
        with self.slots:
            # each concurrent fetch gets its own login: the NVR serves keyframe
            # requests one at a time per login (dahua_sdk.download_keyframes)
            uid = self._kf_login()
            try:
                return self.mod.download_keyframes(uid, self.sdk_channel(camera_no), start, end, dst,
                                                   progress_cb=progress_cb, timeout=timeout)
            finally:
                self._kf_free.append(uid)

    def _kf_login(self):
        with self._kf_lock:
            if self._kf_free:
                return self._kf_free.pop()
            uid, info = self.mod.login(self.cfg["ip"], self.cfg["port"], self.cfg["user"], self.cfg["password"])
            if not uid:
                raise RuntimeError(f"{self.name}: extra login for keyframes failed ({info})")
            self._kf_all.append(uid)
            return uid

    def list_cameras(self):
        """[(camera_no, name)] for every camera the device has, camera_no in the same form
        Camera() takes: "aN" for a Hik DVR's analog inputs, a plain number otherwise."""
        self.login()
        if self.cfg["vendor"] == "dahua":
            return [(i + 1, name) for i, name in enumerate(self.mod.channel_names(self.uid))]
        out = []
        for n in range(1, self.chan_num + 1):
            name = self.mod.channel_name(self.sdk, self.uid, self.start_chan + n - 1)
            if name is not None:
                out.append((f"a{n}", name))
        if self.chan_num and self.first_channel == self.start_chan:
            return out                   # analog-only DVR: no IP channels after the analog ones
        for n in range(1, 513):          # IP channels run until the first missing one
            name = self.mod.channel_name(self.sdk, self.uid, self.first_channel + n - 1)
            if name is None:
                break
            out.append((n, name))
        return out

    def last_error(self):
        if self.mod is None:
            return None
        if self.cfg["vendor"] == "hik":
            return self.mod.last_error(self.sdk)
        return self.mod.last_error()

    def close(self):
        if self.mod is None:
            return
        if self.cfg["vendor"] == "hik":
            self.mod.logout(self.sdk, self.uid)
            _hik_release()
        else:
            for uid in self._kf_all:
                self.mod.logout(uid)
            self.mod.logout(self.uid)


# --------------------------------------------------------------------------
# Snapshot: one still picture from *recorded* footage at a wall-clock time,
# pulled over RTSP playback (no SDK login). See CONTEXT.md for the three
# Precisions. Both methods come from an earlier tool that used them for months
# on a Hikvision NVR. Dahua's /cam/playback URL is NOT yet verified on a real NVR.
# --------------------------------------------------------------------------

PRECISIONS = ("exact", "keyframe", "hour")
SNAPSHOT_INDEX = "snapshots.csv"   # file,requested,actual per picture written by snapshots()

_SNAP_MIN_BYTES = 10_000         # smaller = ffmpeg wrote a broken/empty picture
_EXACT_BEFORE_S = 20             # exact: start this early so a whole GOP decodes before t
                                 # (assumes the stream starts at the requested time -- it
                                 # doesn't, see snapshot()'s docstring)
_EXACT_AFTER_S = 5
_HOUR_TRIES = 10                 # hour: minutes tried within the hour, t itself first
_RETRY_DELAY_S = 2               # NVR overload errors usually clear after a short wait


def _mostly_blank(path, white=240, frac=0.5):
    """True if more than `frac` of the picture is near-white.

    Seen in practice (2026-08-08): ffmpeg sometimes returns a valid JPEG
    decoded from a keyframe whose data was cut short -- one corner real, the rest
    empty white macroblocks. Return code and file size can't tell; only pixels can.
    Rare (none in a random 200), but the check costs ~10-20ms vs ~1-5s per grab.
    Skipped (False) when OpenCV isn't installed."""
    try:
        import cv2
    except ImportError:
        return False
    img = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    if img is None:
        return True
    return bool((img > white).mean() > frac)


def _grab_frame(url, dst, seek_s=None, timeout=30):
    """Run ffmpeg once: first keyframe of the stream (seek_s None) or the frame
    seek_s seconds in (decoding forward). Writes dst; returns an error string or ''."""
    cmd = ["ffmpeg", "-v", "error", "-rtsp_transport", "tcp", "-timeout", "5000000"]
    if seek_s is None:
        cmd += ["-skip_frame", "nokey", "-t", "10", "-i", url]
    else:
        cmd += ["-t", str(seek_s + _EXACT_AFTER_S + 2), "-i", url, "-ss", str(seek_s)]
    cmd += ["-frames:v", "1", "-an", "-q:v", "2", "-c:v", "mjpeg", "-f", "image2",
            "-update", "1", "-y", str(dst)]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return f"ffmpeg timeout ({timeout}s)"
    if r.returncode != 0 or not dst.exists() or dst.stat().st_size < _SNAP_MIN_BYTES:
        # stderr can echo the URL, which holds the password: cut everything from rtsp:// on
        tail = r.stderr.decode(errors="replace").strip().splitlines()[-1:] or [f"rc={r.returncode}"]
        return "no picture: " + tail[0].split("rtsp://")[0][:200]
    if _mostly_blank(dst):
        return "partial-decode picture (mostly blank)"
    return ""


_sessions = {}
_sessions_lock = threading.Lock()


class Camera:
    """One camera on one NVR. Opening several Cameras on the same NVR shares one login."""

    def __init__(self, nvr_name, camera_no, max_connections=4, nvrs=None):
        nvrs = nvrs if nvrs is not None else load_nvrs()
        if nvr_name not in nvrs:
            raise SystemExit(f"No NVR named '{nvr_name}' in {NVRS_ENV} (have: {', '.join(sorted(nvrs)) or '-'})")
        self.nvr_name, self.camera_no = nvr_name, camera_no
        self.cfg = nvrs[nvr_name]
        if self.cfg.get("max_connections"):
            max_connections = min(max_connections, self.cfg["max_connections"])
        with _sessions_lock:
            sess = _sessions.get(nvr_name)
            if sess is None:
                sess = _sessions[nvr_name] = _Session(nvr_name, self.cfg, max_connections)
            sess.refs += 1
        self.session = sess
        self._snap = threading.local()   # per-thread last_snapshot_error

    @property
    def max_connections(self):
        """Concurrent downloads allowed on this camera's NVR (after the nvrs.env cap)."""
        return self.session.max_connections

    @property
    def id(self):
        """Stable id used for folder names: home-hik_ch15"""
        return f"{self.nvr_name}_ch{self.camera_no}"

    def download(self, start, end, dst, progress_cb=None, timeout=300):
        """True = done, False = failed, None = no recording in this range."""
        return self.session.download(self.camera_no, start, end, dst, progress_cb, timeout)

    @property
    def supports_keyframes(self):
        """True if download_keyframes() works for this vendor (Dahua only)."""
        return self.session.supports_keyframes

    def download_keyframes(self, start, end, dst, progress_cb=None, timeout=300):
        """Like download(), but only the I-frames (Dahua .dav). The first frame is the
        first keyframe at or after start -- read its time with dahua_sdk.dhav_time()."""
        return self.session.download_keyframes(self.camera_no, start, end, dst, progress_cb, timeout)

    def last_error(self):
        return self.session.last_error()

    @property
    def _rtsp_ch(self):
        """RTSP channel id. Hik analog aN -> N. Hik IP camera N -> N on a pure NVR, but
        start_dchan+N-1 (33 = D1) on a DVR with analog inputs -- only known after an SDK
        login, so without one an IP camera on such a DVR gets the pure-NVR number."""
        analog, n = split_camera_no(self.camera_no)
        s = self.session
        if not analog and s.chan_num:
            return s.first_channel + n - 1
        return n

    def rtsp_url(self, main=True):
        c = self.cfg
        auth = f"{quote(c['user'], safe='')}:{quote(c['password'], safe='')}@"
        host = f"{c['ip']}:{c['rtsp_port']}"
        if c["vendor"] == "hik":
            return f"rtsp://{auth}{host}/Streaming/Channels/{self._rtsp_ch}0{1 if main else 2}"
        return f"rtsp://{auth}{host}/cam/realmonitor?channel={self._rtsp_ch}&subtype={0 if main else 1}"

    @property
    def last_snapshot_error(self):
        """Why this thread's last snapshot() returned None ('' after a success)."""
        return getattr(self._snap, "err", "")

    def login(self):
        """Log in to the SDK now instead of at the first download -- to fail early on a
        bad password / unreachable SDK port. snapshot() and rtsp_url() never need it."""
        self.session.sdk_channel(self.camera_no)   # logs in, and rejects an analog input the DVR lacks
        return self

    def playback_url(self, start, end, main=True):
        """RTSP URL for *recorded* footage start->end (contains the password -- never print)."""
        c = self.cfg
        auth = f"{quote(c['user'], safe='')}:{quote(c['password'], safe='')}@"
        host = f"{c['ip']}:{c['rtsp_port']}"
        if c["vendor"] == "hik":
            # the NVR reads these as local time despite the Z
            f = "%Y%m%dT%H%M%SZ"
            return (f"rtsp://{auth}{host}/Streaming/tracks/{self._rtsp_ch}0{1 if main else 2}"
                    f"?starttime={start:{f}}&endtime={end:{f}}")
        f = "%Y_%m_%d_%H_%M_%S"
        return (f"rtsp://{auth}{host}/cam/playback?channel={self._rtsp_ch}&subtype={0 if main else 1}"
                f"&starttime={start:{f}}&endtime={end:{f}}")

    def snapshot(self, t, dst, precision="keyframe", retries=2):
        """Save one picture from recorded footage at local time t to dst (.jpg).

        precision:
          "exact"    -- meant to be the frame at t (decodes forward from 20s before t;
                        slower), but measured 2026-10-09 it is still off by up to one GOP
                        (~2s): the NVR starts the stream at a keyframe, not at the requested
                        second (Hik: the keyframe before it, Dahua: the one after it), and
                        RTSP doesn't say which. Kept so callers needn't change once fixed.
          "keyframe" -- a keyframe near t, within one GOP (~2s) (default)
          "hour"     -- any picture in t .. t+59min: t first, then other minutes
                        later in the hour until one works (up to 10 tries)
        retries: extra attempts at the same time after a failure (exact/keyframe);
                 "hour" moves on to another minute instead.

        Returns the time actually requested from the NVR (t, or the minute "hour"
        landed on), or None if no picture could be had -- the reason is then in
        self.last_snapshot_error. Never asks for a time in the future.
        """
        if precision not in PRECISIONS:
            raise ValueError(f"precision must be one of {PRECISIONS}, not {precision!r}")
        dst = Path(dst)
        if precision == "hour":
            later = sorted(random.sample(range(1, 60), _HOUR_TRIES - 1))
            plan = [t] + [t + timedelta(minutes=m) for m in later]
            attempts = 1
        else:
            plan, attempts = [t], 1 + retries
        tmp = dst.with_name(dst.stem + ".part.jpg")
        self._snap.err = "time is in the future"
        try:
            for at in plan:
                if at > datetime.now():
                    break
                for attempt in range(attempts):
                    if attempt:
                        time.sleep(_RETRY_DELAY_S)
                    with self.session.slots:
                        if precision == "exact":
                            url = self.playback_url(at - timedelta(seconds=_EXACT_BEFORE_S),
                                                    at + timedelta(seconds=_EXACT_AFTER_S))
                            err = _grab_frame(url, tmp, _EXACT_BEFORE_S, timeout=45)
                        else:
                            err = _grab_frame(self.playback_url(at, at + timedelta(seconds=1)), tmp)
                    self._snap.err = err
                    if not err:
                        os.replace(tmp, dst)
                        return at
            return None
        finally:
            if tmp.exists():
                tmp.unlink()

    def snapshots(self, times, out_dir, precision="hour", workers=None, verbose=True):
        """snapshot() for many times, in parallel -- at most max_connections at once
        per NVR, shared with download(). Files are named
        <camera id>_<YYYY-MM-DD_HHMMSS of the requested time>.jpg; ones already in
        out_dir are skipped, so an interrupted run picks up where it left off. The
        actual time of each picture is kept in out_dir/snapshots.csv.

        Returns {requested time: actual time, or None if it failed}."""
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        index_path = out_dir / SNAPSHOT_INDEX
        index = {}
        if index_path.exists():
            with open(index_path, newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    index[row["file"]] = datetime.fromisoformat(row["actual"])

        result, todo = {}, []
        for t in times:
            name = f"{self.id}_{t:%Y-%m-%d_%H%M%S}.jpg"
            if (out_dir / name).exists():
                result[t] = index.get(name, t)
            else:
                todo.append((t, name))
        if verbose:
            print(f"{self.id}: {len(todo)} to fetch, {len(result)} already in {out_dir}")
        if not todo:
            return result

        def one(t, name):
            actual = self.snapshot(t, out_dir / name, precision)
            return actual, (None if actual else self.last_snapshot_error)

        new_index = not index_path.exists()
        ok = fail = 0
        with open(index_path, "a", newline="", encoding="utf-8") as idx, \
                ThreadPoolExecutor(max_workers=workers or self.session.max_connections) as pool:
            w = csv.writer(idx)
            if new_index:
                w.writerow(["file", "requested", "actual"])
            futures = {pool.submit(one, t, name): (t, name) for t, name in todo}
            for fut in as_completed(futures):
                t, name = futures[fut]
                try:
                    actual, err = fut.result()
                except Exception as e:      # one bad time must not stop the batch
                    actual, err = None, f"{type(e).__name__}: {e}"
                result[t] = actual
                if actual is None:
                    fail += 1
                    if verbose:
                        print(f"FAIL  {name}  {err}")
                else:
                    ok += 1
                    w.writerow([name, t.isoformat(sep=" "), actual.isoformat(sep=" ")])
                    idx.flush()
                    if verbose:
                        print(f"OK    {name}  +{(actual - t).total_seconds() / 60:.0f}min")
                if verbose and (ok + fail) % 20 == 0:
                    print(f"[{ok + fail}/{len(todo)}] ok={ok} fail={fail}")
        if verbose:
            print(f"{self.id}: done ok={ok} fail={fail} (of {len(todo)})")
        return result

    def close(self):
        with _sessions_lock:
            self.session.refs -= 1
            if self.session.refs == 0:
                self.session.close()
                _sessions.pop(self.nvr_name, None)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def __repr__(self):
        return f"Camera({self.id}, {self.cfg['vendor']})"


def open_camera(spec, max_connections=4):
    (name, no), = parse_cameras(spec)
    return Camera(name, no, max_connections)


def list_cameras(nvr_name, nvrs=None):
    """[(camera_no, name)] of every camera on an NVR, names as set on the NVR (SDK login;
    unused Dahua channels keep "ChannelN"). Raises RuntimeError if the login fails."""
    with Camera(nvr_name, 1, nvrs=nvrs) as cam:
        return cam.session.list_cameras()


def _test(args):
    import subprocess
    import time
    start = (datetime.strptime(args.start, "%Y-%m-%d %H:%M:%S") if args.start
             else datetime.now().replace(microsecond=0) - timedelta(minutes=args.minutes + 5))
    end = start + timedelta(minutes=args.minutes)
    with open_camera(args.camera) as cam:
        print(f"login OK: {cam} -> SDK channel {cam.session.sdk_channel(cam.camera_no)}")
        dst = Path(args.out) / f"_test_{cam.id}{'.dav' if cam.cfg['vendor'] == 'dahua' else '.mp4'}"
        t0 = time.time()
        ok = cam.download(start, end, str(dst), progress_cb=lambda p: None)
        dt = time.time() - t0
        if ok is None:
            print(f"no recording in {start} - {end}")
            return
        if not ok:
            print(f"download failed (error={cam.last_error()}, {dt:.1f}s)")
            return
        mb = dst.stat().st_size / 1e6
        print(f"download OK: {dst} {mb:.1f} MB in {dt:.1f}s = {args.minutes * 60 / dt:.1f}x realtime")
        probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                                "stream=codec_name,width,height:format=format_name,duration",
                                "-of", "compact", str(dst)], capture_output=True, text=True)
        print("ffprobe:", (probe.stdout or probe.stderr).strip())
        if not args.keep:
            dst.unlink()


def parse_every(text):
    """"1h" / "30m" / "90s" / "2d" -> timedelta."""
    units = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}
    num, unit = text[:-1], text[-1:].lower()
    if unit not in units or not num.isdigit() or int(num) < 1:
        raise SystemExit(f"--every '{text}': use a number + s/m/h/d, e.g. 1h or 30m")
    return timedelta(**{units[unit]: int(num)})


def snapshot_times(days_back, every, now=None):
    """Every `every` from midnight (days_back - 1) days ago up to now, oldest first --
    days_back counts today."""
    now = now or datetime.now()
    t = (now - timedelta(days=days_back - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    out = []
    while t <= now:
        out.append(t)
        t += every
    return out


def _parse_time(text):
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    raise SystemExit(f"time '{text}': use \"YYYY-mm-dd HH:MM[:SS]\"")


def _snapshot(args):
    with open_camera(args.camera) as cam:
        got = cam.snapshot(_parse_time(args.time), args.dst, precision=args.precision)
        if got is None:
            raise SystemExit(f"no picture: {cam.last_snapshot_error}")
        print(f"OK {args.dst}  requested {got}")


def _snapshots(args):
    times = snapshot_times(args.days_back, parse_every(args.every))
    with open_camera(args.camera, max_connections=args.connections) as cam:
        result = cam.snapshots(times, args.out, precision=args.precision)
    if any(v is None for v in result.values()):
        sys.exit(1)


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("test", help="login + download a short stretch + ffprobe")
    t.add_argument("camera", help="<NVR name>:<camera no>, e.g. home-hik:15")
    t.add_argument("--minutes", type=int, default=1)
    t.add_argument("--start", help='"YYYY-mm-dd HH:MM:SS" (default: minutes+5 minutes ago)')
    t.add_argument("--out", default=".")
    t.add_argument("--keep", action="store_true", help="keep the downloaded file (default: delete it)")
    sub.add_parser("list", help="list the NVRs in nvrs.env (no passwords)")
    lc = sub.add_parser("cameras", help="camera numbers + names set on an NVR (SDK login)")
    lc.add_argument("nvr", help="NVR name from nvrs.env, e.g. home-hik")
    sn = sub.add_parser("snapshot", help="one picture from recorded footage (RTSP, no SDK login)")
    sn.add_argument("camera", help="<NVR name>:<camera no>, e.g. home-hik:15")
    sn.add_argument("time", help='"YYYY-mm-dd HH:MM[:SS]" local time')
    sn.add_argument("dst", help="output .jpg")
    sn.add_argument("--precision", choices=PRECISIONS, default="keyframe")
    ss = sub.add_parser("snapshots", help="a picture every --every for the last --days-back days")
    ss.add_argument("camera", help="<NVR name>:<camera no>, e.g. home-hik:15")
    ss.add_argument("--days-back", type=int, default=14, help="days including today (default 14)")
    ss.add_argument("--every", default="1h", help="interval: 1h, 30m, ... (default 1h)")
    ss.add_argument("--out", default="output",
                    help="output folder (default: ./output; already-fetched pictures are skipped)")
    ss.add_argument("--precision", choices=PRECISIONS, default="hour")
    ss.add_argument("--connections", type=int, default=4,
                    help="max concurrent pulls from this NVR (default 4)")
    a = p.parse_args()
    if a.cmd == "test":
        _test(a)
    elif a.cmd == "snapshot":
        _snapshot(a)
    elif a.cmd == "snapshots":
        _snapshots(a)
    elif a.cmd == "cameras":
        for no, name in list_cameras(a.nvr.lower()):
            print(f"{a.nvr.lower()}:{no}	{name}")
    else:
        for n, c in sorted(load_nvrs().items()):
            print(f"{n:20s} {c['vendor']:6s} {c['ip']}:{c['port']}  user={c['user']}")
