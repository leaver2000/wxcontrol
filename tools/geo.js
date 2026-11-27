// Builds compact basemap lines + land mask for the domain, writes geo.json
const fs = require('fs');
const topo = require('topojson-client');
const d3 = require('d3-geo');
const W = require('world-atlas/countries-50m.json');
const L = require('world-atlas/land-50m.json');
const US = require('us-atlas/states-10m.json');

const LON0 = -135, LON1 = -55, LAT0 = 15, LAT1 = 62, M = 2;
const inBox = ([x, y]) => x >= LON0 - M && x <= LON1 + M && y >= LAT0 - M && y <= LAT1 + M;

function lines(geom, tol) {
  const out = [];
  const coords = geom.type === 'MultiLineString' ? geom.coordinates : [geom.coordinates];
  for (const line of coords) {
    let cur = [];
    const flush = () => { if (cur.length > 1) out.push(cur); cur = []; };
    for (const p of line) {
      if (!inBox(p)) { flush(); continue; }
      const q = [Math.round(p[0] * 10), Math.round(p[1] * 10)];
      const last = cur[cur.length - 1];
      if (last && Math.hypot(q[0] - last[0], q[1] - last[1]) < tol) continue;
      cur.push(q);
    }
    flush();
  }
  return out;
}
function enc(ls) { // delta-encode into flat int arrays
  return ls.map(l => { const r = [l[0][0], l[0][1]]; for (let i = 1; i < l.length; i++) r.push(l[i][0] - l[i - 1][0], l[i][1] - l[i - 1][1]); return r; });
}
const coast = topo.mesh(L, L.objects.land);
const borders = topo.mesh(W, W.objects.countries, (a, b) => a !== b);
const states = topo.mesh(US, US.objects.states, (a, b) => a !== b);
const land = topo.feature(L, L.objects.land);

// land mask at 0.5 deg
const NX = (LON1 - LON0) * 2 + 1, NY = (LAT1 - LAT0) * 2 + 1;
let mask = '';
for (let j = 0; j < NY; j++) for (let i = 0; i < NX; i++) mask += d3.geoContains(land, [LON0 + i / 2, LAT0 + j / 2]) ? '1' : '0';
// run-length encode
let rle = [], c = mask[0], n = 0;
for (const ch of mask) { if (ch === c) n++; else { rle.push(n); c = ch; n = 1; } }
rle.push(n);

const out = { c: enc(lines(coast, 1.5)), b: enc(lines(borders, 2)), s: enc(lines(states, 2.5)), m0: mask[0], m: rle };
const s = JSON.stringify(out);
fs.writeFileSync(require('path').join(__dirname, '..', 'data', 'geo.json'), s);
console.log('bytes', s.length, 'coast', out.c.length, 'borders', out.b.length, 'states', out.s.length, 'mask', NX, NY);
