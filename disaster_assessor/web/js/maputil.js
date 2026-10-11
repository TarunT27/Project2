// Leaflet helpers shared by the console, map and task views.
import { CLASSES, COLORS, SEVERITY, validCoord } from "./ui.js";

export function darkTiles(config, dark = true) {
  return L.tileLayer(config.tile_url, { attribution: config.tile_attribution, maxZoom: 19, className: dark ? "tiles-dark" : "" });
}

// Observations sharing one coordinate (all boxes from one photo) become one capture point.
export function groupByPoint(observations) {
  const points = new Map();
  observations.forEach((o) => {
    if (!validCoord(o.latitude, o.longitude)) return;
    const key = `${o.latitude.toFixed(6)},${o.longitude.toFixed(6)}`;
    if (!points.has(key)) points.set(key, { key, lat: o.latitude, lon: o.longitude, list: [] });
    points.get(key).list.push(o);
  });
  return [...points.values()].map((p) => {
    p.list.sort((a, b) => SEVERITY[b.damage_class] - SEVERITY[a.damage_class] || b.confidence - a.confidence);
    p.worst = p.list[0].damage_class;
    return p;
  });
}

// Split-colour ring: each class gets an arc proportional to its share.
export function ringGradient(list) {
  const n = list.length, counts = CLASSES.slice().reverse().map((c) => [c, list.filter((o) => o.damage_class === c).length]).filter(([, k]) => k);
  if (counts.length === 1) return COLORS[counts[0][0]];
  let at = 0;
  const stops = counts.map(([c, k]) => {
    const end = at + (k / n) * 100, s = `#0e0f11 ${at}% ${at + 1.5}%, ${COLORS[c]} ${at + 1.5}% ${end}%`;
    at = end;
    return s;
  });
  return `conic-gradient(${stops.join(", ")})`;
}

export function pointIcon(point, selected = false) {
  const n = point.list.length, size = n > 1 ? 34 : 22;
  return L.divIcon({
    className: "",
    iconSize: [size, size],
    html: `<span class="map-pt${selected ? " sel" : ""}" style="width:${size}px;height:${size}px;background:${ringGradient(point.list)}">${n > 1 ? `<b>${n}</b>` : ""}</span>`,
  });
}

export function clusterIcon(cluster) {
  const kids = cluster.getAllChildMarkers();
  const worst = kids.reduce((m, k) => Math.max(m, SEVERITY[k.options.worst] ?? 0), 0);
  const count = kids.reduce((s, k) => s + (k.options.count || 1), 0);
  return L.divIcon({ className: "marker-cluster", iconSize: [40, 40],
    html: `<div style="border-color:${COLORS[CLASSES[worst]]}">${count}</div>` });
}

export function buildingStyle(cls, reviewed) {
  return { color: COLORS[cls] || "#888", weight: reviewed ? 2 : 1.2, fillColor: COLORS[cls] || "#888", fillOpacity: 0.45, dashArray: reviewed ? null : "4 3" };
}
