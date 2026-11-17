// ===== Sidebar Rendering =====

// 260528-hhj: format a Unix-epoch float into a short German relative-time
// string. Used by createSidebarDevice for the per-device hover tooltip and
// by buildInverterConfigForm for the inline status card at the top of the
// config panel. Pure vanilla — no Intl/RelativeTimeFormat fallback needed.
function formatRelativeTime(epochSeconds) {
    if (epochSeconds === null || epochSeconds === undefined) return '';
    var delta = Math.floor((Date.now() / 1000) - epochSeconds);
    if (delta < 0) return 'gerade eben';
    if (delta < 60) return 'vor ' + delta + 's';
    if (delta < 3600) return 'vor ' + Math.floor(delta / 60) + 'min';
    if (delta < 86400) return 'vor ' + Math.floor(delta / 3600) + 'h';
    return 'vor ' + Math.floor(delta / 86400) + 'd';
}

function renderSidebar(devices) {
    if (devices) _devices = devices;
    var container = document.getElementById('device-sidebar');
    if (!container) return;
    container.innerHTML = '';

    var inverters = [];
    var venusDevice = null;
    var virtualDevice = null;
    var mqttPubDevice = null;
    var influxDevice = null;

    for (var i = 0; i < _devices.length; i++) {
        var d = _devices[i];
        if (d.type === 'venus') venusDevice = d;
        else if (d.type === 'virtual') virtualDevice = d;
        else if (d.type === 'mqtt_pub') mqttPubDevice = d;
        else if (d.type === 'influx') influxDevice = d;
        else inverters.push(d);
    }

    // STANDORTE group sits at the very top of the sidebar.
    container.appendChild(createLocationsSidebarGroup());

    // INVERTERS — grouped by Standort. Every Standort with at least one
    // assigned inverter becomes a collapsible group; unassigned inverters
    // fall into "OHNE STANDORT". Each group carries its own Add button that
    // opens the add-device flow pre-filled with that group's Standort.
    if (inverters.length > 0) {
        var byLoc = {};
        var noLoc = [];
        for (var gi = 0; gi < inverters.length; gi++) {
            var lid = inverters[gi].location_id || '';
            if (lid && _locationById(lid)) {
                (byLoc[lid] = byLoc[lid] || []).push(inverters[gi]);
            } else {
                noLoc.push(inverters[gi]);
            }
        }
        for (var li = 0; li < _locations.length; li++) {
            var lgrp = byLoc[_locations[li].id];
            if (!lgrp || !lgrp.length) continue;
            container.appendChild(createSidebarGroup(
                (_locations[li].name || 'Standort').toUpperCase(), lgrp, true,
                _locations[li].id));
        }
        if (noLoc.length > 0) {
            container.appendChild(createSidebarGroup('OHNE STANDORT', noLoc, true, ''));
        }
    }

    // VENUS OS group (includes Virtual PV as sub-entry)
    if (venusDevice) {
        var venusGroup = [venusDevice];
        if (virtualDevice) venusGroup.push(virtualDevice);
        container.appendChild(createSidebarGroup('VENUS OS', venusGroup));
    } else if (virtualDevice) {
        container.appendChild(createSidebarGroup('VENUS OS', [virtualDevice]));
    }

    // MQTT PUBLISH group
    if (mqttPubDevice) {
        container.appendChild(createSidebarGroup('MQTT PUBLISH', [mqttPubDevice]));
    }

    // INFLUXDB group
    if (influxDevice) {
        container.appendChild(createSidebarGroup('INFLUXDB', [influxDevice]));
    }

    // Phase 46-03 UI-01: SYSTEM group with Software entry ALWAYS visible
    // (routes to #system/software). Phase 44 CHECK-04 still adorns the entry
    // with an update badge when _availableUpdateState.available_update is set.
    container.appendChild(
        createSystemSidebarGroup(
            (_availableUpdateState && _availableUpdateState.available_update) || null
        )
    );

    // Update active highlight
    highlightActiveSidebar();
}

// ===== Phase 44: Available Update handler =====

function handleAvailableUpdate(data) {
    _availableUpdateState = data || null;
    renderVersionFooter();
    // Re-render sidebar so the SYSTEM group can appear/disappear
    renderSidebar();
}

function renderVersionFooter() {
    var footer = document.getElementById('ve-version-footer');
    if (!footer) return;
    var textSpan = footer.querySelector('.ve-version-footer-text');
    if (!textSpan) return;

    if (!_availableUpdateState) {
        textSpan.textContent = 'v\u2014';
        footer.title = '';
        footer.classList.remove('ve-version-footer--failed');
        return;
    }

    var v = _availableUpdateState.current_version || 'unknown';
    var c = _availableUpdateState.current_commit;
    var text = 'v' + v;
    if (c) {
        text += ' (' + c + ')';
    }
    textSpan.textContent = text;

    // CHECK-06 indicator: last_check_failed_at sets tooltip + orange color
    // Only show failure state if it's newer than the last successful check
    var failedAt = _availableUpdateState.last_check_failed_at;
    var okAt = _availableUpdateState.last_check_at;
    var showFailed = failedAt && (!okAt || failedAt > okAt);

    if (showFailed) {
        var whenFail = new Date(failedAt * 1000);
        footer.title = 'Letzter Update-Check fehlgeschlagen: ' + whenFail.toLocaleTimeString();
        footer.classList.add('ve-version-footer--failed');
    } else if (okAt) {
        var whenOk = new Date(okAt * 1000);
        footer.title = 'Letzter Update-Check: ' + whenOk.toLocaleTimeString();
        footer.classList.remove('ve-version-footer--failed');
    } else {
        footer.title = '';
        footer.classList.remove('ve-version-footer--failed');
    }
}

function createSystemSidebarGroup(availableUpdate) {
    var group = document.createElement('div');
    group.className = 've-sidebar-group';

    var header = document.createElement('div');
    header.className = 've-sidebar-group-header';
    header.innerHTML = '<span>SYSTEM</span><span class="ve-sidebar-header-right"><span class="ve-chevron">&#9660;</span></span>';
    header.addEventListener('click', function() {
        var items = group.querySelector('.ve-sidebar-group-items');
        var chevron = header.querySelector('.ve-chevron');
        if (items.classList.contains('ve-sidebar-group-items--collapsed')) {
            items.classList.remove('ve-sidebar-group-items--collapsed');
            chevron.classList.remove('ve-chevron--collapsed');
        } else {
            items.classList.add('ve-sidebar-group-items--collapsed');
            chevron.classList.add('ve-chevron--collapsed');
        }
    });
    group.appendChild(header);

    var itemsContainer = document.createElement('div');
    itemsContainer.className = 've-sidebar-group-items';
    itemsContainer.appendChild(createSoftwareSidebarEntry(availableUpdate));
    group.appendChild(itemsContainer);
    return group;
}

function createSoftwareSidebarEntry(availableUpdate) {
    // Phase 46-03 UI-01: this entry is ALWAYS rendered (availableUpdate may
    // be null); clicking it navigates to #system/software.
    var entry = document.createElement('a');
    entry.href = '#system/software';
    entry.setAttribute('data-nav', 'system-software');
    var entryClass = 've-sidebar-device';
    if (availableUpdate) {
        entryClass += ' ve-sidebar-device--system-with-update';
    }
    entry.className = entryClass;

    if (availableUpdate) {
        var tagName = availableUpdate.tag_name || availableUpdate.latest_version || '';
        var publishedAt = availableUpdate.published_at || '';
        var titleParts = ['Update available'];
        if (tagName) titleParts.push(tagName);
        if (publishedAt) titleParts.push(publishedAt);
        entry.title = titleParts.join(' — ');
    } else {
        entry.title = 'System software settings';
    }

    // Badge dot uses existing --ve-orange token via the CSS class selector above
    var dotHtml = '<span class="ve-dot"></span>';

    var linkHtml = '';
    if (availableUpdate && availableUpdate.html_url) {
        linkHtml = '<a class="ve-sidebar-device-github-link" href="' + esc(availableUpdate.html_url) + '" target="_blank" rel="noopener">GitHub &rarr;</a>';
    }

    entry.innerHTML =
        dotHtml +
        '<span class="ve-sidebar-device-name">' + esc('Software') + '</span>' +
        linkHtml;

    entry.addEventListener('click', function(e) {
        // Allow the inner GitHub link to navigate externally.
        if (e.target && e.target.closest && e.target.closest('.ve-sidebar-device-github-link')) return;
        e.preventDefault();
        window.location.hash = 'system/software';
        var sidebar = document.getElementById('sidebar');
        if (sidebar) sidebar.classList.remove('open');
        var overlay = document.getElementById('sidebar-overlay');
        if (overlay) overlay.classList.remove('active');
    });

    return entry;
}

// Collapse state survives the sidebar rebuild that fires on every device
// list broadcast (~5 s) — keyed by the group label.
var _collapsedSidebarGroups = {};

function createSidebarGroup(label, devices, showAddBtn, locationId) {
    var group = document.createElement('div');
    group.className = 've-sidebar-group';

    // Aggregate power — shown on the header while the group is collapsed,
    // so a folded-up Standort still reports its current output.
    var hasPower = false, totalPower = 0;
    for (var pi = 0; pi < devices.length; pi++) {
        if (typeof devices[pi].power_w === 'number') {
            hasPower = true;
            totalPower += devices[pi].power_w;
        }
    }

    var header = document.createElement('div');
    header.className = 've-sidebar-group-header';
    var addBtnHtml = showAddBtn ? '<button class="ve-sidebar-add-btn" title="Gerät hinzufügen">+</button>' : '';
    var powerHtml = hasPower
        ? '<span class="ve-sidebar-group-power">' + formatW(totalPower) + '</span>'
        : '';
    header.innerHTML = '<span>' + label + '</span><span class="ve-sidebar-header-right">' +
        powerHtml + addBtnHtml + '<span class="ve-chevron">&#9660;</span></span>';

    var collapsed = !!_collapsedSidebarGroups[label];

    function _applyCollapsed(isCollapsed) {
        var items = group.querySelector('.ve-sidebar-group-items');
        var chevron = header.querySelector('.ve-chevron');
        items.classList.toggle('ve-sidebar-group-items--collapsed', isCollapsed);
        chevron.classList.toggle('ve-chevron--collapsed', isCollapsed);
        group.classList.toggle('ve-sidebar-group--collapsed', isCollapsed);
    }

    header.addEventListener('click', function(e) {
        // Don't toggle collapse when clicking the add button
        if (e.target.closest('.ve-sidebar-add-btn')) return;
        collapsed = !collapsed;
        _collapsedSidebarGroups[label] = collapsed;
        _applyCollapsed(collapsed);
    });
    group.appendChild(header);

    // Wire add button if present — opens the add-device flow with this
    // group's Standort pre-selected (locationId '' = OHNE STANDORT).
    if (showAddBtn) {
        var addBtn = header.querySelector('.ve-sidebar-add-btn');
        if (addBtn) addBtn.addEventListener('click', function(e) {
            e.stopPropagation();
            showAddDeviceModal(locationId);
        });
    }

    var itemsContainer = document.createElement('div');
    itemsContainer.className = 've-sidebar-group-items';

    for (var i = 0; i < devices.length; i++) {
        itemsContainer.appendChild(createSidebarDevice(devices[i]));
    }

    group.appendChild(itemsContainer);
    _applyCollapsed(collapsed);   // restore persisted collapse state
    return group;
}

function createSidebarDevice(device) {
    var entry = document.createElement('a');
    entry.className = 've-sidebar-device';
    entry.setAttribute('data-device-id', device.id);

    if (!device.enabled && device.type !== 'virtual') {
        entry.classList.add('ve-sidebar-device--disabled');
    }

    var dotColor = dotColorForState(device.connection_state);

    var powerStr = '';
    if (device.power_w != null && device.type !== 'venus') {
        powerStr = formatW(device.power_w);
    } else if (device.type === 'venus') {
        powerStr = device.connection_state === 'connected' ? 'Connected' : '';
    }

    // Lowercase inverter-type label shown under the device name (skipped
    // for pseudo-devices like venus / virtual / mqtt_pub / influx).
    var typeLabel = SIDEBAR_TYPE_LABELS[device.type] || '';

    entry.innerHTML =
        '<span class="ve-dot" style="background:var(' + dotColor + ')"></span>' +
        '<span class="ve-sidebar-device-main">' +
            '<span class="ve-sidebar-device-name">' + esc(device.name || device.id) + '</span>' +
            (typeLabel ? '<span class="ve-sidebar-device-type">' + esc(typeLabel) + '</span>' : '') +
            (device.info ? '<span class="ve-sidebar-device-info">' + esc(device.info) + '</span>' : '') +
        '</span>' +
        '<span class="ve-sidebar-device-power">' + powerStr + '</span>';

    if (!device.enabled && device.type !== 'virtual') {
        entry.innerHTML += '<span class="ve-sidebar-disabled-label">Disabled</span>';
    }

    // 260528-hhj: per-device poll-health tooltip. Pseudo-devices (venus /
    // virtual / mqtt_pub / influx) have no poll-loop and stay untouched.
    if (device.type !== 'venus' && device.type !== 'virtual' &&
        device.type !== 'mqtt_pub' && device.type !== 'influx') {
        var titleText;
        if (device.last_successful_poll_at) {
            titleText = 'Letzter erfolgreicher Poll: ' +
                formatRelativeTime(device.last_successful_poll_at);
            if (device.last_poll_error) {
                titleText += ' — Fehler: ' + device.last_poll_error;
            }
        } else if (device.connection_state === 'unknown' || device.last_poll_at == null) {
            titleText = 'Noch nicht gepollt';
        } else {
            titleText = 'Noch nie erfolgreich gepollt';
            if (device.last_poll_error) {
                titleText += ' — ' + device.last_poll_error;
            }
        }
        entry.title = titleText;
    }

    entry.addEventListener('click', function(e) {
        e.preventDefault();
        navigateTo(device.id, 'dashboard');
        // Close mobile sidebar
        document.getElementById('sidebar').classList.remove('open');
        var overlay = document.getElementById('sidebar-overlay');
        if (overlay) overlay.classList.remove('active');
    });

    return entry;
}

function highlightActiveSidebar() {
    var entries = document.querySelectorAll('.ve-sidebar-device');
    for (var i = 0; i < entries.length; i++) {
        if (entries[i].getAttribute('data-device-id') === _activeDeviceId) {
            entries[i].classList.add('ve-sidebar-device--active');
        } else {
            entries[i].classList.remove('ve-sidebar-device--active');
        }
    }
}

