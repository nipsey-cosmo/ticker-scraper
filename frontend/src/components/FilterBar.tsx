import type { ReactNode } from 'react';

import { linkProps, usePath } from '@/lib/router';

const TABS = [
  { path: '/', label: 'News' },
  { path: '/graphs', label: 'Charts' },
];

/**
 * The control strip under the ticker bar: whatever the page filters by on the
 * left, the two tabs on the right.
 *
 * Shared so the tabs sit in the same place on both pages and neither needs a
 * back link — the way out is always the tab you are not on.
 */
const FilterBar = ({ children }: { children: ReactNode }) => {
  const path = usePath();

  return (
    <div className="border-b border-rule bg-paper">
      <div className="mx-auto flex max-w-[1600px] flex-wrap items-center gap-2 px-3 py-2 md:gap-3 md:px-5">
        {children}

        {/* Width of the bar, not width of whatever is left over: the two pages
            put different controls on the left, so a flex-1 nav put the tabs in
            a different place on each and they jumped when you switched. A fixed
            share of the row pins the divider to one spot on both. */}
        <nav
          aria-label="Sections"
          className="ml-auto flex w-[28%] min-w-[180px] shrink-0 items-center justify-center gap-4"
        >
          {TABS.map((tab, index) => {
            const active = path === tab.path;
            return [
              index > 0 && <span key={`${tab.path}-sep`} className="text-rule">|</span>,
              <a
                key={tab.path}
                {...linkProps(tab.path)}
                aria-current={active ? 'page' : undefined}
                className={`pb-0.5 font-mono text-xs uppercase tracking-[0.08em] ${
                  active
                    ? 'border-b-2 border-ftblue font-semibold text-ink-strong'
                    : 'text-ink-muted hover:text-ink-strong'
                }`}
              >
                {tab.label}
              </a>,
            ];
          })}
        </nav>
      </div>
    </div>
  );
};

export default FilterBar;
