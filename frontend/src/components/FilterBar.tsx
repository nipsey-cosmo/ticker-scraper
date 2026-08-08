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

        {/* Takes the whole leftover row and spreads the tabs through it, rather
            than clustering them against the right edge. */}
        <nav aria-label="Sections" className="flex flex-1 items-center justify-evenly">
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
