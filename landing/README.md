# Static landing page

Plain HTML, CSS and JS: serve the folder from any static host.

Before publishing, replace the `{{site url}}` placeholder (canonical, Open Graph and Twitter URLs, JSON-LD, `robots.txt`,
`sitemap.xml`) with the real origin, without a trailing slash:

```bash
SITE=https://your-domain.example
sed -i.bak "s|{{site url}}|$SITE|g" index.html robots.txt sitemap.xml && rm -f *.bak
```

`og.jpg` should be 1200x630. `{{repository url}}` in the quickstart is a second placeholder for your repository link.
