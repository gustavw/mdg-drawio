"""Library-invariant constants shared across packages.

These replace scattered magic numbers with named values, making the code
more readable and easier to tune.
"""

# ---------------------------------------------------------------------------
# Default node geometry — used when size lookup fails
# ---------------------------------------------------------------------------
from __future__ import annotations

DEFAULT_NODE_WIDTH = 120.0
DEFAULT_NODE_HEIGHT = 60.0

# ---------------------------------------------------------------------------
# Default page dimensions (draw.io canvas)
# ---------------------------------------------------------------------------
DEFAULT_PAGE_WIDTH = 800
DEFAULT_PAGE_HEIGHT = 600

# A4 landscape (points)
A4_LANDSCAPE_WIDTH = 827.0
A4_LANDSCAPE_HEIGHT = 1169.0

# ---------------------------------------------------------------------------
# Generator canvas attributes (mxGraphModel defaults)
# ---------------------------------------------------------------------------
CANVAS_DX = 1422
CANVAS_DY = 794

# ---------------------------------------------------------------------------
# Reserved mxCell IDs (draw.io protocol)
# ---------------------------------------------------------------------------
ROOT_CELL_ID = "0"
PAGE_CELL_ID = "1"

# ---------------------------------------------------------------------------
# Layout defaults
# ---------------------------------------------------------------------------
DEFAULT_BOUNDARY_PADDING = 20.0

# ---------------------------------------------------------------------------
# Layout config defaults (dataclass field defaults in LayoutConfig)
# ---------------------------------------------------------------------------
DEFAULT_MARGIN_X = 40
DEFAULT_MARGIN_Y = 40

# Default container padding overrides. Bottom matches top so a container hugs
# its children symmetrically (the title band already adds room above).
DEFAULT_TOP_PADDING = 30
DEFAULT_BOTTOM_PADDING = 30

# Clearance added around a swimlane/pool title's estimated text width when
# sizing the container to fit its own rotated or unrotated header on one line.
ROTATED_LABEL_PADDING = 16.0

# ---------------------------------------------------------------------------
# C4 text-driven shape scaling defaults
# ---------------------------------------------------------------------------
C4_SCALER_HORIZONTAL_PADDING = 96
C4_SCALER_VERTICAL_PADDING = 44
C4_SCALER_LINE_HEIGHT = 15
C4_SCALER_TITLE_LINE_HEIGHT = 21
C4_SCALER_FRAGMENT_GAP = 5
C4_SCALER_EXTRA_LINE_COUNT = 1
C4_SCALER_RECTANGULAR_WIDTH_SCALE = 0.8
C4_SCALER_RECTANGULAR_HEIGHT_SCALE = 0.8
C4_SCALER_BASE_WIDTH_CUSHION = 1.35
C4_SCALER_BASE_MAX_WIDTH = 460
C4_SCALER_WIDTH_CUSHION = (
    C4_SCALER_BASE_WIDTH_CUSHION * C4_SCALER_RECTANGULAR_WIDTH_SCALE
)
C4_SCALER_MAX_WIDTH = C4_SCALER_BASE_MAX_WIDTH * C4_SCALER_RECTANGULAR_WIDTH_SCALE
C4_SCALER_PERSON_ASPECT_RATIO = 200 / 180
C4_SCALER_SUBTITLE_KEY = "c4Subtitle"

# ---------------------------------------------------------------------------
# Text-driven scaling defaults for small, plain (non-bold ~12px label) boxes
# shared across several notations' row-less leaf shapes (ERD entities, plain
# general-purpose boxes, UML/UML25 free-text shapes, BPMN2's text annotation
# -- see the corresponding _<notation>_shape_scaling factories in
# engine/convert.py). Row/stack-based containers (ERD Table, general List,
# ...) size from their own row content instead and are excluded there.
# ---------------------------------------------------------------------------
SMALL_BOX_SCALER_HORIZONTAL_PADDING = 24
SMALL_BOX_SCALER_VERTICAL_PADDING = 20
SMALL_BOX_SCALER_TITLE_FONT_SIZE = 12
SMALL_BOX_SCALER_TITLE_LINE_HEIGHT = 16

# Character-width estimates deliberately do NOT live here: they are read by
# ``layout/_container_layout.estimate_text_width`` and nothing else, so they
# stay named next to that algorithm. Duplicating them here once produced two
# sets of values that disagreed (the copies here were also misnamed -- 6.5 is
# the AVERAGE character width, not the narrow one) with nothing importing
# either. This module is for values genuinely shared across packages.

# ---------------------------------------------------------------------------
# Palette layout defaults
# ---------------------------------------------------------------------------
# Layout mode that renders a shape palette / golden reference verbatim. Type-
# level rendering overrides are bypassed in this mode so palette artifacts stay
# true to the raw palette.
PALETTE_MODE = "palette"
PALETTE_DEFAULT_PAGE_WIDTH = 1200
PALETTE_DEFAULT_PAGE_HEIGHT = 800

# ---------------------------------------------------------------------------
# DSL parser
# ---------------------------------------------------------------------------
PAGE_PREFIX_LENGTH = 5   # len("page ")
QUOTE_OFFSET = 6         # len('page "')
