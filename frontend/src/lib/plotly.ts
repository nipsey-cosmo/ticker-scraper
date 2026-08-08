import Plotly from 'plotly.js/lib/core';
import bar from 'plotly.js/lib/bar';
import box from 'plotly.js/lib/box';
import candlestick from 'plotly.js/lib/candlestick';
import heatmap from 'plotly.js/lib/heatmap';

/**
 * A Plotly build carrying only the five trace types this page draws.
 *
 * `plotly.js-dist` is ~4.5MB of source for forty trace types — globes, ternary
 * plots, sankeys, mesh3d. In an app that refuses to load a webfont because it
 * would block the first paint, shipping that to draw seven charts is not a
 * trade worth making. Core plus five registrations is roughly a tenth of it.
 *
 * `core` already includes scatter, which is why it isn't registered below.
 *
 * This module is only ever reached through the lazy import of the graphs page,
 * so it lands in its own chunk and the home page never downloads it.
 */
Plotly.register([bar, box, candlestick, heatmap]);

export default Plotly;
