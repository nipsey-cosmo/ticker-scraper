import { useEffect, useState } from 'react';

/**
 * Two routes, so: the History API.
 *
 * react-router would ship a matcher, a history abstraction and a context tree
 * to decide between "/" and "/graphs" — more code than the whole graphs page's
 * routing needs. This is the platform feature it wraps.
 *
 * Both the dev server and `scripts/serve-dist.mjs` already fall back to
 * index.html for unknown paths, so a hard reload on /graphs works without any
 * further configuration.
 */

/** popstate only fires for back/forward, so pushes announce themselves. */
const CHANGED = 'stocky:navigated';

export const navigate = (path: string) => {
  if (path === window.location.pathname) return;
  window.history.pushState(null, '', path);
  window.dispatchEvent(new Event(CHANGED));
  window.scrollTo({ top: 0 });
};

export const usePath = (): string => {
  const [path, setPath] = useState(() => window.location.pathname);

  useEffect(() => {
    const sync = () => setPath(window.location.pathname);
    window.addEventListener('popstate', sync);
    window.addEventListener(CHANGED, sync);
    return () => {
      window.removeEventListener('popstate', sync);
      window.removeEventListener(CHANGED, sync);
    };
  }, []);

  return path;
};

/**
 * Click handler for an in-app link.
 *
 * Kept as a real `<a href>` in the markup so the link is middle-clickable,
 * copyable and announced as a link; this only intercepts the plain left click
 * that would otherwise reload the whole bundle.
 */
export const linkProps = (path: string) => ({
  href: path,
  onClick: (event: React.MouseEvent) => {
    if (event.defaultPrevented || event.metaKey || event.ctrlKey ||
        event.shiftKey || event.altKey || event.button !== 0) {
      return;
    }
    event.preventDefault();
    navigate(path);
  },
});
