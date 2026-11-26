// Recruiters page: counting characters, copying a message, opening a sent one.
function ocount(id, lim) { var t = document.getElementById('om' + id), c = document.getElementById('on' + id);
  if (!c) return; c.textContent = t.value.length + ' / ' + lim + ' characters';
  c.className = 'rc-count' + (t.value.length > lim ? ' over' : ''); }
function ocopy(id, subj) {  // subj: copy the subject line instead of the message
  var t = document.getElementById((subj ? 'os' : 'om') + id), b = document.getElementById((subj ? 'ocs' : 'oc') + id), ok = false;
  var label = b.textContent; t.focus(); t.select(); try { ok = document.execCommand('copy'); } catch (err) {}
  if (navigator.clipboard && window.isSecureContext) { navigator.clipboard.writeText(t.value); ok = true; }
  b.textContent = ok ? '✓ Copied' : 'Press Ctrl+C'; setTimeout(function () { b.textContent = label; }, 1800); }
function oview(id) { document.getElementById('dlg' + id).showModal(); }
