"""
dahua_sdk.py -- Dahua NetSDK (port 37777) through Dahua's official Python wheel.
Install the wheel first (it ships in the SDK zip; put it in <this folder>/dahua):

    pip install <this folder>/dahua/NetSDK-2.0.0.1-py3-none-win_amd64.whl

As a library:
  init()
  login_id, chan_num = login(ip, 37777, user, pw)
  download(login_id, channel, start_dt, end_dt, dst, progress_cb)   # channel is 0-based
  logout(login_id); cleanup()

The file is .dav (Dahua's container) -- ffmpeg reads it with the `dhav` demuxer.
"""

import ctypes
import os
import struct
import threading
import time
import uuid
from ctypes import POINTER, Structure, byref, c_int, c_void_p, sizeof
from datetime import datetime, timedelta

import NetSDK.NetSDK as _netsdk
from NetSDK.NetSDK import NetClient
from NetSDK.SDK_Callback import fDataCallBack, fTimeDownLoadPosCallBack
from NetSDK.SDK_Enum import EM_LOGIN_SPAC_CAP_TYPE, EM_QUERY_RECORD_TYPE, EM_USEDEV_MODE
from NetSDK.SDK_Struct import (
    C_DWORD, C_LLONG, NET_IN_LOGIN_WITH_HIGHLEVEL_SECURITY, NET_OUT_LOGIN_WITH_HIGHLEVEL_SECURITY, NET_TIME,
)

sdk = NetClient()   # instantiating loads the DLLs

# QueryRecordFile's GetLastError for a range with no recording ("There is no
# searched result."), seen for the future, years back and a camera that was off
QUERY_NO_RECORD_ERROR = 24


def _wheel_print(*args, **kwargs):
    """The wheel prints every failed call's error text straight to stdout;
    drop the "no recording" one (a normal answer) and send the rest through
    this module's print, which callers like nvr_scan.py reroute."""
    if args and args[0] == "There is no searched result.":
        return
    print(*args, **kwargs)


_netsdk.print = _wheel_print
_init_lock = threading.Lock()
_inited = False

# download handle -> {"total", "done", "event", "failed"}; filled by the pos callback
_downloads = {}
_DONE = (-1, 0xFFFFFFFF)
_FAILED = (-2, 0xFFFFFFFE)


def _new_state():
    return {"total": 0, "done": 0, "event": threading.Event(), "failed": False}


@fTimeDownLoadPosCallBack
def _pos_cb(handle, total, done, index, recordfile_info, user):
    # setdefault: the callback can fire before download() has registered the handle
    st = _downloads.setdefault(handle, _new_state())
    if done in _DONE:
        st["event"].set()
    elif done in _FAILED:
        st["failed"] = True
        st["event"].set()
    else:
        st["total"], st["done"] = total, done


_no_data_cb = fDataCallBack(0)   # NULL: let the SDK write the file itself


def init():
    global _inited
    with _init_lock:
        if not _inited:
            if not sdk.InitEx():
                raise RuntimeError(f"Dahua CLIENT_InitEx failed: {last_error()}")
            _inited = True


def cleanup():
    global _inited
    with _init_lock:
        if _inited:
            sdk.Cleanup()
            _inited = False


def last_error():
    return sdk.GetLastErrorMessage()


def login(ip, port, user, password):
    """(login_id, channel count). login_id == 0 means it failed (see last_error())."""
    inp = NET_IN_LOGIN_WITH_HIGHLEVEL_SECURITY()
    inp.dwSize = sizeof(NET_IN_LOGIN_WITH_HIGHLEVEL_SECURITY)
    inp.szIP = ip.encode()
    inp.nPort = int(port)
    inp.szUserName = user.encode()
    inp.szPassword = password.encode()
    inp.emSpecCap = EM_LOGIN_SPAC_CAP_TYPE.TCP
    inp.pCapParam = None
    out = NET_OUT_LOGIN_WITH_HIGHLEVEL_SECURITY()
    out.dwSize = sizeof(NET_OUT_LOGIN_WITH_HIGHLEVEL_SECURITY)
    login_id, device_info, error_msg = sdk.LoginWithHighLevelSecurity(inp, out)
    if not login_id:
        return 0, error_msg
    # main stream only (1 = main, 2 = sub, 0 = both)
    sdk.SetDeviceMode(login_id, int(EM_USEDEV_MODE.RECORD_STREAM_TYPE), c_int(1))
    return login_id, int(device_info.nChanNum)


def logout(login_id):
    if login_id:
        sdk.Logout(login_id)


def channel_names(login_id):
    """Camera names shown on the NVR, one per channel (index 0 = channel 0), from the
    "ChannelTitle" config. Unused channels keep the default "ChannelN"."""
    import json
    buf = ctypes.create_string_buffer(256 * 1024)
    if not sdk.GetNewDevConfig(login_id, "ChannelTitle", -1, buf, len(buf), 0, 5000):
        raise RuntimeError(f"ChannelTitle query failed: {last_error()}")
    table = json.loads(buf.value.decode("utf-8", "replace"))["params"]["table"]
    return [(t.get("Name") or "").strip() for t in table]


def _net_time(dt):
    t = NET_TIME()
    t.dwYear, t.dwMonth, t.dwDay = dt.year, dt.month, dt.day
    t.dwHour, t.dwMinute, t.dwSecond = dt.hour, dt.minute, dt.second
    return t


def query_records(login_id, channel, start, end):
    """[(start_dt, end_dt), ...] of the recordings in this range ([] if none, None if the query failed)."""
    from datetime import datetime
    ok, count, infos = sdk.QueryRecordFile(login_id, channel, int(EM_QUERY_RECORD_TYPE.ALL),
                                           _net_time(start), _net_time(end), None, 5000, False)
    if not ok:
        # a range with no recording (camera off) fails with this code instead of
        # returning 0 files -- an answer, not an error worth retrying
        return [] if sdk.GetLastError() == QUERY_NO_RECORD_ERROR else None
    out = []
    for i in range(count):
        s, e = infos[i].starttime, infos[i].endtime
        out.append((datetime(s.dwYear, s.dwMonth, s.dwDay, s.dwHour, s.dwMinute, s.dwSecond),
                    datetime(e.dwYear, e.dwMonth, e.dwDay, e.dwHour, e.dwMinute, e.dwSecond)))
    return out


def download(login_id, channel, start, end, dst, progress_cb=None, timeout=300):
    """Download start->end of channel (0-based) into dst.
    True = done, False = failed, None = no recording in this range."""
    recs = query_records(login_id, channel, start, end)
    if recs is not None and not recs:
        return None
    dst_dir = os.path.dirname(os.path.abspath(dst))
    os.makedirs(dst_dir, exist_ok=True)
    # the SDK takes a gbk path -- write to a temporary ASCII name, then rename
    tmp = os.path.join(dst_dir, "_dl_" + uuid.uuid4().hex + ".tmp")
    handle = sdk.DownloadByTimeEx(login_id, channel, int(EM_QUERY_RECORD_TYPE.ALL),
                                  _net_time(start), _net_time(end), tmp,
                                  _pos_cb, 0, _no_data_cb, 0)
    if not handle:
        print(f"  [DBG] dahua ch{channel} DownloadByTimeEx failed: {last_error()}")
        return False
    st = _downloads.setdefault(handle, _new_state())
    ok = False
    try:
        deadline = time.time() + timeout
        while not st["event"].wait(0.3):
            if progress_cb and st["total"]:
                progress_cb(min(99, int(st["done"] * 100 / st["total"])))
            if time.time() > deadline:
                print(f"  [DBG] dahua ch{channel} timeout ({timeout}s)")
                return False
        if st["failed"]:
            print(f"  [DBG] dahua ch{channel} download failed: {last_error()}")
            return False
        if progress_cb:
            progress_cb(100)
        ok = os.path.exists(tmp) and os.path.getsize(tmp) > 10_000
        return ok
    finally:
        sdk.StopDownload(handle)
        _downloads.pop(handle, None)
        if ok:
            os.replace(tmp, dst)
        elif os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


# --------------------------------------------------------------------------
# Keyframe-only fetch: CLIENT_GetIFrameDataFromStorageVideo returns the first
# recorded I-frame at or after a given second (one DHAV-wrapped frame, full
# resolution). Not wrapped by the Python wheel; struct layout taken from
# Dahua's Java wrapper plus a leading dwSize, verified on NVR5432-EI
# (2026-10-08). Walking "last frame's time + 1s" visits every keyframe once.
# --------------------------------------------------------------------------

class _IN_GET_IFRAME(Structure):
    _fields_ = [("dwSize", C_DWORD), ("nChannel", c_int), ("stuTime", NET_TIME)]


class _OUT_GET_IFRAME(Structure):
    _fields_ = [("dwSize", C_DWORD), ("nBufferLen", c_int), ("szBuffer", c_void_p), ("nRetBufferLen", c_int)]


_get_iframe = sdk.sdk.CLIENT_GetIFrameDataFromStorageVideo
_get_iframe.restype = c_int
_get_iframe.argtypes = [C_LLONG, POINTER(_IN_GET_IFRAME), POINTER(_OUT_GET_IFRAME), c_int]
_IFRAME_BUF = 4 * 1024 * 1024
_tls = threading.local()
# GetLastError for a time with no recording (seen for a date years back and for
# the future, ~24ms): treat as "nothing here", unlike transient errors
NO_RECORD_ERROR = 21


def dhav_time(frame):
    """Wall-clock time (whole seconds) from a DHAV frame header."""
    d = struct.unpack_from("<I", frame, 16)[0]
    return datetime(2000 + (d >> 26), (d >> 22) & 0xF, (d >> 17) & 0x1F,
                    (d >> 12) & 0x1F, (d >> 6) & 0x3F, d & 0x3F)


def get_iframe(login_id, channel, t, wait_ms=10000):
    """(bytes, None) for the first I-frame at or after t, or (None, error_code)."""
    buf = getattr(_tls, "buf", None)
    if buf is None:
        buf = _tls.buf = ctypes.create_string_buffer(_IFRAME_BUF)
    inp = _IN_GET_IFRAME(sizeof(_IN_GET_IFRAME), channel, _net_time(t))
    out = _OUT_GET_IFRAME(sizeof(_OUT_GET_IFRAME), _IFRAME_BUF, ctypes.cast(buf, c_void_p), 0)
    if not _get_iframe(login_id, byref(inp), byref(out), wait_ms):
        return None, sdk.GetLastError() & 0x7FFFFFFF
    data = buf.raw[:out.nRetBufferLen]
    if data[:4] != b"DHAV":
        return None, -1
    return data, None


def download_keyframes(login_id, channel, start, end, dst, progress_cb=None, timeout=300, retries=3):
    """Like download(), but writes only the I-frames of start->end (a valid
    .dav that ffmpeg's dhav demuxer reads). Same return contract: True / False
    / None (no recording in range). The first frame is the first keyframe at
    or after start, so read its time with dhav_time() rather than assuming start.

    One login fetches ~9 keyframes/s no matter how many threads share it, but
    separate logins add up (4 logins ~34/s on NVR5432-EI) -- nvr.py gives each
    concurrent fetch its own login for that reason."""
    recs = query_records(login_id, channel, start, end)
    if recs is None:
        return False
    spans = sorted((max(s, start), min(e, end)) for s, e in recs if e > start and s < end)
    if not spans:
        return None
    total = sum((e - s).total_seconds() for s, e in spans) or 1
    deadline = time.time() + timeout
    tmp = dst + ".tmp"
    n = 0
    try:
        with open(tmp, "wb") as f:
            done_s = 0.0
            for s, e in spans:
                t = s
                while t < e:
                    if time.time() > deadline:
                        print(f"  [DBG] dahua ch{channel} keyframes timeout ({timeout}s)")
                        return False
                    for attempt in range(retries + 1):
                        data, err = get_iframe(login_id, channel, t)
                        if data is not None or err == NO_RECORD_ERROR:
                            break
                        time.sleep(0.5 * (attempt + 1))
                    if data is None:
                        if err == NO_RECORD_ERROR:
                            break      # recording ends inside this span
                        # anything else (e.g. 2 under load) must not pass as "end of
                        # recording": the caller would mark a half-fetched chunk done
                        print(f"  [DBG] dahua ch{channel} GetIFrame {t:%H:%M:%S} error {err}")
                        return False
                    ft = dhav_time(data)
                    if ft >= e:
                        break
                    f.write(data)
                    n += 1
                    t = max(ft, t) + timedelta(seconds=1)
                    if progress_cb:
                        progress_cb(min(99, int((done_s + (t - s).total_seconds()) * 100 / total)))
                done_s += (e - s).total_seconds()
        if n == 0:
            return None
        os.replace(tmp, dst)
        if progress_cb:
            progress_cb(100)
        return True
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
