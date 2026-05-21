# Next.js config snippet for `.well-known/` CORS headers

Add this to `next.config.js` (or merge into your existing `headers()` function)
in the `agent.minebean.com` repo so the skill files are fetchable by Hermes
agents running on any host.

```js
/** @type {import('next').NextConfig} */
const nextConfig = {
  // ... your existing config ...

  async headers() {
    return [
      {
        source: '/.well-known/skills/:path*',
        headers: [
          { key: 'Access-Control-Allow-Origin', value: '*' },
          { key: 'Access-Control-Allow-Methods', value: 'GET, OPTIONS' },
          { key: 'Cache-Control', value: 'public, max-age=300' },
          // index.json gets served as application/json by default.
          // SKILL.md needs explicit content-type for some Hermes clients.
          { key: 'X-Content-Type-Options', value: 'nosniff' },
        ],
      },
      // Override Content-Type for .md files so Hermes parses as markdown.
      {
        source: '/.well-known/skills/:path*\\.md',
        headers: [
          { key: 'Content-Type', value: 'text/markdown; charset=utf-8' },
        ],
      },
    ];
  },
};

module.exports = nextConfig;
```

If your `next.config.js` already has a `headers()` function, just append the
two entries above to the array it returns. If it uses `next.config.mjs` (ES
module syntax), wrap in `export default` instead of `module.exports`.

## After adding

Commit + push. Vercel auto-deploys. Verify:

```bash
curl -s https://agent.minebean.com/.well-known/skills/index.json | jq
curl -s -I https://agent.minebean.com/.well-known/skills/mine-bean/SKILL.md
```

Expected from the second curl:
- `HTTP/2 200`
- `content-type: text/markdown; charset=utf-8`
- `access-control-allow-origin: *`
