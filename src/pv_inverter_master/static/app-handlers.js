// ===== Legacy Snapshot Handler (backward compat) =====

function handleSnapshot(data) {
    var inv = data.inverter;
    if (!inv) return;

    // Update sidebar with legacy data
    if (data.connection && data.connection.state) {
        var sidebarDevices = document.querySelectorAll('.ve-sidebar-device');
        // Try to update first inverter
        for (var i = 0; i < sidebarDevices.length; i++) {
            var did = sidebarDevices[i].getAttribute('data-device-id');
            if (did && did !== 'venus' && did !== 'virtual') {
                updateSidebarPower(did, data);
                break;
            }
        }
    }

    // If active device is an inverter and we have no device_snapshot routing yet
    if (_activeDeviceContainer && _activeDeviceTab === 'dashboard' &&
        _activeDeviceId !== 'venus' && _activeDeviceId !== 'virtual') {
        updateActiveDeviceDashboard(data);
    }

    // Update Venus MQTT status in sidebar
    if (data.venus_mqtt_connected != null) {
        var venusEntry = document.querySelector('.ve-sidebar-device[data-device-id="venus"]');
        if (venusEntry) {
            var dot = venusEntry.querySelector('.ve-dot');
            if (dot) {
                dot.style.background = data.venus_mqtt_connected ? 'var(--ve-green)' : 'var(--ve-red)';
            }
            var pwrEl = venusEntry.querySelector('.ve-sidebar-device-power');
            if (pwrEl) {
                pwrEl.textContent = data.venus_mqtt_connected ? 'Connected' : 'Disconnected';
            }
        }
    }

    // Update Venus ESS on device page if showing
    if (_activeDeviceId === 'venus' && _activeDeviceContainer) {
        updateVenusESSOnPage(data);
    }
}

function updateVenusESSOnPage(snapshot) {
    var vs = snapshot.venus_settings;
    if (!vs) return;

    var container = _activeDeviceContainer.parentElement || document.getElementById('device-content');
    if (!container) return;

    var now = Date.now();
    function notCooling(el) { return (now - (el._userChangedAt || 0)) > 8000; }

    // MQTT dot update
    var mqttDot = container.querySelector('.ve-venus-mqtt-dot');
    var mqttText = container.querySelector('.ve-venus-mqtt-text');
    if (mqttDot) {
        mqttDot.className = 've-dot ' + (snapshot.venus_mqtt_connected ? 've-dot--ok' : 've-dot--err');
    }
    if (mqttText) {
        mqttText.textContent = snapshot.venus_mqtt_connected ? 'Connected' : 'Disconnected';
    }

    // ESS toggles
    var acToggle = container.querySelector('.ve-ess-ac-excess');
    var dcToggle = container.querySelector('.ve-ess-dc-excess');
    var limitToggle = container.querySelector('.ve-ess-limit-feedin');
    var feedInDD = container.querySelector('.ve-ess-feed-in-dd');
    var feedInActual = container.querySelector('.ve-ess-feed-in-actual');
    var limiterEl = container.querySelector('.ve-ess-limiter-value');
    var limitRow = container.querySelector('.ve-ess-limit-row');
    var maxRow = container.querySelector('.ve-ess-max-feedin-row');
    var invLimitToggle = container.querySelector('.ve-ess-limit-inverter');
    var invLimitRow = container.querySelector('.ve-ess-max-inverter-row');
    var invLimitDD = container.querySelector('.ve-ess-max-inverter-dd');

    var acOn = !vs.prevent_feedback;
    if (acToggle && notCooling(acToggle)) acToggle.checked = acOn;

    var dcOn = vs.overvoltage_feed_in;
    if (dcToggle && notCooling(dcToggle)) dcToggle.checked = dcOn;

    var excessActive = acOn || dcOn;
    if (limitRow) limitRow.style.display = excessActive ? '' : 'none';

    var feedInLimited = vs.max_feed_in_w >= 0;
    if (limitToggle && notCooling(limitToggle)) limitToggle.checked = feedInLimited;
    if (maxRow) maxRow.style.display = (excessActive && feedInLimited) ? '' : 'none';

    if (feedInActual) {
        feedInActual.textContent = formatW(vs.grid_feed_in_w);
        if (vs.max_feed_in_w > 0 && vs.grid_feed_in_w > vs.max_feed_in_w) {
            feedInActual.style.color = 'var(--ve-red)';
        } else if (vs.grid_feed_in_w > 0) {
            feedInActual.style.color = 'var(--ve-green)';
        } else {
            feedInActual.style.color = '';
        }
    }

    if (feedInDD && !feedInDD.matches(':focus') && vs.max_feed_in_w > 0) {
        var closest = Math.round(vs.max_feed_in_w / 1000) * 1000;
        feedInDD.value = closest;
    }

    var invLimited = vs.max_inverter_w >= 0;
    if (invLimitToggle && notCooling(invLimitToggle)) invLimitToggle.checked = invLimited;
    if (invLimitRow) invLimitRow.style.display = invLimited ? '' : 'none';

    if (invLimitDD && !invLimitDD.matches(':focus') && invLimited) {
        var closestInv = Math.round(vs.max_inverter_w / 1000) * 1000;
        invLimitDD.value = closestInv;
    }

    if (limiterEl) {
        if (vs.limiter_active) {
            limiterEl.textContent = 'Active';
            limiterEl.style.color = 'var(--ve-green)';
        } else {
            limiterEl.textContent = 'Inactive';
            limiterEl.style.color = 'var(--ve-text-dim)';
        }
    }

    // MQTT gate
    var essPanel = container.querySelector('#venus-ess-panel-device');
    if (essPanel) {
        if (snapshot.venus_mqtt_connected) {
            essPanel.classList.remove('mqtt-gated');
        } else {
            essPanel.classList.add('mqtt-gated');
        }
    }
}

// ===== No Inverter Handler =====

function handleNoInverter() {
    // Show empty state if viewing an inverter that no longer exists
    if (_activeDeviceContainer && _activeDeviceId !== 'venus' && _activeDeviceId !== 'virtual') {
        _activeDeviceContainer.innerHTML =
            '<div class="ve-hint-card" style="max-width:420px;margin:80px auto;text-align:center;padding:32px">' +
            '<h3 style="margin:0 0 8px;color:var(--ve-text)">Kein Inverter konfiguriert</h3>' +
            '<p style="margin:0;color:var(--ve-text-dim);font-size:0.9rem">Klicke auf "+" um einen Inverter hinzuzufuegen.</p>' +
            '</div>';
    }
}

// ===== History Handler =====

function handleHistory(data) {
    if (data.ac_power_w && Array.isArray(data.ac_power_w)) {
        sparklineData = data.ac_power_w.map(function(p) { return p[1]; });
    }
}

// ===== Override Event Handler =====

function handleOverrideEvent(eventData) {
    var sourceNames = { 'webapp': 'Webapp', 'venus_os': 'Venus OS', 'system': 'System' };
    var sourceName = sourceNames[eventData.source] || eventData.source;
    var msg = sourceName + ': ' + eventData.action;
    if (eventData.value != null) msg += ' ' + eventData.value.toFixed(1) + '%';
    if (eventData.detail) msg += ' (' + eventData.detail + ')';
    var toastType = eventData.source === 'venus_os' ? 'error' : 'info';
    showToast(msg, toastType);
}

// ===== Discovery / Scan =====

function handleScanProgress(data) {
    // Check for add-modal scan area
    var addFill = document.querySelector('.ve-add-scan-fill');
    var addStatus = document.querySelector('.ve-add-scan-status');
    if (addFill) {
        var pct = data.total > 0 ? Math.round((data.current / data.total) * 100) : 0;
        addFill.style.width = pct + '%';
    }
    if (addStatus) {
        if (data.phase === 'probe') {
            addStatus.textContent = 'Scanning network (' + data.current + '/' + data.total + ')...';
        } else if (data.phase === 'verify') {
            addStatus.textContent = 'Verifying SunSpec (' + data.current + '/' + data.total + ')...';
        }
    }
}

function handleScanComplete(data) {
    _scanRunning = false;
    var devices = data.devices || [];

    // Check for add-modal scan results
    var scanResults = document.querySelector('.ve-add-scan-results');
    var scanProgress = document.querySelector('.ve-add-scan-area .ve-scan-progress');
    if (scanProgress) scanProgress.style.display = 'none';

    if (scanResults) {
        scanResults.innerHTML = '';
        if (devices.length === 0) {
            scanResults.innerHTML = '<div class="ve-hint-card ve-scan-hint"><div class="ve-hint-header">Keine Inverter gefunden</div></div>';
        } else {
            scanResults.innerHTML = '<div class="ve-scan-results-hint">' + devices.length + ' gefunden — zum Übernehmen anklicken:</div>';
            for (var i = 0; i < devices.length; i++) {
                var dev = devices[i];
                var row = document.createElement('div');
                row.className = 've-scan-result' + _addedRowClass(dev.already_added);
                var ident = ((dev.manufacturer || '') + ' ' + (dev.model || '')).trim() || 'Unknown';
                row.innerHTML = '<span class="ve-scan-result-host">' + esc(dev.ip) + ':' + esc(dev.port) + '</span><span class="ve-scan-result-identity">' + esc(ident) + _addedBadge(dev.already_added) + '</span>';
                row._device = dev;
                row.addEventListener('click', function() {
                    var d = this._device;
                    var siblings = this.parentElement.querySelectorAll('.ve-scan-result');
                    for (var s = 0; s < siblings.length; s++) {
                        siblings[s].classList.remove('ve-scan-result--selected');
                    }
                    this.classList.add('ve-scan-result--selected');
                    var hostInput = document.querySelector('.ve-add-host');
                    var portInput = document.querySelector('.ve-add-port');
                    var unitInput = document.querySelector('.ve-add-unit');
                    if (hostInput) hostInput.value = d.ip;
                    if (portInput) portInput.value = d.port;
                    if (unitInput) unitInput.value = d.unit_id;
                    // Carry the discovered OpenDTU inverter serial so the
                    // serial picker can pre-select the right one.
                    if (hostInput && d.device_type === 'opendtu' && d.serial_number) {
                        hostInput.setAttribute('data-dtu-serial', d.serial_number);
                    }
                    _flashFilledFields(document);
                });
                scanResults.appendChild(row);
            }
        }
    }
}

function handleScanError(data) {
    _scanRunning = false;
    showToast('Scan fehlgeschlagen: ' + (data.error || 'Unbekannter Fehler'), 'error');
}

