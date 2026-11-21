"""
hik_sdk.py -- Hikvision HCNetSDK (the protocol iVMS-4200 uses, port 8000) over ctypes

Downloads a time range "fast and trimmed": a 3-minute clip takes ~4 s (RTSP playback
runs at 1x = 180 s). ISAPI download is no use here (the firmware ignores endtime and
streams the whole segment), hence the SDK.

The DLLs live in <this folder>/hikvision (HCNetSDK.dll + HCNetSDKCom/ + its other .dll).

As a library:
  sdk = load_sdk(); net_init(sdk)
  uid = login(sdk, ip, 8000, user, pw)          # or login_ex(...) -> (uid, start_dchan, start_chan, chan_num)
  download(sdk, uid, channel, start_dt, end_dt, dst, progress_cb)
  logout(sdk, uid); net_cleanup(sdk)

channel here is the SDK's number: an NVR's first IP camera is usually 33 (D1=33, D2=34, ...),
a DVR's analog inputs start at 1 -- login_ex returns both starts from the device info.
Try it against a real NVR with `python nvr.py test ...` (user/password from nvrs.env).
"""

import os
import shutil
import tempfile
import time
import uuid
import ctypes
from ctypes import (
    c_byte, c_uint32 as DWORD, c_uint16 as WORD, c_long as LONG,
    c_void_p, c_char_p, c_int, byref, POINTER, Structure,
)
from datetime import datetime

SDK_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hikvision")

NET_DVR_PLAYSTART = 1


class NET_DVR_TIME(Structure):
    _fields_ = [
        ("dwYear", DWORD), ("dwMonth", DWORD), ("dwDay", DWORD),
        ("dwHour", DWORD), ("dwMinute", DWORD), ("dwSecond", DWORD),
    ]

    @classmethod
    def from_dt(cls, dt: datetime):
        return cls(dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second)


class NET_DVR_PLAYCOND(Structure):
    # 180 bytes in all, per the HCNetSDK spec
    _fields_ = [
        ("dwChannel", DWORD),
        ("struStartTime", NET_DVR_TIME),
        ("struStopTime", NET_DVR_TIME),
        ("byDrawFrame", c_byte),
        ("byStreamType", c_byte),      # 0 = main stream
        ("byAudioFile", c_byte),
        ("byRes", c_byte * 125),
    ]


def load_sdk():
    """Load HCNetSDK.dll with the dependencies next to it, and set argtypes."""
    dll_path = os.path.join(SDK_DIR, "HCNetSDK.dll")
    if not os.path.isfile(dll_path):
        raise RuntimeError(f"HCNetSDK.dll not found in {SDK_DIR}")

    if hasattr(os, "add_dll_directory"):
        os.add_dll_directory(os.path.abspath(SDK_DIR))
    os.environ["PATH"] = os.path.abspath(SDK_DIR) + os.pathsep + os.environ.get("PATH", "")

    sdk = ctypes.WinDLL(dll_path)
    sdk.NET_DVR_Init.restype = c_int
    sdk.NET_DVR_Cleanup.restype = c_int
    sdk.NET_DVR_GetLastError.restype = DWORD
    sdk.NET_DVR_Login_V30.argtypes = [c_char_p, WORD, c_char_p, c_char_p, c_void_p]
    sdk.NET_DVR_Login_V30.restype = LONG
    sdk.NET_DVR_Logout.argtypes = [LONG]
    sdk.NET_DVR_Logout.restype = c_int
    sdk.NET_DVR_GetFileByTime_V40.argtypes = [LONG, c_char_p, POINTER(NET_DVR_PLAYCOND)]
    sdk.NET_DVR_GetFileByTime_V40.restype = LONG
    sdk.NET_DVR_PlayBackControl_V40.argtypes = [LONG, DWORD, c_void_p, DWORD, c_void_p, c_void_p]
    sdk.NET_DVR_PlayBackControl_V40.restype = c_int
    sdk.NET_DVR_GetDownloadPos.argtypes = [LONG]
    sdk.NET_DVR_GetDownloadPos.restype = c_int
    sdk.NET_DVR_StopGetFile.argtypes = [LONG]
    sdk.NET_DVR_StopGetFile.restype = c_int
    return sdk


def last_error(sdk):
    return sdk.NET_DVR_GetLastError()


def net_init(sdk):
    return bool(sdk.NET_DVR_Init())


def net_cleanup(sdk):
    sdk.NET_DVR_Cleanup()


def login(sdk, ip, port, user, password):
    """userID (>=0), or -1 on failure."""
    devinfo = ctypes.create_string_buffer(256)   # NET_DVR_DEVICEINFO_V30 (with room to spare)
    return sdk.NET_DVR_Login_V30(
        ip.encode(), WORD(port), user.encode(), password.encode(), devinfo
    )


def login_ex(sdk, ip, port, user, password):
    """Like login, but returns (uid, start_dchan, start_chan, chan_num) from NET_DVR_DEVICEINFO_V30:
    start_dchan = SDK number of the first IP camera (0 if none), start_chan = first analog input,
    chan_num = number of analog channels (0 = pure NVR, >0 = DVR / hybrid DVR)"""
    devinfo = ctypes.create_string_buffer(256)
    uid = sdk.NET_DVR_Login_V30(ip.encode(), WORD(port), user.encode(), password.encode(), devinfo)
    raw = devinfo.raw
    # offsets in NET_DVR_DEVICEINFO_V30: sSerialNumber[48], ..., byChanNum@52, byStartChan@53, byStartDChan@66
    return uid, raw[66], raw[53], raw[52]


def logout(sdk, uid):
    if uid is not None and uid >= 0:
        sdk.NET_DVR_Logout(uid)


NET_DVR_GET_PICCFG_V30 = 1002
_PICCFG_V30_SIZE = 7752          # sizeof(NET_DVR_PICCFG_V30); any other size -> error 17
NET_DVR_CHANNEL_ERROR = 4        # no such channel


def channel_name(sdk, uid, channel):
    """The camera name shown on the NVR for an SDK channel (sChanName, right after
    dwSize in NET_DVR_PICCFG_V30). None if the channel doesn't exist; RuntimeError
    on any other failure."""
    sdk.NET_DVR_GetDVRConfig.argtypes = [LONG, DWORD, LONG, c_void_p, DWORD, POINTER(DWORD)]
    sdk.NET_DVR_GetDVRConfig.restype = c_int
    buf = ctypes.create_string_buffer(_PICCFG_V30_SIZE)
    ret = DWORD(0)
    if not sdk.NET_DVR_GetDVRConfig(uid, NET_DVR_GET_PICCFG_V30, channel, buf, _PICCFG_V30_SIZE, byref(ret)):
        err = last_error(sdk)
        if err == NET_DVR_CHANNEL_ERROR:
            return None
        raise RuntimeError(f"NET_DVR_GET_PICCFG_V30 channel {channel} failed (HCNetSDK error={err})")
    return buf.raw[4:36].split(b"\0")[0].decode("utf-8", "replace").strip()


def download(sdk, uid, channel, start, end, dst, progress_cb=None, timeout=300, stream_type=0, draw_frame=0):
    """
    Download start->end of channel into dst (trimmed exactly, fast).
    progress_cb(percent 0..100) is called while downloading.
    Returns True (downloaded) / False (failed, worth a retry) / None (no recording in range).

    The SDK is an ANSI API and can't write to a non-ASCII path, so it writes a
    temporary ASCII-named file that is then renamed to dst (Python handles Unicode).
    """
    dst_dir = os.path.dirname(os.path.abspath(dst))
    os.makedirs(dst_dir, exist_ok=True)
    # the folder must be ASCII too (mbcs can't hold Thai on a non-Thai locale), else %TEMP%
    tmp_dir = dst_dir if dst_dir.isascii() else tempfile.gettempdir()
    tmp = os.path.join(tmp_dir, "_dl_" + uuid.uuid4().hex + ".tmp")

    cond = NET_DVR_PLAYCOND()
    cond.dwChannel = channel
    cond.struStartTime = NET_DVR_TIME.from_dt(start)
    cond.struStopTime = NET_DVR_TIME.from_dt(end)
    # 1 = "frame extraction" (I-frames only) -- ignored for downloads by the NVR tested
    # 2026-10-08 (same file, every frame), kept so other models can be tried
    cond.byDrawFrame = draw_frame
    cond.byStreamType = stream_type

    ctx = f"ch{channel} {start.strftime('%H:%M:%S')}~{end.strftime('%H:%M:%S')}"

    handle = sdk.NET_DVR_GetFileByTime_V40(uid, tmp.encode("mbcs"), byref(cond))
    if handle < 0:
        err = last_error(sdk)
        if err == 21:
            return None  # no footage for this time slot
        print(f"  [DBG] {ctx} GetFileByTime failed: error={err}")
        return False
    ok = False
    try:
        if not sdk.NET_DVR_PlayBackControl_V40(handle, NET_DVR_PLAYSTART, None, 0, None, None):
            print(f"  [DBG] {ctx} PlayBackControl failed: error={last_error(sdk)}")
            return False
        deadline = time.time() + timeout
        stuck99_since = None   # when the progress got stuck at 99%
        stuck99_size  = None   # file size when it got stuck
        while True:
            pos = sdk.NET_DVR_GetDownloadPos(handle)
            if progress_cb and 0 <= pos <= 100:
                progress_cb(pos)
            if pos in (100, 200):  # 200 = "done" signal on some NVR firmware
                if progress_cb:
                    progress_cb(100)
                break

            # some NVRs sit at 99% for a long time: once the file stops growing, call it done
            if pos == 99:
                cur_size = os.path.getsize(tmp) if os.path.exists(tmp) else 0
                if stuck99_since is None:
                    stuck99_since = time.time()
                    stuck99_size  = cur_size
                elif cur_size == stuck99_size and time.time() - stuck99_since > 3:
                    if progress_cb:
                        progress_cb(100)
                    break
                else:
                    stuck99_size = cur_size  # still growing: reset the timer
            else:
                stuck99_since = None
                stuck99_size  = None

            if pos < 0:
                print(f"  [DBG] {ctx} GetDownloadPos={pos}, error={last_error(sdk)}")
                return False
            if time.time() > deadline:
                print(f"  [DBG] {ctx} timeout ({timeout}s), last pos={pos}, error={last_error(sdk)}")
                return False
            time.sleep(0.3)
        ok = os.path.exists(tmp) and os.path.getsize(tmp) > 10_000
        # the NVR finished the download but sent (next to) nothing: no recording
        # in this range (camera off), seen with error 0 -- an answer, not a failure
        return True if ok else None
    finally:
        sdk.NET_DVR_StopGetFile(handle)
        if ok:
            try:
                shutil.move(tmp, dst)   # not os.replace: %TEMP% may be on another drive
            except OSError:
                pass
        elif os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
