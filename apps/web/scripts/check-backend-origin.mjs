// Run inside the build, where project environment values are actually available.
const value = process.env.EMER_BACKEND_ORIGIN;
if (!value) {
  throw new Error('Set EMER_BACKEND_ORIGIN to the HTTPS origin of the deployed EMER API.');
}
let backend;
try {
  backend = new URL(value);
} catch {
  throw new Error('EMER_BACKEND_ORIGIN must be a valid HTTPS origin.');
}
if (
  backend.protocol !== 'https:' || backend.username || backend.password ||
  backend.pathname !== '/' || backend.search || backend.hash || value !== backend.origin
) {
  throw new Error('EMER_BACKEND_ORIGIN must be an HTTPS origin without credentials, path, query, fragment or trailing slash.');
}
