import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { Suspense, lazy } from 'react';

import { usePath } from './lib/router';
import Home from './pages/Home';

// Behind a route, so Plotly and the seven charts stay out of the front page's
// bundle entirely — the graphs chunk is larger than everything else combined.
const Graphs = lazy(() => import('./pages/Graphs'));

/**
 * Two screens, one provider.
 *
 * Still no react-router: `lib/router.ts` is the History API and a popstate
 * listener, which is all two routes need. No theme provider either — FT is one
 * palette on paper, and a dark mode nobody asked for is a class toggle plus a
 * second set of tokens to keep in sync.
 */
const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // The API is local, so a failed request means the backend is down;
      // retrying once surfaces that quickly instead of hanging.
      retry: 1,
      refetchOnWindowFocus: false,
    },
  },
});

const Routes = () => {
  const path = usePath();

  if (path === '/graphs') {
    // No spinner: the fallback flashes for the length of one chunk fetch on
    // localhost, and an empty paper-coloured screen reads better than a
    // spinner that appears and vanishes.
    return (
      <Suspense fallback={<div className="min-h-screen bg-paper" />}>
        <Graphs />
      </Suspense>
    );
  }
  return <Home />;
};

const App = () => (
  <QueryClientProvider client={queryClient}>
    <Routes />
  </QueryClientProvider>
);

export default App;
