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

        {/* 320px right-aligned inside the same max-w/padding the pages use, so
            the tabs sit centred over the right-hand rail — the Pinned tickers
            column on the front page. Sized from the bar rather than from
            whatever the controls left over, which differs per page and used to
            make the tabs jump as you switched. */}
        <nav
          aria-label="Sections"
          className="ml-auto flex w-80 max-w-full shrink-0 items-center justify-center gap-4"
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
