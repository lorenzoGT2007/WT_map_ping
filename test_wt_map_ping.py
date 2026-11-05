"""Self-check, sends no input to anything:  py test_wt_map_ping.py
Uses test_snapshot.json, a real capture of the game's map API in a Tropical Coast battle."""
import json
import tempfile
import time
import tkinter as tk
from pathlib import Path

import wt_map_ping as w

snap = json.loads(Path(__file__).with_name('test_snapshot.json').read_text())
info, objs = snap['info'], snap['objs']
sites = w.load_sites()  # cached copy when fresh, else downloaded from the interactive map

# the site's world coordinates land on the game's own numbers: blue main runway start (sx, sy)
u, v = w.to_uv(info, -46631.288, -69327.6426)
assert abs(u - 0.322116) < 2e-6 and abs(v - 0.764464) < 2e-6, (u, v)

# map and side detection
assert w.detect(info, objs, sites) == ('Tropical Coast', 2)  # our visible SAM sites decide
captured = [dict(o, **{'color[]': [23, 77, 255]}) if o['type'] == 'airfield' else o for o in objs]
assert w.detect(info, captured, sites) == ('Tropical Coast', 2)  # captured airfields can't fool it
fields_only = [o for o in objs if o['type'] != 'ground_model']
assert w.detect(info, fields_only, sites) == ('Tropical Coast', 2)  # no units: airfield colours
swapped = [dict(o, **{'color[]': o['color[]'][::-1]}) for o in fields_only]  # red <-> blue
assert w.detect(info, swapped, sites) == ('Tropical Coast', 1)
neutral = [dict(o, **{'color[]': [255, 255, 250]}) for o in fields_only]
assert w.detect(info, neutral, sites) == (None, None)  # side unknowable: refuse, don't guess
small = {'map_min': [-65536.0, -65536.0], 'map_max': [65536.0, 65536.0]}
for name in ('Southeastern City', 'Volcano Valley'):  # same frame, only the airfields tell them apart
    fake = [{'type': 'airfield', 'color[]': [23, 77, 255] if team == 1 else [250, 12, 0],
             'sx': u, 'sy': v, 'ex': u + 0.003, 'ey': v}
            for team, x, z in sites[name]['airfields'] for u, v in [w.to_uv(small, x, z)]]
    assert w.detect(small, fake, sites) == (name, 2), name
assert w.detect(small, objs, sites) == (None, None)  # Tropical Coast objects on a 131 km frame

# every target type exists for both teams on every map, once per place
for name, site in sites.items():
    for kind, rows in site['targets'].items():
        for team in (1, 2):
            spots = [(x, z) for t, x, z in rows if t == team]
            assert spots and len(set(spots)) == len(spots), (name, kind, team)
    assert sum(t == 2 for kind in ('long_sam', 'short_sam') for t, _, _ in site['targets'][kind]) == 8, name

# target picking: chosen types only, nearest first, capped, airfields by their live colour
me = w.player(objs)
sams = w.pick_targets(info, objs, sites['Tropical Coast'], 2, ['long_sam', 'short_sam'], 10)
assert len(sams) == 8 and sams == sorted(sams, key=lambda p: w.math.dist(p, me))
assert len(w.pick_targets(info, objs, sites['Tropical Coast'], 2, list(w.KINDS), 10)) == 10
fields = w.pick_targets(info, objs, sites['Tropical Coast'], 2, ['airfield'], 10)
red = [p for p, o, _ in w.matched_airfields(info, objs, sites['Tropical Coast']) if w.colour(o) < -100]
assert fields and sorted(fields) == sorted(red) and (0.5, 0.5) not in fields
assert w.pick_targets(info, objs, sites['Tropical Coast'], 2, [], 10) == []
depots = lambda game: w.pick_targets(info, game, sites['Tropical Coast'], 2, ['depot'], 10)
recolour = lambda rgb: [dict(o, **{'color[]': rgb}) if o.get('icon') == 'TankStorage' else o for o in objs]
assert len(depots(recolour([23, 77, 255]))) < len(depots(objs)) < len(depots(recolour([250, 12, 0])))  # captures

got = w.solve((1000, 300, 0.25, 0.20), (1500, 700, 0.75, 0.60))
assert all(abs(a - b) < 1e-6 for a, b in zip(got, [750, 100, 1000, 1000])), got

# settings: anything broken in a hand-edited settings.json falls back to the defaults
w.SETTINGS_FILE = Path(tempfile.mkdtemp()) / 'settings.json'
for text, expected in [('{broken', w.DEFAULTS), ('{"map_key": "x"}', w.DEFAULTS), ('{"place_key": 300}', w.DEFAULTS),
                       ('{"place_key": "120"}', w.DEFAULTS), ('{"place_key": 119.0}', w.DEFAULTS),
                       ('{"place_key": true}', w.DEFAULTS), ('{"place_key": 121}', w.DEFAULTS),  # 121 = F10 too
                       ('{"clear_key": 77}', w.DEFAULTS),  # 77 = M, the map key
                       ('{"kinds": 5}', w.DEFAULTS), ('{"max_points": Infinity}', w.DEFAULTS),
                       ('{"place_key": 120, "kinds": ["radar", "bogus"], "max_points": 99}',
                        dict(w.DEFAULTS, place_key=120, kinds=['radar'], max_points=10))]:
    w.SETTINGS_FILE.write_text(text)
    assert w.load_settings() == expected, text
assert w.key_name(0x77) == 'F8' and w.key_name(0x4D) == 'M'
assert (w.scancode(0x77), w.scancode(0xA4), w.scancode(0x25), w.scancode(0xA3)) == (0x42, 0x38, 0xE04B, 0xE01D)

# the window builds (hidden, so nothing pops up)
root = tk.Tk()
root.withdraw()
w.App(root, dict(w.DEFAULTS))
root.update_idletasks()
root.destroy()

# calibrate, place and clear points end to end against a fake game; nothing reaches Windows
SCREEN = [2560, 1080]  # the fake game runs borderless fullscreen at (0, 0)
FRAME = (1044, 200, 816)  # the map frame on screen (left, top, size in px): clicks outside it do nothing
NOTCH = 1.3  # how much one wheel notch zooms the fake map
PLANE = (0.8, 0.85)  # the fake plane, for a game that only takes points of interest near it
state = {'home': FRAME, 'view': FRAME, 'zoom_around': 'cursor', 'reach': None, 'poi': None, 'clicks': [],
         'marks': [], 'right': [], 'middle': [], 'wheel': 0, 'nudge': 0, 'shift_after': 0}
real = w.user32


class FakeUser32:
    pos = (100, 100)  # cursor parked off the map: calibration must not need it

    def __getattr__(self, name):
        return getattr(real, name)  # GetSystemMetrics, MapVirtualKeyW, GetKeyNameTextW...

    def SetCursorPos(self, x, y):
        self.pos = (x, y)

    def GetCursorPos(self, point):
        point[0], point[1] = self.pos

    def GetAsyncKeyState(self, vk):
        return 0x8000 if vk == w.DEFAULTS['map_key'] else 0  # the player holds the map key

    def GetForegroundWindow(self):
        return 1

    def GetClassNameW(self, hwnd, name, size):
        name.value = 'DagorWClass'

    def GetClientRect(self, hwnd, rect):
        rect[0], rect[1], rect[2], rect[3] = 0, 0, *SCREEN

    def ClientToScreen(self, hwnd, point):
        pass  # the client area starts at the screen's corner


def fake_send(kind, **event):
    """The fake game's map. It zooms around the cursor (or the frame's middle, or not at all), and all the way
    out it snaps to state['home'], which changes with the spawn like War Thunder's. Clicks outside the frame
    do nothing."""
    flags, (x, y) = event['mi'].flags if kind == 0 else 0, w.user32.pos
    left, top, size = state['view']
    if flags == 0x0800:
        state['wheel'] += 1
        if state['zoom_around'] == 'nowhere':
            return  # a game that ignores the wheel
        f = NOTCH if event['mi'].data > 0 else 1 / NOTCH
        cx, cy = (x, y) if state['zoom_around'] == 'cursor' else (FRAME[0] + FRAME[2] / 2, FRAME[1] + FRAME[2] / 2)
        if size * f <= state['home'][2] + 1e-6:
            state['view'] = state['home']  # all the way out
        elif size * f <= FRAME[2] * 40:
            state['view'] = (cx - (cx - left) * f, cy - (cy - top) * f, size * f)
        return
    on_frame = FRAME[0] <= x <= FRAME[0] + FRAME[2] and FRAME[1] <= y <= FRAME[1] + FRAME[2]
    u, v = (x - left) / size, (y - top) / size
    if flags == 0x40 and on_frame:  # middle up: the point of interest moves there
        if state['reach'] is None or w.math.dist((u, v), PLANE) <= state['reach']:
            state['poi'] = (round(u, 6), round(v, 6))
    if flags == 0x2 and on_frame:
        state['marks'].append((u, v))  # where the aiming point lands on the map
    for flag, record in ((0x2, 'clicks'), (0x8, 'right'), (0x20, 'middle')):
        if flags == flag:
            state[record].append((x, y))
            if len(state[record]) == state['nudge']:
                w.user32.pos = (x + 7, y)  # the player bumps the mouse during this click
    if flags == 0x4 and len(state['clicks']) == state['shift_after']:  # the map slides 3 px while F8 works
        state['home'] = (state['home'][0] + 3, *state['home'][1:])
        state['view'] = (state['view'][0] + 3, *state['view'][1:])


def calibrate_with(home, cursor_at=(100, 100), reach=None, zoom_around='cursor', file=None):
    """Calibrate (F10) against a fake game whose zoomed-out map sits at `home`; returns the saved file."""
    state.update(home=home, view=home, reach=reach, zoom_around=zoom_around, middle=[], wheel=0)
    w.user32.pos = cursor_at
    w.CALIB_FILE = file or Path(tempfile.mkdtemp()) / 'calibration.json'
    w.calibrate(dict(w.DEFAULTS), sites)
    return w.load_calibration()


def last_log():
    lines = []
    while not w.messages.empty():
        kind, text = w.messages.get()
        if kind == 'log':
            lines.append(text)
    return lines[-1]


def logs():
    lines = []
    while not w.messages.empty():
        kind, text = w.messages.get()
        lines.append(text if kind == 'log' else '')
    return '\n'.join(lines)


clock = [0.0]  # fake time, so the 4 s waits for a point of interest that never comes take no time
time.sleep, time.time = lambda s: clock.__setitem__(0, clock[0] + s), lambda: clock[0]
game_objs = [o for o in objs if o['type'] != 'point_of_interest']  # the capture's own point is stale here
poi_obj = lambda: [{'type': 'point_of_interest', 'x': state['poi'][0], 'y': state['poi'][1]}] if state['poi'] else []
w.user32, w.send = FakeUser32(), fake_send
w.api = lambda path: {'map_info.json': info, 'state': {'valid': True}, 'map_obj.json': game_objs + poi_obj()}[path]
key = w.calib_key('Tropical Coast', info)
close = lambda view, home: max(abs(a - b) for a, b in zip(view, (*home, home[2]))) < 0.01
off_px = lambda: max(w.math.dist(m, t) for m, t in zip(state['marks'], sams)) * FRAME[2]  # worst miss, frame px
zoom = NOTCH ** w.ZOOM_IN_NOTCHES

# F10 measures the zoom once; no aiming, it starts in the middle of the screen
saved = calibrate_with(FRAME)
assert abs(saved['zoom'][0] / zoom - 1) < 0.001 and close(saved[key], FRAME), saved
assert state['middle'][0] in ((1280, 540), (1320, 580)) and state['wheel'] == 2 * w.ZOOM_NOTCHES + w.ZOOM_IN_NOTCHES
plane = (round(FRAME[0] + PLANE[0] * FRAME[2]), round(FRAME[1] + PLANE[1] * FRAME[2]))
assert 'zoom' in calibrate_with(FRAME, cursor_at=plane, reach=0.45)  # far clicks ignored: the cursor rescues it
assert state['middle'][1] == plane, state['middle']
assert calibrate_with(FRAME, zoom_around='centre')['zoom'][0] == 1  # zooms around the middle: plain clicks
assert calibrate_with((500, -300, 1700), zoom_around='nowhere')['zoom'][0] == 1  # stuck zoom: plain clicks too
kept = Path(tempfile.mkdtemp()) / 'calibration.json'
assert 'zoom' in calibrate_with(FRAME, file=kept)
assert 'zoom' not in calibrate_with(FRAME, reach=0, file=kept)  # a failed try drops it, so the dot turns amber
assert 'No point of interest appeared' in last_log()

# F8 after spawning elsewhere: the zoomed-out map moved, F8 finds it again and still lands precisely
assert 'zoom' in calibrate_with(FRAME)
spawned = (1037.9, 175.4, 842.66)
state.update(home=spawned, view=(700, 100, 1500), poi=(0.4, 0.45), clicks=[], marks=[])  # left zoomed in, a POI set
w.place_points(dict(w.DEFAULTS), sites)
assert len(state['marks']) == len(sams) and off_px() < 0.75 / zoom, off_px()  # within a fraction of a pixel
assert state['view'] == spawned and w.math.dist(state['poi'], (0.4, 0.45)) * FRAME[2] < 0.4  # POI put back
assert close(w.load_calibration()[key], spawned) and 'nearest first.' in logs()

w.clear_points(dict(w.DEFAULTS), sites)  # Alt+right-clicks the same targets, newest first, then forgets them
assert len(state['right']) == len(sams) and not any(w.placed.values())
assert w.clear_points(dict(w.DEFAULTS), sites) is None and 'No aiming points placed by this tool' in last_log()

state.update(clicks=[], marks=[], shift_after=3)  # the map slides while F8 works: the tool says so
w.place_points(dict(w.DEFAULTS), sites)
assert 'The map moved while the tool worked' in logs()
state.update(shift_after=0, home=spawned, view=spawned)
w.clear_points(dict(w.DEFAULTS), sites)

state.update(reach=0, marks=[])  # an aircraft that cannot set a point of interest: last time's map position
w.place_points(dict(w.DEFAULTS), sites)
assert len(state['marks']) == len(sams) and off_px() < 0.75 / zoom and 'where it was last time' in logs()
w.clear_points(dict(w.DEFAULTS), sites)
w.CALIB_FILE = Path(tempfile.mkdtemp()) / 'calibration.json'  # ...and with no position saved yet: refused
state['clicks'] = []
w.place_points(dict(w.DEFAULTS), sites)
assert state['clicks'] == [] and 'cannot find the map' in last_log()
state['reach'] = None

w.CALIB_FILE.write_text('{}')  # never calibrated: F8 still works, with plain clicks
state.update(home=FRAME, view=FRAME, marks=[])
w.place_points(dict(w.DEFAULTS), sites)
assert len(state['marks']) == len(sams) and 0.15 < off_px() < 0.75, off_px()
w.clear_points(dict(w.DEFAULTS), sites)

left_over = lambda: sum(map(len, w.placed.values()))
state.update(clicks=[], marks=[], right=[], nudge=3)  # the player touches the mouse during F8's 3rd click
w.place_points(dict(w.DEFAULTS), sites)
assert len(state['clicks']) == 3 and left_over() == 3 and 'the mouse moved' in last_log()
state.update(nudge=1)  # ...and during End's 1st click
w.clear_points(dict(w.DEFAULTS), sites)
assert len(state['right']) == 1 and left_over() == 2 and 'the mouse moved' in last_log()

SCREEN[:] = 1280, 720  # the game window shrank: the map's spot is off the window, nothing may be clicked there
state.update(clicks=[], nudge=0)
w.place_points(dict(w.DEFAULTS), sites)
assert state['clicks'] == [] and 'skipped: off the game window' in last_log()

print('all checks passed')
