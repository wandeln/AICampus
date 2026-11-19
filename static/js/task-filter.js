// Gemeinsame Mehr-Schlagwort-Filter-Logik (client-seitig) — dieselbe
// Semantik wie serverseitig (services/task_filter.py): alle
// leerzeichen-getrennten Begriffe müssen irgendwo als Teilzeichenfolge
// vorkommen (UND-Verknüpfung, Groß-/Kleinschreibung egal).

function multiKeywordTerms(filterText) {
  return String(filterText || '').toLowerCase().split(/\s+/).filter(Boolean);
}

function multiKeywordMatches(text, filterText) {
  const terms = multiKeywordTerms(filterText);
  if (!terms.length) return true; // leeres Suchfeld = kein Filter
  if (!text) return false;
  const t = String(text).toLowerCase();
  return terms.every(term => t.includes(term));
}

// ─── Filter-Panel ein-/ausblenden ────────────────────────────
// Toggle-Button blendet das Panel (hidden-Klasse) ein und aus.
function initFilterToggle(btn, panel) {
  btn.addEventListener('click', () => {
    const open = panel.classList.toggle('hidden') === false;
    btn.setAttribute('aria-expanded', String(open));
  });
}

// ─── Filter-Chips (Facetten) ─────────────────────────────────
// groups: [{ id, label, multi, values: [{ value, label }] }]
// Innerhalb einer Gruppe OR, zwischen Gruppen AND.
//   multi: false → Single-Select-Toggle (Klick aktiviert, erneuter Klick
//                  deaktiviert, Klick auf anderen Wert wechselt)
//   multi: true  → Toggle pro Wert
// row (optional): Gruppen mit gleicher row-ID teilen sich eine Zeile
// (Label der ersten Gruppe der Zeile wird angezeigt).
// initial (optional): { [groupId]: string[] } — vorab aktivierte Werte
// (z. B. aus der URL).
// Liefert { get, set, setMany, clear, hasActive }:
//   get()        → { [groupId]: string[] } (aktive Werte)
//   clear()      → alle Chips deaktivieren (+ onChange)
//   hasActive()  → true, wenn mindestens ein Chip aktiv
function initFilterChips(container, groups, onChange, initial) {
  const state = {};
  const buttons = [];
  const rows = {};
  container.innerHTML = '';
  for (const g of groups) {
    state[g.id] = new Set();
    if (initial && initial[g.id]) {
      for (const v of g.values) {
        if (initial[g.id].includes(v.value)) state[g.id].add(v.value);
      }
    }
    const rowKey = g.row || g.id;
    let row = rows[rowKey];
    if (!row) {
      row = document.createElement('div');
      row.className = 'filter-chip-row';
      if (g.label) {
        const lab = document.createElement('span');
        lab.className = 'filter-chip-label';
        lab.textContent = g.label;
        row.appendChild(lab);
      }
      rows[rowKey] = row;
      container.appendChild(row);
    }
    for (const v of g.values) {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'filter-chip';
      b.textContent = v.label;
      b.dataset.group = g.id;
      b.dataset.value = v.value;
      b.addEventListener('click', () => {
        const set = state[g.id];
        if (set.has(v.value)) set.delete(v.value);
        else if (g.multi) set.add(v.value);
        else { set.clear(); set.add(v.value); }
        sync();
      });
      row.appendChild(b);
      buttons.push(b);
    }
  }

  function paint() {
    for (const b of buttons) {
      b.classList.toggle('active', state[b.dataset.group].has(b.dataset.value));
    }
  }

  function sync() {
    paint();
    if (onChange) onChange();
  }

  paint(); // initialer State (z. B. aus URL) sichtbar machen

  return {
    get: () => Object.fromEntries(Object.entries(state).map(([k, s]) => [k, [...s]])),
    // Gruppe auf exakt diese Werte setzen (leer = keine aktiv) — löst onChange aus
    set: (groupId, values) => {
      const s = state[groupId];
      if (!s) return;
      s.clear();
      for (const v of values) s.add(v);
      sync();
    },
    // Mehrere Gruppen auf einmal setzen ({ [groupId]: string[] }) — ein onChange
    setMany: (map) => {
      for (const [groupId, values] of Object.entries(map)) {
        const s = state[groupId];
        if (!s) continue;
        s.clear();
        for (const v of values) s.add(v);
      }
      sync();
    },
    clear: syncClear,
    hasActive: () => Object.values(state).some(s => s.size > 0),
  };

  function syncClear() {
    Object.values(state).forEach(s => s.clear());
    sync();
  }
}
