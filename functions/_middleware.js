// Canonical-host redirect.
//
// Cloudflare Pages serves every project on `<project>.pages.dev` as well as on
// each of its custom domains, and that alias cannot be turned off. Once the site
// is public it would therefore answer at three addresses with byte-identical
// content — enough for a reader to cite, or a search engine to index, the wrong
// one.
// `site/` already carries <link rel=canonical>, which settles the search-engine
// half; this settles the human half by making the wrong address bounce.
//
// It has to be a Function: `_redirects` cannot match on hostname. Cloudflare
// states so explicitly ("Domain-level redirects ❌"), because its source field
// is a path, never a full URL.
//
// ⚠️ PREVIEW DEPLOYMENTS MUST NOT REDIRECT. They are also `*.co2gap.pages.dev`,
// and bouncing them to production would silently defeat the whole point of
// previewing an unreleased build — you would look at the live site believing it
// was the preview. Hence an exact-match list and not a `.endsWith('.pages.dev')`
// test: `co2gap.pages.dev` is production, `<hash>.co2gap.pages.dev` is not.

const CANONICAL_ORIGIN = "https://co2gap.org";

// Exact hostnames to bounce.
//
// `www.co2gap.org` is here for the same reason as the pages.dev alias and not a
// different one: it is a second address serving byte-identical content. It must
// stay attached as a custom domain on the Pages project — detaching it would
// stop the name resolving instead of redirecting it, and the request would
// never reach this code.
const REDIRECT_FROM = new Set([
  "co2gap.pages.dev",
  "www.co2gap.pages.dev",
  "www.co2gap.org",
]);

export async function onRequest(context) {
  const url = new URL(context.request.url);

  if (!REDIRECT_FROM.has(url.hostname)) {
    return context.next();
  }

  const target = CANONICAL_ORIGIN + url.pathname + url.search;

  // 301: the alias is never going to become the canonical address, so the
  // browser and the crawler are both entitled to remember this.
  return Response.redirect(target, 301);
}
