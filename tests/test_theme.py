"""Smoke tests for Venus OS theme and frontend file structure."""

import importlib.resources as r


def _read_static(filename: str) -> str:
    """Read a static file from the package."""
    ref = r.files("pv_inverter_master") / "static" / filename
    return ref.read_text(encoding="utf-8")


def _read_app_bundle() -> str:
    """Read and concatenate every app-*.js file (the former app.js)."""
    static = r.files("pv_inverter_master") / "static"
    parts = sorted(
        p.name for p in static.iterdir()
        if p.name.startswith("app-") and p.name.endswith(".js")
    )
    return "\n".join(_read_static(name) for name in parts)


class TestVenusColors:
    """Verify Venus OS gui-v2 color palette is present in style.css."""

    def test_primary_blue(self):
        css = _read_static("style.css")
        assert "--ve-blue: #387DC5" in css

    def test_background(self):
        css = _read_static("style.css")
        assert "--ve-bg: #141414" in css

    def test_text_color(self):
        css = _read_static("style.css")
        assert "--ve-text: #FAF9F5" in css

    def test_text_dim(self):
        css = _read_static("style.css")
        assert "--ve-text-dim: #969591" in css

    def test_surface_background(self):
        css = _read_static("style.css")
        assert "--ve-bg-surface: #272622" in css

    def test_widget_background(self):
        css = _read_static("style.css")
        assert "--ve-bg-widget: #11263B" in css

    def test_green(self):
        css = _read_static("style.css")
        assert "--ve-green: #72B84C" in css

    def test_orange(self):
        css = _read_static("style.css")
        assert "--ve-orange: #F0962E" in css

    def test_red(self):
        css = _read_static("style.css")
        assert "--ve-red: #F35C58" in css


class TestHtmlReferences:
    """Verify index.html references external CSS and JS correctly."""

    def test_css_link(self):
        html = _read_static("index.html")
        assert 'href="/static/style.css"' in html

    def test_js_script(self):
        html = _read_static("index.html")
        # app.js was split into an ordered app-*.js bundle.
        assert 'src="/static/app-core.js"' in html
        assert 'src="/static/app-init.js"' in html


class TestNoInlineCode:
    """Verify no inline styles or scripts in index.html."""

    def test_no_inline_styles(self):
        html = _read_static("index.html")
        assert "<style>" not in html, "Found inline <style> tag in index.html"

    def test_no_inline_scripts(self):
        html = _read_static("index.html")
        # Find all <script tags -- each must have a src= attribute
        import re
        script_tags = re.findall(r"<script[^>]*>", html)
        for tag in script_tags:
            assert "src=" in tag, f"Found inline <script> without src: {tag}"


class TestSidebarNavigation:
    """Verify sidebar navigation structure for device-centric architecture."""

    def test_device_sidebar_container(self):
        html = _read_static("index.html")
        assert 'id="device-sidebar"' in html

    def test_add_device_button(self):
        """Add button is rendered inline in INVERTERS group header by JS."""
        js = _read_app_bundle()
        assert 'btn-add-device' in js
        assert 'showAddDeviceModal' in js

    def test_device_content_area(self):
        html = _read_static("index.html")
        assert 'id="device-content"' in html

    def test_sidebar_header(self):
        html = _read_static("index.html")
        assert "sidebar-header" in html


class TestResponsiveBreakpoints:
    """Verify responsive media queries exist."""

    def test_tablet_breakpoint(self):
        css = _read_static("style.css")
        assert "1024px" in css

    def test_mobile_breakpoint(self):
        css = _read_static("style.css")
        assert "768px" in css

    def test_has_media_queries(self):
        css = _read_static("style.css")
        assert "@media" in css


class TestLayoutClasses:
    """Verify key layout classes are defined in CSS."""

    def test_app_shell(self):
        css = _read_static("style.css")
        assert "app-shell" in css

    def test_ve_panel(self):
        css = _read_static("style.css")
        assert ".ve-panel" in css

    def test_ve_card(self):
        css = _read_static("style.css")
        assert ".ve-card" in css

    def test_ve_grid(self):
        css = _read_static("style.css")
        assert ".ve-grid" in css

    def test_ve_dot(self):
        css = _read_static("style.css")
        assert ".ve-dot" in css
