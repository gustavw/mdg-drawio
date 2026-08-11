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
DEFAULT_PAGE_MARGIN = 40.0

# ---------------------------------------------------------------------------
# Layout config defaults (dataclass field defaults in LayoutConfig)
# ---------------------------------------------------------------------------
DEFAULT_MARGIN_X = 40
DEFAULT_MARGIN_Y = 40
DEFAULT_LANE_MARGIN_X = 40
DEFAULT_LANE_MARGIN_Y = 50
DEFAULT_COLUMN_GAP = 80

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
# Container layout minimums
# ---------------------------------------------------------------------------
MIN_CONTAINER_WIDTH = 240.0
MIN_CONTAINER_HEIGHT = 160.0

# ---------------------------------------------------------------------------
# Character width estimates (container_layout)
# ---------------------------------------------------------------------------
NARROW_CHAR_WIDTH = 6.5
WIDE_CHAR_WIDTH = 9.5
RELATIVE_CHAR_WIDTH = 1.1

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
