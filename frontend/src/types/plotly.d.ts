/**
 * Minimal declarations for the custom Plotly build in `@/lib/plotly`.
 *
 * `@types/plotly.js` describes `plotly.js-dist`, not `plotly.js/lib/core` plus
 * registrations, and pulling it in would type a bundle this app deliberately
 * does not ship. Only the four calls actually made are declared; `data` and
 * `layout` stay `unknown[]`/`object` because every chart passes a different
 * trace shape and a hand-written union would be wrong within a week.
 */
declare module 'plotly.js/lib/core' {
  interface PlotlyStatic {
    react(el: HTMLElement, data: unknown[], layout?: object, config?: object): Promise<void>;
    purge(el: HTMLElement): void;
    register(modules: unknown[]): void;
    Plots: { resize(el: HTMLElement): void };
  }
  const Plotly: PlotlyStatic;
  export default Plotly;
}

declare module 'plotly.js/lib/bar' {
  const trace: unknown;
  export default trace;
}
declare module 'plotly.js/lib/box' {
  const trace: unknown;
  export default trace;
}
declare module 'plotly.js/lib/candlestick' {
  const trace: unknown;
  export default trace;
}
declare module 'plotly.js/lib/heatmap' {
  const trace: unknown;
  export default trace;
}
