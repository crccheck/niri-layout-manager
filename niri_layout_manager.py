"""Save and load niri window layouts.

Usage:
  niri-layout-manager show [NAME]   Print a saved layout.
  niri-layout-manager save [NAME]   Save the current windows as layout NAME.
  niri-layout-manager load [NAME]   Move open windows into layout NAME and start missing apps.
  niri-layout-manager diff [NAME]   Compare the current windows with layout NAME.

NAME defaults to a guess:
  save: "single" if all windows are on the laptop monitor, else "double".
  load, show, diff: "single" if one monitor is connected, else "double".

Files are in ~/.config/layout/: NAME.json for each layout, and config.toml.

Windows that niri-layout-manager skips:
  - Floating windows.
  - Windows whose process was started by another app (for example a browser
    started by a test runner, or a program started from a terminal). Only
    processes whose parent is in `launchers` are kept.
  - Windows that match an `ignore` rule in config.toml.

Ghostty and Nautilus: load opens the window in its saved directory. Ghostty
shows the directory in its title at a shell prompt. Nautilus reports its open
directories over D-Bus. If the directory is not known, load opens a window
with no directory. Tabs are not saved. A Ghostty window whose directory is not
known (it runs a command) takes the place of a saved Ghostty window that no
other window matches, and diff shows it with the saved directory.
"""

import argparse
import json
import os
import re
import shlex
import socket
import subprocess
import sys
import time
import tomllib
import urllib.parse
from collections import defaultdict
from functools import cache
from pathlib import Path
from typing import NamedTuple

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "layout"
CONFIG_FILE = CONFIG_DIR / "config.toml"
GHOSTTY = "com.mitchellh.ghostty"
NAUTILUS = "org.gnome.Nautilus"
DIR_APPS = {GHOSTTY: "ghostty", NAUTILUS: "nautilus"}
SPAWN_TIMEOUT = 30.0
SELF_RESTORE_SETTLE = 3.0
LAYOUT_VERSION = 3

DEFAULT_CONFIG = {
    # Parent process names (comm) that count as "started by a launcher".
    "launchers": [
        "systemd",
        "niri",
        "qs",
        "quickshell",
        "dbus-daemon",
        "dbus-broker",
        "gtk-launch",
    ],
    # Apps that restore their own windows. load starts them once.
    "self_restoring": ["firefox"],
    # Rules for windows to skip: {app_id = "regex", title = "regex"}; all given keys must match.
    "ignore": [],
}


# --- niri IPC -------------------------------------------------------------


class NiriError(Exception):
    pass


def niri(request):
    path = os.environ.get("NIRI_SOCKET")
    if not path:
        raise NiriError("NIRI_SOCKET is not set; run this inside a niri session")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.connect(path)
        sock.sendall(json.dumps(request).encode() + b"\n")
        sock.shutdown(socket.SHUT_WR)
        data = b""
        while chunk := sock.recv(65536):
            data += chunk
    reply = json.loads(data)
    if "Err" in reply:
        raise NiriError(f"{request}: {reply['Err']}")
    return reply["Ok"]


def query(name):
    return niri(name)[name]


def action(name, **fields):
    niri({"Action": {name: fields}})


def outputs():
    """Enabled outputs keyed by connector name."""
    return {name: o for name, o in query("Outputs").items() if o.get("logical")}


def output_key(o):
    """Stable monitor identity: survives port changes on a dock."""
    return " | ".join(str(o.get(k) or "Unknown") for k in ("model", "serial"))


def is_laptop(name):
    return name.startswith(("eDP", "LVDS", "DSI"))


# --- config and window filtering -----------------------------------------


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_FILE.exists():
        with CONFIG_FILE.open("rb") as f:
            cfg.update(tomllib.load(f))
    return cfg


def ppid(pid):
    stat = Path(f"/proc/{pid}/stat").read_text()
    return int(stat.rsplit(")", 1)[1].split()[1])


def parent_comm(pid):
    try:
        parent = ppid(pid)
        if parent <= 1:
            return "init"
        return Path(f"/proc/{parent}/comm").read_text().strip()
    except (OSError, ValueError, IndexError):
        return None


@cache
def ancestors():
    """Pids of the processes above this one."""
    pids, pid = set(), os.getpid()
    while pid > 1:
        try:
            pid = ppid(pid)
        except (OSError, ValueError, IndexError):
            break
        pids.add(pid)
    return pids


def own_dir():
    """This process's directory, as the shell shows it ($PWD keeps symlinks)."""
    cwd = os.getcwd()
    pwd = os.environ.get("PWD")
    if pwd and os.path.isdir(pwd) and os.path.samefile(pwd, cwd):
        cwd = pwd
    return short_path(cwd)


def ghostty_path(title):
    """Directory shown in a Ghostty title, or None."""
    if not title or not title.startswith(("~", "/")):
        return None
    path = os.path.expanduser(title)
    return title if os.path.isdir(path) else None


def short_path(path):
    home = str(Path.home())
    return (
        "~" + path[len(home) :] if path == home or path.startswith(home + "/") else path
    )


@cache
def nautilus_dirs():
    """Directory of each open Nautilus window (first location), from D-Bus."""
    out = subprocess.run(
        [
            "busctl",
            "--user",
            "--json=short",
            "get-property",
            "org.freedesktop.FileManager1",
            "/org/freedesktop/FileManager1",
            "org.freedesktop.FileManager1",
            "OpenWindowsWithLocations",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if out.returncode != 0:
        return []
    dirs = []
    for uris in json.loads(out.stdout)["data"].values():
        uri = urllib.parse.urlparse(uris[0]) if uris else None
        if uri and uri.scheme == "file":
            dirs.append(short_path(urllib.parse.unquote(uri.path)))
    return dirs


def nautilus_path(title):
    """Directory of the Nautilus window with this title (its folder name), or None.
    D-Bus does not say which niri window a location belongs to, so match by name."""
    dirs = nautilus_dirs()
    named = [d for d in dirs if os.path.basename(os.path.expanduser(d)) == title]
    if named:
        return named[0]
    return dirs[0] if len(dirs) == 1 else None


def window_dir(window):
    """Directory of a Ghostty or Nautilus window, or None."""
    if window["app_id"] == GHOSTTY:
        # The terminal that runs niri-layout-manager shows the command in its title, not
        # its directory. That terminal is the focused Ghostty window.
        if window["is_focused"] and window["pid"] in ancestors():
            return own_dir()
        return ghostty_path(window["title"])
    if window["app_id"] == NAUTILUS:
        return nautilus_path(window["title"])
    return None


def skip_reason(window, cfg):
    """Why a window is not part of layouts, or None if it is."""
    if window["is_floating"]:
        return "floating"
    for rule in cfg["ignore"]:
        if all(
            re.search(rule[k], window.get(k) or "")
            for k in ("app_id", "title")
            if k in rule
        ):
            return f"ignore rule {rule}"
    parent = parent_comm(window["pid"])
    if parent not in ("init", *cfg["launchers"]):
        return f"started by another process ({parent})"
    return None


# --- capture --------------------------------------------------------------


def desktop_index():
    """Lower-case desktop id and StartupWMClass -> desktop id."""
    data_home = os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))
    data_dirs = os.environ.get("XDG_DATA_DIRS", "/usr/local/share:/usr/share")
    index = {}
    for base in reversed([data_home, *data_dirs.split(":")]):
        apps = Path(base) / "applications"
        if not apps.is_dir():
            continue
        for path in apps.rglob("*.desktop"):
            desktop_id = (
                str(path.relative_to(apps)).replace("/", "-").removesuffix(".desktop")
            )
            index[desktop_id.lower()] = desktop_id
            try:
                text = path.read_text(errors="replace")
            except OSError:
                continue
            if m := re.search(r"^StartupWMClass=(.+)$", text, re.MULTILINE):
                index[m.group(1).strip().lower()] = desktop_id
    return index


def launch_spec(window, desktops):
    if window["app_id"] in DIR_APPS:
        return {"dir": window_dir(window)}
    desktop_id = desktops.get((window["app_id"] or "").lower())
    if desktop_id:
        return {"desktop": desktop_id}
    proc = Path(f"/proc/{window['pid']}")
    argv = [
        a.decode(errors="replace")
        for a in (proc / "cmdline").read_bytes().rstrip(b"\0").split(b"\0")
    ]
    return {"argv": argv, "cwd": os.readlink(proc / "cwd")}


def capture(cfg):
    """Return (layout, skipped) for the current windows."""
    outs = outputs()
    workspaces = {w["id"]: w for w in query("Workspaces")}
    desktops = desktop_index()
    skipped = []
    by_ws = defaultdict(list)
    for window in query("Windows"):
        if window["workspace_id"] is None:
            continue
        reason = skip_reason(window, cfg)
        if reason:
            skipped.append((window, reason))
        else:
            by_ws[window["workspace_id"]].append(window)

    used_outputs = {}
    layout_workspaces = []
    for ws in sorted(workspaces.values(), key=lambda w: (w["output"] or "", w["idx"])):
        windows = by_ws.get(ws["id"])
        if not windows or ws["output"] not in outs:
            continue
        out = outs[ws["output"]]
        key = output_key(out)
        used_outputs[key] = {
            "name": ws["output"],
            "laptop": is_laptop(ws["output"]),
            "width": out["logical"]["width"],
            "height": out["logical"]["height"],
        }
        columns = defaultdict(list)
        for w in windows:
            col, row = w["layout"]["pos_in_scrolling_layout"]
            # TODO: investigate removing width/height; niri window-rules may already set sizes.
            width, height = w["layout"]["window_size"]
            columns[col].append(
                (
                    row,
                    {
                        "app_id": w["app_id"],
                        "launch": launch_spec(w, desktops),
                        "width": width,
                        "height": height,
                    },
                )
            )
        layout_workspaces.append(
            {
                "output": key,
                "name": ws["name"],
                "idx": ws["idx"],
                "columns": [
                    {
                        "index": c,
                        "windows": [
                            s for _, s in sorted(columns[c], key=lambda rs: rs[0])
                        ],
                    }
                    for c in sorted(columns)
                ],
            }
        )
    return {
        "version": LAYOUT_VERSION,
        "outputs": used_outputs,
        "workspaces": layout_workspaces,
    }, skipped


# --- names and files ------------------------------------------------------


def guess_save_name(layout):
    names = [o["laptop"] for o in layout["outputs"].values()]
    return "single" if names == [True] or not names else "double"


def guess_load_name():
    return "single" if len(outputs()) <= 1 else "double"


def layout_path(name):
    return CONFIG_DIR / f"{name}.json"


def read_layout(name):
    path = layout_path(name)
    if not path.exists():
        sys.exit(f"niri-layout-manager: no saved layout {name!r} ({path})")
    layout = json.loads(path.read_text())
    if layout.get("version") != LAYOUT_VERSION:
        sys.exit(
            f"niri-layout-manager: {path} has an old format; run niri-layout-manager save {name} again"
        )
    return layout


def slot_label(slot):
    launch = slot["launch"]
    if "dir" in launch:
        return f"{DIR_APPS[slot['app_id']]}[{launch['dir'] or '?'}]"
    return slot["app_id"]


def window_label(window):
    """App id; for Ghostty and Nautilus a short name and the directory."""
    if window["app_id"] in DIR_APPS:
        return f"{DIR_APPS[window['app_id']]}[{window_dir(window) or '?'}]"
    return window["app_id"]


# --- matching -------------------------------------------------------------


def slot_matches(slot, window, loose=False):
    """True if window can fill slot. With `loose`, a Ghostty window whose
    directory is not known fills a Ghostty slot with any directory."""
    if slot["app_id"] != window["app_id"]:
        return False
    if slot["launch"].get("dir"):
        path = window_dir(window)
        if path is None:
            return loose and window["app_id"] == GHOSTTY
        return os.path.expanduser(path) == os.path.expanduser(slot["launch"]["dir"])
    return True


def all_slots(layout):
    """(workspace index, column index, row index, slot) for every slot.
    Indexes are positions in the layout lists, not niri's indexes."""
    for wi, ws in enumerate(layout["workspaces"]):
        for ci, column in enumerate(ws["columns"]):
            for ri, slot in enumerate(column["windows"]):
                yield wi, ci, ri, slot


def target_workspaces(layout):
    """(connector name, niri workspace index) for each saved workspace.

    Workspaces of a connected monitor keep their saved index. Workspaces of a
    missing monitor go after the last saved workspace of the fallback monitor."""
    mapping = output_mapping(layout)
    present = {output_key(o) for o in outputs().values()}
    top = defaultdict(int)
    for ws in layout["workspaces"]:
        if ws["output"] in present:
            top[mapping[ws["output"]]] = max(top[mapping[ws["output"]]], ws["idx"])
    targets = []
    for ws in layout["workspaces"]:
        out = mapping[ws["output"]]
        if ws["output"] in present:
            targets.append((out, ws["idx"]))
        else:
            top[out] += 1
            targets.append((out, top[out]))
    return targets


class Address(NamedTuple):
    """Where a window is. Numbers are niri's indexes; column 0 means floating.
    Tuple order sorts by monitor, workspace, column."""

    output: str
    workspace: int
    column: int

    def __str__(self):
        return f"{self.output}:w{self.workspace}:" + (
            f"c{self.column}" if self.column else "float"
        )


def address(window, workspaces):
    ws = workspaces[window["workspace_id"]]
    column = (
        0 if window["is_floating"] else window["layout"]["pos_in_scrolling_layout"][0]
    )
    return Address(ws["output"], ws["idx"], column)


def actual_positions(windows):
    """Window id -> address."""
    workspaces = {w["id"]: w for w in query("Workspaces")}
    return {w["id"]: address(w, workspaces) for w in windows}


def match_windows(layout, windows, targets=None, actual=None):
    """Map slot position -> window id; also return unmatched windows.

    Preference: already on the target workspace, then any window of the app.
    Without `targets`/`actual`, position is not used. Slots with no directory
    match any window of their app, so they go last in each pass. Ghostty
    windows with no known directory fill Ghostty slots last."""
    free = {w["id"]: w for w in windows}
    matched = {}
    blank = lambda s: "dir" in s["launch"] and not s["launch"]["dir"]
    slots = sorted(all_slots(layout), key=lambda p: blank(p[3]))

    def placed(pos, wid):
        if targets is None or actual is None:
            return False
        return actual[wid][:2] == targets[pos[0]]

    for loose in (False, True):
        for need_place in (True, False):
            for wi, ci, ri, slot in slots:
                pos = (wi, ci, ri)
                if pos in matched:
                    continue
                for wid, w in free.items():
                    if slot_matches(slot, w, loose) and (
                        not need_place or placed(pos, wid)
                    ):
                        matched[pos] = wid
                        del free[wid]
                        break
    return matched, list(free.values())


# --- placement ------------------------------------------------------------


def output_mapping(layout):
    """Saved output key -> current connector name. Missing monitors go to the laptop."""
    outs = outputs()
    by_key = {output_key(o): name for name, o in outs.items()}
    fallback = next((n for n in outs if is_laptop(n)), next(iter(outs)))
    return {key: by_key.get(key, fallback) for key in layout["outputs"]}


def width_change(saved_output, target_output, px):
    same = (saved_output["width"], saved_output["height"]) == (
        target_output["logical"]["width"],
        target_output["logical"]["height"],
    )
    if same:
        return {"SetFixed": int(px)}
    return {"SetProportion": round(100 * px / saved_output["width"], 1)}


def height_change(saved_output, target_output, px):
    same = (saved_output["width"], saved_output["height"]) == (
        target_output["logical"]["width"],
        target_output["logical"]["height"],
    )
    if same:
        return {"SetFixed": int(px)}
    return {"SetProportion": round(100 * px / saved_output["height"], 1)}


def window_by_id(wid):
    return next(w for w in query("Windows") if w["id"] == wid)


def position(wid):
    """(workspace id, niri column index) of a window; column 0 means floating."""
    w = window_by_id(wid)
    col = w["layout"]["pos_in_scrolling_layout"]
    return w["workspace_id"], col[0] if col else 0


def column_members(ws_id, col):
    return {
        w["id"]
        for w in query("Windows")
        if w["workspace_id"] == ws_id
        and not w["is_floating"]
        and w["layout"]["pos_in_scrolling_layout"][0] == col
    }


def target_workspace(output_name, idx, name):
    """Workspace id for niri workspace index idx on output_name (or the named
    workspace). If the output has fewer workspaces, use its last (empty) one."""
    workspaces = query("Workspaces")
    if name:
        named = next((w for w in workspaces if w["name"] == name), None)
        if named:
            return named["id"]
    on_output = sorted(
        (w for w in workspaces if w["output"] == output_name), key=lambda w: w["idx"]
    )
    return next((w for w in on_output if w["idx"] == idx), on_output[-1])["id"]


def bring(wid, ws_id, allowed):
    """Make wid a tiled window on ws_id, in a column with only `allowed`
    windows. Return True if anything changed."""
    changed = False
    window = window_by_id(wid)
    if window["is_floating"]:
        action("MoveWindowToTiling", id=wid)
        changed = True
    if window["workspace_id"] != ws_id:
        action(
            "MoveWindowToWorkspace", window_id=wid, reference={"Id": ws_id}, focus=False
        )
        changed = True
    if not column_members(*position(wid)) <= allowed:
        action("ConsumeOrExpelWindowRight", id=wid)
        changed = True
    return changed


def move_column_after(wid, after):
    """Move the column of wid to just right of niri column `after` (0 = first)."""
    _, col = position(wid)
    action("FocusWindow", id=wid)
    action("MoveColumnToIndex", index=after if col < after else after + 1)


def place_column(ws_id, wids, prev, width):
    """Put the windows `wids` (top to bottom) in one column on ws_id, right of
    niri column `prev`. A column that is already right of `prev` stays where it
    is, so windows that are not in the layout do not cause moves. Return True
    if anything changed."""
    first, *rest = wids
    allowed = set(wids)
    changed = bring(first, ws_id, allowed)
    if changed or position(first)[1] <= prev:
        move_column_after(first, prev)
        action("SetColumnWidth", change=width)
        changed = True
    for wid in rest:
        if position(wid) == position(first):
            continue
        bring(wid, ws_id, allowed)
        move_column_after(wid, position(first)[1])
        action("ConsumeOrExpelWindowLeft", id=wid)
        changed = True
    return changed


# --- spawning -------------------------------------------------------------


def spawn(slot):
    launch = slot["launch"]
    if "dir" in launch:
        cwd = launch["dir"] and os.path.expanduser(launch["dir"])
        if slot["app_id"] == GHOSTTY:
            action(
                "Spawn",
                command=[
                    "ghostty",
                    "+new-window",
                    *([f"--working-directory={cwd}"] if cwd else []),
                ],
            )
        else:
            action(
                "Spawn", command=["nautilus", "--new-window", *([cwd] if cwd else [])]
            )
    elif "desktop" in launch:
        action("Spawn", command=["gtk-launch", launch["desktop"]])
    else:
        action(
            "SpawnSh",
            command=f"cd {shlex.quote(launch['cwd'])} && exec {shlex.join(launch['argv'])}",
        )


def wait_for_windows(app_id, known_ids, count, timeout=SPAWN_TIMEOUT):
    """Wait until `count` new windows of app_id exist; return what appeared."""
    deadline = time.monotonic() + timeout
    new = []
    while time.monotonic() < deadline:
        new = [
            w
            for w in query("Windows")
            if w["app_id"] == app_id and w["id"] not in known_ids
        ]
        if len(new) >= count:
            break
        time.sleep(0.1)
    return new


# --- output ---------------------------------------------------------------


def style(code, text):
    """Text with SGR style `code` if stdout is a terminal and NO_COLOR is not set."""
    if not sys.stdout.isatty() or "NO_COLOR" in os.environ:
        return text
    return f"\033[{code}m{text}\033[0m"


def dim(text):
    return style(2, text)


def cyan(text):
    return style("1;36", text)


def green(text):
    return style(32, text)


def red(text):
    return style(31, text)


def header(name):
    return f"layout {cyan(name)}  ({layout_path(name)})"


# --- commands -------------------------------------------------------------


def cmd_show(name, cfg):
    layout = read_layout(name)
    print(header(name))
    for key, out in layout["outputs"].items():
        print(f"\n{out['name']}  {key}  {out['width']}x{out['height']}")
        for ws in (w for w in layout["workspaces"] if w["output"] == key):
            print(f"  workspace {ws['name'] or ws['idx']}")
            for column in ws["columns"]:
                pct = round(100 * column["windows"][0]["width"] / out["width"])
                print(f"    column {column['index']} ({pct}%)")
                for slot in column["windows"]:
                    print(f"      {slot_label(slot)}")


def cmd_save(name, cfg):
    layout, skipped = capture(cfg)
    name = name or guess_save_name(layout)
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    layout_path(name).write_text(json.dumps(layout, indent=2) + "\n")
    count = sum(1 for _ in all_slots(layout))
    print(f"saved {count} windows to {layout_path(name)}")
    workspaces = {w["id"]: w for w in query("Workspaces")}
    for window, reason in sorted(skipped, key=lambda wr: address(wr[0], workspaces)):
        print(
            f"  skipped  {window_label(window)}  {address(window, workspaces)}  ({reason})"
        )


def cmd_diff(name, cfg):
    layout = read_layout(name)
    windows = [w for w in query("Windows") if w["workspace_id"] is not None]
    skipped = [w for w in windows if skip_reason(w, cfg)]
    candidates = [w for w in windows if not skip_reason(w, cfg)]
    targets = target_workspaces(layout)
    actual = actual_positions(windows)
    matched, extra = match_windows(layout, candidates, targets, actual)

    by_id = {w["id"]: w for w in candidates}
    workspaces = {w["id"]: w for w in query("Workspaces")}
    rows = []  # (status, current Address or None, target Address or None, label)
    # Same rule as place_column: a saved column is in place if its first window
    # is on the target workspace, right of the previous column in place, and
    # shares its niri column only with windows of the same saved column.
    for wi, ws in enumerate(layout["workspaces"]):
        out, idx = targets[wi]
        prev = pending = 0
        for ci, column in enumerate(ws["columns"]):
            wids = [matched.get((wi, ci, ri)) for ri in range(len(column["windows"]))]
            present = [wid for wid in wids if wid is not None]
            anchor = None
            if present:
                a = actual[present[0]]
                members = {wid for wid, addr in actual.items() if addr == a}
                if a[:2] == (out, idx) and a.column > prev and members <= set(present):
                    anchor = prev = a.column
                    pending = 0
                elif a[:2] == (out, idx) and a.column < prev:
                    prev -= 1  # its column leaves from the left of prev
            if anchor is None:
                pending += 1
            target = Address(out, idx, anchor or prev + pending)
            for slot, wid in zip(column["windows"], wids, strict=True):
                if wid is None:
                    rows.append(("missing", None, target, slot_label(slot)))
                    continue
                w = by_id[wid]
                label = window_label(w) if window_dir(w) else slot_label(slot)
                if actual[wid] == target:
                    rows.append(("ok", target, None, label))
                else:
                    rows.append(("moved", actual[wid], target, label))
    for w in extra:
        rows.append(("new", address(w, workspaces), None, window_label(w)))
    for w in skipped:
        rows.append(("ignored", address(w, workspaces), None, window_label(w)))

    print(header(name))
    group = None
    for status, current, target, label in sorted(rows, key=lambda r: r[1] or r[2]):
        sort_key = current or target
        if sort_key[:2] != group:
            group = sort_key[:2]
            print(f"------ workspace {sort_key.workspace} ------")
        if current is None:
            where = f"-> {target}"
        elif target is not None:
            where = f"{current} -> {target}"
        else:
            where = str(current)
        line = f"  {status:<8} {label}  {where}"
        if status == "ignored":
            print(dim(line))
        elif status == "missing":
            print(red(line))
        elif status == "new":
            print(green(line))
        else:
            print(line)
    statuses = {r[0] for r in rows}
    if statuses - {"ok", "ignored"}:
        print("\nto keep the current windows:")
        print(cyan(f"niri-layout-manager save {name}"))
        sys.exit(2 if "missing" in statuses else 1)


def cmd_load(name, cfg):
    layout = read_layout(name)
    windows = [
        w
        for w in query("Windows")
        if w["workspace_id"] is not None and not skip_reason(w, cfg)
    ]
    matched, _ = match_windows(
        layout, windows, target_workspaces(layout), actual_positions(windows)
    )
    slots = list(all_slots(layout))

    # Start missing apps. Self-restoring apps start once if none of their windows are open.
    missing = [
        ((wi, ci, ri), s) for wi, ci, ri, s in slots if (wi, ci, ri) not in matched
    ]
    open_apps = {w["app_id"] for w in query("Windows")}
    by_app = defaultdict(list)
    for pos, slot in missing:
        by_app[slot["app_id"]].append((pos, slot))
    for app_id, items in by_app.items():
        if app_id in cfg["self_restoring"]:
            if app_id in open_apps:
                continue
            known = {w["id"] for w in query("Windows")}
            spawn(items[0][1])
            new = wait_for_windows(app_id, known, len(items))
            time.sleep(SELF_RESTORE_SETTLE)
            new = wait_for_windows(app_id, known, len(items), timeout=0.1) or new
            sub = {
                "version": LAYOUT_VERSION,
                "outputs": layout["outputs"],
                "workspaces": [
                    {
                        "output": "",
                        "name": None,
                        "idx": 1,
                        "columns": [
                            {"index": i + 1, "windows": [s]}
                            for i, (_, s) in enumerate(items)
                        ],
                    }
                ],
            }
            sub_matched, _ = match_windows(sub, new)
            for (_, ci, _), wid in sub_matched.items():
                matched[items[ci][0]] = wid
            continue
        for pos, slot in items:
            known = {w["id"] for w in query("Windows")}
            spawn(slot)
            new = wait_for_windows(app_id, known, 1)
            if new:
                matched[pos] = new[0]["id"]
            else:
                print(
                    f"niri-layout-manager: no window for {slot_label(slot)} after {SPAWN_TIMEOUT:.0f} s",
                    file=sys.stderr,
                )

    # Place windows, one saved workspace at a time.
    targets = target_workspaces(layout)
    outs = outputs()
    for wi, ws in sorted(
        enumerate(layout["workspaces"]), key=lambda iw: targets[iw[0]]
    ):
        out_name, idx = targets[wi]
        saved_output = layout["outputs"][ws["output"]]
        target_output = outs[out_name]
        ids = [
            matched.get((wi, ci, ri))
            for ci, col in enumerate(ws["columns"])
            for ri in range(len(col["windows"]))
        ]
        if not any(ids):
            continue
        ws_id = target_workspace(out_name, idx, ws["name"])
        prev = 0
        for ci, column in enumerate(ws["columns"]):
            placed = [
                (wid, slot)
                for ri, slot in enumerate(column["windows"])
                if (wid := matched.get((wi, ci, ri))) is not None
            ]
            if not placed:
                continue
            width = width_change(saved_output, target_output, placed[0][1]["width"])
            changed = place_column(ws_id, [wid for wid, _ in placed], prev, width)
            if changed and len(placed) > 1:
                for wid, slot in placed:
                    action(
                        "SetWindowHeight",
                        id=wid,
                        change=height_change(
                            saved_output, target_output, slot["height"]
                        ),
                    )
            prev = position(placed[0][0])[1]
    print(f"loaded {name}")


def main():
    parser = argparse.ArgumentParser(
        prog="niri-layout-manager",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("command", choices=["show", "save", "load", "diff"])
    parser.add_argument("name", nargs="?")
    args = parser.parse_args()
    cfg = load_config()
    try:
        if args.command == "save":
            cmd_save(args.name, cfg)
        else:
            name = args.name or guess_load_name()
            {"show": cmd_show, "load": cmd_load, "diff": cmd_diff}[args.command](
                name, cfg
            )
    except NiriError as e:
        sys.exit(f"niri-layout-manager: {e}")
