/* PV-Inverter-Master — Standort-Karte (vendored Leaflet, no CDN) */
(function() {
    'use strict';

    var GERMANY_CENTER = [51.163, 10.447];
    var GERMANY_BOUNDS = [[47.27, 5.87], [55.06, 15.05]];

    // Two honest base layers — both chosen to sit well inside the dark theme.
    // The dark base uses the "nolabels" variant: no baked-in place names.
    // Country borders come from a separate GeoJSON overlay (see _addBorders)
    // so they stay visible on both bases and carry no text clutter.
    var BASE_LAYERS = {
        dunkel: {
            label: '🌙 Dunkel',
            url: 'https://{s}.basemaps.cartocdn.com/dark_nolabels/{z}/{x}/{y}{r}.png',
            opts: {
                maxZoom: 20,
                subdomains: 'abcd',
                attribution: '&copy; OpenStreetMap, &copy; CARTO'
            }
        },
        satellit: {
            label: '🛰️ Satellit',
            url: 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
            opts: {
                maxZoom: 19,
                attribution: 'Tiles &copy; Esri — World Imagery'
            }
        }
    };
    var BASE_ORDER = ['dunkel', 'satellit'];

    // Country borders as a thin line overlay. johan/world.geo.json is a
    // long-standing low-detail country dataset; served here via the jsDelivr
    // CDN. Failure is non-fatal — the map just renders without borders.
    var BORDERS_URL = 'https://cdn.jsdelivr.net/gh/johan/world.geo.json@master/countries.geo.json';
    var bordersData = null;    // cached across re-mounts

    var map = null;
    var dots = {};             // location_id -> L.marker (circular dot)
    var chips = {};            // location_id -> { el, line, nameEl, powerEl, w, h, key }
    var labelOverlay = null;   // screen-space layer holding chips + leader svg
    var leaderSvg = null;
    var bordersLayer = null;
    var lastLocations = [];
    var initialFitDone = false;
    var currentBase = 'dunkel';
    var baseLayer = null;
    var radarLayer = null;
    var radarOn = false;

    // --- Label column tuning --------------------------------------------
    // Every Standort label sits right-aligned in a single vertical column;
    // a thin leader line connects it to its dot. Labels are ordered by the
    // dot's screen-y and spread to sit near it, then a swap pass removes
    // any leader crossings.
    var EDGE_MARGIN = 8;       // gap from the map edge
    var COL_GAP = 6;           // vertical gap between stacked labels

    function _placeable(loc) {
        return loc && loc.inverter_count >= 1 && loc.lat != null && loc.lon != null;
    }

    function _live(loc) {
        return loc.position_source === 'mqtt';
    }

    function _dotIcon(loc) {
        return L.divIcon({
            className: 've-map-dot-icon',
            html: '<div class="ve-map-dot' + (_live(loc) ? ' ve-map-dot--live' : '') + '"></div>',
            iconSize: [14, 14],
            iconAnchor: [7, 7]
        });
    }

    function _chipKey(loc) {
        var pw = (loc.total_power_w || 0) > 0 ? formatW(loc.total_power_w) : '';
        return (loc.name || '') + '|' + pw + '|' + loc.position_source;
    }

    // --- Geometry helpers ------------------------------------------------
    function _ccw(ax, ay, bx, by, cx, cy) {
        return (cy - ay) * (bx - ax) > (by - ay) * (cx - ax);
    }

    // Do segments a-b and c-d cross? (endpoints are {x,y})
    function _segCross(a, b, c, d) {
        return _ccw(a.x, a.y, c.x, c.y, d.x, d.y) !== _ccw(b.x, b.y, c.x, c.y, d.x, d.y) &&
               _ccw(a.x, a.y, b.x, b.y, c.x, c.y) !== _ccw(a.x, a.y, b.x, b.y, d.x, d.y);
    }

    function _setLine(line, x1, y1, x2, y2) {
        line.setAttribute('x1', x1);
        line.setAttribute('y1', y1);
        line.setAttribute('x2', x2);
        line.setAttribute('y2', y2);
    }

    // --- Country borders -------------------------------------------------
    function _addBorders() {
        if (!map) return;
        function draw(data) {
            if (!map || bordersLayer) return;
            bordersLayer = L.geoJSON(data, {
                interactive: false,
                style: function() {
                    return { className: 've-map-border', weight: 1, fill: false, opacity: 1 };
                }
            }).addTo(map);
        }
        if (bordersData) { draw(bordersData); return; }
        fetch(BORDERS_URL)
            .then(function(r) { return r.json(); })
            .then(function(d) { bordersData = d; draw(d); })
            .catch(function() { /* borders are optional polish */ });
    }

    // --- Marker rendering ------------------------------------------------
    function _ensureOverlay() {
        if (!map) return;
        if (labelOverlay && labelOverlay.parentNode) return;
        var cont = map.getContainer();
        labelOverlay = document.createElement('div');
        labelOverlay.className = 've-map-labels';
        leaderSvg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
        leaderSvg.setAttribute('class', 've-map-leaders');
        labelOverlay.appendChild(leaderSvg);
        cont.appendChild(labelOverlay);
        _sizeOverlay();
    }

    function _sizeOverlay() {
        if (!map || !leaderSvg) return;
        var s = map.getSize();
        leaderSvg.setAttribute('width', s.x);
        leaderSvg.setAttribute('height', s.y);
    }

    // Hovering a label enlarges its dot (and lights up the leader), so the
    // eye can jump from the list entry to the spot on the map.
    function _setDotHighlight(id, on) {
        var dot = dots[id];
        if (dot) {
            var el = dot.getElement();
            var inner = el && el.firstElementChild;
            if (inner) inner.classList.toggle('ve-map-dot--hl', on);
        }
        var chip = chips[id];
        if (chip) chip.line.classList.toggle('ve-map-leader--hl', on);
    }

    function _makeChip(loc) {
        var lid = loc.id;
        var el = document.createElement('div');
        var nameEl = document.createElement('span');
        nameEl.className = 've-map-label-name';
        var powerEl = document.createElement('span');
        powerEl.className = 've-map-label-power';
        el.appendChild(nameEl);
        el.appendChild(powerEl);
        el.addEventListener('click', function(e) {
            e.stopPropagation();
            window.location.hash = 'location/' + lid;
        });
        el.addEventListener('mouseenter', function() { _setDotHighlight(lid, true); });
        el.addEventListener('mouseleave', function() { _setDotHighlight(lid, false); });
        labelOverlay.appendChild(el);

        var line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
        line.setAttribute('class', 've-map-leader');
        leaderSvg.appendChild(line);

        return { el: el, line: line, nameEl: nameEl, powerEl: powerEl, w: 0, h: 0, key: '' };
    }

    function _updateChip(chip, loc) {
        var key = _chipKey(loc);
        if (chip.key === key) return;
        chip.nameEl.textContent = loc.name || 'Standort';
        // Power is shown only when the Standort is actually producing.
        if ((loc.total_power_w || 0) > 0) {
            chip.powerEl.textContent = formatW(loc.total_power_w);
            chip.powerEl.style.display = '';
        } else {
            chip.powerEl.textContent = '';
            chip.powerEl.style.display = 'none';
        }
        chip.el.className = 've-map-label' + (_live(loc) ? ' ve-map-label--live' : '');
        chip.key = key;
        // Re-measure: text changed, so the cached box size is stale.
        chip.w = chip.el.offsetWidth;
        chip.h = chip.el.offsetHeight;
    }

    // True only while a live, DOM-attached map exists. WebSocket location
    // pushes can arrive after the user has navigated away from the map.
    function _mapLive() {
        return !!map && document.body.contains(map.getContainer());
    }

    function _render() {
        if (!_mapLive()) return;
        _ensureOverlay();
        var seen = {};
        var count = 0;

        lastLocations.forEach(function(loc) {
            if (!_placeable(loc)) return;
            seen[loc.id] = true;
            count++;
            var ll = [loc.lat, loc.lon];

            // Dot — a Leaflet marker, so it pans/zooms with the map natively.
            var dot = dots[loc.id];
            if (dot) {
                dot.setLatLng(ll);
                var dEl = dot.getElement();
                var inner = dEl && dEl.firstElementChild;
                if (inner) {
                    inner.className = 've-map-dot' + (_live(loc) ? ' ve-map-dot--live' : '');
                } else {
                    dot.setIcon(_dotIcon(loc));
                }
            } else {
                dot = L.marker(ll, {
                    icon: _dotIcon(loc),
                    keyboard: false,
                    title: loc.name || 'Standort'
                });
                dot.on('click', (function(lid) {
                    return function() { window.location.hash = 'location/' + lid; };
                })(loc.id));
                dot.addTo(map);
                dots[loc.id] = dot;
            }

            // Info chip — laid out into the right-hand column by _layoutLabels.
            var chip = chips[loc.id];
            if (!chip) {
                chip = _makeChip(loc);
                chips[loc.id] = chip;
            }
            _updateChip(chip, loc);
        });

        Object.keys(dots).forEach(function(id) {
            if (!seen[id]) { map.removeLayer(dots[id]); delete dots[id]; }
        });
        Object.keys(chips).forEach(function(id) {
            if (!seen[id]) {
                var c = chips[id];
                if (c.el.parentNode) c.el.parentNode.removeChild(c.el);
                if (c.line.parentNode) c.line.parentNode.removeChild(c.line);
                delete chips[id];
            }
        });

        var empty = document.querySelector('.ve-map-empty');
        if (empty) empty.style.display = count ? 'none' : '';

        // One-shot fit; afterwards the view belongs to the user.
        if (!initialFitDone && count > 0) {
            initialFitDone = true;
            _fitMarkers();
        }
        _layoutLabels();
    }

    // Stack every chip right-aligned in one vertical column, each tied to
    // its dot by a thin leader. Labels are ordered by dot screen-y and
    // spread to sit near it; a swap pass then removes leader crossings.
    function _layoutLabels() {
        if (!labelOverlay || !_mapLive()) return;
        var size = map.getSize();
        var W = size.x, H = size.y;
        var colRight = W - EDGE_MARGIN;

        var visible = [];
        lastLocations.forEach(function(loc) {
            if (!_placeable(loc)) return;
            var chip = chips[loc.id];
            if (!chip) return;
            var p = map.latLngToContainerPoint([loc.lat, loc.lon]);
            // Dot off-screen — drop its chip + leader from the column.
            if (p.x < -30 || p.x > W + 30 || p.y < -30 || p.y > H + 30) {
                chip.el.style.display = 'none';
                chip.line.style.display = 'none';
                return;
            }
            visible.push({ id: loc.id, chip: chip, p: p });
        });
        visible.sort(function(a, b) {
            var d = a.p.y - b.p.y;
            return d !== 0 ? d : (a.id < b.id ? -1 : 1);
        });

        // 1D label spreading: place near the dot's y, no overlap, in order.
        function place(order) {
            var n = order.length, res = [], i;
            for (i = 0; i < n; i++) {
                var chip = order[i].chip;
                res.push({
                    it: order[i],
                    w: chip.w || chip.el.offsetWidth,
                    h: chip.h || chip.el.offsetHeight,
                    y: 0
                });
            }
            // Top-down: desired y = centred on the dot, pushed down on overlap.
            var cursor = EDGE_MARGIN;
            for (i = 0; i < n; i++) {
                res[i].y = Math.max(order[i].p.y - res[i].h / 2, cursor);
                cursor = res[i].y + res[i].h + COL_GAP;
            }
            // Bottom-up: if the stack overflowed, push it back up.
            if (n) {
                var over = (res[n - 1].y + res[n - 1].h) - (H - EDGE_MARGIN);
                if (over > 0) {
                    cursor = H - EDGE_MARGIN;
                    for (i = n - 1; i >= 0; i--) {
                        res[i].y = Math.min(res[i].y, cursor - res[i].h);
                        cursor = res[i].y - COL_GAP;
                    }
                    if (res[0].y < EDGE_MARGIN) {
                        // More labels than fit — stack tight from the top.
                        cursor = EDGE_MARGIN;
                        for (i = 0; i < n; i++) { res[i].y = cursor; cursor += res[i].h + COL_GAP; }
                    }
                }
            }
            res.forEach(function(r) {
                r.x = colRight - r.w;
                r.attach = { x: r.x, y: r.y + r.h / 2 };
            });
            return res;
        }

        var order = visible.slice();
        var placed = place(order);

        // Crossing removal: swapping a crossing adjacent pair always uncrosses
        // it; iterate (capped) until the leader fan is clean.
        var cap = order.length * order.length + 1;
        for (var guard = 0; guard < cap; guard++) {
            var swapped = false;
            for (var i = 0; i < placed.length - 1; i++) {
                var A = placed[i], B = placed[i + 1];
                if (_segCross(A.it.p, A.attach, B.it.p, B.attach)) {
                    var t = order[i]; order[i] = order[i + 1]; order[i + 1] = t;
                    placed = place(order);
                    swapped = true;
                    break;
                }
            }
            if (!swapped) break;
        }

        placed.forEach(function(r) {
            var chip = r.it.chip;
            chip.el.style.display = '';
            chip.line.style.display = '';
            chip.el.style.left = r.x + 'px';
            chip.el.style.top = r.y + 'px';
            _setLine(chip.line, r.it.p.x, r.it.p.y, r.attach.x, r.attach.y);
        });
    }

    function _fitMarkers() {
        if (!map) return;
        var pts = [];
        for (var id in dots) {
            if (dots.hasOwnProperty(id)) pts.push(dots[id].getLatLng());
        }
        if (pts.length === 0) map.fitBounds(GERMANY_BOUNDS);
        else if (pts.length === 1) map.setView(pts[0], 13);
        else map.fitBounds(L.latLngBounds(pts).pad(0.25), { maxZoom: 14 });
    }

    function _syncToolbar() {
        BASE_ORDER.forEach(function(name) {
            var btn = document.querySelector('.ve-map-base-btn[data-base="' + name + '"]');
            if (btn) btn.classList.toggle('ve-btn--primary', name === currentBase);
        });
        var rbtn = document.querySelector('.ve-map-radar-btn');
        if (rbtn) rbtn.classList.toggle('ve-btn--primary', radarOn);
    }

    function _setBase(name) {
        if (!map || !BASE_LAYERS[name]) return;
        var b = BASE_LAYERS[name];
        var previous = baseLayer;
        baseLayer = L.tileLayer(b.url, b.opts).addTo(map);
        baseLayer.bringToBack();
        currentBase = name;

        // Cross-fade: keep the old base alive until the new tiles have
        // painted, so switching never flashes the empty container.
        if (previous) {
            var dropPrevious = function() {
                if (map && previous) {
                    try { map.removeLayer(previous); } catch (e) { /* gone */ }
                    previous = null;
                }
            };
            baseLayer.once('load', function() { setTimeout(dropPrevious, 240); });
            setTimeout(dropPrevious, 2000);  // safety net if 'load' never fires
        }

        if (radarLayer) radarLayer.bringToFront();
        _syncToolbar();
    }

    function _setRadar(on) {
        radarOn = on;
        if (!on) {
            if (radarLayer) { map.removeLayer(radarLayer); radarLayer = null; }
            _syncToolbar();
            return;
        }
        // RainViewer publishes the current radar frame paths via a small
        // free, key-less JSON endpoint; tiles are then plain XYZ.
        fetch('https://api.rainviewer.com/public/weather-maps.json')
            .then(function(r) { return r.json(); })
            .then(function(d) {
                var frames = (d.radar && d.radar.past) || [];
                if (!frames.length) throw new Error('no radar frames');
                var path = frames[frames.length - 1].path;
                // RainViewer (post-2025 transition): radar tiles only exist up
                // to zoom 7, and "Universal Blue" (colour code 2) is the only
                // surviving scheme. maxNativeZoom lets Leaflet upscale the z7
                // tile for closer views instead of requesting an unsupported
                // level (which returns a "zoom level not supported" tile).
                var url = (d.host || 'https://tilecache.rainviewer.com') + path +
                    '/256/{z}/{x}/{y}/2/1_1.png';
                if (radarLayer) map.removeLayer(radarLayer);
                radarLayer = L.tileLayer(url, {
                    opacity: 0.7,
                    maxNativeZoom: 7,
                    maxZoom: 20,
                    attribution: 'Radar &copy; RainViewer'
                }).addTo(map);
                radarLayer.bringToFront();
            })
            .catch(function() {
                radarOn = false;
                _syncToolbar();
                if (window.showToast) showToast('Regenradar nicht verfügbar', 'warning');
            });
        _syncToolbar();
    }

    function _mount() {
        // Fresh Leaflet instance each visit — the container div is
        // recreated on every navigation.
        if (map) { map.remove(); map = null; }
        dots = {};
        chips = {};
        labelOverlay = null;
        leaderSvg = null;
        bordersLayer = null;
        initialFitDone = false;
        baseLayer = null;
        radarLayer = null;
        radarOn = false;
        map = L.map('ve-map', {
            zoomControl: true,
            maxZoom: 19,
            wheelPxPerZoomLevel: 110   // gentler, less jumpy wheel zoom
        });
        map.setView(GERMANY_CENTER, 6);
        _setBase(currentBase);
        _addBorders();
        _syncToolbar();

        // Keep the column glued to the dots through every view change.
        // Chips are hidden during the zoom animation, then re-laid-out once
        // it settles — that avoids them sliding across mid-animation.
        map.on('move zoom viewreset moveend zoomend', _layoutLabels);
        map.on('resize', function() { _sizeOverlay(); _layoutLabels(); });
        map.on('zoomstart', function() {
            if (labelOverlay) labelOverlay.classList.add('ve-map-labels--busy');
        });
        map.on('zoomend', function() {
            if (labelOverlay) labelOverlay.classList.remove('ve-map-labels--busy');
        });

        _render();
        fetch('/api/locations')
            .then(function(r) { return r.json(); })
            .then(function(d) { lastLocations = d.locations || []; _render(); })
            .catch(function() {});
        setTimeout(function() { if (map) { map.invalidateSize(); _sizeOverlay(); _layoutLabels(); } }, 150);
    }

    window.renderMapPage = function(content) {
        if (!content) return;
        var baseButtons = BASE_ORDER.map(function(name) {
            return '<button class="ve-btn ve-btn--sm ve-map-base-btn" data-base="' + name + '">' +
                BASE_LAYERS[name].label + '</button>';
        }).join('');
        content.innerHTML =
            '<div class="ve-map-page">' +
            '  <div class="ve-map-toolbar">' +
            '    <h1>Karte</h1>' +
            '    <div class="ve-map-toolbar-btns">' +
            '      <div class="ve-map-btn-group" role="group" aria-label="Basiskarte">' +
                     baseButtons +
            '      </div>' +
            '      <div class="ve-map-btn-group" role="group" aria-label="Overlays">' +
            '        <button class="ve-btn ve-btn--sm ve-map-radar-btn">🌧️ Regen</button>' +
            '      </div>' +
            '      <div class="ve-map-btn-group">' +
            '        <button class="ve-btn ve-btn--sm ve-map-fit-btn">Alle Standorte</button>' +
            '        <button class="ve-btn ve-btn--sm ve-map-de-btn">Deutschland</button>' +
            '      </div>' +
            '    </div>' +
            '  </div>' +
            '  <div class="ve-map-shell">' +
            '    <div id="ve-map" class="ve-map"></div>' +
            '    <div class="ve-map-empty">Keine Standorte mit Koordinaten und mindestens einem zugeordneten Inverter.</div>' +
            '  </div>' +
            '</div>';
        content.querySelectorAll('.ve-map-base-btn').forEach(function(btn) {
            btn.addEventListener('click', function() { _setBase(btn.getAttribute('data-base')); });
        });
        content.querySelector('.ve-map-radar-btn').addEventListener('click', function() {
            _setRadar(!radarOn);
        });
        content.querySelector('.ve-map-fit-btn').addEventListener('click', _fitMarkers);
        content.querySelector('.ve-map-de-btn').addEventListener('click', function() {
            if (map) map.fitBounds(GERMANY_BOUNDS);
        });
        _mount();
    };

    // Hooks called by app.js when WebSocket location data arrives.
    // Both only update marker data — they never move the map view.
    window.veMap = {
        onLocationList: function(locations) {
            lastLocations = locations || [];
            _render();
        },
        onLocationPosition: function(data) {
            for (var i = 0; i < lastLocations.length; i++) {
                if (lastLocations[i].id === data.location_id) {
                    lastLocations[i].lat = data.lat;
                    lastLocations[i].lon = data.lon;
                    lastLocations[i].position_source = 'mqtt';
                    break;
                }
            }
            _render();
        }
    };
})();
