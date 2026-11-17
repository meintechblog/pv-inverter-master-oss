// ===== Locations / Standorte =====

function _locationById(id) {
    for (var i = 0; i < _locations.length; i++) {
        if (_locations[i].id === id) return _locations[i];
    }
    return null;
}

function _isLocationsActive() {
    return (window.location.hash || '').replace('#', '').split('/')[0] === 'locations';
}

// Coarse "time ago" — intentionally not second-granular so a live status
// indicator doesn't flicker restlessly.
function _coarseAgo(ts) {
    if (!ts) return null;
    var s = Math.floor(Date.now() / 1000 - ts);
    if (s < 0) s = 0;
    if (s < 90) return 'gerade eben';
    if (s < 3600) return 'vor ' + Math.round(s / 60) + ' Min';
    if (s < 86400) return 'vor ' + Math.round(s / 3600) + ' Std';
    var d = Math.round(s / 86400);
    return 'vor ' + d + (d === 1 ? ' Tag' : ' Tagen');
}

// Refresh the MQTT live-contact indicator on the Standort form from the
// latest cached location data. Returns false once the panel is gone.
function _updateLocLiveStatus(panel) {
    var statusEl = panel.querySelector('.ve-loc-live-status');
    if (!statusEl) return false;
    var dot = statusEl.querySelector('.ve-loc-live-dot');
    var txt = statusEl.querySelector('.ve-loc-live-text');
    var loc = _locationById(statusEl.getAttribute('data-loc-id'));
    dot.className = 've-loc-live-dot';
    if (!loc) {
        txt.textContent = 'Status nach dem Speichern verfügbar';
    } else if (!loc.mqtt_last_ts) {
        txt.textContent = 'Noch keine MQTT-Position empfangen';
    } else if (loc.mqtt_stale) {
        dot.classList.add('ve-loc-live-dot--stale');
        txt.textContent = 'Letzter Kontakt ' + _coarseAgo(loc.mqtt_last_ts) +
            ' — Standardadresse aktiv';
    } else {
        dot.classList.add('ve-loc-live-dot--live');
        txt.textContent = 'Live · letzter Kontakt ' + _coarseAgo(loc.mqtt_last_ts);
    }
    return true;
}

// Static satellite image (ArcGIS World Imagery, no API key) centred on a
// coordinate — used as the Standort image when no photo was uploaded.
function _satelliteImageUrl(lat, lon, sizePx) {
    // Round to ~11 m so GPS jitter doesn't refetch the tile constantly.
    lat = Math.round(lat * 1e4) / 1e4;
    lon = Math.round(lon * 1e4) / 1e4;
    var R = 20037508.342789244;
    var x = lon * R / 180;
    var y = Math.log(Math.tan((90 + lat) * Math.PI / 360)) / (Math.PI / 180) * R / 180;
    var half = 220;  // metres → ~440 m square around the Standort
    var bbox = (x - half) + ',' + (y - half) + ',' + (x + half) + ',' + (y + half);
    return 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/export' +
        '?bbox=' + bbox + '&bboxSR=3857&imageSR=3857&size=' + sizePx + ',' + sizePx +
        '&format=jpg&f=image';
}

function handleLocationList(data) {
    _locations = (data && data.locations) || [];
    renderSidebar();
    // Refresh the management grid in place (no full page rebuild, no scroll jump)
    if (document.getElementById('ve-locations-grid')) _renderLocationsBody();
    if (window.veMap && window.veMap.onLocationList) window.veMap.onLocationList(_locations);
}

function handleLocationPosition(data) {
    var loc = _locationById(data.location_id);
    if (loc) {
        loc.lat = data.lat;
        loc.lon = data.lon;
        loc.position_source = 'mqtt';
        loc.position_ts = data.ts;
    }
    if (window.veMap && window.veMap.onLocationPosition) window.veMap.onLocationPosition(data);
}

// ----- Standorte sidebar group -----

function _locSidebarEntry(navId, label, hash, loc) {
    var a = document.createElement('a');
    a.className = 've-sidebar-device';
    a.setAttribute('data-device-id', navId);
    a.href = hash;
    var badge = '';
    if (loc) {
        badge = '<span class="ve-sidebar-device-power">' + (loc.inverter_count || 0) + '</span>';
    }
    var liveDot = (loc && loc.position_source === 'mqtt')
        ? '<span class="ve-dot" style="background:var(--ve-green)"></span>'
        : '';
    a.innerHTML = liveDot +
        '<span class="ve-sidebar-device-main"><span class="ve-sidebar-device-name">' + esc(label) + '</span></span>' +
        badge;
    a.addEventListener('click', function(e) {
        e.preventDefault();
        window.location.hash = hash.replace(/^#/, '');
        document.getElementById('sidebar').classList.remove('open');
        var ov = document.getElementById('sidebar-overlay');
        if (ov) ov.classList.remove('active');
    });
    return a;
}

function createLocationsSidebarGroup() {
    var group = document.createElement('div');
    group.className = 've-sidebar-group';

    var header = document.createElement('div');
    header.className = 've-sidebar-group-header';
    header.innerHTML = '<span>STANDORTE</span><span class="ve-sidebar-header-right">' +
        '<button class="ve-sidebar-add-btn" title="Standort hinzufügen">+</button>' +
        '<span class="ve-chevron">&#9660;</span></span>';
    var navCollapsed = !!_collapsedSidebarGroups['__locations_nav__'];
    function _applyNavCollapsed(isCollapsed) {
        var items = group.querySelector('.ve-sidebar-group-items');
        var chevron = header.querySelector('.ve-chevron');
        items.classList.toggle('ve-sidebar-group-items--collapsed', isCollapsed);
        chevron.classList.toggle('ve-chevron--collapsed', isCollapsed);
    }
    header.addEventListener('click', function(e) {
        if (e.target.closest('.ve-sidebar-add-btn')) return;
        navCollapsed = !navCollapsed;
        _collapsedSidebarGroups['__locations_nav__'] = navCollapsed;
        _applyNavCollapsed(navCollapsed);
    });
    group.appendChild(header);

    var addBtn = header.querySelector('.ve-sidebar-add-btn');
    addBtn.addEventListener('click', function(e) {
        e.stopPropagation();
        window.location.hash = 'location/new';
    });

    var items = document.createElement('div');
    items.className = 've-sidebar-group-items';
    items.appendChild(_locSidebarEntry('map', '🗺️  Karte', '#map'));
    items.appendChild(_locSidebarEntry('locations', 'Alle Standorte', '#locations'));
    for (var i = 0; i < _locations.length; i++) {
        var loc = _locations[i];
        items.appendChild(_locSidebarEntry('location:' + loc.id, loc.name || 'Standort', '#location/' + loc.id, loc));
    }
    group.appendChild(items);
    _applyNavCollapsed(navCollapsed);   // restore persisted collapse state
    return group;
}

// ----- Standorte management page -----

// Locations overview starts read-only ("ruhig"); the edit-mode toggle
// reveals the per-card editing controls. Module-level so it survives the
// location_list re-renders and page navigation.
var _locEditMode = false;

function _applyLocEditMode() {
    var page = document.querySelector('.ve-locations-page');
    if (page) page.classList.toggle('ve-loc-edit-on', _locEditMode);
    var btn = document.getElementById('ve-loc-edit-toggle');
    if (btn) {
        btn.textContent = _locEditMode ? 'Fertig' : 'Einträge bearbeiten';
        btn.classList.toggle('ve-btn--primary', _locEditMode);
    }
}

function renderLocationsPage(content) {
    if (!content) return;
    content.innerHTML =
        '<div class="ve-locations-page">' +
        '  <div class="ve-locations-header">' +
        '    <h1>Standorte</h1>' +
        '    <div class="ve-locations-header-actions">' +
        '      <button class="ve-btn ve-loc-edit-toggle" id="ve-loc-edit-toggle"></button>' +
        '      <button class="ve-btn ve-loc-import-btn" id="ve-loc-import-btn">Import</button>' +
        '      <input type="file" id="ve-loc-import-file" accept=".zip" style="display:none">' +
        '      <button class="ve-btn ve-btn--primary" id="ve-loc-add-btn">+ Standort hinzufügen</button>' +
        '    </div>' +
        '  </div>' +
        '  <p class="ve-overview-subtitle">Jeder Standort bündelt einen oder mehrere Inverter an einem Ort — mit Karte und optionalem Live-Tracking. Export/Import (ZIP) sichert einen Standort inkl. Bild und allen Invertern.</p>' +
        '  <div class="ve-locations-grid" id="ve-locations-grid"></div>' +
        '</div>';
    content.querySelector('#ve-loc-add-btn').addEventListener('click', function() {
        window.location.hash = 'location/new';
    });
    _wireLocationImport(content);
    content.querySelector('#ve-loc-edit-toggle').addEventListener('click', function() {
        _locEditMode = !_locEditMode;
        _applyLocEditMode();
    });
    _applyLocEditMode();
    _locCards = {};   // grid DOM was just recreated — drop stale card refs
    _renderLocationsBody();
}

// Wire the page-level "Import" button: pick a .zip and POST it to the
// whole-Standort import endpoint (creates a new Standort or updates one
// by id, restoring its image + all bundled inverters).
function _wireLocationImport(content) {
    var btn = content.querySelector('#ve-loc-import-btn');
    var file = content.querySelector('#ve-loc-import-file');
    if (!btn || !file) return;
    btn.addEventListener('click', function() { file.click(); });
    file.addEventListener('change', function() {
        var f = file.files && file.files[0];
        if (!f) return;
        showToast('Standort wird importiert…', 'info');
        var reader = new FileReader();
        reader.onload = function() {
            fetch('/api/locations/import', {
                method: 'POST',
                credentials: 'same-origin',
                headers: { 'Content-Type': 'application/zip' },
                body: reader.result
            })
            .then(function(r) { return r.json().then(function(j) { return { ok: r.ok, body: j }; }); })
            .then(function(res) {
                if (res.ok && res.body.success) {
                    var b = res.body;
                    showToast(
                        (b.is_new ? 'Standort importiert: ' : 'Standort aktualisiert: ') +
                        (b.location_name || b.location_id) +
                        ' (' + b.inverters_imported + ' Inverter' +
                        (b.inverters_skipped ? ', ' + b.inverters_skipped + ' übersprungen' : '') + ')',
                        'success'
                    );
                } else {
                    showToast('Import fehlgeschlagen: ' + ((res.body && res.body.error) || 'unbekannt'), 'error');
                }
            })
            .catch(function(e) { showToast('Netzwerkfehler: ' + e.message, 'error'); });
        };
        reader.readAsArrayBuffer(f);
        file.value = '';
    });
}

// Card elements are kept between renders and patched in place. A full
// innerHTML rebuild recreated every satellite thumbnail on each 5 s
// location_list broadcast, which made the overview flicker constantly.
var _locCards = {};

function _renderLocationsBody() {
    var grid = document.getElementById('ve-locations-grid');
    if (!grid) return;
    if (!_locations.length) {
        _locCards = {};
        grid.innerHTML =
            '<div class="ve-hint-card"><div class="ve-hint-header">Noch keine Standorte</div>' +
            '<p class="ve-hint-subtext">Lege deinen ersten Standort an, um Inverter einem Ort zuzuordnen.</p></div>';
        return;
    }
    // A leftover empty-state hint must go before cards can be appended.
    if (grid.querySelector('.ve-hint-card')) { grid.innerHTML = ''; _locCards = {}; }

    // Show country on cards only when Standorte span more than one country.
    var countries = {};
    _locations.forEach(function(l) {
        var c = (l.country || '').trim().toLowerCase();
        if (c) countries[c] = true;
    });
    var showCountry = Object.keys(countries).length > 1;

    var seen = {};
    _locations.forEach(function(loc) {
        seen[loc.id] = true;
        var thumbUrl = _locThumbUrl(loc);
        var entry = _locCards[loc.id];
        if (!entry) {
            var card = document.createElement('div');
            card.className = 've-loc-card';
            card.setAttribute('data-loc-id', loc.id);
            card.innerHTML = _locThumbHtml(loc, thumbUrl) +
                '<div class="ve-loc-card-body">' + _locBodyHtml(loc, showCountry) + '</div>';
            grid.appendChild(card);
            entry = _locCards[loc.id] = { card: card, thumbUrl: thumbUrl };
            _wireLocThumb(card);
        } else {
            // Rebuild the thumbnail only when its image truly changed —
            // that recreation is what made the thumbnails flash.
            if (entry.thumbUrl !== thumbUrl) {
                var holder = document.createElement('div');
                holder.innerHTML = _locThumbHtml(loc, thumbUrl);
                var oldThumb = entry.card.querySelector('.ve-loc-card-thumb');
                if (oldThumb && holder.firstChild) {
                    entry.card.replaceChild(holder.firstChild, oldThumb);
                }
                entry.thumbUrl = thumbUrl;
                _wireLocThumb(entry.card);
            }
            entry.card.querySelector('.ve-loc-card-body').innerHTML =
                _locBodyHtml(loc, showCountry);
            grid.appendChild(entry.card);   // keep DOM order aligned to _locations
        }
        _wireLocBody(_locCards[loc.id].card);
    });

    Object.keys(_locCards).forEach(function(id) {
        if (!seen[id]) {
            var e = _locCards[id];
            if (e.card.parentNode) e.card.parentNode.removeChild(e.card);
            delete _locCards[id];
        }
    });
}

function _inverterDevices() {
    return _devices.filter(function(d) {
        return ['venus', 'virtual', 'mqtt_pub', 'influx'].indexOf(d.type) === -1;
    });
}

function _assignInverterToLocation(invId, locId) {
    fetch('/api/devices/' + invId, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ location_id: locId })
    })
        .then(function(r) { return r.json(); })
        .then(function(d) {
            if (d.error) showToast('Fehler: ' + d.error, 'error');
            else showToast(locId ? 'Anlage zugeordnet' : 'Anlage entfernt', 'success');
        })
        .catch(function(e) { showToast('Fehler: ' + e.message, 'error'); });
}

function _locThumbUrl(loc) {
    // Image priority: uploaded photo → satellite view of the coords → none.
    if (loc.has_image) return '/api/locations/' + esc(loc.id) + '/image';
    if (loc.lat != null && loc.lon != null) return _satelliteImageUrl(loc.lat, loc.lon, 400);
    return '';
}

function _locThumbHtml(loc, thumbUrl) {
    return thumbUrl
        ? '<div class="ve-loc-card-thumb" data-edit="' + esc(loc.id) + '">' +
              '<div class="ve-loc-card-thumb-img" style="background-image:url(\'' + thumbUrl + '\')"></div>' +
          '</div>'
        : '<div class="ve-loc-card-thumb ve-loc-card-thumb--empty" data-edit="' + esc(loc.id) + '">📍</div>';
}

function _wireLocThumb(card) {
    var thumb = card.querySelector('.ve-loc-card-thumb[data-edit]');
    if (thumb) thumb.addEventListener('click', function() {
        window.location.hash = 'location/' + thumb.getAttribute('data-edit');
    });
}

function _wireLocBody(card) {
    var del = card.querySelector('.ve-loc-card-del');
    if (del) del.addEventListener('click', function() {
        _deleteLocation(del.getAttribute('data-loc-id'), del.getAttribute('data-loc-name'));
    });
    var toggle = card.querySelector('.ve-loc-card-toggle');
    if (toggle) toggle.addEventListener('click', function() {
        _toggleLocationEnabled(
            toggle.getAttribute('data-loc-id'),
            toggle.getAttribute('data-loc-name'),
            toggle.getAttribute('data-enabled') === '1'   // currently enabled?
        );
    });
    card.querySelectorAll('.ve-loc-inv-remove').forEach(function(btn) {
        btn.addEventListener('click', function() {
            _assignInverterToLocation(btn.getAttribute('data-inv'), '');
        });
    });
    var addSel = card.querySelector('.ve-loc-inv-add');
    if (addSel) addSel.addEventListener('change', function() {
        if (addSel.value) {
            _assignInverterToLocation(addSel.value, card.getAttribute('data-loc-id'));
        }
    });
}

function _locBodyHtml(loc, showCountry) {
    // For an MQTT-tracked Standort the Ort follows the live position
    // (reverse-geocoded server-side) — country only when it is abroad.
    // A static Standort shows its configured Ort, with the country added
    // only when sites span more than one.
    var addr;
    if (loc.position_source === 'mqtt' && loc.live_city) {
        addr = [loc.live_city, loc.live_country].filter(Boolean).join(', ');
    } else {
        addr = [loc.city, showCountry ? loc.country : null].filter(Boolean).join(', ');
    }
    var posBadge;
    if (loc.position_source === 'mqtt') {
        var liveAgo = _coarseAgo(loc.mqtt_last_ts);
        posBadge = '<span class="ve-loc-badge ve-loc-badge--live">Live' +
            (liveAgo ? ' · ' + liveAgo : '') + '</span>';
    } else if (loc.mqtt_stale) {
        posBadge = '<span class="ve-loc-badge ve-loc-badge--warn">MQTT veraltet → Adresse</span>';
    } else if (loc.position_source === 'static') {
        // A plain static Standort needs no badge — the card itself shows
        // it exists. Only the genuinely missing-coordinates case is flagged.
        posBadge = '';
    } else {
        posBadge = '<span class="ve-loc-badge ve-loc-badge--warn">Keine Koordinaten</span>';
    }

    // Assigned inverters — already ordered + string-group-expanded by the
    // backend. A "string_group" row is a named PV-string sub-unit of its
    // parent device; it is not independently removable.
    var assigned = (loc.inverters || []);
    // Each assigned unit is one row. A named string group is shown as its
    // own standalone Anlage; only real devices carry a remove button (a
    // string group follows its parent device), with an aligned spacer so
    // the power column stays flush.
    var invRows = assigned.length
        ? assigned.map(function(inv) {
            var trailing = inv.kind === 'string_group'
                ? '<span class="ve-loc-inv-remove-spacer"></span>'
                : '<button class="ve-loc-inv-remove" data-inv="' + esc(inv.id) + '" title="Vom Standort entfernen">&times;</button>';
            return '<div class="ve-loc-inv-row">' +
                '<span class="ve-loc-inv-name">' + esc(inv.name || inv.id) + '</span>' +
                '<span class="ve-loc-inv-power">' + formatW(inv.power_w || 0) + '</span>' +
                trailing +
                '</div>';
          }).join('')
        : '<div class="ve-loc-inv-empty">Noch keine Anlagen zugeordnet</div>';

    // Only devices not assigned to ANY Standort can be added — a device
    // belongs to exactly one Standort, so the picker is hidden entirely
    // once everything is assigned.
    var available = _inverterDevices().filter(function(d) { return !d.location_id; });
    var addControl = available.length
        ? '<select class="ve-input ve-loc-inv-add">' +
              '<option value="">+ Anlage zuordnen…</option>' +
              available.map(function(d) {
                  return '<option value="' + esc(d.id) + '">' + esc(d.name || d.id) + '</option>';
              }).join('') +
          '</select>'
        : '';

    return '<a class="ve-loc-card-name" href="#location/' + esc(loc.id) + '">' + esc(loc.name || 'Standort') + '</a>' +
        (addr
            ? '<div class="ve-loc-card-addr">' + esc(addr) + '</div>'
            : '<div class="ve-loc-card-addr ve-text-dim">Keine Adresse hinterlegt</div>') +
        '<div class="ve-loc-card-meta">' + posBadge +
        (loc.enabled === false ? '<span class="ve-loc-badge ve-loc-badge--warn">Deaktiviert</span>' : '') +
        '  <span class="ve-loc-card-power">' + formatW(loc.total_power_w || 0) + '</span>' +
        '</div>' +
        '<div class="ve-loc-inv-section">' +
        '  <div class="ve-loc-inv-list">' + invRows + '</div>' +
        '  ' + addControl +
        '</div>' +
        '<div class="ve-loc-card-foot">' +
        '  <a class="ve-btn ve-btn--sm ve-loc-card-edit" href="#location/' + esc(loc.id) + '">Bearbeiten</a>' +
        '  <a class="ve-btn ve-btn--sm ve-loc-card-export" href="/api/locations/' + esc(loc.id) + '/export" download>Export</a>' +
        '  <button class="ve-btn ve-btn--sm ve-loc-card-toggle" data-loc-id="' + esc(loc.id) + '" data-loc-name="' + esc(loc.name || '') + '" data-enabled="' + (loc.enabled === false ? '0' : '1') + '">' +
        (loc.enabled === false ? 'Aktivieren' : 'Deaktivieren') + '</button>' +
        '  <button class="ve-btn ve-btn--sm ve-btn--cancel ve-loc-card-del" data-loc-id="' + esc(loc.id) + '" data-loc-name="' + esc(loc.name || '') + '">Löschen</button>' +
        '</div>';
}

function _deleteLocation(id, name) {
    if (!window.confirm('Standort "' + (name || id) + '" wirklich löschen?\n\nZugeordnete Inverter werden vom Standort getrennt (nicht gelöscht).')) {
        return;
    }
    fetch('/api/locations/' + id, { method: 'DELETE' })
        .then(function(r) { return r.json(); })
        .then(function(d) {
            if (d.success) showToast('Standort gelöscht', 'success');
            else showToast(d.error || 'Löschen fehlgeschlagen', 'error');
        })
        .catch(function(e) { showToast('Fehler: ' + e.message, 'error'); });
}

function _toggleLocationEnabled(id, name, currentlyEnabled) {
    var next = !currentlyEnabled;
    if (!next) {
        if (!window.confirm('Standort "' + (name || id) + '" deaktivieren?\n\nAlle zugeordneten Inverter werden ebenfalls deaktiviert (Polling gestoppt).')) {
            return;
        }
    }
    fetch('/api/locations/' + id + '/enabled', {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled: next })
    })
        .then(function(r) { return r.json().then(function(j) { return { ok: r.ok, body: j }; }); })
        .then(function(res) {
            if (res.ok && res.body.success) {
                showToast(
                    'Standort ' + (next ? 'aktiviert' : 'deaktiviert') +
                    ' (' + res.body.inverters_affected + ' Inverter)',
                    'success'
                );
            } else {
                showToast((res.body && res.body.error) || 'Umschalten fehlgeschlagen', 'error');
            }
        })
        .catch(function(e) { showToast('Fehler: ' + e.message, 'error'); });
}

// ----- Standort detail / add-edit form -----

function renderLocationDetailPage(content, locId) {
    if (!content) return;
    if (locId === 'new') {
        content.innerHTML = '';
        buildLocationForm(content, null);
        return;
    }
    content.innerHTML =
        '<div class="ve-spinner-wrap"><div class="ve-spinner"></div><span class="ve-spinner-label">Lade Standort…</span></div>';
    fetch('/api/locations')
        .then(function(r) { return r.json(); })
        .then(function(data) {
            var loc = null;
            var list = data.locations || [];
            for (var i = 0; i < list.length; i++) {
                if (list[i].id === locId) { loc = list[i]; break; }
            }
            content.innerHTML = '';
            if (!loc) {
                content.innerHTML = '<div class="ve-hint-card"><div class="ve-hint-header">Standort nicht gefunden</div></div>';
                return;
            }
            buildLocationForm(content, loc);
        })
        .catch(function(e) {
            content.innerHTML = '<div class="ve-hint-card"><div class="ve-hint-header">Fehler</div><p class="ve-hint-subtext">' + esc(e.message) + '</p></div>';
        });
}

function _locMqttCardHtml(loc) {
    var topic = loc && loc.mqtt_topic ? loc.mqtt_topic : '';
    if (!topic) {
        return '<div class="ve-hint-card"><div class="ve-hint-header">MQTT-Topic nach dem Speichern verfügbar</div>' +
            '<p class="ve-hint-subtext">Speichere den Standort zuerst — danach erscheint hier das genaue Topic samt Beispiel-Payload.</p></div>';
    }
    return '<div class="ve-hint-card">' +
        '<div class="ve-hint-header">Live-Position per MQTT senden</div>' +
        '<p class="ve-hint-subtext">Veröffentliche die Position auf diesem Topic — Karte und Standortliste aktualisieren sich live:</p>' +
        '<div class="ve-loc-mqtt-row"><span class="ve-text-dim">Topic</span><code>' + esc(topic) + '</code></div>' +
        '<div class="ve-loc-mqtt-row"><span class="ve-text-dim">Payload</span><code>{"lat": 48.137154, "lon": 11.576124}</code></div>' +
        '</div>';
}

function _renderLocImageArea(area, loc, isNew) {
    if (isNew || !loc || !loc.id) {
        area.innerHTML = '<p class="ve-hint-subtext">Ein Bild kann hochgeladen werden, sobald der Standort gespeichert ist.</p>';
        return;
    }
    var hasImg = !!loc.has_image;
    var hasCoords = loc.lat != null && loc.lon != null;
    var thumb;
    if (hasImg) {
        thumb = '<div class="ve-loc-image-thumb" style="background-image:url(/api/locations/' + esc(loc.id) + '/image?t=' + Date.now() + ')"></div>';
    } else if (hasCoords) {
        thumb = '<div class="ve-loc-image-thumb" style="background-image:url(\'' + _satelliteImageUrl(loc.lat, loc.lon, 400) + '\')" title="Satellitenansicht des Standorts"></div>';
    } else {
        thumb = '<div class="ve-loc-image-thumb ve-loc-image-thumb--empty">Kein Bild</div>';
    }
    area.innerHTML = thumb +
        '<div class="ve-loc-image-actions">' +
        '  <button type="button" class="ve-btn ve-btn--sm ve-btn--primary ve-loc-img-upload">' + (hasImg ? 'Bild ändern' : 'Bild hochladen') + '</button>' +
        (hasImg ? '  <button type="button" class="ve-btn ve-btn--sm ve-btn--cancel ve-loc-img-remove">Entfernen</button>' : '') +
        '  <input type="file" accept="image/png,image/jpeg,image/webp,image/gif" class="ve-loc-img-input" style="display:none">' +
        (!hasImg && hasCoords
            ? '<p class="ve-hint-subtext">Automatische Satellitenansicht — lade ein eigenes Bild hoch, um sie zu ersetzen.</p>'
            : '') +
        '</div>';
    var fileInput = area.querySelector('.ve-loc-img-input');
    var thumbEl = area.querySelector('.ve-loc-image-thumb');

    function uploadImage(file) {
        if (!file) return;
        if (file.type && file.type.indexOf('image/') !== 0) {
            showToast('Bitte eine Bilddatei ablegen', 'error');
            return;
        }
        var fd = new FormData();
        fd.append('image', file);
        fetch('/api/locations/' + loc.id + '/image', { method: 'POST', body: fd })
            .then(function(r) { return r.json(); })
            .then(function(d) {
                if (d.success) {
                    showToast('Bild hochgeladen', 'success');
                    loc.has_image = true;
                    _renderLocImageArea(area, loc, false);
                } else {
                    showToast(d.error || 'Upload fehlgeschlagen', 'error');
                }
            })
            .catch(function(e) { showToast('Fehler: ' + e.message, 'error'); });
    }

    area.querySelector('.ve-loc-img-upload').addEventListener('click', function() { fileInput.click(); });
    fileInput.addEventListener('change', function() { uploadImage(fileInput.files[0]); });

    // Drag & drop onto the thumbnail — same upload path as the file picker.
    if (thumbEl) {
        thumbEl.addEventListener('dragover', function(e) {
            e.preventDefault();
            thumbEl.classList.add('ve-loc-image-thumb--dragover');
        });
        thumbEl.addEventListener('dragleave', function() {
            thumbEl.classList.remove('ve-loc-image-thumb--dragover');
        });
        thumbEl.addEventListener('drop', function(e) {
            e.preventDefault();
            thumbEl.classList.remove('ve-loc-image-thumb--dragover');
            var dt = e.dataTransfer;
            if (dt && dt.files && dt.files[0]) uploadImage(dt.files[0]);
        });
    }

    var rm = area.querySelector('.ve-loc-img-remove');
    if (rm) rm.addEventListener('click', function() {
        fetch('/api/locations/' + loc.id + '/image', { method: 'DELETE' })
            .then(function(r) { return r.json(); })
            .then(function(d) {
                if (d.success) {
                    showToast('Bild entfernt', 'success');
                    loc.has_image = false;
                    _renderLocImageArea(area, loc, false);
                } else {
                    showToast(d.error || 'Fehler', 'error');
                }
            })
            .catch(function(e) { showToast('Fehler: ' + e.message, 'error'); });
    });
}

function buildLocationForm(content, loc) {
    var isNew = !loc;
    loc = loc || {};
    var panel = document.createElement('div');
    panel.className = 've-panel ve-loc-form';
    var lat = (loc.config_lat != null) ? loc.config_lat : '';
    var lon = (loc.config_lon != null) ? loc.config_lon : '';

    var fbDays = (loc.mqtt_fallback_days != null) ? loc.mqtt_fallback_days : 7;
    var fbOpts = [1, 2, 3, 7, 14, 30].map(function(d) {
        return '<option value="' + d + '"' + (fbDays === d ? ' selected' : '') + '>' +
            d + (d === 1 ? ' Tag' : ' Tage') + (d === 7 ? ' (Standard)' : '') + '</option>';
    }).join('') +
        '<option value="0"' + (fbDays === 0 ? ' selected' : '') + '>Unendlich – nie zurückswitchen</option>';

    panel.innerHTML =
        '<div class="ve-panel-header">' +
        '  <h2>' + (isNew ? 'Neuer Standort' : 'Standort bearbeiten') + '</h2>' +
        '  <span class="ve-panel-header-actions">' +
        '    <button type="button" class="ve-btn ve-btn--sm ve-loc-close">← Alle Standorte</button>' +
        '    <span class="ve-btn-pair ve-loc-save-pair"' + (isNew ? '' : ' style="display:none"') + '>' +
        '      <button type="button" class="ve-btn ve-btn--sm ve-btn--cancel ve-loc-cancel">Abbrechen</button>' +
        '      <button type="button" class="ve-btn ve-btn--sm ve-btn--save ve-loc-save">Speichern</button>' +
        '    </span>' +
        '  </span>' +
        '</div>' +
        '<div class="ve-form-group"><label>Name</label><input type="text" class="ve-input ve-loc-name" value="' + esc(loc.name || '') + '" placeholder="z.B. Zuhause oder Cottage (Wohnwagen)"></div>' +
        '<div class="ve-loc-section-title">Adresse</div>' +
        '<div class="ve-form-group"><label>Straße &amp; Hausnummer</label><input type="text" class="ve-input ve-loc-street" value="' + esc(loc.street || '') + '" placeholder="Marienplatz 1"></div>' +
        '<div class="ve-config-grid">' +
        '  <div class="ve-form-group"><label>PLZ</label><input type="text" class="ve-input ve-loc-postal" value="' + esc(loc.postal_code || '') + '" placeholder="80331"></div>' +
        '  <div class="ve-form-group"><label>Stadt</label><input type="text" class="ve-input ve-loc-city" value="' + esc(loc.city || '') + '" placeholder="München"></div>' +
        '</div>' +
        '<div class="ve-form-group"><label>Land</label><input type="text" class="ve-input ve-loc-country" value="' + esc(loc.country || '') + '" placeholder="Deutschland"></div>' +
        '<div class="ve-loc-section-title">Netzwerk</div>' +
        '<div class="ve-form-group"><label>Subnetz (für Geräte-Discovery)</label>' +
        '  <div class="ve-input-row">' +
        '    <input type="text" class="ve-input ve-loc-subnet" value="' + esc(loc.subnet || '') + '" placeholder="z.B. 203.0.113.0/24">' +
        '    <button type="button" class="ve-btn ve-btn--sm ve-loc-subnet-check">Prüfen</button>' +
        '  </div>' +
        '  <span class="ve-loc-subnet-result" style="display:none"></span>' +
        '  <span class="ve-form-hint" style="font-size:0.75rem;color:var(--ve-text-dim);margin-top:4px;display:block">' +
        '    Netz, in dem die Geräte dieses Standorts liegen. Beim Discover wird dieses Subnetz gescannt. ' +
        '    Leer = lokales Subnetz. „Prüfen" testet die Erreichbarkeit. ' +
        '    Formate: 203.0.113.0/24 · 203.0.113.x · 203.0.113.1' +
        '  </span>' +
        '</div>' +
        '<button type="button" class="ve-btn ve-btn--primary ve-loc-geocode">Geo-Koordinaten ermitteln</button>' +
        '<div class="ve-loc-geocode-hint ve-hint-card" style="display:none"></div>' +
        '<div class="ve-loc-section-title">Koordinaten</div>' +
        '<div class="ve-ess-row"><label>Koordinaten per MQTT (Live-Tracking)</label><label class="ve-toggle"><input type="checkbox" class="ve-loc-mqtt"' + (loc.mqtt_coords ? ' checked' : '') + '><span class="ve-toggle-track"></span></label></div>' +
        '<div class="ve-config-grid">' +
        '  <div class="ve-form-group"><label>Breitengrad (lat)</label><input type="number" step="any" class="ve-input ve-loc-lat" value="' + lat + '" placeholder="48.137154"></div>' +
        '  <div class="ve-form-group"><label>Längengrad (lon)</label><input type="number" step="any" class="ve-input ve-loc-lon" value="' + lon + '" placeholder="11.576124"></div>' +
        '</div>' +
        '<div class="ve-loc-sat-preview" style="display:none">' +
        '  <div class="ve-loc-sat-thumb"></div>' +
        '  <span class="ve-loc-sat-caption">Satellitenansicht des Standorts</span>' +
        '</div>' +
        '<div class="ve-loc-mqtt-section"' + (loc.mqtt_coords ? '' : ' style="display:none"') + '>' +
        '  <div class="ve-loc-live-status" data-loc-id="' + esc(loc.id || '') + '">' +
        '    <span class="ve-loc-live-dot"></span>' +
        '    <span class="ve-loc-live-text">…</span>' +
        '  </div>' +
        '  <div class="ve-form-group">' +
        '    <label>Zurück zur Standardadresse, wenn keine MQTT-Position eingeht</label>' +
        '    <select class="ve-input ve-loc-fallback">' + fbOpts + '</select>' +
        '  </div>' +
        '  <div class="ve-loc-mqtt-card">' + _locMqttCardHtml(loc) + '</div>' +
        '</div>' +
        '<div class="ve-loc-section-title">Bild</div>' +
        '<div class="ve-loc-image-area"></div>' +
        (isNew ? '' :
            '<div class="ve-panel-danger-section">' +
            '  <button type="button" class="ve-btn ve-btn--danger ve-loc-delete">Standort löschen</button>' +
            '</div>');
    content.appendChild(panel);

    _renderLocImageArea(panel.querySelector('.ve-loc-image-area'), loc, isNew);

    // Satellite preview of the coordinates — a standard element of the detail
    // page, kept in sync with the lat/lon inputs and geocoding results.
    var satPreview = panel.querySelector('.ve-loc-sat-preview');
    var satThumb = satPreview.querySelector('.ve-loc-sat-thumb');
    var latInput = panel.querySelector('.ve-loc-lat');
    var lonInput = panel.querySelector('.ve-loc-lon');
    function _updateSatPreview() {
        var la = parseFloat(latInput.value);
        var lo = parseFloat(lonInput.value);
        if (isFinite(la) && isFinite(lo)) {
            satThumb.style.backgroundImage = "url('" + _satelliteImageUrl(la, lo, 240) + "')";
            satPreview.style.display = '';
        } else {
            satPreview.style.display = 'none';
        }
    }
    _updateSatPreview();
    latInput.addEventListener('change', _updateSatPreview);
    lonInput.addEventListener('change', _updateSatPreview);

    // MQTT toggle shows/hides the whole live-tracking section
    var mqttToggle = panel.querySelector('.ve-loc-mqtt');
    var mqttSection = panel.querySelector('.ve-loc-mqtt-section');
    mqttToggle.addEventListener('change', function() {
        mqttSection.style.display = mqttToggle.checked ? '' : 'none';
    });

    // Live MQTT-contact indicator — refreshed on a calm 10 s cadence.
    _updateLocLiveStatus(panel);
    if (_locLiveInterval) clearInterval(_locLiveInterval);
    _locLiveInterval = setInterval(function() {
        if (!_updateLocLiveStatus(panel) || !document.body.contains(panel)) {
            clearInterval(_locLiveInterval);
            _locLiveInterval = null;
        }
    }, 10000);

    // Geocoding
    var geocodeBtn = panel.querySelector('.ve-loc-geocode');
    var geocodeHint = panel.querySelector('.ve-loc-geocode-hint');
    geocodeBtn.addEventListener('click', function() {
        var body = {
            street: panel.querySelector('.ve-loc-street').value.trim(),
            postal_code: panel.querySelector('.ve-loc-postal').value.trim(),
            city: panel.querySelector('.ve-loc-city').value.trim(),
            country: panel.querySelector('.ve-loc-country').value.trim()
        };
        geocodeBtn.disabled = true;
        geocodeBtn.textContent = 'Suche…';
        fetch('/api/geocode', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body)
        })
            .then(function(r) { return r.json(); })
            .then(function(d) {
                geocodeBtn.disabled = false;
                geocodeBtn.textContent = 'Geo-Koordinaten ermitteln';
                if (d.success) {
                    panel.querySelector('.ve-loc-lat').value = d.lat;
                    panel.querySelector('.ve-loc-lon').value = d.lon;
                    _updateSatPreview();
                    checkLocDirty();
                    geocodeHint.style.display = 'block';
                    geocodeHint.className = 've-hint-card ve-hint-card--success ve-loc-geocode-hint';
                    geocodeHint.innerHTML = '<div class="ve-hint-header">Koordinaten gefunden</div>' +
                        '<p class="ve-hint-subtext">' + esc(d.display_name || '') + '</p>';
                } else {
                    geocodeHint.style.display = 'block';
                    geocodeHint.className = 've-hint-card ve-loc-geocode-hint';
                    geocodeHint.innerHTML = '<div class="ve-hint-header">' + esc(d.error || 'Nicht gefunden') + '</div>';
                }
            })
            .catch(function(e) {
                geocodeBtn.disabled = false;
                geocodeBtn.textContent = 'Geo-Koordinaten ermitteln';
                showToast('Geocoding-Fehler: ' + e.message, 'error');
            });
    });

    // Dirty tracking — Save/Cancel in the header appear only once a field
    // changed (edit mode); for a new Standort they stay visible.
    var locFields = {
        name: panel.querySelector('.ve-loc-name'),
        street: panel.querySelector('.ve-loc-street'),
        postal: panel.querySelector('.ve-loc-postal'),
        city: panel.querySelector('.ve-loc-city'),
        country: panel.querySelector('.ve-loc-country'),
        subnet: panel.querySelector('.ve-loc-subnet'),
        lat: panel.querySelector('.ve-loc-lat'),
        lon: panel.querySelector('.ve-loc-lon'),
        fallback: panel.querySelector('.ve-loc-fallback')
    };
    var locSavePair = panel.querySelector('.ve-loc-save-pair');
    function _locSnapshot() {
        var s = { mqtt: mqttToggle.checked };
        Object.keys(locFields).forEach(function(k) { s[k] = locFields[k].value; });
        return s;
    }
    var locOriginal = _locSnapshot();
    function checkLocDirty() {
        if (isNew) return;   // new Standort: save pair stays visible
        var now = _locSnapshot();
        var dirty = Object.keys(now).some(function(k) { return now[k] !== locOriginal[k]; });
        locSavePair.style.display = dirty ? '' : 'none';
        Object.keys(locFields).forEach(function(k) {
            if (locFields[k].value !== locOriginal[k]) locFields[k].classList.add('ve-input--dirty');
            else locFields[k].classList.remove('ve-input--dirty');
        });
    }
    Object.keys(locFields).forEach(function(k) {
        locFields[k].addEventListener('input', checkLocDirty);
        locFields[k].addEventListener('change', checkLocDirty);
    });
    mqttToggle.addEventListener('change', checkLocDirty);

    // Subnet reachability check
    var subnetCheckBtn = panel.querySelector('.ve-loc-subnet-check');
    var subnetResult = panel.querySelector('.ve-loc-subnet-result');
    subnetCheckBtn.addEventListener('click', function() {
        subnetCheckBtn.disabled = true;
        subnetCheckBtn.textContent = 'Prüfe…';
        subnetResult.style.display = 'none';
        fetch('/api/locations/check-subnet', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ subnet: locFields.subnet.value.trim() })
        })
            .then(function(r) { return r.json(); })
            .then(function(d) {
                subnetResult.style.display = 'block';
                if (!d.ok) {
                    subnetResult.className = 've-loc-subnet-result ve-loc-subnet-result--err';
                    subnetResult.textContent = '✗ ' + (d.error || 'Prüfung fehlgeschlagen');
                } else if (d.hosts_alive > 0) {
                    subnetResult.className = 've-loc-subnet-result ve-loc-subnet-result--ok';
                    subnetResult.textContent = '✓ ' + d.subnet + ' erreichbar — ' +
                        d.hosts_alive + ' von ' + d.hosts_scanned + ' Hosts antworten';
                } else {
                    subnetResult.className = 've-loc-subnet-result ve-loc-subnet-result--warn';
                    subnetResult.textContent = '⚠ Kein Host in ' + d.subnet +
                        ' hat geantwortet (' + d.hosts_scanned + ' geprüft) — nicht erreichbar oder leer';
                }
            })
            .catch(function(e) {
                subnetResult.style.display = 'block';
                subnetResult.className = 've-loc-subnet-result ve-loc-subnet-result--err';
                subnetResult.textContent = '✗ Fehler: ' + e.message;
            })
            .finally(function() {
                subnetCheckBtn.disabled = false;
                subnetCheckBtn.textContent = 'Prüfen';
            });
    });
    // A changed subnet makes the last check result stale.
    locFields.subnet.addEventListener('input', function() {
        subnetResult.style.display = 'none';
    });

    // Header back button always returns to the overview.
    panel.querySelector('.ve-loc-close').addEventListener('click', function() {
        window.location.hash = 'locations';
    });
    // Cancel: a new Standort has nothing to revert → leave; an edit
    // reverts every field to its saved value.
    panel.querySelector('.ve-loc-cancel').addEventListener('click', function() {
        if (isNew) { window.location.hash = 'locations'; return; }
        Object.keys(locFields).forEach(function(k) {
            locFields[k].value = locOriginal[k];
        });
        mqttToggle.checked = locOriginal.mqtt;
        mqttSection.style.display = mqttToggle.checked ? '' : 'none';
        _updateSatPreview();
        checkLocDirty();
    });

    // Delete (edit mode only)
    var delBtn = panel.querySelector('.ve-loc-delete');
    if (delBtn) delBtn.addEventListener('click', function() {
        if (!window.confirm('Standort "' + (loc.name || '') + '" wirklich löschen?\n\nZugeordnete Inverter werden vom Standort getrennt.')) return;
        fetch('/api/locations/' + loc.id, { method: 'DELETE' })
            .then(function(r) { return r.json(); })
            .then(function(d) {
                if (d.success) { showToast('Standort gelöscht', 'success'); window.location.hash = 'locations'; }
                else showToast(d.error || 'Löschen fehlgeschlagen', 'error');
            })
            .catch(function(e) { showToast('Fehler: ' + e.message, 'error'); });
    });

    // Save
    var saveBtn = panel.querySelector('.ve-loc-save');
    saveBtn.addEventListener('click', function() {
        var name = panel.querySelector('.ve-loc-name').value.trim();
        if (!name) { showToast('Name ist erforderlich', 'error'); return; }
        var payload = {
            name: name,
            street: panel.querySelector('.ve-loc-street').value.trim(),
            postal_code: panel.querySelector('.ve-loc-postal').value.trim(),
            city: panel.querySelector('.ve-loc-city').value.trim(),
            country: panel.querySelector('.ve-loc-country').value.trim(),
            subnet: panel.querySelector('.ve-loc-subnet').value.trim(),
            lat: panel.querySelector('.ve-loc-lat').value.trim(),
            lon: panel.querySelector('.ve-loc-lon').value.trim(),
            mqtt_coords: mqttToggle.checked,
            mqtt_fallback_days: parseInt(panel.querySelector('.ve-loc-fallback').value, 10) || 0
        };
        saveBtn.disabled = true;
        saveBtn.textContent = 'Speichern…';
        var url = isNew ? '/api/locations' : '/api/locations/' + loc.id;
        var method = isNew ? 'POST' : 'PUT';
        fetch(url, {
            method: method,
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        })
            .then(function(r) { return r.json(); })
            .then(function(d) {
                saveBtn.disabled = false;
                saveBtn.textContent = 'Speichern';
                if (d.error) { showToast('Fehler: ' + d.error, 'error'); return; }
                showToast('Standort gespeichert', 'success');
                if (isNew && d.id) {
                    window.location.hash = 'location/' + d.id;
                } else {
                    // Edit saved — new baseline, hide the dirty save pair.
                    locOriginal = _locSnapshot();
                    checkLocDirty();
                }
            })
            .catch(function(e) {
                saveBtn.disabled = false;
                saveBtn.textContent = 'Speichern';
                showToast('Fehler: ' + e.message, 'error');
            });
    });
}

// ----- Reusable Standort picker for inverter forms -----

function _locationFieldHtml(currentId) {
    var opts = '<option value="">— Kein Standort —</option>';
    for (var i = 0; i < _locations.length; i++) {
        var l = _locations[i];
        opts += '<option value="' + esc(l.id) + '"' + (l.id === currentId ? ' selected' : '') + '>' + esc(l.name || 'Standort') + '</option>';
    }
    opts += '<option value="__new__">+ Neuer Standort…</option>';
    return '<div class="ve-form-group ve-loc-field">' +
        '<label>Standort</label>' +
        '<select class="ve-input ve-loc-select">' + opts + '</select>' +
        '<div class="ve-loc-new-row" style="display:none">' +
        '  <input type="text" class="ve-input ve-loc-new-name" placeholder="Name des neuen Standorts">' +
        '  <button type="button" class="ve-btn ve-btn--sm ve-btn--primary ve-loc-new-create">Anlegen</button>' +
        '</div></div>';
}

// Derive a /24 from a device already assigned to a Standort, so Discover
// can target that Standort's network even without an explicit subnet.
function _inferSubnetFromLocation(locId) {
    if (!locId) return '';
    for (var i = 0; i < _devices.length; i++) {
        var d = _devices[i];
        if (d.location_id !== locId) continue;
        var host = d.host || d.gateway_host || '';
        var m = /^(\d+\.\d+\.\d+)\.\d+$/.exec(host);
        if (m) return m[1] + '.0/24';
    }
    return '';
}

// Point the add-device Discover scan at the selected Standort's network.
// Priority: the Standort's explicit subnet → a subnet inferred from a
// device already there → the proxy's local subnet.
function _syncDiscoverSubnet(scope, locId) {
    var field = scope.querySelector('.ve-add-subnet');
    if (!field) return;
    var loc = locId ? _locationById(locId) : null;
    if (loc && loc.subnet) {
        field.value = loc.subnet;
        return;
    }
    var inferred = _inferSubnetFromLocation(locId);
    if (inferred) {
        field.value = inferred;
    } else if (_localSubnetCache) {
        field.value = _localSubnetCache;
    }
}

function _wireLocationField(scope, onChange) {
    var sel = scope.querySelector('.ve-loc-select');
    if (!sel) return;
    var newRow = scope.querySelector('.ve-loc-new-row');
    var newName = scope.querySelector('.ve-loc-new-name');
    var createBtn = scope.querySelector('.ve-loc-new-create');
    sel.addEventListener('change', function() {
        if (sel.value === '__new__') {
            newRow.style.display = 'flex';
            newName.focus();
        } else {
            newRow.style.display = 'none';
            _syncDiscoverSubnet(scope, sel.value);
            if (onChange) onChange();
        }
    });
    createBtn.addEventListener('click', function() {
        var nm = newName.value.trim();
        if (!nm) { showToast('Name erforderlich', 'error'); return; }
        createBtn.disabled = true;
        fetch('/api/locations', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name: nm })
        })
            .then(function(r) { return r.json(); })
            .then(function(d) {
                createBtn.disabled = false;
                if (d.id) {
                    var opt = document.createElement('option');
                    opt.value = d.id;
                    opt.textContent = nm;
                    sel.insertBefore(opt, sel.querySelector('option[value="__new__"]'));
                    sel.value = d.id;
                    newRow.style.display = 'none';
                    newName.value = '';
                    showToast('Standort "' + nm + '" angelegt', 'success');
                    if (onChange) onChange();
                } else {
                    showToast(d.error || 'Anlegen fehlgeschlagen', 'error');
                }
            })
            .catch(function(e) { createBtn.disabled = false; showToast('Fehler: ' + e.message, 'error'); });
    });
}

function _getLocationFieldValue(scope) {
    var sel = scope.querySelector('.ve-loc-select');
    if (!sel || sel.value === '__new__') return '';
    return sel.value;
}

