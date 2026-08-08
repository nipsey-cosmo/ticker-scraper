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

        <nav aria-label="Sections" className="ml-auto flex shrink-0 items-center">
          {TABS.map((tab, index) => {
            const active = path === tab.path;
            return (
              <span key={tab.path} className="flex items-center">
                {index > 0 && <span className="px-2 text-rule">|</span>}
                <a
                  {...linkProps(tab.path)}
                  aria-current={active ? 'page' : undefined}
                  className={`font-mono text-xs uppercase tracking-[0.08em] ${
                    active
                      ? 'border-b-2 border-ftblue pb-0.5 font-semibold text-ink-strong'
                      : 'pb-0.5 text-ink-muted hover:text-ink-strong'
                  }`}
                >
                  {tab.label}
                </a>
              </span>
            );
          })}
        </nav>
      </div>
    </div>
  );
};

export default FilterBar;
