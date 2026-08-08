/**
 * The FT palette, expressed for Plotly.
 *
 * The reference dashboard was blue-on-white with an orange accent and 16px
 * rounded cards. This is the same chart grammar — the trace types, the axis
 * pairings, the two-panel splits — repainted onto Stocky's tokens: warm paper,
 * ftblue against claret, hairline rules instead of shadows, and no corner
 * radius anywhere. Values are duplicated from tailwind.config.ts because
 * Plotly takes colours as strings, not classes.
 */
export const C = {
  ink: '#33302E',
  inkStrong: '#1A1817',
  inkMuted: '#66605C',
  rule: '#CCC1B7',
  ruleLight: '#E6D9CE',
  paper: '#FFF1E5',
  paperTint: '#F2E5DA',
  blue: '#0F5499',
  claret: '#990F3D',
  teal: '#0D7680',
  up: '#006B45',
  down: '#CC0000',
} as const;

/** Translucent fills, for bars that sit behind a line. */
export const FILL = {
  blue: 'rgba(15, 84, 153, 0.30)',
  claret: 'rgba(153, 15, 61, 0.30)',
  blueSolid: 'rgba(15, 84, 153, 0.12)',
} as const;

/**
 * Diverging scale for sentiment: claret at −1, paper at 0, ftblue at +1.
 *
 * Anchored at zero rather than at the data's own range, so a neutral week is
 * the same colour on every chart and in every timeframe. A relative scale
 * would repaint the whole heatmap when one stock has a bad Tuesday.
 */
export const SENTIMENT_SCALE: [number, string][] = [
  [0, C.claret],
  [0.5, '#F6EDE4'],
  [1, C.blue],
];

const FONT = {
  family: '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif',
  color: C.ink,
  size: 11,
};

/** No mode bar, no scroll hijacking, resizes with its container. */
export const CONFIG = {
  responsive: true,
  displayModeBar: false,
  scrollZoom: false,
  doubleClick: false as const,
};

const AXIS = {
  gridcolor: C.ruleLight,
  linecolor: C.rule,
  zeroline: false,
  tickfont: { size: 10, color: C.inkMuted },
  automargin: true,
};

/** Merge order matters: `extra` wins, and its axes replace rather than merge. */
export const layout = (extra: Record<string, unknown> = {}) => ({
  paper_bgcolor: 'rgba(0,0,0,0)',
  plot_bgcolor: 'rgba(0,0,0,0)',
  font: FONT,
  margin: { l: 56, r: 20, t: 16, b: 44 },
  hoverlabel: {
    bgcolor: C.inkStrong,
    bordercolor: C.inkStrong,
    font: { color: C.paper, family: FONT.family, size: 11 },
  },
  legend: { orientation: 'h' as const, x: 0, y: 1.1, font: { size: 10 }, bgcolor: 'rgba(0,0,0,0)' },
  ...extra,
  xaxis: { ...AXIS, ...(extra.xaxis as object) },
  yaxis: { ...AXIS, ...(extra.yaxis as object) },
});

/** A sentiment score's colour: FT blue for constructive, claret for hostile.
 *  Green and red are reserved for price, where a sign really does mean money. */
export const scoreColor = (value: number | null | undefined) =>
  value === null || value === undefined ? C.inkMuted : value >= 0 ? C.blue : C.claret;

/** The −1..+1 axis every sentiment chart shares, so they stay comparable. */
export const SENTIMENT_AXIS = {
  range: [-1, 1],
  tickvals: [-1, -0.5, 0, 0.5, 1],
  zeroline: true,
  zerolinecolor: C.ink,
  zerolinewidth: 1,
};
