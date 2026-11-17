/* PV-Inverter-Master - Frontend Application
   Device-centric navigation, WebSocket live dashboard, per-device pages */

var POLL_INTERVAL = 10000;
var previousRegValues = {};
var ws = null;
var sparklineData = [];
// Gauge scaling. A device with a configured rated power uses it directly;
// one without (rated_power = 0) auto-scales to its observed peak so the
// gauge never falls back to a meaningless fixed maximum.
var _gaugePeakW = 0;

function _gaugeMaxW(data) {
    var rated = data && data.rated_power_w;
    if (rated && rated > 0) return rated;
    var acP = (data && data.inverter && data.inverter.ac_power_w) || 0;
    if (acP > _gaugePeakW) _gaugePeakW = acP;
    var max = Math.ceil((_gaugePeakW * 1.15) / 1000) * 1000;
    return Math.max(max, 1000);
}

var GAUGE_ARC_LENGTH = 251.3;
var TEMP_WARNING_C = 75;
var venusLockRemaining = null;
var venusLockSnapshotTs = null;
var venusCountdownInterval = null;

// ===== Device-centric state =====
var _devices = [];
var _activeDeviceId = null;
var _activeDeviceTab = null;
var _activeDeviceContainer = null;
var _activeDeviceType = null;
var _lastVirtualSnapshot = null;
var _lastDeviceSnapshots = {};
var _regPollInterval = null;

// ===== MQTT Publisher State =====
var _mqttPubConnected = false;
var _mqttPubStats = null;
var _influxConnected = false;
var _influxStats = null;
var _influxStatsInterval = null;

// ===== Venus Health Dashboard polling =====
var _venusHealthInterval = null;

// Lowercase type labels shown under each inverter name in the sidebar.
var SIDEBAR_TYPE_LABELS = {
    solaredge: 'solaredge',
    opendtu: 'opendtu',
    hoymiles: 'hoymiles hms',
    shelly: 'shelly',
    sungrow: 'sungrow',
    senec: 'senec.home'
};
var _lastDeviceList = [];

// ===== Locations / Standorte State =====
var _locations = [];
var _locLiveInterval = null;

// ===== Discovery / Scan State =====
var _scanRunning = false;
var _autoScanDone = false;
var _configuredInverters = [];

// ===== Phase 44 CHECK-01/04: version footer + update badge state =====
// Shape: {current_version, current_commit, available_update, last_check_at, last_check_failed_at}
var _availableUpdateState = null;

// ===== Animation Guards =====
var prefersReducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
var entranceAnimated = false;

// ===== Hash Router =====

function _firstInverterId() {
    for (var i = 0; i < _devices.length; i++) {
        if (_devices[i].type !== 'venus' && _devices[i].type !== 'virtual') return _devices[i].id;
    }
    return 'virtual';
}

function parseRoute() {
    var hash = (window.location.hash || '').replace('#', '');
    var parts = hash.split('/');
    if (parts[0] === 'device' && parts.length >= 3) {
        return { type: 'device', id: parts[1], tab: parts[2] };
    }
    if (parts[0] === 'system' && parts[1] === 'software') {
        return { type: 'system-software' };
    }
    if (parts[0] === 'overview') {
        return { type: 'overview' };
    }
    if (parts[0] === 'locations') {
        return { type: 'locations' };
    }
    if (parts[0] === 'location' && parts[1]) {
        return { type: 'location', id: parts[1] };
    }
    if (parts[0] === 'map') {
        return { type: 'map' };
    }
    // Legacy redirects
    if (hash === 'dashboard' || hash === '') return { type: 'device', id: 'virtual', tab: 'dashboard' };
    if (hash === 'config') return { type: 'device', id: _firstInverterId(), tab: 'config' };
    if (hash === 'registers') return { type: 'device', id: _firstInverterId(), tab: 'registers' };
    return { type: 'device', id: 'virtual', tab: 'dashboard' };
}

function navigateTo(deviceId, tab) {
    window.location.hash = 'device/' + deviceId + '/' + (tab || 'dashboard');
}

// Phase 46-03: dispatch #system/software to the softwarePage controller and
// swap visibility of the dedicated #software-root div vs the device content.
function routeDispatch() {
    var route = parseRoute();
    var softwareRoot = document.getElementById('software-root');
    var deviceContent = document.getElementById('device-content');
    if (route.type === 'system-software') {
        if (deviceContent) deviceContent.style.display = 'none';
        if (softwareRoot) softwareRoot.style.display = '';
        if (window.softwarePage) window.softwarePage.onRouteEnter();
        _setSidebarHeaderActive(false);
        return;
    }
    if (softwareRoot) softwareRoot.style.display = 'none';
    if (deviceContent) deviceContent.style.display = '';
    if (window.softwarePage) window.softwarePage.onRouteLeave();
    if (route.type === 'overview') {
        _activeDeviceId = null;
        _activeDeviceContainer = null;
        _activeDeviceType = null;
        if (_regPollInterval) { clearInterval(_regPollInterval); _regPollInterval = null; }
        renderOverviewPage(deviceContent);
        highlightActiveSidebar();
        _setSidebarHeaderActive(true);
        return;
    }
    if (route.type === 'locations' || route.type === 'location' || route.type === 'map') {
        _activeDeviceId = (route.type === 'location') ? ('location:' + route.id) : null;
        _activeDeviceContainer = null;
        _activeDeviceType = null;
        if (_regPollInterval) { clearInterval(_regPollInterval); _regPollInterval = null; }
        _setSidebarHeaderActive(false);
        if (route.type === 'locations') renderLocationsPage(deviceContent);
        else if (route.type === 'location') renderLocationDetailPage(deviceContent, route.id);
        else renderMapPage(deviceContent);
        highlightActiveSidebar();
        return;
    }
    _setSidebarHeaderActive(false);
    showDevicePage(route.id, route.tab);
}

function _setSidebarHeaderActive(active) {
    var link = document.getElementById('sidebar-header-link');
    if (!link) return;
    if (active) link.classList.add('is-active');
    else link.classList.remove('is-active');
}

window.addEventListener('hashchange', routeDispatch);

function esc(s) {
    if (!s) return '';
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function gaugeColor(pct) {
    return pct < 0.5 ? 'var(--ve-green)' : pct < 0.8 ? 'var(--ve-orange)' : 'var(--ve-red)';
}

function dotColorForState(state) {
    if (state === 'connected') return '--ve-green';
    if (state === 'reconnecting') return '--ve-orange';
    if (state === 'disconnected') return '--ve-red';
    return '--ve-text-dim';
}

