// ===== Page Dispatcher =====

// ===== Overview Page =====

function renderOverviewPage(content) {
    if (!content) return;
    content.innerHTML = '';
    var page = document.createElement('div');
    page.className = 've-overview-page';

    page.innerHTML =
        '<h1>Übersicht</h1>' +
        '<p class="ve-overview-subtitle">Alle Geräte und ihr aktueller Status auf einen Blick.</p>' +
        '<div class="ve-overview-totals" id="ve-overview-totals"></div>' +
        '<div class="ve-overview-section-title">Inverter</div>' +
        '<div class="ve-overview-grid" id="ve-overview-inverters"></div>' +
        '<div class="ve-overview-section-title">System</div>' +
        '<div class="ve-overview-grid" id="ve-overview-system"></div>';
    content.appendChild(page);

    _renderOverviewBody();

    // Refresh once on the next tick so the latest snapshot has been received,
    // and again periodically while the overview is showing.
    if (_overviewRefreshInterval) clearInterval(_overviewRefreshInterval);
    _overviewRefreshInterval = setInterval(function() {
        if (_isOverviewActive()) _renderOverviewBody();
        else { clearInterval(_overviewRefreshInterval); _overviewRefreshInterval = null; }
    }, 3000);
}

var _overviewRefreshInterval = null;

function _isOverviewActive() {
    var hash = (window.location.hash || '').replace('#', '');
    return hash === 'overview' || hash.indexOf('overview') === 0;
}

function _renderOverviewBody() {
    var totalsEl = document.getElementById('ve-overview-totals');
    var inverterGrid = document.getElementById('ve-overview-inverters');
    var systemGrid = document.getElementById('ve-overview-system');
    if (!totalsEl || !inverterGrid || !systemGrid) return;

    var inverters = [];
    var systems = [];
    for (var i = 0; i < _devices.length; i++) {
        var d = _devices[i];
        if (d.type === 'venus' || d.type === 'mqtt_pub' || d.type === 'virtual' || d.type === 'influx') {
            systems.push(d);
        } else {
            inverters.push(d);
        }
    }

    // Totals: prefer the cached virtual snapshot for power & limit. Fall
    // back to summing per-device snapshots when the virtual one isn't
    // populated yet.
    var totalPower = 0;
    var totalRated = 0;
    var virtualLimitPct = null;
    var virtualSnap = _lastVirtualSnapshot;
    if (virtualSnap && typeof virtualSnap.total_power_w === 'number') {
        totalPower = virtualSnap.total_power_w;
        totalRated = virtualSnap.total_rated_w || 0;
        if (virtualSnap.control && typeof virtualSnap.control.clamp_max_pct === 'number') {
            virtualLimitPct = virtualSnap.control.clamp_max_pct;
        }
    } else {
        for (var ii = 0; ii < inverters.length; ii++) {
            var snap = _lastDeviceSnapshots[inverters[ii].id];
            if (snap && snap.inverter && typeof snap.inverter.ac_power_w === 'number') {
                totalPower += snap.inverter.ac_power_w;
            }
        }
    }
    var onlineCount = 0;
    for (var k = 0; k < inverters.length; k++) {
        if ((inverters[k].connection_state || 'unknown') === 'connected') onlineCount++;
    }

    var limitHtml = '';
    if (virtualLimitPct !== null && virtualLimitPct < 100 && totalRated > 0) {
        var capW = Math.round((virtualLimitPct / 100) * totalRated);
        limitHtml =
            '<div class="ve-overview-stat-card">' +
            '  <div class="ve-overview-stat-label">Aktives Limit</div>' +
            '  <div class="ve-overview-stat-value" style="color:var(--ve-orange)">' + formatW(capW) + '</div>' +
            '</div>';
    }
    totalsEl.innerHTML =
        '<div class="ve-overview-stat-card">' +
        '  <div class="ve-overview-stat-label">Aktuelle Leistung</div>' +
        '  <div class="ve-overview-stat-value ve-overview-stat-value--accent">' + formatW(totalPower) + '</div>' +
        '</div>' +
        '<div class="ve-overview-stat-card">' +
        '  <div class="ve-overview-stat-label">Inverter online</div>' +
        '  <div class="ve-overview-stat-value">' + onlineCount + ' / ' + inverters.length + '</div>' +
        '</div>' +
        limitHtml;

    inverterGrid.innerHTML = inverters.length
        ? inverters.map(_overviewCardHtml).join('')
        : '<p style="color:var(--ve-text-dim);font-size:0.9rem">Keine Inverter konfiguriert.</p>';
    systemGrid.innerHTML = systems.length
        ? systems.map(_overviewCardHtml).join('')
        : '';
}

function _overviewCardHtml(device) {
    var name = esc(device.name || device.id);
    var typeLabel = (device.type || '').toUpperCase();
    var dotState = device.connection_state || 'unknown';
    var dotColor = dotColorForState(dotState);

    var powerHtml = '<div class="ve-overview-card-power ve-overview-card-power--off">--</div>';
    var metaHtml = '';

    if (device.type === 'virtual') {
        var vs = _lastVirtualSnapshot;
        if (vs && typeof vs.total_power_w === 'number') {
            powerHtml = '<div class="ve-overview-card-power">' + formatW(vs.total_power_w) + '</div>';
        }
        if (vs && vs.control && typeof vs.control.clamp_max_pct === 'number'
            && vs.control.clamp_max_pct < 100 && vs.total_rated_w) {
            var capW = Math.round((vs.control.clamp_max_pct / 100) * vs.total_rated_w);
            metaHtml = '<div class="ve-overview-card-meta ve-overview-card-meta--limit">Limit ' + formatW(capW) + '</div>';
        }
    } else if (device.type === 'venus') {
        metaHtml = '<div class="ve-overview-card-meta">' + (dotState === 'connected' ? 'MQTT verbunden' : 'Nicht verbunden') + '</div>';
    } else if (device.type === 'mqtt_pub') {
        metaHtml = '<div class="ve-overview-card-meta">' + (dotState === 'connected' ? 'Publishing aktiv' : 'Inaktiv') + '</div>';
    } else {
        // Inverter
        var snap = _lastDeviceSnapshots[device.id];
        if (snap && snap.inverter && typeof snap.inverter.ac_power_w === 'number') {
            var p = snap.inverter.ac_power_w;
            var cls = p > 0 ? '' : ' ve-overview-card-power--off';
            powerHtml = '<div class="ve-overview-card-power' + cls + '">' + formatW(p) + '</div>';
        }
        if (snap && snap.control && snap.control.enabled
            && typeof snap.control.limit_pct === 'number' && snap.control.limit_pct < 100) {
            metaHtml = '<div class="ve-overview-card-meta ve-overview-card-meta--limit">Throttle ' +
                snap.control.limit_pct.toFixed(0) + '%</div>';
        }
    }

    var infoHtml = device.info
        ? '<div class="ve-overview-card-info">' + esc(device.info) + '</div>'
        : '';

    var href = '#device/' + encodeURIComponent(device.id) + '/dashboard';
    return '<a class="ve-overview-card" href="' + href + '">' +
        '  <div class="ve-overview-card-header">' +
        '    <span class="ve-dot" style="background:var(' + dotColor + ')"></span>' +
        '    <span class="ve-overview-card-name">' + name + '</span>' +
        '    <span class="ve-overview-card-type">' + esc(typeLabel) + '</span>' +
        '  </div>' +
        infoHtml +
        powerHtml +
        metaHtml +
        '</a>';
}

function showDevicePage(deviceId, tab) {
    if (_regPollInterval) { clearInterval(_regPollInterval); _regPollInterval = null; }
    _activeDeviceId = deviceId;
    _activeDeviceTab = tab || 'dashboard';
    _activeDeviceContainer = null;

    var content = document.getElementById('device-content');
    if (!content) return;
    content.innerHTML = '';

    // Find device info
    var device = null;
    for (var i = 0; i < _devices.length; i++) {
        if (_devices[i].id === deviceId) { device = _devices[i]; break; }
    }

    highlightActiveSidebar();

    if (deviceId === 'mqtt_pub' || (device && device.type === 'mqtt_pub')) {
        _activeDeviceType = 'mqtt_pub';
        renderMqttPubPage(content);
    } else if (deviceId === 'influx' || (device && device.type === 'influx')) {
        _activeDeviceType = 'influx';
        renderInfluxPage(content);
    } else if (deviceId === 'venus' || (device && device.type === 'venus')) {
        _activeDeviceType = 'venus';
        renderVenusPage(content);
    } else if (deviceId === 'virtual' || (device && device.type === 'virtual')) {
        _activeDeviceType = 'virtual';
        renderVirtualPVPage(content);
    } else {
        _activeDeviceType = device ? device.type : 'solaredge';
        renderInverterPage(content, deviceId, _activeDeviceType, tab);
    }
}

// ===== Inverter Page =====

function renderInverterPage(content, deviceId, deviceType, tab) {
    tab = tab || 'dashboard';

    // Sub-tabs
    var tabs = document.createElement('div');
    tabs.className = 've-device-tabs';
    var tabNames = ['Dashboard', 'Registers', 'Config'];
    var tabKeys = ['dashboard', 'registers', 'config'];
    for (var i = 0; i < tabNames.length; i++) {
        var btn = document.createElement('button');
        btn.className = 've-device-tab' + (tabKeys[i] === tab ? ' ve-device-tab--active' : '');
        btn.textContent = tabNames[i];
        btn.setAttribute('data-tab', tabKeys[i]);
        btn.addEventListener('click', (function(key) {
            return function() {
                navigateTo(deviceId, key);
            };
        })(tabKeys[i]));
        tabs.appendChild(btn);
    }
    content.appendChild(tabs);

    // Tab content container
    var tabContent = document.createElement('div');
    tabContent.className = 've-device-tab-content';
    content.appendChild(tabContent);
    _activeDeviceContainer = tabContent;

    if (tab === 'dashboard') {
        renderInverterDashboard(tabContent, deviceId, deviceType);
    } else if (tab === 'registers') {
        renderInverterRegisters(tabContent, deviceId, deviceType);
    } else if (tab === 'config') {
        renderInverterConfig(tabContent, deviceId);
    }
}

// ===== Hamburger Toggle (Mobile) =====

document.getElementById('hamburger').addEventListener('click', function() {
    document.getElementById('sidebar').classList.toggle('open');
    var overlay = document.getElementById('sidebar-overlay');
    if (overlay) overlay.classList.toggle('active');
});

var sidebarOverlay = document.getElementById('sidebar-overlay');
if (sidebarOverlay) {
    sidebarOverlay.addEventListener('click', function() {
        document.getElementById('sidebar').classList.remove('open');
        sidebarOverlay.classList.remove('active');
    });
}

