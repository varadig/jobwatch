"""The Settings page."""
import json

from jobwatch.config import config_store
from jobwatch.domain.scoring import DESC_MAX, SCORING_KINDS
from jobwatch.models import FETCHED_SOURCES, SOURCES_INFO, WORK_MODES
from jobwatch.services.presets import NEW_PRESET, preset_label, template_presets
from jobwatch.services.refresh import KEPT_ON_NEW_SEARCH
from jobwatch.sources.registry import OPTIONAL_BOARDS, SEARCH_FILTERABLE
from jobwatch.util.text import e
from jobwatch.web.components import page, script, ui_field, ui_sec, ui_toggle, wm_html
from jobwatch.web.views.sources import custom_sources_card

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
            json.dumps(pdata, ensure_ascii=False).replace("</", "<\\/"))) + PRESET_PICKER_JS
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
    return page("Settings", body + '<script type="application/json" id="jw_scoring">%s</script>' % json.dumps(
        {k: {"t": list(t), "rest": r} for k, (t, r) in SCORING_KINDS.items()}) + CHIPS_JS + LEAVE_JS, flash)


CHIPS_JS = script("scoring-chips")


# Settings: notices unsaved changes; leaving the page or switching a preset asks first.
LEAVE_JS = script("settings-leave")


# Settings → Presets: the preset list as a dropdown whose own presets have an ✕ to delete them (a second click confirms).
# The hidden <select> stays the form field, so loading, saving and the unsaved-changes check work as before. Deleting is
# a background request: the page is not reloaded, so unsaved settings are not lost.
PRESET_PICKER_JS = script("preset-picker")
