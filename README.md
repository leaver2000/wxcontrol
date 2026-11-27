# Wx Analysis Console

A browser-based weather analysis tool with three linked views:

- **Map**, in the style of windy.com: color-filled fields (wind, temperature, humidity, clouds, rain/snow), animated wind particles, and labelled height contours or isobars.
- **Skew-T log-P**, styled after Air Force operational weather squadron products: temperature, dewpoint, surface parcel with CAPE/CIN shading, LCL/LFC/EL, wind barbs, a hodograph and stability indices.
- **Meteogram**: a time–height cross-section (RH, isotherms, wind) above panels for 2 m T/Td, 10 m wind and gust, MSLP, and precipitation with cloud cover.

It's a single HTML file with no framework and no server. Open `index.html` in a browser.

## Shared state

Every view reads one store, `S = { lat, lon, z, t }`, and changes it through `setState(patch, source)`. Subscribers redraw only what the change affects.

| View      | Controls | Input |
|-----------|----------|-------|
| Map       | Y X      | Click a point, or choose a station |
| Skew-T    | Z        | Click or drag vertically; ↑ ↓ when focused |
| Meteogram | T, Z     | Click or drag for T; height in the time–height panel sets Z; ← → (Shift = 6 h), ↑ ↓, Play |

`z` indexes `['SFC', 1000, 975, …, 100]` hPa and `t` is the forecast hour, 0–120.

## Data

The data is **synthetic**. An analytic atmosphere is built into the page: a travelling baroclinic wave and shortwave, a developing surface cyclone, terrain (Rockies, Sierra/Cascades, Appalachians, Mexican plateau), a land/sea mask, a diurnal cycle, frictional surface winds and flow-dependent moisture. The domain covers 135°W–55°W and 15°N–62°N on a 0.5° grid, with 21 pressure levels from an init time of 27 Nov 2026 12Z. It produces realistic structure, but it is not a forecast.

All views read data through two functions in `src/index.template.html`:

- `column(lon, lat, t)` returns `{ h, ps, mslp, sfc, lev[] }`, where each level is `{ p, z, T, Td, u, v, rh, cf, bg }`.
- `grid(levelIndex, t)` returns 0.5° `Float32Array` fields: `T, RH, U, V, S, CF, PR, SN, ISO, BG`.

To use real model output (GFS or GALWEM GRIB2 decoded server-side, or Open-Meteo pressure-level JSON), replace the bodies of these two functions. The views and the state store don't need to change.

## Layout

```
index.html                  built, deployable page (template + inlined basemap)
src/index.template.html     source: model, state store, map, Skew-T, meteogram
data/geo.json               coastlines, borders, US states, land mask (generated)
tools/geo.js                builds data/geo.json from Natural Earth / us-atlas
tools/build.js              inlines data/geo.json into the template
```

## Build

```sh
npm install        # only needed to regenerate the basemap
npm run geo        # optional: rebuild data/geo.json
npm run build      # writes index.html
npm run serve      # http://localhost:8080
```

Edit `src/index.template.html`, then run `npm run build`. `index.html` is committed so the repo can be served directly with GitHub Pages.

Basemap data comes from Natural Earth (public domain) through `world-atlas` and `us-atlas`.
