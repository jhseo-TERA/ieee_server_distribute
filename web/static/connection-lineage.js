/* Direct-citation traversal over already loaded records. No IO or inferred links. */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.ConnectionLineage = factory();
})(typeof window === 'undefined' ? globalThis : window, function () {
  'use strict';
  function explore({ nodes, edges, root, depth = 1, direction = 'both', limit = 30, filter }) {
    const papers = new Map((nodes || []).map(paper => [String(paper.id), paper]));
    root = String(root);
    if (!papers.has(root)) return { nodes: [], edges: [], prior: [], derivative: [], candidate_count: 0, filtered_count: 0, limited: false };
    depth = [1, 2, 3].includes(Number(depth)) ? Number(depth) : 1;
    limit = Math.max(1, Math.min(60, Number(limit) || 30));
    const incoming = new Map(), outgoing = new Map(), observed = new Map();
    for (const edge of edges || []) {
      const source = String(edge.source), target = String(edge.target);
      if (source === target || !papers.has(source) || !papers.has(target)) continue;
      const citation = { source, target };
      if (['openalex','crossref','pdf','ieee'].includes(edge.reference_source)) citation.reference_source = edge.reference_source;
      for (const key of ['reference_number','reference_page']) if (Number.isInteger(edge[key]) && edge[key] > 0 && edge[key] < 10000) citation[key] = edge[key];
      observed.set(JSON.stringify([source, target]), citation);
      if (!incoming.has(target)) incoming.set(target, new Set());
      if (!outgoing.has(source)) outgoing.set(source, new Set());
      incoming.get(target).add(source); outgoing.get(source).add(target);
    }
    function distances(adjacency, enabled) {
      const found = new Map([[root, 0]]), parent = new Map(), queue = [root];
      if (enabled) for (let i = 0; i < queue.length; i++) {
        const key = queue[i], level = found.get(key);
        if (level >= depth) continue;
        for (const next of [...(adjacency.get(key) || [])].sort()) if (!found.has(next)) {
          found.set(next, level + 1); parent.set(next, key); queue.push(next);
        }
      }
      return { found, parent };
    }
    const priorTree = distances(incoming, direction !== 'derivative');
    const derivativeTree = distances(outgoing, direction !== 'prior');
    const prior = priorTree.found, derivative = derivativeTree.found;
    const connected = [...new Set([...prior.keys(), ...derivative.keys()])].map(id => ({
      ...papers.get(id), id, is_lineage_root: id === root,
      prior_level: prior.get(id) ?? null, derivative_level: derivative.get(id) ?? null,
      lineage_level: Math.min(prior.get(id) ?? Infinity, derivative.get(id) ?? Infinity),
    }));
    const ranked = connected.filter(paper => paper.id === root || !filter || filter(paper)).sort((a, b) =>
      a.lineage_level - b.lineage_level || (Number(b.citation_count) || 0) - (Number(a.citation_count) || 0) || a.id.localeCompare(b.id));
    // Round-robin over direction and level so a large first level does not
    // consume the whole cap. Include each chosen paper's saved path to root.
    const eligible = new Map(ranked.map(paper => [paper.id, paper]));
    const groups = [];
    for (let level = 1; level <= depth; level++) for (const tree of [priorTree, derivativeTree]) {
      groups.push({ tree, queue: ranked.filter(paper => tree.found.get(paper.id) === level) });
    }
    const ids = new Set([root]);
    let advanced = true;
    while (ids.size < limit && advanced) {
      advanced = false;
      for (const group of groups) {
        while (group.queue.length) {
          const candidate = group.queue.shift();
          if (ids.has(candidate.id)) continue;
          const path = []; let key = candidate.id;
          while (key && !ids.has(key)) {
            if (eligible.has(key)) path.push(key);
            key = group.tree.parent.get(key);
          }
          if (ids.size + path.length > limit) continue;
          for (const key of path.reverse()) ids.add(key);
          advanced = true; break;
        }
        if (ids.size >= limit) break;
      }
    }
    const selected = ranked.filter(paper => ids.has(paper.id));
    return { root, nodes: selected,
      edges: [...observed.values()].filter(edge => ids.has(edge.source) && ids.has(edge.target)).sort((a, b) => a.source.localeCompare(b.source) || a.target.localeCompare(b.target)),
      prior: selected.filter(paper => paper.prior_level > 0),
      derivative: selected.filter(paper => paper.derivative_level > 0),
      candidate_count: connected.length, filtered_count: ranked.length, limited: ranked.length > selected.length };
  }
  return { explore };
});
