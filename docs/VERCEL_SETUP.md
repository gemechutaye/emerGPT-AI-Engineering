# Vercel frontend setup

The `emergpt-ai-engineering` project is connected to
`gemechutaye/emerGPT-AI-Engineering` in the personal `gemechutayes-projects`
workspace. Its root directory is `apps/web`, with Node 22.x. This existing
workspace already has a Pro subscription; preparation did not purchase or change
a plan or enable paid add-ons. No application deployment has run yet.

The repository's `vercel.mjs` configures Vite, `npm ci`, `npm run build` and the
`dist` output.
Use a current Vercel CLI for programmatic configuration; preparation uses 59.16.0.

Set one build environment variable: `EMER_BACKEND_ORIGIN`, containing the real
HTTPS origin of the separate EMER API service. The build fails if it is missing
or contains credentials, a path or query parameters. Do not put provider keys or
the database connection in Vercel or any `VITE_*` variable.

The configuration forwards `/api/*` to the backend without changing the browser
origin. This preserves the existing frontend API paths and session cookie flow.
It also routes `/share/*` to the SPA, applies the application's static response
headers, and disables caching for API and shared conversation responses.

Set the backend's `APP_ORIGIN` to the exact Vercel URL used for the demo and set
`COOKIE_SECURE=true`. An arbitrary preview URL is not interchangeable with this
origin: the backend intentionally rejects requests from other origins. Verify a
preview against its own configured backend origin before promoting that release.

After pushing the verified release, check the deployed commit, successful frontend build, API
bootstrap, an actual cited answer, history after refresh, original sources,
stream reconnects and Live voice. A successful static deployment alone does not
establish that the application works.

The backend and managed database are described in
[deployment/README.md](../deployment/README.md). Hosting setup and final hosted
acceptance are still pending; no deployed URL is claimed here.
Actual account and preparation results are recorded in [hosting status](HOSTING_STATUS.md).

References: [Vercel programmatic configuration](https://vercel.com/docs/project-configuration/vercel-ts)
and [external rewrites and cache behavior](https://vercel.com/docs/routing/rewrites).

The deployment configuration keeps the backend origin as an explicit routing
reference to `EMER_BACKEND_ORIGIN`. `scripts/check-backend-origin.mjs` validates the
real value during the build, after Vercel has loaded project environment variables.
It rejects missing values, non-HTTPS URLs, credentials, paths and trailing slashes.
The routing rules preserve security headers, uncached APIs and the share-page SPA.
See [Vercel programmatic configuration](https://vercel.com/docs/project-configuration/vercel-ts).
