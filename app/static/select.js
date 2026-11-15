/* v0.97.0: selection, selection bar, confirmation sheet and swipe on the 8 list pages. Loaded with `defer`; no inline script, no eval, no network call.
   Everything it needs comes from the markup: the actions of a row (`data-actions`), the button texts (`data-label-<action>`), the swipe actions
   (`data-swipe-lead`, `data-swipe-trail`), the actions that ask with Cancel focused (`data-careful`) and the token of each action in the sheet
   (`data-csrf-<action>`). It holds no status, action or page name. Nothing changes before the sheet's Confirm; without this script the buttons
   open the confirmation page of /bulk-status. Text is set with textContent only. */
(function () {
  "use strict";
  var bulk = document.getElementById("bulk");
  var sheet = document.getElementById("sheet");
  var sform = document.getElementById("sheetform");
  if (!bulk || !sheet || !sform || typeof HTMLDialogElement === "undefined" || typeof sheet.showModal !== "function") { return; }
  var main = document.querySelector("main");
  var bar = document.querySelector(".selbar");
  var hint = bar ? bar.querySelector(".selhint") : null;
  var hintText = hint ? hint.textContent : "";
  var barButtons = bar ? [].slice.call(bar.querySelectorAll("button[name=action]")) : [];
  var selectAll = document.querySelector("input.selectall");
  var toggle = document.querySelector(".selecttoggle");
  var titleEl = document.getElementById("sheet-title");
  var listEl = sform.querySelector(".sheet-list");
  var noteEl = sform.querySelector(".sheet-note");
  var warnEl = sform.querySelector("[data-warn-for]");
  var commentBox = sform.querySelector(".sheet-comment");
  var comment = sform.elements.comment;
  var roundBox = sform.querySelector("[data-round-for]");       /* the optional first round: shown and enabled for the one action named in the markup and one job only */
  var confirmBtn = sform.querySelector(".sheet-confirm");
  var cancelBtn = sform.querySelector(".sheet-cancel");
  var words = function (text) { return (text || "").split(" ").filter(Boolean); };
  var careful = words(bulk.getAttribute("data-careful"));
  var leadNames = words(bulk.getAttribute("data-swipe-lead"));
  var trailNames = words(bulk.getAttribute("data-swipe-trail"));
  var labelOf = function (a) { return bulk.getAttribute("data-label-" + a) || a; };
  var SHOWN = 5;          /* titles named in the sheet */
  var EDGE = 24;          /* a touch this close to the left edge is the browser's back gesture */
  var LOCK = 8;           /* pixels before the direction is decided */

  /* the rows: one per checkbox, with what the server rendered about it */
  var rows = [].slice.call(document.querySelectorAll("input.pick")).map(function (box) {
    var holder = box.closest("[data-actions]");
    return { box: box, warn: !!(holder && holder.hasAttribute("data-warn")), el: box.closest("tr, article") || holder, actions: words(holder && holder.getAttribute("data-actions")),
             title: (holder && holder.getAttribute("data-title")) || "", company: (holder && holder.getAttribute("data-company")) || "" };
  });
  if (!rows.length) { return; }
  var allows = function (row, a) { return row.actions.indexOf(a) >= 0; };
  var chosen = function () { return rows.filter(function (r) { return r.box.checked; }); };

  /* --- selection and bar ------------------------------------------------------------------------------ */
  function refresh() {
    var sel = chosen();
    if (hint) { hint.textContent = sel.length ? sel.length + " selected" : hintText; }
    if (bar) { bar.classList.toggle("idle", sel.length === 0); }
    barButtons.forEach(function (b) { b.disabled = !sel.some(function (r) { return allows(r, b.value); }); });
    if (selectAll) {
      selectAll.checked = sel.length === rows.length;
      selectAll.indeterminate = sel.length > 0 && sel.length < rows.length;
    }
  }
  function clear() { rows.forEach(function (r) { r.box.checked = false; }); refresh(); }
  var last = null;
  rows.forEach(function (r) {
    r.box.addEventListener("click", function (e) {
      if (e.shiftKey && last && last !== r.box) {
        var a = rows.map(function (x) { return x.box; }).indexOf(last), b = rows.map(function (x) { return x.box; }).indexOf(r.box);
        rows.slice(Math.min(a, b), Math.max(a, b) + 1).forEach(function (x) { x.box.checked = r.box.checked; });
      }
      last = r.box;
      refresh();
    });
  });
  if (selectAll) {
    selectAll.hidden = false;
    selectAll.addEventListener("change", function () { rows.forEach(function (r) { r.box.checked = selectAll.checked; }); refresh(); });
  }
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && !sheet.open && chosen().length) { clear(); }
  });

  /* --- the sheet ---------------------------------------------------------------------------------------- */
  var sending = false;
  function item(text) { var li = document.createElement("li"); li.textContent = text; return li; }
  function setRound(on) {
    if (!roundBox) { return; }
    roundBox.hidden = !on;
    [].slice.call(roundBox.querySelectorAll("input, select")).forEach(function (f) { f.disabled = !on; if (f.tagName === "SELECT") { f.selectedIndex = 0; } else { f.value = ""; } });
  }
  function fill(action, picked, skipped) {
    var n = picked.length;
    titleEl.textContent = labelOf(action) + " " + n + " job" + (n === 1 ? "" : "s") + "?";
    listEl.textContent = "";
    picked.slice(0, SHOWN).forEach(function (r) { listEl.appendChild(item(r.company ? r.title + ", " + r.company : r.title)); });
    if (n > SHOWN) { listEl.appendChild(item("and " + (n - SHOWN) + " more")); }
    noteEl.textContent = skipped ? skipped + " selected job" + (skipped === 1 ? "" : "s") + " cannot take this action and will be skipped." : "";
    sform.elements.csrf.value = sform.getAttribute("data-csrf-" + action);
    sform.elements.action.value = action;
    sform.elements.ids.value = picked.map(function (r) { return r.box.value; }).join(",");
    warnEl.hidden = !(warnEl.getAttribute("data-warn-for") === action && picked.some(function (r) { return r.warn; }));
    var own = sform.getAttribute("data-comment-for") === action;
    commentBox.hidden = !own;
    comment.disabled = !own;
    comment.value = "";
    var src = barButtons.filter(function (b) { return b.value === action; })[0];
    setRound(roundBox && roundBox.getAttribute("data-round-for") === action && n === 1);
    confirmBtn.className = "sheet-confirm" + (src && src.classList.contains("reject") ? " reject" : "");
    confirmBtn.textContent = labelOf(action);
    confirmBtn.hidden = false;
    confirmBtn.disabled = false;
    sending = false;
  }
  function open(action, picked, skipped) {
    if (!picked.length || !sform.getAttribute("data-csrf-" + action)) { return false; }
    fill(action, picked, skipped || 0);
    if (!sheet.open) { sheet.showModal(); }
    (careful.indexOf(action) >= 0 ? cancelBtn : confirmBtn).focus();
    return true;
  }
  function openMore(row) {          /* the actions of one job that its swipe does not show: a choice, then the same sheet */
    titleEl.textContent = row.title;
    warnEl.hidden = true;
    listEl.textContent = "";
    noteEl.textContent = "";
    commentBox.hidden = true;
    comment.disabled = true;
    setRound(false);
    confirmBtn.hidden = true;
    row.actions.forEach(function (a) {
      var li = document.createElement("li"), b = document.createElement("button");
      b.type = "button";
      b.textContent = labelOf(a);
      b.className = (barButtons.filter(function (x) { return x.value === a; })[0] || {}).className || "";
      b.addEventListener("click", function () { open(a, [row], 0); });
      li.className = "choice";
      li.appendChild(b);
      listEl.appendChild(li);
    });
    if (!sheet.open) { sheet.showModal(); }
    cancelBtn.focus();
  }
  bulk.addEventListener("submit", function (e) {
    var action = e.submitter && e.submitter.value;
    var picked = chosen().filter(function (r) { return allows(r, action); });
    if (open(action, picked, chosen().length - picked.length)) { e.preventDefault(); }       /* else the confirmation page of the form opens */
  });
  cancelBtn.addEventListener("click", function () { sheet.close(); });
  sheet.addEventListener("click", function (e) {      /* a click on the backdrop (outside the box) cancels */
    var r = sheet.getBoundingClientRect();
    if (e.target === sheet && (e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom)) { sheet.close(); }
  });
  sform.addEventListener("submit", function (e) {
    if (sending) { e.preventDefault(); return; }
    sending = true;
    setTimeout(function () { confirmBtn.disabled = true; }, 0);       /* after the form data is built, so confirm=yes is sent */
  });
  window.addEventListener("pageshow", function () { sending = false; confirmBtn.disabled = false; });
  refresh();

  /* --- phone: select mode and swipe (touch screens only) ------------------------------------------------- */
  if (!(window.matchMedia && window.matchMedia("(pointer: coarse)").matches)) { return; }
  main.classList.add("swipe-on");
  if (toggle) {
    toggle.hidden = false;
    toggle.addEventListener("click", function () {
      var on = !main.classList.contains("selecting");
      main.classList.toggle("selecting", on);
      toggle.setAttribute("aria-pressed", on ? "true" : "false");
      toggle.textContent = on ? "Done" : "Select";
      closeOpen();
      if (!on) { clear(); }
    });
  }
  var opened = null;
  function closeOpen() { if (opened) { rest(opened, true); opened = null; } }
  function rest(s, animate) {
    s.el.classList.toggle("settle", !!animate);
    s.el.style.transform = "translateX(0px)";
    s.pos = 0;
    s.el.classList.remove("swiping");
    [s.lead, s.trail].forEach(function (l) { if (l) { l.style.width = ""; } });
  }
  function layer(cls, names, row) {
    var box = document.createElement("div");
    box.className = cls;
    box.setAttribute("aria-hidden", "true");
    names.forEach(function (a) {
      var b = document.createElement("button");
      b.type = "button";
      b.tabIndex = -1;
      b.textContent = a === null ? "More" : labelOf(a);
      b.className = a === null ? "more" : (barButtons.filter(function (x) { return x.value === a; })[0] || {}).className || "";
      b.addEventListener("click", function () { if (a === null) { openMore(row); } else { open(a, [row], 0); } closeOpen(); });
      box.appendChild(b);
    });
    return box;
  }
  rows.forEach(function (row) {
    var lead = leadNames.filter(function (a) { return allows(row, a); })[0];
    var out = trailNames.filter(function (a) { return allows(row, a); })[0];
    var more = row.actions.filter(function (a) { return a !== lead && a !== out; });
    if (!lead && !out && !more.length) { return; }
    var trailItems = (more.length ? [null] : []).concat(out ? [out] : []);
    var s = { el: row.el, lead: lead ? layer("swipe-lead", [lead], row) : null, trail: trailItems.length ? layer("swipe-trail", trailItems, row) : null,
              pos: 0, base: [0, 0], leadAction: lead, outAction: out, stamp: 0 };
    row.el.classList.add("swipe-row");
    [s.lead, s.trail].forEach(function (l) { if (l) { row.el.appendChild(l); } });
    var start = null;
    row.el.addEventListener("pointerdown", function (e) {
      if (main.classList.contains("selecting") || e.pointerType === "mouse" || !e.isPrimary || e.clientX < EDGE) { return; }
      if (e.target.closest(".swipe-lead, .swipe-trail")) { return; }
      if (opened && opened !== s) { closeOpen(); }
      start = { x: e.clientX, y: e.clientY, from: s.pos, locked: false, id: e.pointerId };
    });
    row.el.addEventListener("pointermove", function (e) {
      if (!start || e.pointerId !== start.id) { return; }
      var dx = e.clientX - start.x, dy = e.clientY - start.y;
      if (!start.locked) {
        if (Math.max(Math.abs(dx), Math.abs(dy)) < LOCK) { return; }
        if (Math.abs(dy) > Math.abs(dx)) { start = null; return; }
        start.locked = true;
        row.el.classList.remove("settle");
        row.el.classList.add("swiping");
        s.base = [s.lead ? s.lead.offsetWidth : 0, s.trail ? s.trail.offsetWidth : 0];
        try { row.el.setPointerCapture(e.pointerId); } catch (x) { /* the touch ended */ }
      }
      var room = row.el.offsetWidth * 0.9;
      var pos = Math.max(-room, Math.min(room, start.from + dx));
      if ((pos > 0 && !s.lead) || (pos < 0 && !s.trail)) { pos = 0; }
      s.pos = pos;
      s.el.style.transform = "translateX(" + pos + "px)";
      if (s.lead) { s.lead.style.width = Math.max(s.base[0], pos) + "px"; }
      if (s.trail) { s.trail.style.width = Math.max(s.base[1], -pos) + "px"; }
    });
    function finish() {
      if (!start) { return; }
      var moved = start.locked;
      start = null;
      if (!moved) { return; }
      s.stamp = Date.now();
      var w = row.el.offsetWidth, pos = s.pos;
      row.el.classList.add("settle");
      if (pos > 0.6 * w && s.leadAction) { rest(s, true); open(s.leadAction, [row], 0); }
      else if (pos < -0.6 * w && s.outAction) { rest(s, true); open(s.outAction, [row], 0); }
      else if (pos > 0.4 * s.base[0] && s.lead) { s.pos = s.base[0]; s.el.style.transform = "translateX(" + s.base[0] + "px)"; s.lead.style.width = ""; opened = s; }
      else if (pos < -0.4 * s.base[1] && s.trail) { s.pos = -s.base[1]; s.el.style.transform = "translateX(" + (-s.base[1]) + "px)"; s.trail.style.width = ""; opened = s; }
      else { rest(s, true); if (opened === s) { opened = null; } }
    }
    row.el.addEventListener("pointerup", finish);
    row.el.addEventListener("pointercancel", finish);
    row.el.addEventListener("click", function (e) {      /* a drag is not a tap on the title link */
      if (Date.now() - s.stamp < 400) { e.preventDefault(); e.stopPropagation(); s.stamp = 0; }
    }, true);
  });
  document.addEventListener("pointerdown", function (e) { if (opened && !opened.el.contains(e.target)) { closeOpen(); } });
  window.addEventListener("scroll", closeOpen, { passive: true });
})();
