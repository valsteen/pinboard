# Visitor-guide visual language

The generated diagrams are static reading aids for a visitor who is scrolling through `HOW_IT_WORKS.md`. They should orient the eye before they ask for close reading. A reader should be able to notice the major groups, choose one card, and follow its relationships without operating controls or first reviewing every label.

## Evaluation criteria

- The overview must remain legible before the individual card text is read.
- Closer reading must reward the overview: labels and relationships must be accurate rather than merely suggestive.
- Position must carry meaning. A row, column, lane, or change in elevation needs a domain or ownership reason.
- Visual emphasis must be earned by information importance. Decorative numerals, curves, framing, or slogans do not earn attention by themselves.
- Separation should make distinct responsibilities visible without adding labels that merely say they are distinct.
- The relational-ledger view is the density test. A visual grammar that only works for a short linear workflow is not sufficient.

## Stable grammar

Cards are flat and rectangular, with a surface that remains distinct from the canvas in both reading themes. A narrow left bar establishes the structural accent. Small monospaced labels identify a state, layer, table, or source symbol; the main title states the human meaning; supporting facts remain visually quieter.

The quietest text must remain readable at normal GitHub content width on a 1920×1080 display without browser zoom. When a larger shared type scale exposes a crowded card or route, rephrase or adjust that composition instead of shrinking the text below the common scale.

Every text line stays inside the card's content area with a deliberate right margin. Merely avoiding literal overflow is not enough: a title, detail, or source name must not appear to bump into the border.

Text is painted after geometry. Free-standing labels use a canvas-colored clearance edge, and authored line breaks keep them off meaningful routes, so no grid, guide, connector, card, or frame can consume a glyph.

Connectors are semantic marks, not decoration:

- Routes are horizontal or vertical. Soft elbows are used only when a route must turn.
- Every bend has a straight segment before the arrowhead.
- A connector meets the middle region of a card edge, never a fragile corner.
- Connectors do not cross text, graze unrelated cards, run closely parallel to a card, stop without a destination, or make a detour that carries no meaning.
- Every connector segment keeps a visible clearance corridor around unrelated cards. A route must never overlap or visually merge with another card's border; move it above, below, or farther beside the card instead.
- Distinct relationships use distinct card ports. A shared trunk is reserved for a relationship that genuinely branches.
- Filled arrowheads are the default direction marker. Another arrowhead style requires another real relationship meaning.
- A short statement is preferable to a long route through unrelated groups.

The day canvas uses a warm paper tone; the night canvas uses muted navy selected against GitHub's dark reading surface. Both use a visible square grid and a thin outer border. The grid provides spatial stability across sparse and dense diagrams and must remain legible at normal GitHub reading width. Meaningful group guides remain stronger than the grid. Neither palette adds gradients, shadows, or decorative effects.

## Source ownership

- `model.py` owns the reusable primitives, named day and night palettes, soft-elbow rendering, and mechanically checkable geometry rules. Rendering receives one complete palette rather than selecting or hardcoding theme colors inside drawing helpers.
- `ambiguity_closure.py`, `brief.py`, `product.py`, `layers.py`, `journey.py`, and `database.py` own the semantic inventory and authored composition of their diagrams.
- `render.py` owns the guide text, output paths, dependency validation, and content-based freshness command.
- `HOW_IT_WORKS.md`, the day `assets/how-it-works/*.svg` files, and their `*-dark.svg` counterparts are generated projections and are never edited directly.

The composition is deliberately authored rather than delegated to a general graph layout engine. Automatic placement may be introduced only if it preserves the same semantic axes, stable ports, label clearance, and routing quality on the relational-ledger density test.

## Updating or varying the diagrams

Change the relevant semantic seed when product scope, architecture, a representative flow, or the database schema changes. Change the renderer or its named palettes when the shared visual language changes. Every diagram is rendered from the same semantic seed and geometry in both palettes; a theme variation must not carry different content, layout, typography, routes, or emphasis semantics. Keep style tokens separate from semantic content so another document can vary the palette, type, or surface without discarding the evaluation criteria and connector grammar.

Freshness is the exact generated content, not a checksum of every related authority. The source validators reject relevant package, architecture, symbol, and schema drift. If a source edit passes those validators without changing visitor-facing output, regeneration leaves the committed guide and diagrams untouched.

Generate the outputs with:

```console
uv run --locked python -m docs.how_it_works.render
```

Inspect the generated guide in both day and night modes at its normal reading width, including the relational ledger. Then verify freshness with:

```console
uv run --locked python -m docs.how_it_works.render --check
```

Revisit the grammar when a real diagram cannot express an accurate relationship without violating it. Do not weaken a rule merely to preserve an existing placement.
