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
