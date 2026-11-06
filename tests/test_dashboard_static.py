import json
import os
import re
import shutil
import subprocess
import unittest
from tests import read_file

HTML = read_file(os.path.join(os.path.dirname(__file__), "..", "app", "static", "index.html"))


class ActionsNeverSilent(unittest.TestCase):
    """Static guards for the dashboard script (it has no JS test runner): the regressions that made Release/Deny/Report fraud look dead."""

    def test_no_native_confirm_dialogs(self):
        self.assertIsNone(re.search(r"(?<![\w.])confirm\(", HTML))                    # native confirm() is suppressed in embedded/headless browsers
        self.assertIn("function ask(", HTML)

    def test_every_operator_action_goes_through_act_and_act_handles_every_failure_and_reenables_buttons(self):
        raw = re.findall(r"fetch\('/api/payments/' \+ encodeURIComponent[^;]*;", HTML)                  # no raw action fetch outside act(); the one exception is the advisory /ask read
        self.assertEqual(len(raw), 1); self.assertIn("+ '/ask'", raw[0])
        body = HTML[HTML.index("async function act("):HTML.index("function decide(")]
        for needle in ("r.status === 401", "needToken(true)", "!r.ok", "j.ok === false", "catch (e)", "b.disabled = false", "toast("):
            self.assertIn(needle, body, needle)
        self.assertNotIn("banner('')", body)                                                   # an error message is never wiped by the next poll
        self.assertIn("async function post(path, body, label) { await act(", HTML)             # ruleset edits use the same path


def _lum(h):
    c = [int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


def _ratio(a, b):
    la, lb = sorted((_lum(a), _lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def _tokens():
    """Resolved Day and Night token sets, parsed from the CSS itself (Night overrides Day)."""
    css = HTML[HTML.index("<style>"):HTML.index("</style>")]
    def block(start):
        i = css.index(start) + len(start)
        return dict(re.findall(r"--([\w-]+):([^;}]+)", css[i:css.index("}", i)]))
    day = {k: v.strip() for k, v in block(":root,:root[data-theme=day]{").items()}
    night = dict(day)
    night.update({k: v.strip() for k, v in block(":root[data-theme=night]{").items()})
    return {"day": day, "night": night}


def _over(fg, a, bg):
    return "#%02x%02x%02x" % tuple(round(int(fg[i:i + 2], 16) * a + int(bg[i:i + 2], 16) * (1 - a)) for i in (1, 3, 5))


def _rgba_over(rgba, base):
    r, g, b, a = re.match(r"rgba\((\d+),(\d+),(\d+),([\d.]+)\)", rgba).groups()
    return _over("#%02x%02x%02x" % (int(r), int(g), int(b)), float(a), base)


class ProviderIdentity(unittest.TestCase):
    """Rail identity under the restraint design: chip (code + shape), band (solid vs dashed), lens counts, one filtered strip, rail-worded actions, dialog strip.
    Colour has a job: provider inks never appear on decision/button/stat styles."""

    def test_unknown_provider_renders_the_unknown_chip_never_paypal_or_stripe(self):
        self.assertIn("hasOwnProperty.call(PROVIDERS, v)) ? v : 'unknown'", HTML)               # "evil", "__proto__", "constructor" all fall to unknown
        self.assertIn("'??'", HTML)

    def test_every_confirm_and_toast_carries_the_provider(self):
        pay = HTML[:HTML.index("// ---- Policy")]                                                   # payment confirms only; the Policy page has its own non-payment confirm
        for m in re.finditer(r"await ask\(", pay):
            call = pay[m.start():m.start() + 700].split("decide(")[0]
            self.assertIn("p.data.provider", call)
        self.assertIn("toast('Done: ' + label + '.', 'ok', prov)", HTML)
        self.assertIn("'Release on ' + rail", HTML)
        self.assertIn("'Reject (' + rail + ')'", HTML)

    def test_non_colour_rail_cues_exist(self):
        for needle in ("[data-provider=stripe] .pchip{border-radius:5px}", "border-radius:999px", "repeating-linear-gradient(180deg,var(--prov-ink) 0 6px,transparent 6px 9px)", "role', 'img'", "pband", "forced-colors:active", "headerName: 'Rail'"):
            self.assertIn(needle, HTML, needle)

    def test_provider_inks_are_confined_to_rail_surfaces(self):
        css = HTML[HTML.index("<style>"):HTML.index("</style>")]
        for rule in re.findall(r"([^{}]+)\{([^}]*)\}", css):
            sel, body = rule
            if "--prov" in body:
                self.assertTrue(re.search(r":root|pchip|pedge|pstrip|pband|\.rel|railStrip|tablecard|\[data-provider|lens button\[data-provider", sel), sel)       # rail inks only on rail surfaces
        for dec in (".dec", ".stat", "button.act", ".rk", ".menu"):
            self.assertNotIn("--prov", "".join(b for s_, b in re.findall(r"([^{}]+)\{([^}]*)\}", css) if dec in s_ and "data-provider" not in s_)), dec

    def test_filtered_view_is_one_quiet_strip_and_all_view_has_no_banner(self):
        for needle in ('id="railStrip"', "Rail is fixed when a payment is created", "'Show all'", "rowHeight: 56", "animateRows: false", "prefers-reduced-motion", "tabular-nums"):
            self.assertIn(needle, HTML, needle)
        for gone in ("railBanner", "railPill", "splitbar", "row-lens", ".rail-paypal::before", "outline:2px solid rgba(255,255,255"):
            self.assertNotIn(gone, HTML, gone)
        self.assertNotIn("data-provider=", HTML[HTML.index("<body"):HTML.index("<script")])               # set per element at runtime, never statically

    def test_decisions_are_dot_and_word_and_risk_is_a_number(self):
        self.assertIn(".dec i{width:8px;height:8px;border-radius:50%", HTML)
        self.assertIn("function decisionRenderer", HTML)
        self.assertNotIn(".risk i{", HTML)                                                         # no risk bars
        self.assertNotIn("class=\"chip", HTML)

    def test_one_visible_action_per_row_and_the_rest_in_a_keyboard_reachable_menu(self):
        for needle in ("function openMenu", "role', 'menu'", "'menuitem'", "e.key === 'Escape'", "ArrowDown", "min-height:44px", "aria-haspopup"):
            self.assertIn(needle, HTML, needle)
        # the only filled red button is the dialog's destructive confirm
        css = HTML[HTML.index("<style>"):HTML.index("</style>")]
        for sel, body in re.findall(r"([^{}]+)\{([^}]*)\}", css):
            if re.search(r"background:var\(--freeze-fill\)", body):
                self.assertIn("#askYes.destructive", sel)

    def test_lens_switch_is_a_view_filter_not_a_write(self):
        body = HTML[HTML.index("function buildLens()"):HTML.index("function decisionRenderer")]
        self.assertNotIn("fetch(", body)

    def test_type_system_inter_ui_poppins_wordmark_only(self):
        self.assertIn("--font:Inter", HTML)
        self.assertIn("--wordmark:Poppins", HTML)
        css = HTML[HTML.index("<style>"):HTML.index("</style>")]
        self.assertEqual(css.count("var(--wordmark)"), 1)                                           # Poppins used for the wordmark only


class DemoReadyPass(unittest.TestCase):
    """Rails by name, Policy page, payment panel, attention filter, deterministic Ask box (spec PAYCANARY_DEMO_READY_PASS_v1)."""

    def test_rail_chip_is_the_plain_name_with_platform_shapes_and_a_theme_aware_row_wash(self):
        self.assertIn("provKey(v) === 'unknown' ? 'Unknown' : i.name", HTML)
        css = HTML[HTML.index("<style>"):HTML.index("</style>")]
        self.assertIn(".pchip{", css)
        self.assertIn("[data-provider=stripe] .pchip{border-radius:5px}", css)
        for k, ink in (("paypal", "pp"), ("stripe", "st")):
            self.assertIn("color-mix(in srgb,var(--%s-ink) var(--wash),var(--panel))}" % ink, css)
            self.assertIn(".ag-row.rail-%s{" % k, css)
            self.assertIn("button.act.go-%s{" % k, css)
        self.assertIn("go-' + provKey(p.data.provider)", HTML)                                    # rail-worded button takes the platform shape
        self.assertNotIn("<img", HTML)                                                              # no logos
        self.assertNotIn("data:image/png", HTML)
        self.assertIn("PayCanary is not endorsed by PayPal or Stripe.", HTML)

    def test_policy_page_is_three_read_only_cards_in_engine_order_with_agent_settings_collapsed(self):
        body = HTML[HTML.index("// ---- Policy"):]
        self.assertIn(">Policy</button>", HTML)
        for needle in ("How PayCanary decides", "PayCanary guards payments your agents ask to make. It doesn\\'t run your agents or your PayPal and Stripe accounts.",
                       "Always allowed", "Always stopped", "Everything else", "Advanced: per-agent settings", "Monitor (logs only)", "Active (holds payments)", "+ Add a payee"):
            self.assertIn(needle, body, needle)
        self.assertLess(body.index("Always allowed"), body.index("Always stopped"))
        self.assertLess(body.index("Always stopped"), body.index("Everything else"))
        rules = body[body.index("const RULES"):body.index("async function loadFw")]
        self.assertIn("el('summary', null, 'Details')", body)
        self.assertNotIn("checkbox", rules)                                                          # no rule toggles: the gate has none
        self.assertNotIn("Test a payment", body)
        for ep in ("/mode'", "/profile'", "/approved_payees'"):                                      # existing endpoints only
            self.assertIn(ep, body, ep)
        self.assertNotIn("Firewall ruleset", HTML)

    def test_rule_cards_are_backed_by_real_gate_constants(self):
        g = read_file(os.path.join(os.path.dirname(__file__), "..", "app", "gate.py"))
        for needle in ("WINDOW_MULT = ((3600.0, 2.0), (86400.0, 4.0), (604800.0, 10.0))", "PAYEE_WINDOW_MULT = ((86400.0, 2.0), (604800.0, 5.0))", "VELOCITY_N = 5", "FANOUT_K = 3.0",
                       "NEW_PAYEE_FLOOR = 100.0", "NEW_PAYEE_CEIL_MULT = 2.0", "COLD_START_PAYMENTS = 5", "PROBATION_ABS, PROBATION_MULT = 500.0, 3.0", "DUP_WINDOW_S = 300.0", "BURST_WINDOW_S, BURST_MULT = 60.0, 2.5",
                       "TRUST_MIN_COUNT = 3", "TRUST_MIN_AGE_S = 7 * 86400"):
            self.assertIn(needle, g, needle)
        rules = HTML[HTML.index("const RULES"):HTML.index("async function loadFw")]
        for needle in ("2x its cap in an hour, 4x in a day or 10x in a week", "more than 2x in a day or 5x in a week", "more than 5 payment attempts in a minute", "2.5x", "$100", "fewer than 5 payments", "$500", "3x",
                       "3 payments over 7 days", "5 minutes", "announced"):
            self.assertIn(needle, rules, needle)

    def test_panel_has_plain_verdict_clickable_related_rows_and_sticky_actions_with_the_old_handlers(self):
        for needle in ("function verdictLine(", "Nothing has been sent.", "'Payments to this payee ('", "b.onclick = () => openDrawer(x)", ".dfoot{", "'Mark as fraud'", "'Audit trail'",
                       "h.release()", "h.deny()", "h.fraud()", "function actionHandlers("):
            self.assertIn(needle, HTML, needle)
        vl = HTML[HTML.index("function verdictLine("):HTML.index("function relatedRow(")]
        self.assertNotIn("then released", vl)                                                        # never claim a release the status does not prove
        for line in vl.split("\n"):                                                                  # "Nothing has been sent" only for frozen / denied
            if "Nothing has been sent" in line:
                self.assertTrue("'frozen'" in line or "'denied'" in line, line)
        self.assertNotIn("reason_field", HTML)                                                       # no reject-reason field without an audit write path

    def test_checked_against_line_only_when_the_cap_is_in_the_loaded_ruleset(self):
        self.assertIn("typeof a.hard_cap === 'number' && !a.profile", HTML)
        self.assertIn("Checked against: per-payment cap", HTML)

    def test_needs_attention_filter_sits_with_the_lens_and_search_stays(self):
        for needle in ('id="attn"', "'Needs attention'", "const needsAttention", "rowPass", 'id="q"', "quickFilterText"):
            self.assertIn(needle, HTML, needle)

    def test_ask_paycanary_is_advisory_free_type_with_a_canned_floor_and_cannot_act(self):
        a = HTML[HTML.index("// ---- Ask PayCanary"):HTML.index("async function openDrawer(")]
        for q in ("Why was this held?", "Show similar payments", "What would let this through?", "Is this payee on our list?"):
            self.assertIn(q, a, q)
        self.assertIn("Advisory only. It can\\'t release, reject or change anything.", a)
        for banned in ("innerHTML", "insertAdjacentHTML", "outerHTML", "document.write", "decide(", "act(", "post(", "XMLHttpRequest", "/release", "/deny", "/confirm_fraud", "anthropic", "openai"):
            self.assertNotIn(banned, a, banned)
        self.assertEqual(len(re.findall(r"fetch\(", a)), 1)                                         # the only network call in the box is the advisory /ask
        ask = a[a.index("async function askModel("):a.index("function askBox(")]
        self.assertIn("/ask", ask); self.assertIn("j.source === 'model'", ask); self.assertIn("catch (e) { return { reason: 'timeout' }; }", ask)
        for wall in ("today\\'s limit", "took too long", "offline right now", "could not give a clean answer", "Retry"):     # each wall is named, and the deterministic answer is always shown
            self.assertIn(wall, a, wall)
        self.assertIn("el('div', 'det', can)", a)
        self.assertIn("async function cannedFor(", a); self.assertIn("await cannedFor(q, d)", a)    # model down / guarded out / error => canned answer, never a dead box
        self.assertIn("form.onsubmit", a); self.assertIn("e.preventDefault()", a)                    # Enter submits
        self.assertIn("inp.maxLength = 300", a); self.assertIn("aria-label", a)
        self.assertIn("m.textContent =", a)                                                          # the answer is untrusted text: textContent only
        self.assertNotIn("innerHTML", HTML)                                                          # untrusted payee / memo / agent text is only ever set with textContent
        self.assertNotIn("insertAdjacentHTML", HTML)

    def test_new_row_wash_and_chip_contrast_in_both_themes(self):
        def over(ink, a, base):                                                                      # ink at alpha a over base
            c = [round(int(ink[i:i + 2], 16) * a + int(base[i:i + 2], 16) * (1 - a)) for i in (1, 3, 5)]
            return "#%02x%02x%02x" % tuple(c)
        for ink, panel, floor in (("#46A6FF", "#111418", 4.5), ("#B993FF", "#111418", 4.5), ("#0a64c8", "#ffffff", 4.5), ("#6b3fd6", "#ffffff", 4.5)):
            wash = over(ink, 0.11, panel)
            self.assertGreaterEqual(_ratio(ink, wash), floor, (ink, wash))                           # outlined rail button text on the wash
            for body in ("#e8eaee", "#16191d"):
                if (body == "#e8eaee") == (panel == "#111418"):
                    self.assertGreaterEqual(_ratio(body, wash), 10, (body, wash))                    # body text on the wash
        for fg, bg in (("#8b94a1", over("#46A6FF", 0.11, "#111418")), ("#8b94a1", over("#B993FF", 0.11, "#111418")), ("#5d6672", over("#0a64c8", 0.11, "#ffffff")), ("#5d6672", over("#6b3fd6", 0.11, "#ffffff"))):
            self.assertGreaterEqual(_ratio(fg, bg), 4.5, (fg, bg))                                   # muted text on the wash


class PlainReasons(unittest.TestCase):
    """plainReason(): the engine's reason texts as plain sentences; unmatched text falls back unchanged; output via textContent only."""

    def _table(self):
        a = HTML.index("const PLAIN_REASONS = ["); b = HTML.index("function plainReason(")
        return re.findall(r"\[/(.*?)/(i?), '((?:[^'\\]|\\.)*)'\]", HTML[a:b])

    def test_every_seeded_reason_pattern_is_mapped(self):
        samples = {
            "known payee 'Figma', amount 60 within this agent's norm": "Known payee, normal amount.",
            "first-ever payee 'Delta Supply Co' for this agent (amount 85, within norms)": "First payment to this payee.",
            "on cadence: 'Harbor Coworking' is paid about every 30 days (last 30 days ago), amount 1450 in line": "On its usual schedule.",
            "cumulative 190 in 1 day(s) to 'Veloxa Payments', a payee not yet trusted (needs 3 payments over 7 days or operator approval); budget 172.5: structuring guard": "Too much going to a new payee in a short time.",
            "payee 'QuickTools LLC' is NOT in the agent's stated intent ['AWS'] (intent mismatch)": "This payee isn't in what the agent said it would pay.",
            "amount 9500 is over this agent's hard cap 4000": "Over this agent's spending limit.",
            "off-cycle: 'Harbor Coworking' was last paid 1.46 days ago; it is usually paid about every 30 days": "Paid earlier or later than this bill's usual cycle.",
        }
        pats = [(re.compile(p.replace("\\/", "/"), re.I if f else 0), t.replace("\\'", "'")) for p, f, t in self._table()]
        for raw, want in samples.items():
            got = next((t for r, t in pats if r.search(raw)), raw)                                    # first match wins, as in plainReason()
            self.assertEqual(got, want, raw)

    def test_unmatched_reasons_fall_back_to_the_raw_text(self):
        self.assertIn("return m ? m[1] : t;", HTML)
        pats = [re.compile(p, re.I if f else 0) for p, f, _ in self._table()]
        self.assertFalse(any(p.search("some brand new engine reason") for p in pats))

    def test_plain_reason_is_used_for_the_table_the_verdict_and_keeps_the_raw_text(self):
        self.assertIn("valueFormatter: (p) => plainReason(p.value)", HTML)
        self.assertIn("tooltipField: 'reason'", HTML)
        self.assertIn("'Technical detail'", HTML)
        self.assertIn("noDot(plainReason(d.reason))", HTML)

    def test_a_frozen_decision_that_is_no_longer_frozen_never_says_nothing_was_sent(self):
        vl = HTML[HTML.index("function verdictLine("):HTML.index("function relatedRow(")]
        line = next(l for l in vl.split("\n") if "d.verdict === 'freeze'," in l or "d.verdict === 'freeze')" in l and "enforced" not in l)
        self.assertNotIn("Nothing has been sent", line)


class RowSelection(unittest.TestCase):
    """The open payment is the selected row: whole-row highlight + 2px rule inside the band, cleared when the panel closes."""

    def test_selected_row_css_and_hover_is_a_lighter_step(self):
        css = HTML[HTML.index("<style>"):HTML.index("</style>")]
        self.assertIn(".ag-row.row-open{box-shadow:inset 0 0 0 9999px var(--sel-ov)}", css)
        self.assertIn("--ag-row-hover-color:var(--hov-ov)", css)
        self.assertIn(".ag-row.row-open .ag-cell.railc::after{content:\"\";position:absolute;left:4px;top:0;bottom:0;width:2px;background:var(--focus)", css)   # just inside the 4px band
        self.assertIn(".ag-cell.ag-cell-focus", css)                                                  # no per-cell focus box for the mouse
        self.assertIn(".ag-row:has(.ag-cell:focus-visible){outline:2px solid var(--focus)", css)     # keyboard focus keeps a visible 2px ring

    def test_selection_follows_the_panel_and_clears_on_every_close_path(self):
        self.assertIn("function closeDrawer() { $('#drawer').hidden = true; drawerId = null; markOpen(); }", HTML)
        self.assertIn("close.onclick = closeDrawer;", HTML)
        self.assertIn("if (e.key === 'Escape' && drawerId !== null", HTML)
        self.assertIn("closeDrawer(); $('#grid').style.display", HTML)                              # leaving the Payments tab
        self.assertIn("drawerId = d.payment_id; markOpen();", HTML)                                  # opening another payment moves it
        self.assertIn("(p.data.payment_id === drawerId ? ' row-open' : '')", HTML)                   # rows created while open (scroll, refresh)
        self.assertIn("onRowDataUpdated: () => syncSelection()", HTML)
        self.assertGreaterEqual(HTML.count("syncSelection();"), 3)                                   # rail lens, Needs attention, search re-sync the mark and the note
        self.assertIn("onCellFocused:", HTML)                                                        # arrow keys move the selection with the panel


class SelectedStateIsRobust(unittest.TestCase):
    """Regression (b7bea21 report: 'all grey, no highlight'): the selected state must not depend on color-mix/gradients, must be clearly above hover, and must survive data refreshes."""

    def test_selected_is_a_plain_rgba_overlay_well_above_hover_in_both_themes(self):
        t = _tokens()
        for theme in ("day", "night"):
            sel, hov = float(re.search(r",([\d.]+)\)$", t[theme]["sel-ov"]).group(1)), float(re.search(r",([\d.]+)\)$", t[theme]["hov-ov"]).group(1))
            self.assertGreaterEqual(sel, 0.12, theme)                                                 # >= 12% ink, as asked
            self.assertGreaterEqual(sel, 2 * hov, theme)                                              # hover stays a clearly lighter step
        css = HTML[HTML.index("<style>"):HTML.index("</style>")]
        rule = re.search(r"\.ag-row\.row-open\{([^}]*)\}", css).group(1)
        self.assertNotIn("color-mix", rule)
        self.assertNotIn("gradient", rule)

    def test_rail_wash_has_a_precomputed_fallback_for_engines_without_color_mix(self):
        t = _tokens()
        for theme, pct in (("day", .07), ("night", .09)):
            for rail, ink in (("paypal", "pp-ink"), ("stripe", "st-ink")):
                want = _over(t[theme][ink], pct, t[theme]["panel"]).lower()
                pre = ":root[data-theme=night] " if theme == "night" else ""
                self.assertIn("%s.ag-row.rail-%s{background-color:%s;background-color:color-mix(" % (pre, rail, want), HTML, (theme, rail, want))

    def test_selection_survives_data_refresh_scroll_and_row_recreation(self):
        self.assertIn("onRowDataUpdated: () => syncSelection(), onModelUpdated: () => markOpen(), onViewportChanged: () => markOpen()", HTML)   # poll every 3s re-applies it
        self.assertIn("(p.data.payment_id === drawerId ? ' row-open' : '')", HTML)                   # rows the grid re-creates are born selected
        self.assertIn("setTimeout(markOpen, 60)", HTML)                                               # after ensureIndexVisible renders the row
        sync = HTML[HTML.index("function syncSelection()"):HTML.index("function showHidden(d)")]
        self.assertNotIn("drawerId = ", sync)                                                         # a refresh never clears the selection
        self.assertNotIn("closeDrawer()", sync)


class DayNightThemes(unittest.TestCase):
    """Day / Night (PAYCANARY_DAY_NIGHT_PALETTE_v1): tokens, computed contrast, follow-system default, saved choice, URL override, nothing hard-coded dark in Day."""
    REQUIRED = ("bg", "panel", "panel2", "sel", "line", "ctl", "ink", "mute", "brand", "brand-ink", "allow", "review", "freeze", "dot-allow", "dot-review", "dot-freeze", "freeze-fill", "on-freeze",
                "focus", "focus-gap", "tab-line", "shadow-card", "shadow", "scrim", "pp", "pp-ink", "st", "st-ink", "wash", "sel-ov", "hov-ov")

    def test_palette_tokens_exist_with_the_specified_values(self):
        t = _tokens()
        for theme in ("day", "night"):
            for k in self.REQUIRED:
                self.assertIn(k, t[theme], (theme, k))
        spec = {"day": {"bg": "#F5F7F8", "panel": "#FFFFFF", "panel2": "#EEF3F5", "sel": "#E3ECF0", "line": "#DDE4EA", "ctl": "#7C8C9E", "ink": "#0F2236", "brand": "#F5C518", "dot-allow": "#34C759", "dot-review": "#FF9500", "dot-freeze": "#FF3B30", "wash": "7%", "focus": "#0F2236"},
                "night": {"bg": "#05090F", "panel": "#0B121B", "panel2": "#111B27", "sel": "#16222F", "line": "#1B2836", "ctl": "#5B6B7E", "ink": "#E8EEF5", "brand": "#F5C518", "dot-allow": "#30D158", "dot-review": "#FF9F0A", "dot-freeze": "#FF453A", "wash": "9%", "focus": "#F5C518", "shadow": "none", "shadow-card": "none"}}
        for theme, d in spec.items():
            for k, v in d.items():
                self.assertEqual(t[theme][k], v, (theme, k))
        self.assertEqual(t["day"]["pp"], "#005BB8"); self.assertEqual(t["day"]["st"], "#5B35E8")      # chip fills identical in both themes
        self.assertEqual(t["night"]["pp"], "#005BB8"); self.assertEqual(t["night"]["st"], "#5B35E8")

    def test_contrast_matrix_from_the_tokens_in_both_themes(self):
        t = _tokens()
        for theme in ("day", "night"):
            T = t[theme]
            surf = {"page": T["bg"], "card": T["panel"], "raised": T["panel2"], "selected": T["sel"], "card+sel": _rgba_over(T["sel-ov"], T["panel"])}
            for rail in ("pp-ink", "st-ink"):
                w = _over(T[rail], float(T["wash"].rstrip("%")) / 100, T["panel"])
                surf["wash-" + rail] = w; surf["wash+sel-" + rail] = _rgba_over(T["sel-ov"], w); surf["wash+hover-" + rail] = _rgba_over(T["hov-ov"], w)
            for tok in ("ink", "mute", "pp-ink", "st-ink", "review", "freeze"):                          # every text token on every surface a row or panel can show
                for name, bg in surf.items():
                    self.assertGreaterEqual(_ratio(T[tok], bg), 4.5, (theme, tok, name, round(_ratio(T[tok], bg), 2)))
            for name in ("page", "card", "raised", "wash-pp-ink", "wash-st-ink"):                         # green text is only ever used outside selected rows
                self.assertGreaterEqual(_ratio(T["allow"], surf[name]), 4.5, (theme, "allow", name))
            for ui in ("ctl", "focus"):                                                                    # control borders and focus ring: UI >= 3
                for name in ("page", "card"):
                    self.assertGreaterEqual(_ratio(T[ui], surf[name]), 3.0, (theme, ui, name, round(_ratio(T[ui], surf[name]), 2)))
            for rail in ("pp-ink", "st-ink"):                                                              # rail band/border ink on the card
                self.assertGreaterEqual(_ratio(T[rail], surf["card"]), 3.0, (theme, rail))
            self.assertGreaterEqual(_ratio(T["brand-ink"], T["brand"]), 4.5)                               # primary button text on canary
            self.assertGreaterEqual(_ratio(T["on-freeze"], T["freeze-fill"]), 4.5)                         # destructive confirm
            self.assertGreaterEqual(_ratio("#ffffff", T["pp"]), 4.5); self.assertGreaterEqual(_ratio("#ffffff", T["st"]), 4.5)   # chip text
        self.assertLess(_ratio("#F5C518", t["day"]["panel"]), 3)                                          # documents why canary is fill-only in Day

    def test_nothing_is_hard_coded_dark_outside_the_token_blocks(self):
        css = HTML[HTML.index("<style>"):HTML.index("</style>")]
        css = re.sub(r":root,:root\[data-theme=day\]\{[^}]*\}", "", css); css = re.sub(r":root\[data-theme=night\]\{[^}]*\}", "", css)
        t = _tokens(); washes = {_over(t["day"][r], .07, t["day"]["panel"]) for r in ("pp-ink", "st-ink")} | {_over(t["night"][r], .09, t["night"]["panel"]) for r in ("pp-ink", "st-ink")}
        allowed = {"#fff", "#ffffff"} | washes
        for line in css.splitlines():
            if "[data-theme=night]" in line and "background-image" in line:                                   # the Night search icon
                continue
            for h in re.findall(r"#[0-9a-fA-F]{3,8}\b", line):
                self.assertIn(h.lower(), allowed, (h, line[:90]))
        self.assertEqual(re.findall(r"rgba?\(", css), [])                                                   # no literal colours: shadows, scrim, overlays are tokens
        self.assertNotIn("data-theme=light", HTML)                                                           # the old light override is gone
        self.assertNotIn("#16191d", css)

    def test_canary_is_fill_only_never_text_or_a_line(self):
        css = HTML[HTML.index("<style>"):HTML.index("</style>")]
        for sel, body in re.findall(r"([^{}]+)\{([^}]*)\}", css):
            if "var(--brand)" in body:
                self.assertRegex(body, r"background:var\(--brand\)", sel)                                  # only ever a fill (plus its matching border)
                self.assertNotRegex(body, r"[^-]color:var\(--brand\)", sel)
        self.assertIn("border-bottom-color:var(--tab-line)", css)                                            # active tab line: navy in Day, canary in Night
        self.assertIn("box-shadow:0 0 0 2px var(--focus-gap)", css)                                          # 2px white gap around the Day ring

    def test_toggle_is_accessible_persistent_and_storage_safe(self):
        for needle in ('id="themeBtn"', "'Switch to ' + next + ' mode'", "width:44px;height:44px", "localStorage.setItem('pc_theme', t)", "catch (e) { /* storage blocked",
                       "'ag-theme-quartz-dark' : 'ag-theme-quartz'", "meta[name=color-scheme]", 'name="color-scheme"'):
            self.assertIn(needle, HTML, needle)
        self.assertIn("try { localStorage.setItem", HTML)

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_resolver_follows_system_then_saved_then_url_and_survives_blocked_storage(self):
        src = HTML[HTML.index("(function () {"):HTML.index("})();") + 5]
        def run(search, saved, dark, storage_ok=True, mm=True):
            js = ("var out={};var document={documentElement:{dataset:out}};var location={search:%s};" % json.dumps(search)
                  + ("var localStorage={getItem:function(){return %s}};" % json.dumps(saved) if storage_ok else "var localStorage={getItem:function(){throw new Error('blocked')}};")
                  + ("var matchMedia=function(){return {matches:%s}};" % ("true" if dark else "false") if mm else "var matchMedia=undefined;") + src + ";console.log(JSON.stringify(out))")
            return json.loads(subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True).stdout)
        self.assertEqual(run("", None, True), {"theme": "night", "themeSrc": "system"})                     # default follows the system, both ways
        self.assertEqual(run("", None, False), {"theme": "day", "themeSrc": "system"})
        self.assertEqual(run("", "night", False)["theme"], "night")                                          # a saved choice beats the system
        self.assertEqual(run("", "day", True)["theme"], "day")
        self.assertEqual(run("?theme=night", "day", False), {"theme": "night", "themeSrc": "url"})          # URL beats saved and system
        self.assertEqual(run("?theme=day", "night", True)["theme"], "day")
        self.assertEqual(run("?x=1&theme=light", None, True)["theme"], "day")                                # aliases
        self.assertEqual(run("?theme=dark", None, False)["theme"], "night")
        self.assertEqual(run("?theme=nonsense", None, True)["theme"], "night")                               # unknown value ignored
        self.assertEqual(run("", None, True, storage_ok=False)["theme"], "night")                            # storage blocked: still follows the system
        self.assertEqual(run("", "night", True, storage_ok=False, mm=False)["theme"], "day")                 # nothing available: Day


class OneSelectionPath(unittest.TestCase):
    """Every way of opening a payment (grid click, Enter/arrows, related-payment row) goes through selectPayment()."""

    def test_only_selectPayment_and_closeDrawer_write_drawerId(self):
        writes = re.findall(r"\bdrawerId = ([^;]+);", HTML)
        self.assertEqual(len(writes), 3)                                                              # declaration, closeDrawer, selectPayment
        self.assertEqual(HTML.count("drawerId = d.payment_id;"), 1)
        sel = HTML[HTML.index("function selectPayment("):HTML.index("const isHidden")]
        for needle in ("drawerId = d.payment_id; markOpen();", "ensureIndexVisible", "setTimeout(markOpen"):
            self.assertIn(needle, sel, needle)
        self.assertNotIn(".focus(", sel)                                                              # scrolling never steals focus from the panel
        self.assertIn("selectPayment(d); const box = $('#drawer');", HTML)                            # openDrawer is the only caller of it

    def test_every_opener_calls_openDrawer(self):
        self.assertIn("b.onclick = () => openDrawer(x)", HTML)                                        # related-payment row
        self.assertIn("openDrawer(e.data)", HTML)                                                     # grid click and Enter
        self.assertIn("openDrawer(n.data)", HTML)                                                     # arrow keys
        self.assertIn("openDrawer(f)", HTML)                                                          # refresh after an action

    def test_previous_selection_is_cleared_by_the_same_function(self):
        mo = HTML[HTML.index("function markOpen()"):HTML.index("function closeDrawer()")]
        self.assertIn("classList.toggle('row-open'", mo)                                              # toggle with a boolean: every other row loses the class
        self.assertIn("r.getAttribute('row-id') === drawerId", mo)

    def test_hidden_by_filter_keeps_the_panel_never_changes_filters_silently_and_offers_show_it(self):
        for needle in ("This payment is hidden by the current filter. ", "'Show it'", "function showHidden(d)", "const isHidden = (d)", "n.displayed", "id = 'hiddenNote'"):
            self.assertIn(needle, HTML, needle)
        sh = HTML[HTML.index("function showHidden(d)"):HTML.index("function showHidden(d)") + 520]
        self.assertIn("if (!lensPass(d)) lens = 'all'; if (attn && !needsAttention(d)) attn = false;", sh)   # resets only what hides it
        sync = HTML[HTML.index("function syncSelection()"):HTML.index("function showHidden(d)")]
        self.assertNotIn("closeDrawer", sync)                                                         # a hidden row no longer closes the panel
        self.assertNotIn("lens =", sync)
        self.assertNotIn("attn =", sync)

    def test_panel_marks_the_open_payment_current_in_the_payment_list(self):
        for needle in ("aria-current', 'true'", "#drawer .rel.cur{", "relatedRow(x, x.payment_id === d.payment_id)"):
            self.assertIn(needle, HTML, needle)


@unittest.skipUnless(shutil.which("node"), "node not installed")
class VerdictWithAudit(unittest.TestCase):
    """verdictLine() run for real (node) against fixtures: a release audit event changes only the wording, never the data."""

    def _run(self, d, audit):
        fn = HTML[HTML.index("const PLAIN_REASONS = ["):HTML.index("function evidenceOf(")] + HTML[HTML.index("const noDot"):HTML.index("async function getRuleset")] + HTML[HTML.index("function verdictLine("):HTML.index("function relatedRow(")]
        out = subprocess.run(["node", "-e", fn + "\nconsole.log(JSON.stringify(verdictLine(%s, %s)))" % (json.dumps(d), json.dumps(audit))], capture_output=True, text=True, check=True).stdout
        return json.loads(out)

    BASE = {"verdict": "freeze", "enforced": 1, "status": "sent", "reason": "amount 9500 is over this agent's hard cap 4000"}

    def test_released_after_freeze_says_an_operator_released_it(self):
        head = self._run(self.BASE, [{"event": "decision", "actor": "gate"}, {"event": "released", "actor": "operator"}])[1]
        self.assertEqual(head, "PayCanary froze this payment. An operator released it, and it has been sent.")

    def test_no_release_event_keeps_the_current_status_wording(self):
        for audit in (None, [], [{"event": "decision", "actor": "gate"}]):
            kind, head, tail = self._run(self.BASE, audit)
            self.assertEqual(head, "PayCanary froze this payment.")
            self.assertIn("Current status: sent.", tail)
            self.assertNotIn("Nothing has been sent", tail)

    def test_frozen_status_still_says_nothing_sent_even_if_a_release_row_exists(self):
        kind, head, tail = self._run(dict(self.BASE, status="frozen"), [{"event": "released"}])
        self.assertEqual(head, "PayCanary froze this payment.")
        self.assertIn("Nothing has been sent.", tail)


class MonitorWording(unittest.TestCase):
    """The mode word is 'monitor' everywhere a person can read it; the API value 'shadow' is unchanged."""

    def test_no_user_visible_shadow_or_learning_wording(self):
        body = HTML[HTML.index("<body"):]
        text = re.sub(r"(?m)^\s*//.*$", "", re.sub(r"(?s)/\*.*?\*/", "", body))                                           # comments are not user-visible
        self.assertNotRegex(text, r"(?i)learning")
        for lit in re.findall(r"'([^'\n]{9,})'", text):                                                    # prose string literals only; identifiers and the API value 'shadow' are shorter / not prose
            self.assertNotRegex(lit, r"(?i)\bshadow\b", lit)
        for needle in ("'Would freeze (monitor)'", "'Monitor (logs only)'", "'Active (holds payments)'", "monitor mode, so it was only logged.", "(monitor mode: would freeze)", "'monitor mode (logs only)' : 'active mode (holds payments)'", "const MONITOR_TIP", "d.title = MONITOR_TIP", "c.title = MONITOR_TIP",
                       "Monitor records what PayCanary WOULD do without holding any payment", "Monitor mode: PayCanary would have frozen this"):
            self.assertIn(needle, HTML, needle)
        self.assertNotIn("Phase 2", HTML)                                                                   # no dev notes on the Policy page
        self.assertEqual(HTML.count("Monitor mode: PayCanary would have frozen this"), 1)                    # one wording, defined once
        self.assertIn("['shadow', 'Monitor (logs only)']", HTML)                                             # option VALUE stays 'shadow' for the API
        self.assertIn("{ mode: sel.value }", HTML)


if __name__ == "__main__":
    unittest.main()
