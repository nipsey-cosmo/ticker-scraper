import { act, renderHook } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { linkProps, navigate, usePath } from './router';

/** Restore the path between tests — jsdom keeps one history per file. */
afterEach(() => window.history.pushState(null, '', '/'));

const clickEvent = (overrides: Partial<MouseEvent> = {}) => ({
  defaultPrevented: false,
  metaKey: false,
  ctrlKey: false,
  shiftKey: false,
  altKey: false,
  button: 0,
  preventDefault: vi.fn(),
  ...overrides,
}) as unknown as React.MouseEvent;

describe('router', () => {
  it('re-renders subscribers when the path changes', () => {
    const { result } = renderHook(() => usePath());
    expect(result.current).toBe('/');

    act(() => navigate('/graphs'));
    expect(result.current).toBe('/graphs');
  });

  it('follows the back button', () => {
    const { result } = renderHook(() => usePath());
    act(() => navigate('/graphs'));

    act(() => {
      window.history.pushState(null, '', '/');
      window.dispatchEvent(new PopStateEvent('popstate'));
    });
    expect(result.current).toBe('/');
  });

  it('does not stack duplicate entries for the current path', () => {
    const length = window.history.length;
    navigate('/');
    expect(window.history.length).toBe(length);
  });

  it('keeps the href so the link is copyable and middle-clickable', () => {
    expect(linkProps('/graphs').href).toBe('/graphs');
  });

  it('intercepts a plain left click', () => {
    const event = clickEvent();
    linkProps('/graphs').onClick(event);
    expect(event.preventDefault).toHaveBeenCalled();
    expect(window.location.pathname).toBe('/graphs');
  });

  it('leaves modified clicks to the browser', () => {
    // Ctrl/cmd-click opens a new tab, and hijacking it would break that.
    for (const modifier of ['metaKey', 'ctrlKey', 'shiftKey', 'altKey'] as const) {
      const event = clickEvent({ [modifier]: true });
      linkProps('/graphs').onClick(event);
      expect(event.preventDefault).not.toHaveBeenCalled();
      expect(window.location.pathname).toBe('/');
    }
  });

  it('leaves middle clicks alone', () => {
    const event = clickEvent({ button: 1 });
    linkProps('/graphs').onClick(event);
    expect(event.preventDefault).not.toHaveBeenCalled();
    expect(window.location.pathname).toBe('/');
  });
});
