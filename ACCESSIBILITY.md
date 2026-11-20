# Accessibility

Quantfolio aims to be usable with a keyboard, a screen reader and on a phone, in light and dark themes.

## What is in place

- **Structure:** every page has exactly one `h1` (the browser smoke test fails a page that does not), landmarks from the app shell, and breadcrumbs.
- **Keyboard:** all controls are native buttons, links and form fields or Radix UI primitives with keyboard support; dialogs trap focus and return it; the command palette opens with `Ctrl/Cmd+K`.
- **Touch:** buttons are at least 44 px high on phones (WCAG 2.5.5).
- **Figures:** numbers use tabular figures, and gains and losses carry a sign as well as a colour.
- **Language:** labels and help texts are written in plain words, and every estimate says that it is one.

## Supported environments

Current Chrome, Edge, Firefox and Safari on desktop, and Safari (iOS) and Chrome (Android) on phones. The browser smoke test runs every page at a desktop and a phone viewport.

## Known limitations

- Not every chart has a text or table equivalent yet.
- Long tables scroll horizontally on small screens.
- No formal audit against WCAG 2.2 AA has been done yet.

## Reporting a barrier

Open an issue with the **Accessibility barrier** template: the page, what you tried, the browser and assistive technology you used, and what happened. Barriers are treated as bugs.
