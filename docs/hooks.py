"""MkDocs hook: inject JSON-LD schema markup into homepage <head>."""

_SCHEMA_JSON_LD = """\
<script type="application/ld+json">
  {
    "@context": "https://schema.org",
    "@graph": [
      {
        "@type": "Organization",
        "name": "AI-Rig by Borda",
        "url": "https://borda.github.io/AI-Rig/",
        "description": "Five Claude Code plugins for Python/ML OSS development.",
        "sameAs": ["https://github.com/Borda/AI-Rig"]
      },
      {
        "@type": "WebSite",
        "name": "Borda's AI-Rig",
        "url": "https://borda.github.io/AI-Rig/",
        "description": "Claude Code plugin suite for Python/ML OSS development",
        "potentialAction": {
          "@type": "SearchAction",
          "target": {
            "@type": "EntryPoint",
            "urlTemplate": "https://borda.github.io/AI-Rig/search/?q={search_term_string}"
          },
          "query-input": "required name=search_term_string"
        }
      },
      {
        "@type": "SoftwareApplication",
        "name": "Borda's AI-Rig",
        "applicationCategory": "DeveloperApplication",
        "operatingSystem": "macOS, Linux, Windows",
        "description": "Five Claude Code plugins — foundry, oss, develop, research, codemap — for Python/ML OSS development. Specialist agents, calibrated workflows, validate-first discipline.",
        "url": "https://borda.github.io/AI-Rig/",
        "downloadUrl": "https://github.com/Borda/AI-Rig",
        "offers": {
          "@type": "Offer",
          "price": "0",
          "priceCurrency": "USD"
        },
        "author": {
          "@type": "Person",
          "name": "Jiri Borovec",
          "url": "https://github.com/Borda"
        },
        "featureList": [
          "8 specialist Claude Code agents with calibrated recall thresholds",
          "Validate-first development workflows with mandatory test gates",
          "Multi-agent parallel PR review (6 specialist lenses in parallel)",
          "SemVer-enforced release management with changelog generation",
          "Structured ML experiment pipeline with auto-rollback on regression",
          "Python codebase structural indexer with blast-radius metrics"
        ],
        "hasPart": [
          {
            "@type": "SoftwareApplication",
            "name": "foundry",
            "description": "Base infrastructure plugin: 8 specialist agents, calibration, audit, self-improvement loop.",
            "url": "https://borda.github.io/AI-Rig/foundry/"
          },
          {
            "@type": "SoftwareApplication",
            "name": "oss",
            "description": "OSS maintainer workflows: parallel PR review, resolve feedback, SemVer releases.",
            "url": "https://borda.github.io/AI-Rig/oss/"
          },
          {
            "@type": "SoftwareApplication",
            "name": "develop",
            "description": "Validate-first development: feature, fix, refactor, debug with mandatory test gates.",
            "url": "https://borda.github.io/AI-Rig/develop/"
          },
          {
            "@type": "SoftwareApplication",
            "name": "research",
            "description": "Structured ML experiments: SOTA search, design review, automated improvement loop.",
            "url": "https://borda.github.io/AI-Rig/research/"
          },
          {
            "@type": "SoftwareApplication",
            "name": "codemap",
            "description": "Python structural indexer: import graph, blast-radius metrics, function call graph.",
            "url": "https://borda.github.io/AI-Rig/codemap/"
          }
        ]
      }
    ]
  }
</script>"""


def on_post_page(output, page, config):
    """Inject JSON-LD schema markup into homepage <head>."""
    if page.url in ("", ".", "./"):
        return output.replace("</head>", _SCHEMA_JSON_LD + "\n</head>", 1)
    return output
