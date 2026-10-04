"""Check repository links after README content moves into the documentation site."""

import runpy
from pathlib import Path
from types import SimpleNamespace

import pytest
from mkdocs.commands.build import build
from mkdocs.config import load_config


@pytest.mark.integration
def test_included_readme_links_build_locally(tmp_path):
    """Keep nested guides, assets, reference links, and product links inside the site."""
    docs = tmp_path / "docs"
    plugin = tmp_path / "plugins" / "example"
    docs.mkdir()
    (plugin / "shared").mkdir(parents=True)
    (docs / "index.md").write_text("# Home\n\n[Product](product.md)\n", encoding="utf-8")
    (docs / "product.md").write_text('--8<-- "plugins/example/README.md"\n', encoding="utf-8")
    (plugin / "README.md").write_text(
        '# Product\n\n[Guide][guide]\n\n[guide]: shared/guide.md#details "Guide title"\n\n'
        "[Same guide](https://github.com/Borda/AI-Rig/blob/main/plugins/example/shared/guide.md#details)\n\n"
        "![Image](shared/picture.svg)\n\n[External](https://example.com/guide.md)\n\n"
        "```text\n[Example](not-a-real-file.md)\n```\n",
        encoding="utf-8",
    )
    (plugin / "shared" / "guide.md").write_text(
        "# Guide\n\n## Details\n\n[Back](../README.md)\n\n[Next](next.md)\n\n"
        "[Wrapper](../../../docs/product.md?view=full#product)\n",
        encoding="utf-8",
    )
    (plugin / "shared" / "next.md").write_text("# Next\n\n[Guide](guide.md)\n", encoding="utf-8")
    (plugin / "shared" / "picture.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"/>', encoding="utf-8")
    hook = Path(__file__).resolve().parents[1] / "docs" / "hooks.py"
    config_path = tmp_path / "mkdocs.yml"
    config_path.write_text(
        "site_name: Test\nsite_url: https://example.com/project/\n"
        "repo_url: https://github.com/Borda/AI-Rig\n"
        f"hooks: [{hook.as_posix()}]\nplugins: []\nnav:\n  - Home: index.md\n  - Product: product.md\n",
        encoding="utf-8",
    )
    build(load_config(str(config_path), strict=True))
    product = (tmp_path / "site" / "product" / "index.html").read_text(encoding="utf-8")
    guide = tmp_path / "site" / "repository" / "plugins" / "example" / "shared" / "guide" / "index.html"
    assert product.count('href="../repository/plugins/example/shared/guide/#details"') == 2
    assert 'src="../repository/plugins/example/shared/picture.svg"' in product
    assert 'href="https://example.com/guide.md"' in product
    assert "[Example](not-a-real-file.md)" in product
    assert 'href="../../../../../product/"' in guide.read_text(encoding="utf-8")
    assert 'href="../../../../../product/?view=full#product"' in guide.read_text(encoding="utf-8")
    assert (guide.parent.parent / "next" / "index.html").exists()
    assert (guide.parent.parent / "picture.svg").exists()
    assert not (tmp_path / "site" / "repository" / "docs" / "product").exists()


def test_homepage_metadata_is_preserved():
    """Keep the existing JSON-LD injection while adding documentation link handling."""
    hooks = runpy.run_path(str(Path(__file__).resolve().parents[1] / "docs" / "hooks.py"))
    output = hooks["on_post_page"]("<head></head>", SimpleNamespace(url=""), None)
    assert output.count('type="application/ld+json"') == 1
