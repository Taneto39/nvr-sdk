"""
camera_picker.py -- turn a --camera argument into a list of Cameras, asking the
user with a checklist window when it doesn't name every camera itself.

    from camera_picker import resolve_cameras
    sel = resolve_cameras(args.camera, ".camera_picks.json")           # cameras only
    sel = resolve_cameras(args.camera, ".camera_picks.json",
                          ask_audio=True, audio_spec=args.audio)       # + Audio sources
    sel.cameras   # [(nvr name, camera no), ...]
    sel.audio     # [(nvr name, camera no), ...] subset of sel.cameras, in pick order

--camera forms:
    (none / "")            window listing every NVR in nvrs.env
    "home-hik,shop-dahua"   window listing just those NVRs
    "home-hik:15,shop-dahua:3"  those cameras, no window
    "home-hik:15,shop-dahua"   home-hik:15 fixed (pre-ticked) + window for shop-dahua

The window starts with the previous run's ticks (kept in picks_path) and lists
cameras by the names set on the NVR. Listing logs in to every NVR at once; an
NVR that doesn't answer within list_timeout seconds is left out with a warning.
"""

import json
import re
import threading
from collections import namedtuple
from pathlib import Path

import nvr

LIST_TIMEOUT_S = 15
PICKER_ROWS = 16                    # checkboxes per column before wrapping
DEFAULT_NAME = re.compile(r"^(channel|ipcamera|camera)\s*\d+$", re.I)   # never renamed = likely unused
AUDIO_KEY = "_audio"                # NVR names never contain "_" (nvr.load_nvrs), so no clash

Selection = namedtuple("Selection", "cameras audio")


def list_all(names, nvrs, timeout=LIST_TIMEOUT_S):
    """{nvr name: [(camera no, camera name)]} for every NVR that answered within timeout.
    All NVRs are listed at once. A hung SDK call can't be cancelled, so its thread is
    a daemon left behind rather than something to wait for."""
    results = {}

    def one(name):
        try:
            results[name] = nvr.list_cameras(name, nvrs=nvrs)
        except (RuntimeError, SystemExit, OSError) as e:
            results[name] = e

    threads = {name: threading.Thread(target=one, args=(name,), daemon=True) for name in names}
    for t in threads.values():
        t.start()
    listed = {}
    for name, t in threads.items():
        t.join(timeout)        # all started together: later joins have mostly waited already
        r = results.get(name)
        if r is None:
            print(f"[camera picker] {name} skipped: no answer within {timeout}s")
        elif isinstance(r, Exception):
            print(f"[camera picker] {name} skipped: {r}")
        else:
            listed[name] = r
    return listed


def resolve_cameras(spec, picks_path, *, ask_audio=False, audio_spec=None, nvrs=None,
                    list_timeout=LIST_TIMEOUT_S):
    """--camera (and --audio) -> Selection(cameras, audio). See the module docstring.
    audio is always [] unless ask_audio. Raises SystemExit if nothing was chosen."""
    nvrs = nvrs if nvrs is not None else nvr.load_nvrs()
    parts = [p.strip() for p in (spec or "").split(",") if p.strip()]
    bare = [p.lower() for p in parts if ":" not in p]
    fixed_spec = ",".join(p for p in parts if ":" in p)
    fixed = nvr.parse_cameras(fixed_spec) if fixed_spec else []
    audio = nvr.parse_cameras(audio_spec) if (ask_audio and audio_spec) else []
    if not parts:
        bare = sorted(nvrs)

    if not bare:
        stray = [c for c in audio if c not in fixed]
        if stray:
            raise SystemExit(f"--audio {', '.join(f'{n}:{c}' for n, c in stray)} not in --camera")
        return Selection(fixed, audio)

    for name in bare:
        if name not in nvrs:
            raise SystemExit(f"No NVR named '{name}' in nvrs.env (have: {', '.join(sorted(nvrs))})")
    print(f"[camera picker] reading camera names from {', '.join(dict.fromkeys(bare))}...", flush=True)
    listed = list_all(list(dict.fromkeys(bare)), nvrs, list_timeout)
    if not listed:
        raise SystemExit("No NVR could be listed.")

    picks_path = Path(picks_path)
    try:
        picks = json.loads(picks_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        picks = {}
    saved_audio = [tuple(x) for x in picks.get(AUDIO_KEY, [])]
    pre_audio = list(dict.fromkeys(audio + [(n, _no(c)) for n, c in saved_audio]))
    chosen = _pick(listed, picks, already=set(fixed), ask_audio=ask_audio, pre_audio=pre_audio)
    if chosen is None:
        raise SystemExit("[camera picker] cancelled.")
    cams, heard = chosen

    for name in listed:
        picks[name] = [str(no) for n, no in cams if n == name]
    if ask_audio:
        # keep audio picks of NVRs not listed this time; replace the listed ones
        picks[AUDIO_KEY] = [list(x) for x in saved_audio if x[0] not in listed] + \
                           [[n, str(no)] for n, no in heard]
    picks_path.parent.mkdir(parents=True, exist_ok=True)
    picks_path.write_text(json.dumps(picks, indent=1), encoding="utf-8")

    out = list(dict.fromkeys(fixed + cams))
    if not out:
        raise SystemExit("[camera picker] no camera ticked.")
    heard = [c for c in dict.fromkeys(audio + heard) if c in out] if ask_audio else []
    msg = f"[camera picker] --camera {_spec(out)}"
    if ask_audio:
        msg += " --audio " + (_spec(heard) or '""')
    print(msg)
    return Selection(out, heard)


def _no(text):
    """'15' -> 15, 'a3' -> 'a3' (the camera-no forms nvr.parse_cameras gives)."""
    return int(text) if str(text).isdigit() else str(text)


def _spec(cams):
    return ",".join(f"{n}:{no}" for n, no in cams)


def _pick(listed, picks, already=(), ask_audio=False, pre_audio=()):
    """Tk checklist, one frame per NVR. Returns (cameras, audio) or None if cancelled.
    audio is in the order the boxes were ticked (pre-ticked ones first, in pre_audio order)."""
    import tkinter as tk
    from tkinter import ttk

    root = tk.Tk()
    root.title("Pick cameras" + (" and audio" if ask_audio else ""))
    result = {"ok": False}
    boxes = []                     # (nvr name, camera no, camera var)
    audio_order = []               # (nvr name, camera no) in tick order
    body = ttk.Frame(root, padding=8)
    body.pack(fill="both", expand=True)
    if ask_audio:
        ttk.Label(root, text="Left box: download this camera.  Speaker box: put its sound in the "
                             "video (first ticked plays by default).", padding=(8, 4, 8, 0)).pack(
            side="top", anchor="w", before=body)

    for col, (name, cams) in enumerate(listed.items()):
        prev = set(picks.get(name, []))
        frame = ttk.LabelFrame(body, text=f"{name} ({len(cams)})", padding=6)
        frame.grid(row=0, column=col, sticky="nsew", padx=4)
        own = []
        span = 2 if ask_audio else 1
        for i, (no, cam_name) in enumerate(cams):
            key = (name, no)
            var = tk.BooleanVar(value=str(no) in prev or key in already)
            label = f"{no}  {cam_name}" + ("  (default name)" if DEFAULT_NAME.match(cam_name) else "")
            r, c = 1 + i % PICKER_ROWS, (i // PICKER_ROWS) * span
            ttk.Checkbutton(frame, text=label, variable=var).grid(row=r, column=c, sticky="w", padx=(0, 4))
            own.append(var)
            boxes.append((name, no, var))
            if ask_audio:
                avar = tk.BooleanVar(value=key in pre_audio and var.get())
                abox = ttk.Checkbutton(frame, text="\U0001F50A", variable=avar)
                abox.grid(row=r, column=c + 1, sticky="w", padx=(0, 12))
                if avar.get():
                    audio_order.append(key)

                def on_audio(*_, k=key, v=avar):
                    if v.get() and k not in audio_order:
                        audio_order.append(k)
                    elif not v.get() and k in audio_order:
                        audio_order.remove(k)

                def on_camera(*_, v=var, a=avar, b=abox):
                    if not v.get():
                        a.set(False)
                    b.state(["!disabled"] if v.get() else ["disabled"])

                avar.trace_add("write", on_audio)
                var.trace_add("write", on_camera)
                on_camera()
        bar = ttk.Frame(frame)
        bar.grid(row=0, column=0, columnspan=max(1, -(-len(cams) // PICKER_ROWS)) * span,
                 sticky="w", pady=(0, 4))
        ttk.Button(bar, text="All", width=6, command=lambda vs=own: [v.set(True) for v in vs]).pack(side="left")
        ttk.Button(bar, text="None", width=6, command=lambda vs=own: [v.set(False) for v in vs]).pack(side="left", padx=4)
    audio_order.sort(key=lambda k: list(pre_audio).index(k) if k in pre_audio else len(pre_audio))

    def done(ok):
        result["ok"] = ok
        # quit, not destroy: OK's click is still being dispatched, and the root-level
        # <ButtonRelease-1> binding below runs after the button's own. Destroying here
        # deletes that binding's Tcl command first -> "invalid command name ...<lambda>".
        root.quit()

    foot = ttk.Frame(root, padding=(8, 0, 8, 8))
    foot.pack(fill="x")
    count = ttk.Label(foot)
    count.pack(side="left")
    ttk.Button(foot, text="Cancel", command=lambda: done(False)).pack(side="right")
    ttk.Button(foot, text="OK", command=lambda: done(True)).pack(side="right", padx=4)

    def update_count(*_):
        text = f"{sum(v.get() for _, _, v in boxes)} ticked"
        if ask_audio:
            text += f", {len(audio_order)} with sound"
        count.config(text=text)
    for _, _, v in boxes:
        v.trace_add("write", update_count)
    update_count()
    # audio ticks change audio_order (a plain list, not a Tk variable): recount after clicks
    root.bind("<ButtonRelease-1>", lambda e: root.after_idle(update_count), add="+")
    root.bind("<Return>", lambda e: done(True))
    root.bind("<Escape>", lambda e: done(False))
    root.protocol("WM_DELETE_WINDOW", lambda: done(False))
    root.attributes("-topmost", True)   # launched from a terminal: don't open behind it
    untop = root.after(300, lambda: root.attributes("-topmost", False))
    root.focus_force()
    root.mainloop()
    # Leave no Tk callback pending: tkinter keeps pumping Tk events while input() waits,
    # so one that fires after destroy() prints "invalid command name" over the y/N prompt.
    root.after_cancel(untop)
    root.update_idletasks()   # run the after_idle recount the closing click queued
    root.destroy()
    if not result["ok"]:
        return None
    cams = [(name, no) for name, no, v in boxes if v.get()]
    return cams, [k for k in audio_order if k in cams]
