// ===== Inverter Registers Renderer =====

/**
 * Read-policy panel: states which register blocks are ACTUALLY polled and how
 * often. The content comes from the plugin (device.register_read_policy) rather
 * than being hardcoded here, so the display cannot drift from the code that
 * does the reading.
 *
 * Renders nothing for plugins that synthesise their blocks (no policy) — an
 * empty section would be noise, not information.
 */
function _buildReadPolicyHtml(deviceId) {
    // Looked up from the global device list rather than passed in: the
    // registers tab is rendered with only an id/type in scope. If the list is
    // not populated yet (direct load of this tab) the panel simply does not
    // render — it is transparency, not control, so degrading to nothing is fine.
    var device = null;
    for (var i = 0; i < _devices.length; i++) {
        if (_devices[i].id === deviceId) { device = _devices[i]; break; }
    }
    var policy = device && device.register_read_policy;
    if (!policy || !policy.length) return '';
    var rows = policy.map(function(p) {
        return '<div class="ve-reg-policy-row">' +
               '<span class="ve-reg-policy-block">' + esc(p.block || '') + '</span>' +
               '<span class="ve-reg-policy-detail">' + esc(p.detail || '') + '</span>' +
               '<span class="ve-reg-policy-cadence">' + esc(p.cadence || '') + '</span>' +
               '</div>';
    }).join('');
    var iv = device.effective_poll_interval
        ? '<span class="ve-reg-policy-interval">Effektives Poll-Intervall: ' +
          esc(String(device.effective_poll_interval)) + ' s</span>'
        : '';
    return '<div class="ve-reg-policy">' +
           '<div class="ve-reg-policy-head"><span>Was periodisch gelesen wird</span>' + iv + '</div>' +
           rows +
           '</div>';
}

function renderInverterRegisters(container, deviceId, deviceType) {
    container.innerHTML = '';

    // Doc links per device type
    var docLinks = {
        solaredge:
            '<a href="https://github.com/nmakel/solaredge_modbus" target="_blank" rel="noopener" class="ve-doc-link" title="SolarEdge Modbus Register Reference">SE</a>',
        opendtu:
            '<a href="https://www.opendtu.solar/" target="_blank" rel="noopener" class="ve-doc-link" title="OpenDTU Documentation">DTU</a>',
        shelly:
            '<a href="https://shelly-api-docs.shelly.cloud/" target="_blank" rel="noopener" class="ve-doc-link" title="Shelly API Documentation">Shelly</a>',
        sungrow:
            '<a href="https://github.com/bohdan-s/SunGather" target="_blank" rel="noopener" class="ve-doc-link" title="Sungrow Modbus Register Map (SunGather)">SG</a>',
        hoymiles:
            '<a href="https://github.com/suaveolent/hoymiles-wifi" target="_blank" rel="noopener" class="ve-doc-link" title="Hoymiles HMS Local Protocol (hoymiles-wifi)">HMS</a>',
        senec:
            '<a href="https://github.com/mchwalisz/pysenec" target="_blank" rel="noopener" class="ve-doc-link" title="SENEC lala.cgi local API (pysenec)">SENEC</a>'
    };
    var typeLink = docLinks[deviceType] || '';

    // Toolbar
    var toolbar = document.createElement('div');
    toolbar.className = 've-panel';
    toolbar.innerHTML =
        '<div class="ve-reg-toolbar">' +
        '  <h2>Register Viewer' +
        '    ' + typeLink +
        '    <a href="https://github.com/victronenergy/dbus-fronius" target="_blank" rel="noopener" class="ve-doc-link" title="Victron dbus-fronius">VE</a>' +
        '    <a href="https://files.sma.de/downloads/SunSpecModbus-TI-en-11.pdf" target="_blank" rel="noopener" class="ve-doc-link" title="SunSpec Modbus Register Reference (PDF)">SunSpec</a>' +
        '  </h2>' +
        '  <label class="ve-toggle-label">' +
        '    <span>Hide empty</span>' +
        '    <input type="checkbox" class="ve-reg-hide-toggle" checked>' +
        '    <span class="ve-switch"><span class="ve-switch-knob"></span></span>' +
        '  </label>' +
        '</div>' +
        _buildReadPolicyHtml(deviceId) +
        '<div class="ve-spinner-wrap ve-reg-spinner"><div class="ve-spinner"></div><span class="ve-spinner-label">Loading registers...</span></div>' +
        '<div class="ve-reg-models"></div>';
    container.appendChild(toolbar);

    // Hide empty toggle
    var toggle = toolbar.querySelector('.ve-reg-hide-toggle');
    toggle.addEventListener('change', function() {
        var rows = container.querySelectorAll('.ve-reg-row.ve-empty');
        for (var i = 0; i < rows.length; i++) {
            if (toggle.checked) {
                rows[i].classList.remove('ve-show-empty');
            } else {
                rows[i].classList.add('ve-show-empty');
            }
        }
    });

    // Fetch registers for this specific device
    var regUrl = '/api/devices/' + encodeURIComponent(deviceId) + '/registers';
    var regSpinner = container.querySelector('.ve-reg-spinner');
    fetch(regUrl)
        .then(function(res) { return res.json(); })
        .then(function(models) {
            if (regSpinner) regSpinner.style.display = 'none';
            var modelsContainer = container.querySelector('.ve-reg-models');
            buildRegisterViewer(modelsContainer, models);
        })
        .catch(function(err) {
            if (regSpinner) regSpinner.innerHTML = '<span class="ve-hint-subtext">Failed to load registers</span>';
            console.error('Register load failed:', err);
        });

    // Poll registers while this tab is active
    if (_regPollInterval) clearInterval(_regPollInterval);
    _regPollInterval = setInterval(function() {
        if (_activeDeviceTab !== 'registers' || _activeDeviceId !== deviceId) {
            clearInterval(_regPollInterval);
            _regPollInterval = null;
            return;
        }
        fetch(regUrl)
            .then(function(res) { return res.json(); })
            .then(function(models) {
                updateRegisterValues(models);
            })
            .catch(function() {});
    }, POLL_INTERVAL);
}

// ===== Strings & Panels editor =====
// Renders the per-string panel-inventory editor used inside the device
// config panel. The data model mirrors the server-side StringSpec /
// PanelSpec dataclasses; total Wp = sum of count*wp_each across every
// panel group on every string.

// Phase dropdown + Phase-Detective button helpers.
//
// Always render four options so the user can move freely between
// "default L1" and "explicit L1/L2/L3":
//   value 0  →  "L1 (Standard)" sentinel — server interprets as
//               phase=1, phase_explicit=False
//   value 1  →  explicit L1     — server interprets as phase=1, _explicit=True
//   value 2  →  explicit L2     — same shape
//   value 3  →  explicit L3     — same shape
//
// The selected option is value=0 when phase_explicit is False, else
// value=phase. This lets a user revert a previously-detected L2 back
// to default by simply picking "L1 (Standard)" — no extra UI needed.
function _phaseOptionsHtml(device) {
    var current = device.phase || 1;
    var explicit = !!device.phase_explicit;
    var selectedValue = explicit ? current : 0;
    function opt(val, label) {
        var sel = selectedValue === val ? ' selected' : '';
        return '<option value="' + val + '"' + sel + '>' + label + '</option>';
    }
    return opt(0, 'L1 (Standard)') + opt(1, 'L1') + opt(2, 'L2') + opt(3, 'L3');
}

function _detectiveButtonLabel(device) {
    return device.phase_explicit ? 'Erkennung verifizieren' : 'Erkennung starten';
}

function _renderPhaseDropdown(selectEl, device) {
    selectEl.innerHTML = _phaseOptionsHtml(device);
}

function _renderDetectiveButton(rootPanel, device) {
    var btn = rootPanel.querySelector('.ve-phase-detective-start');
    if (btn) btn.textContent = _detectiveButtonLabel(device);
}


function _buildPanelGroupHtml(p) {
    p = p || {};
    return '<div class="ve-panel-group">' +
        '  <input type="number" class="ve-input ve-panel-count" min="0" value="' + (p.count || 0) + '" title="Anzahl Panels" style="width:64px">' +
        '  <span class="ve-panel-x">×</span>' +
        '  <input type="number" class="ve-input ve-panel-wp" min="0" value="' + (p.wp_each || 0) + '" title="Wp pro Panel" style="width:80px">' +
        '  <span class="ve-panel-unit">Wp</span>' +
        '  <input type="text" class="ve-input ve-panel-brand" placeholder="Marke (opt.)" value="' + esc(p.brand || '') + '">' +
        '  <input type="text" class="ve-input ve-panel-model" placeholder="Modell (opt.)" value="' + esc(p.model || '') + '">' +
        '  <span class="ve-panel-group-wp">' + ((p.count || 0) * (p.wp_each || 0)) + ' Wp</span>' +
        '  <button type="button" class="ve-btn ve-btn--sm ve-btn--cancel ve-panel-remove" title="Gruppe entfernen">×</button>' +
        '</div>';
}

function _buildStringHtml(s, idx) {
    s = s || {};
    var panels = s.panels || [];
    var stringWp = 0;
    panels.forEach(function(p) { stringWp += (p.count || 0) * (p.wp_each || 0); });
    var groupsHtml = panels.length
        ? panels.map(_buildPanelGroupHtml).join('')
        : '<div class="ve-panel-groups-empty">Noch keine Panel-Gruppen</div>';
    return '<div class="ve-string-card" data-string-idx="' + idx + '">' +
        '  <div class="ve-string-header">' +
        '    <input type="text" class="ve-input ve-string-name" placeholder="String-Name (z.B. Süd-Dach)" value="' + esc(s.name || '') + '">' +
        '    <span class="ve-string-wp">' + stringWp + ' Wp</span>' +
        '    <button type="button" class="ve-btn ve-btn--sm ve-btn--cancel ve-string-remove" title="String entfernen">×</button>' +
        '  </div>' +
        '  <div class="ve-panel-groups">' + groupsHtml + '</div>' +
        '  <button type="button" class="ve-btn ve-btn--sm ve-panel-add">+ Panel-Gruppe</button>' +
        '</div>';
}

function _buildStringsEditorHtml(strings) {
    strings = strings || [];
    var total = 0;
    strings.forEach(function(s) {
        (s.panels || []).forEach(function(p) { total += (p.count || 0) * (p.wp_each || 0); });
    });
    var listHtml = strings.length
        ? strings.map(_buildStringHtml).join('')
        : '<div class="ve-strings-empty">Noch keine Strings konfiguriert</div>';
    return '<div class="ve-form-group ve-strings-section">' +
        '  <label>Strings &amp; Panels <span class="ve-strings-total-wrap">(Total: <span class="ve-strings-total">' + total + '</span> Wp)</span></label>' +
        '  <div class="ve-strings-list">' + listHtml + '</div>' +
        '  <button type="button" class="ve-btn ve-btn--sm ve-string-add">+ String</button>' +
        '  <div class="ve-form-hint" style="font-size:0.75rem;color:var(--ve-text-dim);margin-top:6px">' +
        '    Pro String beliebig viele Panel-Gruppen (z.B. Süd-Dach: 8×545 Wp JA Solar + 4×440 Wp Trina). Total wird automatisch summiert.' +
        '  </div>' +
        '</div>';
}

function _readStringsFromDom(panel) {
    var out = [];
    panel.querySelectorAll('.ve-string-card').forEach(function(card) {
        var nm = card.querySelector('.ve-string-name');
        var panels = [];
        card.querySelectorAll('.ve-panel-group').forEach(function(pg) {
            panels.push({
                count: parseInt(pg.querySelector('.ve-panel-count').value) || 0,
                wp_each: parseInt(pg.querySelector('.ve-panel-wp').value) || 0,
                brand: pg.querySelector('.ve-panel-brand').value.trim(),
                model: pg.querySelector('.ve-panel-model').value.trim()
            });
        });
        out.push({ name: nm ? nm.value.trim() : '', panels: panels });
    });
    return out;
}

function _recomputeStringsTotals(panel) {
    var grandTotal = 0;
    panel.querySelectorAll('.ve-string-card').forEach(function(card) {
        var stringWp = 0;
        card.querySelectorAll('.ve-panel-group').forEach(function(pg) {
            var c = parseInt(pg.querySelector('.ve-panel-count').value) || 0;
            var w = parseInt(pg.querySelector('.ve-panel-wp').value) || 0;
            var gwp = c * w;
            stringWp += gwp;
            pg.querySelector('.ve-panel-group-wp').textContent = gwp + ' Wp';
        });
        var stringWpEl = card.querySelector('.ve-string-wp');
        if (stringWpEl) stringWpEl.textContent = stringWp + ' Wp';
        grandTotal += stringWp;
    });
    var totalEl = panel.querySelector('.ve-strings-total');
    if (totalEl) totalEl.textContent = grandTotal;
}

function _rerenderStringsList(panel, strings) {
    var list = panel.querySelector('.ve-strings-list');
    if (!list) return;
    list.innerHTML = strings.length
        ? strings.map(_buildStringHtml).join('')
        : '<div class="ve-strings-empty">Noch keine Strings konfiguriert</div>';
    _recomputeStringsTotals(panel);
}

// ===== Inverter Config Renderer =====

function renderInverterConfig(container, deviceId) {
    container.innerHTML =
        '<div class="ve-spinner-wrap"><div class="ve-spinner"></div><span class="ve-spinner-label">Loading config...</span></div>';

    // Load devices + locations together. The Standort dropdown is built from
    // the global _locations, which otherwise only arrives via the WS push --
    // not guaranteed to be ready on a direct load/reload of this config tab,
    // so the assigned Standort would wrongly render as "Kein Standort".
    // A failed locations fetch is non-fatal: keep whatever _locations holds.
    Promise.all([
        fetch('/api/devices').then(function(res) { return res.json(); }),
        fetch('/api/locations').then(function(res) { return res.json(); })
            .catch(function() { return null; })
    ])
        .then(function(results) {
            var data = results[0];
            var locData = results[1];
            if (locData && locData.locations) _locations = locData.locations;
            var device = null;
            var devices = data.devices || [];
            for (var i = 0; i < devices.length; i++) {
                if (devices[i].id === deviceId) { device = devices[i]; break; }
            }
            if (!device) {
                container.innerHTML = '<div class="ve-hint-card"><div class="ve-hint-header">Device not found</div></div>';
                return;
            }
            container.innerHTML = '';
            buildInverterConfigForm(container, device);
        })
        .catch(function(err) {
            container.innerHTML = '<div class="ve-hint-card"><div class="ve-hint-header">Failed to load config</div><p class="ve-hint-subtext">' + err.message + '</p></div>';
        });
}

function buildInverterConfigForm(container, device) {
    var panel = document.createElement('div');
    panel.className = 've-panel';

    var identity = ((device.manufacturer || '') + ' ' + (device.model || '')).trim();

    // 260528-hhj: inline status card at the top of the config panel showing
    // connection state + relative time since the last successful poll, with
    // the truncated last error underneath when present.
    var _statusDotColor = dotColorForState(device.connection_state);
    var _statusHeadline, _statusClass;
    if (device.connection_state === 'connected' && device.last_successful_poll_at) {
        _statusHeadline = 'Verbunden · letzter Poll ' +
            formatRelativeTime(device.last_successful_poll_at);
        _statusClass = 've-status-card--ok';
    } else if (device.last_successful_poll_at) {
        _statusHeadline = 'Getrennt · letzter Erfolg ' +
            formatRelativeTime(device.last_successful_poll_at);
        _statusClass = 've-status-card--warn';
    } else if (device.last_poll_at) {
        _statusHeadline = 'Noch nie erfolgreich gepollt';
        _statusClass = 've-status-card--err';
    } else {
        _statusHeadline = 'Noch nicht gepollt';
        _statusClass = 've-status-card--idle';
    }
    var _statusErrLine = device.last_poll_error
        ? '<div class="ve-status-error" title="' + esc(device.last_poll_error) + '">' +
          esc(device.last_poll_error) + '</div>'
        : '';
    var _statusHtml =
        '<div class="ve-status-card ' + _statusClass + '">' +
        '  <div class="ve-status-row">' +
        '    <span class="ve-dot" style="background:var(' + _statusDotColor + ')"></span>' +
        '    <span class="ve-status-headline">' + esc(_statusHeadline) + '</span>' +
        '  </div>' +
        _statusErrLine +
        '</div>';

    panel.innerHTML =
        '<div class="ve-panel-header">' +
        '  <h2>Device Configuration</h2>' +
        '  <span class="ve-btn-pair ve-cfg-save-pair" style="display:none">' +
        '    <button type="button" class="ve-btn ve-btn--sm ve-btn--cancel ve-cfg-cancel">Cancel</button>' +
        '    <button type="button" class="ve-btn ve-btn--sm ve-btn--save ve-cfg-save">Save</button>' +
        '  </span>' +
        '</div>' +
        _statusHtml +
        '<div class="ve-ess-row" style="margin-bottom:12px">' +
        '  <label>Enabled</label>' +
        '  <label class="ve-toggle"><input type="checkbox" class="ve-cfg-enabled" ' + (device.enabled ? 'checked' : '') + '><span class="ve-toggle-track"></span></label>' +
        '</div>' +
        '<div class="ve-form-group"><label>Display Name</label><input type="text" class="ve-input ve-cfg-name" value="' + esc(device.name || '') + '" placeholder="e.g. SE30K"></div>' +
        '<div class="ve-form-group"><label>Info</label><input type="text" class="ve-input ve-cfg-info" value="' + esc(device.info || '') + '" placeholder="optionaler Hinweis, z.B. Dachausrichtung"></div>' +
        '<div class="ve-form-group"><label>Host</label><input type="text" class="ve-input ve-cfg-host" value="' + esc(device.host || '') + '" placeholder="198.51.100.100"></div>' +
        (device.type !== 'shelly' && device.type !== 'hoymiles' && device.type !== 'senec' ?
            '<div class="ve-form-group"><label>Port</label><input type="number" class="ve-input ve-cfg-port" value="' + (device.port || 1502) + '" min="1" max="65535"></div>' +
            '<div class="ve-form-group"><label>Unit ID</label><input type="number" class="ve-input ve-cfg-unit" value="' + (device.unit_id || 1) + '" min="1" max="247"></div>'
        : '') +
        (device.type === 'shelly' ?
            '<div class="ve-form-group"><label>Generation</label><span class="ve-gen-badge">' + esc(device.shelly_gen === 'gen2' ? 'Gen2' : (device.shelly_gen === 'gen3' ? 'Gen3' : 'Gen1')) + '</span></div>'
        : '') +
        '<div class="ve-form-group"><label>Inverter Rated Power (W)</label><input type="number" class="ve-input ve-cfg-rated-power" value="' + (device.rated_power || 0) + '" min="0"></div>' +
        // Phase assignment — relevant for single-phase devices (Hoymiles HMS,
        // OpenDTU, Shelly) so their L1 output lands on the wired phase in the
        // Venus per-phase view. Three-phase devices distribute themselves.
        ((device.type === 'hoymiles' || device.type === 'opendtu' || device.type === 'shelly') ?
            '<div class="ve-form-group"><label>Phase (L1/L2/L3)</label>' +
            '  <select class="ve-input ve-cfg-phase">' +
            _phaseOptionsHtml(device) +
            '  </select>' +
            '  <span class="ve-form-hint" style="font-size:0.75rem;color:var(--ve-text-dim);margin-top:4px;display:block">' +
            '    An welcher Netzphase dieser einphasige Inverter hängt. Bestimmt, wo Venus Strom &amp; Spannung zuordnet.' +
            '  </span>' +
            // Phasen-Detective only makes sense at the Standort that owns the
            // grid meter (`virtual_inverter.location_ids`). For inverters at
            // other locations the throttle wouldn't change the grid meter the
            // Venus actually watches, so we hide the panel entirely and just
            // surface a one-line hint instead of letting the user kick off a
            // null probe.
            (device.phase_detect_supported ?
              '  <div class="ve-phase-detective" data-device-id="' + esc(device.id) + '" style="margin-top:12px">' +
              '    <div class="ve-phase-detective-header">' +
              '      <span class="ve-phase-detective-title">🔎 Phasen-Detective</span>' +
              '      <button type="button" class="ve-btn ve-btn--sm ve-phase-detective-start" disabled>' +
              esc(_detectiveButtonLabel(device)) +
              '</button>' +
              '    </div>' +
              '    <ul class="ve-phase-detective-preconds"></ul>' +
              '    <div class="ve-phase-detective-progress" style="display:none">' +
              '      <div class="ve-phase-detective-step"></div>' +
              '      <ol class="ve-phase-detective-log"></ol>' +
              '    </div>' +
              '    <div class="ve-phase-detective-result" style="display:none"></div>' +
              '  </div>'
            :
              '  <div class="ve-form-hint" style="margin-top:8px;color:var(--ve-text-dim);font-size:0.8rem">' +
              '    Phasen-Detective nicht verfügbar: dieser Standort ist nicht ans Grid-Meter der Venus angebunden.' +
              '  </div>'
            ) +
            '</div>'
        : '') +
        _buildStringsEditorHtml(device.strings || []) +
        '<div class="ve-form-group"><label>Poll-Intervall (s, 0 = Standard)</label><input type="number" step="0.5" min="0" class="ve-input ve-cfg-poll-interval" value="' + (device.poll_interval || 0) + '"></div>' +
        _locationFieldHtml(device.location_id || '') +
        (device.type === 'opendtu' ?
            '<div class="ve-form-group"><label>Inverter am Gateway</label>' +
            '  <select class="ve-input ve-cfg-serial"><option value="' + esc(device.serial || '') + '" selected>' +
                (device.serial ? esc(device.serial) : '— kein Inverter gewählt —') + '</option></select>' +
            '  <span class="ve-form-hint" style="font-size:0.75rem;color:var(--ve-text-dim);margin-top:4px;display:block">' +
            '    Welcher Inverter dieses OpenDTU-Gateways gelesen wird — Pflicht, wenn am Gateway mehrere Inverter hängen.' +
            '  </span>' +
            '</div>' +
            '<div class="ve-form-group"><label>Gateway User</label><input type="text" class="ve-input ve-cfg-gw-user" value="' + esc(device.gateway_user || '') + '" placeholder="admin (default)"></div>' +
            '<div class="ve-form-group"><label>Gateway Password</label><input type="password" class="ve-input ve-cfg-gw-pass" value="' + esc(device.gateway_password || '') + '" placeholder="openDTU42 (default)"></div>'
        : '') +
        '<div class="ve-form-group"><label>Type</label><input type="text" class="ve-input" value="' + (device.type || '') + '" readonly style="opacity:0.6"></div>' +
        (identity ? '<div class="ve-form-group"><label>Identity</label><input type="text" class="ve-input" value="' + esc(identity) + '" readonly style="opacity:0.6"></div>' : '') +
        '<div style="margin-top:16px;padding-top:12px;border-top:1px solid var(--ve-border)">' +
        '  <div style="font-size:0.85rem;color:var(--ve-text-dim);margin-bottom:8px">Fronius Proxy</div>' +
        '  <div class="ve-ess-row">' +
        '    <label>Power Limit Control</label>' +
        '    <label class="ve-toggle"><input type="checkbox" class="ve-cfg-throttle-enabled" ' + (device.throttle_enabled !== false ? 'checked' : '') + '><span class="ve-toggle-track"></span></label>' +
        '  </div>' +
        '</div>' +
        (device.supports_reboot ?
            '<div class="ve-panel-reboot-section" style="margin-top:16px;padding-top:12px;border-top:1px solid var(--ve-border)">' +
            '  <div style="font-size:0.85rem;color:var(--ve-text-dim);margin-bottom:8px">WiNet-Modul</div>' +
            '  <button type="button" class="ve-btn ve-btn--sm ve-btn--warning ve-cfg-reboot">WiNet Reboot</button>' +
            '  <span class="ve-cfg-reboot-status" style="margin-left:12px;font-size:0.85rem;color:var(--ve-text-dim)"></span>' +
            '  <div class="ve-form-hint" style="margin-top:8px;color:var(--ve-text-dim);font-size:0.8rem">' +
            '    Startet das WiNet-S Kommunikationsmodul neu. Der Inverter selbst läuft weiter; der Modbus-Slot ist ~60-120s offline.' +
            '  </div>' +
            '</div>'
        : '') +
        '<div class="ve-panel-config-section" style="margin-top:16px;padding-top:12px;border-top:1px solid var(--ve-border)">' +
        '  <div style="font-size:0.85rem;color:var(--ve-text-dim);margin-bottom:8px">Konfiguration sichern</div>' +
        '  <button type="button" class="ve-btn ve-btn--sm ve-cfg-export">Export</button>' +
        '  <button type="button" class="ve-btn ve-btn--sm ve-cfg-import">Import</button>' +
        '  <input type="file" class="ve-cfg-import-file" accept=".yaml,.yml,.json" style="display:none">' +
        '  <span class="ve-cfg-iexport-status" style="margin-left:12px;font-size:0.85rem;color:var(--ve-text-dim)"></span>' +
        '  <div class="ve-form-hint" style="margin-top:8px;color:var(--ve-text-dim);font-size:0.8rem">' +
        '    Export lädt diesen Inverter als Datei auf deinen Rechner. Import lädt eine Datei hoch und legt den Inverter an (oder aktualisiert ihn bei gleicher ID). Hinweis: die Datei kann Gateway-Zugangsdaten enthalten.' +
        '  </div>' +
        '</div>' +
        '<div class="ve-panel-danger-section">' +
        '  <button type="button" class="ve-btn ve-btn--danger ve-cfg-delete">Delete Device</button>' +
        '</div>';

    container.appendChild(panel);

    // Store originals for dirty tracking. Strings are tracked as a
    // JSON snapshot since the live DOM can grow/shrink dynamically.
    var originals = {
        name: device.name || '',
        info: device.info || '',
        host: device.host || '',
        port: String(device.port || 1502),
        unit_id: String(device.unit_id || 1),
        throttle_enabled: device.throttle_enabled !== false,
        enabled: device.enabled !== false,
        gateway_user: device.gateway_user || '',
        gateway_password: device.gateway_password || '',
        serial: device.serial || '',
        rated_power: String(device.rated_power || 0),
        strings_json: JSON.stringify(_readStringsFromDom(panel)),
        poll_interval: String(device.poll_interval || 0),
        location: device.location_id || '',
        // Stored as the dropdown's *selected value*, not the phase int:
        // "0" while phase_explicit is False (the "L1 (Standard)" sentinel),
        // "1"/"2"/"3" when the user (or Phase-Detective) has picked one
        // explicitly. The dirty check just compares this to the select's
        // current value, so the sentinel round-trips cleanly.
        phase: String(device.phase_explicit ? (device.phase || 1) : 0)
    };

    var nameInput = panel.querySelector('.ve-cfg-name');
    var infoInput = panel.querySelector('.ve-cfg-info');
    var hostInput = panel.querySelector('.ve-cfg-host');
    var portInput = panel.querySelector('.ve-cfg-port');
    var unitInput = panel.querySelector('.ve-cfg-unit');
    var gwUserInput = panel.querySelector('.ve-cfg-gw-user');
    var gwPassInput = panel.querySelector('.ve-cfg-gw-pass');
    var serialSelect = panel.querySelector('.ve-cfg-serial');
    var rpInput = panel.querySelector('.ve-cfg-rated-power');
    var stringsSection = panel.querySelector('.ve-strings-section');
    var ppiInput = panel.querySelector('.ve-cfg-poll-interval');
    var phaseSelect = panel.querySelector('.ve-cfg-phase');
    var teToggle = panel.querySelector('.ve-cfg-throttle-enabled');
    var enabledToggle = panel.querySelector('.ve-cfg-enabled');
    var savePair = panel.querySelector('.ve-cfg-save-pair');
    var saveBtn = panel.querySelector('.ve-cfg-save');
    var cancelBtn = panel.querySelector('.ve-cfg-cancel');
    var deleteBtn = panel.querySelector('.ve-cfg-delete');
    var rebootBtn = panel.querySelector('.ve-cfg-reboot');
    var rebootStatus = panel.querySelector('.ve-cfg-reboot-status');
    var exportBtn = panel.querySelector('.ve-cfg-export');
    var importBtn = panel.querySelector('.ve-cfg-import');
    var importFile = panel.querySelector('.ve-cfg-import-file');
    var iexpStatus = panel.querySelector('.ve-cfg-iexport-status');

    function checkDirty() {
        var stringsJson = JSON.stringify(_readStringsFromDom(panel));
        var dirty = nameInput.value !== originals.name ||
                    infoInput.value !== originals.info ||
                    hostInput.value !== originals.host ||
                    (portInput && portInput.value !== originals.port) ||
                    (unitInput && unitInput.value !== originals.unit_id) ||
                    teToggle.checked !== originals.throttle_enabled ||
                    (gwUserInput && gwUserInput.value !== originals.gateway_user) ||
                    (gwPassInput && gwPassInput.value !== originals.gateway_password) ||
                    (serialSelect && serialSelect.value !== originals.serial) ||
                    rpInput.value !== originals.rated_power ||
                    stringsJson !== originals.strings_json ||
                    ppiInput.value !== originals.poll_interval ||
                    (phaseSelect && phaseSelect.value !== originals.phase) ||
                    _getLocationFieldValue(panel) !== originals.location;
        savePair.style.display = dirty ? '' : 'none';
        // Highlight dirty fields
        var trackFields = [nameInput, infoInput, hostInput, rpInput];
        if (portInput) trackFields.push(portInput);
        if (unitInput) trackFields.push(unitInput);
        if (gwUserInput) trackFields.push(gwUserInput);
        if (gwPassInput) trackFields.push(gwPassInput);
        trackFields.forEach(function(el) {
            var orig = el === nameInput ? originals.name : el === infoInput ? originals.info : el === hostInput ? originals.host : el === portInput ? originals.port : el === unitInput ? originals.unit_id : el === gwUserInput ? originals.gateway_user : el === gwPassInput ? originals.gateway_password : el === rpInput ? originals.rated_power : '';
            if (el.value !== orig) el.classList.add('ve-input--dirty');
            else el.classList.remove('ve-input--dirty');
        });
        if (serialSelect) {
            if (serialSelect.value !== originals.serial) serialSelect.classList.add('ve-input--dirty');
            else serialSelect.classList.remove('ve-input--dirty');
        }
        if (stringsSection) {
            if (stringsJson !== originals.strings_json) stringsSection.classList.add('ve-strings-section--dirty');
            else stringsSection.classList.remove('ve-strings-section--dirty');
        }
    }

    var inputFields = [nameInput, infoInput, hostInput, rpInput, ppiInput];
    if (portInput) inputFields.push(portInput);
    if (unitInput) inputFields.push(unitInput);
    if (gwUserInput) inputFields.push(gwUserInput);
    if (gwPassInput) inputFields.push(gwPassInput);
    inputFields.forEach(function(el) {
        el.addEventListener('input', checkDirty);
    });
    teToggle.addEventListener('change', checkDirty);
    if (serialSelect) serialSelect.addEventListener('change', checkDirty);
    if (phaseSelect) phaseSelect.addEventListener('change', checkDirty);

    // Phasen-Detective wiring (only for single-phase devices)
    var detective = panel.querySelector('.ve-phase-detective');
    if (detective) _wirePhaseDetective(detective, device, phaseSelect, checkDirty, function(newPhase) {
        // Auto-apply callback: server persisted phase=newPhase + explicit.
        // Sync the local state used by the dirty check so the save pair
        // stays hidden (the change is already committed server-side).
        device.phase = newPhase;
        device.phase_explicit = true;
        originals.phase = String(newPhase);
        _renderPhaseDropdown(phaseSelect, device);
        _renderDetectiveButton(panel, device);
        checkDirty();
    });
    _wireLocationField(panel, checkDirty);

    // Strings & Panels editor — delegated input + click handlers.
    if (stringsSection) {
        stringsSection.addEventListener('input', function(e) {
            // Numeric/text edits update live totals + dirty state.
            _recomputeStringsTotals(panel);
            checkDirty();
        });
        stringsSection.addEventListener('click', function(e) {
            var t = e.target;
            if (!t || !t.classList) return;
            if (t.classList.contains('ve-string-add')) {
                var cur = _readStringsFromDom(panel);
                cur.push({ name: '', panels: [{ count: 1, wp_each: 0, brand: '', model: '' }] });
                _rerenderStringsList(panel, cur);
                checkDirty();
                return;
            }
            if (t.classList.contains('ve-string-remove')) {
                var card = t.closest('.ve-string-card');
                if (card) card.remove();
                _recomputeStringsTotals(panel);
                // Remove the "empty" placeholder if any cards remain, or add one.
                var list = panel.querySelector('.ve-strings-list');
                if (list && !list.querySelector('.ve-string-card')) {
                    list.innerHTML = '<div class="ve-strings-empty">Noch keine Strings konfiguriert</div>';
                }
                checkDirty();
                return;
            }
            if (t.classList.contains('ve-panel-add')) {
                var card2 = t.closest('.ve-string-card');
                var groups = card2 && card2.querySelector('.ve-panel-groups');
                if (groups) {
                    // Drop the "empty" placeholder if first add.
                    var empty = groups.querySelector('.ve-panel-groups-empty');
                    if (empty) empty.remove();
                    groups.insertAdjacentHTML('beforeend',
                        _buildPanelGroupHtml({ count: 1, wp_each: 0 }));
                    _recomputeStringsTotals(panel);
                    checkDirty();
                }
                return;
            }
            if (t.classList.contains('ve-panel-remove')) {
                var pg = t.closest('.ve-panel-group');
                var card3 = t.closest('.ve-string-card');
                if (pg) pg.remove();
                if (card3) {
                    var groups2 = card3.querySelector('.ve-panel-groups');
                    if (groups2 && !groups2.querySelector('.ve-panel-group')) {
                        groups2.innerHTML = '<div class="ve-panel-groups-empty">Noch keine Panel-Gruppen</div>';
                    }
                }
                _recomputeStringsTotals(panel);
                checkDirty();
                return;
            }
        });
    }

    // OpenDTU: populate the serial picker from the gateway's inverter list.
    if (device.type === 'opendtu' && serialSelect) {
        fetch('/api/devices/' + device.id + '/opendtu/inverters')
            .then(function(r) { return r.json(); })
            .then(function(d) {
                if (!d.ok || !d.inverters || !d.inverters.length) return;
                var cur = serialSelect.value;
                var hasCur = false;
                var html = '<option value="">— kein Inverter gewählt —</option>';
                d.inverters.forEach(function(it) {
                    if (it.serial === cur) hasCur = true;
                    html += '<option value="' + esc(it.serial) + '"' +
                        (it.serial === cur ? ' selected' : '') + '>' +
                        esc(it.name) + ' (' + esc(it.serial) + ')</option>';
                });
                if (cur && !hasCur) {
                    html += '<option value="' + esc(cur) + '" selected>' +
                        esc(cur) + ' (aktuell konfiguriert)</option>';
                }
                serialSelect.innerHTML = html;
            })
            .catch(function() { /* gateway unreachable — keep the single option */ });
    }

    cancelBtn.addEventListener('click', function() {
        nameInput.value = originals.name;
        infoInput.value = originals.info;
        hostInput.value = originals.host;
        if (portInput) portInput.value = originals.port;
        if (unitInput) unitInput.value = originals.unit_id;
        teToggle.checked = originals.throttle_enabled;
        if (gwUserInput) gwUserInput.value = originals.gateway_user;
        if (gwPassInput) gwPassInput.value = originals.gateway_password;
        if (serialSelect) serialSelect.value = originals.serial;
        rpInput.value = originals.rated_power;
        _rerenderStringsList(panel, JSON.parse(originals.strings_json));
        ppiInput.value = originals.poll_interval;
        if (phaseSelect) phaseSelect.value = originals.phase;
        var locSel = panel.querySelector('.ve-loc-select');
        if (locSel) locSel.value = originals.location;
        var locNewRow = panel.querySelector('.ve-loc-new-row');
        if (locNewRow) locNewRow.style.display = 'none';
        checkDirty();
    });

    saveBtn.addEventListener('click', function() {
        var payload = {
            name: nameInput.value.trim(),
            info: infoInput.value.trim(),
            host: hostInput.value.trim(),
            throttle_enabled: teToggle.checked
        };
        if (portInput) payload.port = parseInt(portInput.value);
        if (unitInput) payload.unit_id = parseInt(unitInput.value);
        if (gwUserInput) payload.gateway_user = gwUserInput.value.trim();
        if (gwPassInput) payload.gateway_password = gwPassInput.value.trim();
        if (serialSelect) payload.serial = serialSelect.value;
        payload.rated_power = parseInt(rpInput.value) || 0;
        payload.strings = _readStringsFromDom(panel);
        payload.poll_interval = parseFloat(ppiInput.value) || 0;
        payload.location_id = _getLocationFieldValue(panel);
        // Phase: only single-phase inverters render the phase dropdown,
        // but for them the user's choice was being silently dropped from
        // the save payload — fixed here. Including phase in the body is
        // also what server-side flips ``phase_explicit`` from default to
        // True, switching the dropdown label and detection button text.
        if (phaseSelect) payload.phase = parseInt(phaseSelect.value);

        fetch('/api/devices/' + device.id, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        })
        .then(function(res) { return res.json(); })
        .then(function(data) {
            if (data.error) {
                showToast('Update failed: ' + data.error, 'error');
                return;
            }
            showToast('Device updated', 'success');
            originals.name = payload.name;
            originals.info = payload.info;
            originals.host = payload.host;
            if (portInput) originals.port = String(payload.port);
            if (unitInput) originals.unit_id = String(payload.unit_id);
            originals.throttle_enabled = payload.throttle_enabled;
            if (gwUserInput) originals.gateway_user = payload.gateway_user || '';
            if (gwPassInput) originals.gateway_password = payload.gateway_password || '';
            if (serialSelect) originals.serial = payload.serial || '';
            originals.rated_power = String(payload.rated_power || 0);
            originals.strings_json = JSON.stringify(payload.strings || []);
            originals.poll_interval = String(payload.poll_interval || 0);
            if (phaseSelect) {
                // Mirror the server's interpretation of the sentinel:
                //   payload.phase === 0  →  phase=1, phase_explicit=False
                //   payload.phase 1/2/3  →  phase=N, phase_explicit=True
                if (payload.phase === 0) {
                    device.phase = 1;
                    device.phase_explicit = false;
                } else {
                    device.phase = payload.phase;
                    device.phase_explicit = true;
                }
                originals.phase = String(payload.phase);
                _renderPhaseDropdown(phaseSelect, device);
                _renderDetectiveButton(panel, device);
            }
            originals.location = payload.location_id || '';
            checkDirty();
        })
        .catch(function(e) { showToast('Update failed: ' + e.message, 'error'); });
    });

    // Enabled toggle -- instant save
    enabledToggle.addEventListener('change', function() {
        fetch('/api/devices/' + device.id, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ enabled: enabledToggle.checked })
        })
        .then(function(res) { return res.json(); })
        .then(function(data) {
            if (data.error) {
                showToast('Toggle failed: ' + data.error, 'error');
                enabledToggle.checked = !enabledToggle.checked;
                return;
            }
            showToast(enabledToggle.checked ? 'Device enabled' : 'Device disabled', 'success');
        })
        .catch(function(e) {
            showToast('Toggle failed: ' + e.message, 'error');
            enabledToggle.checked = !enabledToggle.checked;
        });
    });

    // Delete button
    deleteBtn.addEventListener('click', function() {
        deleteDeviceWithUndo(device.id, device.name || device.host || device.id);
    });

    // Export this inverter as a downloadable file (server sets the filename).
    if (exportBtn) {
        exportBtn.addEventListener('click', function() {
            var a = document.createElement('a');
            a.href = '/api/inverters/' + encodeURIComponent(device.id) + '/export';
            a.download = '';
            document.body.appendChild(a);
            a.click();
            a.remove();
        });
    }

    // Import a single inverter from an uploaded file: add new or update by id.
    if (importBtn && importFile) {
        importBtn.addEventListener('click', function() { importFile.click(); });
        importFile.addEventListener('change', function() {
            var f = importFile.files && importFile.files[0];
            if (!f) return;
            iexpStatus.textContent = 'Importiere…';
            iexpStatus.style.color = 'var(--ve-text-dim)';
            var reader = new FileReader();
            reader.onload = function() {
                fetch('/api/inverters/import', {
                    method: 'POST',
                    credentials: 'same-origin',
                    headers: { 'Content-Type': 'application/x-yaml' },
                    body: reader.result
                })
                .then(function(r) {
                    return r.json().then(function(j) { return { ok: r.ok, body: j }; });
                })
                .then(function(res) {
                    if (res.ok && res.body.success) {
                        var label = res.body.name || res.body.device_id;
                        iexpStatus.textContent = (res.body.is_new ? 'Importiert: ' : 'Aktualisiert: ') + label;
                        iexpStatus.style.color = 'var(--ve-green)';
                        showToast('Inverter ' + (res.body.is_new ? 'importiert' : 'aktualisiert') + ': ' + label, 'success');
                        setTimeout(function() { window.location.reload(); }, 1200);
                    } else {
                        iexpStatus.textContent = 'Fehler: ' + ((res.body && res.body.error) || 'unbekannt');
                        iexpStatus.style.color = 'var(--ve-red)';
                    }
                })
                .catch(function(err) {
                    iexpStatus.textContent = 'Netzwerkfehler: ' + err.message;
                    iexpStatus.style.color = 'var(--ve-red)';
                });
            };
            reader.readAsText(f);
            importFile.value = '';
        });
    }

    // WiNet Reboot button (only rendered when device.supports_reboot is true,
    // i.e. the backend plugin exposes an async reboot() method — currently
    // only Sungrow via WiNet-S).
    if (rebootBtn) {
        rebootBtn.addEventListener('click', function() {
            var ok = window.confirm('WiNet-Modul wird ~60s offline sein. Reboot fortfahren?');
            if (!ok) return;
            rebootBtn.disabled = true;
            rebootStatus.textContent = 'Reboot wird ausgelöst…';
            rebootStatus.style.color = 'var(--ve-text-dim)';
            fetch('/api/devices/' + encodeURIComponent(device.id) + '/reboot', {
                method: 'POST',
                credentials: 'same-origin',
                headers: { 'Content-Type': 'application/json' },
                body: '{}'
            })
            .then(function(r) {
                return r.json().then(function(j) { return { ok: r.ok, body: j }; });
            })
            .then(function(res) {
                if (res.ok && res.body.success) {
                    rebootStatus.textContent = 'Reboot getriggert, ~60s warten…';
                    rebootStatus.style.color = 'var(--ve-green)';
                    setTimeout(function() {
                        rebootBtn.disabled = false;
                        rebootStatus.textContent = '';
                    }, 60000);
                } else {
                    rebootStatus.textContent = 'Fehler: ' + (res.body.error || 'unbekannt');
                    rebootStatus.style.color = 'var(--ve-red)';
                    rebootBtn.disabled = false;
                }
            })
            .catch(function(err) {
                rebootStatus.textContent = 'Netzwerkfehler: ' + err.message;
                rebootStatus.style.color = 'var(--ve-red)';
                rebootBtn.disabled = false;
            });
        });
    }
}

// ----- Phasen-Detective (auto-detect which L1/L2/L3 an einphasiger Inverter hängt) -----
function _wirePhaseDetective(root, device, phaseSelect, dirtyCb, onAutoApplied) {
    var btn = root.querySelector('.ve-phase-detective-start');
    var preList = root.querySelector('.ve-phase-detective-preconds');
    var prog = root.querySelector('.ve-phase-detective-progress');
    var stepEl = root.querySelector('.ve-phase-detective-step');
    var logEl = root.querySelector('.ve-phase-detective-log');
    var resultEl = root.querySelector('.ve-phase-detective-result');

    var pollTimer = null;
    var precondTimer = null;
    var running = false;

    function renderPreconds(p) {
        if (!p) return;
        var html = '';
        Object.keys(p).forEach(function(key) {
            var item = p[key];
            var dot = item.ok ? 've-dot--ok' : 've-dot--err';
            html +=
                '<li><span class="ve-dot ' + dot + '"></span>' +
                '<span>' + esc(item.label) + '</span></li>';
        });
        preList.innerHTML = html;
        var allOk = Object.keys(p).every(function(k) { return p[k].ok; });
        btn.disabled = running || !allOk;
        btn.title = allOk ? '' : 'Voraussetzungen nicht erfüllt';
    }

    function _renderAtsBadge(data) {
        // ATS badge shows current Auto-Transfer-Switch state. Color-coded:
        //   grid (1)  → blue (normal grid-parallel operation)
        //   island (2) → orange (Inselbetrieb — Phase-Detective signals
        //                differ from grid mode, Multiplus is in charge)
        //   error (-1) → red
        //   null      → grey (no ATS publish seen yet)
        var hdr = root.querySelector('.ve-phase-detective-header');
        if (!hdr) return;
        var existing = hdr.querySelector('.ve-pd-ats-badge');
        var ats = data.ats || {};
        if (ats.state === null || ats.state === undefined) {
            if (existing) existing.remove();
            return;
        }
        var color = ats.state === 1 ? 'var(--ve-blue)'
                  : ats.state === 2 ? 'var(--ve-orange)'
                  : 'var(--ve-red)';
        var label = ats.label || ('ATS ' + ats.state);
        if (!existing) {
            existing = document.createElement('span');
            existing.className = 've-pd-ats-badge';
            existing.style.cssText = 'font-size:0.75rem;padding:2px 8px;border-radius:4px;'
                + 'background:rgba(255,255,255,0.08);border:1px solid;margin-left:8px';
            hdr.querySelector('.ve-phase-detective-title').appendChild(existing);
        }
        existing.textContent = label;
        existing.style.borderColor = color;
        existing.style.color = color;
    }

    function refreshPreconds() {
        if (running) return;  // status endpoint already includes them when running
        fetch('/api/devices/' + device.id + '/detect-phase')
            .then(function(r) { return r.json(); })
            .then(function(data) {
                renderPreconds(data.preconditions);
                _renderAtsBadge(data);
            })
            .catch(function() {});
    }

    function pollStatus() {
        fetch('/api/devices/' + device.id + '/detect-phase')
            .then(function(r) { return r.json(); })
            .then(function(data) {
                renderLogs(data.logs || []);
                _renderAtsBadge(data);
                var stepText = data.current_step ? 'Schritt: ' + data.current_step : '';
                if (data.total_runs && data.total_runs > 1 && data.current_run) {
                    stepText = 'Run ' + data.current_run + '/' + data.total_runs
                             + (stepText ? ' · ' + stepText : '');
                }
                stepEl.textContent = stepText;
                if (data.status === 'complete' || data.status === 'error') {
                    clearInterval(pollTimer);
                    pollTimer = null;
                    running = false;
                    btn.disabled = false;
                    btn.textContent = 'Erneut erkennen';
                    if (data.status === 'error') {
                        renderResultError(data.error);
                    } else {
                        renderResult(data.result);
                    }
                    refreshPreconds();
                }
            })
            .catch(function() {});
    }

    function renderLogs(logs) {
        var html = '';
        for (var i = 0; i < logs.length; i++) {
            var l = logs[i];
            var cls = l.level === 'error' ? 've-pd-log-error'
                    : l.level === 'warn' ? 've-pd-log-warn' : '';
            html += '<li class="' + cls + '">' + esc(l.msg) + '</li>';
        }
        logEl.innerHTML = html;
        logEl.scrollTop = logEl.scrollHeight;
    }

    function renderResult(result) {
        if (!result) {
            resultEl.style.display = 'none';
            return;
        }
        resultEl.style.display = '';
        var medians = result.medians || [];
        var medianText = medians.length === 3
            ? 'Peak Δ: L1=' + Math.round(medians[0]) + ' W, L2=' + Math.round(medians[1])
              + ' W, L3=' + Math.round(medians[2]) + ' W'
            : '';
        var rampText = (result.ramp_down_time_s !== null && result.ramp_down_time_s !== undefined)
            ? ' · Rampdown ' + result.ramp_down_time_s.toFixed(1) + ' s'
            : '';
        // Multi-run consensus annotation: in verify mode (N=3) show the
        // per-run vote tally so the user can see whether the answer was
        // 3/3 or 2/3 (tie-break, weaker confidence) etc.
        var consensusText = '';
        if (result.n_runs && result.n_runs > 1) {
            consensusText = ' · Konsens ' + (result.consensus_count || 0)
                          + '/' + (result.n_successful || result.n_runs)
                          + (result.consensus_strong ? ' (eindeutig)' : ' (Mehrheit)');
        }
        if (result.detected_phase) {
            // Three accepted-result cases:
            //   1. auto-applied (entry was implicit, server wrote config) —
            //      success card, no buttons, sync local state.
            //   2. verify mismatch (entry was already explicit + saved
            //      phase differs from detected) — warning card with an
            //      explicit "Überschreiben" button. We intentionally don't
            //      auto-override because small-inverter detection isn't
            //      reproducible enough to trust silently on a re-run.
            //   3. verify match — informational green card, no action.
            var autoApplied = result.applied_to_config === true;
            var mismatchPhase = result.verify_mismatch_phase;
            var storedBefore = result.stored_phase_before_run;
            if (autoApplied) {
                resultEl.innerHTML =
                    '<div class="ve-hint-card ve-hint-card--success">' +
                    '  <div class="ve-hint-header">✓ L' + result.detected_phase +
                    '    <span class="ve-text--ok">erkannt &amp; automatisch übernommen</span></div>' +
                    '  <div class="ve-hint-subtext">' + esc(medianText) + esc(rampText) + esc(consensusText) + '</div>' +
                    '</div>';
                if (onAutoApplied) onAutoApplied(result.detected_phase);
            } else if (mismatchPhase) {
                resultEl.innerHTML =
                    '<div class="ve-hint-card">' +
                    '  <div class="ve-hint-header">⚠ Verifikation: erkannt L' + mismatchPhase +
                    ', gespeichert L' + storedBefore + '</div>' +
                    '  <div class="ve-hint-subtext">' + esc(medianText) + esc(rampText) + esc(consensusText) +
                    '<br>Auto-Apply übersprungen — kleine Inverter sind durch Multiplus-Rebalancing nicht immer stabil reproduzierbar. Manuell entscheiden:' +
                    '</div>' +
                    '  <button type="button" class="ve-btn ve-btn--sm ve-btn--primary ve-pd-apply" style="margin-top:8px">Auf L' +
                      mismatchPhase + ' überschreiben</button>' +
                    '</div>';
                var apply = resultEl.querySelector('.ve-pd-apply');
                apply.addEventListener('click', function() {
                    if (phaseSelect) {
                        phaseSelect.value = String(mismatchPhase);
                        if (dirtyCb) dirtyCb();
                        apply.disabled = true;
                        apply.textContent = 'Übernommen — Save Pair nutzen';
                    }
                });
            } else {
                // Verify OK: same phase as stored.
                resultEl.innerHTML =
                    '<div class="ve-hint-card ve-hint-card--success">' +
                    '  <div class="ve-hint-header">✓ Verifikation OK — L' + result.detected_phase + '</div>' +
                    '  <div class="ve-hint-subtext">' + esc(medianText) + esc(rampText) + esc(consensusText) + '</div>' +
                    '</div>';
            }
        } else {
            var verdict = result.verdict || {};
            var why = [];
            if (verdict.floor_ok === false) {
                why.push('Residual ' + Math.round(verdict.winner_residual_w || 0) + ' W unter Schwelle '
                       + Math.round(verdict.abs_threshold_w || 0) + ' W');
            }
            if (verdict.dominance_ok === false) {
                why.push('keine klare Dominanz (Verhältnis ' + (verdict.dominance_ratio || 0).toFixed(1) + '×)');
            }
            resultEl.innerHTML =
                '<div class="ve-hint-card">' +
                '  <div class="ve-hint-header">Keine eindeutige Phase erkannt</div>' +
                '  <div class="ve-hint-subtext">' + esc(medianText) + esc(rampText) + esc(consensusText) + '<br>'
                + (why.length ? esc('Grund: ' + why.join('; ')) : 'Versuch es bei höherer PV-Produktion oder ordne die Phase manuell zu.') +
                '</div>' +
                '</div>';
        }
    }

    function renderResultError(err) {
        resultEl.style.display = '';
        resultEl.innerHTML =
            '<div class="ve-hint-card ve-hint-card--error">' +
            '  <div class="ve-hint-header">Fehler bei der Erkennung</div>' +
            '  <div class="ve-hint-subtext">' + esc(err || 'unbekannt') + '</div>' +
            '</div>';
    }

    btn.addEventListener('click', function() {
        if (running) return;
        running = true;
        btn.disabled = true;
        btn.textContent = 'Läuft …';
        resultEl.style.display = 'none';
        resultEl.innerHTML = '';
        prog.style.display = '';
        logEl.innerHTML = '<li>Starte Erkennung …</li>';
        fetch('/api/devices/' + device.id + '/detect-phase', { method: 'POST' })
            .then(function(r) { return r.json(); })
            .then(function(data) {
                if (data.success === false) {
                    running = false;
                    btn.disabled = false;
                    btn.textContent = 'Erkennung starten';
                    if (data.preconditions) renderPreconds(data.preconditions);
                    renderResultError(data.error);
                    return;
                }
                pollTimer = setInterval(pollStatus, 1500);
                pollStatus();
            })
            .catch(function(e) {
                running = false;
                btn.disabled = false;
                btn.textContent = 'Erkennung starten';
                renderResultError(String(e));
            });
    });

    // Initial precondition check + light periodic refresh while not running.
    refreshPreconds();
    precondTimer = setInterval(refreshPreconds, 5000);
}

