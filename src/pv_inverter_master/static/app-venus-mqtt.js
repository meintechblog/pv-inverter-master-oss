// ===== Venus OS Page =====

function renderVenusPage(container) {
    container.innerHTML =
        '<div class="ve-spinner-wrap"><div class="ve-spinner"></div><span class="ve-spinner-label">Loading Venus OS...</span></div>';

    // Load config + use last snapshot for status
    Promise.all([
        fetch('/api/config').then(function(r) { return r.json(); }),
        fetch('/api/devices').then(function(r) { return r.json(); })
    ])
    .then(function(results) {
        var config = results[0];
        var deviceData = results[1];
        var venusDevice = null;
        if (deviceData.devices) {
            for (var i = 0; i < deviceData.devices.length; i++) {
                if (deviceData.devices[i].type === 'venus') { venusDevice = deviceData.devices[i]; break; }
            }
        }
        container.innerHTML = '';
        buildVenusPage(container, config, venusDevice);
    })
    .catch(function(err) {
        container.innerHTML = '<div class="ve-hint-card"><div class="ve-hint-header">Failed to load Venus OS data</div><p class="ve-hint-subtext">' + err.message + '</p></div>';
    });
}

function updateMqttPubStatusDot() {
    var dot = document.querySelector('.ve-mqtt-pub-status-dot');
    if (dot) dot.style.background = _mqttPubConnected ? 'var(--ve-green)' : 'var(--ve-red)';
    // Page-level dot (on MQTT Pub page)
    var pageDot = document.querySelector('.ve-mqtt-pub-page-dot');
    if (pageDot) {
        pageDot.className = 've-dot ' + (_mqttPubConnected ? 've-dot--ok' : 've-dot--err') + ' ve-mqtt-pub-page-dot';
    }
    var pageText = document.querySelector('.ve-mqtt-pub-page-text');
    if (pageText) pageText.textContent = _mqttPubConnected ? 'Connected' : 'Disconnected';
}

function _formatBytes(bytes) {
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1048576) return (bytes / 1024).toFixed(1) + ' KB';
    return (bytes / 1048576).toFixed(1) + ' MB';
}

function _timeAgo(ts) {
    if (!ts) return 'never';
    var secs = Math.floor(Date.now() / 1000 - ts);
    if (secs < 5) return 'just now';
    if (secs < 60) return secs + 's ago';
    if (secs < 3600) return Math.floor(secs / 60) + 'm ago';
    return Math.floor(secs / 3600) + 'h ago';
}

function updateMqttPubStats() {
    var el = document.querySelector('.ve-mqtt-pub-stats');
    if (!el) return;
    if (!_mqttPubStats) { el.innerHTML = ''; return; }
    var s = _mqttPubStats;
    el.innerHTML =
        '<div class="ve-mqtt-pub-stat"><span class="ve-text-dim">Messages</span><span>' + s.messages.toLocaleString() + '</span></div>' +
        '<div class="ve-mqtt-pub-stat"><span class="ve-text-dim">Data sent</span><span>' + _formatBytes(s.bytes) + '</span></div>' +
        '<div class="ve-mqtt-pub-stat"><span class="ve-text-dim">Skipped (dedup)</span><span>' + s.skipped.toLocaleString() + '</span></div>' +
        '<div class="ve-mqtt-pub-stat"><span class="ve-text-dim">Last publish</span><span>' + _timeAgo(s.last_ts) + '</span></div>';
}

function renderInfluxPage(container) {
    container.innerHTML =
        '<div class="ve-spinner-wrap"><div class="ve-spinner"></div><span class="ve-spinner-label">Loading InfluxDB config...</span></div>';
    fetch('/api/config')
        .then(function(r) { return r.json(); })
        .then(function(config) {
            container.innerHTML = '';
            buildInfluxPage(container, config);
        })
        .catch(function(err) {
            container.innerHTML = '<div class="ve-hint-card"><div class="ve-hint-header">Failed to load InfluxDB config</div><p class="ve-hint-subtext">' + esc(err.message) + '</p></div>';
        });
}

function buildInfluxPage(container, config) {
    var influx = config.influx || {};

    // Status + live stats card
    var statusCard = document.createElement('div');
    statusCard.className = 've-card';
    var conn = _influxConnected ? 'Connected' : 'Disconnected';
    var dotClass = _influxConnected ? 've-dot--ok' : 've-dot--err';
    statusCard.innerHTML =
        '<h2 class="ve-card-title">InfluxDB Logging</h2>' +
        '<div class="ve-status-row"><span class="ve-dot ' + dotClass + ' ve-influx-page-dot"></span><span class="ve-influx-page-text">' + conn + '</span></div>' +
        '<div class="ve-mqtt-pub-stats ve-influx-stats"></div>';
    container.appendChild(statusCard);
    updateInfluxStats();

    // Config panel
    var panel = document.createElement('div');
    panel.className = 've-panel ve-influx-panel';
    var orig = {
        url: influx.url || '',
        token: influx.token || '',
        org: influx.org || '',
        bucket: influx.bucket || '',
        interval_s: String(influx.interval_s || 10)
    };
    var intervalOpts = [2, 5, 10, 30, 60].map(function(s) {
        return '<option value="' + s + '"' + (orig.interval_s === String(s) ? ' selected' : '') +
            '>' + s + 's' + (s === 10 ? ' (default)' : '') + '</option>';
    }).join('');
    panel.innerHTML =
        '<div class="ve-panel-header">' +
        '  <h2><span class="ve-dot ve-influx-status-dot"></span> InfluxDB</h2>' +
        '  <span class="ve-btn-pair ve-influx-save-pair" style="display:none">' +
        '    <button type="button" class="ve-btn ve-btn--sm ve-btn--cancel ve-influx-cancel">Cancel</button>' +
        '    <button type="button" class="ve-btn ve-btn--sm ve-btn--save ve-influx-save">Save</button>' +
        '  </span>' +
        '</div>' +
        '<div class="ve-form-group"><label>Enable</label><label class="ve-toggle"><input type="checkbox" class="ve-influx-enabled"><span class="ve-toggle-track"></span></label></div>' +
        '<div class="ve-form-group"><label>URL</label><input type="text" class="ve-input ve-influx-url" value="' + esc(orig.url) + '" placeholder="http://192.0.2.70:8086"></div>' +
        '<div class="ve-form-group"><label>API Token</label><input type="password" class="ve-input ve-influx-token" value="' + esc(orig.token) + '" placeholder="InfluxDB API token"></div>' +
        '<div class="ve-form-group"><label>Organization</label><input type="text" class="ve-input ve-influx-org" value="' + esc(orig.org) + '" placeholder="my-org"></div>' +
        '<div class="ve-form-group"><label>Bucket</label><input type="text" class="ve-input ve-influx-bucket" value="' + esc(orig.bucket) + '" placeholder="default"></div>' +
        '<div class="ve-form-group"><label>Write Interval</label><select class="ve-input ve-influx-interval">' + intervalOpts + '</select></div>';
    container.appendChild(panel);

    // Enable toggle — instant save
    var enabledCb = panel.querySelector('.ve-influx-enabled');
    enabledCb.checked = !!influx.enabled;
    enabledCb.addEventListener('change', function() {
        fetch('/api/config', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ influx: { enabled: enabledCb.checked } })
        })
        .then(function(r) { return r.json(); })
        .then(function(d) {
            if (d.success) showToast('InfluxDB Logging: ' + (enabledCb.checked ? 'On' : 'Off'), 'success');
            else { showToast('Failed: ' + d.error, 'error'); enabledCb.checked = !enabledCb.checked; }
        })
        .catch(function(e) { showToast('Failed: ' + e.message, 'error'); enabledCb.checked = !enabledCb.checked; });
    });

    // Dirty tracking
    var fUrl = panel.querySelector('.ve-influx-url'),
        fToken = panel.querySelector('.ve-influx-token'),
        fOrg = panel.querySelector('.ve-influx-org'),
        fBucket = panel.querySelector('.ve-influx-bucket'),
        fInterval = panel.querySelector('.ve-influx-interval'),
        savePair = panel.querySelector('.ve-influx-save-pair');
    function checkDirty() {
        var fields = [
            { el: fUrl, o: orig.url }, { el: fToken, o: orig.token },
            { el: fOrg, o: orig.org }, { el: fBucket, o: orig.bucket },
            { el: fInterval, o: orig.interval_s }
        ];
        var dirty = false;
        fields.forEach(function(f) {
            if (f.el.value !== f.o) { dirty = true; f.el.classList.add('ve-input--dirty'); }
            else f.el.classList.remove('ve-input--dirty');
        });
        savePair.style.display = dirty ? '' : 'none';
    }
    [fUrl, fToken, fOrg, fBucket, fInterval].forEach(function(el) {
        el.addEventListener('input', checkDirty);
    });

    panel.querySelector('.ve-influx-cancel').addEventListener('click', function() {
        fUrl.value = orig.url; fToken.value = orig.token; fOrg.value = orig.org;
        fBucket.value = orig.bucket; fInterval.value = orig.interval_s;
        checkDirty();
    });

    panel.querySelector('.ve-influx-save').addEventListener('click', function() {
        var btn = panel.querySelector('.ve-influx-save');
        btn.textContent = 'Saving...'; btn.disabled = true;
        var payload = { influx: {
            url: fUrl.value.trim(), token: fToken.value.trim(),
            org: fOrg.value.trim(), bucket: fBucket.value.trim(),
            interval_s: parseInt(fInterval.value) || 10
        }};
        fetch('/api/config', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        })
        .then(function(r) { return r.json(); })
        .then(function(d) {
            if (d.success) {
                showToast('InfluxDB config saved', 'success');
                orig.url = payload.influx.url; orig.token = payload.influx.token;
                orig.org = payload.influx.org; orig.bucket = payload.influx.bucket;
                orig.interval_s = String(payload.influx.interval_s);
                checkDirty();
            } else showToast('Save failed: ' + d.error, 'error');
        })
        .catch(function(e) { showToast('Save failed: ' + e.message, 'error'); })
        .finally(function() { btn.textContent = 'Save'; btn.disabled = false; });
    });

    // Live stats: poll /api/devices every 3s while this page is shown.
    if (_influxStatsInterval) clearInterval(_influxStatsInterval);
    _influxStatsInterval = setInterval(function() {
        if (!document.querySelector('.ve-influx-stats')) {
            clearInterval(_influxStatsInterval); _influxStatsInterval = null; return;
        }
        fetch('/api/devices').then(function(r) { return r.json(); }).then(function(data) {
            (data.devices || []).forEach(function(dev) {
                if (dev.type === 'influx') {
                    _influxConnected = dev.connection_state === 'connected';
                    _influxStats = dev.stats || null;
                }
            });
            updateInfluxStatusDot();
            updateInfluxStats();
        }).catch(function() {});
    }, 3000);
}

function updateInfluxStatusDot() {
    var dot = document.querySelector('.ve-influx-status-dot');
    if (dot) dot.style.background = _influxConnected ? 'var(--ve-green)' : 'var(--ve-red)';
    var pageDot = document.querySelector('.ve-influx-page-dot');
    if (pageDot) pageDot.className = 've-dot ' + (_influxConnected ? 've-dot--ok' : 've-dot--err') + ' ve-influx-page-dot';
    var pageText = document.querySelector('.ve-influx-page-text');
    if (pageText) pageText.textContent = _influxConnected ? 'Connected' : 'Disconnected';
}

function updateInfluxStats() {
    var el = document.querySelector('.ve-influx-stats');
    if (!el) return;
    if (!_influxStats) { el.innerHTML = ''; return; }
    var s = _influxStats;
    var errStyle = (s.errors || 0) > 0 ? ' style="color:var(--ve-warning)"' : '';
    el.innerHTML =
        '<div class="ve-mqtt-pub-stat"><span class="ve-text-dim">Points written</span><span>' + (s.points || 0).toLocaleString() + '</span></div>' +
        '<div class="ve-mqtt-pub-stat"><span class="ve-text-dim">Data written</span><span>' + _formatBytes(s.bytes || 0) + '</span></div>' +
        '<div class="ve-mqtt-pub-stat"><span class="ve-text-dim">Write requests</span><span>' + (s.writes || 0).toLocaleString() + '</span></div>' +
        '<div class="ve-mqtt-pub-stat"><span class="ve-text-dim">Errors</span><span' + errStyle + '>' + (s.errors || 0).toLocaleString() + '</span></div>' +
        '<div class="ve-mqtt-pub-stat"><span class="ve-text-dim">Last write</span><span>' + _timeAgo(s.last_ts) + '</span></div>' +
        (s.last_error ? '<div class="ve-mqtt-pub-stat"><span class="ve-text-dim">Last error</span><span style="color:var(--ve-warning)">' + esc(s.last_error) + '</span></div>' : '');
}

function renderMqttTopicPreview() {
    var list = document.querySelector('.ve-mqtt-pub-topic-list');
    if (!list) return;
    var prefixInput = document.querySelector('.ve-mqtt-pub-prefix');
    var prefix = prefixInput ? prefixInput.value.trim() : 'pv-inverter-master';
    if (!prefix) prefix = 'pv-inverter-master';
    var html = '';
    // Device topics
    for (var i = 0; i < _lastDeviceList.length; i++) {
        var dev = _lastDeviceList[i];
        if (dev.type === 'venus' || dev.type === 'virtual' || dev.type === 'mqtt_pub') continue;
        html += '<div class="ve-mqtt-pub-topic-item">' +
            '<span class="ve-mqtt-pub-topic-label">' + (dev.name || dev.id) + '</span>' +
            '<code class="ve-mqtt-pub-topic-path">' + prefix + '/device/' + dev.id + '/state</code>' +
            '</div>';
    }
    // Virtual PV
    html += '<div class="ve-mqtt-pub-topic-item">' +
        '<span class="ve-mqtt-pub-topic-label">Virtual PV</span>' +
        '<code class="ve-mqtt-pub-topic-path">' + prefix + '/virtual/state</code>' +
        '</div>';
    // LWT / availability
    html += '<div class="ve-mqtt-pub-topic-item">' +
        '<span class="ve-mqtt-pub-topic-label">Availability (LWT)</span>' +
        '<code class="ve-mqtt-pub-topic-path">' + prefix + '/status</code>' +
        '</div>';
    list.innerHTML = html;
}

// ===== Venus Health Dashboard helpers =====
// 4 cards inserted between the top MQTT-status row and the config panel.
// Cards are built once; the 5s poll loop only patches individual cell
// .textContent / .className so it never blows away DOM state.

var _VENUS_PEER_TYPES = [
    { key: 'grid_meter', label: 'Grid-Meter' },
    { key: 'pvinverter', label: 'PV-Wechselrichter (unser Proxy)' },
    { key: 'vebus', label: 'Multiplus (VE.Bus)' },
    { key: 'solarcharger', label: 'MPPT Solarcharger' },
    { key: 'battery', label: 'Batterie' }
];

function _venusHealthCellHTML(cls, label, valueCls) {
    return '<div><label>' + label + '</label><span class="' + valueCls + '">—</span></div>';
}

function _peerHeadline(key, p) {
    var name = p.product_name || ('Instance ' + (p.device_instance != null ? p.device_instance : '?'));
    var parts = [name];
    if (key === 'grid_meter' || key === 'pvinverter') {
        if (p.ac_power_w != null) parts.push(formatW(p.ac_power_w));
    } else if (key === 'vebus') {
        if (p.dc_power_w != null) parts.push('DC ' + formatW(p.dc_power_w));
        if (p.ac_out_power_w != null) parts.push('AC-out ' + formatW(p.ac_out_power_w));
    } else if (key === 'solarcharger') {
        if (p.dc_power_w != null) parts.push(formatW(p.dc_power_w));
    } else if (key === 'battery') {
        if (p.soc_pct != null) parts.push(Math.round(p.soc_pct) + '% SoC');
        if (p.dc_power_w != null) parts.push(formatW(p.dc_power_w));
        if (p.count > 1) parts.push(p.count + ' Geräte');
    }
    return parts.join(' · ');
}

function _renderPeerDetails(row, key, p) {
    // For peer classes that legitimately have multiple instances (batteries:
    // aggregator + per-BMS), render a small sub-list under the headline so
    // the user sees every member. Other peer classes typically have a single
    // device — for those we still surface the list when count > 1 (rare).
    var list = row.querySelector('.ve-venus-health-peer-list');
    if (!p || !p.devices || p.devices.length <= 1) {
        if (list) list.style.display = 'none';
        return;
    }
    if (!list) {
        list = document.createElement('ul');
        list.className = 've-venus-health-peer-list';
        row.appendChild(list);
    }
    var html = '';
    for (var k = 0; k < p.devices.length; k++) {
        var d = p.devices[k];
        var lbl = d.product_name || ('Instance ' + d.device_instance);
        var extras = [];
        if (d.device_instance != null) extras.push('#' + d.device_instance);
        if (d.soc_pct != null) extras.push(Math.round(d.soc_pct) + '%');
        if (d.dc_power_w != null) extras.push(formatW(d.dc_power_w));
        if (d.ac_power_w != null) extras.push(formatW(d.ac_power_w));
        var extraStr = extras.length ? ' (' + extras.join(' · ') + ')' : '';
        html += '<li>' + esc(lbl) + esc(extraStr) + '</li>';
    }
    list.innerHTML = html;
    list.style.display = '';
}

function _buildVenusHealthCards(container) {
    // ----- Card 1: Venus OS System ----------------------------------------
    var sysCard = document.createElement('div');
    sysCard.className = 've-card venus-dependent ve-venus-health-card ve-venus-health-system';
    sysCard.innerHTML =
        '<h2 class="ve-card-title">Venus OS System</h2>' +
        '<div class="ve-grid">' +
            _venusHealthCellHTML('', 'Portal-ID', 've-venus-health-portal') +
            _venusHealthCellHTML('', 'Seriennummer', 've-venus-health-serial') +
            _venusHealthCellHTML('', 'Firmware', 've-venus-health-firmware') +
            _venusHealthCellHTML('', 'Hostname', 've-venus-health-hostname') +
            _venusHealthCellHTML('', 'Verbunden seit', 've-venus-health-since') +
        '</div>';
    container.appendChild(sysCard);

    // ----- Card 2: Kopplung -----------------------------------------------
    var coupCard = document.createElement('div');
    coupCard.className = 've-card venus-dependent ve-venus-health-card ve-venus-health-coupling';
    coupCard.innerHTML =
        '<h2 class="ve-card-title ve-venus-health-coupling-title">Kopplung</h2>' +
        '<div class="ve-grid">' +
            '<div><label>Status</label><span><span class="ve-dot ve-dot--dim ve-venus-health-coupling-dot"></span> <span class="ve-venus-health-coupling-status">—</span></span></div>' +
            _venusHealthCellHTML('', 'Position', 've-venus-health-position') +
            '<div><label>Limiter</label><span><span class="ve-dot ve-dot--dim ve-venus-health-limiter-dot"></span> <span class="ve-venus-health-limiter-text">—</span></span></div>' +
            '<div><label>Verbindung</label><span><code class="ve-venus-health-conn">—</code></span></div>' +
            _venusHealthCellHTML('', 'Max Leistung', 've-venus-health-max-power') +
            _venusHealthCellHTML('', 'Aktuelles Limit', 've-venus-health-power-limit') +
        '</div>';
    container.appendChild(coupCard);

    // ----- Card 3: Erkannte Venus-Geräte -----------------------------------
    var peersCard = document.createElement('div');
    peersCard.className = 've-card venus-dependent ve-venus-health-card ve-venus-health-peers';
    var peersHtml = '<h2 class="ve-card-title">Erkannte Venus-Geräte</h2>';
    for (var i = 0; i < _VENUS_PEER_TYPES.length; i++) {
        var pt = _VENUS_PEER_TYPES[i];
        peersHtml +=
            '<div class="ve-venus-health-peer-row ve-venus-health-peer-' + pt.key + '">' +
                '<span class="ve-dot ve-dot--dim"></span>' +
                '<span class="ve-venus-health-peer-label">' + esc(pt.label) + '</span>' +
                '<span class="ve-venus-health-peer-sub"></span>' +
            '</div>';
    }
    peersCard.innerHTML = peersHtml;
    container.appendChild(peersCard);

    // ----- Card 4: Steuerung & Regelung -----------------------------------
    var ctrlCard = document.createElement('div');
    ctrlCard.className = 've-card venus-dependent ve-venus-health-card ve-venus-health-control';
    ctrlCard.innerHTML =
        '<h2 class="ve-card-title">Steuerung &amp; Regelung</h2>' +
        '<div class="ve-grid">' +
            '<div><label>Einspeisung erlaubt</label>' +
                '<span class="ve-venus-health-feedin-row">' +
                    '<span class="ve-dot ve-dot--dim ve-venus-health-feedin-dot"></span>' +
                    '<span class="ve-venus-health-feedin">—</span>' +
                '</span>' +
            '</div>' +
            _venusHealthCellHTML('', 'Feed-in-Limit', 've-venus-health-feedin-cap') +
            '<div><label>Aktive Drossel</label>' +
                '<span class="ve-venus-health-throttle-row">' +
                    '<span class="ve-dot ve-dot--dim ve-venus-health-throttle-dot"></span>' +
                    '<span class="ve-venus-health-throttle">—</span>' +
                '</span>' +
            '</div>' +
        '</div>' +
        '<div class="ve-hint-card ve-venus-health-missing">' +
            '<div class="ve-hint-header">Für aktive PV-Regelung fehlt:</div>' +
            '<ol></ol>' +
        '</div>';
    container.appendChild(ctrlCard);
}

function _refreshVenusHealth() {
    fetch('/api/venus/health')
        .then(function(r) { return r.json(); })
        .then(_applyVenusHealth)
        .catch(function() { /* swallow — banner already reflects connect state */ });
}

function _setText(sel, value) {
    var el = document.querySelector(sel);
    if (el) el.textContent = (value === null || value === undefined || value === '') ? '—' : String(value);
}

function _setDotClass(sel, dotClass) {
    var el = document.querySelector(sel);
    if (!el) return;
    el.className = 've-dot ' + dotClass + ' ' + sel.replace(/^\./, '');
}

function _applyVenusHealth(data) {
    if (!data) return;

    // ----- Connection banner (top of container, above MQTT-status card) ----
    var container = document.querySelector('.ve-venus-health-system');
    container = container ? container.parentNode : null;
    if (container) {
        var banner = container.querySelector('.ve-venus-health-banner');
        if (data.mqtt && data.mqtt.configured && !data.mqtt.connected) {
            if (!banner) {
                banner = document.createElement('div');
                banner.className = 've-hint-card ve-venus-health-banner';
                banner.textContent = 'Venus nicht erreichbar — Werte sind möglicherweise veraltet';
                container.insertBefore(banner, container.firstChild);
            }
        } else if (banner) {
            banner.parentNode.removeChild(banner);
        }
    }

    // ----- Card 1: System -------------------------------------------------
    var sys = data.system || {};
    _setText('.ve-venus-health-portal', (data.mqtt && data.mqtt.portal_id) || null);
    _setText('.ve-venus-health-serial', sys.serial);
    _setText('.ve-venus-health-firmware', sys.firmware);
    _setText('.ve-venus-health-hostname', sys.hostname);
    var since = (data.mqtt && data.mqtt.connected_since_ts) ? _timeAgo(data.mqtt.connected_since_ts) : '—';
    _setText('.ve-venus-health-since', since);

    // ----- Card 2: Kopplung ------------------------------------------------
    var coup = data.coupling || {};
    var coupTitle = document.querySelector('.ve-venus-health-coupling-title');
    if (coupTitle) {
        coupTitle.textContent = 'Kopplung «' + (coup.custom_name || 'unbenannt') + '»';
    }
    var coupDot = document.querySelector('.ve-venus-health-coupling-dot');
    if (coupDot) {
        coupDot.className = 've-dot ' + (coup.registered ? 've-dot--ok' : 've-dot--err') + ' ve-venus-health-coupling-dot';
    }
    _setText('.ve-venus-health-coupling-status', coup.registered ? 'Registriert' : 'Nicht registriert');
    _setText('.ve-venus-health-position', coup.position_label || null);

    // Limiter dot: green = enabled, orange = supported-but-off,
    // red = not supported, dim = unknown.
    var limClass = 've-dot--dim';
    var limText = 'Unbekannt';
    if (coup.limiter_enabled === true) {
        limClass = 've-dot--ok';
        limText = 'Aktiv';
    } else if (coup.limiter_supported === true && coup.limiter_enabled === false) {
        limClass = 've-dot--warn';
        limText = 'Unterstützt, aus';
    } else if (coup.limiter_supported === false) {
        limClass = 've-dot--err';
        limText = 'Nicht unterstützt';
    }
    var limDot = document.querySelector('.ve-venus-health-limiter-dot');
    if (limDot) limDot.className = 've-dot ' + limClass + ' ve-venus-health-limiter-dot';
    _setText('.ve-venus-health-limiter-text', limText);

    _setText('.ve-venus-health-conn', coup.connection_string);
    _setText('.ve-venus-health-max-power', coup.max_power_w != null ? formatW(coup.max_power_w) : null);
    _setText('.ve-venus-health-power-limit', coup.power_limit_w != null ? formatW(coup.power_limit_w) : null);

    // ----- Card 3: Peers ---------------------------------------------------
    // Core peers (Grid-Meter, PV-Inverter, Multiplus, Battery) are always
    // rendered so users see whether the expected ESS components are wired up.
    // Optional peers (MPPT Solarcharger) are hidden when not present — they
    // are only relevant if the user actually has a DC-coupled MPPT, so the
    // greyed-out perpetual row is noise for the typical AC-coupled setup.
    var OPTIONAL_PEERS = { solarcharger: true };
    var peers = data.peers || {};
    for (var i = 0; i < _VENUS_PEER_TYPES.length; i++) {
        var pt = _VENUS_PEER_TYPES[i];
        var p = peers[pt.key] || {};
        var row = document.querySelector('.ve-venus-health-peer-' + pt.key);
        if (!row) continue;
        var hide = OPTIONAL_PEERS[pt.key] === true && !p.present;
        row.style.display = hide ? 'none' : '';
        if (hide) continue;
        var dot = row.querySelector('.ve-dot');
        var sub = row.querySelector('.ve-venus-health-peer-sub');
        if (dot) dot.className = 've-dot ' + (p.present ? 've-dot--ok' : 've-dot--dim');
        var subText = '';
        if (p.present) {
            subText = _peerHeadline(pt.key, p);
        } else {
            subText = 'nicht erkannt';
        }
        if (sub) sub.textContent = subText;
        _renderPeerDetails(row, pt.key, p);
    }

    // ----- Card 4: Control + missing list ---------------------------------
    var ctrl = data.control || {};
    // Aktive Drossel — context-aware. Even when feed-in is allowed Venus
    // sends WMaxLimPct=100 every 5s as a keepalive — that's NOT a throttle,
    // so the old "Letzter Throttle-Befehl: just now · 100%" was misleading.
    var throttleDot = document.querySelector('.ve-venus-health-throttle-dot');
    var throttleTxt = document.querySelector('.ve-venus-health-throttle');
    if (throttleDot && throttleTxt) {
        var pct = ctrl.last_throttle_pct;
        var maxW = (data.coupling && data.coupling.max_power_w) || null;
        if (pct == null) {
            throttleDot.className = 've-dot ve-dot--dim ve-venus-health-throttle-dot';
            throttleTxt.textContent = 'noch nichts empfangen';
        } else if (pct >= 100) {
            throttleDot.className = 've-dot ve-dot--ok ve-venus-health-throttle-dot';
            throttleTxt.textContent = 'kein Cap (100% freigegeben)';
        } else {
            throttleDot.className = 've-dot ve-dot--warn ve-venus-health-throttle-dot';
            var capW = maxW != null ? ' (' + formatW(maxW * pct / 100) + ')' : '';
            var ago = _timeAgo(ctrl.last_throttle_command_ts);
            throttleTxt.textContent = pct + '%' + capW + ' · ' + ago;
        }
    }

    // Feed-in master switch (Venus PreventFeedback). Green dot = allowed,
    // red dot = blocked (= Venus actively throttles PV to zero feed-in).
    var feedDot = document.querySelector('.ve-venus-health-feedin-dot');
    var feedTxt = document.querySelector('.ve-venus-health-feedin');
    if (feedDot && feedTxt) {
        if (ctrl.feed_in_allowed === true) {
            feedDot.className = 've-dot ve-dot--ok ve-venus-health-feedin-dot';
            feedTxt.textContent = 'ja';
        } else if (ctrl.feed_in_allowed === false) {
            feedDot.className = 've-dot ve-dot--err ve-venus-health-feedin-dot';
            feedTxt.textContent = 'NEIN — Venus blockiert';
        } else {
            feedDot.className = 've-dot ve-dot--dim ve-venus-health-feedin-dot';
            feedTxt.textContent = '—';
        }
    }
    _setText('.ve-venus-health-feedin-cap', (ctrl.max_feed_in_w != null)
        ? formatW(ctrl.max_feed_in_w)
        : 'unbegrenzt');

    var missCard = document.querySelector('.ve-venus-health-missing');
    if (missCard) {
        var items = (ctrl.missing_for_active_regulation || []);
        if (items.length === 0) {
            missCard.className = 've-hint-card ve-hint-card--success ve-venus-health-missing';
            missCard.innerHTML =
                '<div class="ve-hint-header">PV-Regelung aktiv</div>';
        } else {
            missCard.className = 've-hint-card ve-venus-health-missing';
            var olHtml = '';
            for (var j = 0; j < items.length; j++) {
                olHtml += '<li>' + esc(items[j]) + '</li>';
            }
            missCard.innerHTML =
                '<div class="ve-hint-header">Für aktive PV-Regelung fehlt:</div>' +
                '<ol>' + olHtml + '</ol>';
        }
    }
}

function buildVenusPage(container, config, venusDevice) {
    var connState = venusDevice ? venusDevice.connection_state : 'disconnected';
    var connDotClass = connState === 'connected' ? 've-dot--ok' : 've-dot--err';
    var connText = connState === 'connected' ? 'Connected' : 'Disconnected';

    // Section 1: MQTT Status (preserved unchanged from the legacy layout)
    var statusCard = document.createElement('div');
    statusCard.className = 've-card';
    statusCard.innerHTML =
        '<h2 class="ve-card-title">MQTT Connection</h2>' +
        '<div class="ve-status-row"><span class="ve-dot ' + connDotClass + ' ve-venus-mqtt-dot"></span><span class="ve-venus-mqtt-text">' + connText + '</span></div>';
    container.appendChild(statusCard);

    // ----- Venus Health Dashboard ----------------------------------------
    // If Venus OS isn't configured yet, skip the health cards entirely and
    // surface a single hint pointing at the config form below.
    if (!config.venus || !config.venus.host) {
        var noVenusHint = document.createElement('div');
        noVenusHint.className = 've-hint-card';
        noVenusHint.innerHTML =
            '<div class="ve-hint-header">Venus OS nicht konfiguriert</div>' +
            '<p class="ve-hint-subtext">Bitte unten Host + Port eintragen, um die Health-Kacheln zu aktivieren.</p>';
        container.appendChild(noVenusHint);
    } else {
        _buildVenusHealthCards(container);
        // Initial fetch + 5s polling loop (DOM-patch only, no full re-render).
        if (_venusHealthInterval) {
            clearInterval(_venusHealthInterval);
            _venusHealthInterval = null;
        }
        _refreshVenusHealth();
        _venusHealthInterval = setInterval(function() {
            // Auto-stop polling when the user navigates away.
            if (!document.querySelector('.ve-venus-health-system')) {
                clearInterval(_venusHealthInterval);
                _venusHealthInterval = null;
                return;
            }
            _refreshVenusHealth();
        }, 5000);
    }

    // Section: Config form
    var cfgPanel = document.createElement('div');
    cfgPanel.className = 've-panel';

    var origVenus = {
        name: config.venus.name || '',
        host: config.venus.host || '',
        port: String(config.venus.port || 1883)
    };

    cfgPanel.innerHTML =
        '<div class="ve-panel-header">' +
        '  <h2>Venus OS Configuration</h2>' +
        '  <span class="ve-btn-pair ve-venus-save-pair" style="display:none">' +
        '    <button type="button" class="ve-btn ve-btn--sm ve-btn--cancel ve-venus-cancel">Cancel</button>' +
        '    <button type="button" class="ve-btn ve-btn--sm ve-btn--save ve-venus-save">Save</button>' +
        '  </span>' +
        '</div>' +
        '<div class="ve-form-group"><label>Display Name</label><input type="text" class="ve-input ve-venus-name" value="' + esc(origVenus.name) + '" placeholder="e.g. Workshop"></div>' +
        '<div class="ve-form-group"><label>Venus OS IP</label><input type="text" class="ve-input ve-venus-host" value="' + origVenus.host + '" placeholder="e.g. 198.51.100.1"></div>' +
        '<div class="ve-form-group"><label>MQTT Port</label><input type="number" class="ve-input ve-venus-port" value="' + origVenus.port + '" placeholder="1883" min="1" max="65535"></div>';
    container.appendChild(cfgPanel);

    // Dirty tracking for Venus config
    var vName = cfgPanel.querySelector('.ve-venus-name');
    var vHost = cfgPanel.querySelector('.ve-venus-host');
    var vPort = cfgPanel.querySelector('.ve-venus-port');
    var vSavePair = cfgPanel.querySelector('.ve-venus-save-pair');

    function checkVenusDirty() {
        var dirty = vName.value !== origVenus.name || vHost.value !== origVenus.host || vPort.value !== origVenus.port;
        vSavePair.style.display = dirty ? '' : 'none';
        [vName, vHost, vPort].forEach(function(el) {
            var orig = el === vName ? origVenus.name : el === vHost ? origVenus.host : origVenus.port;
            if (el.value !== orig) el.classList.add('ve-input--dirty');
            else el.classList.remove('ve-input--dirty');
        });
    }
    [vName, vHost, vPort].forEach(function(el) { el.addEventListener('input', checkVenusDirty); });

    cfgPanel.querySelector('.ve-venus-cancel').addEventListener('click', function() {
        vName.value = origVenus.name;
        vHost.value = origVenus.host;
        vPort.value = origVenus.port;
        checkVenusDirty();
    });

    cfgPanel.querySelector('.ve-venus-save').addEventListener('click', function() {
        var btn = cfgPanel.querySelector('.ve-venus-save');
        btn.textContent = 'Saving...';
        btn.disabled = true;

        var payload = {
            venus: {
                name: vName.value.trim(),
                host: vHost.value.trim(),
                port: parseInt(vPort.value) || 1883
            }
        };

        fetch('/api/config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        })
        .then(function(res) { return res.json(); })
        .then(function(data) {
            if (data.success) {
                showToast('Configuration saved. Reconnecting...', 'success');
                origVenus.name = payload.venus.name;
                origVenus.host = payload.venus.host;
                origVenus.port = String(payload.venus.port);
                checkVenusDirty();
            } else {
                showToast('Save failed: ' + data.error, 'error');
            }
        })
        .catch(function(e) { showToast('Save failed: ' + e.message, 'error'); })
        .finally(function() {
            btn.textContent = 'Save';
            btn.disabled = false;
        });
    });

}

// ===== MQTT Publishing Page =====

function renderMqttPubPage(container) {
    container.innerHTML =
        '<div class="ve-spinner-wrap"><div class="ve-spinner"></div><span class="ve-spinner-label">Loading MQTT config...</span></div>';

    fetch('/api/config')
        .then(function(r) { return r.json(); })
        .then(function(config) {
            container.innerHTML = '';
            buildMqttPubPage(container, config);
        })
        .catch(function(err) {
            container.innerHTML = '<div class="ve-hint-card"><div class="ve-hint-header">Failed to load MQTT config</div><p class="ve-hint-subtext">' + esc(err.message) + '</p></div>';
        });
}

function buildMqttPubPage(container, config) {
    // Connection status card
    var statusCard = document.createElement('div');
    statusCard.className = 've-card';
    var mqttConn = _mqttPubConnected ? 'Connected' : 'Disconnected';
    var mqttDotClass = _mqttPubConnected ? 've-dot--ok' : 've-dot--err';
    statusCard.innerHTML =
        '<h2 class="ve-card-title">Broker Connection</h2>' +
        '<div class="ve-status-row"><span class="ve-dot ' + mqttDotClass + ' ve-mqtt-pub-page-dot"></span><span class="ve-mqtt-pub-page-text">' + mqttConn + '</span></div>' +
        '<div class="ve-mqtt-pub-stats"></div>';
    container.appendChild(statusCard);
    updateMqttPubStats();

    // Config panel
    var mqttPubPanel = document.createElement('div');
    mqttPubPanel.className = 've-panel ve-mqtt-pub-panel';

    var mqttPub = config.mqtt_publish || {};
    var origMqttPub = {
        host: mqttPub.host || '',
        port: String(mqttPub.port || 1883),
        topic_prefix: mqttPub.topic_prefix || 'pv-inverter-master',
        interval_s: String(mqttPub.interval_s || 5)
    };

    mqttPubPanel.innerHTML =
        '<div class="ve-panel-header">' +
        '  <h2><span class="ve-dot ve-mqtt-pub-status-dot"></span> MQTT Publishing</h2>' +
        '  <span class="ve-btn-pair ve-mqtt-pub-save-pair" style="display:none">' +
        '    <button type="button" class="ve-btn ve-btn--sm ve-btn--cancel ve-mqtt-pub-cancel">Cancel</button>' +
        '    <button type="button" class="ve-btn ve-btn--sm ve-btn--save ve-mqtt-pub-save">Save</button>' +
        '  </span>' +
        '</div>' +
        '<div class="ve-form-group"><label>Enable</label><label class="ve-toggle"><input type="checkbox" class="ve-mqtt-pub-enabled"><span class="ve-toggle-track"></span></label></div>' +
        '<div class="ve-form-group"><label>Broker Host</label>' +
        '  <div class="ve-mqtt-pub-discover-row">' +
        '    <input type="text" class="ve-input ve-mqtt-pub-host" value="' + origMqttPub.host + '" placeholder="e.g. mqtt.example">' +
        '    <button type="button" class="ve-mqtt-pub-discover-btn">Discover</button>' +
        '  </div>' +
        '  <select class="ve-mqtt-pub-broker-select" style="display:none"></select>' +
        '</div>' +
        '<div class="ve-form-group"><label>Port</label><input type="number" class="ve-input ve-mqtt-pub-port" value="' + origMqttPub.port + '" placeholder="1883" min="1" max="65535"></div>' +
        '<div class="ve-form-group"><label>Topic Prefix</label><input type="text" class="ve-input ve-mqtt-pub-prefix" value="' + origMqttPub.topic_prefix + '" placeholder="pv-inverter-master"></div>' +
        '<div class="ve-form-group"><label>Publish Interval</label><select class="ve-input ve-mqtt-pub-interval">' +
        '<option value="1"' + (origMqttPub.interval_s === '1' ? ' selected' : '') + '>1s (debug)</option>' +
        '<option value="2"' + (origMqttPub.interval_s === '2' ? ' selected' : '') + '>2s</option>' +
        '<option value="5"' + (origMqttPub.interval_s === '5' ? ' selected' : '') + '>5s (default)</option>' +
        '<option value="10"' + (origMqttPub.interval_s === '10' ? ' selected' : '') + '>10s</option>' +
        '<option value="30"' + (origMqttPub.interval_s === '30' ? ' selected' : '') + '>30s</option>' +
        '<option value="60"' + (origMqttPub.interval_s === '60' ? ' selected' : '') + '>60s</option>' +
        '</select></div>';
    container.appendChild(mqttPubPanel);

    // MQTT Publishing: enable toggle (instant-save)
    var mqttPubEnabled = mqttPubPanel.querySelector('.ve-mqtt-pub-enabled');
    mqttPubEnabled.checked = !!mqttPub.enabled;
    mqttPubEnabled.addEventListener('change', function() {
        fetch('/api/config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ mqtt_publish: { enabled: mqttPubEnabled.checked } })
        })
        .then(function(res) { return res.json(); })
        .then(function(data) {
            if (data.success) {
                showToast('MQTT Publishing: ' + (mqttPubEnabled.checked ? 'On' : 'Off'), 'success');
            } else {
                showToast('Failed: ' + data.error, 'error');
                mqttPubEnabled.checked = !mqttPubEnabled.checked;
            }
        })
        .catch(function(e) {
            showToast('Failed: ' + e.message, 'error');
            mqttPubEnabled.checked = !mqttPubEnabled.checked;
        });
    });

    // MQTT Publishing: dirty tracking
    var mpHost = mqttPubPanel.querySelector('.ve-mqtt-pub-host');
    var mpPort = mqttPubPanel.querySelector('.ve-mqtt-pub-port');
    var mpPrefix = mqttPubPanel.querySelector('.ve-mqtt-pub-prefix');
    var mpInterval = mqttPubPanel.querySelector('.ve-mqtt-pub-interval');
    var mpSavePair = mqttPubPanel.querySelector('.ve-mqtt-pub-save-pair');

    function checkMqttPubDirty() {
        var dirty = mpHost.value !== origMqttPub.host ||
                    mpPort.value !== origMqttPub.port ||
                    mpPrefix.value !== origMqttPub.topic_prefix ||
                    mpInterval.value !== origMqttPub.interval_s;
        mpSavePair.style.display = dirty ? '' : 'none';
        var fields = [
            { el: mpHost, orig: origMqttPub.host },
            { el: mpPort, orig: origMqttPub.port },
            { el: mpPrefix, orig: origMqttPub.topic_prefix },
            { el: mpInterval, orig: origMqttPub.interval_s }
        ];
        fields.forEach(function(f) {
            if (f.el.value !== f.orig) f.el.classList.add('ve-input--dirty');
            else f.el.classList.remove('ve-input--dirty');
        });
    }
    [mpHost, mpPort, mpPrefix, mpInterval].forEach(function(el) {
        el.addEventListener('input', checkMqttPubDirty);
    });

    // MQTT Publishing: cancel
    mqttPubPanel.querySelector('.ve-mqtt-pub-cancel').addEventListener('click', function() {
        mpHost.value = origMqttPub.host;
        mpPort.value = origMqttPub.port;
        mpPrefix.value = origMqttPub.topic_prefix;
        mpInterval.value = origMqttPub.interval_s;
        checkMqttPubDirty();
    });

    // MQTT Publishing: save
    mqttPubPanel.querySelector('.ve-mqtt-pub-save').addEventListener('click', function() {
        var btn = mqttPubPanel.querySelector('.ve-mqtt-pub-save');
        btn.textContent = 'Saving...';
        btn.disabled = true;

        var payload = {
            mqtt_publish: {
                host: mpHost.value.trim(),
                port: parseInt(mpPort.value) || 1883,
                topic_prefix: mpPrefix.value.trim(),
                interval_s: parseInt(mpInterval.value) || 5
            }
        };

        fetch('/api/config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        })
        .then(function(res) { return res.json(); })
        .then(function(data) {
            if (data.success) {
                showToast('MQTT config saved', 'success');
                origMqttPub.host = payload.mqtt_publish.host;
                origMqttPub.port = String(payload.mqtt_publish.port);
                origMqttPub.topic_prefix = payload.mqtt_publish.topic_prefix;
                origMqttPub.interval_s = String(payload.mqtt_publish.interval_s);
                checkMqttPubDirty();
            } else {
                showToast('Save failed: ' + data.error, 'error');
            }
        })
        .catch(function(e) { showToast('Save failed: ' + e.message, 'error'); })
        .finally(function() {
            btn.textContent = 'Save';
            btn.disabled = false;
        });
    });

    // MQTT Publishing: discover button
    var discoverBtn = mqttPubPanel.querySelector('.ve-mqtt-pub-discover-btn');
    var brokerSelect = mqttPubPanel.querySelector('.ve-mqtt-pub-broker-select');

    discoverBtn.addEventListener('click', function() {
        discoverBtn.textContent = 'Scanning...';
        discoverBtn.disabled = true;
        brokerSelect.style.display = 'none';

        fetch('/api/mqtt/discover', { method: 'POST' })
        .then(function(res) { return res.json(); })
        .then(function(data) {
            if (!data.success) {
                showToast('Discovery failed: ' + (data.error || 'unknown'), 'error');
                return;
            }
            var brokers = data.brokers || [];
            if (brokers.length === 0) {
                showToast('No MQTT brokers found', 'warning');
            } else if (brokers.length === 1) {
                mpHost.value = brokers[0].host;
                if (brokers[0].port) mpPort.value = String(brokers[0].port);
                checkMqttPubDirty();
                showToast('Found broker: ' + brokers[0].host, 'success');
            } else {
                // Multiple brokers: show dropdown
                brokerSelect.innerHTML = '';
                brokers.forEach(function(b) {
                    var opt = document.createElement('option');
                    opt.value = b.host + ':' + (b.port || 1883);
                    opt.textContent = (b.name || b.host) + ' (' + b.host + ':' + (b.port || 1883) + ')';
                    brokerSelect.appendChild(opt);
                });
                brokerSelect.style.display = '';
                // Auto-select first
                var parts = brokerSelect.value.split(':');
                mpHost.value = parts[0];
                mpPort.value = parts[1] || '1883';
                checkMqttPubDirty();
                showToast('Found ' + brokers.length + ' brokers — select one', 'success');

                brokerSelect.addEventListener('change', function() {
                    var p = brokerSelect.value.split(':');
                    mpHost.value = p[0];
                    mpPort.value = p[1] || '1883';
                    checkMqttPubDirty();
                });
            }
        })
        .catch(function(e) { showToast('Discovery failed: ' + e.message, 'error'); })
        .finally(function() {
            discoverBtn.textContent = 'Discover';
            discoverBtn.disabled = false;
        });
    });

    // Section 6: MQTT Topic Preview card
    var topicCard = document.createElement('div');
    topicCard.className = 've-card ve-mqtt-pub-topic-card';
    topicCard.innerHTML =
        '<h2 class="ve-card-title">MQTT Topics</h2>' +
        '<div class="ve-mqtt-pub-topic-list"></div>';
    container.appendChild(topicCard);

    // Re-render topic preview when prefix input changes
    mpPrefix.addEventListener('input', renderMqttTopicPreview);

    // Initial renders: status dot + topic preview
    // Seed _lastDeviceList from config.inverters if no WS data yet
    if (_lastDeviceList.length === 0 && config.inverters) {
        _lastDeviceList = config.inverters.map(function(inv) {
            return { id: inv.id, name: inv.name || inv.id, type: inv.type || 'sunspec', enabled: inv.enabled };
        });
        // Add venus + virtual placeholders
        _lastDeviceList.push({ id: 'venus', name: 'Venus OS', type: 'venus' });
        _lastDeviceList.push({ id: 'virtual', name: config.virtual_inverter ? config.virtual_inverter.name : 'Virtual PV', type: 'virtual' });
    }
    updateMqttPubStatusDot();
    renderMqttTopicPreview();
}

function wireESSToggles(essCard) {
    var acToggle = essCard.querySelector('.ve-ess-ac-excess');
    var dcToggle = essCard.querySelector('.ve-ess-dc-excess');
    var limitToggle = essCard.querySelector('.ve-ess-limit-feedin');
    var feedInDD = essCard.querySelector('.ve-ess-feed-in-dd');
    var invLimitToggle = essCard.querySelector('.ve-ess-limit-inverter');
    var invLimitDD = essCard.querySelector('.ve-ess-max-inverter-dd');

    if (acToggle) acToggle.addEventListener('change', function() {
        acToggle._userChangedAt = Date.now();
        writeVenusDbus('/Settings/CGwacs/PreventFeedback', acToggle.checked ? 0 : 1);
        showToast('AC PV Excess: ' + (acToggle.checked ? 'On' : 'Off'), 'success');
    });
    if (dcToggle) dcToggle.addEventListener('change', function() {
        dcToggle._userChangedAt = Date.now();
        writeVenusDbus('/Settings/CGwacs/OvervoltageFeedIn', dcToggle.checked ? 1 : 0);
        showToast('DC PV Excess: ' + (dcToggle.checked ? 'On' : 'Off'), 'success');
    });
    if (limitToggle) limitToggle.addEventListener('change', function() {
        limitToggle._userChangedAt = Date.now();
        if (limitToggle.checked) {
            writeVenusDbus('/Settings/CGwacs/MaxFeedInPower', 10000);
            showToast('Feed-in limit: ' + formatW(10000), 'success');
        } else {
            writeVenusDbus('/Settings/CGwacs/MaxFeedInPower', -1);
            showToast('Feed-in limit: Off', 'success');
        }
    });
    if (feedInDD) feedInDD.addEventListener('change', function() {
        var watts = parseInt(feedInDD.value);
        writeVenusDbus('/Settings/CGwacs/MaxFeedInPower', watts);
        showToast('Max feed-in: ' + formatW(watts), 'success');
    });
    if (invLimitToggle) invLimitToggle.addEventListener('change', function() {
        invLimitToggle._userChangedAt = Date.now();
        if (invLimitToggle.checked) {
            writeVenusDbus('/Settings/CGwacs/MaxDischargePower', 20000);
            showToast('Inverter limit: ' + formatW(20000), 'success');
        } else {
            writeVenusDbus('/Settings/CGwacs/MaxDischargePower', -1);
            showToast('Inverter limit: Off', 'success');
        }
    });
    if (invLimitDD) invLimitDD.addEventListener('change', function() {
        var watts = parseInt(invLimitDD.value);
        writeVenusDbus('/Settings/CGwacs/MaxDischargePower', watts);
        showToast('Max inverter: ' + formatW(watts), 'success');
    });
}

