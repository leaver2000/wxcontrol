// Inlines data/geo.json into src/index.template.html and writes the deployable index.html.
const fs = require('fs');
const path = require('path');
const root = path.join(__dirname, '..');
const tpl = fs.readFileSync(path.join(root, 'src', 'index.template.html'), 'utf8');
const geo = fs.readFileSync(path.join(root, 'data', 'geo.json'), 'utf8');
if (!tpl.includes('/*GEO*/')) throw new Error('template is missing the /*GEO*/ placeholder');
const out = tpl.replace('/*GEO*/', geo);
fs.writeFileSync(path.join(root, 'index.html'), out);
console.log(`index.html written (${out.length} bytes)`);
