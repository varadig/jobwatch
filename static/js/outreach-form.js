// Recruiters page: the list of people to write to, filled in from a LinkedIn profile link.
var RL = [];
function rval(id) { return document.getElementById(id).value.trim(); }
function rcell(tr, text, cls) { var td = document.createElement('td'); td.textContent = text; if (cls) td.className = cls; tr.appendChild(td); }
function rdraw() {
  var body = document.getElementById('rlist'), empty = document.getElementById('rempty'), table = document.getElementById('rtable');
  body.innerHTML = '';
  RL.forEach(function (r, i) {
    var tr = document.createElement('tr');
    rcell(tr, r.name + (r.about ? '  ℹ️' : ''));
    rcell(tr, r.title || '—', 'hide-sm');
    rcell(tr, r.role ? (r.role.length > 48 ? r.role.slice(0, 48) + '…' : r.role) : '—', 'hide-sm');
    rcell(tr, r.kind === 'agency' ? 'Agency' : r.kind === 'inhouse' ? 'In-house' : 'Auto');
    var td = document.createElement('td'), b = document.createElement('button');
    b.type = 'button'; b.className = 'btn btn-ghost btn-xs'; b.textContent = 'Remove'; b.setAttribute('aria-label', 'Remove ' + r.name);
    b.onclick = function () { RL.splice(i, 1); rdraw(); }; td.style.textAlign = 'right'; td.appendChild(b); tr.appendChild(td);
    body.appendChild(tr);
  });
  table.style.display = RL.length ? '' : 'none'; empty.style.display = RL.length ? 'none' : '';
  document.getElementById('rjson').value = JSON.stringify(RL);
  var go = document.getElementById('rgo'); go.disabled = !RL.length;
  go.textContent = RL.length ? 'Write ' + RL.length + ' message' + (RL.length === 1 ? '' : 's') : 'Write messages';
}
var RLOOK = null;
function rlookup() {  // LinkedIn link -> name + title from the profile's public link preview
  var url = rval('r_contact'), hint = document.getElementById('rhint');
  if (!/linkedin\.com\/in\//i.test(url)) return Promise.resolve();
  hint.textContent = 'Reading the profile…';
  RLOOK = fetch('/outreach/lookup?url=' + encodeURIComponent(url)).then(function (r) { return r.json(); }).then(function (d) {
    if (!d.ok) { hint.textContent = d.error || ''; return; }
    if (rval('r_contact') === url) document.getElementById('r_contact').value = d.url;
    if (!rval('r_name')) document.getElementById('r_name').value = d.name || '';
    if (!rval('r_title')) document.getElementById('r_title').value = d.title || '';
    document.getElementById('r_about').value = d.about || '';
    hint.textContent = d.from === 'profile' ? '✓ Filled in from the profile. Check it, then add the person.'
      : 'The profile is not public: the name was guessed from the link. Check it and add the title by hand.';
  }).catch(function () { hint.textContent = 'Could not read the link. Fill in the name by hand.'; })
    .then(function () { RLOOK = null; });
  return RLOOK;
}
function radd() {
  if (!rval('r_name') && /linkedin\.com\/in\//i.test(rval('r_contact'))) {  // only the link was given
    (RLOOK || rlookup()).then(function () { if (rval('r_name')) radd(); });
    return false;
  }
  if (!rval('r_name')) { document.getElementById('rhint').textContent = 'Give a LinkedIn link or a name first.';
    document.getElementById('r_contact').focus(); return false; }
  RL.push({name: rval('r_name'), title: rval('r_title'), contact: rval('r_contact'), kind: document.getElementById('r_kind').value,
           role: rval('r_role'), about: rval('r_about')});
  ['r_name', 'r_title', 'r_contact', 'r_kind', 'r_role', 'r_about'].forEach(function (id) { document.getElementById(id).value = ''; });
  document.getElementById('rhint').textContent = '';
  rdraw(); document.getElementById('r_contact').focus(); return true;
}
function renter(ev) { if (ev.key === 'Enter') { ev.preventDefault(); radd(); } }
function rsubmit() {  // a filled-in but not yet added recruiter counts too
  if (rval('r_name')) radd();
  if (!RL.length) { document.getElementById('rhint').textContent = 'Add at least one recruiter.'; return false; }
  return true;
}
function rchannel() {  // the introducer field matters only for introduction requests
  var intro = document.getElementById('channel').value === 'linkedin_intro';
  document.getElementById('viawrap').style.display = intro ? '' : 'none';
  if (intro) document.getElementById('rmore').open = true;
}
rdraw(); rchannel();
