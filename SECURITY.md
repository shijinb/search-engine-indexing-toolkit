# Security Policy

Report vulnerabilities privately through GitHub's **Report a vulnerability** (Security → Advisories). Expect an acknowledgement within 7 days.

In scope: anything that could leak credentials (API keys, service-account tokens), submit URLs for hosts the user did not allow, bypass the dry-run/confirmation step, or let a hostile sitemap exhaust memory or read local files.

Operational guidance:
- Keep credentials in the environment or a secret manager; never pass them as arguments or commit `.env` / service-account files (both are git-ignored).
- Give the Google service account access to only the Search Console properties it needs.
- Rotate the IndexNow key if it leaks: change the key file and `INDEXNOW_KEY` together.
