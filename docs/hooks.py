"""Keep repository documentation links local and add homepage metadata.

Purpose: Build the site from the same Markdown that readers browse in the repository.

Scope: Product wrappers include one plugin README; links resolve beside that source, not beside the wrapper. Reachable
repository files are published under repository/.

Usage: MkDocs loads this module through the hooks setting in mkdocs.yml. The file collection hook discovers linked pages
before MkDocs validates or renders links.

Outputs: In-memory page content and additional MkDocs files, plus homepage JSON-LD. Source Markdown is never modified.
Existing product URLs remain unchanged.

Failure: Missing relative targets remain visible to MkDocs strict link validation; paths outside the checkout are never
added to the site. External URLs are retained.

Used by: Local MkDocs builds, the documentation CI workflow, and docs link regressions.
"""

import posixpath
import re
from collections.abc import Callable
from functools import partial
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit, urlunsplit
from xml.etree.ElementTree import Element

from markdown import Markdown
from markdown.extensions import Extension
from markdown.treeprocessors import Treeprocessor
from mkdocs.config.defaults import MkDocsConfig
from mkdocs.structure.files import File, Files, InclusionLevel
from mkdocs.structure.pages import Page

_README_INCLUDE = re.compile(r'^--8<-- "(plugins/[^"\n]+/README\.md)"\s*$', re.MULTILINE)
_sources: dict[str, Path] = {}
_published: dict[Path, File] = {}


class _LocalLinks(Treeprocessor):
    """Translate parsed links without rewriting code samples or Markdown syntax."""

    def __init__(self, rewrite: Callable[[str], str]) -> None:
        """Bind the source-aware URL translator for one document."""
        super().__init__()
        self.rewrite = rewrite

    def run(self, root: Element) -> Element:
        """Rebase Markdown anchors and images before MkDocs validates their targets."""
        for element in root.iter():
            key = {"a": "href", "img": "src"}.get(element.tag)
            if key and element.get(key):
                element.set(key, self.rewrite(element.get(key)))
        return root


class _LinkExtension(Extension):
    """Install source-aware link translation ahead of MkDocs URL conversion."""

    def __init__(self, rewrite: Callable[[str], str]) -> None:
        """Retain the translator for this Markdown conversion."""
        super().__init__()
        self.rewrite = rewrite

    def extendMarkdown(self, md: Markdown) -> None:
        """Run after inline parsing and before MkDocs' priority-zero link handler."""
        md.treeprocessors.register(_LocalLinks(self.rewrite), "repository_links", 1)


def _rewrite_link(url: str, *, source: Path, page: File, files: Files, config: MkDocsConfig) -> str:
    """Publish a reachable local target and return its page-relative source URI."""
    parts = urlsplit(url)
    root = Path(config.config_file_path).resolve().parent
    repo_url = config.repo_url.rstrip("/")
    prefixes = (f"{repo_url}/blob/main/", f"{repo_url}/tree/main/")
    prefix = next((prefix for prefix in prefixes if url.startswith(prefix)), None)
    if prefix:
        target = root / unquote(urlsplit(url[len(prefix) :]).path)
    elif parts.scheme or parts.netloc or not parts.path or parts.path.startswith("/"):
        return url
    else:
        target = source.parent / unquote(parts.path)
    target = target.resolve()
    if target.is_dir():
        target = (target / "README.md").resolve()
    if not target.is_relative_to(root):
        return url
    if not target.is_file():
        return url
    if target not in _published:
        uri = "repository/" + target.relative_to(root).as_posix()
        added = File.generated(config, uri, abs_src_path=str(target), inclusion=InclusionLevel.NOT_IN_NAV)
        files.append(added)
        _sources[uri] = target
        _published[target] = added
    relative = posixpath.relpath(_published[target].src_uri, posixpath.dirname(page.src_uri) or ".")
    return urlunsplit(("", "", quote(relative, safe="/"), parts.query, parts.fragment))


def on_files(files: Files, config: MkDocsConfig) -> Files:
    """Expand product wrappers and collect the linked repository files before rendering."""
    _sources.clear()
    _published.clear()
    root = Path(config.config_file_path).resolve().parent
    for file in files:
        source = Path(file.abs_src_path).resolve()
        _published[source] = file
        if file.is_documentation_page():
            content = file.content_string
            match = _README_INCLUDE.search(content)
            if match:
                source = (root / match[1]).resolve()
                if not source.is_relative_to(root):
                    raise ValueError(f"README include leaves the repository: {match[1]}")
                file.content_string = (
                    content[: match.start()] + source.read_text(encoding="utf-8") + content[match.end() :]
                )
                # The content setter clears provenance; revision-date plugins
                # still need the actual README path for their Git lookup.
                file.abs_src_path = str(source)
        _sources[file.src_uri] = source
        _published[source] = file

    # Discover through the actual Markdown parser: reference links work, while
    # examples in fenced code do not accidentally become published files.
    visited: set[str] = set()
    while pending := [file for file in files.documentation_pages() if file.src_uri not in visited]:
        for file in pending:
            visited.add(file.src_uri)
            rewrite = partial(_rewrite_link, source=_sources[file.src_uri], page=file, files=files, config=config)
            extensions = [ext for ext in config.markdown_extensions if not isinstance(ext, _LinkExtension)]
            Markdown(extensions=[*extensions, _LinkExtension(rewrite)], extension_configs=config.mdx_configs).convert(
                file.content_string
            )
    return files


def on_page_markdown(markdown: str, page: Page, config: MkDocsConfig, files: Files) -> str:
    """Resolve this page's links using the original repository source directory."""
    rewrite = partial(_rewrite_link, source=_sources[page.file.src_uri], page=page.file, files=files, config=config)
    config.markdown_extensions = [ext for ext in config.markdown_extensions if not isinstance(ext, _LinkExtension)]
    config.markdown_extensions.append(_LinkExtension(rewrite))
    return markdown


_SCHEMA_JSON_LD = """\
<script type="application/ld+json">
  {
    "@context": "https://schema.org",
    "@graph": [
      {
        "@type": "Organization",
        "name": "AI-Rig by Borda",
        "url": "https://borda.github.io/AI-Rig/",
        "description": "Six Claude Code plugins and Codex Rig for Python/ML OSS development.",
        "sameAs": ["https://github.com/Borda/AI-Rig"]
      },
      {
        "@type": "WebSite",
        "name": "Borda's AI-Rig",
        "url": "https://borda.github.io/AI-Rig/",
        "description": "Claude Code and OpenAI Codex plugin suite for Python/ML OSS development",
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
        "description": "Six Claude Code plugins plus Codex Rig for Python/ML OSS development. Specialist roles, calibrated workflows, and validate-first discipline.",
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
          "16 specialist Claude Code agents across independently installable plugins",
          "Validate-first Python development with explicit reproduction and acceptance gates",
          "Evidence-backed issue, pull request, feedback-resolution, and release-readiness workflows",
          "Reviewable ML research planning, execution, verification, ablation, and retrospective workflows",
          "Static Python indexing, structural queries, test-impact analysis, and reference renames",
          "13 Codex workflows, one lifecycle manager, 15 role cards, and shared artifact gates",
          "Bidirectional Claude Code and Codex bridge for bounded implement, advise, and review calls"
        ],
        "hasPart": [
          {
            "@type": "SoftwareApplication",
            "name": "foundry",
            "description": "Claude configuration and workflow maintenance: 11 skills, 10 specialist agents, rules, hooks, audit, calibration, and reviewed instruction distillation.",
            "url": "https://borda.github.io/AI-Rig/cc_foundry/"
          },
          {
            "@type": "SoftwareApplication",
            "name": "oss",
            "description": "Five OSS maintainer skills for analysis, PR review, feedback resolution, release-readiness assessment, and setup.",
            "url": "https://borda.github.io/AI-Rig/cc_oss/"
          },
          {
            "@type": "SoftwareApplication",
            "name": "develop",
            "description": "Seven validate-first Python skills for planning, features, fixes, refactors, debugging, review, and setup.",
            "url": "https://borda.github.io/AI-Rig/cc_develop/"
          },
          {
            "@type": "SoftwareApplication",
            "name": "research",
            "description": "Ten reviewable ML research skills for literature, planning, methodology review, bounded execution, verification, ablation, retrospectives, Kaggle, and setup.",
            "url": "https://borda.github.io/AI-Rig/cc_research/"
          },
          {
            "@type": "SoftwareApplication",
            "name": "codemap-py",
            "description": "Six dual-runtime skills for static Python indexing, structural queries, test impact, reference renames, integration, and telemetry debriefs.",
            "url": "https://borda.github.io/AI-Rig/codemap-py/"
          },
          {
            "@type": "SoftwareApplication",
            "name": "Codex Rig",
            "description": "Thirteen evidence-first Codex workflows, one legacy-shim lifecycle manager, fifteen role cards, shared gates, and reviewable artifacts.",
            "url": "https://borda.github.io/AI-Rig/codex-rig/"
          },
          {
            "@type": "SoftwareApplication",
            "name": "bridge_CC-Codex",
            "description": "Bidirectional Claude Code and Codex bridge: bounded implement, advise, and review calls with explicit models, budgets, compact envelopes, and recursion safety.",
            "url": "https://borda.github.io/AI-Rig/bridge_cc-codex/"
          }
        ]
      }
    ]
  }
</script>"""


def on_post_page(output, page, config):
    """Insert site metadata before the homepage's first closing head tag.

    Treat ``""``, ``"."``, and ``"./"`` as homepage URLs. Other pages and HTML
    without the literal ``</head>`` tag pass through unchanged. ``config`` is
    accepted for the MkDocs hook interface and is unused.

    Examples:
        >>> from types import SimpleNamespace
        >>> on_post_page("<head></head>", SimpleNamespace(url="guide/"), None)
        '<head></head>'
        >>> html = on_post_page("<head></head>", SimpleNamespace(url=""), None)
        >>> html.count('type="application/ld+json"')
        1
        >>> on_post_page("<body>Hello</body>", SimpleNamespace(url=""), None)
        '<body>Hello</body>'
    """
    if page.url in ("", ".", "./"):
        return output.replace("</head>", _SCHEMA_JSON_LD + "\n</head>", 1)
    return output
