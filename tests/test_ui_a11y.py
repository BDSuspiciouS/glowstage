"""Keyboard and screen-reader checks on the window's HTML, CSS and JavaScript that need no browser:
every control has a name, dialogs and switches say what they are, no focus outline is removed
without a replacement, and the quiet text colors keep their contrast (WCAG AA) on every surface."""

import re
import unittest
from html.parser import HTMLParser
from pathlib import Path

UI = Path(__file__).parent.parent / "glowstage" / "ui"
INDEX = (UI / "index.html").read_text(encoding="utf-8")
CSS = (UI / "style.css").read_text(encoding="utf-8")
JS = {name: (UI / name).read_text(encoding="utf-8") for name in ("app.js", "setup.js", "stage.js")}


# ---------- contrast ----------

def luminance(color: str) -> float:
    color = color.lstrip("#")
    channels = [int(color[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def contrast(a: str, b: str) -> float:
    hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def root_colors() -> dict[str, str]:
    root = re.search(r":root\s*\{(.*?)\}", CSS, re.S).group(1)
    return dict(re.findall(r"--([a-z0-9-]+)\s*:\s*(#[0-9a-fA-F]{6})\b", root))


class Contrast(unittest.TestCase):
    SURFACES = ("bg", "side", "card", "card-2")           # (hover is a brief state: checked at 4.0 below)

    def test_the_math(self):
        self.assertAlmostEqual(contrast("#000000", "#ffffff"), 21.0, places=1)
        self.assertAlmostEqual(contrast("#777777", "#ffffff"), 4.48, places=1)

    def test_text_colors_on_every_surface(self):
        c = root_colors()
        for name in ("text", "muted", "faint", "ok", "warn", "danger", "love"):
            for surface in self.SURFACES:
                with self.subTest(text=name, surface=surface):
                    self.assertGreaterEqual(contrast(c[name], c[surface]), 4.5)

    def test_quiet_text_on_hover_surface(self):
        c = root_colors()
        for name in ("text", "muted", "faint"):
            with self.subTest(text=name):
                self.assertGreaterEqual(contrast(c[name], c["hover"]), 4.0)

    def test_dark_text_on_light_buttons(self):
        c = root_colors()
        self.assertGreaterEqual(contrast(c["primary-ink"], c["primary"]), 4.5)       # main buttons
        self.assertGreaterEqual(contrast("#1c1406", c["warn"]), 4.5)                  # the Pair button
        self.assertGreaterEqual(contrast("#141418", "#f3f1ed"), 4.5)                  # toasts
        self.assertGreaterEqual(contrast("#1a0d0b", c["danger"]), 4.5)                # error toasts
        self.assertGreaterEqual(contrast(c["bg"], c["text"]), 4.5)                    # a day picked in a schedule

    def test_focus_ring_and_switch_parts_are_visible(self):
        c = root_colors()
        for surface in self.SURFACES:
            self.assertGreaterEqual(contrast(c["text"], c[surface]), 3.0)             # the focus ring
        self.assertGreaterEqual(contrast(c["ok"], c["card"]), 3.0)                    # a switch that is on

    def test_the_quiet_colors_are_only_set_in_root(self):
        """Fixing a color means changing its variable, not patching one element."""
        self.assertEqual(len(re.findall(r"--faint\s*:", CSS)), 1)
        self.assertEqual(len(re.findall(r"--muted\s*:", CSS)), 1)

    def test_faded_states_stay_readable(self):
        """Offline, paused and off rows are faded: not so far that muted text drops under AA."""
        c = root_colors()
        for selector in (r"\.tile\.is-offline", r"\.sched-row\.is-paused", r"\.night-row\.is-off"):
            alpha = float(re.search(selector + r"\s*\{\s*opacity:\s*([0-9.]+)", CSS).group(1))
            mix = "#" + "".join(f"{round(int(c['muted'][i:i + 2], 16) * alpha + int(c['card'][i:i + 2], 16) * (1 - alpha)):02x}"
                                for i in (1, 3, 5))
            with self.subTest(selector=selector):
                self.assertGreaterEqual(contrast(mix, c["card"]), 4.5)


# ---------- index.html ----------

class Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack: list[dict] = []
        self.elements: list[dict] = []

    def handle_starttag(self, tag, attrs):
        el = {"tag": tag, "attrs": dict(attrs), "text": "", "parents": [s["tag"] for s in self.stack],
              "ancestors": list(self.stack), "line": self.getpos()[0], "hidden_decor": False}
        self.elements.append(el)
        if tag not in ("input", "meta", "link", "br", "img", "use", "path", "circle", "rect"):
            self.stack.append(el)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i]["tag"] == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        for el in self.stack:
            el["text"] += data


PAGE = Page()
PAGE.feed(INDEX)
ELEMENTS = PAGE.elements


def describe(el):
    a = el["attrs"]
    return f"<{el['tag']} id={a.get('id')!r} class={a.get('class')!r}> (line {el['line']})"


def is_named(el, labels_for):
    a = el["attrs"]
    if a.get("aria-label") or a.get("aria-labelledby"):
        return True
    if a.get("id") in labels_for:
        return True
    if any(p["tag"] == "label" and p["text"].strip() for p in el["ancestors"]):
        return True
    return False


class IndexHtml(unittest.TestCase):
    def test_every_button_has_a_name(self):
        for el in ELEMENTS:
            if el["tag"] == "button" or el["attrs"].get("role") in ("button", "tab", "radio"):
                with self.subTest(el=describe(el)):
                    # (the window's main button gets its words from the script, whatever the window says)
                    self.assertTrue(el["text"].strip() or el["attrs"].get("aria-label") or el["attrs"].get("id") == "modal-ok",
                                    "an icon-only button needs aria-label")

    def test_every_field_has_a_label(self):
        labels_for = {e["attrs"].get("for") for e in ELEMENTS if e["tag"] == "label" and e["attrs"].get("for")}
        for el in ELEMENTS:
            if el["tag"] in ("input", "select", "textarea") and el["attrs"].get("type") != "hidden":
                with self.subTest(el=describe(el)):
                    self.assertTrue(is_named(el, labels_for), "no label, aria-label or wrapping <label>")

    def test_sliders_have_names_and_ranges(self):
        for el in ELEMENTS:
            if el["attrs"].get("role") == "slider":
                with self.subTest(el=describe(el)):
                    self.assertTrue(el["attrs"].get("aria-label"))
                    self.assertEqual(el["attrs"].get("tabindex"), "0")
                    self.assertIn("aria-valuemin", el["attrs"])
                    self.assertIn("aria-valuemax", el["attrs"])

    def test_switches_say_so(self):
        for el in ELEMENTS:
            if el["tag"] == "input" and "switch" in (el["attrs"].get("class") or "").split():
                with self.subTest(el=describe(el)):
                    self.assertEqual(el["attrs"].get("type"), "checkbox")
                    self.assertEqual(el["attrs"].get("role"), "switch")

    def test_dialogs_are_labelled_and_modal(self):
        found = [e for e in ELEMENTS if e["attrs"].get("role") == "dialog"]
        self.assertGreaterEqual(len(found), 2)                       # the tour and the window
        for el in found:
            with self.subTest(el=describe(el)):
                self.assertEqual(el["attrs"].get("aria-modal"), "true")
                self.assertTrue(el["attrs"].get("aria-label") or el["attrs"].get("aria-labelledby"))

    def test_labelledby_points_at_a_real_id_or_one_the_script_makes(self):
        ids = {e["attrs"].get("id") for e in ELEMENTS}
        scripted = "\n".join(JS.values())
        for el in ELEMENTS:
            target = el["attrs"].get("aria-labelledby")
            if target:
                with self.subTest(el=describe(el)):
                    self.assertTrue(target in ids or f'id: "{target}"' in scripted)

    def test_navigation_and_regions_are_named(self):
        for el in ELEMENTS:
            if el["tag"] == "nav" or (el["tag"] == "aside"):
                with self.subTest(el=describe(el)):
                    self.assertTrue(el["attrs"].get("aria-label"))

    def test_each_page_has_one_heading_one(self):
        sections = [e for e in ELEMENTS if e["tag"] == "section" and (e["attrs"].get("id") or "").startswith("page-")]
        self.assertEqual(len(sections), 4)
        for sec in sections:
            with self.subTest(page=sec["attrs"]["id"]):
                owned = [e for e in ELEMENTS if e["tag"] == "h1" and sec in e["ancestors"]]
                self.assertEqual(len(owned), 1)
                self.assertEqual(sec["attrs"].get("aria-labelledby"), owned[0]["attrs"].get("id"))

    def test_images_have_alt_and_decorative_svgs_are_hidden(self):
        for el in ELEMENTS:
            if el["tag"] == "img":
                with self.subTest(el=describe(el)):
                    self.assertIn("alt", el["attrs"])
            if el["tag"] == "svg" and not any(p["tag"] in ("defs", "symbol") for p in el["ancestors"]):
                with self.subTest(el=describe(el)):
                    hidden = el["attrs"].get("aria-hidden") == "true" or any(p["attrs"].get("aria-hidden") == "true" for p in el["ancestors"])
                    self.assertTrue(hidden or el["attrs"].get("aria-label") or el["attrs"].get("role"))

    def test_live_regions_for_messages(self):
        toasts = next(e for e in ELEMENTS if e["attrs"].get("id") == "toasts")
        self.assertEqual(toasts["attrs"].get("aria-live"), "polite")
        err = next(e for e in ELEMENTS if e["attrs"].get("id") == "modal-error")
        self.assertEqual(err["attrs"].get("role"), "alert")
        self.assertIn('role: isError ? "alert" : "status"', JS["app.js"])          # an error toast interrupts, others wait

    def test_tabs_are_wired(self):
        ids = {e["attrs"].get("id") for e in ELEMENTS}
        tabs = [e for e in ELEMENTS if e["attrs"].get("role") == "tab"]
        self.assertEqual(len(tabs), 3)
        for tab in tabs:
            with self.subTest(tab=tab["attrs"].get("data-tab")):
                self.assertIn(tab["attrs"].get("aria-controls"), ids)
                panel = next(e for e in ELEMENTS if e["attrs"].get("id") == tab["attrs"]["aria-controls"])
                self.assertEqual(panel["attrs"].get("aria-labelledby"), tab["attrs"].get("id"))

    def test_the_color_wheel_has_a_keyboard_way(self):
        wheel = next(e for e in ELEMENTS if "wheel-wrap" in (e["attrs"].get("class") or ""))
        self.assertEqual(wheel["attrs"].get("tabindex"), "0")
        self.assertTrue(wheel["attrs"].get("aria-label"))
        self.assertIn("ArrowRight", JS["app.js"][JS["app.js"].index("function bindWheel"):])
        self.assertTrue(any(e["attrs"].get("id") == "hex" for e in ELEMENTS))     # and a typed color as the plain alternative


# ---------- the h(...) calls in the scripts ----------

def calls(source: str, tag: str):
    """Every h("tag", ...) call as (start, end, text), with the parentheses balanced (strings and templates skipped)."""
    out = []
    for m in re.finditer(r'\bh\(\s*"' + tag + r'"', source):
        depth, i, quote = 0, m.start() + 1, None
        while i < len(source):
            ch = source[i]
            if quote:
                if ch == "\\":
                    i += 1
                elif ch == quote:
                    quote = None
            elif ch in "\"'`":
                quote = ch
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        out.append((m.start(), i, source[m.start():i + 1]))
    return out


def attrs_and_children(call: str):
    """The attribute object of an h() call (up to its matching brace) and what follows it."""
    start = call.find("{")
    if start < 0 or call[:start].count(",") > 1:
        return "", call
    depth, i, quote = 0, start, None
    while i < len(call):
        ch = call[i]
        if quote:
            if ch == "\\":
                i += 1
            elif ch == quote:
                quote = None
        elif ch in "\"'`":
            quote = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                break
        i += 1
    return call[start:i + 1], call[i + 1:]


class ScriptControls(unittest.TestCase):
    def test_icon_only_buttons_have_an_aria_label(self):
        seen = 0
        for name, source in JS.items():
            for _, _, call in calls(source, "button"):
                seen += 1
                attrs, kids = attrs_and_children(call)
                icon_only = "icon(" in kids and not re.search(r'["`]', kids) and "text:" not in attrs
                if icon_only:
                    with self.subTest(file=name, call=call[:90]):
                        self.assertIn("aria-label", attrs)
        self.assertGreater(seen, 40)

    def test_inputs_and_selects_are_labelled(self):
        """Each h("input") / h("select") has an aria-label, or sits inside an h("label") (or is labelled by id)."""
        seen = 0
        for name, source in JS.items():
            labels = [(s, e) for s, e, _ in calls(source, "label")]
            for tag in ("input", "select", "textarea"):
                for start, _, call in calls(source, tag):
                    attrs, _ = attrs_and_children(call)
                    if re.search(r'type:\s*"(hidden|color|range)"', attrs) and "aria-label" in attrs:
                        continue
                    seen += 1
                    named = "aria-label" in attrs or "aria-labelledby" in attrs or bool(re.search(r"\.\.\.\w+", attrs))   # (a spread: the caller names it)
                    inside = any(s < start < e for s, e in labels) or self.used_in_label(source, start, labels)
                    with self.subTest(file=name, call=call[:100]):
                        self.assertTrue(named or inside or self.label_for(source, attrs), "no label")
        self.assertGreater(seen, 40)

    @staticmethod
    def used_in_label(source: str, start: int, labels) -> bool:
        """`const box = h("input", ...)` that is later put into an h("label", ...)."""
        m = re.search(r"(?:const|let)\s+(\w+)\s*=\s*$", source[max(0, start - 60):start])
        return bool(m) and any(re.search(r"\b" + re.escape(m.group(1)) + r"\b", source[s:e]) for s, e in labels)

    @staticmethod
    def label_for(source: str, attrs: str) -> bool:
        m = re.search(r'\bid:\s*"([^"]+)"', attrs)
        return bool(m) and bool(re.search(r'\bfor:\s*"' + re.escape(m.group(1)) + '"', source))

    def test_scripted_dialogs_are_labelled(self):
        for name, source in JS.items():
            for _, _, call in calls(source, "div"):
                attrs, _ = attrs_and_children(call)
                if re.search(r'role:\s*"dialog"', attrs):
                    with self.subTest(file=name, call=call[:90]):
                        self.assertIn("aria-modal", attrs)
                        self.assertRegex(attrs, r"aria-label(ledby)?")

    def test_scripted_switches_get_their_role(self):
        """h() adds role=switch to every checkbox with the switch class, so none has to remember it."""
        helper = JS["app.js"][JS["app.js"].index("function h("):JS["app.js"].index("/** Replaces")]
        self.assertRegex(helper, r'"role",\s*"switch"')
        for name, source in JS.items():
            for _, _, call in calls(source, "input"):
                attrs, _ = attrs_and_children(call)
                if 'class: "switch"' in attrs:
                    with self.subTest(file=name, call=call[:90]):
                        self.assertIn('type: "checkbox"', attrs)

    def test_scripted_sliders_get_a_name_somewhere(self):
        for name, source in JS.items():
            for _, _, call in calls(source, "div"):
                attrs, _ = attrs_and_children(call)
                if re.search(r'role:\s*"slider"', attrs) and "aria-label" not in attrs:
                    with self.subTest(file=name, call=call[:90]):
                        self.assertIn('slider.setAttribute("aria-label"', source)      # (a tile names it as it draws)

    def test_no_button_inside_a_button(self):
        """A role=button holding other controls hides them from screen readers: the preset card and the light tile don't."""
        for name, source in JS.items():
            for _, _, call in calls(source, "button"):
                with self.subTest(file=name, call=call[:80]):
                    self.assertNotRegex(call, r'role:\s*"button"')
        self.assertNotRegex(JS["app.js"], r'class:\s*"tile"[^\n]*role:\s*"button"')

    def test_stage_has_a_keyboard_way_to_move_a_light(self):
        stage = JS["stage.js"]
        for key in ("ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"):
            self.assertIn(key, stage)
        self.assertIn("stage-live", stage)
        self.assertIn("With the keyboard", stage)

    def test_dialogs_trap_focus_and_give_it_back(self):
        app = JS["app.js"]
        self.assertIn("function trapFocus", app)
        self.assertIn("modalFrom", app)
        self.assertIn("sheetFrom", app)
        self.assertIn("finishWizard()", app[app.index('e.key !== "Escape"'):])     # Escape closes the tour too
        self.assertIn("wizard-next", JS["setup.js"])


# ---------- style.css ----------

class FocusAndMotion(unittest.TestCase):
    def rules(self):
        return re.findall(r"([^{}]+)\{([^{}]*)\}", re.sub(r"/\*.*?\*/", "", CSS, flags=re.S))

    def test_no_outline_is_removed_without_a_replacement(self):
        rules = self.rules()
        for selector, body in rules:
            if re.search(r"outline\s*:\s*(none|0)\b", body):
                base = re.sub(r":(focus-visible|focus|focus-within)\b", "", selector.strip().split(",")[0]).strip()
                with self.subTest(selector=selector.strip()):
                    ok = any(base in s and ":focus-visible" in s and re.search(r"outline\s*:\s*\d|box-shadow", b)
                             for s, b in rules)
                    self.assertTrue(ok)

    def test_there_is_a_global_focus_ring(self):
        self.assertRegex(CSS, r"(?m)^:focus-visible\s*\{\s*outline:\s*2px solid")

    def test_reduced_motion_stops_css_animation(self):
        block = re.search(r"@media \(prefers-reduced-motion: reduce\)\s*\{\s*\*.*?\}", CSS, re.S).group(0)
        self.assertIn("animation: none", block)
        self.assertIn("transition: none", block)

    def test_scripted_motion_respects_reduced_motion(self):
        app = JS["app.js"]
        self.assertIn("prefers-reduced-motion", app)
        self.assertNotRegex(app, r'behavior:\s*"smooth"')               # scrolling goes through smoothScroll()
        self.assertIn("smoothScroll", app)

    def test_small_targets_have_a_minimum_size(self):
        self.assertRegex(CSS, r"\.link-btn\s*\{[^}]*min-height:\s*(2[4-9]|[3-9]\d)px")
        self.assertRegex(CSS, r"\.chip-x\s*\{[^}]*width:\s*(2[4-9]|[3-9]\d)px")
        self.assertRegex(CSS, r'input\[type="checkbox"\]:not\(\.switch\)\s*\{[^}]*width:\s*(2[4-9]|[3-9]\d)px')

    def test_narrow_windows_wrap_instead_of_cutting_off(self):
        self.assertRegex(CSS, r"\.page-head\s*\{[^}]*flex-wrap:\s*wrap")
        self.assertRegex(CSS, r"\.head-actions\s*\{[^}]*flex-wrap:\s*wrap")



ROOT_UI = Path(__file__).resolve().parent.parent / "glowstage" / "ui"


class LeftoversFixed(unittest.TestCase):
    """What the first pass left: the Light show page's outline, and focus kept on Home's notes."""

    def test_the_light_show_page_has_headings(self):
        html = (ROOT_UI / "index.html").read_text(encoding="utf-8")
        show = html[html.index('id="page-show"'):html.index("</section>", html.index('id="page-show"'))]
        self.assertGreaterEqual(show.count('role="heading" aria-level="2"'), 5)

    def test_home_notes_give_focus_back(self):
        js = (ROOT_UI / "app.js").read_text(encoding="utf-8")
        body = js[js.index("function renderHomeNotes()"):]
        body = body[:body.index(chr(10) + "}" + chr(10))]
        self.assertIn("document.activeElement", body)
        self.assertIn(".focus(", body)


if __name__ == "__main__":
    unittest.main()
