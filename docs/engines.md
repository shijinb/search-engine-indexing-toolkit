# Search engine APIs: what each one actually does

Verified against the official documentation on **2026-10-25**. APIs change, so check the linked pages before relying on a limit.

> None of these APIs index a page. A successful response means the request was **received**. Whether and when a page is crawled and indexed is decided by the search engine, based on its own quality and crawl systems.

## Summary

| Integration | What you send | Scope | Limits (documented defaults) | Auth | indexkit command |
|---|---|---|---|---|---|
| **IndexNow** | Up to 10,000 URLs per request | Any URL on a host you control | Rate limits not published; 429 means slow down | Key file hosted on your site | `submit indexnow` |
| **Bing Webmaster URL Submission API** | Up to 500 URLs per batch | URLs of a site verified in Bing Webmaster Tools | Daily quota per site (`GetUrlSubmissionQuota`) | API key | `submit bing` |
| **Google Search Console API: sitemaps.submit** | One **sitemap** URL | A verified Search Console property | 20 QPS / 200 QPM per user for this resource | OAuth 2.0 service account, `webmasters` scope | `gsc-sitemap` |
| **Google Indexing API** | One URL per publish call | **Only** pages with `JobPosting` or `BroadcastEvent` (in a `VideoObject`) | 200 publish requests/day per project by default | Service account, `indexing` scope, owner in Search Console | `submit google-indexing` (requires `--confirm-indexing-api-eligible`) |

**Google has no general-purpose URL submission API.** For ordinary pages, submit or update the sitemap via Search Console, keep `<lastmod>` accurate, and let Google crawl. The Indexing API is restricted to job posting and livestream pages, and using it outside that scope can lead to access being revoked.

## IndexNow

Source: [indexnow.org/documentation](https://www.indexnow.org/documentation)

- Request: `POST https://<engine>/indexnow` with JSON `{"host", "key", "keyLocation", "urlList"}`. indexkit uses `https://api.indexnow.org/indexnow`; participating engines share submissions with each other.
- Key: 8–128 characters of `a-z A-Z 0-9 -`. Publish it as a UTF-8 text file, by default `https://<host>/<key>.txt`. indexkit checks the file before an `--execute` run and stops if it is missing or different.
- All URLs in a request must belong to `host`. indexkit refuses to send mixed-host batches.

| HTTP | Meaning | indexkit status |
|---|---|---|
| 200 | URL(s) received | accepted |
| 202 | Received, key validation pending | accepted |
| 400 | Invalid format | rejected |
| 403 | Key not valid (file missing, or key not in file) | rejected |
| 422 | URLs don't belong to the host, or key doesn't match the schema | rejected |
| 429 | Too many requests (potential spam) | failed (retried with backoff) |

## Bing Webmaster Tools URL Submission API

Source: [SubmitUrlBatch](https://learn.microsoft.com/en-us/dotnet/api/microsoft.bing.webmaster.api.interfaces.iwebmasterapi.submiturlbatch)

- Request: `POST https://ssl.bing.com/webmaster/api.svc/json/SubmitUrlbatch?apikey=KEY` with `{"siteUrl", "urlList"}`.
- At most 500 URLs per batch and never more than the remaining quota. indexkit reads `GetUrlSubmissionQuota` first and marks the rest `skipped`.
- The API key is a query parameter by design. indexkit passes it through `params` so it never appears in logged URLs.
- Microsoft now points site owners to IndexNow; this integration is for sites already using a Webmaster API key.

## Google Search Console API: sitemaps

Sources: [sitemaps.submit](https://developers.google.com/webmaster-tools/v1/sitemaps/submit), [usage limits](https://developers.google.com/webmaster-tools/limits)

- Request: `PUT https://www.googleapis.com/webmasters/v3/sites/{siteUrl}/sitemaps/{feedpath}`, no body, scope `https://www.googleapis.com/auth/webmasters`.
- `siteUrl` is the property exactly as in Search Console: `https://www.example.com/` (URL-prefix) or `sc-domain:example.com` (domain property). Both path parts are URL-encoded.
- The service account's e-mail must be added as an **Owner** of the property in Search Console.

## Google Indexing API

Sources: [quickstart](https://developers.google.com/search/apis/indexing-api/v3/quickstart), [quota](https://developers.google.com/search/apis/indexing-api/v3/quota-pricing)

- Request: `POST https://indexing.googleapis.com/v3/urlNotifications:publish` with `{"url", "type": "URL_UPDATED" | "URL_DELETED"}`, scope `https://www.googleapis.com/auth/indexing`.
- Supported content: pages with `JobPosting`, or `BroadcastEvent` embedded in a `VideoObject`. Nothing else.
- Default quota: 200 publish requests per day per project (resets at midnight Pacific time); 380 requests/minute across endpoints.
- Google applies spam detection to submissions; abuse, including using several accounts to exceed quota, can lead to revoked access.
- indexkit refuses to construct this submitter unless `--confirm-indexing-api-eligible` is passed, and stops at the configured daily quota.

## Sitemaps protocol

Source: [sitemaps.org/protocol.html](https://www.sitemaps.org/protocol.html)

- At most 50,000 URLs and 50 MB (52,428,800 bytes) uncompressed per sitemap file; gzip allowed; UTF-8; entity-escaped values.
- Sitemap index files group sitemaps; indexkit follows them to `--max-depth` (default 3).
- indexkit warns about files over 50,000 URLs and refuses files over 50 MB, before or after decompression.
