/**
 * autosave-versions.js — wiederverwendbare Autosave- + Versions-History-Logik.
 *
 * Für alle bearbeitbaren Elementtypen (aktuell Skript-Kapitel, Slide-Decks
 * + Applets, später u. a. Aufgaben). Koppelt an die generische Versions-API:
 *   GET/POST /api/versions/{entity_type}/{entity_id}
 *   PUT/PATCH/DELETE/POST(restore) /api/versions/{version_id}
 *
 * Die API-Endpunkte speichert gleichzeitig das Element (Adapter) und die
 * Version → ein Request pro Autosave.
 *
 * Nutzung (Seite):
 *   const av = AV.create({
 *     entityType: 'script_section',
 *     entityId: 42,                // null = neues Element (erstellt per createEntity)
 *     createEntity: async (p) => { ...POST...; return {id}; },  // nur bei Neu
 *     getPayload:  () => ({title, content, summary}),
 *     applyPayload:(p) => { ...Editor befüllen... },
 *     shouldSave:  (p) => !!p.title.trim(),   // optional, Default: true
 *     onSaved:     (info) => {},              // optional
 *     onRestored:  (v) => {},                 // optional
 *     hostEl: toolbarElement,
 *   });
 *   // Beim Editor-Ändern:  av.onChange();
 *   // Baum-/Datei-Änderung: av.touch();   // Payload gleich, nur Dateien
 *   // „Fertig":            const id = await av.finish();
 *   // Aufräumen:           av.destroy();
 *
 * Semantik History: Pro Bearbeitungs-Session eine Version (wird beim ersten
 * Autosave angelegt und durch subsequente Autosaves geupdatet). Restore
 * hinterlegt den alten Stand als neue (aktuelle) Version.
 */
(function () {
  const AV = {};
  window.AV = AV;

  // ─── API ──────────────────────────────────────────────────────
  const api = {
    async _json(url, method, body) {
      const res = await fetch(url, {
        method: method || 'GET',
        credentials: 'same-origin',
        headers: body ? { 'Content-Type': 'application/json' } : undefined,
        body: body ? JSON.stringify(body) : undefined,
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || 'Fehler (' + res.status + ')');
      return data;
    },
    list: (type, id) => api._json(`/api/versions/${type}/${id}`).then(d => d.versions || []),
    create: (type, id, snapshot) =>
      api._json(`/api/versions/${type}/${id}`, 'POST', { snapshot }).then(d => d.version),
    update: (versionId, snapshot) =>
      api._json(`/api/versions/${versionId}`, 'PUT', { snapshot }).then(d => d.version),
    rename: (versionId, name) =>
      api._json(`/api/versions/${versionId}/name`, 'PATCH', { name }).then(d => d.version),
    remove: (versionId) => api._json(`/api/versions/${versionId}`, 'DELETE'),
    restore: (versionId) => api._json(`/api/versions/${versionId}/restore`, 'POST').then(d => d.version),
  };
  AV.api = api;

  // ─── Utilities ────────────────────────────────────────────────
  AV.fmtTime = function (iso) {
    if (!iso) return '';
    const d = new Date(iso);
    if (isNaN(d.getTime())) return '';
    const p = (n) => String(n).padStart(2, '0');
    return `${p(d.getDate())}.${p(d.getMonth() + 1)}.${d.getFullYear()}, ${p(d.getHours())}:${p(d.getMinutes())}`;
  };

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function samePayload(a, b) {
    if (!a || !b) return false;
    // Payloads stammen jeweils aus derselben getPayload-Funktion → gleiche
    // Schlüsselreihenfolge, daher ist ein JSON-Vergleich elementtyp-agnostisch
    // sicher (z. B. auch für Applets: {title, html, llm_description}).
    return JSON.stringify(a) === JSON.stringify(b);
  }

  const STATUS = {
    clean: '',
    pending: '…',
    saving: 'Wird gespeichert…',
    saved: 'Gespeichert ✓',
    error: '⚠ Auto-Save-Fehler',
  };

  // ─── Controller ───────────────────────────────────────────────
  function createController(cfg) {
    const type = cfg.entityType;
    let id = cfg.entityId != null ? cfg.entityId : null;
    const getPayload = cfg.getPayload;
    const applyPayload = cfg.applyPayload;
    const shouldSave = cfg.shouldSave || (() => true);
    const createEntity = cfg.createEntity || null;
    const onSaved = cfg.onSaved || (() => {});
    const onRestored = cfg.onRestored || null;
    const hostEl = cfg.hostEl;
    const debounceMs = cfg.debounceMs || 1200;

    let versionId = null;
    let lastPayload = null;
    let dirty = false;
    let saving = false;
    let timer = null;
    let force = false;  // nächstes Save: samePayload-Check überspringen
    let versions = [];

    // ── UI: Button + Status + Panel ──
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'bg-gray-200 hover:bg-gray-300 text-gray-700 px-3 py-1.5 rounded-lg text-sm font-medium transition';
    btn.innerHTML = '🕓 Versionen';
    btn.title = 'Versions-History (wiederherstellen, umbenennen, löschen)';

    const statusEl = document.createElement('span');
    statusEl.className = 'av-status text-xs text-gray-400 select-none whitespace-nowrap';
    statusEl.textContent = '';

    const panel = document.createElement('div');
    panel.className = 'av-panel hidden fixed z-50 bg-white border border-gray-200 rounded-xl shadow-xl overflow-hidden';
    panel.style.width = '340px';
    panel.style.maxHeight = '420px';
    panel.style.display = 'none';

    if (hostEl) {
      hostEl.appendChild(btn);
      hostEl.appendChild(statusEl);
    }
    document.body.appendChild(panel);

    function setStatus(key) {
      statusEl.textContent = STATUS[key] || '';
      statusEl.style.color = key === 'error' ? '#b91c1c' : '#9ca3af';
    }

    // ── Autosave ──
    function onChange() {
      // Auch während eines laufenden Saves: Änderung wird erfasst und
      // nach dem laufenden Save erneut gespeichert (sonst gehen
      // Tipppausen während langsamer Requests verloren).
      dirty = true;
      setStatus('pending');
      if (timer) clearTimeout(timer);
      timer = setTimeout(doAutosave, debounceMs);
    }

    // Datei-/Baum-Änderung (z. B. Workspace-Dateibaum): Das Element-Payload
    // (reguläre Felder) bleibt dabei oft unverändert, aber gespeichert
    // werden MUSS — der Server erfasst den aktuellen Datei-Stand in der
    // Version. Discrete Operationen → kürzeres Debounce als onChange.
    function touch() {
      dirty = true;
      force = true;
      setStatus('pending');
      if (timer) clearTimeout(timer);
      timer = setTimeout(doAutosave, 600);
    }

    async function doAutosave() {
      if (timer) { clearTimeout(timer); timer = null; }
      if (saving) {
        // Vorheriges Save läuft noch → kurz später erneut versuchen
        timer = setTimeout(doAutosave, 500);
        return;
      }
      const payload = getPayload();
      const forced = force;
      force = false;
      if (!shouldSave(payload) || (samePayload(payload, lastPayload) && !forced)) {
        dirty = false;
        setStatus('clean');
        return;
      }
      saving = true;
      setStatus('saving');
      try {
        let created = false;
        if (id == null) {
          if (!createEntity) throw new Error('Kein createEntity definiert (neues Element).');
          const saved = await createEntity(payload);
          id = saved && saved.id != null ? saved.id : saved;
          created = true;
        }
        if (versionId == null) {
          const v = await api.create(type, id, payload);
          versionId = v.id;
        } else {
          await api.update(versionId, payload);
        }
        lastPayload = payload;
        dirty = false;
        setStatus('saved');
        try { onSaved({ created, id, versionId }); } catch (e) { /* ignoriere */ }
      } catch (e) {
        dirty = true; // beim nächsten Ändern/„Fertig“ erneut versuchen
        setStatus('error');
        showToast('Auto-Save fehlgeschlagen: ' + (e.message || ''), 'error');
      } finally {
        saving = false;
      }
    }

    // „Fertig“: ausstehendes Autosave-Flush.
    // Liefert { id, ok, versionId } – ok=false, wenn das letzte Speichern fehlgeschlagen ist.
    async function finish() {
      if (timer) { clearTimeout(timer); timer = null; }
      if (dirty) await doAutosave();
      return { id, ok: !dirty && id != null, versionId };
    }

    function setBaseline(payload) {
      lastPayload = payload;
      dirty = false;
      if (timer) { clearTimeout(timer); timer = null; }
      setStatus('clean');
    }

    // ── Versions-Dropdown ──
    async function refreshVersions() {
      if (id == null) {
        versions = [];
        renderPanel();
        return;
      }
      try {
        versions = await api.list(type, id);
      } catch (e) {
        versions = [];
      }
      renderPanel();
    }

    function displayName(v) {
      return v.name || AV.fmtTime(v.updated_at);
    }

    function renderPanel() {
      // Mülleimer: Bulk-Delete aller Versionen ohne eigenen Namen
      // (die aktuelle Version, Index 0, bleibt stets erhalten).
      const unlabeledCount = versions.reduce((n, v, i) => n + (i > 0 && !v.name ? 1 : 0), 0);
      let html = '<div class="px-3 py-2 border-b border-gray-100 bg-gray-50 flex items-center justify-between gap-2">' +
        '<span class="text-xs font-semibold text-gray-500 uppercase tracking-wide">Versions-History</span>' +
        (unlabeledCount > 0
          ? '<button type="button" data-act="bulk-delete-unlabeled" class="text-gray-400 hover:text-red-600 text-sm px-1.5 py-0.5 rounded hover:bg-red-50 transition" title="Alle Versionen ohne eigenen Namen löschen (die aktuelle Version bleibt erhalten)">🗑️</button>'
          : '') +
        '</div>';
      html += '<div class="av-list overflow-y-auto" style="max-height:360px">';
      if (!versions.length) {
        html += '<div class="px-3 py-4 text-sm text-gray-400">' +
          (id == null ? 'Noch nicht gespeichert – Versionen erscheinen nach dem ersten Auto-Save.'
                      : 'Noch keine Versionen.') +
          '</div>';
      }
      versions.forEach((v, i) => {
        const current = i === 0;
        html += '<div class="av-row group flex items-center gap-2 px-3 py-2 border-b border-gray-50 hover:bg-blue-50 transition" data-vid="' + v.id + '">' +
          '<div class="flex-1 min-w-0 cursor-pointer" data-act="restore" title="Diese Version wiederherstellen">' +
            '<div class="text-sm text-gray-800 truncate">' + esc(displayName(v)) +
              (current ? ' <span class="ml-1 text-[10px] font-semibold text-blue-600 bg-blue-100 rounded px-1 py-0.5 align-middle">aktuell</span>' : '') +
            '</div>' +
            '<div class="text-[11px] text-gray-400">' + (v.name ? AV.fmtTime(v.updated_at) : 'Stand: ' + AV.fmtTime(v.updated_at)) + '</div>' +
          '</div>' +
          '<button type="button" class="av-rename opacity-0 group-hover:opacity-100 text-gray-400 hover:text-blue-600 text-sm px-1.5 py-1 rounded hover:bg-blue-50" data-act="rename" title="Umbenennen">✏️</button>' +
          '<button type="button" class="av-delete opacity-0 group-hover:opacity-100 text-gray-400 hover:text-red-600 text-sm px-1.5 py-1 rounded hover:bg-red-50" data-act="delete" title="Version löschen">✖</button>' +
        '</div>';
      });
      html += '</div>';
      panel.innerHTML = html;
    }

    function positionPanel() {
      const rect = btn.getBoundingClientRect();
      const w = 340;
      let left = rect.right - w;
      if (left < 8) left = 8;
      let top = rect.bottom + 6;
      panel.style.left = left + 'px';
      panel.style.top = top + 'px';
      // Unten im Viewport → nach oben klappen
      requestAnimationFrame(() => {
        const ph = panel.offsetHeight;
        if (top + ph > window.innerHeight - 8) {
          panel.style.top = Math.max(8, rect.top - ph - 6) + 'px';
        }
      });
    }

    function openPanel() {
      refreshVersions();
      panel.style.display = 'block';
      panel.classList.remove('hidden');
      positionPanel();
    }
    function closePanel() {
      panel.style.display = 'none';
      panel.classList.add('hidden');
    }
    function togglePanel() {
      if (panel.style.display === 'block') closePanel();
      else openPanel();
    }

    async function restoreVersion(vid) {
      const v = versions.find((x) => x.id === vid);
      const label = v ? displayName(v) : 'diese Version';
      if (!confirm('Version „' + label + '“ wiederherstellen?\nDer aktuelle Inhalt wird durch den Version-Stand ersetzt.')) return;
      try {
        const nv = await api.restore(vid);
        versionId = nv.id;
        applyPayload(nv.snapshot || {});
        setBaseline(nv.snapshot || {});
        setStatus('saved');
        await refreshVersions();
        closePanel();
        try { if (onRestored) onRestored(nv); } catch (e) { /* ignoriere */ }
        showToast('Version wiederhergestellt.', 'success');
      } catch (e) {
        showToast('Wiederherstellen fehlgeschlagen: ' + (e.message || ''), 'error');
      }
    }

    async function deleteVersion(vid) {
      if (!confirm('Diese Version wirklich löschen?')) return;
      try {
        await api.remove(vid);
        if (versionId === vid) versionId = null; // aktuelle Version gelöscht → nächstes Speichern legt neue an
        await refreshVersions();
      } catch (e) {
        showToast('Löschen fehlgeschlagen: ' + (e.message || ''), 'error');
      }
    }

    // Bulk-Delete: alle Versionen ohne eigenen Namen (die aktuelle
    // Version, Index 0, bleibt stets erhalten).
    async function bulkDeleteUnlabeled() {
      const targets = [];
      versions.forEach((v, i) => { if (i > 0 && !v.name) targets.push(v); });
      if (!targets.length) return;
      const n = targets.length;
      const plural = n === 1 ? '' : 'en';
      if (!confirm(n + ' Version' + plural + ' ohne eigenen Namen löschen?\nDie aktuelle Version bleibt erhalten.')) return;
      try {
        for (const v of targets) {
          await api.remove(v.id);
          if (versionId === v.id) versionId = null;
        }
        await refreshVersions();
        showToast(n + ' Version' + plural + ' gelöscht.', 'success');
      } catch (e) {
        showToast('Löschen fehlgeschlagen: ' + (e.message || ''), 'error');
        refreshVersions();
      }
    }

    function startRename(vid, rowEl) {
      const v = versions.find((x) => x.id === vid);
      if (!v) return;
      const nameWrap = rowEl.querySelector('[data-act="restore"] > div:first-child');
      if (!nameWrap) return;
      const input = document.createElement('input');
      input.type = 'text';
      input.value = v.name || '';
      input.placeholder = AV.fmtTime(v.updated_at) + ' (Standard)';
      input.maxLength = 200;
      input.className = 'w-full text-sm border border-blue-300 rounded px-2 py-1 focus:outline-none focus:ring-2 focus:ring-blue-400';
      nameWrap.innerHTML = '';
      nameWrap.appendChild(input);
      input.focus();
      input.select();
      let done = false;
      const commit = async (save) => {
        if (done) return;
        done = true;
        const newName = save ? input.value.trim() : v.name;
        if (save && newName !== v.name) {
          try {
            await api.rename(vid, newName);
            await refreshVersions();
          } catch (e) {
            showToast('Umbenennen fehlgeschlagen: ' + (e.message || ''), 'error');
            renderPanel();
          }
        } else {
          renderPanel();
        }
      };
      input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') { e.preventDefault(); commit(true); }
        else if (e.key === 'Escape') { e.preventDefault(); commit(false); }
      });
      input.addEventListener('blur', () => commit(true));
    }

    // Event-Delegierung im Panel
    panel.addEventListener('click', (e) => {
      if (e.target.closest('[data-act="bulk-delete-unlabeled"]')) {
        bulkDeleteUnlabeled();
        return;
      }
      const row = e.target.closest('.av-row');
      if (!row) return;
      const vid = parseInt(row.getAttribute('data-vid'), 10);
      const actEl = e.target.closest('[data-act]');
      const act = actEl ? actEl.getAttribute('data-act') : null;
      if (act === 'rename') startRename(vid, row);
      else if (act === 'delete') deleteVersion(vid);
      else if (act === 'restore' || act == null) restoreVersion(vid);
    });

    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      togglePanel();
    });
    const onDocClick = (e) => {
      if (panel.style.display === 'block' && !panel.contains(e.target) && !btn.contains(e.target)) {
        closePanel();
      }
    };
    const onDocKey = (e) => {
      if (e.key === 'Escape') closePanel();
    };
    document.addEventListener('click', onDocClick);
    document.addEventListener('keydown', onDocKey);
    window.addEventListener('scroll', closePanel, true);
    window.addEventListener('resize', closePanel);

    // ── Initial: Baseline setzen (aktueller Editor-Stand) ──
    try { setBaseline(getPayload()); } catch (e) { /* Editor noch nicht bereit */ }

    return {
      get id() { return id; },
      get versionId() { return versionId; },
      get dirty() { return dirty; },
      onChange,
      touch,
      finish,
      setBaseline,
      refreshVersions,
      restoreVersion,
      deleteVersion,
      closePanel,
      destroy() {
        if (timer) { clearTimeout(timer); timer = null; }
        document.removeEventListener('click', onDocClick);
        document.removeEventListener('keydown', onDocKey);
        window.removeEventListener('scroll', closePanel, true);
        window.removeEventListener('resize', closePanel);
        closePanel();
        if (btn.parentNode) btn.parentNode.removeChild(btn);
        if (statusEl.parentNode) statusEl.parentNode.removeChild(statusEl);
        if (panel.parentNode) panel.parentNode.removeChild(panel);
      },
    };
  }

  AV.create = createController;
})();
