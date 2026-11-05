"""WT Map Ping: places War Thunder aiming points (Alt+click on the tactical map) on Nuclear
Escalation targets such as SAM sites, radars, depots and airfields. Windows only. See README.md."""
import ctypes
import json
import math
import queue
import re
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import urllib.request
import webbrowser
from contextlib import contextmanager
from pathlib import Path
from tkinter import ttk

API = 'http://127.0.0.1:8111/'  # the game's local map API; 'localhost' would try IPv6 first (~2 s each)
SITE = 'https://majorlier.github.io/nuclear-thunder-interactive-map/'
DONATE = 'https://buymeacoffee.com/lorenzogt'
MAPS = {'Tropical Coast': 'map_data.json', 'Southeastern City': 'map_data_city.json',
        'Volcano Valley': 'map_data_kamchatka.json'}  # standard missions; the mirrored ones only swap teams
KINDS = {  # target type: (label, regex on the site's object names, whose t1_/t2_ prefix is the team)
    'long_sam': ('Long-range SAM sites', r'_long_sam_site_\d+$'),
    'short_sam': ('Short-range SAM sites', r'_short_sam_site_\d+$'),
    'radar': ('Early-warning radars', r'_longrange_radar_place_\d+$'),
    'airfield': ('Airfields (current owner)', None),  # read live from the map: airfields get captured
    'depot': ('Vehicle depots (current owner)', r'_base_\d+$'),
    'storage': ('Fuel and ammo storage (current owner)', r'_(fuel|ammo)_storage'),
    'strongpoint': ('Strongpoints (starting owner)', r'_strongpoint_\d+$'),
}  # TBM / MLRS launchers are left out on purpose: they drive kilometres away from where they spawn
LIVE_ICONS = {'depot': ('TankStorage',), 'storage': ('FuelStorage', 'AmmoStorage')}  # drawn on the spot, owner's colour
HERE = Path(__file__).resolve().parent
SITES_FILE, CALIB_FILE, SETTINGS_FILE = (HERE / n for n in ('sites_cache.json', 'calibration.json', 'settings.json'))
DEFAULTS = {'place_key': 0x77, 'clear_key': 0x23, 'calibrate_key': 0x79, 'map_key': 0x4D,  # F8, End, F10, M
            'kinds': ['long_sam', 'short_sam'], 'max_points': 10}  # the game keeps at most 10 aiming points
ALT = 0xA4  # left Alt: Alt+left click on the tactical map adds an aiming point
STEP = 0.05  # seconds between input events (2-3 frames); raise it if clicks get lost at low FPS
ZOOM_NOTCHES = 80  # wheel notches that zoom the map all the way out even from its closest zoom
ZOOM_IN_NOTCHES = 6  # F8 zooms in this much on each target, for a click finer than one zoomed-out pixel
SETTLE = 0.5  # seconds for the zoom animation after zooming in or out on a target; raise it if F8 misses

user32 = ctypes.windll.user32
user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # real pixels, like the game
user32.GetForegroundWindow.restype = ctypes.c_void_p
GAP = user32.GetDoubleClickTime() / 1000 + 0.1  # closer clicks count as a double-click: the game drops the 2nd
messages = queue.Queue()  # ('log', text) or ('status', (state, text)) for the window
busy = threading.Event()  # set while an action is sending input to the game
calib_shift = [0, 40]  # px: the first calibration click alternates between two spots, so a retry always
                       # moves the point of interest even if the last try left it on the first spot
placed = {}  # calibration key: map points the tool Alt+clicked this battle, so the clear key can remove them


def say(text):
    print(text)  # to the console when there is one (under pythonw print does nothing)
    messages.put(('log', text))


# ---------------------------------------------------------------- simulated input (Win32 SendInput)

class MOUSEINPUT(ctypes.Structure):
    _fields_ = [('dx', ctypes.c_long), ('dy', ctypes.c_long), ('data', ctypes.c_long),
                ('flags', ctypes.c_ulong), ('time', ctypes.c_ulong), ('extra', ctypes.c_size_t)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [('vk', ctypes.c_ushort), ('scan', ctypes.c_ushort), ('flags', ctypes.c_ulong),
                ('time', ctypes.c_ulong), ('extra', ctypes.c_size_t)]


class INPUTUNION(ctypes.Union):
    _fields_ = [('mi', MOUSEINPUT), ('ki', KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [('type', ctypes.c_ulong), ('u', INPUTUNION)]


def send(kind, **event):
    if not user32.SendInput(1, ctypes.byref(INPUT(kind, INPUTUNION(**event))), ctypes.sizeof(INPUT)):
        raise ctypes.WinError()


def scancode(vk):
    """The game reads scancodes, not virtual keys (0xE0xx = extended). For the arrows, Insert, Delete,
    Home, End, PgUp and PgDn Windows answers with their numpad twins, so they get the E0 back here."""
    return user32.MapVirtualKeyW(vk, 4) | (0xE000 if 0x21 <= vk <= 0x28 or vk in (0x2D, 0x2E) else 0)


def key(vk, up=False):
    scan = scancode(vk)
    flags = 0x8 | (0x1 if scan >> 8 == 0xE0 else 0) | (0x2 if up else 0)  # SCANCODE [|EXTENDEDKEY] [|KEYUP]
    send(1, ki=KEYBDINPUT(scan=scan & 0xFF, flags=flags))


def mouse(flags, dx=0, dy=0, data=0):
    send(0, mi=MOUSEINPUT(dx=dx, dy=dy, data=data, flags=flags))


def click(down, up, gap=True):
    """Press and release a mouse button. Returns where the cursor was at the release: checking later
    could blame the player for the game re-centring the cursor when the map closes."""
    mouse(down)
    try:
        time.sleep(STEP)
    finally:
        mouse(up)
    at = cursor()
    if gap:
        time.sleep(GAP)
    return at


def move_to(x, y):
    """Put the cursor on pixel (x, y) in a way the game notices: it only re-reads the cursor on a
    mouse move whose (truncated) delta is non-zero, so a bare SetCursorPos can click at the old spot."""
    vx, vy, vw, vh = (user32.GetSystemMetrics(i) for i in (76, 77, 78, 79))  # whole virtual desktop
    for px in (x + 2, x):  # absolute moves skip mouse acceleration; a 2 px step always leaves a delta
        mouse(0xC001, (px - vx) * 65536 // vw + 1, (y - vy) * 65536 // vh + 1)  # MOVE|VIRTUALDESK|ABSOLUTE
    user32.SetCursorPos(x, y)  # pin the exact pixel in case Windows rounded the absolute move
    time.sleep(STEP)


def cursor():
    point = (ctypes.c_long * 2)()
    user32.GetCursorPos(point)
    return point[0], point[1]


def held(vk):
    return bool(user32.GetAsyncKeyState(vk) & 0x8000)


def key_name(vk):
    scan = {0x13: 0x45, 0x90: 0xE045}.get(vk) or scancode(vk)  # Windows mixes up Pause and Num Lock here
    name = ctypes.create_unicode_buffer(32)
    user32.GetKeyNameTextW((scan & 0xFF) << 16 | (1 << 24 if scan >> 8 == 0xE0 else 0), name, 32)
    return name.value or f'key {vk}'


def game_focused():
    name = ctypes.create_unicode_buffer(32)
    user32.GetClassNameW(ctypes.c_void_p(user32.GetForegroundWindow()), name, 32)
    return name.value == 'DagorWClass'  # War Thunder's window


def map_up(map_vk):
    """Tactical map open in front of us. Otherwise the wheel is the throttle and moves steer the plane."""
    return held(map_vk) and game_focused()


@contextmanager
def map_open(map_vk):
    """Hold the map key meanwhile, unless the player already holds it (WT default: hold to show)."""
    mine = not held(map_vk)
    try:
        if mine:
            key(map_vk)
            time.sleep(0.5)  # the map opens, then the game parks the cursor; let that finish first
        yield
    finally:
        if mine:
            key(map_vk, up=True)


def wheel(map_vk, x, y, notches, settle):
    """Turn the mouse wheel with the cursor on (x, y): positive notches zoom the map in around the cursor,
    negative ones zoom it out. Then wait `settle` s for the zoom animation. False if the map is (or gets)
    closed, so no notch reaches the throttle."""
    if not map_up(map_vk):
        return False
    move_to(x, y)
    for _ in range(abs(notches)):
        if not map_up(map_vk):
            return False
        mouse(0x0800, data=120 if notches > 0 else -120)  # MOUSEEVENTF_WHEEL, one notch
        time.sleep(0.008)  # a fast spin of a real wheel
    time.sleep(settle)
    return map_up(map_vk)


def zoom_out(map_vk, x, y):
    """Wheel the map all the way out, from any zoom: extra notches do nothing once the whole map shows."""
    return wheel(map_vk, x, y, -ZOOM_NOTCHES, 0.8)


def game_area():
    """War Thunder's client area as (left, top, right, bottom) screen pixels (the game is in front)."""
    hwnd = ctypes.c_void_p(user32.GetForegroundWindow())
    rect, origin = (ctypes.c_long * 4)(), (ctypes.c_long * 2)()
    user32.GetClientRect(hwnd, rect)
    user32.ClientToScreen(hwnd, origin)
    return origin[0], origin[1], origin[0] + rect[2], origin[1] + rect[3]


# ---------------------------------------------------------------- game API, target data, geometry

def api(path):
    with urllib.request.urlopen(API + path, timeout=2) as response:
        return json.load(response)


def save_json(path, data):
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(data, indent=1))
    temp.replace(path)  # atomic: a crash never leaves a half-written file


def load_sites():
    """{map: {'airfields': [[team, x, z]], 'targets': {kind: [[team, x, z]]}}} from the interactive
    map, refreshed once a day and cached so the tool also works when the site is unreachable."""
    if SITES_FILE.exists() and time.time() - SITES_FILE.stat().st_mtime < 86400:
        return json.loads(SITES_FILE.read_text())
    try:
        sites = {}
        for name, file in MAPS.items():
            with urllib.request.urlopen(SITE + file, timeout=30) as response:
                rows = json.load(response)
            pick = lambda keep: [[s['team'], s['world_pos'][0], s['world_pos'][2]] for s in rows if keep(s)]
            sites[name] = {'airfields': pick(lambda s: s.get('category') == 'airfield'),
                           'targets': {kind: pick(lambda s: re.search(rx, s['name']))
                                       for kind, (_, rx) in KINDS.items() if rx}}
            if not sites[name]['airfields'] or not all(sites[name]['targets'].values()):
                raise ValueError(f'{file} lacks airfields or a target type (site format changed?)')
        save_json(SITES_FILE, sites)
    except Exception as e:
        if not SITES_FILE.exists():
            raise
        say(f'Could not refresh the map data from the website ({e!r}); using the cached copy.')
        sites = json.loads(SITES_FILE.read_text())
    return sites


def to_uv(info, x, z):
    """World (x, z) -> fraction of the game's map image: origin top-left, north (+z) up."""
    (x0, z0), (x1, z1) = info['map_min'], info['map_max']
    return (x - x0) / (x1 - x0), (z1 - z) / (z1 - z0)


def colour(o):
    """> 100 for our (blue) objects, < -100 for enemy (red) ones, near 0 for neutral (white) ones.
    Assumes default-ish colours: blue-dominant ally, red-dominant enemy."""
    r, _, b = o['color[]']
    return b - r


def matched_airfields(info, objs, site):
    """(map position, game object, site team) for every airfield the game shows that is on this map."""
    known = [(to_uv(info, x, z), team) for team, x, z in site['airfields']]
    found = []
    for o in objs:
        if o['type'] != 'airfield' or (o['sx'], o['sy']) == (o['ex'], o['ey']):
            continue  # unused airfields sit at (0.5, 0.5)
        p = ((o['sx'] + o['ex']) / 2, (o['sy'] + o['ey']) / 2)
        distance, team = min((math.dist(p, q), team) for q, team in known)
        if distance < 0.01:  # airfields are tens of km apart; 0.01 of the map is 1.3-2.6 km
            found.append((p, o, team))
    return found


def detect(info, objs, sites):
    """(map, enemy team), else (None, None). The map is the one whose airfields line up with the
    game's. The enemy is the team whose SAM batteries are not drawn in our colour: ours always are
    and batteries never change hands. Airfields do (captures recolour them), so their colours only
    break a tie."""
    found = {name: matched_airfields(info, objs, site) for name, site in sites.items()}
    name = max(found, key=lambda n: len(found[n]))
    if len(found[name]) < 3:
        return None, None
    ours = [(o['x'], o['y']) for o in objs if o['type'] == 'ground_model' and colour(o) > 100]
    sams = sites[name]['targets']['long_sam'] + sites[name]['targets']['short_sam']
    covered = {t: sum(any(math.dist(to_uv(info, x, z), p) < 0.002 for p in ours)  # units sit <150 m out
                      for team, x, z in sams if team == t) for t in (1, 2)}
    if covered[1] != covered[2]:
        return name, min(covered, key=covered.get)
    votes = [team if colour(o) < -100 else 3 - team for _, o, team in found[name] if abs(colour(o)) > 100]
    if votes.count(1) == votes.count(2):
        return None, None
    return name, max((1, 2), key=votes.count)


def player(objs):
    return next(((o['x'], o['y']) for o in objs if o.get('icon') == 'Player'), (0.5, 0.5))


def pick_targets(info, objs, site, enemy, kinds, limit):
    """Map positions (u, v) of the chosen enemy target types, nearest to the player first. The AI
    captures airfields, depots and storage, so their owner comes from the live map colours."""
    points = []
    for kind in kinds:
        icons = [((o['x'], o['y']), o) for o in objs
                 if o['type'] == 'ground_model' and o.get('icon') in LIVE_ICONS.get(kind, ())]
        for team, x, z in site['targets'].get(kind, []):
            p = to_uv(info, x, z)
            live = [o for q, o in icons if math.dist(p, q) < 0.0005]  # drawn right on the spot
            if colour(live[0]) < -100 if live else team == enemy:  # no icon: fall back to the mission team
                points.append(p)
    if 'airfield' in kinds:
        points += [p for p, o, _ in matched_airfields(info, objs, site) if colour(o) < -100]
    me = player(objs)
    return sorted(points, key=lambda p: math.dist(p, me))[:limit]


def solve(p1, p2):
    """Two (pixel x, pixel y, u, v) readings -> [left, top, width, height] of the map image on screen."""
    (x1, y1, u1, v1), (x2, y2, u2, v2) = p1, p2
    width, height = (x2 - x1) / (u2 - u1), (y2 - y1) / (v2 - v1)
    return [x1 - u1 * width, y1 - v1 * height, width, height]


def calib_key(name, info):
    return f"{name} {info['grid_zero']} {info['grid_size']}"  # a mirrored mission may frame it differently


def load_calibration():
    return json.loads(CALIB_FILE.read_text()) if CALIB_FILE.exists() else {}


def load_settings():
    settings = dict(DEFAULTS)
    try:
        saved = json.loads(SETTINGS_FILE.read_text()) if SETTINGS_FILE.exists() else {}
        settings.update({k: saved[k] for k in DEFAULTS if k in saved})
        keys = [settings[k] for k in DEFAULTS if k.endswith('_key')]
        if any(type(vk) is not int or not 0 < vk < 256 for vk in keys):
            raise ValueError('hotkeys must be virtual-key codes, whole numbers like 119 for F8')
        if len(set(keys)) < len(keys):
            raise ValueError('two hotkeys share a key')
        settings['kinds'] = [kind for kind in settings['kinds'] if kind in KINDS]
        settings['max_points'] = min(10, max(1, int(settings['max_points'])))
    except (ValueError, TypeError, AttributeError, OverflowError) as e:  # a hand-edited file can hold anything
        say(f'settings.json is invalid ({e}); using the defaults.')
        settings = dict(DEFAULTS)
    return settings


# ---------------------------------------------------------------- the two hotkey actions

def read_battle(sites):
    """(map info, map objects, map name, enemy team) when flying on a known map, else None (says why)."""
    info, objs = api('map_info.json'), api('map_obj.json')
    if not (info.get('valid') and info.get('hud_type') == 0 and api('state').get('valid')):
        say('Not flying in a battle right now (spawn in an aircraft first).')
        return None
    name, enemy = detect(info, objs, sites)
    if not name:
        say('Could not tell the Nuclear Escalation map or the enemy side from the map objects.')
        return None
    return info, objs, name, enemy


def poi():
    return next(((o['x'], o['y']) for o in api('map_obj.json') if o['type'] == 'point_of_interest'), None)


def moved(point, before):
    """A new point of interest, not the old one wobbling (in flight it drifts a few metres per poll)."""
    return point is not None and (before is None or math.dist(point, before) > 0.005)


def new_poi(before, wait=4):
    """The point of interest once it has moved away from `before` (it shows up ~0.1 s after the click),
    or None after `wait` seconds."""
    deadline = time.time() + wait
    point = poi()
    while not moved(point, before) and time.time() < deadline:
        time.sleep(0.1)
        point = poi()
    return point if moved(point, before) else None


class Stop(Exception):
    """Ends an action before it clicks anything; the message tells the player why."""


def map_closed(map_vk):
    return Stop(f'Stopped: keep {key_name(map_vk)} held (War Thunder in front) until the tool finishes.')


def middle_click(map_vk, x, y, before):
    """Middle-click pixel (x, y), which moves the point of interest there, and read back where the game
    put it: (x, y, u, v), or None if no new point of interest showed up."""
    if not map_up(map_vk):
        raise map_closed(map_vk)
    move_to(x, y)
    if not map_up(map_vk):  # checked right before the press, so it never reaches the flight controls
        raise map_closed(map_vk)
    if click(0x20, 0x40) != (x, y):  # middle button down, up
        raise Stop("Stopped: the mouse moved. Don't touch it until the tool finishes.")
    point = new_poi(before)
    if not point:
        return None
    say(f'  middle-click at pixel {x},{y} -> point of interest at {point[0]:.6f},{point[1]:.6f}')
    return x, y, *point


def find_map(map_vk, pointed=None):
    """Where the map image is on screen right now, as [left, top, width, height], and the last reading.
    The zoomed-out map is not always in the same place (it depends on where you spawned), so every action
    measures it: two middle-clicks near the middle of the game window, which the map always covers, give
    two exact pixel <-> map readings. None if no point of interest appeared (the aircraft can't set one)."""
    left, top, right, bottom = game_area()
    calib_shift.reverse()
    middle = ((left + right) // 2 + calib_shift[0], (top + bottom) // 2 + calib_shift[0])
    step = round(0.15 * (bottom - top))  # px between the two clicks on each axis: a fifth of the map
    tries = [middle]
    if pointed and left < pointed[0] < right and top < pointed[1] < bottom and math.dist(pointed, middle) > 50:
        tries.append(pointed)  # only inside the game: a middle-click elsewhere could land in another app
    first = None
    for x, y in tries:
        first = middle_click(map_vk, x, y, poi())
        if first:
            break
        say(f'  middle-click at pixel {x},{y} set no point of interest')
    if not first:
        return None
    sx, sy = (1 if first[2] < 0.5 else -1), (1 if first[3] < 0.5 else -1)  # towards the map centre
    second = middle_click(map_vk, first[0] + sx * step, first[1] + sy * step, first[2:])
    if not second or min(abs(second[2] - first[2]), abs(second[3] - first[3])) < 0.01:
        raise Stop('The map readings make no sense: the second middle-click did not move the point of '
                   'interest as expected. Try again.')
    view = solve(first, second)
    if not (0.3 * (bottom - top) < view[3] < 5 * (bottom - top) and abs(view[2] / view[3] - 1) < 0.02):
        raise Stop(f'The map readings make no sense (map image would be {view[2]:.0f} x {view[3]:.0f} px at '
                   f'{view[0]:.0f},{view[1]:.0f}). Try again.')
    return view, second


def click_point(map_vk, view, zoom, notches, point, down, up, alt=True):
    """Click map point (u, v) of `view`. With zoom > 1 it first zooms in around the nearest pixel: the exact
    spot is then `zoom` times further from the cursor, so it can be hit `zoom` times more precisely. Then it
    zooms back out just as far, which restores `view`. Returns (pixel aimed at, cursor at the release): the
    two differ if the player touched the mouse, and the pixel is None if that happened before the click.
    Returns None if the map was closed before the click."""
    left, top, width, height = view
    px, py = left + point[0] * width, top + point[1] * height
    x, y = anchor = round(px), round(py)
    if zoom > 1:
        if not wheel(map_vk, *anchor, notches, SETTLE):
            return None
        if cursor() != anchor:  # moved while zooming in: the zoom is centred elsewhere, so don't click
            return None, cursor()
        x, y = round(x + (px - x) * zoom), round(y + (py - y) * zoom)
    if not map_up(map_vk):  # map closed: even a cursor move would steer the plane
        return None
    move_to(x, y)
    if not map_up(map_vk):
        return None
    if alt:
        key(ALT)
    try:
        time.sleep(STEP)
        if not map_up(map_vk):  # checked right before the press, so it never reaches the flight controls
            return None
        at = click(down, up, gap=False)
        time.sleep(STEP)
    finally:
        if alt:
            key(ALT, up=True)
    if at == (x, y):  # else the player touched the mouse: send nothing more
        if zoom > 1:
            if wheel(map_vk, *anchor, -notches, SETTLE) and cursor() != anchor:
                at = cursor()  # moved while zooming back out: the view may have shifted, so stop after this one
        else:
            time.sleep(GAP)  # the game drops the second click of a double-click
    return (x, y), at


def on_map(settings, key, points, down, up, precise):
    """The map part of F8 and End: open the map, zoom it all the way out, find where it is now, Alt+click each
    point (zooming in on it first if `precise` and calibrated) and put the point of interest back. Returns
    the points clicked and, if it stopped early, why. Raises Stop when it could not start."""
    map_vk = settings['map_key']
    place, clear = key_name(settings['place_key']), key_name(settings['clear_key'])
    calibration = load_calibration()
    zoom, notches = calibration.get('zoom', (1, 0))  # the point of interest goes back precisely even for End
    left, top, right, bottom = game_area()
    before = poi()
    clicked, skipped, why = [], 0, ''
    try:
        with map_open(map_vk):
            if not zoom_out(map_vk, (left + right) // 2, (top + bottom) // 2):
                raise map_closed(map_vk)
            found = find_map(map_vk)
            if found:
                view = calibration[key] = found[0]  # kept for an aircraft that cannot set a point of interest
                save_json(CALIB_FILE, calibration)
            elif key in calibration:
                view = calibration[key][:4]
                say('  No point of interest appeared, so the map is taken to be where it was last time. If the '
                    'points are off, use an aircraft that can set a point of interest.')
            else:
                raise Stop('No point of interest appeared, so the tool cannot find the map. Use an aircraft whose '
                           'middle-click on the map sets one (a red square).')
            on_screen = lambda p: (left + 10 <= view[0] + p[0] * view[2] <= right - 10
                                   and top + 10 <= view[1] + p[1] * view[3] <= bottom - 10)
            for point in points:
                if not on_screen(point):
                    skipped += 1  # a click there would land outside the game
                    continue
                result = click_point(map_vk, view, zoom if precise else 1, notches, point, down, up)
                if not result:
                    why = ' (stopped: the map closed or War Thunder lost focus)'
                    break
                aimed, at = result
                if aimed:
                    clicked.append(point)
                if aimed != at:
                    why = " (stopped: the mouse moved; don't touch it while the tool works)"
                    break
            target = found and (before or found[1][2:])  # no point of interest before: the click is only a check
            if target and not why and on_screen(target):  # put the point of interest back, checking the map
                result = click_point(map_vk, view, zoom, notches, target, 0x20, 0x40, alt=False)
                second = found[1][2:]  # new_poi can't see a click that lands where the point already is
                back = result and result[0] == result[1] and (new_poi(second, 2) if moved(target, second) else poi())
                if back and math.dist(back, target) * view[2] > (0.4 if zoom > 1 else 1):  # zoomed-out px
                    say(f'  The map moved while the tool worked, so the points can be off. Press {clear}, then '
                        f'{place} again, best while standing still.' if precise else
                        '  The map moved while the tool worked, so a right-click may have missed: remove any '
                        'aiming point still on the map with Alt+right-click.')
    finally:
        try:
            if before and moved(poi(), before):  # stopped before it was put back
                say('  Your point of interest was moved: set it again if you use it (CCRP).')
        except (OSError, ValueError):
            pass  # the game is gone: whatever stopped the run says why
    if skipped:
        why += f' ({skipped} skipped: off the game window)'
    return clicked, why


def place_points(settings, sites):
    battle = read_battle(sites)
    if not battle:
        return
    info, objs, name, enemy = battle
    targets = pick_targets(info, objs, sites[name], enemy, settings['kinds'], settings['max_points'])
    if not targets:
        say('No target types are selected.' if not settings['kinds'] else
            'None of the selected target types has an enemy target right now.')
        return
    try:
        clicked, why = on_map(settings, calib_key(name, info), targets, 0x2, 0x4, precise=True)  # left button
    except Stop as e:
        say(str(e))
        return
    placed.setdefault(calib_key(name, info), []).extend(clicked)
    say(f'{name}: Alt+clicked {len(clicked)} of {len(targets)} enemy targets (team {enemy}), nearest first{why}.')


def clear_points(settings, sites):
    """Alt+right-click (remove) the aiming points this tool placed. The game has no key that removes all of
    them, and it does not report aiming points, so points placed by hand are left alone."""
    battle = read_battle(sites)
    if not battle:
        return
    info, _, name, _ = battle
    points = placed.get(calib_key(name, info), [])
    if not points:
        say('No aiming points placed by this tool to clear. Remove points you placed yourself with '
            'Alt+right-click on the map.')
        return
    total = len(points)
    try:  # right button, without zooming in: the point icons are much bigger than a pixel
        clicked, why = on_map(settings, calib_key(name, info), points[::-1], 0x8, 0x10, precise=False)
    except Stop as e:
        say(str(e))
        return
    for point in clicked:
        points.remove(point)
    say(f'{name}: Alt+right-clicked {len(clicked)} of {total} aiming points this tool placed{why}.')


def measure_zoom(map_vk, anchor, known, width, height):
    """Zoom in ZOOM_IN_NOTCHES notches with the cursor on `anchor`, whose map point `known` is known, then
    middle-click a spot level with it and one straight above or below it. If the map grew evenly around the
    cursor, they give the zoom factor F8 uses for precise clicks; otherwise 1 (plain clicks)."""
    (ax, ay), d = anchor, round(0.3 * height)
    sx, sy = (1 if known[0] < 0.5 else -1), (1 if known[1] < 0.5 else -1)  # towards the map centre
    if not wheel(map_vk, ax, ay, ZOOM_IN_NOTCHES, SETTLE):
        raise map_closed(map_vk)
    across = middle_click(map_vk, ax + sx * d, ay, known)
    along = across and middle_click(map_vk, ax, ay + sy * d, across[2:])
    if not along:
        say('  precise clicks off: no point of interest appeared on the zoomed-in map')
        return 1
    try:
        zu = sx * d / ((across[2] - known[0]) * width)
        zv = sy * d / ((along[3] - known[1]) * height)
    except ZeroDivisionError:
        zu = zv = 0
    drift = max(abs(across[3] - known[1]) * height, abs(along[2] - known[0]) * width) * max(zu, zv)  # zoomed px
    if not (1.5 < zu < 100 and abs(zv / zu - 1) < 0.02 and drift < 1.5):
        say(f'  precise clicks off: the map did not zoom evenly around the cursor ({zu:.2f}x, {zv:.2f}x, '
            f'{drift:.1f} px off)')
        return 1
    return (zu + zv) / 2


def calibrate(settings, sites):
    """Measures how much ZOOM_IN_NOTCHES wheel notches zoom the map in, for F8's precise clicks. The zoom
    step is the same on every map, so once is enough."""
    map_vk = settings['map_key']
    map_name, key = key_name(map_vk), key_name(settings['calibrate_key'])
    if not held(map_vk):
        say(f'Calibrate: hold {map_name} so the map stays open, scroll the map all the way out, then press {key}.')
        return
    battle = read_battle(sites)
    if not battle:
        return
    info, _, name, _ = battle
    calibration = load_calibration()
    if calibration.pop('zoom', None):  # replaced: a failed try leaves the dot amber, not green
        save_json(CALIB_FILE, calibration)
    say(f'Calibrating: keep holding {map_name} and do not touch the mouse...')
    time.sleep(0.5)  # hand back off the mouse after pressing the hotkey
    pointed = cursor()  # second try for the first click, in case the game wants it near the plane
    left, top, right, bottom = game_area()
    try:
        if not zoom_out(map_vk, (left + right) // 2, (top + bottom) // 2):
            raise map_closed(map_vk)
        found = find_map(map_vk, pointed)
        if not found:
            raise Stop(f'No point of interest appeared. Use an aircraft whose middle-click on the map sets one '
                       f'(a red square) and keep {map_name} held. If it still fails, put the cursor on your plane '
                       f'before pressing {key}.')
        view, second = found
        zoom = measure_zoom(map_vk, second[:2], second[2:], view[2], view[3])
        zoom_out(map_vk, *second[:2])  # best effort: the measuring is done, and wheel() stops once the map closes
    except Stop as e:
        say(str(e))
        return
    calibration = load_calibration()
    calibration['zoom'] = [zoom, ZOOM_IN_NOTCHES]
    calibration[calib_key(name, info)] = view
    save_json(CALIB_FILE, calibration)
    metres_per_px = (info['map_max'][0] - info['map_min'][0]) / view[2]
    place = key_name(settings['place_key'])
    precision = f'{place} zooms in {zoom:.1f}x on each target' if zoom > 1 else f'{place} clicks without zooming in'
    say(f'Calibrated: {precision}, so on {name} points land within about {metres_per_px / zoom / 1.4:.0f} m. '
        f'You can let go of {map_name}. Your point of interest was moved: set it again if you use it (CCRP).')


# ---------------------------------------------------------------- background threads

def status_text(sites, settings):
    """(state, text) for the status card; the state picks its colour."""
    try:
        info = api('map_info.json')
    except OSError:
        return 'idle', 'Waiting for War Thunder (its map API at 127.0.0.1:8111 is not answering).'
    if not info.get('valid'):
        placed.clear()  # out of the battle, so its aiming points are gone
        return 'idle', 'War Thunder is running. Join a Nuclear Escalation battle in an aircraft.'
    name, enemy = detect(info, api('map_obj.json'), sites)
    if not name:
        return 'idle', 'In a battle, but not on a known Nuclear Escalation map (or the sides are not visible yet).'
    place, zoom = key_name(settings['place_key']), load_calibration().get('zoom')
    if zoom and zoom[0] > 1:
        return 'ok', f'{name}, enemy is team {enemy}. Ready: press {place}, best before take-off.'
    if zoom:  # calibrated, but the zoom could not be measured: plain clicks
        return 'warn', (f'{name}, enemy is team {enemy}. Calibrated without precise clicks (the log says why): '
                        f'{place} works with plain clicks. Calibrate again on the runway to retry.')
    return 'warn', (f'{name}, enemy is team {enemy}. Not calibrated yet: {place} works, but follow "How to '
                    'calibrate" on the right once for precise points.')


def run_status(sites, settings):
    while True:
        try:
            status = status_text(sites, settings)
        except Exception as e:
            status = 'bad', f'Status check failed: {e!r}'
        messages.put(('status', status))
        time.sleep(2)


def run_hotkeys(settings):
    """Load the target data, then watch the hotkeys; they only act while War Thunder is in front."""
    try:
        sites = load_sites()
    except Exception as e:
        say(f'Could not download the map data ({e!r}). Check your internet connection and restart.')
        messages.put(('status', ('bad', 'No map data yet: check your internet connection and restart.')))
        return
    say('Map data ready: ' + ', '.join(sites) + '.')
    threading.Thread(target=run_status, args=(sites, settings), daemon=True).start()
    actions = {'place_key': place_points, 'clear_key': clear_points, 'calibrate_key': calibrate}
    was_down = {}
    while True:
        for setting, action in actions.items():
            down = held(settings[setting])
            if down and not was_down.get(setting) and game_focused():
                busy.set()
                try:
                    action(settings, sites)
                except Exception as e:  # keep the hotkeys alive whatever went wrong
                    say(f'{action.__name__} failed: {e!r}')
                finally:
                    busy.clear()
            was_down[setting] = down
        time.sleep(0.02)


# ---------------------------------------------------------------- the window

COLOURS = {'bg': '#12181c', 'panel': '#1a2328', 'raised': '#243139', 'line': '#2e3e46', 'fg': '#e4eaec',
           'muted': '#8d9ea5', 'accent': '#f0b43c', 'idle': '#6c7d85', 'ok': '#4cc38a', 'warn': '#f0b43c',
           'bad': '#ef6b5b'}  # dark slate with the amber of War Thunder's own player marker


SIDES = {'Shift_L': 0xA0, 'Shift_R': 0xA1, 'Control_L': 0xA2, 'Control_R': 0xA3}  # Tk gives both sides one code


def first_font(root, *names):
    families = set(tkfont.families(root))
    return next((name for name in names if name in families), names[-1])


def crosshair_icon():
    """A small amber crosshair for the title bar and taskbar."""
    icon = tk.PhotoImage(width=32, height=32)
    for y in range(32):
        for x in range(32):
            dx, dy = x - 15.5, y - 15.5
            d = math.hypot(dx, dy)
            if 10 <= d <= 13 or d <= 2.2 or (min(abs(dx), abs(dy)) < 1 and 6 <= d <= 16):
                icon.put(COLOURS['accent'], (x, y))
    return icon


class App:
    """Settings window: status card, hotkeys, target types and a log."""
    KEYS = (('place_key', 'Place aiming points'), ('clear_key', 'Clear the points it placed'),
            ('calibrate_key', 'Calibrate the map'), ('map_key', 'Tactical map (same key as in game)'))

    def __init__(self, root, settings):
        self.root, self.settings, self.capturing = root, settings, None
        scale = float(root.tk.call('tk', 'scaling')) * 0.75  # 1 at 100 % Windows scaling, 1.25 at 125 %...
        self.px = px = lambda n: round(n * scale)  # wrap widths grow with the fonts, or text wraps into more lines
        c = COLOURS
        head = first_font(root, 'Bahnschrift SemiBold', 'Segoe UI Semibold', 'Segoe UI')
        body = first_font(root, 'Segoe UI', 'Tahoma')
        mono = first_font(root, 'Cascadia Mono', 'Consolas', 'Courier New')
        root.title('WT Map Ping')
        root.configure(bg=c['bg'])
        self.icon = crosshair_icon()  # keep a reference, or Tk drops the image
        root.iconphoto(True, self.icon)

        style = ttk.Style(root)
        style.theme_use('clam')  # the only built-in theme that takes custom colours everywhere
        style.configure('.', background=c['bg'], foreground=c['fg'], font=(body, 10), bordercolor=c['line'],
                        focuscolor=c['accent'], troughcolor=c['panel'])
        style.configure('Title.TLabel', font=(head, 20))
        style.configure('Muted.TLabel', foreground=c['muted'])
        style.configure('Section.TLabel', foreground=c['accent'], font=(head, 10))
        style.configure('Step.TLabel', foreground=c['accent'], font=(head, 11))
        style.configure('Card.TLabel', background=c['panel'])
        style.configure('TCheckbutton', indicatorbackground=c['raised'], indicatorforeground=c['bg'],
                        upperbordercolor=c['line'], lowerbordercolor=c['line'], padding=(0, 2))
        style.map('TCheckbutton', background=[('active', c['bg'])],  # ticked = solid amber square
                  indicatorbackground=[('selected', c['accent']), ('active', c['line'])],
                  indicatorforeground=[('selected', c['accent'])])
        for name, back, fore in (('Key.TButton', c['raised'], c['fg']), ('Wait.TButton', c['accent'], c['bg'])):
            style.configure(name, background=back, foreground=fore, font=(mono, 10, 'bold'), width=-13,  # at least
                            padding=(8, 5), bordercolor=c['line'], lightcolor=back, darkcolor=back)
            style.map(name, background=[('active', c['line'] if back == c['raised'] else back)])
        style.configure('TSpinbox', fieldbackground=c['raised'], background=c['raised'], foreground=c['fg'],
                        arrowcolor=c['fg'], bordercolor=c['line'], lightcolor=c['raised'], darkcolor=c['raised'])
        style.map('TSpinbox', fieldbackground=[('readonly', c['raised'])], selectbackground=[('readonly', c['raised'])],
                  selectforeground=[('readonly', c['fg'])])
        style.configure('Vertical.TScrollbar', background=c['raised'], arrowcolor=c['muted'], troughcolor=c['panel'],
                        bordercolor=c['panel'], lightcolor=c['raised'], darkcolor=c['raised'], gripcount=0)
        style.map('Vertical.TScrollbar', background=[('active', c['line'])])

        outer = ttk.Frame(root, padding=(20, 16, 20, 14))
        outer.pack(fill='both', expand=True)
        header = ttk.Frame(outer)
        header.pack(fill='x')
        ttk.Label(header, text='WT Map Ping', style='Title.TLabel').pack(side='left')
        ttk.Label(header, text='aiming points for Nuclear Escalation', style='Muted.TLabel').pack(
            side='left', padx=(12, 0), pady=(9, 0))

        card = tk.Frame(outer, bg=c['panel'], highlightthickness=1, highlightbackground=c['line'])
        card.pack(fill='x', pady=(12, 0))
        self.dot = tk.Canvas(card, width=12, height=12, bg=c['panel'], highlightthickness=0)
        self.dot.create_oval(1, 1, 11, 11, fill=c['idle'], outline='', tags='dot')
        self.dot.pack(side='left', anchor='n', padx=(14, 10), pady=(15, 0))
        self.status = ttk.Label(card, text='Loading the map data...', style='Card.TLabel', wraplength=px(780),
                                justify='left')
        self.status.pack(side='left', fill='x', expand=True, padx=(0, 14), pady=11)

        middle = ttk.Frame(outer)  # settings and log | guide, side by side
        middle.pack(fill='both', expand=True)
        middle.columnconfigure(1, weight=1)
        middle.rowconfigure(0, weight=1)
        left, right = ttk.Frame(middle), ttk.Frame(middle)
        left.grid(row=0, column=0, sticky='nsew', padx=(0, 28))
        right.grid(row=0, column=1, sticky='new')

        self.section(left, 'HOTKEYS')
        keys = ttk.Frame(left)
        keys.pack(fill='x')
        keys.columnconfigure(0, weight=1)
        self.buttons = {}
        for row, (setting, label) in enumerate(self.KEYS):
            ttk.Label(keys, text=label).grid(row=row, column=0, sticky='w')
            self.buttons[setting] = ttk.Button(keys, style='Key.TButton', text=key_name(settings[setting]),
                                               command=lambda s=setting: self.capture(s))
            self.buttons[setting].grid(row=row, column=1, sticky='e', padx=(16, 0), pady=3)
        ttk.Label(left, style='Muted.TLabel', wraplength=px(360), justify='left',
                  text="Click a key, then press the new one (Esc cancels). Pick keys War Thunder doesn't use: "
                       'the game sees them too.').pack(anchor='w', pady=(2, 0))

        self.section(left, 'ENEMY TARGETS')
        self.kinds = {}
        for kind, (label, _) in KINDS.items():
            self.kinds[kind] = tk.BooleanVar(value=kind in settings['kinds'])
            ttk.Checkbutton(left, text=label, variable=self.kinds[kind], command=self.save).pack(anchor='w')
        limit = ttk.Frame(left)
        limit.pack(fill='x', pady=(8, 0))
        self.limit = tk.IntVar(value=settings['max_points'])
        ttk.Label(limit, text='Place at most').pack(side='left')
        ttk.Spinbox(limit, from_=1, to=10, width=3, textvariable=self.limit, command=self.save, state='readonly',
                    justify='center', font=(body, 10)).pack(side='left', padx=8)
        ttk.Label(limit, text='points, nearest first', style='Muted.TLabel').pack(side='left')

        self.section(left, 'LOG')
        box = tk.Frame(left, bg=c['panel'], highlightthickness=1, highlightbackground=c['line'])
        box.pack(fill='both', expand=True)
        self.log = tk.Text(box, width=1, height=7, bg=c['panel'], fg=c['fg'], font=(mono, 9), relief='flat',
                           wrap='word', padx=10, pady=8, highlightthickness=0, selectbackground=c['line'],
                           state='disabled')  # width=1: it takes the column's width instead of setting it

        self.section(right, 'HOW TO CALIBRATE  ·  ONCE, ON ANY MAP')
        self.steps = self.rows(right, 5)
        ttk.Label(right, wraplength=px(486), justify='left', style='Muted.TLabel',
                  text='It measures how much the map zooms, for precise points. It moves your point of interest: '
                       'set it again afterwards if you use it.').pack(anchor='w', pady=(6, 0))
        self.section(right, 'IN BATTLE')
        self.uses = self.rows(right, 4)
        self.show_guide()
        scroll = ttk.Scrollbar(box, command=self.log.yview)
        self.log['yscrollcommand'] = scroll.set
        scroll.pack(side='right', fill='y')
        self.log.pack(side='left', fill='both', expand=True)
        self.log.tag_configure('time', foreground=c['muted'])

        footer = ttk.Frame(outer)
        footer.pack(fill='x', pady=(10, 0))
        ttk.Label(footer, text='Unofficial fan tool, not affiliated with Gaijin', style='Muted.TLabel',
                  font=(body, 8)).pack(side='left')
        donate = tk.Label(footer, text='Like it? Buy me a coffee', fg=c['accent'], bg=c['bg'], cursor='hand2',
                          font=(body, 9, 'underline'))
        donate.pack(side='right')
        donate.bind('<Button-1>', lambda _: webbrowser.open(DONATE))

        root.update_idletasks()
        root.minsize(root.winfo_reqwidth(), 600)  # narrower would cut the two columns
        dark = ctypes.c_int(1)  # dark title bar to match (DWMWA_USE_IMMERSIVE_DARK_MODE, Windows 10 20H1+)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(user32.GetParent(root.winfo_id()), 20, ctypes.byref(dark), 4)
        root.after(100, self.drain)

    def section(self, parent, title):
        ttk.Label(parent, text=title, style='Section.TLabel').pack(anchor='w', pady=(14, 5))

    def rows(self, parent, count):
        """Guide rows: an amber tag (step number or key) and a wrapped text, filled in by show_guide."""
        frame = ttk.Frame(parent)
        frame.pack(fill='x')
        frame.columnconfigure(0, minsize=self.px(46))  # the same tag column in both guide parts aligns the texts
        rows = [(ttk.Label(frame, style='Step.TLabel'), ttk.Label(frame, wraplength=self.px(440), justify='left'))
                for _ in range(count)]
        for row, (tag, text) in enumerate(rows):
            tag.grid(row=row, column=0, sticky='nw', pady=2)
            text.grid(row=row, column=1, sticky='w', pady=2)
        return rows

    def show_guide(self):
        """The guide's texts, with the keys as they are set now."""
        place, clear, calibrate, hold = (key_name(self.settings[setting]) for setting, _ in self.KEYS)
        steps = (
            "Spawn on the runway and don't take off. Use an aircraft that can set a point of interest: "
            'middle-clicking the map shows a small red square.',
            f'Press and hold {hold} to open the map. Keep holding it until the end: if you let go, the map closes '
            'and the calibration stops.',
            f'Still holding {hold}, scroll the mouse wheel down until the map stops zooming out and you see the '
            'whole map.',
            f"Still holding {hold}, press {calibrate} and let go of the mouse. You don't need to point at anything.",
            f'Keep holding {hold} while the tool works (about 8 s): the red square jumps twice, the map zooms in, '
            f'the square jumps twice more and the map zooms back out. Then let go. The dot at the top of this '
            'window turns green if it worked; if not, the log says why.')
        uses = ((place, 'Places aiming points on the ticked enemy targets, nearest first. It opens the map, finds '
                        'it with two middle-clicks, zooms in on each target and puts your point of interest back. '
                        'Only in aircraft with aiming points: their map shows "Alt + left click: add aiming point".'),
                (clear, 'Removes the aiming points the tool placed (it finds the map the same way). Remove your own '
                        'with Alt+right-click on the map.'),
                ('!', f"DON'T TOUCH THE MOUSE while {place} or {clear} runs ({place} takes about 15 s for 8 points). "
                      'The tool moves it for you; if you move it, the tool stops.'),
                (hold, 'Hold it to see the points. The game keeps at most 10.'))
        for number, ((tag, label), text) in enumerate(zip(self.steps, steps), 1):
            tag.configure(text=number)
            label.configure(text=text)
        for (tag, label), (key, text) in zip(self.uses, uses):
            tag.configure(text=key)
            label.configure(text=text)

    def capture(self, setting):
        if self.capturing:
            self.end_capture()  # another key was waiting: put it back
        self.capturing = setting
        self.buttons[setting].configure(text='press a key', style='Wait.TButton')
        self.root.focus_set()  # so Space or Enter reach the binding instead of pressing the button
        self.root.bind('<KeyPress>', self.got_key)

    def got_key(self, event):
        if event.keysym in ('Alt_L', 'Alt_R', 'Win_L', 'Win_R', 'Pause'):
            say('Alt, the Windows key and Pause cannot be hotkeys (the tool presses Alt itself, and Windows '
                'releases Pause the instant it is pressed). Press another key.')
            return 'break'  # keep waiting
        vk = SIDES.get(event.keysym, event.keycode)  # on Windows Tk reports the virtual-key code
        taken = [label for s, label in self.KEYS if s != self.capturing and self.settings[s] == vk]
        if event.keysym == 'Escape':
            pass
        elif taken:
            say(f'{key_name(vk)} is already used for: {taken[0]}.')
        else:
            self.settings[self.capturing] = vk
            self.save()
        self.end_capture()
        return 'break'  # keep F10 or Alt from also opening the window menu

    def end_capture(self):
        self.root.unbind('<KeyPress>')
        self.buttons[self.capturing].configure(text=key_name(self.settings[self.capturing]), style='Key.TButton')
        self.capturing = None
        self.show_guide()

    def save(self):
        self.settings['kinds'] = [kind for kind, chosen in self.kinds.items() if chosen.get()]
        self.settings['max_points'] = self.limit.get()
        save_json(SETTINGS_FILE, self.settings)

    def drain(self):
        while not messages.empty():
            kind, payload = messages.get()
            if kind == 'status':
                state, text = payload
                self.dot.itemconfigure('dot', fill=COLOURS[state])
                self.status.configure(text=text)
            else:
                self.log.configure(state='normal')
                self.log.insert('end', time.strftime('%H:%M:%S  '), 'time')
                self.log.insert('end', payload + '\n')
                self.log.see('end')
                self.log.configure(state='disabled')
        self.root.after(100, self.drain)


def main():
    settings = load_settings()
    root = tk.Tk()
    App(root, settings)
    threading.Thread(target=run_hotkeys, args=(settings,), daemon=True).start()
    try:
        root.mainloop()
    finally:  # closed (or Ctrl+C) mid-action: the game is no longer in front, so it stops and lifts its keys
        deadline = time.time() + 5
        while busy.is_set() and time.time() < deadline:
            time.sleep(0.05)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        pass
