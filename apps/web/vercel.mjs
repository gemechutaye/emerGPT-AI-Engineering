// Build-time routing only. Provider keys and database credentials belong on the API host.
const value = process.env.EMER_BACKEND_ORIGIN;
if (!value) {
  throw new Error('Set EMER_BACKEND_ORIGIN to the HTTPS origin of the deployed EMER API.');
}
const backend = new URL(value);
if (
  backend.protocol !== 'https:' || backend.username || backend.password ||
  backend.pathname !== '/' || backend.search || backend.hash
) {
  throw new Error('EMER_BACKEND_ORIGIN must be an HTTPS origin without credentials, path, query or fragment.');
}

export const config = {
  framework: 'vite',
  installCommand: 'npm ci',
  buildCommand: 'npm run build',
  outputDirectory: 'dist',
  rewrites: [
    { source: '/api/:path*', destination: `${backend.origin}/api/:path*` },
    { source: '/share/:path*', destination: '/index.html' },
  ],
  headers: [
    {
      source: '/:path*',
      headers: [
        { key: 'X-Content-Type-Options', value: 'nosniff' },
        { key: 'Referrer-Policy', value: 'no-referrer' },
        { key: 'X-Robots-Tag', value: 'noindex, nofollow' },
        { key: 'Permissions-Policy', value: 'microphone=(self), camera=(), geolocation=()' },
        { key: 'Content-Security-Policy', value: "default-src 'self'; connect-src 'self' https://api.openai.com wss://api.openai.com; media-src 'self' blob:; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; frame-ancestors 'none'; base-uri 'self'" },
      ],
    },
    ...['/api/:path*', '/share/:path*'].map(source => ({
      source,
      headers: [
        { key: 'Cache-Control', value: 'no-store' },
        { key: 'CDN-Cache-Control', value: 'no-store' },
        { key: 'Vercel-CDN-Cache-Control', value: 'no-store' },
        { key: 'x-vercel-enable-rewrite-caching', value: '0' },
      ],
    })),
  ],
};
