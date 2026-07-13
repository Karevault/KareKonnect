(function () {
  "use strict";

  var HOURS = [];
  for (var h = 7; h <= 18; h++) {
    HOURS.push((h < 10 ? "0" : "") + h + ":00");
  }

  var DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

  function parsePage() {
    var el = document.getElementById("appt-cal-data");
    if (!el) return null;
    try {
      return JSON.parse(el.textContent || "{}");
    } catch (e) {
      return null;
    }
  }

  function pad(n) {
    return n < 10 ? "0" + n : String(n);
  }

  function addDays(iso, delta) {
    var p = iso.split("-");
    var d = new Date(Date.UTC(+p[0], +p[1] - 1, +p[2]));
    d.setUTCDate(d.getUTCDate() + delta);
    return d.getUTCFullYear() + "-" + pad(d.getUTCMonth() + 1) + "-" + pad(d.getUTCDate());
  }

  function mondayOf(iso) {
    var p = iso.split("-");
    var d = new Date(Date.UTC(+p[0], +p[1] - 1, +p[2]));
    var day = d.getUTCDay();
    var diff = day === 0 ? -6 : 1 - day;
    d.setUTCDate(d.getUTCDate() + diff);
    return d.getUTCFullYear() + "-" + pad(d.getUTCMonth() + 1) + "-" + pad(d.getUTCDate());
  }

  function monthGridStart(anchorIso) {
    var p = anchorIso.split("-");
    var first = new Date(Date.UTC(+p[0], +p[1] - 1, 1));
    var day = first.getUTCDay();
    var diff = day === 0 ? -6 : 1 - day;
    first.setUTCDate(first.getUTCDate() + diff);
    return first.getUTCFullYear() + "-" + pad(first.getUTCMonth() + 1) + "-" + pad(first.getUTCDate());
  }

  function anchorMonth(iso) {
    return iso.slice(0, 7);
  }

  function CalApp() {
    this.page = parsePage() || {};
    this.view = this.page.view || "month";
    this.anchor = this.page.anchor_date || new Date().toISOString().slice(0, 10);
    this.today = (this.root && this.root.getAttribute("data-today")) || "";
    this.events = (this.page.events || []).slice();
    this.pending = (this.page.pending || []).slice();
    this.dragId = null;
    this.root = document.getElementById("appt-cal-root");
    this.pool = document.getElementById("appt-pending-pool");
    this.mount = document.getElementById("appt-cal-mount");
    this.labelEl = document.getElementById("appt-cal-nav-label");
    this.toastEl = document.getElementById("appt-cal-toast");
    this.bindToolbar();
    this.render();
  }

  CalApp.prototype.bindToolbar = function () {
    var self = this;
    document.querySelectorAll("[data-appt-view]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        self.setView(btn.getAttribute("data-appt-view"));
      });
    });
    var prev = document.getElementById("appt-cal-prev");
    var next = document.getElementById("appt-cal-next");
    var todayBtn = document.getElementById("appt-cal-today");
    if (prev) prev.addEventListener("click", function () { self.navigate(-1); });
    if (next) next.addEventListener("click", function () { self.navigate(1); });
    if (todayBtn) todayBtn.addEventListener("click", function () {
      self.anchor = self.today;
      self.reloadPage();
    });
    var notifyChk = document.getElementById("appt-notify-on-drag");
    this.notifyOnDrag = notifyChk ? notifyChk.checked : true;
    if (notifyChk) {
      notifyChk.addEventListener("change", function () {
        self.notifyOnDrag = notifyChk.checked;
      });
    }
  };

  CalApp.prototype.setView = function (view) {
    this.view = view;
    this.reloadPage();
  };

  CalApp.prototype.navigate = function (dir) {
    var nav = this.page.nav || {};
    if (dir < 0 && nav.prev_date) this.anchor = nav.prev_date;
    if (dir > 0 && nav.next_date) this.anchor = nav.next_date;
    this.reloadPage();
  };

  CalApp.prototype.reloadPage = function () {
    var url = "/admin/appointment?view=" + encodeURIComponent(this.view) +
      "&date=" + encodeURIComponent(this.anchor);
    window.location.href = url;
  };

  CalApp.prototype.eventsForDate = function (iso) {
    return this.events.filter(function (e) { return e.date === iso; });
  };

  CalApp.prototype.eventsForDateHour = function (iso, hour) {
    return this.events.filter(function (e) {
      if (e.date !== iso) return false;
      var t = e.time || "09:00";
      return t.slice(0, 2) === hour.slice(0, 2);
    });
  };

  CalApp.prototype.pendingUnscheduled = function () {
    return this.pending.filter(function (p) { return !p.date; });
  };

  CalApp.prototype.showToast = function (msg, isError) {
    if (!this.toastEl) return;
    this.toastEl.className = "appt-cal-toast alert alert-" + (isError ? "danger" : "success");
    this.toastEl.textContent = msg;
    this.toastEl.style.display = "block";
    var self = this;
    setTimeout(function () { self.toastEl.style.display = "none"; }, 3500);
  };

  CalApp.prototype.render = function () {
    var self = this;
    document.querySelectorAll("[data-appt-view]").forEach(function (btn) {
      btn.classList.toggle("active", btn.getAttribute("data-appt-view") === self.view);
    });
    if (this.labelEl && this.page.nav) {
      this.labelEl.textContent = this.page.nav.label || "";
    }
    this.renderPool();
    if (this.view === "day") this.renderDay();
    else if (this.view === "week") this.renderWeek();
    else this.renderMonth();
  };

  CalApp.prototype.renderPool = function () {
    var self = this;
    if (!this.pool) return;
    this.pool.innerHTML = "";
    var items = this.pendingUnscheduled();
    if (!items.length) {
      this.pool.innerHTML = '<span class="text-muted small">No pending requests in queue.</span>';
      return;
    }
    items.forEach(function (ev) {
      self.pool.appendChild(self.createEventEl(ev, true));
    });
  };

  CalApp.prototype.eventHtml = function (ev) {
    var time = ev.time ? ev.time + " · " : "";
    return (
      '<span class="ev-ref">' + ev.ref + "</span> " +
      '<span class="ev-meta">' + time + ev.visit_label + " · " + ev.status + "</span>" +
      (ev.provider_name && ev.provider_name !== "—"
        ? '<br/><span class="ev-meta">' + ev.provider_name + "</span>"
        : "") +
      '<div class="ev-actions">' +
      (ev.can_assign
        ? '<a href="' + ev.assign_url + '" onclick="event.stopPropagation()">Approve</a>'
        : "") +
      '<a href="' + ev.detail_url + '" onclick="event.stopPropagation()">Details</a>' +
      "</div>"
    );
  };

  CalApp.prototype.createEventEl = function (ev, inPool) {
    var el = document.createElement("div");
    el.className = "appt-event status-" + (ev.status || "requested");
    el.setAttribute("draggable", ev.draggable !== false ? "true" : "false");
    el.setAttribute("data-id", ev.id);
    el.innerHTML = this.eventHtml(ev);
    el.title = ev.reason || "";
    if (ev.draggable !== false) {
      var self = this;
      el.addEventListener("dragstart", function (e) {
        self.dragId = ev.id;
        e.dataTransfer.setData("text/plain", ev.id);
        e.dataTransfer.effectAllowed = "move";
      });
      el.addEventListener("dragend", function () {
        self.dragId = null;
      });
    }
    return el;
  };

  CalApp.prototype.bindDropZone = function (el, dateIso, timeStr, sourceOnly) {
    var self = this;
    if (sourceOnly) return;
    el.addEventListener("dragover", function (e) {
      e.preventDefault();
      e.dataTransfer.dropEffect = "move";
      el.classList.add("is-drag-over");
    });
    el.addEventListener("dragleave", function () {
      el.classList.remove("is-drag-over");
    });
    el.addEventListener("drop", function (e) {
      e.preventDefault();
      el.classList.remove("is-drag-over");
      var id = e.dataTransfer.getData("text/plain") || self.dragId;
      if (!id || !dateIso) return;
      self.reschedule(id, dateIso, timeStr);
    });
  };

  CalApp.prototype.findEvent = function (id) {
    var i, ev;
    for (i = 0; i < this.events.length; i++) {
      if (this.events[i].id === id) return this.events[i];
    }
    for (i = 0; i < this.pending.length; i++) {
      if (this.pending[i].id === id) return this.pending[i];
    }
    return null;
  };

  CalApp.prototype.moveToPending = function (ev) {
    this.events = this.events.filter(function (e) { return e.id !== ev.id; });
    var found = false;
    this.pending = this.pending.map(function (p) {
      if (p.id === ev.id) { found = true; return ev; }
      return p;
    });
    if (!found) this.pending.push(ev);
    this.render();
  };

  CalApp.prototype.reschedule = function (id, dateIso, timeStr) {
    var self = this;
    if (this.root) this.root.classList.add("appt-cal-saving");
    fetch("/admin/appointment/reschedule", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      body: JSON.stringify({
        appointment_id: id,
        date: dateIso,
        time: timeStr || null,
        notify_patient: this.notifyOnDrag,
      }),
    })
      .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
      .then(function (res) {
        if (self.root) self.root.classList.remove("appt-cal-saving");
        if (!res.ok) {
          self.showToast(res.j.error || "Reschedule failed", true);
          self.reloadPage();
          return;
        }
        var updated = res.j.event;
        if (updated) {
          self.applyEventUpdate(updated);
          self.render();
        }
        self.showToast(res.j.message || "Rescheduled");
      })
      .catch(function () {
        if (self.root) self.root.classList.remove("appt-cal-saving");
        self.showToast("Network error", true);
      });
  };

  CalApp.prototype.applyEventUpdate = function (ev) {
    var self = this;
    this.events = this.events.filter(function (e) { return e.id !== ev.id; });
    this.pending = this.pending.filter(function (p) { return p.id !== ev.id; });
    if (ev.status === "requested" && !ev.date) {
      this.pending.push(ev);
    } else if (ev.date) {
      this.events.push(ev);
    }
  };

  CalApp.prototype.renderMonth = function () {
    if (!this.mount) return;
    this.mount.innerHTML = "";
    this.mount.className = "appt-cal-grid";
    var self = this;
    DOW.forEach(function (d) {
      var h = document.createElement("div");
      h.className = "appt-cal-dow";
      h.textContent = d;
      self.mount.appendChild(h);
    });
    var start = monthGridStart(this.anchor);
    var curMonth = anchorMonth(this.anchor);
    for (var i = 0; i < 42; i++) {
      var iso = addDays(start, i);
      this.mount.appendChild(this.buildDayCell(iso, anchorMonth(iso) !== curMonth));
    }
  };

  CalApp.prototype.renderWeek = function () {
    if (!this.mount) return;
    this.mount.innerHTML = "";
    this.mount.className = "appt-cal-grid week-view";
    var self = this;
    var weekStart = mondayOf(this.anchor);
    DOW.forEach(function (d) {
      var h = document.createElement("div");
      h.className = "appt-cal-dow";
      h.textContent = d;
      self.mount.appendChild(h);
    });
    for (var i = 0; i < 7; i++) {
      var iso = addDays(weekStart, i);
      this.mount.appendChild(this.buildDayCell(iso, false));
    }
  };

  function weekdayIndex(iso) {
    var p = iso.split("-");
    var d = new Date(Date.UTC(+p[0], +p[1] - 1, +p[2]));
    var w = d.getUTCDay();
    return w === 0 ? 6 : w - 1;
  }

  CalApp.prototype.buildDayCell = function (iso, isOutside) {
    var self = this;
    var cell = document.createElement("div");
    cell.className = "appt-cal-day";
    if (iso === this.today) cell.classList.add("is-today");
    if (isOutside) cell.classList.add("is-outside");
    var parts = iso.split("-");
    var head = document.createElement("div");
    head.className = "appt-cal-day-head";
    head.innerHTML = parts[2] + ' <span class="muted">' + DOW[weekdayIndex(iso)] + "</span>";
    cell.appendChild(head);
    this.eventsForDate(iso).forEach(function (ev) {
      cell.appendChild(self.createEventEl(ev, false));
    });
    this.bindDropZone(cell, iso, null);
    return cell;
  };

  CalApp.prototype.renderDay = function () {
    if (!this.mount) return;
    this.mount.innerHTML = "";
    this.mount.className = "";
    var self = this;
    var layout = document.createElement("div");
    layout.className = "appt-day-layout";
    var hoursCol = document.createElement("div");
    hoursCol.className = "appt-day-hours";
    var slotsCol = document.createElement("div");
    slotsCol.className = "appt-day-slots";
    HOURS.forEach(function (hour) {
      var lbl = document.createElement("div");
      lbl.className = "appt-day-hour-label";
      lbl.textContent = hour;
      hoursCol.appendChild(lbl);
      var slot = document.createElement("div");
      slot.className = "appt-day-slot";
      self.eventsForDateHour(self.anchor, hour).forEach(function (ev) {
        slot.appendChild(self.createEventEl(ev, false));
      });
      self.bindDropZone(slot, self.anchor, hour);
      slotsCol.appendChild(slot);
    });
    layout.appendChild(hoursCol);
    layout.appendChild(slotsCol);
    this.mount.appendChild(layout);
  };

  document.addEventListener("DOMContentLoaded", function () {
    if (document.getElementById("appt-cal-root")) {
      new CalApp();
    }
  });
})();
