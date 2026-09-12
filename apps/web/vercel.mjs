// Explicit deployment-time environment reference; never put provider secrets here.
// Vercel resolves this public origin in its routing layer, after config parsing.
export const config = {
  framework: 'vite',
  installCommand: 'npm ci',
  buildCommand: 'node scripts/check-backend-origin.mjs && npm run build',
  outputDirectory: 'dist',
  routes: [
    {
      src: '/(.*)',
      headers: {
        'X-Content-Type-Options': 'nosniff',
        'Referrer-Policy': 'no-referrer',
        'X-Robots-Tag': 'noindex, nofollow',
        'Permissions-Policy': 'microphone=(self), camera=(), geolocation=()',
        'Content-Security-Policy': "default-src 'self'; connect-src 'self' https://api.openai.com wss://api.openai.com; media-src 'self' blob:; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; frame-ancestors 'none'; base-uri 'self'",
      },
      continue: true,
    },
    {
      src: '/(?:api|share)(?:/.*)?',
      headers: {
        'Cache-Control': 'no-store',
        'CDN-Cache-Control': 'no-store',
        'Vercel-CDN-Cache-Control': 'no-store',
        'x-vercel-enable-rewrite-caching': '0',
      },
      continue: true,
    },
    { src: '/api/(.*)', dest: '$EMER_BACKEND_ORIGIN/api/$1', env: ['EMER_BACKEND_ORIGIN'] },
    { handle: 'filesystem' },
    { src: '/share/(.*)', dest: '/index.html' },
  ],
};
