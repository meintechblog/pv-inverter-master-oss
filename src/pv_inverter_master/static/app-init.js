// renderMapPage is defined in the map module section below.
if (typeof renderMapPage !== 'function') {
    window.renderMapPage = function(content) {
        if (content) content.innerHTML = '<div class="ve-hint-card"><div class="ve-hint-header">Karte wird geladen…</div></div>';
    };
}

// ===== Initialization =====

document.addEventListener('DOMContentLoaded', function() {
    // Fetch device list and render sidebar
    fetch('/api/devices')
        .then(function(res) { return res.json(); })
        .then(function(data) {
            renderSidebar(data.devices || []);
            // Phase 46-03: use routeDispatch so #system/software is honored on first load.
            routeDispatch();
        })
        .catch(function() {
            // No devices yet -- show virtual
            renderSidebar([]);
            routeDispatch();
        });

    // Start WebSocket
    connectWebSocket();

    // Phase 44: initial version fetch fallback — WS push is the primary path,
    // this only runs if WS hasn't delivered available_update within 2 seconds of page load.
    setTimeout(function() {
        if (_availableUpdateState === null) {
            fetch('/api/update/available')
                .then(function(r) { return r.ok ? r.json() : null; })
                .then(function(data) {
                    if (data && _availableUpdateState === null) {
                        handleAvailableUpdate(data);
                    }
                })
                .catch(function() { /* silent */ });
        }
    }, 2000);

    // Config Export
    document.getElementById('btn-config-export').addEventListener('click', function() {
        window.location.href = '/api/config/export';
    });

    // Config Import
    var importBtn = document.getElementById('btn-config-import');
    var fileInput = document.getElementById('config-file-input');
    importBtn.addEventListener('click', function() { fileInput.click(); });
    fileInput.addEventListener('change', function() {
        var file = fileInput.files[0];
        if (!file) return;
        var reader = new FileReader();
        reader.onload = function(e) {
            fetch('/api/config/import', {
                method: 'POST',
                headers: { 'Content-Type': 'application/x-yaml' },
                body: e.target.result
            }).then(function(r) { return r.json(); }).then(function(d) {
                if (d.success) {
                    showToast('Config imported — restart recommended', 'success');
                    setTimeout(function() { location.reload(); }, 1500);
                } else {
                    showToast('Import failed: ' + d.error, 'error');
                }
            }).catch(function(err) { showToast('Import error: ' + err.message, 'error'); });
        };
        reader.readAsText(file);
        fileInput.value = '';  // Reset for re-import of same file
    });
});
