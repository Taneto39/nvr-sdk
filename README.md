# nvr-sdk

One Python interface over **Hikvision** (HCNetSDK) and **Dahua** (NetSDK) NVRs/DVRs:
download recorded footage by time range (fast and trimmed exactly, unlike RTSP
playback at 1x or Hikvision ISAPI downloads that ignore the end time), pull stills
from recordings, list cameras by the names set on the NVR, and pick cameras in a
small checklist window.

Windows only (the vendor SDKs are Windows DLLs), 64-bit Python.

| File | What it is |
|---|---|
| `nvr.py` | The shared layer: `Camera("home-hik", 15).download(...)`, `.rtsp_url()`, `.playback_url()`, `.snapshot()` / `.snapshots()`, `.download_keyframes()` (Dahua only), `list_cameras()` |
| `camera_picker.py` | Turns a `--camera` argument into cameras, opening a checklist window when it names only NVRs (or nothing) |
| `hik_sdk.py` | ctypes wrapper for HCNetSDK: login, `GetFileByTime`, channel names |
| `dahua_sdk.py` | Wrapper over Dahua's NetSDK Python wheel: login, `DownloadByTimeEx`, keyframe-only fetch, channel names |
| `nvrs.env.example` | Template for `nvrs.env`, the list of NVRs with their credentials (gitignored) |

## Setup

1. Get the vendor SDKs (not included -- they aren't ours to redistribute):
   - **Hikvision** Device Network SDK for Windows 64-bit: put `HCNetSDK.dll`,
     `HCNetSDKCom/` and the other DLLs in `hikvision/`.
   - **Dahua** NetSDK for Windows 64-bit: the zip contains a Python wheel; put it in
     `dahua/`.
2. `pip install -r requirements.txt` (run from this folder: it installs the Dahua wheel
   from `./dahua/` plus `opencv-python`).
3. `copy nvrs.env.example nvrs.env` and fill in each NVR:

   ```
   NVR_HOME_HIK_VENDOR=hik          # hik or dahua
   NVR_HOME_HIK_IP=192.168.x.x
   NVR_HOME_HIK_USER=admin
   NVR_HOME_HIK_PASSWORD=...
   # NVR_HOME_HIK_MAX_CONNECTIONS=4 # cap on concurrent downloads from this NVR
   ```

   `HOME_HIK` becomes the name `home-hik`.

`ffmpeg`/`ffprobe` on `PATH` are needed for snapshots and for `nvr.py test`.

## Naming cameras

A camera is `<NVR name>:<camera no>`, where the number is the one shown on the
NVR's own screen / iVMS / SmartPSS, starting at 1 -- never the SDK channel number
(Hikvision's first IP camera is SDK channel 33, Dahua's is 0; that mapping is done
for you). On a Hikvision hybrid DVR, analog inputs are `a<N>` (`dvr:a1`); a plain
number is still an IP camera.

## Use from another project

Point the project at this folder (e.g. `NVR_SDK_DIR=/path/to/nvr-sdk` in its `.env`)
and put it on `sys.path` before importing:

```python
from nvr import open_camera, list_cameras

with open_camera("home-hik:15") as cam:
    ok = cam.download(start, end, "clip.mp4")   # True / False (retry) / None (no recording)
    cam.snapshot(t, "still.jpg", precision="keyframe")

print(list_cameras("home-hik"))                 # [(1, "Front gate"), (2, "Garage"), ...]
```

Cameras on the same NVR share one SDK login. The login is lazy -- RTSP calls
(`rtsp_url`, `snapshot`) never need it; call `cam.login()` to fail early on a bad
password. `rtsp_url()` / `playback_url()` contain the password: never print them.

### Camera picker

```python
from camera_picker import resolve_cameras

sel = resolve_cameras(args.camera, ".camera_picks.json")
sel.cameras        # [("home-hik", 15), ("shop-dahua", 3)]

sel = resolve_cameras(args.camera, ".camera_picks.json", ask_audio=True, audio_spec=args.audio)
sel.audio          # cameras ticked for sound, in the order they were ticked
```

| `--camera` | Result |
|---|---|
| *(empty)* | Window listing every NVR in `nvrs.env` |
| `home-hik,shop-dahua` | Window listing just those NVRs |
| `home-hik:15,shop-dahua:3` | Those cameras, no window |
| `home-hik:15,shop-dahua` | `home-hik:15` pre-ticked + window for `shop-dahua` |

The window starts from the previous run's ticks (saved to the picks file). All
NVRs are listed at once; one that doesn't answer within 15 s is skipped with a
warning. With `ask_audio=True` each camera gets a speaker box too.

## Try it against a real NVR

```
python nvr.py list                     # NVRs in nvrs.env (no passwords)
python nvr.py cameras home-hik         # camera numbers + names set on the NVR
python nvr.py test home-hik:15         # login + download 1 minute + ffprobe
python nvr.py snapshot home-hik:15 "2026-10-09 04:00" x.jpg --precision exact
python nvr.py snapshots home-hik:15 --days-back 7 --every 1h   # -> ./output/
```

## Snapshots

`cam.snapshot(t, dst, precision=...)` saves one still from *recorded* footage over
RTSP playback (no SDK login, no DLLs) and returns the time it was requested at, or
`None` (reason in `cam.last_snapshot_error`).

| Precision | Picture |
|---|---|
| `keyframe` (default) | A keyframe near `t` (within one GOP, ~2 s) |
| `exact` | Meant to be the frame at `t`; today still off by up to one GOP, because the NVR starts the stream at a keyframe and RTSP doesn't say which |
| `hour` | Any picture in `t` .. `t`+59 min (tries `t`, then later minutes) |

`cam.snapshots(times, out_dir)` fetches many in parallel, skips files already
there and records each picture's actual time in `out_dir/snapshots.csv`.
Occasionally ffmpeg returns a valid JPEG that decoded only one corner and is white
elsewhere; with OpenCV installed such pictures are detected and retried.

## Notes

- Hikvision: too many concurrent downloads per NVR gives transient error 10 -- use
  `MAX_CONNECTIONS`. Downloads come back as MPEG-PS; the SDK can't write to a
  non-ASCII path, so files go to an ASCII temp name and are renamed.
- Dahua: downloads are `.dav` (ffmpeg's `dhav` demuxer reads them). "No recording
  in range" is reported as `None`, not a failure, on both vendors.
- Hikvision NVRs read RTSP playback times as local time despite the trailing `Z`.
