# UI Playbook

Conventions for building consistent forms, panels and editors in the
PV-Inverter-Master web app.
This playbook covers **layout and interaction**: where things go and
how they behave.

The rule of thumb: a user who has seen one config screen should be able
to predict where the Save button is on every other one.

---

## 1. Two form contexts — pick the right one

There are exactly two places a user edits and saves data. Decide which
one you are building before writing markup.

| Context | Container | Used for |
|---------|-----------|----------|
| **Config panel** | `ve-panel` | A full-page settings screen — device config, Standort form, Venus/Influx/MQTT config |
| **Card editor** | `ve-card` | A small editable region inside a dashboard widget — DC-string group names, aggregation membership |

A third case, **entity creation**, uses a modal with a footer action
bar (e.g. the add-device modal). Modals are not covered further here —
keep their actions in the modal footer, that is the standard.

---

## 2. Config panel layout (`ve-panel`)

This is the canonical settings screen. Structure, top to bottom:

```
┌─ ve-panel ─────────────────────────────────────────┐
│ ve-panel-header                                    │
│   <h2>Title</h2>        [Cancel] [Save]  ← actions │
├────────────────────────────────────────────────────┤
│ form fields …                                      │
│                                                    │
│ ve-panel-danger-section                            │
│   [Delete]                                         │
└────────────────────────────────────────────────────┘
```

### Rules

1. **Save/Cancel live in the panel header — never at the bottom.**
   Put them in `ve-panel-header`, right-aligned, as a `ve-btn-pair`:
   ```html
   <div class="ve-panel-header">
     <h2>Device Configuration</h2>
     <span class="ve-btn-pair ve-xxx-save-pair" style="display:none">
       <button class="ve-btn ve-btn--sm ve-btn--cancel ve-xxx-cancel">Cancel</button>
       <button class="ve-btn ve-btn--sm ve-btn--save ve-xxx-save">Save</button>
     </span>
   </div>
   ```
   If the panel also needs a back/secondary button, wrap the header's
   right side in `ve-panel-header-actions` (a flex row) so the back
   button and the `ve-btn-pair` sit together — see the Standort form.

2. **The save pair is dirty-gated.** It starts `display:none` and only
   appears once a field changes. A *create-new* form is the exception —
   there is nothing to revert to, so the pair is visible from the start.

3. **Cancel reverts, it does not navigate.** Clicking Cancel restores
   every field to its last-saved value and hides the save pair. Leaving
   the screen is a separate affordance (a back button, the sidebar).

4. **Delete goes to the bottom**, in `ve-panel-danger-section`, as a
   `ve-btn--danger` button. Destructive actions are never in the header
   and never next to Save.

5. **After a successful save**, re-snapshot the originals and hide the
   save pair — the freshly-saved state is the new baseline.

### Dirty tracking

Every config panel tracks dirtiness the same way:

- On build, snapshot every field's value into an `originals` object.
- On every `input` (and `change`, for checkboxes/selects) event, compare
  current values to `originals`.
- Show the save pair when anything differs; hide it when all match.
- Mark each changed field with `ve-input--dirty` (subtle green border).
- Cancel writes `originals` back into the fields and re-checks.
- If a field is set programmatically (e.g. geocoding fills lat/lon), the
  code must call the dirty-check itself — `.value = …` fires no event.

Reference implementations: `buildInverterConfigForm` and
`buildLocationForm` in `static/app.js`.

---

## 3. Card editor layout (`ve-card`)

A dashboard widget that contains a small editable region (not a whole
settings screen). A `ve-card` has only an `h2.ve-card-title`, no header
action space — so here the save pair sits at the **bottom of the
editable region**, paired with any helper text:

```html
<div class="ve-xxx-foot">
  <span class="ve-xxx-hint">Short explanation of the field.</span>
  <span class="ve-btn-pair ve-xxx-save-pair" style="display:none">
    <button class="ve-btn ve-btn--sm ve-btn--cancel ve-xxx-cancel">Abbrechen</button>
    <button class="ve-btn ve-btn--sm ve-btn--save ve-xxx-save">Speichern</button>
  </span>
</div>
```

Same dirty-gating and Cancel-reverts rules as a config panel — only the
*position* differs (bottom of the card, because a card has no header bar
to host actions). Reference: the DC-string group editor
(`_wireStringGroups`) and the aggregation editor.

Do **not** invent a third placement. If it is a `ve-panel`, the save
pair is in the header; if it is a `ve-card` editor, it is at the bottom.

---

## 4. Buttons

- Save + Cancel always travel together inside a `ve-btn-pair`, **Cancel
  left, Save right**.
- Save: `ve-btn ve-btn--save` (green). Cancel: `ve-btn ve-btn--cancel`.
  Delete: `ve-btn ve-btn--danger`.
- In headers and card editors use the small size, `ve-btn--sm`.
- A primary non-save action uses `ve-btn--primary` (blue).
- Never leave a Save button visible when the form is clean.

---

## 5. Selects with an "implicit default" — the sentinel-option pattern

Some fields have a value that the user *might* have actively chosen, or
might just be the silent default. The phase dropdown on single-phase
inverters is the canonical example: every entry starts at `phase=1`,
but until the user (or Phasen-Detective) commits to an L1/L2/L3
assignment, that 1 is meaningless. We need to surface the difference
*and* let the user move between the two states cleanly.

The rule of thumb: render **four options**, not three. Use one extra
option as a sentinel value the server interprets as "reset to default".

```html
<select>
  <option value="0">L1 (Standard)</option>   <!-- implicit default -->
  <option value="1">L1</option>              <!-- explicit L1 -->
  <option value="2">L2</option>
  <option value="3">L3</option>
</select>
```

- The selected value is `0` while `phase_explicit=false`, else `phase`.
- Save payload sends `0/1/2/3`. Server side: `0` → `phase=1, explicit=False`,
  otherwise `phase=N, explicit=True`.
- The dirty check compares the dropdown's *selected value* (string), not
  the underlying phase int. Round-trips cleanly.

Pair this with a derived button label so the rest of the UI also
reflects the implicit/explicit state — e.g. `"Erkennung starten"`
becomes `"Erkennung verifizieren"` once an explicit value has been
saved. The action verb tells the user what their next click *means*.

Reference: `_phaseOptionsHtml()` + `_detectiveButtonLabel()` in
`app-inverter-config.js`.

---

## 6. Result cards — three variants for "an algorithm just ran"

When an async background task (Phase-Detective, an import, etc.) finishes,
the result card has three canonical states. Pick the right one based on
what the user can or should do next:

| State | Card class | What it shows | Buttons |
|-------|-----------|---------------|---------|
| **Auto-applied** | `ve-hint-card--success` | "✓ L2 erkannt & automatisch übernommen" + metrics | none — the change is committed |
| **Verify OK** | `ve-hint-card--success` | "✓ Verifikation OK — L2" + metrics | none |
| **Mismatch / weak / failed** | `ve-hint-card` (warning) | "⚠ erkannt L2, gespeichert L1" + reason | `ve-btn--primary` to manually apply, OR explanation if nothing to do |

Rules:

- Auto-apply only happens in the "first encounter" case (the entry was
  in its implicit default). On any re-run, surface the result and let
  the user decide — never silently overwrite a previous explicit
  assignment.
- Keep the metrics line short (one line ≤ 100 chars). The detail belongs
  in the per-step log, not the result card.
- When a manual-apply button is rendered, after click change its label
  to confirm the dirty state (e.g. `"Übernommen — Save Pair nutzen"`)
  rather than removing it.

Reference: `renderResult()` in `app-inverter-config.js`.

---

## 7. Quick checklist

Before shipping a form, confirm:

- [ ] `ve-panel` → Save/Cancel in `ve-panel-header`; `ve-card` editor →
      Save/Cancel at the bottom of the editable region.
- [ ] Save pair is `display:none` until the form is dirty (except a
      create-new form).
- [ ] Cancel reverts field values; it does not navigate away.
- [ ] Changed fields get `ve-input--dirty`.
- [ ] Programmatic value changes call the dirty-check explicitly.
- [ ] Delete (if any) is in `ve-panel-danger-section` at the bottom.
- [ ] Save pair hides again after a successful save.
- [ ] Tokens only — `var(--ve-*)`, no hardcoded colors/durations.
