// ===== WebSocket Connection =====

function connectWebSocket() {
    var protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    ws = new WebSocket(protocol + '//' + location.host + '/ws');
    var reconnectDelay = 1000;

    ws.onopen = function() {
        reconnectDelay = 1000;
        // Phase 46-03: notify software page so it can re-fetch /api/version
        // (stale-tab detection) and replay missed update_progress history.
        if (window.softwarePage) window.softwarePage.onWsReconnect();
    };

    ws.onmessage = function(event) {
        try {
            var msg = JSON.parse(event.data);
            if (msg.type === 'snapshot') handleSnapshot(msg.data);
            if (msg.type === 'device_snapshot') handleDeviceSnapshot(msg);
            if (msg.type === 'virtual_snapshot') handleVirtualSnapshot(msg.data);
            if (msg.type === 'update_progress' && window.softwarePage) {
                window.softwarePage.handleWsMessage(msg);
            }
            if (msg.type === 'device_list') {
                _lastDeviceList = msg.data.devices || [];
                // Extract mqtt_pub + influx connection state + stats from device list
                for (var di = 0; di < _lastDeviceList.length; di++) {
                    var _dl = _lastDeviceList[di];
                    if (_dl.type === 'mqtt_pub') {
                        _mqttPubConnected = _dl.connection_state === 'connected';
                        _mqttPubStats = _dl.stats || null;
                    } else if (_dl.type === 'influx') {
                        _influxConnected = _dl.connection_state === 'connected';
                        _influxStats = _dl.stats || null;
                    }
                }
                renderSidebar(msg.data.devices);
                updateMqttPubStatusDot();
                updateMqttPubStats();
                renderMqttTopicPreview();
                updateInfluxStatusDot();
                updateInfluxStats();
            }
            if (msg.type === 'location_list') handleLocationList(msg.data);
            if (msg.type === 'location_position') handleLocationPosition(msg.data);
            if (msg.type === 'available_update') handleAvailableUpdate(msg.data);
            if (msg.type === 'history') handleHistory(msg.data);
            if (msg.type === 'override_event') handleOverrideEvent(msg.data);
            if (msg.type === 'scan_progress') handleScanProgress(msg.data);
            if (msg.type === 'scan_complete') handleScanComplete(msg.data);
            if (msg.type === 'scan_error') handleScanError(msg.data);
            if (msg.type === 'no_inverter') handleNoInverter();
        } catch (e) {
            console.error('WebSocket message parse error:', e);
        }
    };

    ws.onclose = function() {
        setTimeout(function() {
            reconnectDelay = Math.min(reconnectDelay * 2, 30000);
            connectWebSocket();
        }, reconnectDelay);
    };

    ws.onerror = function() {
        ws.close();
    };

    return ws;
}

// ===== Device Snapshot WS Handler =====

function handleDeviceSnapshot(msg) {
    var deviceId = msg.device_id;
    var data = msg.data;

    // Cache for the overview page (which renders out of these snapshots)
    if (deviceId) _lastDeviceSnapshots[deviceId] = data;

    // Update sidebar power and dot
    updateSidebarPower(deviceId, data);

    // If this device's dashboard is active, update it
    if (deviceId === _activeDeviceId && _activeDeviceContainer && _activeDeviceTab === 'dashboard') {
        updateActiveDeviceDashboard(data);
    }
}

function handleVirtualSnapshot(data) {
    _lastVirtualSnapshot = data;

    // Update sidebar power for virtual
    var entry = document.querySelector('.ve-sidebar-device[data-device-id="virtual"]');
    if (entry) {
        var pwrEl = entry.querySelector('.ve-sidebar-device-power');
        if (pwrEl && data.total_power_w != null) {
            pwrEl.textContent = formatW(data.total_power_w);
        }
    }

    // If virtual page is showing, update it
    if (_activeDeviceId === 'virtual' && _activeDeviceContainer) {
        updateVirtualPVPage(data);
    }
}

function updateSidebarPower(deviceId, data) {
    var entry = document.querySelector('.ve-sidebar-device[data-device-id="' + deviceId + '"]');
    if (!entry) return;

    // Update power
    var pwrEl = entry.querySelector('.ve-sidebar-device-power');
    if (pwrEl) {
        var inv = data.inverter || data;
        var pw = inv.ac_power_w || data.power_w;
        if (pw != null) {
            pwrEl.textContent = formatW(pw);
        }
    }

    var dot = entry.querySelector('.ve-dot');
    if (dot) {
        var connState = data.connection ? data.connection.state : data.connection_state;
        dot.style.background = 'var(' + dotColorForState(connState) + ')';
    }
}

