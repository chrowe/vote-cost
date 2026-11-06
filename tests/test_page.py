#!/usr/bin/env python3
"""Browser tests for index.html, using only Python and Chrome.

Run with: python3 tests/test_page.py

The page does its own arithmetic, so these tests recalculate every displayed figure
from data/races.js in Python and compare the results with what Chrome renders.
Chrome is driven over its remote debugging protocol, which lets the tooltip tests
use real mouse movement. Set CHROME to the browser's path if it isn't found.
"""
import base64
import decimal
import functools
import itertools
import json
import os
import pathlib
import re
import shutil
import socket
import struct
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.parse
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
RACES = json.loads((ROOT / "data" / "races.js").read_text(encoding="utf-8").split("=", 1)[1].strip().rstrip(";"))["races"]
COMBOS = list(itertools.product(("all", "campaign", "outside"), ("all", "general"), ("2", "6")))
ALL_OPEN = {race["id"] for race in RACES}
CHROME_NAMES = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser")
MAC_CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

# Each edit breaks one figure in a copy of the page. The tests must notice every one.
WRONG_FIGURES = {
    "race cost": ("cost: (campaign + outside) / race.votes", "cost: (campaign + outside) / race.votes * 1.01"),
    "candidate per vote": ("perVote(backed / c.votes)", "perVote(backed * 1.01 / c.votes)"),
    "tooltip": ('line("Outside groups", m.outside)', 'line("Outside groups", m.outside * 1.01)'),
    "period filter": ('state.period === "6" ? c.spend6 : c.spend2', "c.spend2"),
    "candidates filter": ('state.who === "all" || c.votes', "true"),
    "money filter": ('if (state.money !== "campaign") outside += c.for + c.against;', "outside += c.for + c.against;"),
}


# --- The separate calculation: what the page should display -------------------------

def fixed(number, places):
    """JavaScript's toFixed, which rounds the exact binary value and takes ties upward."""
    step = decimal.Decimal(1).scaleb(-places)
    return str(decimal.Decimal(number).quantize(step, rounding=decimal.ROUND_HALF_UP))


def whole(number):
    return int(decimal.Decimal(number).quantize(decimal.Decimal(1), rounding=decimal.ROUND_HALF_UP))


def money(n):
    if n >= 1e9:
        return f"${fixed(n / 1e9, 2)}B"
    if n >= 1e6:
        return f"${fixed(n / 1e6, 1)}M"
    if n >= 1e3:
        return f"${whole(n / 1e3)}K"
    return f"${whole(n)}" if n > 0 else "$0"


def per_vote(n):
    return f"${fixed(n, 2)}"


def percent(n):
    return f"{fixed(n * 100, 1)}%"


def label(race):
    return race["state"] + (" (special)" if race["special"] else "")


def people(race, who):
    return [c for c in race["candidates"] if who == "all" or c["votes"]]


def campaign_of(candidate, period):
    return candidate["spend6"] if period == "6" else candidate["spend2"]


def measure(race, filters):
    campaign = outside = 0
    for candidate in people(race, filters["who"]):
        if filters["money"] != "outside":
            campaign += campaign_of(candidate, filters["period"])
        if filters["money"] != "campaign":
            outside += candidate["for"] + candidate["against"]
    return {"race": race, "campaign": campaign, "outside": outside, "total": campaign + outside,
            "cost": (campaign + outside) / race["votes"]}


def candidate_row(candidate, period):
    campaign = campaign_of(candidate, period)
    votes = candidate["votes"]
    return [f"{candidate['name']} ({candidate['party']})",
            f"{votes:,}" if votes else "",
            percent(candidate["pct"]) if votes else "",
            money(campaign), money(candidate["for"]), money(candidate["against"]),
            per_vote((campaign + candidate["for"]) / votes) if votes else ""]


def detail_rows(race, filters):
    period = filters["period"]
    size = lambda c: campaign_of(c, period) + c["for"] + c["against"]
    counted = people(race, filters["who"])
    others = sorted((c for c in counted if not c["votes"]), key=lambda c: -size(c))
    shown = [c for c in others if size(c) >= 50000]
    minor = [c for c in others if size(c) < 50000]
    rows = [candidate_row(c, period) for c in counted if c["votes"]]
    if shown or minor:
        rows.append(["Not on the general ballot: primary losers, withdrawals, retirees"])
        rows += [candidate_row(c, period) for c in shown]
        if minor:
            combined = 0
            for candidate in minor:
                combined += size(candidate)
            rows.append([f"{len(minor)} others under $50K each", "", "", f"{money(combined)} combined", ""])
    return rows


def order(items, key, descending):
    """The page's sort: by the chosen column, with ties going to the higher cost."""
    direction = -1 if descending else 1

    def compare(a, b):
        if key == "race":
            return direction * ((label(a["race"]) > label(b["race"])) - (label(a["race"]) < label(b["race"])))
        pick = (lambda m: m["race"][key]) if key in ("votes", "margin") else (lambda m: m[key])
        difference = direction * (pick(a) - pick(b))
        return difference or b["cost"] - a["cost"]

    return sorted(items, key=functools.cmp_to_key(lambda a, b: (compare(a, b) > 0) - (compare(a, b) < 0)))


def expected_view(filters, opened=frozenset(), key="cost", descending=True):
    items = [measure(race, filters) for race in RACES]
    largest = max(m["cost"] for m in items) or 1
    total, votes = 0, 0
    for item in items:
        total += item["total"]
        votes += item["race"]["votes"]
    ranked = sorted(items, key=lambda m: -m["cost"])
    rows = []
    for item in order(items, key, descending):
        race = item["race"]
        bars = []
        if item["campaign"] > 0:
            bars.append({"cls": "s1", "width": item["campaign"] / race["votes"] / largest * 100})
        if item["outside"] > 0:
            bars.append({"cls": "s2", "width": item["outside"] / race["votes"] / largest * 100})
        row = {"id": race["id"], "name": label(race), "winner": f"{race['winner']} ({race['winnerParty']})",
               "votes": f"{race['votes']:,}", "margin": percent(race["margin"]), "money": money(item["total"]),
               "cost": per_vote(item["cost"]), "bars": bars, "expanded": race["id"] in opened,
               "detail": detail_rows(race, filters) if race["id"] in opened else None,
               "note": (race["note"] or None) if race["id"] in opened else None}
        rows.append(row)
    return {
        "kpis": [["All races combined", per_vote(total / votes), "per vote"],
                 ["Most expensive", per_vote(ranked[0]["cost"]), label(ranked[0]["race"])],
                 ["Least expensive", per_vote(ranked[-1]["cost"]), label(ranked[-1]["race"])],
                 ["Total money", money(total), f"{votes:,} votes"]],
        "legend": ["Campaigns", "Outside groups"] if filters["money"] == "all" else [],
        "periodDisabled": filters["money"] == "outside",
        "rows": rows,
    }


def expected_tip(race, filters):
    item = measure(race, filters)
    lines = []
    if filters["money"] != "outside":
        lines.append(["Campaigns", f"{money(item['campaign'])} · {per_vote(item['campaign'] / race['votes'])}"])
    if filters["money"] != "campaign":
        lines.append(["Outside groups", f"{money(item['outside'])} · {per_vote(item['outside'] / race['votes'])}"])
    return {"head": label(race), "lines": lines}


def differences(expected, actual, path=""):
    """Every place two nested values disagree. Bar widths may differ by rounding only."""
    if isinstance(expected, dict) and isinstance(actual, dict):
        found = []
        for key in expected:
            found += differences(expected[key], actual.get(key), f"{path}.{key}")
        return found
    if isinstance(expected, list) and isinstance(actual, list):
        if len(expected) != len(actual):
            return [f"{path}: expected {len(expected)} items, page has {len(actual)}: {actual}"]
        found = []
        for index, (want, got) in enumerate(zip(expected, actual)):
            found += differences(want, got, f"{path}[{index}]")
        return found
    if isinstance(expected, float) and isinstance(actual, (int, float)):
        return [] if abs(expected - actual) < 0.01 else [f"{path}: expected {expected}, page has {actual}"]
    return [] if expected == actual else [f"{path}: expected {expected!r}, page has {actual!r}"]


# --- Driving Chrome ------------------------------------------------------------------

def find_chrome():
    candidates = [os.environ.get("CHROME"), MAC_CHROME] + [shutil.which(name) for name in CHROME_NAMES]
    for candidate in candidates:
        if candidate and os.path.exists(candidate):
            return candidate
    raise SystemExit("Chrome not found. Install it, or set CHROME to its path.")


class WebSocket:
    """The small part of the WebSocket protocol that Chrome's debugger needs."""

    def __init__(self, url):
        parts = urllib.parse.urlparse(url)
        self.sock = socket.create_connection((parts.hostname, parts.port), timeout=30)
        self.file = self.sock.makefile("rb")
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((f"GET {parts.path} HTTP/1.1\r\nHost: {parts.netloc}\r\nUpgrade: websocket\r\n"
                           f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        status = self.file.readline()
        if b" 101 " not in status:
            raise RuntimeError(f"WebSocket handshake failed: {status!r}")
        while self.file.readline() not in (b"\r\n", b""):
            pass

    def _send_frame(self, opcode, payload):
        size = len(payload)
        if size < 126:
            header = struct.pack("!BB", 0x80 | opcode, 0x80 | size)
        elif size < 65536:
            header = struct.pack("!BBH", 0x80 | opcode, 0x80 | 126, size)
        else:
            header = struct.pack("!BBQ", 0x80 | opcode, 0x80 | 127, size)
        mask = os.urandom(4)
        self.sock.sendall(header + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def send(self, text):
        self._send_frame(1, text.encode())

    def receive(self):
        message = b""
        while True:
            first, second = self.file.read(2)
            size = second & 0x7F
            if size == 126:
                size = struct.unpack("!H", self.file.read(2))[0]
            elif size == 127:
                size = struct.unpack("!Q", self.file.read(8))[0]
            payload = self.file.read(size)
            opcode = first & 0x0F
            if opcode == 8:
                raise RuntimeError("Chrome closed the debugging connection")
            if opcode == 9:
                self._send_frame(10, payload)
            elif opcode in (0, 1, 2):
                message += payload
                if first & 0x80:
                    return message.decode()

    def close(self):
        self.file.close()
        self.sock.close()


class Browser:
    def __init__(self):
        self.profile = tempfile.mkdtemp(prefix="vote-cost-chrome-")
        args = [find_chrome(), "--headless=new", "--remote-debugging-port=0", f"--user-data-dir={self.profile}",
                "--no-first-run", "--no-default-browser-check", "--disable-gpu", "about:blank"]
        if os.environ.get("CI"):  # hosted CI runners don't allow Chrome's sandbox
            args.insert(1, "--no-sandbox")
        self.process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        self.socket = None
        self.next_id = 0
        self.events = []
        try:
            self.socket = WebSocket(self._page_url(self._debug_port()))
            for domain in ("Page", "Runtime", "Log"):
                self.call(f"{domain}.enable")
            self.call("Emulation.setFocusEmulationEnabled", enabled=True)
            self.resize(1280, 900)
        except Exception:
            self.close()
            raise

    def _debug_port(self):
        found = {}
        ready = threading.Event()

        def watch():  # keep draining stderr so Chrome never blocks on a full pipe
            for line in self.process.stderr:
                match = re.search(r"DevTools listening on ws://[^:]+:(\d+)/", line)
                if match:
                    found["port"] = match.group(1)
                    ready.set()

        threading.Thread(target=watch, daemon=True).start()
        if not ready.wait(30):
            raise RuntimeError("Chrome didn't report a debugging port within 30 seconds")
        return found["port"]

    @staticmethod
    def _page_url(port):
        for _ in range(100):
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=10) as response:
                pages = [t for t in json.load(response) if t["type"] == "page"]
            if pages:
                return pages[0]["webSocketDebuggerUrl"]
            time.sleep(0.1)
        raise RuntimeError("Chrome opened no page")

    def call(self, method, **params):
        self.next_id += 1
        self.socket.send(json.dumps({"id": self.next_id, "method": method, "params": params}))
        while True:
            message = json.loads(self.socket.receive())
            if message.get("id") == self.next_id:
                if "error" in message:
                    raise RuntimeError(f"{method}: {message['error']}")
                return message["result"]
            self.events.append(message)

    def load(self, path):
        self.events.clear()
        self.call("Page.navigate", url=pathlib.Path(path).as_uri())
        deadline = time.time() + 30
        while not any(e.get("method") == "Page.loadEventFired" for e in self.events):
            if time.time() > deadline:
                raise RuntimeError(f"{path} didn't finish loading")
            self.events.append(json.loads(self.socket.receive()))

    def js(self, expression):
        result = self.call("Runtime.evaluate", expression=expression, returnByValue=True)
        if "exceptionDetails" in result:
            raise RuntimeError(f"{result['exceptionDetails']}\nwhile running: {expression}")
        return result["result"].get("value")

    def page_errors(self):
        errors = []
        for event in self.events:
            method, params = event.get("method"), event.get("params", {})
            if method == "Runtime.exceptionThrown":
                errors.append(params["exceptionDetails"].get("text", "exception"))
            elif method == "Log.entryAdded" and params["entry"]["level"] == "error":
                errors.append(params["entry"]["text"])
            elif method == "Runtime.consoleAPICalled" and params["type"] == "error":
                errors.append(str(params["args"]))
        return errors

    def resize(self, width, height):
        self.call("Emulation.setDeviceMetricsOverride", width=width, height=height, deviceScaleFactor=1, mobile=False)

    def move_mouse(self, x, y):
        self.call("Input.dispatchMouseEvent", type="mouseMoved", x=x, y=y)

    def click_mouse(self, x, y):
        self.move_mouse(x, y)
        for kind in ("mousePressed", "mouseReleased"):
            self.call("Input.dispatchMouseEvent", type=kind, x=x, y=y, button="left", clickCount=1)

    def press_enter(self):
        self.call("Input.dispatchKeyEvent", type="keyDown", key="Enter", code="Enter",
                  windowsVirtualKeyCode=13, text="\r")
        self.call("Input.dispatchKeyEvent", type="keyUp", key="Enter", code="Enter", windowsVirtualKeyCode=13)

    def press_escape(self):
        for kind in ("keyDown", "keyUp"):
            self.call("Input.dispatchKeyEvent", type=kind, key="Escape", code="Escape", windowsVirtualKeyCode=27)

    def close(self):
        if self.socket:
            self.socket.close()
        self.process.terminate()
        try:
            self.process.wait(10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        shutil.rmtree(self.profile, ignore_errors=True)


# --- Reading and using the page ------------------------------------------------------

SNAPSHOT = """(() => {
  const text = node => node ? node.textContent : null;
  const rows = [...document.querySelectorAll('#rows > tr.race')].map(tr => {
    const next = tr.nextElementSibling;
    const detail = next && next.classList.contains('detail') ? next : null;
    const cells = tr.children;
    return {
      id: tr.dataset.id,
      name: text(tr.querySelector('.race-name')),
      winner: text(tr.querySelector('.winner')),
      votes: text(cells[1]),
      margin: text(cells[2]),
      money: text(cells[3]),
      cost: text(tr.querySelector('.cost-value')),
      bars: [...tr.querySelectorAll('.bar b')].map(b => ({cls: b.className, width: parseFloat(b.style.width)})),
      expanded: tr.querySelector('.race-name').getAttribute('aria-expanded') === 'true',
      detail: detail ? [...detail.querySelector('table').tBodies[0].rows].map(r => [...r.children].map(text)) : null,
      note: detail ? text(detail.querySelector('.note')) : null
    };
  });
  return {
    kpis: [...document.querySelectorAll('#kpis > div')].map(d => [...d.children].map(text)),
    legend: [...document.querySelectorAll('#legend span')].map(text),
    periodDisabled: document.getElementById('period-field').disabled,
    rows
  };
})()"""

TIP = """(() => {
  const tip = document.getElementById('tip');
  const box = tip.getBoundingClientRect();
  return {
    shown: getComputedStyle(tip).display !== 'none',
    head: tip.querySelector('.head') ? tip.querySelector('.head').textContent : null,
    lines: [...tip.querySelectorAll('div:not(.head)')].map(d => [...d.children].map(s => s.textContent)),
    left: box.left, right: box.right, top: box.top, bottom: box.bottom,
    width: innerWidth, height: innerHeight
  };
})()"""


def pick(page, name, value):
    page.js(f"document.querySelector('input[name={name}][value=\"{value}\"]').click()")


def set_filters(page, money_choice, who, period):
    # The period field is disabled for outside money alone, so set it before choosing that.
    pick(page, "money", "all")
    pick(page, "period", period)
    pick(page, "who", who)
    pick(page, "money", money_choice)
    return {"money": money_choice, "who": who, "period": period}


def toggle_race(page, race_id):
    page.js(f"document.querySelector('[data-id=\"{race_id}\"] .race-name').click()")


def point_on(page, race_id, selector, block="center", side="center"):
    box = page.js(f"""(() => {{
      const node = document.querySelector('[data-id="{race_id}"] {selector}');
      node.scrollIntoView({{block: '{block}'}});
      const r = node.getBoundingClientRect();
      return {{left: r.left, right: r.right, top: r.top, bottom: r.bottom}};
    }})()""")
    x = box["right"] - 2 if side == "right" else (box["left"] + box["right"]) / 2
    return x, (box["top"] + box["bottom"]) / 2


def filter_failures(page):
    """Compare the whole page with the separate calculation under every filter combination."""
    failures = differences(expected_view(set_filters(page, "all", "all", "2")), page.js(SNAPSHOT), "default")
    for race in RACES:
        toggle_race(page, race["id"])
    for choice in COMBOS:
        filters = set_filters(page, *choice)
        failures += differences(expected_view(filters, ALL_OPEN), page.js(SNAPSHOT), str(choice))
    return failures


def tooltip_failures(page):
    """Hover over every race's cost bar with the mouse, under every filter combination."""
    failures = []
    for choice in COMBOS:
        filters = set_filters(page, *choice)
        for race in RACES:
            x, y = point_on(page, race["id"], ".cost-wrap")
            page.move_mouse(x, y)
            tip = page.js(TIP)
            where = f"{choice} {race['id']}"
            if not tip["shown"]:
                failures.append(f"{where}: tooltip didn't appear")
                continue
            failures += differences(expected_tip(race, filters), tip, where)
            failures += off_screen(tip, where)
    return failures


def info_state(page):
    """Each help button's state, with the box of its pop-up when one is open."""
    return page.js("""[...document.querySelectorAll('.info')].map(button => {
      const pop = document.getElementById(button.getAttribute('aria-controls'));
      const box = pop.getBoundingClientRect();
      return {
        id: pop.id, label: button.getAttribute('aria-label'), expanded: button.getAttribute('aria-expanded') === 'true',
        shown: !pop.hidden && getComputedStyle(pop).display !== 'none', left: box.left, right: box.right,
        pageWidth: document.documentElement.clientWidth, scrollWidth: document.documentElement.scrollWidth
      };
    })""")


def click_info(page, key):
    page.click_mouse(*page.js(f"""(() => {{
      const button = document.querySelector('[aria-controls="info-{key}"]');
      button.scrollIntoView({{block: 'center'}});
      const r = button.getBoundingClientRect();
      return [r.left + r.width / 2, r.top + r.height / 2];
    }})()"""))


def open_info(page):
    return [state["id"] for state in info_state(page) if state["shown"] and state["expanded"]]


def off_screen(tip, where):
    inside = tip["left"] >= 0 and tip["right"] <= tip["width"] and tip["top"] >= 0 and tip["bottom"] <= tip["height"]
    return [] if inside else [f"{where}: tooltip is off screen: {tip}"]


class PageTests(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls.page = Browser()
        cls.addClassCleanup(cls.page.close)

    def setUp(self):
        self.page.resize(1280, 900)
        self.page.load(ROOT / "index.html")

    def assertNoFailures(self, failures):
        self.assertFalse(failures, f"{len(failures)} problems, starting with:\n" + "\n".join(failures[:20]))

    def test_page_loads_without_errors(self):
        self.assertEqual(self.page.js("document.querySelectorAll('#rows > tr.race').length"), len(RACES))
        self.assertTrue(self.page.js("document.getElementById('outside-basis').textContent"))
        self.assertEqual(self.page.page_errors(), [])

    def test_every_figure_under_every_filter_combination(self):
        self.assertNoFailures(filter_failures(self.page))
        self.assertEqual(self.page.page_errors(), [])

    def test_period_filter_is_disabled_for_outside_money(self):
        set_filters(self.page, "outside", "all", "2")
        self.assertTrue(self.page.js("document.getElementById('period-field').disabled"))
        before = self.page.js(SNAPSHOT)
        x, y = self.page.js("""(() => {
          const r = document.querySelector('input[name=period][value="6"]').getBoundingClientRect();
          return [r.left + r.width / 2, r.top + r.height / 2];
        })()""")
        self.page.click_mouse(x, y)
        self.assertNoFailures(differences(before, self.page.js(SNAPSHOT)))
        self.assertTrue(self.page.js("document.querySelector('input[name=period][value=\"2\"]').checked"))

    def test_sorting(self):
        # Each column, with the direction its first click sorts in. The second click reverses it.
        columns = [("race", False), ("votes", True), ("margin", False), ("total", True), ("cost", True)]
        for choice in (("all", "all", "2"), ("outside", "general", "2"), ("campaign", "all", "6")):
            filters = set_filters(self.page, *choice)
            for key, first in columns:
                for descending in (first, not first):
                    self.page.js(f"document.querySelector('[data-sort={key}]').click()")
                    where = f"{choice} {key} {'down' if descending else 'up'}"
                    self.assertNoFailures(differences(expected_view(filters, key=key, descending=descending),
                                                      self.page.js(SNAPSHOT), where))
                    self.assertEqual(self.page.js(f"document.querySelector('[data-sort={key}]').closest('th').getAttribute('aria-sort')"),
                                     "descending" if descending else "ascending", where)

    def test_first_click_on_the_default_sort_reverses_it(self):
        self.page.js("document.querySelector('[data-sort=cost]').click()")
        filters = {"money": "all", "who": "all", "period": "2"}
        self.assertNoFailures(differences(expected_view(filters, descending=False), self.page.js(SNAPSHOT)))

    def test_expanding_a_race_with_the_mouse(self):
        filters = {"money": "all", "who": "all", "period": "2"}
        self.page.click_mouse(*point_on(self.page, "MD", ".race-name"))
        self.assertNoFailures(differences(expected_view(filters, {"MD"}), self.page.js(SNAPSHOT), "opened"))
        self.page.click_mouse(*point_on(self.page, "MD", ".winner"))
        self.assertNoFailures(differences(expected_view(filters), self.page.js(SNAPSHOT), "closed"))

    def test_expanding_a_race_with_the_keyboard(self):
        filters = {"money": "all", "who": "all", "period": "2"}
        self.page.js("document.querySelector('[data-id=\"MD\"] .race-name').focus()")
        self.page.press_enter()
        self.assertNoFailures(differences(expected_view(filters, {"MD"}), self.page.js(SNAPSHOT), "opened"))
        self.assertEqual(self.page.js("document.activeElement.textContent"), "Maryland", "focus was lost")

    def test_tooltip_figures_with_real_mouse_movement(self):
        self.assertNoFailures(tooltip_failures(self.page))
        self.assertEqual(self.page.page_errors(), [])

    def test_tooltip_hides_when_the_mouse_moves_away(self):
        self.page.move_mouse(*point_on(self.page, "MT", ".cost-wrap"))
        self.assertTrue(self.page.js(TIP)["shown"])
        self.page.move_mouse(*point_on(self.page, "MT", ".race-name"))
        self.assertFalse(self.page.js(TIP)["shown"], "still shown over the race name")
        self.page.move_mouse(*point_on(self.page, "MT", ".cost-wrap"))
        self.assertTrue(self.page.js(TIP)["shown"])
        box = self.page.js("(() => { const r = document.querySelector('h1'); r.scrollIntoView(); const b = r.getBoundingClientRect(); return [b.left + 5, b.top + 5]; })()")
        self.page.move_mouse(*box)
        self.assertFalse(self.page.js(TIP)["shown"], "still shown after leaving the table")

    def test_tooltip_stays_on_screen_at_the_edges(self):
        filters = {"money": "all", "who": "all", "period": "2"}
        for width, height in ((1280, 900), (390, 844)):
            self.page.resize(width, height)
            for block in ("start", "end"):
                for race in RACES:
                    self.page.move_mouse(*point_on(self.page, race["id"], ".cost-wrap", block=block, side="right"))
                    tip = self.page.js(TIP)
                    where = f"{width}px {block} {race['id']}"
                    self.assertTrue(tip["shown"], where)
                    self.assertNoFailures(differences(expected_tip(race, filters), tip, where) + off_screen(tip, where))

    def test_info_buttons_open_and_close_with_the_mouse(self):
        self.assertEqual(open_info(self.page), [])
        self.assertEqual([s["label"] for s in info_state(self.page)],
                         ["About Money counted", "About Candidates counted", "About Campaign spending period"])
        for key in ("money", "who", "period"):
            click_info(self.page, key)
            self.assertEqual(open_info(self.page), [f"info-{key}"], "opening")
            click_info(self.page, key)
            self.assertEqual(open_info(self.page), [], "clicking the button again")
        click_info(self.page, "money")
        click_info(self.page, "who")
        self.assertEqual(open_info(self.page), ["info-who"], "only one is open at a time")
        inside = self.page.js("(() => { const r = document.getElementById('info-who').getBoundingClientRect(); return [r.left + 20, r.top + 20]; })()")
        self.page.click_mouse(*inside)
        self.assertEqual(open_info(self.page), ["info-who"], "clicking inside the pop-up")
        away = self.page.js("(() => { const r = document.querySelector('h1').getBoundingClientRect(); return [r.left + 5, r.top + 5]; })()")
        self.page.click_mouse(*away)
        self.assertEqual(open_info(self.page), [], "clicking elsewhere")

    def test_info_buttons_work_with_the_keyboard(self):
        self.page.js("document.querySelector('[aria-controls=\"info-who\"]').focus()")
        self.page.press_enter()
        self.assertEqual(open_info(self.page), ["info-who"], "Enter opens")
        self.page.press_escape()
        self.assertEqual(open_info(self.page), [], "Esc closes")
        self.assertEqual(self.page.js("document.activeElement.getAttribute('aria-controls')"), "info-who", "focus returns to the button")

    def test_info_popups_do_not_change_the_results(self):
        filters = {"money": "all", "who": "all", "period": "2"}
        for key in ("money", "who", "period"):
            click_info(self.page, key)
            self.assertNoFailures(differences(expected_view(filters), self.page.js(SNAPSHOT), key))

    def test_period_info_works_when_the_filter_is_off(self):
        set_filters(self.page, "outside", "all", "2")
        self.assertTrue(self.page.js("document.getElementById('period-field').disabled"))
        click_info(self.page, "period")
        self.assertEqual(open_info(self.page), ["info-period"])
        self.assertIn("turned off when you view outside groups alone", self.page.js("document.getElementById('info-period').textContent"))
        self.assertEqual(self.page.js("getComputedStyle(document.getElementById('info-period')).opacity"), "1", "pop-up is dimmed")

    def test_info_popups_stay_on_screen(self):
        for width, height in ((1280, 900), (800, 900), (390, 844), (320, 700)):
            self.page.resize(width, height)
            self.page.load(ROOT / "index.html")
            closed_width = self.page.js("document.documentElement.scrollWidth")
            for key in ("money", "who", "period"):
                click_info(self.page, key)
                state = next(s for s in info_state(self.page) if s["id"] == f"info-{key}")
                where = f"{width}px {key}: {state}"
                self.assertTrue(state["shown"], where)
                self.assertGreaterEqual(state["left"], 0, where)
                self.assertLessEqual(state["right"], state["pageWidth"], where)
                self.assertEqual(state["scrollWidth"], closed_width, f"adds sideways scrolling at {where}")
                click_info(self.page, key)

    def test_maryland_figures_are_the_only_hardcoded_results(self):
        maryland = next(race for race in RACES if race["id"] == "MD")
        figures = [per_vote(measure(maryland, {"money": "all", "who": who, "period": "2"})["cost"]) for who in ("all", "general")]
        copy = self.page.js("[...document.querySelectorAll('.info-pop')].map(p => p.textContent).join(' ')")
        self.assertEqual(re.findall(r"\$\d[\d,.]*", copy), figures)

    def test_wrong_figures_make_the_checks_fail(self):
        source = (ROOT / "index.html").read_text(encoding="utf-8")
        for name, (correct, wrong) in WRONG_FIGURES.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as folder:
                self.assertEqual(source.count(correct), 1, f"{name}: the code this edit targets has changed")
                folder = pathlib.Path(folder)
                (folder / "data").mkdir()
                shutil.copy(ROOT / "data" / "races.js", folder / "data" / "races.js")
                (folder / "index.html").write_text(source.replace(correct, wrong), encoding="utf-8")
                self.page.load(folder / "index.html")
                self.assertTrue(filter_failures(self.page) or tooltip_failures(self.page), f"{name} went unnoticed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
