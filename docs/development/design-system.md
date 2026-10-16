# Web design system

The web app uses a "sticker book" design: crisp outlined, rounded cards and pill controls on a
sky-wash canvas, with bright colors reserved for small, meaningful accents. It adapts the palette
and component language of the [Slush](https://styles.refero.design/style/8b6b547f-a357-4f1b-9842-4579c62dd42b)
reference without copying its layouts or typography scale.

Everything lives in two stylesheets with no build step:

- `web/static/app.css`: tokens, base elements, shell, navigation, and shared components.
- `web/static/operational.css`: page workspaces (dashboard, deals, sales, listing prep).

## Tokens

Define colors once on `:root` and reference them by role. Older rules use legacy aliases
(`--surface`, `--tint`, `--danger`, …) that map onto the tokens below.

| Group | Tokens | Use |
| --- | --- | --- |
| Surfaces | `--canvas`, `--paper`, `--paper-2`, `--field`, `--deep` | Page, cards, inset panels, form controls, code blocks |
| Text and lines | `--ink`, `--ink-2`, `--muted`, `--line`, `--line-soft` | Body text, outlines, row separators |
| Brand accents | `--yellow`, `--mint`, `--lavender`, `--orange`, `--violet`, `--sky-deep` | Active navigation, price tags, nav dots, hover |
| Text and edges on fills | `--on-ink`, `--on-accent`, `--accent-edge` | Text on ink-filled controls, text on bright fills, outline of bright fills |
| Profit / confirmed | `--profit-ink`, `--profit-soft`, `--profit-edge` | Realized results, confirmed categories |
| Loss / attention | `--loss-ink`, `--loss-soft` | Losses, incomplete reconciliation, errors |
| Provisional | `--provisional-ink`, `--provisional-soft` | Recorded-but-not-final profit, estimates |
| Warning | `--warning-ink`, `--warning-soft` | Cautions, missing evidence |
| Unknown | `--unknown-ink`, `--unknown-soft` | Values that are not known |
| Shape | `--radius` (22px), `--radius-sm`, `--radius-xs`, `--pill`, `--stroke` (1.5px) | Cards, controls, outlines |

### Financial meaning

Financial state is never carried by color alone; every state also has a text label.

- **Realized** profit uses the profit tokens and appears only for fully reconciled sales.
- **Provisional** (recorded but incomplete) uses the provisional tokens and says *provisional*.
- **Unknown** values use `.unknown-value`: a dashed chip reading *Unknown* or *Unavailable*, so an
  unknown never looks like `USD 0.00`.
- Lifecycle badges (`acquired`, `listed`, `sold`, `archived`) use neutral or brand fills that do
  not overlap the profit and loss colors.

## Typography

The CSP has no `font-src`, so no web fonts are downloaded or bundled. Stacks use installed system
fonts:

- `--font-display`: `ui-rounded` (SF Pro Rounded on Apple devices), then the system UI font at
  weight 900, for page titles and headline numbers.
- `--font-body`: Inter when installed, then the system UI font, for operational content.
- `--font-mono`: Q-numbers and code.

Financial figures use `font-variant-numeric: tabular-nums`. Display sizes are capped so dense
tables and accounting forms stay at body size.

## Components

| Class | Purpose |
| --- | --- |
| `.panel` | Outlined card |
| `.metrics` / `.metrics-3` | Headline number tiles; `.metric-primary`, `-profit`, `-provisional`, `-attention`, `-sky` set meaning |
| `.badge` | Pill status sticker; modifier classes match lifecycle and reconciliation states |
| `.q-chip` | Inventory-sticker label for Q- and S-numbers |
| `.button`, `.button.secondary`, `.button.accent`, `.button.small` | Pill buttons; `.small` applies only to fine pointers |
| `.deal-price`, `.asking` | Price-tag treatment for source asking prices |
| `.recon-grid` / `.recon-card` | Per-category reconciliation checklist |
| `.unknown-value` | Dashed chip for unknown amounts |
| `.prep-tag` (`.user`, `.estimate`, `.fact`) | Provenance labels in Quick Listing Prep |
| `.empty`, `.prep-limit` | Dashed empty and limitation states |

## Accessibility and responsiveness

- Focus is a 3px `--focus` outline on every interactive element.
- Below 900px the navigation becomes a wrapped four-column grid; touch layouts use 44px controls and
  16px form text. Dense tables scroll inside focusable regions with a pinned first column.
- `prefers-reduced-motion: reduce` disables transitions.
- Pages contain no scripts or inline styles, which the CSP and tests enforce.

Several minified rules in `app.css` are asserted verbatim by `tests/test_web_mobile.py` (the
phone/touch media block, table scrolling, overflow wrapping, and navigation grid). Keep their exact
text when editing them.

## Known gaps

- Remote marketplace photos are not shown on Deals cards because the CSP allows only same-origin
  images; cards show a labeled placeholder instead.
- Bundling an open-source display font would require adding `font-src 'self'` to the CSP.
- There is a single light theme. Every color is a token on `:root`, so a dark variant needs only an
  alternate token block (a dark draft was explored during the redesign and set aside).
