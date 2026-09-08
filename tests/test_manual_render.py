# 매뉴얼 템플릿의 실제 렌더링과 내부 링크·HTML 구조를 검증합니다.
import unittest
import re
from html.parser import HTMLParser
from pathlib import Path

from jinja2 import ChoiceLoader, DictLoader, Environment, FileSystemLoader, StrictUndefined


class ManualRenderTest(unittest.TestCase):
    def test_manuals_render_with_balanced_markup_and_valid_links(self):
        root = Path(__file__).resolve().parents[1]
        env = Environment(loader=ChoiceLoader([
            DictLoader({"base.html": "{% block content %}{% endblock %}"}),
            FileSystemLoader(root / "templates"),
        ]), undefined=StrictUndefined)
        env.globals["url_for"] = lambda endpoint, filename: "/bookoasis_mate/static/" + filename

        class Markup(HTMLParser):
            def __init__(self):
                super().__init__()
                self.stack, self.links, self.ids = [], [], set()

            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if tag not in {"link", "img", "input", "br", "hr", "meta"}:
                    self.stack.append(tag)
                if attrs.get("id"):
                    assert attrs["id"] not in self.ids, attrs["id"]
                    self.ids.add(attrs["id"])
                if attrs.get("href"):
                    self.links.append(attrs["href"])
                if attrs.get("src"):
                    self.links.append(attrs["src"])

            def handle_endtag(self, tag):
                assert self.stack and self.stack.pop() == tag, tag

        for path in sorted((root / "templates").glob("*manual*.html")):
            with self.subTest(template=path.name):
                parser = Markup()
                parser.feed(env.get_template(path.name).render())
                self.assertEqual([], parser.stack)
                for link in parser.links:
                    if link.startswith("#"):
                        self.assertIn(link[1:], parser.ids)
                    elif link.startswith("/bookoasis_mate/static/"):
                        self.assertTrue((root / "static" / link.split("/static/", 1)[1].split("?", 1)[0]).is_file())
                    elif link.startswith("/bookoasis_mate/"):
                        module, page = link.removeprefix("/bookoasis_mate/").split("/")
                        self.assertTrue((root / ("mod_" + module + ".py")).is_file())
                        self.assertTrue((root / "templates" / ("bookoasis_mate_" + module + "_" + page + ".html")).is_file())

        manual = Markup()
        manual.feed(env.get_template("bookoasis_mate_manual.html").render())
        for name in ("bookoasis_mate_setting.html", "bookoasis_mate_plugins.html"):
            source = (root / "templates" / name).read_text(encoding="utf-8")
            for anchor in re.findall(r'href="/bookoasis_mate/manual#([^"]+)"', source):
                self.assertIn(anchor, manual.ids, name)
