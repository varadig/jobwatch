"""The Settings page."""
import json

from jobwatch.domain.scoring import DESC_MAX, SCORING_KINDS
from jobwatch.models import FETCHED_SOURCES, SOURCES_INFO, WORK_MODES
from jobwatch.sources.registry import OPTIONAL_BOARDS, SEARCH_FILTERABLE
from jobwatch.services.presets import NEW_PRESET, preset_label, template_presets
from jobwatch.services.refresh import KEPT_ON_NEW_SEARCH
from jobwatch.util.text import e
from jobwatch.web.components import ic, page, ui_field, ui_sec, ui_toggle, wm_html
from jobwatch.web.views.sources import custom_sources_card
from jobwatch.config import config_store


SETTINGS_NAV = (("presets", "Presets"), ("notify", "Notifications"), ("search", "Job search"), ("own", "Own sources"), ("mail", "E-mail alerts"), ("scoring", "Scoring"),
                ("claude", "Claude API"), ("general", "General"), ("danger", "Maintenance"))


def settings_page(flash=""):
    with config_store.lock:
        cfg = config_store.load()
    n, m = cfg["ntfy"], cfg["email"]
    chk = lambda b: " checked" if b else ""
    sub = "%s/%s" % (n["server"].rstrip("/"), n["topic"])
    box = lambda k: '<label><input type="checkbox" class="checkbox checkbox-sm checkbox-primary" name="src_%s"%s> %s</label>' % (
        k, chk(cfg["sources"].get(k)), e(SOURCES_INFO[k][0]))
    src_boxes = "".join(box(k) for k in FETCHED_SOURCES if k not in OPTIONAL_BOARDS)
    board_boxes = "".join(box(k) for k in OPTIONAL_BOARDS)
    modes = "".join('<label><input type="checkbox" class="checkbox checkbox-sm checkbox-primary" name="nwm_%s"%s> %s</label>' % (
        k, chk(k in cfg.get("notify_work_modes", ["remote", "unknown"])), wm_html(k)) for k in WORK_MODES)
    filt = "".join('<option value="%s"%s>%s</option>' % (k, " selected" if k == cfg.get("default_work_filter", "remote_unknown") else "", l)
                   for k, l in (("remote", "Remote only"), ("remote_unknown", "Remote + unknown"),
                                ("remote_hybrid", "Remote + hybrid"), ("all", "All")))
    filt_boxes = "".join('<label><input type="checkbox" class="checkbox checkbox-sm checkbox-primary" name="sf_%s"%s> %s</label>' % (
        k, chk((cfg.get("search_filter") or {}).get(k)), e(SOURCES_INFO[k][0])) for k in SEARCH_FILTERABLE)
    set_badge = lambda on: '<span class="badge badge-soft badge-success">set</span>' if on else ""

    def ta(name, label, val, rows, help_="", wide=True):
        return '<div%s><label for="%s">%s</label><textarea class="textarea ui-mono" id="%s" name="%s" rows="%d">%s</textarea>%s</div>' % (
            ' class="wide"' if wide else "", name, label, name, name, rows, e(val),
            ('<span class="help">%s</span>' % help_) if help_ else "")

    def test(what, label):
        return '<button class="btn btn-sm btn-outline" formaction="/test" name="what" value="%s">%s</button>' % (what, label)

    notify = ui_sec("notify", "Notifications", "Where new, relevant postings are announced.",
        '<h3 class="ui-h3">Push notifications (ntfy)</h3><div class="ui-grid">'
        + ui_toggle("ntfy_enabled", "Send push notifications", n.get("enabled"))
        + '<div class="wide"><ol class="ui-steps"><li>Install the <b>ntfy</b> app on your phone (App Store / Play Store).</li>'
          '<li>In the app: “+” → Subscribe to topic → <code>%s</code> (server: %s).</li>'
          '<li>Press “Save and send a test push” below.</li></ol>'
          '<span class="help">The topic name works as a password: anyone who has it can read the notifications. '
          'Subscribe link: <a href="%s" target="_blank" rel="noopener">%s</a></span></div>' % (e(n["topic"]), e(n["server"]), e(sub), e(sub))
        + ui_field("ntfy_server", "Server", n["server"]) + ui_field("ntfy_topic", "Topic", n["topic"])
        + '</div><h3 class="ui-h3">E-mail</h3><div class="ui-grid">'
        + ui_toggle("email_enabled", "Send e-mail notifications", m.get("enabled"),
                    "Gmail needs an <b>app password</b> (Google account → Security → 2-Step Verification → App passwords), "
                    "not your normal password.")
        + ui_field("smtp_host", "SMTP server", m["smtp_host"]) + ui_field("smtp_port", "Port", m["smtp_port"], "number")
        + ui_field("smtp_user", "User (e-mail address)", m["username"], "email")
        + '<div><label for="smtp_pass">Password %s</label><input class="input" id="smtp_pass" type="password" name="smtp_pass" '
          'placeholder="Leave empty to keep" autocomplete="new-password"></div>' % set_badge(m.get("password"))
        + ui_field("email_to", "Recipient", m["to"], "email", "Empty: the sender's own address.")
        + '</div><h3 class="ui-h3">When to notify</h3><div class="ui-grid">'
        + ui_field("notify_min_score", "Minimum score", cfg["notify_min_score"], "number",
                   "A posting with at least this many points is announced.")
        + '<div><label>Work modes</label><div class="ui-checks">%s</div>'
          '<span class="help">No notification for the unticked ones.</span></div></div>' % modes,
        test("ntfy", "Save and send a test push") + test("email", "Save and send a test e-mail"))

    search = ui_sec("search", "Job search", "Which sites are searched, and how often.",
        '<div class="ui-grid"><div class="wide"><label>Sources</label><div class="ui-checks">%s</div></div>' % src_boxes
        + ('<div class="wide"><label>Optional boards</label><div class="ui-checks">%s</div><span class="help">Free public job boards, '
           'switch on the ones you want. Those that list every trade keep only the postings matching a search term '
           '(a word of it in the title). Remote Game Jobs and Work With Indies are for game roles.</span></div>' % board_boxes)
        + ui_field("interval", "Refresh every (minutes)", cfg["interval_minutes"], "number", "At least 15.", ' min="15"')
        + '<div><label>Work mode (remote / hybrid / on-site)</label><label class="ui-toggle" style="font-weight:400"><input type="checkbox" '
          'class="toggle toggle-sm toggle-primary" name="mode_claude"%s> Claude reads the postings the rules cannot settle</label>'
          '<span class="help">First the job board\'s own data and the posting text (in many languages) decide; Claude only reads '
          'what is still unknown, about ¼ cent per posting. You can always set it by hand on the card.</span></div>' % chk(cfg.get("mode_claude", True))
        + ta("search_terms", "What to search for", "\n".join(cfg.get("search_terms") or []), 7,
             "One per line: keywords, optionally <code>| location</code> (e.g. <code>unity | Hungary</code>). Used by every "
             "source: LinkedIn, Remotive and Flexa search with them, and the sites ticked below keep only the postings "
             "that contain every word of a line. LinkedIn: postings from the last 7 days.")
        + ta("search_locations", "Default locations (LinkedIn)", "\n".join(cfg.get("search_locations") or []), 3,
             "For the lines without a location, one per line: e.g. <code>European Union</code>, <code>Hungary</code>, "
             "<code>Worldwide</code>. Each such line is searched in every one, so keep the list short.", wide=False)
        + '<div><label>Keep only matching postings from</label><div class="ui-checks">%s</div><span class="help">These '
          'sites list all their jobs. Untick a hand-picked list (e.g. game studios) whose job titles vary; the '
          'scoring still ranks those.</span></div>' % filt_boxes
        + ta("studios", "Company job boards", "\n".join(cfg.get("studios") or []), 6,
             "One per line: <code>ashby|lever|greenhouse|bamboohr|smartrecruiters | id | name</code>.")
        + ta("rss_feeds", "RSS feeds", "\n".join(cfg.get("rss_feeds") or []), 3, "One URL per line.") + '</div>')

    own = ui_sec("own", "Own sources", "Any other job site, with a login if it needs one.",
                 custom_sources_card(cfg), test("custom", "Save and test own sources"))

    ms = cfg.get("mail_source") or {}
    mail = ui_sec("mail", "E-mail alerts", "For sites that only send their jobs by e-mail or need a login you cannot "
                  "automate (Jack &amp; Jill, LinkedIn job alerts, Indeed, Glassdoor…): jobwatch reads those e-mails from your "
                  "mailbox. It only reads: the mailbox is opened read-only, nothing is marked as read, moved or deleted. "
                  "Turn the source on under Job search → Sources.",
        '<div class="ui-grid">'
        + ta("mail_senders", "Senders to read", "\n".join(ms.get("senders") or []), 4,
             "One per line: an address or just the domain, e.g. <code>jackandjill.ai</code> or "
             "<code>jobalerts-noreply@linkedin.com</code>.")
        + ui_field("mail_days", "Look back (days)", ms.get("days", 7), "number", "Only e-mails from the last days.", ' min="1" max="60"')
        + '<div><label>Reading the e-mails</label><label class="ui-toggle" style="font-weight:400"><input type="checkbox" '
          'class="toggle toggle-sm toggle-primary" name="mail_use_claude"%s> Claude reads them</label><span class="help">More accurate '
          '(title, company, location) in any format; about 1 cent per e-mail, every e-mail once. Off: only the links that look like '
          'postings, free.</span></div>' % chk(ms.get("use_claude", True))
        + '<div class="wide"><span class="help">Mailbox: by default the e-mail account under Notifications (for Gmail the same app '
          'password works; IMAP is on by default). Fill these in only for another mailbox.</span></div>'
        + ui_field("mail_imap_host", "IMAP server", ms.get("imap_host", "imap.gmail.com"))
        + ui_field("mail_imap_port", "Port", ms.get("imap_port", 993), "number")
        + ui_field("mail_folder", "Folder", ms.get("folder", "INBOX"), help_="For Gmail labels: the label name.")
        + ui_field("mail_user", "User (empty = the e-mail account)", ms.get("username", ""), "email")
        + '<div><label for="mail_pass">Password %s</label><input class="input" id="mail_pass" type="password" name="mail_pass" '
          'placeholder="Empty = the e-mail account\'s" autocomplete="new-password"></div></div>' % set_badge(ms.get("password")),
        test("mail", "Save and test e-mail alerts"))
    own += mail

    mine, tpls, active = sorted(cfg.get("presets") or {}), template_presets(), cfg.get("active_preset") or ""
    sel = lambda v: " selected" if v == active else ""
    opts = (('<option value=""%s>None (custom settings)</option>' % ("" if active in mine or active[4:] in tpls else " selected"))
            + ('<optgroup label="My presets">%s</optgroup>' % "".join('<option value="%s"%s>%s</option>' % (
                e(n), sel(n), e(n)) for n in mine) if mine else "")
            + '<optgroup label="Templates (read-only)">%s</optgroup>' % "".join(
                '<option value="tpl:%s"%s>%s</option>' % (e(n), sel("tpl:" + n), e(n)) for n in tpls))
    pdata = dict({n: (cfg.get("presets") or {})[n] for n in mine}, **{"tpl:" + n: t for n, t in tpls.items()})
    pbtn = lambda act, label, cls="": ('<button class="btn btn-sm %s" formaction="/preset" name="preset_do" value="%s"%s>%s</button>'
                                       % (cls or "btn-outline", act, " formnovalidate" if act != "save" else "", label))
    presets = ui_sec("presets", "Presets", "A preset is what you are looking for: the search terms, locations, filters, the "
                     "scoring chips and the minimum score, saved under a name, e.g. to try game designer or other roles. "
                     "Picking one only fills in the settings below; nothing changes until you save. Saving the settings "
                     "also updates the active preset.",
        '<p class="ui-lead" id="preset_state" aria-live="polite">In use: <b>%s</b></p><div class="ui-grid"><div>'
        '<label for="pp_btn">Preset</label><div class="pp" id="pp"><select class="pp-native" id="preset_pick" name="preset_pick" '
        'tabindex="-1" aria-hidden="true">%s</select><button type="button" class="select pp-btn" id="pp_btn" aria-haspopup="listbox" '
        'aria-expanded="false" aria-controls="pp_list"><span id="pp_label"></span></button>'
        '<ul class="pp-list" id="pp_list" role="listbox" aria-label="Presets" tabindex="-1"></ul></div>'
        '<input type="hidden" name="active_preset_set" id="active_preset_set" value="%s">'
        '<div class="ui-inline">%s</div><span class="help">Picking one fills in Job search and Scoring below. Review it, '
        'then Save changes; leaving without saving asks first.</span></div>'
        '<div><label for="preset_name">Name for changed settings</label><input class="input" id="preset_name" '
        'name="preset_name" maxlength="60" placeholder="%s"><div class="ui-inline">%s</div>'
        '<span class="help">If you change the settings of a preset and save, they are kept as a new preset under this '
        'name (empty: “%s”); the picked preset stays as it was. Type an existing name to update that one.</span></div></div>'
        '<script type="application/json" id="preset_data">%s</script>' % (
            e(preset_label(active)) if active else "custom settings (no preset)", opts, e(active),
            "", e(NEW_PRESET),
            pbtn("rename", "Rename the preset in use") if active in mine else "", e(NEW_PRESET),
            json.dumps(pdata, ensure_ascii=False).replace("</", "<\\/"))) + PRESET_PICKER_JS % {"x": ic("close")}
    sc = cfg.get("scoring") or {}
    chips = lambda kind, label, help_: (
        '<div class="wide"><label for="sc_%s">%s</label><textarea class="textarea ui-mono chips-src" id="sc_%s" name="sc_%s" '
        'rows="4" data-kind="%s">%s</textarea><span class="help">%s</span></div>' % (
            kind, label, kind, kind, kind, e("\n".join(sc.get(kind) or [])), help_))
    scoring = ui_sec("scoring", "Scoring", "Tell jobwatch what interests you, most important first: the order is the score. "
                     "Drag a chip or use its arrows to move it; Enter adds one. <code>unity / unity3d</code> counts as one "
                     "(words that mean the same). Saving re-scores every posting.",
        '<div class="ui-grid">'
        + chips("interests", "What interests me (job title)", "By position: +6, +5, +4, +4, +3, +3, +2, +2, +2, then +1 each.")
        + chips("desc", "Bonus if the description mentions it", "+3, +2, then +1 each; at most +%d together. Not counted "
                "again when the title already has it." % DESC_MAX)
        + chips("less", "Less relevant (job title)", "&minus;3 each: still listed when the rest fits well.")
        + chips("never", "Not for me (job title)", "&minus;8 each: hides the posting.")
        + chips("loc_good", "Preferred locations", "+3, +2, then +1 each; only for postings whose title fits (4+ points).")
        + chips("loc_bad", "Locations to avoid", "&minus;5 each.")
        + '<div class="wide"><label for="score_try">Try a job title</label><input class="input" id="score_try" type="text" '
          'placeholder="e.g. Senior Unity Developer" autocomplete="off"><span class="help" id="score_try_out" aria-live="polite">'
          'The title part of the score, with the chips above (also before saving).</span></div>'
        + ui_field("min_score", "Minimum score in the list", cfg["min_score"], "number", "Postings below this are hidden by default.")
        + '<div><label for="default_work_filter">Default list filter</label><select class="select" id="default_work_filter" '
          'name="default_work_filter">%s</select></div></div>' % filt)

    claude = ui_sec("claude", "Claude API", "Used for tailored CVs, cover letters and recruiter messages. "
                    "A CV + cover letter costs about 5–15 cents, a recruiter message 1–3 cents. The key stays on this machine.",
        '<div class="ui-grid"><div class="wide"><label for="anthropic_api_key">Anthropic API key %s</label>'
        '<input class="input" id="anthropic_api_key" type="password" name="anthropic_api_key" placeholder="Leave empty to keep" '
        'autocomplete="new-password"><span class="help">Create one at <a href="https://console.anthropic.com/settings/keys" '
        'target="_blank" rel="noopener">console.anthropic.com → API Keys</a>.</span></div></div>' % set_badge(cfg.get("anthropic_api_key")))

    general = ui_sec("general", "General", "",
        '<div class="ui-grid">' + ui_field("public_url", "Web address of this app", cfg.get("public_url", ""), "url",
                                           "Used for the link in notifications.", wide=True) + '</div>')

    danger = ui_sec("danger", "Maintenance", "",
        '<div class="ui-danger"><div><b>Reset the job cache</b>'
        '<span class="help">Deletes the found postings; settings, profile, CVs and documents are kept, and the postings you made a CV for stay in the list (marked applied). '
        'The next refresh starts from scratch.</span></div>'
        '<button type="button" class="btn btn-sm btn-error btn-outline" onclick="document.getElementById(\'clear_dlg\').showModal()">'
        'Reset job cache…</button></div>')

    nav = "".join('<a href="#%s">%s</a>' % (a, l) for a, l in SETTINGS_NAV)
    body = ('<div class="ui"><div class="ui-head"><h1>Settings</h1><p>Notifications, job sources, scoring and API keys. '
            'Changes take effect after you save.</p></div>'
            '<div class="ui-layout"><nav class="ui-side" aria-label="Settings sections">%s</nav><div>'
            '<form method="post" action="/settings" id="settings">%s%s%s%s%s%s%s'
            '<input type="hidden" name="next" id="settings_next" value="">'
            '<div class="ui-savebar"><span class="muted" id="savebar_note" aria-live="polite">Saving re-scores every posting.</span>'
            '<button class="btn btn-primary" id="settings_save">Save changes</button></div></form>%s</div></div></div>'
            '<dialog id="leave_dlg" class="modal" aria-labelledby="leave_h"><div class="modal-box"><h3 id="leave_h" '
            'class="text-lg font-bold">Save your changes?</h3><p id="leave_what" class="py-2"></p><p id="leave_search" '
            'class="text-sm opacity-80" hidden>The search changed: saving clears the postings found so far and searches again '
            'with the new settings (%s).</p><div class="modal-action"><button type="button" class="btn btn-ghost" '
            'id="leave_stay">Stay here</button><button type="button" class="btn btn-outline" id="leave_discard">Don\'t save'
            '</button><button type="button" class="btn btn-primary" id="leave_save">Save</button></div></div>'
            '<form method="dialog" class="modal-backdrop"><button aria-label="Close" tabindex="-1"></button></form></dialog>') % (
        nav, presets, notify, search, own, scoring, claude, general, danger, e(KEPT_ON_NEW_SEARCH))
    return page("Settings", body + CHIPS_JS % json.dumps(
        {k: {"t": list(t), "rest": r} for k, (t, r) in SCORING_KINDS.items()}) + LEAVE_JS, flash)


CHIPS_JS = """<script>(function () {
var P = %s;
function pts(k, i) { var p = P[k]; return i < p.t.length ? p.t[i] : p.rest; }
function fmt(n) { return (n >= 0 ? "+" : "\u2212") + Math.abs(n); }
function alts(c) { return c.toLowerCase().split(/\\s+\\/\\s+/).map(function (s) { return s.trim(); }).filter(Boolean); }
function esc(s) { return s.replace(/[.*+?^${}()|[\\]\\\\]/g, "\\\\$&"); }
function hit(as, txt) {
  for (var i = 0; i < as.length; i++)
    if (new RegExp("(?<![a-z0-9])" + esc(as[i]) + "(?![a-z0-9#+])").test(txt)) return as[i];
  return null;
}
document.querySelectorAll("textarea.chips-src").forEach(function (ta) {
  var k = ta.dataset.kind, ordered = P[k].t.length > 0, drag = null;
  var items = ta.value.split("\\n").map(function (s) { return s.trim(); }).filter(Boolean);
  var box = document.createElement("div"), inp = document.createElement("input");
  box.className = "chips"; box.setAttribute("role", "list");
  inp.type = "text"; inp.id = ta.id + "_in"; inp.placeholder = "Add… (Enter)"; inp.autocomplete = "off";
  var lab = document.querySelector('label[for="' + ta.id + '"]');
  if (lab) { lab.htmlFor = inp.id; inp.setAttribute("aria-label", "Add to: " + lab.textContent); }
  ta.hidden = true; ta.after(box);
  function sync(focus) {
    ta.value = items.join("\\n"); render();
    if (focus) { var el = box.querySelector(focus); if (el) el.focus(); }
    document.dispatchEvent(new Event("chips"));
  }
  function button(label, text, fn) {
    var b = document.createElement("button"); b.type = "button"; b.textContent = text;
    b.setAttribute("aria-label", label); b.title = label; b.onclick = fn; return b;
  }
  function render() {
    box.textContent = "";
    items.forEach(function (t, i) {
      var c = document.createElement("span"), p = pts(k, i), b = document.createElement("b");
      c.className = "chip" + (p < 0 ? " neg" : ""); c.setAttribute("role", "listitem"); c.draggable = true;
      b.className = "pts"; b.textContent = fmt(p); c.append(b, document.createTextNode(t));
      if (ordered) {
        if (i > 0) c.append(button("Move " + t + " up", "\u2039", function () {
          items.splice(i - 1, 0, items.splice(i, 1)[0]); sync(".chip:nth-child(" + i + ") button"); }));
        if (i < items.length - 1) c.append(button("Move " + t + " down", "\u203a", function () {
          items.splice(i + 1, 0, items.splice(i, 1)[0]); sync(".chip:nth-child(" + (i + 2) + ") button:last-of-type"); }));
      }
      c.append(button("Remove " + t, "\u00d7", function () { items.splice(i, 1); sync("input"); }));
      c.addEventListener("dragstart", function (ev) { drag = i; c.classList.add("dragging"); ev.dataTransfer.effectAllowed = "move"; ev.dataTransfer.setData("text/plain", t); });
      c.addEventListener("dragend", function () { drag = null; c.classList.remove("dragging"); });
      c.addEventListener("dragover", function (ev) { if (drag !== null) { ev.preventDefault(); c.classList.add("over"); } });
      c.addEventListener("dragleave", function () { c.classList.remove("over"); });
      c.addEventListener("drop", function (ev) {
        ev.preventDefault(); c.classList.remove("over");
        if (drag === null || drag === i) return;
        items.splice(i, 0, items.splice(drag, 1)[0]); drag = null; sync();
      });
      box.append(c);
    });
    box.append(inp);
  }
  function add(focus) {
    var v = inp.value.replace(/,+$/, "").trim();
    if (!v) return;
    if (!items.some(function (x) { return x.toLowerCase() === v.toLowerCase(); })) items.push(v);
    inp.value = ""; sync(focus ? "input" : null);
  }
  inp.addEventListener("keydown", function (ev) {
    if (ev.key === "Enter" || ev.key === ",") { ev.preventDefault(); add(true); }
    else if (ev.key === "Backspace" && !inp.value && items.length) { items.pop(); sync("input"); }
  });
  inp.addEventListener("change", function () { add(false); });
  ta.setChips = function (list) { items = (list || []).slice(); sync(); };
  render();
});
var tr = document.getElementById("score_try"), out = document.getElementById("score_try_out");
if (!tr) return;
var hint = out.textContent;
function lines(k) { var t = document.getElementById("sc_" + k); return t ? t.value.split("\\n").map(function (s) { return s.trim(); }).filter(Boolean) : []; }
function num(id, d) { var el = document.getElementById(id); return el && el.value !== "" ? +el.value : d; }
function calc() {
  var t = tr.value.toLowerCase().trim();
  if (!t) { out.textContent = hint; return; }
  var s = 0, parts = [];
  ["interests", "less", "never"].forEach(function (k) {
    lines(k).forEach(function (c, i) { var a = hit(alts(c), t); if (a) { var p = pts(k, i); s += p; parts.push(a + " " + fmt(p)); } });
  });
  var min = num("min_score", 4), note = num("notify_min_score", 7);
  out.textContent = (parts.length ? parts.join(", ") : "nothing matches") + "  =  " + s + " points (title only) \u00b7 " +
    (s >= note ? "listed and notified" : s >= min ? "listed" : "hidden; the description can add up to +" + P.desc.t[0] + "\u2026+4");
}
tr.addEventListener("input", calc); document.addEventListener("chips", calc);
document.querySelectorAll("#min_score,#notify_min_score").forEach(function (el) { el.addEventListener("input", calc); });
})();</script>"""


# Settings: notices unsaved changes; leaving the page or switching a preset asks first.
LEAVE_JS = """<script>(function () {
var form = document.getElementById("settings"), dlg = document.getElementById("leave_dlg");
if (!form || !dlg) return;
var SEARCH = ["search", "own"], SKIP = /^(preset_pick|preset_name|preset_do|what|next|save_first)$/;
function snap() {
  var out = {};
  form.querySelectorAll("section.ui-section").forEach(function (sec) {
    var v = [];
    sec.querySelectorAll("input,select,textarea").forEach(function (el) {
      if (!el.name || SKIP.test(el.name) || el.type === "button" || el.type === "submit") return;
      v.push(el.name + "=" + (el.type === "checkbox" || el.type === "radio" ? el.checked : el.value));
    });
    out[sec.id] = v.join("\\u0001");
  });
  return out;
}
var base = snap(), leaving = false, pending = null;
function dirty() {
  var now = snap(), out = [];
  Object.keys(now).forEach(function (k) { if (now[k] !== base[k]) out.push(k); });
  return out;
}
function title(id) { var h = document.getElementById(id + "-h"); return h ? h.textContent : id; }
var note = document.getElementById("savebar_note"), note0 = note.textContent;
var PF = /^(search_terms|search_locations|min_score|sf_.*|sc_.*)$/;
function psnap() {
  var v = [];
  form.querySelectorAll("input,textarea").forEach(function (el) {
    if (el.name && PF.test(el.name)) v.push(el.name + "=" + (el.type === "checkbox" ? el.checked : el.value)); });
  return v.join("\\u0001");
}
var pbase = psnap();
function newPreset() {
  if (psnap() === pbase) return "";
  var n = (document.getElementById("preset_name").value || "").trim() || document.getElementById("preset_name").placeholder;
  return " Saving keeps them as the preset \\u201c" + n + "\\u201d.";
}
function update() {
  var d = dirty();
  if (!d.length) { note.textContent = note0; return; }
  note.textContent = "Unsaved changes: " + d.map(title).join(", ") +
    (d.some(function (x) { return SEARCH.indexOf(x) >= 0; }) ? ". The search changed: saving clears the found postings and searches again." : ".") +
    newPreset();
}
form.addEventListener("input", update); form.addEventListener("change", update); document.addEventListener("chips", update);
form.addEventListener("submit", function () { leaving = true; });
window.addEventListener("beforeunload", function (ev) { if (!leaving && dirty().length) { ev.preventDefault(); ev.returnValue = ""; } });
function ask(onSave, onDiscard, saveLabel, discardLabel) {
  var d = dirty();
  document.getElementById("leave_what").textContent = "You changed: " + d.map(title).join(", ") + "." + newPreset();
  document.getElementById("leave_search").hidden = !d.some(function (x) { return SEARCH.indexOf(x) >= 0; });
  document.getElementById("leave_save").textContent = saveLabel;
  document.getElementById("leave_discard").textContent = discardLabel;
  pending = {save: onSave, discard: onDiscard};
  dlg.showModal();
}
document.getElementById("leave_stay").onclick = function () { pending = null; dlg.close(); };
document.getElementById("leave_save").onclick = function () { var p = pending; dlg.close(); if (p) { leaving = true; p.save(); } };
document.getElementById("leave_discard").onclick = function () { var p = pending; dlg.close(); if (p) { leaving = true; p.discard(); } };
// links and other forms that leave the page
document.addEventListener("click", function (ev) {
  var a = ev.target.closest("a[href]");
  if (!a || ev.defaultPrevented || ev.ctrlKey || ev.metaKey || ev.shiftKey || a.target === "_blank") return;
  var href = a.getAttribute("href");
  if (!href || href.charAt(0) === "#" || !dirty().length) return;
  ev.preventDefault();
  ask(function () { document.getElementById("settings_next").value = a.pathname + a.search;
                    form.requestSubmit(document.getElementById("settings_save")); },
      function () { location.href = a.href; }, "Save and continue", "Don't save");
}, true);
document.querySelectorAll("form").forEach(function (f) {
  if (f === form || f.method === "dialog") return;
  f.addEventListener("submit", function (ev) {
    if (leaving || !dirty().length) return;
    ev.preventDefault();
    var sub = ev.submitter;
    ask(function () { document.getElementById("settings_next").value = "/settings";
                      form.requestSubmit(document.getElementById("settings_save")); },
        function () { leaving = true; f.requestSubmit(sub || undefined); }, "Save (stay here)", "Don't save, continue");
  });
});
// picking a preset or template fills in the form; it is applied only when you save
var pick = document.getElementById("preset_pick"), data = JSON.parse(document.getElementById("preset_data").textContent || "{}");
pick.addEventListener("change", function () {
  var p = data[pick.value];
  if (!p) return;
  function set(id, v) { var el = document.getElementById(id); if (el && v !== undefined) el.value = v; }
  if (p.search_terms) set("search_terms", p.search_terms.join("\\n"));
  if (p.search_locations) set("search_locations", p.search_locations.join("\\n"));
  if (p.min_score !== undefined) set("min_score", p.min_score);
  if (p.search_filter) form.querySelectorAll("input[name^=sf_]").forEach(function (c) { c.checked = !!p.search_filter[c.name.slice(3)]; });
  if (p.scoring) Object.keys(p.scoring).forEach(function (k) {
    var ta = document.getElementById("sc_" + k); if (ta && ta.setChips) ta.setChips(p.scoring[k]); });
  var tpl = pick.value.indexOf("tpl:") === 0;
  document.getElementById("active_preset_set").value = pick.value;
  pbase = psnap();   // this is the picked preset; changes from here on make a new one
  document.getElementById("preset_state").innerHTML = "";
  document.getElementById("preset_state").append("Loaded \\u201c" + pick.options[pick.selectedIndex].text + "\\u201d" +
    (tpl ? " (template)" : "") + " into the settings below. Nothing is applied until you click Save changes.");
  update();
});
})();</script>"""


# Settings → Presets: the preset list as a dropdown whose own presets have an ✕ to delete them (a second click confirms).
# The hidden <select> stays the form field, so loading, saving and the unsaved-changes check work as before. Deleting is
# a background request: the page is not reloaded, so unsaved settings are not lost.
PRESET_PICKER_JS = """<script>(function () {
var sel = document.getElementById("preset_pick"), btn = document.getElementById("pp_btn"), list = document.getElementById("pp_list");
var label = document.getElementById("pp_label"), state = document.getElementById("preset_state");
var X = '%(x)s', data = JSON.parse(document.getElementById("preset_data").textContent || "{}");
function showLabel() { var o = sel.options[sel.selectedIndex]; label.textContent = o ? o.text : ""; }
function build() {
  list.innerHTML = "";
  Array.prototype.forEach.call(sel.children, function (node) {
    var opts = node.tagName === "OPTGROUP" ? node.children : [node];
    if (node.tagName === "OPTGROUP") {
      if (!opts.length) return;
      var g = document.createElement("li"); g.className = "pp-group"; g.setAttribute("role", "presentation");
      g.textContent = node.label; list.appendChild(g);
    }
    Array.prototype.forEach.call(opts, function (o) {
      var li = document.createElement("li"), mine = node.tagName === "OPTGROUP" && o.value && o.value.indexOf("tpl:") !== 0;
      li.setAttribute("role", "option"); li.dataset.value = o.value; li.id = "pp_o" + list.children.length;
      li.setAttribute("aria-selected", o.selected ? "true" : "false");
      var t = document.createElement("span"); t.className = "pp-t"; t.textContent = o.text; li.appendChild(t);
      if (mine) {
        var x = document.createElement("button"); x.type = "button"; x.className = "pp-x"; x.innerHTML = X;
        x.setAttribute("aria-label", "Delete preset " + o.text); x.title = "Delete this preset";
        li.appendChild(x);
      }
      list.appendChild(li);
    });
  });
}
function items() { return Array.prototype.slice.call(list.querySelectorAll("[role=option]")); }
function setActive(li) {
  items().forEach(function (x) { x.classList.toggle("act", x === li); });
  if (li) { list.setAttribute("aria-activedescendant", li.id); li.scrollIntoView({block: "nearest"}); }
}
function open() {
  build(); list.classList.add("open"); btn.setAttribute("aria-expanded", "true");
  setActive(list.querySelector("[aria-selected=true]") || items()[0]); list.focus();
}
function close(focus) {
  list.classList.remove("open"); btn.setAttribute("aria-expanded", "false");
  if (focus) btn.focus();
}
function choose(li) {
  if (!li) return;
  sel.value = li.dataset.value; showLabel(); close(true);
  sel.dispatchEvent(new Event("change", {bubbles: true}));
}
function remove(li, x) {
  if (!x.classList.contains("sure")) {   // first click: ask
    x.classList.add("sure"); x.textContent = "Delete?"; x.setAttribute("aria-label", "Click again to delete");
    setTimeout(function () { if (x.isConnected && x.classList.contains("sure")) { x.classList.remove("sure"); x.innerHTML = X; } }, 3500);
    return;
  }
  var name = li.dataset.value;
  li.classList.add("busy");
  fetch("/preset", {method: "POST", body: new URLSearchParams({preset_do: "delete", preset_pick: name})}).then(function (r) {
    if (!r.ok) throw r;
    var o = Array.prototype.find.call(sel.options, function (op) { return op.value === name; }), was = o && o.selected;
    if (o) { var g = o.parentNode; o.remove(); if (g.tagName === "OPTGROUP" && !g.children.length) g.remove(); }
    delete data[name];
    var act = document.getElementById("active_preset_set");
    if (act && act.value === name) act.value = "";
    if (was) { sel.value = ""; showLabel(); }
    li.remove(); build(); setActive(items()[0]);
    state.textContent = "Preset \u201c" + name + "\u201d deleted. The settings below did not change.";
  }).catch(function () { li.classList.remove("busy"); state.textContent = "Could not delete \u201c" + name + "\u201d; try again."; });
}
btn.addEventListener("click", function () { list.classList.contains("open") ? close(true) : open(); });
btn.addEventListener("keydown", function (ev) { if (ev.key === "ArrowDown" || ev.key === "ArrowUp") { ev.preventDefault(); open(); } });
list.addEventListener("click", function (ev) {
  var li = ev.target.closest("[role=option]"); if (!li) return;
  var x = ev.target.closest(".pp-x");
  if (x) { ev.stopPropagation(); remove(li, x); } else choose(li);
});
list.addEventListener("keydown", function (ev) {
  var all = items(), cur = list.querySelector(".act"), i = all.indexOf(cur);
  if (ev.key === "ArrowDown") { ev.preventDefault(); setActive(all[Math.min(i + 1, all.length - 1)]); }
  else if (ev.key === "ArrowUp") { ev.preventDefault(); setActive(all[Math.max(i - 1, 0)]); }
  else if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); choose(cur); }
  else if (ev.key === "Delete" && cur && cur.querySelector(".pp-x")) { ev.preventDefault(); remove(cur, cur.querySelector(".pp-x")); }
  else if (ev.key === "Escape" || ev.key === "Tab") { if (ev.key === "Escape") ev.preventDefault(); close(ev.key === "Escape"); }
});
document.addEventListener("click", function (ev) { if (!ev.target.closest("#pp")) close(false); });
sel.addEventListener("change", showLabel);
showLabel();
})();</script>"""
