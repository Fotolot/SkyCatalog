/* Mapa materiałów – Leaflet (lokalny) + QWebChannel.
 *
 * Bezpieczeństwo: dane od aplikacji przychodzą jako JSON (liczby i identyfikatory).
 * Nazwy plików i ścieżki NIE trafiają do tej strony – lista do wyboru i panel podglądu
 * są w Qt. Teksty interfejsu wstawiane są przez textContent, nigdy innerHTML.
 */
'use strict';

const THEMES = {
  light: { photo: '#1565c0', photoFill: '#42a5f5', start: '#e65100', startFill: '#ffb74d',
           track: '#d84315', highlight: '#c2185b', ring: '#ffffff' },
  dark:  { photo: '#90caf9', photoFill: '#64b5f6', start: '#ffb74d', startFill: '#ff9800',
           track: '#ff8a65', highlight: '#f48fb1', ring: '#1e1f22' },
};

let bridge = null;
let map = null;
let renderer = null;
let tileLayer = null;
let theme = 'light';
let colors = THEMES.light;
let cfg = null;

const photoLayer = L.layerGroup();
const videoLayer = L.layerGroup();
const highlightLayer = L.layerGroup();
const photos = new Map();      // id -> circleMarker
const starts = new Map();      // id -> circleMarker
const lines = new Map();       // id -> polyline
const trackCache = new Map();  // id -> segmenty (LRU)
const TRACK_CACHE_MAX = 6000;
const wantTracks = new Set();  // filmy w widoku, które mają trasę
let selected = null;
let viewTimer = null;
let tileErrors = 0;
let tileLoads = 0;
let lastTileOk = 0;

function send(type, payload) {
  if (bridge) bridge.fromJs(JSON.stringify(Object.assign({ type: type }, payload || {})));
}

function escapeText(s) {
  const d = document.createElement('div');
  d.textContent = String(s == null ? '' : s);
  return d.innerHTML;
}

// ── Inicjalizacja ───────────────────────────────────────────

function init(config) {
  cfg = config;
  renderer = L.canvas({ padding: 0.5, tolerance: 5 });
  map = L.map('map', { preferCanvas: true, renderer: renderer, zoomControl: true,
                       minZoom: 2, maxZoom: config.maxZoom || 19, worldCopyJump: true });
  map.attributionControl.setPrefix('<a href="https://leafletjs.com">Leaflet</a>');
  // Oznaczenie źródła wymagane przez dostawcę – tekst z ustawień jest escapowany
  const attribution = escapeText(config.attribution) +
      ' | <a href="https://www.openstreetmap.org/copyright">Licencja</a>' +
      ' | <a href="https://www.openstreetmap.org/fixthemap">Zgłoś błąd mapy</a>';
  tileLayer = L.tileLayer(config.tileUrl, { maxZoom: config.maxZoom || 19, attribution: attribution,
                                            crossOrigin: false });
  tileLayer.on('tileerror', function () { tileErrors++; updateBanner(); });
  tileLayer.on('tileload', function () { tileLoads++; lastTileOk = Date.now(); updateBanner(); });
  tileLayer.addTo(map);
  photoLayer.addTo(map);
  videoLayer.addTo(map);
  highlightLayer.addTo(map);
  addLegend();
  setTheme(config.theme || 'light');
  if (config.bbox) fitBbox(config.bbox, false); else map.setView([52.0, 19.0], 6);

  map.on('moveend', scheduleView);
  map.on('click', function (e) { pick(e.latlng); });
  scheduleView();
}

function addLegend() {
  const Legend = L.Control.extend({
    options: { position: 'bottomleft' },
    onAdd: function () {
      const box = L.DomUtil.create('div', 'legend');
      L.DomEvent.disableClickPropagation(box);
      const title = document.createElement('div');
      title.className = 'title';
      title.textContent = 'Legenda';
      box.appendChild(title);
      box.appendChild(legendRow('photo', 'Zdjęcie (miejsce wykonania)'));
      box.appendChild(legendRow('start', 'Film – punkt startowy'));
      box.appendChild(legendRow('track', 'Film – trasa lotu z SRT'));
      box.appendChild(legendRow('highlight', 'Wybrany materiał'));
      return box;
    },
  });
  map.addControl(new Legend());
}

function legendRow(kind, label) {
  const row = document.createElement('div');
  row.className = 'row';
  const ns = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(ns, 'svg');
  svg.setAttribute('width', '22');
  svg.setAttribute('height', '14');
  svg.dataset.kind = kind;
  row.appendChild(svg);
  const text = document.createElement('span');
  text.textContent = label;
  row.appendChild(text);
  return row;
}

function drawLegendSymbols() {
  const ns = 'http://www.w3.org/2000/svg';
  document.querySelectorAll('.legend svg').forEach(function (svg) {
    while (svg.firstChild) svg.removeChild(svg.firstChild);
    const kind = svg.dataset.kind;
    if (kind === 'track' || kind === 'highlight') {
      const l = document.createElementNS(ns, 'path');
      l.setAttribute('d', 'M1 11 L8 4 L14 9 L21 2');
      l.setAttribute('fill', 'none');
      l.setAttribute('stroke', kind === 'track' ? colors.track : colors.highlight);
      l.setAttribute('stroke-width', kind === 'track' ? '2.5' : '5');
      svg.appendChild(l);
    } else {
      const c = document.createElementNS(ns, 'circle');
      c.setAttribute('cx', '11');
      c.setAttribute('cy', '7');
      c.setAttribute('r', kind === 'photo' ? '4' : '6');
      c.setAttribute('fill', kind === 'photo' ? colors.photoFill : colors.startFill);
      c.setAttribute('stroke', kind === 'photo' ? colors.photo : colors.start);
      c.setAttribute('stroke-width', kind === 'photo' ? '1' : '2.5');
      svg.appendChild(c);
    }
  });
}

// ── Styl ────────────────────────────────────────────────────

function photoStyle() {
  return { renderer: renderer, radius: 4, weight: 1, color: colors.photo, fillColor: colors.photoFill,
           fillOpacity: 0.9, bubblingMouseEvents: false };
}
function startStyle() {
  return { renderer: renderer, radius: 6, weight: 2.5, color: colors.start, fillColor: colors.startFill,
           fillOpacity: 1, bubblingMouseEvents: false };
}
function lineStyle() {
  return { renderer: renderer, color: colors.track, weight: 2.5, opacity: 0.85, smoothFactor: 1.2,
           bubblingMouseEvents: false };
}

function setTheme(name) {
  theme = name === 'dark' ? 'dark' : 'light';
  colors = THEMES[theme];
  document.body.classList.toggle('dark', theme === 'dark');
  photos.forEach(function (m) { m.setStyle(photoStyle()); });
  starts.forEach(function (m) { m.setStyle(startStyle()); });
  lines.forEach(function (l) { l.setStyle(lineStyle()); });
  drawLegendSymbols();
  if (selected) select(selected, false);
}

// ── Dane widoku ─────────────────────────────────────────────

function scheduleView() {
  clearTimeout(viewTimer);
  viewTimer = setTimeout(function () {
    const b = map.getBounds();
    const size = map.getSize();
    send('view', { s: b.getSouth(), w: b.getWest(), n: b.getNorth(), e: b.getEast(),
                   zoom: map.getZoom(), width: size.x, height: size.y });
  }, 120);
}

function pick(latlng) {
  send('pick', { lat: latlng.lat, lon: latlng.lng, zoom: map.getZoom() });
}

function onFeatureClick(e) {
  L.DomEvent.stop(e);
  pick(e.latlng);
}

function updateData(m) {
  // Zdjęcia
  const wantPhotos = new Set();
  for (const p of m.photos) {
    wantPhotos.add(p[0]);
    if (!photos.has(p[0])) {
      const mk = L.circleMarker([p[1], p[2]], photoStyle()).on('click', onFeatureClick);
      photos.set(p[0], mk);
      photoLayer.addLayer(mk);
    }
  }
  photos.forEach(function (mk, id) { if (!wantPhotos.has(id)) { photoLayer.removeLayer(mk); photos.delete(id); } });

  // Filmy: punkt startowy + trasa
  const wantStarts = new Set();
  wantTracks.clear();
  const missing = [];
  for (const v of m.videos) {
    wantStarts.add(v[0]);
    if (!starts.has(v[0])) {
      const mk = L.circleMarker([v[1], v[2]], startStyle()).on('click', onFeatureClick);
      starts.set(v[0], mk);
    }
    if (v[3]) {
      wantTracks.add(v[0]);
      if (trackCache.has(v[0])) drawTrack(v[0]); else missing.push(v[0]);
    }
  }
  starts.forEach(function (mk, id) { if (!wantStarts.has(id)) { videoLayer.removeLayer(mk); starts.delete(id); } });
  lines.forEach(function (l, id) { if (!wantTracks.has(id)) { videoLayer.removeLayer(l); lines.delete(id); } });
  // punkty startowe nad liniami
  starts.forEach(function (mk) { if (!videoLayer.hasLayer(mk)) videoLayer.addLayer(mk); else mk.bringToFront(); });
  if (missing.length) send('needTracks', { ids: missing });
}

function drawTrack(id) {
  if (lines.has(id) || !wantTracks.has(id)) return;
  const segs = trackCache.get(id);
  if (!segs || !segs.length) return;
  // LRU: odśwież pozycję
  trackCache.delete(id);
  trackCache.set(id, segs);
  const line = L.polyline(segs, lineStyle()).on('click', onFeatureClick);
  lines.set(id, line);
  videoLayer.addLayer(line);
  const mk = starts.get(id);
  if (mk) mk.bringToFront();
}

function addTracks(tracks) {
  for (const id in tracks) {
    trackCache.set(Number(id), tracks[id]);
  }
  if (trackCache.size > TRACK_CACHE_MAX) {
    // usuń najdawniej używane trasy, których nie ma aktualnie na mapie
    for (const key of Array.from(trackCache.keys())) {
      if (trackCache.size <= TRACK_CACHE_MAX) break;
      if (!lines.has(key)) trackCache.delete(key);
    }
  }
  for (const id in tracks) drawTrack(Number(id));
}

function setLayers(showPhotos, showVideos) {
  if (showPhotos) map.addLayer(photoLayer); else map.removeLayer(photoLayer);
  if (showVideos) map.addLayer(videoLayer); else map.removeLayer(videoLayer);
}

function reset() {
  photoLayer.clearLayers(); videoLayer.clearLayers(); highlightLayer.clearLayers();
  photos.clear(); starts.clear(); lines.clear(); trackCache.clear(); wantTracks.clear();
  selected = null;
  scheduleView();
}

// ── Wybór i widok ───────────────────────────────────────────

function select(m, fit) {
  selected = m;
  highlightLayer.clearLayers();
  if (!m || m.lat == null) return;
  if (m.segments && m.segments.length) {
    trackCache.set(m.id, m.segments);
    L.polyline(m.segments, { renderer: renderer, color: colors.ring, weight: 9, opacity: 0.9,
                             interactive: false }).addTo(highlightLayer);
    L.polyline(m.segments, { renderer: renderer, color: colors.highlight, weight: 5, opacity: 1,
                             interactive: false }).addTo(highlightLayer);
  }
  L.circleMarker([m.lat, m.lon], { renderer: renderer, radius: m.kind === 'wideo' ? 10 : 8, weight: 3,
                                   color: colors.highlight, fill: false, interactive: false }).addTo(highlightLayer);
  if (fit) {
    if (m.bbox && (m.bbox[0] !== m.bbox[2] || m.bbox[1] !== m.bbox[3])) fitBbox(m.bbox, true);
    else map.setView([m.lat, m.lon], Math.max(map.getZoom(), 17));
  }
}

function fitBbox(b, animate) {
  if (!b) return;
  if (b[0] === b[2] && b[1] === b[3]) { map.setView([b[0], b[1]], 16, { animate: animate }); return; }
  map.fitBounds([[b[0], b[1]], [b[2], b[3]]], { padding: [30, 30], maxZoom: 17, animate: animate });
}

// ── Status podkładu ─────────────────────────────────────────

function updateBanner() {
  const banner = document.getElementById('banner');
  const failing = tileErrors > 0 && (Date.now() - lastTileOk > 4000 || tileLoads === 0);
  if (failing) {
    banner.textContent = 'Podkład mapy niedostępny (brak połączenia lub odmowa serwera). ' +
                         'Materiały, filtry i podgląd działają normalnie.';
    banner.hidden = false;
  } else {
    banner.hidden = true;
  }
  send('tiles', { ok: !failing, errors: tileErrors, loads: tileLoads });
  if (!failing) tileErrors = 0;
}

// ── Komunikacja ─────────────────────────────────────────────

function onMessage(text) {
  let m;
  try { m = JSON.parse(text); } catch (err) { return; }
  switch (m.type) {
    case 'init': init(m); break;
    case 'data': updateData(m); break;
    case 'tracks': addTracks(m.tracks || {}); break;
    case 'select': select(m.material, !!m.fit); break;
    case 'fit': fitBbox(m.bbox, true); break;
    case 'layers': setLayers(!!m.photos, !!m.videos); break;
    case 'theme': setTheme(m.theme); break;
    case 'reset': reset(); break;
    case 'refresh': scheduleView(); break;
    default: break;
  }
}

new QWebChannel(qt.webChannelTransport, function (channel) {
  bridge = channel.objects.bridge;
  bridge.toJs.connect(onMessage);
  send('ready', {});
});
